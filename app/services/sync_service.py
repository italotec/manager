"""Background sync job: fetch Meta data for all WABAs in parallel.

Job flow:
1. Load bms.json once up front.
2. Submit each WABA to a ThreadPoolExecutor (up to SYNC_MAX_CONCURRENCY at once).
3. Pure worker _sync_one_waba: makes 4–6 Meta API calls, returns result dict (no I/O).
4. Single orchestrator thread merges results into in-memory bms dict, writes file once at end.
"""
from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from ..services.meta import (
    get_waba_name,
    get_phone_numbers,
    get_phone_numbers_health,
    get_templates,
    templates_status_summary,
    evaluate_health,
    pick_test_template,
    send_test_message,
    get_phone_messaging_limit,
)
from ..json_store import load_user_bms, save_user_bms

_live_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()
_job_counter = 0
_counter_lock = threading.Lock()

API_BLOCKED_MARK = "API access blocked."
_WEBHOOK_PROTECTED = {"PERMANENTE", "ANALISANDO", "RESTRITA"}


def _next_job_id() -> int:
    global _job_counter
    with _counter_lock:
        _job_counter += 1
        return _job_counter


def get_job(job_id: int) -> Optional[dict]:
    return _live_jobs.get(job_id)


def request_stop(job_id: int) -> None:
    with _jobs_lock:
        if job_id in _live_jobs:
            _live_jobs[job_id]["stop_requested"] = True


def _max_concurrency() -> int:
    try:
        from ..models import AppSetting
        from .. import db
        row = db.session.get(AppSetting, "SYNC_MAX_CONCURRENCY")
        return max(1, int((row.value if row else "15") or 15))
    except Exception:
        return 15


def _sync_one_waba(api_version: str, waba_id: str, token: str, prev_snap: dict) -> dict:
    """Pure worker: make all Meta calls for one WABA, return result dict (no file I/O)."""
    current_status = prev_snap.get("status_label", "")
    prev_name = prev_snap.get("waba_name") or "—"

    def _effective_status(api_status: str) -> str:
        return current_status if current_status in _WEBHOOK_PROTECTED else api_status

    waba_name, err_name = get_waba_name(api_version, token, waba_id)
    phones, err_phones = get_phone_numbers(api_version, token, waba_id)
    templates, err_tpl = get_templates(api_version, token, waba_id)

    all_errors = " ".join(e for e in (err_name, err_phones, err_tpl) if e)

    if API_BLOCKED_MARK in all_errors:
        return {
            "category": "blocked",
            "fields": {
                "waba_name": waba_name or prev_name,
                "phone_numbers": [],
                "template_counts": {"APPROVED": 0, "PAUSED": 0, "DISABLED": 0, "OTHER": 0},
                "last_error": "",
                "status_label": _effective_status("Developers Travado"),
                "last_sync_at": int(time.time()),
            },
        }

    if all_errors:
        return {
            "category": "errors",
            "fields": {
                "waba_name": waba_name or "—",
                "phone_numbers": phones or [],
                "template_counts": templates_status_summary(templates or []),
                "last_error": all_errors[:900],
                "status_label": _effective_status("Erro"),
                "last_sync_at": int(time.time()),
            },
        }

    # Health check
    health_phones, _ = get_phone_numbers_health(api_version, token, waba_id)
    health_label = evaluate_health(health_phones) if health_phones else "OK"

    # Test message probe for ERRO GENERIC
    if health_label in ("OK", "PROBLEMA CARTÃO") and phones and templates:
        test_tpl = pick_test_template(templates)
        first_phone_id = phones[0].get("id") if phones else None
        if test_tpl and first_phone_id:
            test_ok, test_resp = send_test_message(token, first_phone_id, test_tpl)
            if not test_ok and "#135000" in test_resp:
                health_label = "ERRO GENERIC"

    ever_erro_generic = prev_snap.get("ever_had_erro_generic", False)
    if health_label == "ERRO GENERIC":
        ever_erro_generic = True

    messaging_limit_tier = None
    if phones:
        messaging_limit_tier = get_phone_messaging_limit(api_version, token, phones[0].get("id"))

    tpl_list = templates or []
    tpl_counts = templates_status_summary(tpl_list)
    tpl_map = {
        str(t.get("id") or t.get("name") or ""): {
            "id": str(t.get("id") or ""),
            "name": t.get("name") or "",
            "language": t.get("language") or "",
            "category": t.get("category") or "",
            "status": (t.get("status") or "").upper(),
        }
        for t in tpl_list
        if t.get("id") or t.get("name")
    }

    return {
        "category": "synced",
        "fields": {
            "waba_name": waba_name or "—",
            "phone_numbers": phones or [],
            "template_counts": tpl_counts,
            "template_status_map": tpl_map,
            "last_error": "",
            "status_label": _effective_status(health_label),
            "last_sync_at": int(time.time()),
            "ever_had_erro_generic": ever_erro_generic,
            "messaging_limit_tier": messaging_limit_tier,
        },
    }


def _run_job(app, job_id: int, user_id: int, bms: dict, api_version: str) -> None:
    """Daemon orchestrator: process all WABAs in parallel, write file once at end."""
    state = _live_jobs[job_id]
    state["status"] = "running"

    # Build work items
    work_items = []
    for key, data in bms.items():
        if not isinstance(data, dict):
            continue
        waba_id = str(data.get("waba_id") or "").strip()
        token = (data.get("token") or "").strip()
        if not waba_id or not token:
            continue
        prev_snap = data.get("snapshot", {}) or {}
        work_items.append((key, waba_id, token, prev_snap))

    state["total"] = len(work_items)

    checkpoint_interval = 25
    next_checkpoint = checkpoint_interval

    with app.app_context():
        max_workers = _max_concurrency()
    # connection released here; network-bound sync below holds no pool slot

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_key = {
            pool.submit(_sync_one_waba, api_version, waba_id, token, prev_snap): key
            for key, waba_id, token, prev_snap in work_items
        }

        for future in as_completed(future_to_key):
            if state.get("stop_requested"):
                for f in future_to_key:
                    f.cancel()
                break

            key = future_to_key[future]
            data = bms.get(key)
            waba_id = str((data or {}).get("waba_id") or key).strip()

            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "category": "errors",
                    "fields": {
                        "waba_name": "—",
                        "phone_numbers": [],
                        "template_counts": {"APPROVED": 0, "PAUSED": 0, "DISABLED": 0, "OTHER": 0},
                        "last_error": str(exc)[:400],
                        "status_label": "Erro",
                        "last_sync_at": int(time.time()),
                    },
                }

            # Merge into in-memory bms (single thread — safe, no lock needed here)
            if isinstance(data, dict):
                snap = data.get("snapshot", {}) if isinstance(data.get("snapshot"), dict) else {}
                snap.update(result["fields"])
                data["snapshot"] = snap

            # Update counters
            with _jobs_lock:
                state["done"] += 1
                cat = result["category"]
                if cat == "synced":
                    state["synced"] += 1
                elif cat == "blocked":
                    state["blocked"] += 1
                else:
                    state["errors"] += 1

                snap = result["fields"]
                state["results"].append({
                    "waba_id": waba_id,
                    "waba_name": snap.get("waba_name") or waba_id,
                    "status_label": snap.get("status_label") or "",
                    "category": cat,
                })

            # Checkpoint: write every N completions for crash resilience
            if state["done"] >= next_checkpoint:
                save_user_bms(user_id, bms)
                next_checkpoint += checkpoint_interval

    # Final write (covers the tail after last checkpoint)
    save_user_bms(user_id, bms)

    state["status"] = "stopped" if state.get("stop_requested") else "done"


def start_sync_job(user_id: int, api_version: str) -> int:
    """Load bms, spawn background sync job, return job_id."""
    bms = load_user_bms(user_id)

    job_id = _next_job_id()
    state = {
        "job_id": job_id,
        "status": "queued",
        "total": 0,
        "done": 0,
        "synced": 0,
        "blocked": 0,
        "errors": 0,
        "results": [],
        "stop_requested": False,
    }
    with _jobs_lock:
        _live_jobs[job_id] = state

    from flask import current_app
    app = current_app._get_current_object()

    t = threading.Thread(
        target=_run_job,
        args=(app, job_id, user_id, bms, api_version),
        daemon=True,
    )
    t.start()
    return job_id
