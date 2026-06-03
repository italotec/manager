"""
agent_core.py — shared WebSocket agent logic.

Imported by both agent.py (CLI) and agent_gui.py (GUI/exe).

Call init(adspower_client, debug_dir) before use so the module knows
which AdsPower instance to talk to and where to drop debug screenshots.
"""
import asyncio
import base64
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import websockets
import websockets.exceptions

# ── Module-level init ─────────────────────────────────────────────────────────

_client = None          # AdsPowerClient — set by init()
_debug_dir: Path = Path("debug")

_BMS_WS_BASE = "wss://manager.verifywaba.store"
_MAX_WORKER_THREADS = 32
_INACTIVE_THRESHOLD = 3


def init(adspower_client, debug_dir):
    """Call once from the shell script (agent.py / agent_gui.py) before connecting."""
    global _client, _debug_dir
    _client = adspower_client
    _debug_dir = Path(debug_dir)


# ── Job-level cancellation ────────────────────────────────────────────────────

_cancel_flags: dict[int, threading.Event] = {}
_cancel_lock = threading.Lock()

# Thread-local current job/run ids — used by the milestone patch mid-run
_milestone_job_id = threading.local()
_milestone_run_id = threading.local()

# ── WS-based gerador acquire ──────────────────────────────────────────────────

_ws_loop: asyncio.AbstractEventLoop | None = None
_ws_outbox: asyncio.Queue | None = None
_acquire_futures: dict[str, dict] = {}
_acquire_futures_lock = threading.Lock()


def _handle_acquire_run_result(msg: dict):
    request_id = msg.get("request_id", "")
    if not request_id:
        return
    with _acquire_futures_lock:
        entry = _acquire_futures.get(request_id)
    if entry:
        entry["result"] = msg
        entry["event"].set()


def _acquire_run_id_via_ws() -> int:
    """Request a CNPJRun over the authenticated WS — no HTTP, no token needed."""
    import uuid as _uuid
    request_id = _uuid.uuid4().hex
    event = threading.Event()
    with _acquire_futures_lock:
        _acquire_futures[request_id] = {"event": event, "result": None}

    if _ws_loop is None or _ws_outbox is None:
        with _acquire_futures_lock:
            _acquire_futures.pop(request_id, None)
        raise RuntimeError("WS não conectado — não é possível adquirir run_id")

    print(f"[ACQ:1] enqueue request_id={request_id[:8]}", flush=True)
    _ws_loop.call_soon_threadsafe(
        _ws_outbox.put_nowait,
        json.dumps({"type": "acquire_run_request", "request_id": request_id}),
    )
    print(f"[ACQ:1] enqueued; waiting up to 600s request_id={request_id[:8]}", flush=True)

    if not event.wait(timeout=600):
        with _acquire_futures_lock:
            _acquire_futures.pop(request_id, None)
        print(f"[ACQ:7] TIMEOUT request_id={request_id[:8]}", flush=True)
        raise TimeoutError("acquire_run_request expirou após 10 minutos")

    with _acquire_futures_lock:
        entry = _acquire_futures.pop(request_id, {})
    result = (entry.get("result") or {})
    print(f"[ACQ:7] event resolved request_id={request_id[:8]} result_keys={list(result.keys())}", flush=True)
    if "error" in result:
        raise RuntimeError(f"Gerador: {result['error']}")
    return result["run_id"]


# ── Browser-status tracking ───────────────────────────────────────────────────

_open_pids: set[str] = set()
_inactive_count: dict[str, int] = {}
_status_outboxes: list[asyncio.Queue] = []
_status_outboxes_lock = threading.Lock()

_delete_queue: asyncio.Queue = asyncio.Queue()
_delete_worker_task: asyncio.Task | None = None
_pinger_task: asyncio.Task | None = None


# ── Screenshot capture ────────────────────────────────────────────────────────

def _capture_screenshot_b64(since_epoch: float) -> str:
    if not _debug_dir.exists():
        return ""
    candidates = [p for p in _debug_dir.rglob("*.png") if p.stat().st_mtime >= since_epoch]
    if not candidates:
        candidates = list(_debug_dir.rglob("*.png"))
    if not candidates:
        return ""
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return base64.b64encode(latest.read_bytes()).decode()


# ── Gerador block helpers ─────────────────────────────────────────────────────

def _parse_gerador_block(remark: str, marker: str) -> dict | None:
    """Return the parsed root JSON object (flat dict or {'runs':[...]}). None if absent."""
    if marker not in remark:
        return None
    _, _, tail = remark.partition(marker)
    try:
        return json.loads(tail.strip())
    except Exception:
        return None


def _extract_run_entry(parsed: dict | None, sequence: int) -> dict:
    if not parsed:
        return {}
    if "runs" in parsed:
        for r in parsed.get("runs", []):
            if r.get("sequence") == sequence:
                return dict(r)
        return {}
    if sequence == 1:
        return dict(parsed)
    return {}


def _extract_email_mode(parsed: dict | None) -> str:
    if not parsed:
        return "own"
    if "runs" in parsed:
        for r in parsed.get("runs", []):
            if r.get("email_mode"):
                return r["email_mode"]
        return "own"
    return parsed.get("email_mode", "own")


# ── Profile remark builder (for create_profiles) ──────────────────────────────

_REMARK_FIELDS = [
    ("id",             "ID"),
    ("email",          "E-mail"),
    ("password",       "Senha"),
    ("email_password", "Senha do E-mail"),
    ("fakey",          "2FA"),
]


def _build_profile_remark(acc: dict) -> str:
    return "\n".join(f"{label}: {acc[k]}" for k, label in _REMARK_FIELDS if acc.get(k))


# ── FacebookBot wizard-entry & milestone tracker ──────────────────────────────

_bot_tracker = threading.local()


def _install_bot_tracker():
    from services.facebook_bot import FacebookBot
    if getattr(FacebookBot, "_agent_tracker_installed", False):
        return

    _orig_init = FacebookBot.__init__
    def _tracked_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        _bot_tracker.last = self
    FacebookBot.__init__ = _tracked_init

    _orig_mark_step_done = FacebookBot._mark_step_done
    def _milestone_mark_step_done(self, step, value=True):
        _orig_mark_step_done(self, step, value)
        job_id = getattr(_milestone_job_id, "value", None)
        if job_id is None or _ws_loop is None or _ws_outbox is None:
            return
        if step == "waba_done":
            frame = json.dumps({
                "type":   "job_milestone",
                "job_id": job_id,
                "step":   "waba_created",
            })
            try:
                _ws_loop.call_soon_threadsafe(_ws_outbox.put_nowait, frame)
                print(f"[MILESTONE] job_id={job_id} waba_created sent to VPS")
            except Exception as _me:
                print(f"[MILESTONE] Failed to send waba_created frame: {_me}")
        elif step == "domain_reached":
            run_id = getattr(_milestone_run_id, "value", None)
            frame = json.dumps({
                "type":   "job_milestone",
                "job_id": job_id,
                "run_id": run_id,
                "step":   "domain_used",
            })
            try:
                _ws_loop.call_soon_threadsafe(_ws_outbox.put_nowait, frame)
                print(f"[MILESTONE] job_id={job_id} run_id={run_id} domain_used sent to VPS")
            except Exception as _me:
                print(f"[MILESTONE] Failed to send domain_used frame: {_me}")
    FacebookBot._mark_step_done = _milestone_mark_step_done

    FacebookBot._agent_tracker_installed = True


# ── Job execution ─────────────────────────────────────────────────────────────

def _execute_job_sync(job: dict, log=print, progress=None) -> dict:
    """
    Execute a verification job synchronously (called via asyncio.to_thread).

    log      — callable(str); defaults to print so agent.py needs no changes.
    progress — optional callable(str) for mid-job job_progress WS frames.
    """
    job_id      = job["id"]
    profile_id  = job["profile_id"]
    business_id = job.get("business_id", "")
    sms_payload = job.get("sms")
    sequence    = int(job.get("sequence") or 1)

    _milestone_job_id.value = job_id

    cancel_event = threading.Event()
    with _cancel_lock:
        _cancel_flags[job_id] = cancel_event

    def _progress(msg: str):
        log(f"[JOB {job_id}] {msg}")
        if progress:
            progress(msg)

    if cancel_event.is_set():
        with _cancel_lock:
            _cancel_flags.pop(job_id, None)
        return {"type": "job_cancelled", "job_id": job_id, "success": False,
                "message": "Cancelado antes de iniciar", "step_name": "", "page_url": "",
                "page_html": "", "traceback": "", "screenshot_b64": ""}

    _progress(f"Iniciando para perfil {profile_id}…")

    success         = False
    message         = ""
    screenshot_b64  = ""
    error_traceback = ""
    e               = None
    run_id          = None

    try:
        from main import _run_for_profile, _mark_verified, _mark_restricted
        from services.facebook_bot import BmRestrictedException
        import main as _main_mod
        _main_mod.adspower = _client  # use the instance discovered/configured by this process

        import config as _cfg
        parsed = _parse_gerador_block(
            (_client.get_profile(profile_id) or {}).get("remark", "") or "",
            _cfg.GERADOR_REMARK_MARKER,
        )

        persisted_entry = _extract_run_entry(parsed, sequence=sequence)
        run_id     = job.get("run_id") or persisted_entry.get("run_id")
        email_mode = persisted_entry.get("email_mode", "own")

        if run_id is None:
            _progress("Adquirindo dados do Gerador…")
            run_id = _acquire_run_id_via_ws()
            persisted_entry = {}

        _milestone_run_id.value = run_id

        if not business_id:
            business_id = persisted_entry.get("business_id") or ""

        gerador_data = dict(persisted_entry)
        gerador_data["run_id"]     = run_id
        gerador_data["email_mode"] = email_mode
        if business_id:
            gerador_data["business_id"] = business_id
        gerador_data.pop("sequence", None)

        profile = _client.get_profile(profile_id)

        _progress("Executando verificação no Facebook…")
        start_time = time.time()

        _install_bot_tracker()
        _bot_tracker.last = None

        success = _run_for_profile(
            profile=profile,
            run_id=run_id,
            email_mode=email_mode,
            sms_payload=sms_payload,
            business_id=business_id,
            gerador_data=gerador_data,
            sequence=sequence,
        )

        _last_bot = getattr(_bot_tracker, "last", None)
        if success and _last_bot is not None and not getattr(_last_bot, "wizard_entered", False):
            success = False
            message = "Verificação falhou — wizard da central de segurança não foi aberto"
        elif success:
            _mark_verified(profile_id)
            message = "Verificação concluída com sucesso!"
        else:
            message = "Verificação falhou."

        screenshot_b64 = _capture_screenshot_b64(since_epoch=start_time)

    except Exception as _exc:  # noqa: BLE001
        from services.facebook_bot import BmRestrictedException
        import traceback as _tb
        e = _exc
        message = str(_exc)[:500]
        error_traceback = _tb.format_exc()
        if isinstance(_exc, BmRestrictedException):
            log(f"[JOB {job_id}] BM Restrita: {_exc}")
            try:
                from main import _mark_restricted
                _mark_restricted(profile_id)
            except Exception as _me:
                log(f"[JOB {job_id}] Could not mark as restricted: {_me}")
        else:
            log(f"[JOB {job_id}] Exceção: {_exc}")

    step_name = ""
    page_url  = ""
    page_html = ""
    try:
        from services.facebook_bot import VerificationStepError as _VSE
        cause = e if isinstance(e, _VSE) else getattr(e, "__cause__", None)
        if isinstance(cause, _VSE):
            step_name = cause.step
            page_url  = cause.page_url
            page_html = (cause.page_html or "")[:50000]
    except Exception:
        pass

    with _cancel_lock:
        _cancel_flags.pop(job_id, None)
    _milestone_job_id.value = None
    _milestone_run_id.value = None

    if cancel_event.is_set():
        log(f"[JOB {job_id}] ✗ Cancelado")
        return {"type": "job_cancelled", "job_id": job_id, "success": False,
                "message": "Cancelado manualmente", "step_name": "", "page_url": "",
                "page_html": "", "traceback": "", "screenshot_b64": screenshot_b64}

    log(f"[JOB {job_id}] {'✓ Sucesso' if success else '✗ Falha'}")
    return {
        "type":           "job_done",
        "job_id":         job_id,
        "success":        success,
        "message":        message,
        "run_id":         run_id,
        "step_name":      step_name,
        "page_url":       page_url,
        "page_html":      page_html,
        "traceback":      error_traceback if not success else "",
        "screenshot_b64": screenshot_b64,
    }


# ── Link-WABA execution ───────────────────────────────────────────────────────

def _execute_link_waba_sync(msg: dict, log=print) -> dict:
    waba_record_id   = msg["waba_record_id"]
    profile_id       = msg["profile_id"]
    business_id      = msg.get("business_id") or ""
    waba_id          = msg.get("waba_id") or ""
    waba_name        = msg.get("waba_name") or ""
    sequence         = int(msg.get("sequence") or 1)
    partner_biz_id   = msg["partner_business_id"]
    meta_token       = msg["meta_token"]
    manager_api_key  = msg["manager_api_key"]
    manager_base_url = msg.get("manager_base_url") or "https://manager.verifywaba.store"

    def _result(status: str, message: str = "", **extra) -> dict:
        return {
            "type":            "link_done",
            "waba_record_id":  waba_record_id,
            "status":          status,
            "message":         message,
            "waba_id":         waba_id,
            "business_id":     business_id,
            **extra,
        }

    log(f"[LINK {waba_record_id}] Iniciando vincular — profile={profile_id}")

    try:
        from services.adspower import connect_cdp_with_retry
        from services.facebook_bot import FacebookBot, BmRestrictedException
        from services.manager_api import register_business_manager
        from services.gerador_facade import GeradorService
        from services.sms_factory import get_sms_service
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return _result("error", f"Falha ao importar dependências: {exc}")

    import config as _cfg

    # Cheap business_id resolution from AdsPower remark — no browser needed.
    if not business_id:
        try:
            remark = (_client.get_profile(profile_id) or {}).get("remark", "") or ""
            parsed = _parse_gerador_block(remark, _cfg.GERADOR_REMARK_MARKER)
            if parsed:
                for r in (parsed.get("runs") or [parsed]):
                    if isinstance(r, dict):
                        if r.get("sequence", 1) == sequence and r.get("business_id"):
                            business_id = str(r["business_id"])
                            break
                if not business_id:
                    for r in (parsed.get("runs") or [parsed]):
                        if isinstance(r, dict) and r.get("business_id"):
                            business_id = str(r["business_id"])
                            break
        except Exception:
            pass

    try:
        browser_data = _client.open_browser(profile_id)
    except Exception as exc:
        return _result("error", f"Falha ao abrir perfil AdsPower: {exc}")

    ws_endpoint = (browser_data.get("ws") or {}).get("puppeteer", "")
    if not ws_endpoint:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass
        return _result("error", "Sem WebSocket endpoint do AdsPower")

    try:
        with sync_playwright() as p:
            browser, _ws = connect_cdp_with_retry(
                p, ws_endpoint,
                profile_id=profile_id,
                ads_client=_client,
            )
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()

            base_url = (
                f"https://business.facebook.com/latest/settings/whatsapp_account?business_id={business_id}"
                if business_id
                else "https://business.facebook.com/latest/settings/whatsapp_account"
            )
            page.goto(base_url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)

            gerador = GeradorService()
            sms = get_sms_service()
            bot = FacebookBot(
                ws_endpoint=ws_endpoint,
                run_data={},
                gerador=gerador,
                sms=sms,
                profile_user_id=profile_id,
                adspower_client=_client,
            )

            if not business_id:
                bid = bot._resolve_owning_business_id(page)
                if bid:
                    business_id = bid
                    log(f"[LINK {waba_record_id}] business_id={bid} resolvido live")
                else:
                    return _result("error", "Não foi possível resolver o business_id do perfil")

            if not waba_id:
                wid = bot._extract_waba_id_graphql(
                    page, business_id, expected_name=waba_name or None
                )
                if wid:
                    waba_id = wid
                    log(f"[LINK {waba_record_id}] waba_id={wid} extraído")
                else:
                    return _result("error", "Não foi possível identificar o ID da WABA")

            try:
                ok = bot._share_waba_graphql(page, business_id, partner_biz_id, waba_id)
            except BmRestrictedException as exc:
                log(f"[LINK {waba_record_id}] BM restrito: {exc}")
                return _result("restrita", str(exc))

            if not ok:
                return _result("error", f"Falha ao compartilhar WABA com BM parceiro (waba_id={waba_id})")

            log(f"[LINK {waba_record_id}] WABA compartilhada com partner={partner_biz_id}")

    except Exception as exc:
        import traceback as _tb
        log(f"[LINK {waba_record_id}] Exceção no browser: {exc}")
        print(_tb.format_exc(), flush=True)
        return _result("error", str(exc)[:500])
    finally:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass

    try:
        reg = register_business_manager(
            base_url=manager_base_url,
            api_key=manager_api_key,
            waba_id=waba_id,
            token=meta_token,
            adspower_profile_id=profile_id,
        )
        if not reg["ok"]:
            return _result("error", f"Manager API error: {reg.get('error')}")
        log(f"[LINK {waba_record_id}] Registrado no manager platform")
    except Exception as exc:
        return _result("error", f"Falha ao registrar no manager: {exc}")

    log(f"[LINK {waba_record_id}] ✓ Vinculado com sucesso")
    return _result("ok", shared=True, registered=True)


# ── Profile sync ──────────────────────────────────────────────────────────────

async def _sync_profiles(outbox: asyncio.Queue, log=print):
    try:
        import config as _cfg

        def _collect():
            group_data = _client._get("/api/v1/group/list", page=1, page_size=200)
            name_to_id = {
                g["group_name"]: str(g["group_id"])
                for g in group_data.get("list", [])
            }
            target = {_cfg.VERIFICAR_GROUP_NAME, _cfg.VERIFICADAS_GROUP_NAME}
            profiles = []
            for gname, gid in name_to_id.items():
                if gname not in target:
                    continue
                for p in _client.list_profiles(group_id=gid):
                    profiles.append({
                        "profile_id": p["user_id"],
                        "name":       p.get("name", ""),
                        "group_name": gname,
                        "remark":     p.get("remark", ""),
                    })
            return profiles

        profiles = await asyncio.to_thread(_collect)
        await outbox.put(json.dumps({"type": "profiles_push", "profiles": profiles}))
        log(f"[SYNC] {len(profiles)} perfis enviados ao VPS")
    except Exception as e:
        log(f"[SYNC] Falha: {e}")


# ── Message handlers ──────────────────────────────────────────────────────────

async def _handle_run_job(msg: dict, outbox: asyncio.Queue, log=print):
    job    = msg["job"]
    job_id = job["id"]

    await outbox.put(json.dumps({"type": "job_start", "job_id": job_id}))

    loop = asyncio.get_event_loop()
    def progress(message: str):
        frame = json.dumps({"type": "job_progress", "job_id": job_id, "message": message})
        loop.call_soon_threadsafe(outbox.put_nowait, frame)

    result = await asyncio.to_thread(_execute_job_sync, job, log, progress)
    await outbox.put(json.dumps(result))


async def _handle_link_waba(msg: dict, outbox: asyncio.Queue, log=print):
    waba_record_id = msg.get("waba_record_id")
    log(f"[LINK {waba_record_id}] Recebido link_waba do VPS")
    await outbox.put(json.dumps({"type": "link_start", "waba_record_id": waba_record_id}))
    result = await asyncio.to_thread(_execute_link_waba_sync, msg, log)
    await outbox.put(json.dumps(result))


def _execute_add_card_sync(msg: dict, log=print) -> dict:
    """
    Add a credit card to the Facebook billing of the profile's WABA.

    Modeled on _execute_link_waba_sync: open the AdsPower profile, attach
    Playwright over CDP, auto-discover the business_id, then run the verified
    card-add automation (facebook_card.add_card_via_cdp). Returns a card_result
    frame for the BMs WebSocket.
    """
    cmd_id      = msg.get("cmd_id")
    profile_id  = msg.get("profile_id", "")
    card        = msg.get("card", {}) or {}
    business_id = msg.get("business_id", "") or ""
    waba_id     = msg.get("waba_id", "") or ""

    def _result(ok: bool, **extra) -> dict:
        return {"type": "card_result", "cmd_id": cmd_id, "ok": ok, **extra}

    log(f"[CARD {profile_id}] Iniciando add_card (•••• {str(card.get('number',''))[-4:]})")

    try:
        from services.adspower import connect_cdp_with_retry
        from services.facebook_bot import FacebookBot
        from services.gerador_facade import GeradorService
        from services.sms_factory import get_sms_service
        from playwright.sync_api import sync_playwright
        import facebook_card
    except Exception as exc:
        return _result(False, error=f"Falha ao importar dependências: {exc}")

    try:
        browser_data = _client.open_browser(profile_id)
        _open_pids.add(profile_id)
    except Exception as exc:
        return _result(False, error=f"Falha ao abrir perfil AdsPower: {exc}")

    ws_endpoint = (browser_data.get("ws") or {}).get("puppeteer", "")
    if not ws_endpoint:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass
        return _result(False, error="Sem WebSocket endpoint do AdsPower")

    try:
        with sync_playwright() as p:
            browser, _ws = connect_cdp_with_retry(
                p, ws_endpoint,
                profile_id=profile_id,
                ads_client=_client,
            )
            ctx  = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()

            # Auto-discover the owning business_id from the live session if not given.
            if not business_id:
                try:
                    gerador = GeradorService()
                    sms     = get_sms_service()
                    bot = FacebookBot(
                        ws_endpoint=ws_endpoint,
                        run_data={},
                        gerador=gerador,
                        sms=sms,
                        profile_user_id=profile_id,
                        adspower_client=_client,
                    )
                    page.goto(
                        "https://business.facebook.com/latest/settings/whatsapp_account",
                        timeout=30000,
                    )
                    page.wait_for_load_state("networkidle", timeout=20000)
                    bid = bot._resolve_owning_business_id(page)
                    if bid:
                        business_id = bid
                        log(f"[CARD {profile_id}] business_id={bid} resolvido live")
                except Exception as exc:
                    log(f"[CARD {profile_id}] Falha ao resolver business_id: {exc}")

            if not business_id:
                return _result(False, error="Não foi possível resolver o business_id do perfil")

            res = facebook_card.add_card_via_cdp(page, card, business_id=business_id, waba_id=waba_id, log=log)
            return _result(
                bool(res.get("ok")),
                code=res.get("code"),
                credential_id=res.get("credential_id"),
                stage=res.get("stage"),
                error=res.get("error", "") or "",
            )

    except Exception as exc:
        import traceback as _tb
        log(f"[CARD {profile_id}] Exceção no browser: {exc}")
        print(_tb.format_exc(), flush=True)
        return _result(False, error=str(exc)[:500])
    finally:
        try:
            _client.close_browser(profile_id)
        except Exception:
            pass


async def _handle_add_card(msg: dict, outbox: asyncio.Queue, log=print):
    result = await asyncio.to_thread(_execute_add_card_sync, msg, log)
    await outbox.put(json.dumps(result))


async def _handle_cancel_job(msg: dict, outbox: asyncio.Queue, log=print):
    job_id = msg.get("job_id")
    if job_id is None:
        return
    with _cancel_lock:
        flag = _cancel_flags.get(job_id)
    if flag:
        flag.set()
        log(f"[JOB {job_id}] Cancel flag set — job will stop at next checkpoint")
    else:
        await outbox.put(json.dumps({"type": "job_cancelled", "job_id": job_id,
                                     "success": False, "message": "Cancelado (job não ativo)"}))


async def _handle_open_browser(msg: dict, log=print):
    profile_id = msg.get("profile_id", "")
    cmd_id     = msg.get("cmd_id")
    try:
        await asyncio.to_thread(_client.open_browser, profile_id)
        _open_pids.add(profile_id)
        log(f"[CMD] Browser aberto para {profile_id}")
    except Exception as e:
        log(f"[CMD] Erro ao abrir browser: {e}")
    return cmd_id


async def _handle_change_proxy(msg: dict, log=print):
    profile_id   = msg.get("profile_id", "")
    proxy_config = msg.get("proxy_config", {})
    try:
        await asyncio.to_thread(_client.update_profile, profile_id, user_proxy_config=proxy_config)
        log(f"[CMD] Proxy atualizado para {profile_id}")
    except Exception as e:
        log(f"[CMD] Erro ao atualizar proxy para {profile_id}: {e}")


async def _handle_create_profiles(msg: dict, outbox: asyncio.Queue, log=print):
    request_id = msg.get("request_id", "")
    group_name = (msg.get("group_name") or "").strip()
    accounts   = msg.get("accounts") or []
    proxies    = msg.get("proxies") or []

    total = len(accounts)

    def _emit_progress(done: int, ok: int, fail: int):
        if _ws_loop is None or _ws_outbox is None:
            return
        frame = json.dumps({
            "type":       "create_profiles_progress",
            "request_id": request_id,
            "done":       done,
            "total":      total,
            "ok":         ok,
            "fail":       fail,
        })
        try:
            _ws_loop.call_soon_threadsafe(_ws_outbox.put_nowait, frame)
        except Exception:
            pass

    def _do_batch():
        created, failed = [], []
        try:
            group_id = _client.get_group_id(group_name)
        except Exception as e:
            return [], [f"(grupo) {group_name} — {e}"]
        for i, acc in enumerate(accounts):
            proxy_cfg = acc.get("proxy_config")
            if proxy_cfg is None and proxies:
                proxy_cfg = proxies[i % len(proxies)]
            login_user = acc.get("id") or acc.get("email") or ""
            disp       = acc.get("email") or acc.get("id") or "?"
            try:
                cookie = acc.get("cookies") or ""
                profile_id = _client.create_profile(
                    name=disp,
                    username=login_user,
                    password=acc.get("password", ""),
                    fakey=acc.get("fakey", ""),
                    proxy_config=proxy_cfg,
                    group_id=group_id,
                    remark=_build_profile_remark(acc),
                    cookie=cookie,
                )
                created.append(disp)
            except Exception as e:
                if acc.get("cookies"):
                    try:
                        profile_id = _client.create_profile(
                            name=disp,
                            username=login_user,
                            password=acc.get("password", ""),
                            fakey=acc.get("fakey", ""),
                            proxy_config=proxy_cfg,
                            group_id=group_id,
                            remark=_build_profile_remark(acc),
                        )
                        created.append(disp)
                        failed.append(f"{disp} — perfil criado SEM cookies (cookie inválido): {e}")
                    except Exception as e2:
                        failed.append(f"{disp} — {e2}")
                else:
                    failed.append(f"{disp} — {e}")
            _emit_progress(i + 1, len(created), len(failed))
        return created, failed

    log(f"[CRIAR] {len(accounts)} perfil(s) em '{group_name}'…")
    created, failed = await asyncio.to_thread(_do_batch)
    log(f"[CRIAR] {len(created)} ok, {len(failed)} falha(s)")
    await outbox.put(json.dumps({
        "type":       "create_profiles_result",
        "request_id": request_id,
        "created":    created,
        "failed":     failed,
    }))


# ── Background workers ────────────────────────────────────────────────────────

async def _delete_worker(log=print):
    while True:
        profile_id = await _delete_queue.get()
        try:
            await asyncio.to_thread(_client.delete_profile, profile_id)
            log(f"[CMD] Perfil {profile_id} deletado do AdsPower", flush=True)
        except Exception as e:
            log(f"[CMD] Erro ao deletar perfil {profile_id}: {e}", flush=True)
        finally:
            _delete_queue.task_done()


async def _browser_status_pinger(log=print, stop: asyncio.Event | None = None):
    while True:
        await asyncio.sleep(5)
        if stop and stop.is_set():
            break
        if not _open_pids:
            continue
        for pid in list(_open_pids):
            state = await asyncio.to_thread(_client.is_browser_active, pid)
            if state == "active":
                _inactive_count.pop(pid, None)
            elif state == "inactive":
                _inactive_count[pid] = _inactive_count.get(pid, 0) + 1
                if _inactive_count[pid] >= _INACTIVE_THRESHOLD:
                    _open_pids.discard(pid)
                    _inactive_count.pop(pid, None)

        frame = json.dumps({
            "type": "browser_status",
            "open_profile_ids": list(_open_pids),
        })
        with _status_outboxes_lock:
            targets = list(_status_outboxes)
        for q in targets:
            try:
                q.put_nowait(frame)
            except Exception:
                pass


# ── Per-connection tasks ──────────────────────────────────────────────────────

async def _receiver(ws, outbox: asyncio.Queue, is_verificador: bool,
                    log=print, stop: asyncio.Event | None = None):
    async for raw in ws:
        if stop and stop.is_set():
            break
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        t = msg.get("type", "")

        if t == "open_browser":
            cmd_id = await _handle_open_browser(msg, log)
            if cmd_id is not None and is_verificador:
                await outbox.put(json.dumps({"type": "command_done", "cmd_id": cmd_id}))
        elif t == "add_card":
            # BMs feature — runs on the manager connection (and would on verif too).
            asyncio.create_task(_handle_add_card(msg, outbox, log))
        elif not is_verificador:
            continue
        elif t == "run_job":
            asyncio.create_task(_handle_run_job(msg, outbox, log))
        elif t == "link_waba":
            asyncio.create_task(_handle_link_waba(msg, outbox, log))
        elif t == "cancel_job":
            asyncio.create_task(_handle_cancel_job(msg, outbox, log))
        elif t == "create_profiles":
            asyncio.create_task(_handle_create_profiles(msg, outbox, log))
        elif t == "change_proxy":
            asyncio.create_task(_handle_change_proxy(msg, log))
        elif t == "delete_profile":
            _delete_queue.put_nowait(msg.get("profile_id", ""))
        elif t == "sync_request":
            asyncio.create_task(_sync_profiles(outbox, log))
        elif t == "acquire_run_result":
            print(f"[ACQ:6] _receiver got acquire_run_result request_id={msg.get('request_id','')[:8]}", flush=True)
            _handle_acquire_run_result(msg)


async def _sender(ws, outbox: asyncio.Queue):
    while True:
        msg = await outbox.get()
        if msg is None:
            break
        if '"acquire_run' in msg:
            print(f"[ACQ:2] _sender dispatching {msg[:140]}", flush=True)
        await ws.send(msg)


async def _periodic_sync(outbox: asyncio.Queue, log=print,
                         stop: asyncio.Event | None = None, interval: int = 60):
    while True:
        await asyncio.sleep(interval)
        if stop and stop.is_set():
            break
        log("[SYNC] Sync periódico…")
        await _sync_profiles(outbox, log)


# ── Per-connection loop ───────────────────────────────────────────────────────

async def _run_one_connection(
    label: str,
    ws_url: str,
    is_verificador: bool,
    *,
    log=print,
    on_status=None,       # callable(which: str, state: str) | None
    stop: asyncio.Event | None = None,
    sync_interval: int = 60,
):
    """
    Maintain a single persistent WebSocket connection with auto-reconnect.

    on_status  — optional GUI callback(which, state); CLI agents pass None.
    stop       — optional asyncio.Event; when set the loop exits cleanly.
    """
    global _ws_loop, _ws_outbox

    which   = label.lower()
    backoff = 5.0
    last_failure_repr: str = ""

    def _status(state: str):
        if on_status:
            on_status(which, state)

    while True:
        if stop and stop.is_set():
            break
        outbox: asyncio.Queue = asyncio.Queue()
        try:
            if last_failure_repr == "":
                log(f"[AGENT-{label.upper()}] Conectando…")
            _status("connecting")
            async with websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=10,
                open_timeout=15,
                compression=None,
            ) as ws:
                log(f"[AGENT-{label.upper()}] Conectado!")
                last_failure_repr = ""
                if is_verificador:
                    log("[AGENT-VERIF] build-marker: ACQ-DIAG-1")
                _status("online")
                backoff = 5.0

                stop_ev = asyncio.Event()

                with _status_outboxes_lock:
                    _status_outboxes.append(outbox)

                if is_verificador:
                    _ws_loop   = asyncio.get_running_loop()
                    _ws_outbox = outbox
                    await _sync_profiles(outbox, log)
                    sync_task = asyncio.create_task(
                        _periodic_sync(outbox, log, stop_ev, sync_interval)
                    )

                sender_task = asyncio.create_task(_sender(ws, outbox))
                recv_task   = asyncio.create_task(
                    _receiver(ws, outbox, is_verificador, log, stop_ev)
                )

                # Wait until recv finishes or outer stop fires
                if stop:
                    while not stop.is_set():
                        if recv_task.done():
                            break
                        await asyncio.sleep(0.5)
                else:
                    await recv_task

                stop_ev.set()
                recv_task.cancel()

                with _status_outboxes_lock:
                    try:
                        _status_outboxes.remove(outbox)
                    except ValueError:
                        pass

                if is_verificador:
                    sync_task.cancel()
                    _ws_loop   = None
                    _ws_outbox = None

                await outbox.put(None)
                await sender_task

        except (
            websockets.exceptions.ConnectionClosed,
            websockets.exceptions.InvalidHandshake,
            OSError,
            asyncio.TimeoutError,
        ) as e:
            repr_key = f"{type(e).__name__}: {str(e)[:120]}"
            if repr_key != last_failure_repr:
                log(f"[AGENT-{label.upper()}] Desconectado: {e}. Reconectando em {backoff:.0f}s…")
                last_failure_repr = repr_key
        except Exception as e:
            repr_key = f"{type(e).__name__}: {str(e)[:120]}"
            if repr_key != last_failure_repr:
                log(f"[AGENT-{label.upper()}] Erro inesperado: {e}. Reconectando em {backoff:.0f}s…")
                last_failure_repr = repr_key
        finally:
            with _status_outboxes_lock:
                try:
                    _status_outboxes.remove(outbox)
                except ValueError:
                    pass
            if is_verificador:
                _ws_loop   = None
                _ws_outbox = None
            _status("offline")

        if stop and stop.is_set():
            break

        log(f"[AGENT-{label.upper()}] Reconectando em {backoff:.0f}s…")
        # Fine-grained sleep so stop event is honoured quickly
        for _ in range(int(backoff * 10)):
            if stop and stop.is_set():
                break
            await asyncio.sleep(0.1)

        backoff = min(backoff * 1.5, 60.0) if not is_verificador else 5.0

    log(f"[AGENT-{label.upper()}] Encerrado.")
    _status("offline")


# ── Top-level orchestrator ────────────────────────────────────────────────────

async def connect_loop(
    verif_url: str,
    bms_url: str,
    *,
    log=print,
    on_status=None,
    stop: asyncio.Event | None = None,
    sync_interval: int = 60,
):
    """
    Start both WebSocket connections in parallel.

    CLI usage (agent.py):   await connect_loop(verif_ws, bms_ws, sync_interval=60)
    GUI usage (agent_gui):  await connect_loop(verif_ws, bms_ws, log=..., on_status=..., stop=...)
    """
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=_MAX_WORKER_THREADS, thread_name_prefix="job")
    )

    global _delete_worker_task, _pinger_task
    if _delete_worker_task is None or _delete_worker_task.done():
        _delete_worker_task = asyncio.create_task(_delete_worker(log))
    if _pinger_task is None or _pinger_task.done():
        _pinger_task = asyncio.create_task(_browser_status_pinger(log, stop))

    log(f"[AGENT] Verificador: {verif_url[:verif_url.index('?')]}")
    log(f"[AGENT] BMs:         {bms_url[:bms_url.index('?')]}")

    await asyncio.gather(
        _run_one_connection("verif", verif_url, True,
                            log=log, on_status=on_status, stop=stop,
                            sync_interval=sync_interval),
        _run_one_connection("bms",   bms_url,  False,
                            log=log, on_status=on_status, stop=stop),
    )

    if _pinger_task and not _pinger_task.done():
        _pinger_task.cancel()
