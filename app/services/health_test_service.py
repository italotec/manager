"""
WABA deliverability test — sends a real template message to the user's test_phone
and waits for a sent/delivered webhook confirmation.

Job state stored in _jobs (in-memory). Webhook correlation is file-based via
bms.json (health_test_pending / health_test_ok_at) so it works across workers.
"""

import random
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from ..json_store import load_user_bms, patch_snapshot, find_users_with_waba
from .meta import get_templates, pick_test_template, _count_body_vars

_WEBHOOK_PROTECTED = {"PERMANENTE", "DESABILITADA", "ANALISANDO", "RESTRITA"}

_jobs: dict[int, dict] = {}
_job_counter = 0
_counter_lock = threading.Lock()


def _next_job_id() -> int:
    global _job_counter
    with _counter_lock:
        _job_counter += 1
        return _job_counter


# ── random variable filler ────────────────────────────────────────────────────

_WORDS = [
    "alfa", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
    "hotel", "india", "juliet", "kilo", "lima", "mike", "novembro",
    "oscar", "papa", "quebec", "romeo", "sierra", "tango",
]


def _random_var_value() -> str:
    word = random.choice(_WORDS)
    suffix = "".join(random.choices(string.digits, k=4))
    return f"{word}{suffix}"


# ── send one template message ─────────────────────────────────────────────────

def _send_health_template(
    token: str,
    phone_number_id: str,
    to: str,
    template: dict,
) -> tuple[bool, str, str]:
    """Send template to `to`. Returns (ok, wamid, diagnosis)."""
    tpl_name = template.get("name", "")
    tpl_lang = template.get("language", "en")
    var_count = _count_body_vars(template)

    components = []
    if var_count > 0:
        components.append({
            "type": "body",
            "parameters": [
                {"type": "text", "text": _random_var_value()}
                for _ in range(var_count)
            ],
        })

    payload = {
        "messaging_product": "whatsapp",
        "type": "template",
        "to": to,
        "template": {
            "name": tpl_name,
            "language": {"code": tpl_lang},
            "components": components,
        },
    }

    url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        diagnosis = (r.text or "")[:800]
        if r.status_code == 200:
            try:
                j = r.json()
                wamid = (j.get("messages") or [{}])[0].get("id", "")
            except Exception:
                wamid = ""
            return True, wamid, diagnosis
        return False, "", diagnosis
    except Exception as e:
        return False, "", str(e)[:800]


# ── webhook correlation ───────────────────────────────────────────────────────

def mark_health_test(waba_id: str, wamid: str) -> None:
    """Called by webhook handler when sent/delivered arrives.
    If wamid is in health_test_pending for this WABA, stamps health_test_ok_at."""
    now_ts = int(time.time())
    for user_id in find_users_with_waba(waba_id):
        bms = load_user_bms(user_id)
        entry = bms.get(str(waba_id).strip()) or {}
        snap = entry.get("snapshot") or {}
        pending = snap.get("health_test_pending") or {}
        if wamid in (pending.get("wamids") or []):
            patch_snapshot(
                user_id, waba_id,
                health_test_ok_at=now_ts,
                health_test_pending={},
            )


# ── per-WABA test loop ────────────────────────────────────────────────────────

def _run_one_test(user_id: int, waba_id: str, test_phone: str, api_version: str) -> dict:
    """Run up to 5 send attempts (30s apart). Returns {state, msg}."""
    bms = load_user_bms(user_id)
    entry = bms.get(str(waba_id).strip()) or {}
    snap = entry.get("snapshot") or {}
    token = entry.get("token", "")

    # Resolve phone_number_id
    phones = snap.get("phone_numbers") or []
    phone_number_id = phones[0].get("id", "") if phones else entry.get("phone_number_id", "")

    if not token or not phone_number_id:
        return {"state": "failed", "msg": "WABA sem token ou phone_number_id. Sincronize o dashboard."}

    # Fetch templates
    templates, err = get_templates(api_version, token, waba_id)
    if err or not templates:
        msg = err or "Nenhum template encontrado para este WABA."
        return {"state": "failed", "msg": msg}

    tpl = pick_test_template(templates)
    if not tpl:
        return {"state": "failed", "msg": "Nenhum template APROVADO encontrado."}

    max_attempts = 5
    poll_interval = 2   # seconds between polls for webhook
    poll_timeout = 28   # seconds to wait for webhook before next attempt
    attempt_gap = 30    # seconds between attempts

    for attempt in range(1, max_attempts + 1):
        ok, wamid, diagnosis = _send_health_template(token, phone_number_id, test_phone, tpl)

        if not ok:
            # Apply existing #135000 → ERRO GENERIC mapping
            if "#135000" in diagnosis:
                _flag_erro_generic(user_id, waba_id, snap, diagnosis)
            patch_snapshot(user_id, waba_id, health_test_last_error=diagnosis)
            return {"state": "failed", "msg": f"Erro no envio (tentativa {attempt}): {diagnosis}"}

        # Register wamid as pending for webhook correlation
        started_at = int(time.time())
        pending = snap.get("health_test_pending") or {}
        existing_wamids = list(pending.get("wamids") or [])
        if wamid:
            existing_wamids.append(wamid)
        patch_snapshot(
            user_id, waba_id,
            health_test_pending={"wamids": existing_wamids, "started_at": started_at},
        )

        # Poll for webhook confirmation
        deadline = started_at + poll_timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            fresh_bms = load_user_bms(user_id)
            fresh_snap = (fresh_bms.get(str(waba_id).strip()) or {}).get("snapshot") or {}
            ok_at = fresh_snap.get("health_test_ok_at") or 0
            if ok_at >= started_at:
                return {"state": "passed", "msg": f"Mensagem entregue (tentativa {attempt})."}

        # No confirmation yet — wait before next attempt (skip on last)
        if attempt < max_attempts:
            time.sleep(attempt_gap - poll_timeout)

    patch_snapshot(user_id, waba_id, health_test_pending={})
    return {"state": "failed", "msg": f"Sem confirmação de entrega após {max_attempts} tentativas."}


def _flag_erro_generic(user_id: int, waba_id: str, snap: dict, msg: str) -> None:
    """Apply #135000 → ERRO GENERIC, respecting protected statuses."""
    fields = {"ever_had_erro_generic": True}
    if snap.get("status_label", "") not in _WEBHOOK_PROTECTED:
        fields["status_label"] = "ERRO GENERIC"
    patch_snapshot(user_id, waba_id, **fields)


# ── job runner ────────────────────────────────────────────────────────────────

def _run_job(job_id: int, user_id: int, waba_ids: list, test_phone: str, api_version: str) -> None:
    job = _jobs[job_id]
    job["status"] = "running"

    def run_one(waba_id: str):
        job["results"][waba_id] = {"state": "running", "msg": "Testando…"}
        result = _run_one_test(user_id, waba_id, test_phone, api_version)
        job["results"][waba_id] = result
        with _counter_lock:
            job["done"] += 1

    with ThreadPoolExecutor(max_workers=min(8, len(waba_ids))) as pool:
        pool.map(run_one, waba_ids)

    job["status"] = "done"


def start_health_test_job(user_id: int, waba_ids: list, test_phone: str, api_version: str) -> int:
    job_id = _next_job_id()
    _jobs[job_id] = {
        "status": "queued",
        "total": len(waba_ids),
        "done": 0,
        "results": {},
    }
    t = threading.Thread(
        target=_run_job,
        args=(job_id, user_id, waba_ids, test_phone, api_version),
        daemon=True,
    )
    t.start()
    return job_id


def get_job(job_id: int) -> dict | None:
    return _jobs.get(job_id)
