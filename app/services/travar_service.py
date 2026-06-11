import threading
from concurrent.futures import ThreadPoolExecutor

from .disparar_service import (
    _send_template,
    _random_namespace,
    _flag_erro_generic_if_needed,
)

_live_jobs: dict[int, dict] = {}
_stop_events: dict[int, threading.Event] = {}
_jobs_lock = threading.Lock()
_job_counter = 0

_TOTAL_BUDGET = 300  # max concurrent sends across all WABAs in a job


def _next_job_id() -> int:
    global _job_counter
    with _jobs_lock:
        _job_counter += 1
        return _job_counter


def get_job(job_id: int) -> dict | None:
    return _live_jobs.get(job_id)


def request_stop(job_id: int) -> bool:
    ev = _stop_events.get(job_id)
    if ev:
        ev.set()
    state = _live_jobs.get(job_id)
    if state:
        state["stop_requested"] = True
        # Mark stopped immediately so the next poll reflects it
        if state.get("status") == "running":
            state["status"] = "stopped"
        return True
    return False


def _run_waba(job_id: int, user_id: int, result_entry: dict,
              spec: dict, rows: list, phone_col: str, param_map: list,
              stop_event: threading.Event, concurrency: int) -> None:
    """Stream messages to one WABA continuously until error 135000 or stop."""
    if not rows:
        with _jobs_lock:
            result_entry["status"] = "error"
            result_entry["last_message"] = "Lista vazia"
            s = _live_jobs.get(job_id)
            if s:
                s["travadas"] += 1
        return

    waba_id = spec["waba_id"]
    phone_number_id = spec["phone_number_id"]
    token = spec["token"]
    template_name = spec["template_name"]
    template_language = spec["template_language"]
    namespace = _random_namespace()
    waba_flag_state = {}
    travada = threading.Event()
    sem = threading.BoundedSemaphore(concurrency)

    def _worker(row: dict) -> None:
        try:
            phone = str(row.get(phone_col, "")).strip()
            if not phone:
                return
            params = [
                {"name": pm["name"], "value": str(row.get(pm.get("column", ""), "")).strip()}
                for pm in param_map
            ]
            success, msg = _send_template(
                phone, phone_number_id, token, template_name, template_language, params, namespace
            )
            with _jobs_lock:
                s = _live_jobs.get(job_id)
                if s:
                    if success is True:
                        result_entry["sent"] += 1
                        s["sent"] += 1
                    elif success is False:
                        result_entry["failed"] += 1
                        s["failed"] += 1
                        result_entry["last_message"] = msg or ""
            if success is False and "#135000" in (msg or "") and not travada.is_set():
                _flag_erro_generic_if_needed(user_id, waba_id, waba_flag_state, msg)
                travada.set()
        finally:
            sem.release()

    pool = ThreadPoolExecutor(max_workers=concurrency)

    # Streaming loop: cycle rows repeatedly, submitting as fast as slots free up
    while not stop_event.is_set() and not travada.is_set():
        for row in rows:
            if stop_event.is_set() or travada.is_set():
                break
            # Acquire a slot — check stop/travada every 200ms so we don't block long
            acquired = False
            while not (stop_event.is_set() or travada.is_set()):
                if sem.acquire(timeout=0.2):
                    acquired = True
                    break
            if not acquired:
                break
            pool.submit(_worker, row)

    # Don't wait for in-flight sends — return control immediately
    pool.shutdown(wait=False, cancel_futures=True)

    with _jobs_lock:
        if travada.is_set():
            result_entry["status"] = "travada"
            result_entry["last_message"] = "Erro #135000 detectado"
            s = _live_jobs.get(job_id)
            if s:
                s["travadas"] += 1
        else:
            result_entry["status"] = "stopped"
            result_entry["last_message"] = "Interrompido"


def _manager(job_id: int, user_id: int, waba_specs: list,
             rows: list, phone_col: str, param_map: list,
             stop_event: threading.Event) -> None:
    state = _live_jobs[job_id]
    num_wabas = max(1, len(waba_specs))
    concurrency = max(5, min(50, _TOTAL_BUDGET // num_wabas))

    with ThreadPoolExecutor(max_workers=num_wabas) as pool:
        futures = [
            pool.submit(
                _run_waba, job_id, user_id, state["results"][i],
                spec, rows, phone_col, param_map, stop_event, concurrency
            )
            for i, spec in enumerate(waba_specs)
        ]
        for future in futures:
            try:
                future.result()
            except Exception:
                pass

    with _jobs_lock:
        s = _live_jobs.get(job_id)
        if s and s.get("status") not in ("stopped",):
            s["status"] = "stopped" if stop_event.is_set() else "done"
    _stop_events.pop(job_id, None)


def start_travar_job(user_id: int, waba_specs: list,
                     rows: list, phone_col: str, param_map: list) -> int:
    job_id = _next_job_id()

    results = [
        {
            "waba_id": spec["waba_id"],
            "waba_name": spec.get("waba_name", spec["waba_id"]),
            "template": spec.get("template_name", ""),
            "status": "running",
            "sent": 0,
            "failed": 0,
            "last_message": "",
        }
        for spec in waba_specs
    ]

    state = {
        "job_id": job_id,
        "status": "running",
        "total": len(waba_specs),
        "travadas": 0,
        "stop_requested": False,
        "sent": 0,
        "failed": 0,
        "results": results,
    }

    stop_event = threading.Event()

    with _jobs_lock:
        _live_jobs[job_id] = state
        _stop_events[job_id] = stop_event

    t = threading.Thread(
        target=_manager,
        args=(job_id, user_id, waba_specs, rows, phone_col, param_map, stop_event),
        daemon=True,
    )
    t.start()
    return job_id
