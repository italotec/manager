"""Orchestrates bulk credit-card addition to WABA accounts.

Job flow:
1. Receive a list of waba_ids selected by the user.
2. Assign a card to each WABA (random, respecting the 10-distinct-WABA cap).
3. Dispatch add_card commands to the WebSocket agent, LINK_MAX_CONCURRENCY at a time (default 5).
4. Handle results, update card usage/status in DB.
"""
from __future__ import annotations

import random
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


def assign_cards(user_id: int, waba_ids: list[str]) -> list[tuple[str, Optional[object]]]:
    """Return [(waba_id, Card|None)] for each requested waba.

    Cards are picked randomly from the pool of available cards. Each card's
    budget (10 - usage_count) is tracked locally during assignment so the same
    card can serve multiple WABAs in one batch without exceeding its cap.
    Cards that already have this waba_id in used_waba_ids are excluded for
    that specific waba.
    """
    from ..models import Card

    cards = Card.query.filter_by(user_id=user_id, status="active").all()
    available = [c for c in cards if c.remaining > 0]

    # Local budget tracking: card.id -> remaining slots this run
    budget = {c.id: c.remaining for c in available}
    # Pre-parse used sets for fast membership checks
    import json
    used_set: dict[int, set[str]] = {}
    for c in available:
        try:
            used_set[c.id] = set(json.loads(c.used_waba_ids or "[]"))
        except Exception:
            used_set[c.id] = set()

    card_map: dict[int, object] = {c.id: c for c in available}

    result = []
    for waba_id in waba_ids:
        candidates = [
            c for c in available
            if budget.get(c.id, 0) > 0 and waba_id not in used_set[c.id]
        ]
        if not candidates:
            result.append((waba_id, None))
            continue
        chosen = random.choice(candidates)
        budget[chosen.id] -= 1
        used_set[chosen.id].add(waba_id)  # prevent re-use in this same batch
        result.append((waba_id, chosen))

    return result


def _run_job(app, job_id: int, user_id: int, assignments: list, bms: dict):
    """Background daemon: processes assignments 5 at a time."""
    from ..routes.agent_ws import send_command_and_wait
    from ..models import Card
    from .. import db

    state = _live_jobs[job_id]
    state["status"] = "running"

    def _process_one(waba_id: str, card) -> dict:
        waba_name = ""
        entry = bms.get(str(waba_id), {})
        if isinstance(entry, dict):
            snap = entry.get("snapshot", {}) or {}
            waba_name = snap.get("waba_name") or waba_id
            profile_id = (entry.get("adspower_profile_id") or "").strip()
            business_manager_id = (entry.get("business_manager_id") or "").strip()
            payment_account_id = (entry.get("payment_account_id") or "").strip()
        else:
            profile_id = ""
            business_manager_id = ""
            payment_account_id = ""

        if card is None:
            return {
                "waba_id": waba_id, "waba_name": waba_name,
                "card_last4": "—", "ok": False,
                "msg": "Sem cartões disponíveis",
            }

        if not profile_id:
            return {
                "waba_id": waba_id, "waba_name": waba_name,
                "card_last4": card.last4, "ok": False,
                "msg": "WABA sem perfil AdsPower vinculado",
            }

        cmd = {
            "type": "add_card",
            "profile_id": profile_id,
            "waba_id": waba_id,
            "business_id": business_manager_id,
            "payment_account_id": payment_account_id,
            "card": {
                "number": card.number,
                "exp_month": card.exp_month,
                "exp_year": card.exp_year,
                "csc": card.csc,
                "name": card.holder_name,
            },
        }

        res = send_command_and_wait(user_id, cmd, timeout=300.0)

        return {
            "waba_id": waba_id,
            "waba_name": waba_name,
            "card_last4": card.last4,
            "card_id": card.id,
            "ok": res.get("ok", False),
            "code": res.get("code"),
            "error_raw": res.get("error", ""),
            "msg": res.get("error", "Sucesso") if not res.get("ok") else "Cartão adicionado com sucesso",
        }

    with app.app_context():
        with ThreadPoolExecutor(max_workers=_max_concurrency()) as pool:
            future_to_item = {
                pool.submit(_process_one, waba_id, card): (waba_id, card)
                for waba_id, card in assignments
            }
            for future in as_completed(future_to_item):
                if state.get("stop_requested"):
                    break
                row = future.result()

                # Update card status/usage in DB (sequentially in this thread)
                card_id = row.get("card_id")
                if card_id:
                    card = db.session.get(Card, card_id)
                    if card:
                        if row["ok"]:
                            card.mark_used(row["waba_id"])
                        elif row.get("code") == 4992003:
                            card.status = "overused"
                            card.last_error = "Usado em muitas contas (FB 4992003)"
                        elif row.get("code") == 4992001:
                            card.status = "invalid"
                            card.last_error = row.get("error_raw", "")[:255]
                        db.session.commit()

                with _jobs_lock:
                    state["done"] += 1
                    if row["ok"]:
                        state["success"] += 1
                    else:
                        state["failed"] += 1
                    state["results"].append({
                        "waba_id": row["waba_id"],
                        "waba_name": row["waba_name"],
                        "card_last4": row["card_last4"],
                        "ok": row["ok"],
                        "msg": row["msg"],
                    })

        state["status"] = "stopped" if state.get("stop_requested") else "done"


def start_card_job(user_id: int, waba_ids: list[str]) -> int:
    """Assign cards and launch the background job. Returns job_id."""
    from ..json_store import load_user_bms

    bms = load_user_bms(user_id)
    assignments = assign_cards(user_id, waba_ids)

    job_id = _next_job_id()
    state = {
        "job_id": job_id,
        "status": "queued",
        "total": len(assignments),
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
        args=(app, job_id, user_id, assignments, bms),
        daemon=True,
    )
    t.start()
    return job_id


def request_stop(job_id: int):
    with _jobs_lock:
        if job_id in _live_jobs:
            _live_jobs[job_id]["stop_requested"] = True
