import sys
import asyncio
import threading

from .disparar_service import (
    _send_template_async,
    _random_namespace,
    _flag_erro_generic_if_needed,
)

_live_jobs: dict[int, dict] = {}
_loops: dict[int, asyncio.AbstractEventLoop] = {}
_main_tasks: dict[int, asyncio.Task] = {}
_jobs_lock = threading.Lock()
_job_counter = 0

_GLOBAL_CONCURRENCY = 500


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
        if state.get("status") == "running":
            state["status"] = "stopped"

    loop = _loops.get(job_id)
    task = _main_tasks.get(job_id)
    if loop and task and not task.done():
        loop.call_soon_threadsafe(task.cancel)
        return True
    return bool(state)


async def _run_waba(session, sem: asyncio.Semaphore, job_id: int, user_id: int,
                   result_entry: dict, spec: dict, rows: list,
                   phone_col: str, param_map: list) -> None:
    state = _live_jobs.get(job_id)

    if not rows:
        with _jobs_lock:
            result_entry["status"] = "error"
            result_entry["last_message"] = "Lista vazia"
            if state:
                state["travadas"] += 1
        return

    waba_id = spec["waba_id"]
    phone_number_id = spec["phone_number_id"]
    token = spec["token"]
    template_name = spec["template_name"]
    template_language = spec["template_language"]
    namespace = _random_namespace()
    waba_flag_state = {}
    travada = asyncio.Event()

    def _stopping() -> bool:
        # Flag-based stop — reliable even if task cancellation doesn't propagate
        return state is None or state.get("stop_requested") or travada.is_set()

    async def _one(row: dict) -> None:
        if _stopping():
            return
        async with sem:
            if _stopping():
                return
            phone = str(row.get(phone_col, "")).strip()
            if not phone:
                return
            params = [
                {"name": pm["name"], "value": str(row.get(pm.get("column", ""), "")).strip()}
                for pm in param_map
            ]
            success, msg = await _send_template_async(
                session, phone, phone_number_id, token,
                template_name, template_language, params, namespace,
            )
            with _jobs_lock:
                if state:
                    if success is True:
                        result_entry["sent"] += 1
                        state["sent"] += 1
                    elif success is False:
                        result_entry["failed"] += 1
                        state["failed"] += 1
                        result_entry["last_message"] = msg or ""
            if success is False and "#135000" in (msg or "") and not travada.is_set():
                _flag_erro_generic_if_needed(user_id, waba_id, waba_flag_state, msg)
                travada.set()

    while not _stopping():
        await asyncio.gather(*[_one(row) for row in rows])

    with _jobs_lock:
        if travada.is_set():
            result_entry["status"] = "travada"
            result_entry["last_message"] = "Erro #135000 detectado"
            if state:
                state["travadas"] += 1
        elif result_entry.get("status") == "running":
            result_entry["status"] = "stopped"
            result_entry["last_message"] = "Interrompido"


async def _main(job_id: int, user_id: int, waba_specs: list,
                rows: list, phone_col: str, param_map: list) -> None:
    import aiohttp as _aiohttp

    state = _live_jobs[job_id]

    # Bail early if request_stop() was called before the loop started
    if state.get("stop_requested"):
        return

    # Register loop + task so request_stop() can cancel us from another thread
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    with _jobs_lock:
        _loops[job_id] = loop
        _main_tasks[job_id] = task

    connector = _aiohttp.TCPConnector(limit=_GLOBAL_CONCURRENCY, limit_per_host=_GLOBAL_CONCURRENCY)
    sem = asyncio.Semaphore(_GLOBAL_CONCURRENCY)

    try:
        async with _aiohttp.ClientSession(connector=connector) as session:
            await asyncio.gather(*[
                _run_waba(session, sem, job_id, user_id, state["results"][i],
                          spec, rows, phone_col, param_map)
                for i, spec in enumerate(waba_specs)
            ])
        with _jobs_lock:
            s = _live_jobs.get(job_id)
            if s and s.get("status") == "running":
                s["status"] = "stopped" if s.get("stop_requested") else "done"
    except asyncio.CancelledError:
        with _jobs_lock:
            for r in state.get("results", []):
                if r.get("status") == "running":
                    r["status"] = "stopped"
                    r["last_message"] = "Interrompido"
            if state.get("status") not in ("stopped",):
                state["status"] = "stopped"
    finally:
        _loops.pop(job_id, None)
        _main_tasks.pop(job_id, None)


def _thread_runner(job_id: int, user_id: int, waba_specs: list,
                   rows: list, phone_col: str, param_map: list) -> None:
    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_main(job_id, user_id, waba_specs, rows, phone_col, param_map))
        finally:
            loop.close()
            asyncio.set_event_loop(None)
    else:
        asyncio.run(_main(job_id, user_id, waba_specs, rows, phone_col, param_map))


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
        target=_thread_runner,
        args=(job_id, user_id, waba_specs, rows, phone_col, param_map),
        daemon=True,
    )
    t.start()
    return job_id
