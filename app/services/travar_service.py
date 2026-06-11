import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .disparar_service import (
    _send_template,
    _random_namespace,
    _flag_erro_generic_if_needed,
)

_live_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()
_job_counter = 0
_BATCH_SIZE = 10


def _next_job_id() -> int:
    global _job_counter
    with _jobs_lock:
        _job_counter += 1
        return _job_counter


def get_job(job_id: int) -> dict | None:
    return _live_jobs.get(job_id)


def request_stop(job_id: int) -> bool:
    state = _live_jobs.get(job_id)
    if state:
        state["stop_requested"] = True
        return True
    return False


def _run_waba(job_id: int, user_id: int, result_entry: dict,
              spec: dict, rows: list, phone_col: str, param_map: list) -> None:
    """Hammer a single WABA with messages until error 135000 or stop requested."""
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

    def _worker(row: dict):
        phone = str(row.get(phone_col, "")).strip()
        if not phone:
            return phone, None, "telefone vazio"
        params = [
            {"name": pm["name"], "value": str(row.get(pm.get("column", ""), "")).strip()}
            for pm in param_map
        ]
        success, msg = _send_template(
            phone, phone_number_id, token, template_name, template_language, params, namespace
        )
        return phone, success, msg

    row_cursor = 0

    while True:
        state = _live_jobs.get(job_id)
        if not state or state.get("stop_requested"):
            with _jobs_lock:
                result_entry["status"] = "stopped"
                result_entry["last_message"] = "Interrompido"
            return

        batch = [rows[(row_cursor + j) % len(rows)] for j in range(_BATCH_SIZE)]
        row_cursor = (row_cursor + _BATCH_SIZE) % len(rows)

        travou = False
        with ThreadPoolExecutor(max_workers=_BATCH_SIZE) as pool:
            futures = {pool.submit(_worker, row): row for row in batch}
            for future in as_completed(futures):
                phone, success, msg = future.result()
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

                if success is False and "#135000" in (msg or ""):
                    travou = True
                    _flag_erro_generic_if_needed(user_id, waba_id, waba_flag_state, msg)

        if travou:
            with _jobs_lock:
                result_entry["status"] = "travada"
                result_entry["last_message"] = "Erro #135000 detectado"
                s = _live_jobs.get(job_id)
                if s:
                    s["travadas"] += 1
            return


def _manager(job_id: int, user_id: int, waba_specs: list,
             rows: list, phone_col: str, param_map: list) -> None:
    state = _live_jobs[job_id]

    with ThreadPoolExecutor(max_workers=max(1, len(waba_specs))) as pool:
        futures = [
            pool.submit(_run_waba, job_id, user_id, state["results"][i],
                        spec, rows, phone_col, param_map)
            for i, spec in enumerate(waba_specs)
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    with _jobs_lock:
        s = _live_jobs.get(job_id)
        if s:
            s["status"] = "stopped" if s.get("stop_requested") else "done"


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

    with _jobs_lock:
        _live_jobs[job_id] = state

    t = threading.Thread(
        target=_manager,
        args=(job_id, user_id, waba_specs, rows, phone_col, param_map),
        daemon=True,
    )
    t.start()
    return job_id
