"""Orchestrates bulk virtual phone number addition to WABA accounts.

Job flow:
1. Receive a list of waba_ids selected by the admin.
2. Dispatch add_virtual_phone commands to the WebSocket agent, LINK_MAX_CONCURRENCY at a time.
3. Handle results from agent_core._execute_add_virtual_phone_sync.

Mirrors card_service.py — same in-memory job state, same ThreadPoolExecutor pattern.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from flask import current_app

_live_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()
_job_counter = 0
_counter_lock = threading.Lock()


def _next_job_id() -> int:
    global _job_counter
    with _counter_lock:
        _job_counter += 1
        return _job_counter


def get_job(job_id: int) -> Optional[dict]:
    return _live_jobs.get(job_id)


def _max_concurrency() -> int:
    from ..models import AppSetting
    from .. import db
    row = db.session.get(AppSetting, "LINK_MAX_CONCURRENCY")
    try:
        return max(1, int((row.value if row else "5") or 5))
    except (ValueError, TypeError):
        return 5


def _run_job(app, job_id: int, user_id: int, waba_ids: list[str], bms: dict):
    """Background daemon: processes one virtual-phone addition per WABA."""
    from ..routes.agent_ws import send_command_and_wait

    state = _live_jobs[job_id]
    state["status"] = "running"

    def _process_one(waba_id: str) -> dict:
        entry = bms.get(str(waba_id), {})
        waba_name = ""
        profile_id = ""
        business_manager_id = ""

        if isinstance(entry, dict):
            snap = entry.get("snapshot", {}) or {}
            waba_name = snap.get("waba_name") or waba_id
            profile_id = (entry.get("adspower_profile_id") or "").strip()
            business_manager_id = (entry.get("business_manager_id") or "").strip()

        if not profile_id:
            return {
                "waba_id": waba_id,
                "waba_name": waba_name,
                "ok": False,
                "phone": "",
                "msg": "WABA sem perfil AdsPower vinculado",
            }

        cmd = {
            "type": "add_virtual_phone",
            "profile_id": profile_id,
            "waba_id": waba_id,
            "business_id": business_manager_id,
            "display_name": waba_name,
        }

        res = send_command_and_wait(user_id, cmd, timeout=300.0)

        if not res.get("ok"):
            return {
                "waba_id": waba_id,
                "waba_name": waba_name,
                "ok": False,
                "phone": res.get("display_phone_number", ""),
                "msg": res.get("error", "Falha ao criar número virtual"),
            }

        created_phone = res.get("display_phone_number", "")

        # Resolve phone_number_id via Graph API and register (connect) the new number.
        # api_version captured from app.config before the ThreadPoolExecutor (app context
        # is not inherited by worker threads, so current_app cannot be used here).
        from ..services.meta import get_phone_numbers, register_number

        token = (entry.get("token") or "").strip()
        if not token:
            return {
                "waba_id": waba_id, "waba_name": waba_name,
                "ok": False, "phone": created_phone,
                "msg": "Número criado, mas registro falhou: WABA sem token configurado",
            }

        phones, fetch_err = get_phone_numbers(api_version, token, waba_id)

        def _digits(s: str) -> str:
            return "".join(c for c in (s or "") if c.isdigit())

        created_digits = _digits(created_phone)
        phone_id = ""
        display = created_phone

        non_connected = [p for p in phones if (p.get("status") or "").upper() != "CONNECTED"]
        if created_digits:
            for p in non_connected:
                if _digits(p.get("display_phone_number", "")) == created_digits:
                    phone_id = p.get("id", "")
                    display = p.get("display_phone_number", created_phone)
                    break
        if not phone_id and len(non_connected) == 1:
            phone_id = non_connected[0].get("id", "")
            display = non_connected[0].get("display_phone_number", created_phone)

        if not phone_id:
            reason = fetch_err or "phone_number_id não encontrado — sincronize e use 'Registrar pendentes'"
            return {
                "waba_id": waba_id, "waba_name": waba_name,
                "ok": False, "phone": created_phone,
                "msg": f"Número criado, mas registro falhou: {reason}",
            }

        try:
            reg = register_number(api_version, token, phone_id, "123456", None)
            try:
                rj = reg.json()
            except Exception:
                rj = {}
            if reg.status_code == 200 and rj.get("success"):
                return {
                    "waba_id": waba_id, "waba_name": waba_name,
                    "ok": True, "phone": display,
                    "msg": "Número adicionado e registrado",
                }
            err_detail = (rj.get("error") or {}).get("message") or f"HTTP {reg.status_code}"
            return {
                "waba_id": waba_id, "waba_name": waba_name,
                "ok": False, "phone": display,
                "msg": f"Número criado, mas registro falhou: {err_detail}",
            }
        except Exception as exc:
            return {
                "waba_id": waba_id, "waba_name": waba_name,
                "ok": False, "phone": created_phone,
                "msg": f"Número criado, mas registro falhou: {str(exc)[:300]}",
            }

    with app.app_context():
        api_version = app.config["META_API_VERSION"]
        with ThreadPoolExecutor(max_workers=_max_concurrency()) as pool:
            futures = {pool.submit(_process_one, waba_id): waba_id for waba_id in waba_ids}
            for future in as_completed(futures):
                if state.get("stop_requested"):
                    break
                try:
                    row = future.result()
                except Exception as exc:
                    waba_id_f = futures[future]
                    print(f"[VPHONE] worker exception waba={waba_id_f}: {exc}", flush=True)
                    row = {
                        "waba_id": waba_id_f, "waba_name": waba_id_f,
                        "ok": False, "phone": "", "msg": f"Erro interno: {str(exc)[:300]}",
                    }

                with _jobs_lock:
                    state["done"] += 1
                    if row["ok"]:
                        state["success"] += 1
                    else:
                        state["failed"] += 1
                    state["results"].append(row)

        state["status"] = "stopped" if state.get("stop_requested") else "done"


def start_virtual_phone_job(user_id: int, waba_ids: list[str]) -> int:
    """Launch the background job. Returns job_id."""
    from ..json_store import load_user_bms

    bms = load_user_bms(user_id)

    job_id = _next_job_id()
    state = {
        "job_id": job_id,
        "status": "queued",
        "total": len(waba_ids),
        "done": 0,
        "success": 0,
        "failed": 0,
        "results": [],
        "stop_requested": False,
    }
    with _jobs_lock:
        _live_jobs[job_id] = state

    app = current_app._get_current_object()
    t = threading.Thread(
        target=_run_job,
        args=(app, job_id, user_id, waba_ids, bms),
        daemon=True,
    )
    t.start()
    return job_id


def request_stop(job_id: int):
    with _jobs_lock:
        if job_id in _live_jobs:
            _live_jobs[job_id]["stop_requested"] = True
