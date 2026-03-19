import os
import json
import csv
import asyncio
import threading
import uuid
import random
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
_SP = ZoneInfo("America/Sao_Paulo")

import requests
from requests.adapters import HTTPAdapter

from .. import db
from ..models import DisparoJob
from ..json_store import patch_snapshot

LOCK = threading.Lock()
_tls = threading.local()


def _get_session() -> requests.Session:
    """One persistent HTTP session per worker thread — avoids shared pool contention."""
    if not hasattr(_tls, "session"):
        s = requests.Session()
        s.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=1))
        _tls.session = s
    return _tls.session

# ── in-memory job state (no DB during sending) ───────────────────────────────
# {job_id: {status, total, sent, failed, skipped, last_message, stop_requested}}
_live_jobs: dict[int, dict] = {}


def get_live_state(job_id: int) -> dict | None:
    """Return in-memory state for a running job, or None if not live."""
    return _live_jobs.get(job_id)


def request_stop(job_id: int) -> bool:
    """Set stop flag in RAM. Returns True if job was live."""
    state = _live_jobs.get(job_id)
    if state:
        state["stop_requested"] = True
        return True
    return False


# ── path helpers ──────────────────────────────────────────────────────────────

def _user_base(user_id: int) -> str:
    base = os.path.join(os.getcwd(), "instance", "users", str(user_id))
    os.makedirs(base, exist_ok=True)
    return base

def csvs_dir(user_id: int) -> str:
    path = os.path.join(_user_base(user_id), "csvs")
    os.makedirs(path, exist_ok=True)
    return path

def sent_log_path(user_id: int) -> str:
    return os.path.join(_user_base(user_id), "sent_log.txt")

def disparo_log_path(user_id: int, job_id: int) -> str:
    return os.path.join(_user_base(user_id), f"disparo_log_{job_id}.jsonl")


# ── random generators ─────────────────────────────────────────────────────────

def _random_namespace() -> str:
    return str(uuid.uuid4()).replace("-", "_")

def _random_param_name(length: int = 7) -> str:
    first = random.choice(string.ascii_lowercase)
    rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=length - 1))
    return first + rest


# ── CSV / XLSX helpers ────────────────────────────────────────────────────────

def _read_rows(path: str, has_header: bool = True) -> list:
    """Read all rows as list of dicts. Supports .csv and .xlsx."""
    if path.lower().endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not all_rows:
            return []
        if has_header:
            headers = [str(c) if c is not None else "" for c in all_rows[0]]
            data_rows = all_rows[1:]
        else:
            headers = [f"Coluna {i+1}" for i in range(len(all_rows[0]))]
            data_rows = all_rows
        return [dict(zip(headers, [str(v) if v is not None else "" for v in row])) for row in data_rows]
    else:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            raw = list(csv.reader(f))
        if not raw:
            return []
        if has_header:
            headers = raw[0]
            data_rows = raw[1:]
        else:
            headers = [f"Coluna {i+1}" for i in range(len(raw[0]))]
            data_rows = raw
        return [dict(zip(headers, row)) for row in data_rows]


def get_csv_columns(csv_path: str, has_header: bool = True) -> list:
    rows = _read_rows(csv_path, has_header=has_header)
    if not rows:
        return []
    return list(rows[0].keys())

def get_csv_preview(csv_path: str, n: int = 3, has_header: bool = True) -> list:
    return _read_rows(csv_path, has_header=has_header)[:n]


# ── Meta API call (runs inside worker threads) ────────────────────────────────

def _send_template(phone: str, phone_number_id: str, token: str,
                   template_name: str, template_language: str,
                   parameters: list, namespace: str) -> tuple:
    """Returns (success: bool, message: str). Pure HTTP — no DB/file access."""
    api_url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    components = []
    if parameters:
        components.append({
            "type": "body",
            "parameters": [
                {"type": "text", "parameter_name": p["name"], "text": p["value"]}
                for p in parameters
            ],
        })

    payload = {
        "messaging_product": "whatsapp",
        "type": "template",
        "to": phone,
        "template": {
            "namespace": namespace,
            "name": template_name,
            "language": {"code": template_language},
            "components": components,
        },
    }

    try:
        r = _get_session().post(api_url, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            return True, f"OK ({r.status_code})"
        return False, f"Erro {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, f"Exceção: {str(exc)[:300]}"


# ── async MAX mode ────────────────────────────────────────────────────────────

async def _send_template_async(session, phone, phone_number_id, token,
                               template_name, template_language, parameters, namespace):
    url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
    hdrs = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    components = []
    if parameters:
        components.append({
            "type": "body",
            "parameters": [
                {"type": "text", "parameter_name": p["name"], "text": p["value"]}
                for p in parameters
            ],
        })
    payload = {
        "messaging_product": "whatsapp",
        "type": "template",
        "to": phone,
        "template": {
            "namespace": namespace,
            "name": template_name,
            "language": {"code": template_language},
            "components": components,
        },
    }
    import aiohttp as _aiohttp
    try:
        async with session.post(url, headers=hdrs, json=payload,
                                timeout=_aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 200:
                return True, f"OK ({r.status})"
            text = await r.text()
            return False, f"Erro {r.status}: {text[:300]}"
    except Exception as exc:
        return False, f"Exceção: {str(exc)[:300]}"


async def _run_async_jobs(state, pending, phone_col, phone_number_id, token,
                          template_name, template_language, param_map, namespace,
                          sent_path, log_path, skip_log):
    import aiohttp as _aiohttp
    sem = asyncio.Semaphore(300)
    # Collect results in memory — no file I/O inside coroutines (would block event loop)
    results = []  # list of (phone, success, msg, ts)

    connector = _aiohttp.TCPConnector(limit=300, limit_per_host=300)
    async with _aiohttp.ClientSession(connector=connector) as session:
        async def _task(row):
            async with sem:
                if state["stop_requested"]:
                    return
                phone = str(row.get(phone_col, "")).strip()
                if not phone:
                    state["skipped"] += 1
                    return
                params = [
                    {"name": pm["name"],
                     "value": str(row.get(pm.get("column", ""), "")).strip()}
                    for pm in param_map
                ]
                success, msg = await _send_template_async(
                    session, phone, phone_number_id, token,
                    template_name, template_language, params, namespace)
                ts = datetime.now(_SP).strftime("%H:%M:%S")
                results.append((phone, success, msg, ts))
                # update in-memory counters (asyncio is single-threaded — no races)
                if success:
                    state["sent"] += 1
                else:
                    state["failed"] += 1
                state["last_message"] = f"{'✓' if success else '✗'} {phone}: {msg}"

        await asyncio.gather(*[_task(row) for row in pending])

    # Batch write all logs after all requests complete
    if results:
        if not skip_log:
            sent_phones = [p for p, ok, _, _ in results if ok]
            if sent_phones:
                with open(sent_path, "a", encoding="utf-8") as sf:
                    sf.write("\n".join(sent_phones) + "\n")
        with open(log_path, "a", encoding="utf-8") as lf:
            for phone, success, msg, ts in results:
                lf.write(json.dumps({
                    "ts": ts, "phone": phone,
                    "status": "sent" if success else "failed",
                    "message": msg,
                }, ensure_ascii=False) + "\n")


# ── background orchestrator ───────────────────────────────────────────────────

def _run_disparo(app, job_id: int, user_id: int,
                 csv_path: str, phone_col: str,
                 phone_number_id: str, token: str,
                 template_name: str, template_language: str,
                 param_map: list,
                 max_workers: int = 1,
                 skip_log: bool = False,
                 waba_id: str = "",
                 has_header: bool = True):
    """
    Runs in a single daemon thread (the 'orchestrator').
    All counters live in RAM (_live_jobs). DB is only written at start and end.
    """
    # Init in-memory state
    state = {
        "status": "running",
        "total": 0, "sent": 0, "failed": 0, "skipped": 0,
        "last_message": "", "stop_requested": False,
    }
    _live_jobs[job_id] = state

    def _finish(status: str, msg: str):
        state["status"] = status
        state["last_message"] = msg
        # Single DB write at the end
        with app.app_context():
            job = db.session.get(DisparoJob, job_id)
            if job:
                job.status = status
                job.total = state["total"]
                job.sent = state["sent"]
                job.failed = state["failed"]
                job.skipped = state["skipped"]
                job.last_message = msg
                db.session.commit()
        # Stamp ultimo_disparo when the job ends (manually or automatically)
        if waba_id and status in ("done", "stopped"):
            patch_snapshot(
                user_id, waba_id,
                ultimo_disparo=datetime.now(_SP).strftime("%d/%m %H:%M"),
            )
        _live_jobs.pop(job_id, None)

    namespace = _random_namespace()
    sent_path = sent_log_path(user_id)
    log_path = disparo_log_path(user_id, job_id)

    # Load already-sent (skip if skip_log)
    already_sent: set = set()
    if not skip_log and os.path.exists(sent_path):
        with open(sent_path, "r", encoding="utf-8") as f:
            already_sent = {ln.strip() for ln in f if ln.strip()}

    # Read CSV / XLSX
    try:
        rows = _read_rows(csv_path, has_header=has_header)
    except Exception as exc:
        _finish("error", f"Erro ao ler arquivo: {exc}")
        return

    if rows and phone_col not in rows[0]:
        _finish("error", f"Coluna '{phone_col}' não encontrada no CSV.")
        return

    pending = [r for r in rows if str(r.get(phone_col, "")).strip() not in already_sent]

    state["total"] = len(rows)
    state["skipped"] = len(rows) - len(pending)

    if not pending:
        _finish("done", f"Concluído — Enviados: 0  |  Falhas: 0  |  Pulados: {state['skipped']}")
        return

    def _append_log(entry: dict):
        with LOCK:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _worker(row: dict):
        """Pure HTTP worker — returns (phone, success, msg)."""
        phone = str(row.get(phone_col, "")).strip()
        if not phone:
            return phone, None, "telefone vazio"
        params = [
            {"name": pm["name"],
             "value": str(row.get(pm.get("column", ""), "")).strip()}
            for pm in param_map
        ]
        success, msg = _send_template(
            phone, phone_number_id, token, template_name, template_language, params, namespace
        )
        return phone, success, msg

    # ── main pool loop ─────────────────────────────────────────────────
    try:
        if max_workers == 0:
            # MAX mode: async I/O via aiohttp — 300 concurrent requests, single OS thread
            asyncio.run(_run_async_jobs(
                state, pending, phone_col, phone_number_id, token,
                template_name, template_language, param_map, namespace,
                sent_path, log_path, skip_log,
            ))
            if state["stop_requested"]:
                _finish("stopped", "Envio interrompido pelo usuário.")
                return
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {executor.submit(_worker, row): row for row in pending}

                for future in as_completed(future_map):
                    if state["stop_requested"]:
                        for f in future_map:
                            f.cancel()
                        _finish("stopped", "Envio interrompido pelo usuário.")
                        return

                    try:
                        phone, success, msg = future.result()
                    except Exception as exc:
                        phone, success, msg = "?", False, str(exc)

                    if success is None:           # empty phone
                        state["skipped"] += 1
                    elif success:
                        state["sent"] += 1
                        if not skip_log:
                            with LOCK:
                                with open(sent_path, "a", encoding="utf-8") as sf:
                                    sf.write(phone + "\n")
                    else:
                        state["failed"] += 1

                    _append_log({
                        "ts":      datetime.now(_SP).strftime("%H:%M:%S"),
                        "phone":   phone,
                        "status":  "sent" if success else "failed",
                        "message": msg,
                    })

                    icon = "✓" if success else "✗"
                    state["last_message"] = f"{icon} {phone}: {msg}"

    except Exception as exc:
        _finish("error", f"Erro inesperado: {str(exc)[:300]}")
        return

    _finish(
        "done",
        f"Concluído — Enviados: {state['sent']}  |  Falhas: {state['failed']}  |  Pulados: {state['skipped']}",
    )


# ── public API ────────────────────────────────────────────────────────────────

def start_disparo_job(app, user_id: int, csv_filename: str,
                      phone_col: str, phone_number_id: str, token: str,
                      template_name: str, template_language: str,
                      param_map: list,
                      max_workers: int = 1,
                      skip_log: bool = False,
                      waba_id: str = "",
                      has_header: bool = True) -> int:
    csv_path = os.path.join(csvs_dir(user_id), csv_filename)

    with app.app_context():
        job = DisparoJob(user_id=user_id, status="queued")
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    t = threading.Thread(
        target=_run_disparo,
        args=(app, job_id, user_id, csv_path, phone_col,
              phone_number_id, token, template_name, template_language,
              param_map, max_workers, skip_log, waba_id, has_header),
        daemon=True,
    )
    t.start()
    return job_id
