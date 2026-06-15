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

        ok = bool(res.get("ok"))
        return {
            "waba_id": waba_id,
            "waba_name": waba_name,
            "ok": ok,
            "phone": res.get("display_phone_number", ""),
            "msg": res.get("error", "Número adicionado com sucesso") if not ok else "Número adicionado com sucesso",
        }

    with app.app_context():
        with ThreadPoolExecutor(max_workers=_max_concurrency()) as pool:
            futures = {pool.submit(_process_one, waba_id): waba_id for waba_id in waba_ids}
            for future in as_completed(futures):
                if state.get("stop_requested"):
                    break
                row = future.result()

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
