"""Orchestrates bulk template creation across multiple WABAs.

Job flow:
1. Build a task list: one entry per (waba_id, template_name).
2. Dispatch batches of 8 concurrent create_template_rl calls.
3. On rate-limit: push the task back to the pending deque, record a per-WABA
   cooldown using Meta-recommended 4^retry_count exponential backoff.
4. Sleep until the earliest cooldown expires, then retry.
5. Guard: max 12 retries per task; after that mark it as failed.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

_live_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()
_job_counter = 0
_counter_lock = threading.Lock()

_MAX_RETRIES = 12
_MAX_BACKOFF = 300  # seconds cap on a single cooldown


def _next_job_id() -> int:
    global _job_counter
    with _counter_lock:
        _job_counter += 1
        return _job_counter


def get_job(job_id: int) -> Optional[dict]:
    return _live_jobs.get(job_id)


def request_stop(job_id: int):
    with _jobs_lock:
        if job_id in _live_jobs:
            _live_jobs[job_id]["stop_requested"] = True


def _backoff(retry_count: int, override: int = 0) -> float:
    if override > 0:
        return min(override, _MAX_BACKOFF)
    return min(4 ** retry_count, _MAX_BACKOFF)


def _run_job(app, job_id: int, tasks: list[dict]):
    """Background daemon: processes tasks with rate-limit cooldown requeue."""
    from ..services.meta import create_template_rl
    from ..config import Config

    state = _live_jobs[job_id]
    state["status"] = "running"

    pending: deque[dict] = deque(tasks)
    # {waba_id: epoch_when_ready}
    cooldown_until: dict[str, float] = {}

    with app.app_context():
        while pending and not state.get("stop_requested"):
            now = time.time()

            # Separate ready vs cooling tasks
            ready, still_cooling = [], []
            while pending:
                task = pending.popleft()
                wid = task["waba_id"]
                if cooldown_until.get(wid, 0) <= now:
                    ready.append(task)
                else:
                    still_cooling.append(task)

            # Put cooling tasks back
            for t in still_cooling:
                pending.append(t)

            if not ready:
                # All remaining tasks are cooling — sleep until the earliest wakes up
                if pending:
                    next_wake = min(cooldown_until.get(t["waba_id"], now) for t in pending)
                    sleep_secs = max(0.5, next_wake - time.time())
                    # Interruptible sleep in 0.5s chunks
                    slept = 0.0
                    while slept < sleep_secs and not state.get("stop_requested"):
                        time.sleep(0.5)
                        slept += 0.5
                continue

            # Dispatch batch of up to 8 concurrently
            def _do_one(task: dict):
                return task, create_template_rl(
                    Config.META_API_VERSION,
                    task["token"],
                    task["waba_id"],
                    {**task["base_payload"], "name": task["name"]},
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(_do_one, t): t for t in ready}
                for future in as_completed(futures):
                    if state.get("stop_requested"):
                        break
                    task, (result, err, rate_limited, retry_after) = future.result()

                    if rate_limited:
                        rc = task.get("retry_count", 0) + 1
                        if rc > _MAX_RETRIES:
                            # Exhausted retries — count as failed
                            with _jobs_lock:
                                state["done"] += 1
                                state["failed"] += 1
                                state["queued"] = max(0, state["queued"] - 1)
                                state["results"].append({
                                    "waba_id":   task["waba_id"],
                                    "waba_name": task["waba_name"],
                                    "name":      task["name"],
                                    "ok":        False,
                                    "msg":       "Rate limit não liberou após várias tentativas",
                                })
                        else:
                            task["retry_count"] = rc
                            backoff = _backoff(rc, retry_after)
                            cooldown_until[task["waba_id"]] = time.time() + backoff
                            pending.append(task)
                            with _jobs_lock:
                                state["queued"] = sum(
                                    1 for t in pending
                                    if cooldown_until.get(t["waba_id"], 0) > time.time()
                                )
                    else:
                        with _jobs_lock:
                            state["done"] += 1
                            if err:
                                state["failed"] += 1
                                state["results"].append({
                                    "waba_id":   task["waba_id"],
                                    "waba_name": task["waba_name"],
                                    "name":      task["name"],
                                    "ok":        False,
                                    "msg":       err,
                                })
                            else:
                                state["success"] += 1
                                state["results"].append({
                                    "waba_id":   task["waba_id"],
                                    "waba_name": task["waba_name"],
                                    "name":      task["name"],
                                    "ok":        True,
                                    "msg":       "",
                                })

    state["status"] = "stopped" if state.get("stop_requested") else "done"


def start_template_job(user_id: int, model_id: int, quantity: int, waba_ids: list[str]) -> int:
    """Build task list and launch background job. Returns job_id."""
    from ..models import TemplateModel
    from ..json_store import load_user_bms
    from flask import current_app

    model = TemplateModel.query.filter_by(id=model_id, user_id=user_id).first()
    if not model:
        raise ValueError("Template model não encontrado.")

    try:
        base_payload = json.loads(model.payload_json)
    except Exception:
        raise ValueError("Payload do template inválido.")

    base_name = model.name  # e.g. "template"
    bms = load_user_bms(user_id)

    tasks: list[dict] = []
    for waba_id in waba_ids:
        entry = bms.get(str(waba_id))
        if not isinstance(entry, dict):
            continue
        token = (entry.get("token") or "").strip()
        if not token:
            continue
        snap = entry.get("snapshot", {}) or {}
        waba_name = snap.get("waba_name") or waba_id
        for i in range(1, quantity + 1):
            tasks.append({
                "waba_id":      waba_id,
                "token":        token,
                "waba_name":    waba_name,
                "name":         f"{base_name}{i:02d}",
                "base_payload": base_payload,
                "retry_count":  0,
            })

    job_id = _next_job_id()
    state = {
        "job_id":        job_id,
        "status":        "queued",
        "total":         len(tasks),
        "done":          0,
        "success":       0,
        "failed":        0,
        "queued":        0,
        "results":       [],
        "stop_requested": False,
    }
    with _jobs_lock:
        _live_jobs[job_id] = state

    app = current_app._get_current_object()
    t = threading.Thread(
        target=_run_job,
        args=(app, job_id, tasks),
        daemon=True,
    )
    t.start()
    return job_id
