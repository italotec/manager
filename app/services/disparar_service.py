import os
import json
import csv
import threading
import uuid
import random
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

from .. import db
from ..models import DisparoJob

LOCK = threading.Lock()

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


# ── CSV helpers ───────────────────────────────────────────────────────────────

def get_csv_columns(csv_path: str) -> list:
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        return next(csv.reader(f), [])

def get_csv_preview(csv_path: str, n: int = 3) -> list:
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i >= n:
                break
            rows.append(dict(row))
    return rows


# ── Meta API call (runs inside worker threads) ────────────────────────────────

def _send_template(phone: str, phone_number_id: str, token: str,
                   template_name: str, parameters: list, namespace: str) -> tuple:
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
            "language": {"code": "en"},
            "components": components,
        },
    }

    try:
        r = requests.post(api_url, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            return True, f"OK ({r.status_code})"
        return False, f"Erro {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, f"Exceção: {str(exc)[:300]}"


# ── background orchestrator ───────────────────────────────────────────────────

def _run_disparo(app, job_id: int, user_id: int,
                 csv_path: str, phone_col: str,
                 phone_number_id: str, token: str,
                 template_name: str, param_map: list,
                 max_workers: int = 1,
                 skip_log: bool = False):
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
        _live_jobs.pop(job_id, None)

    namespace = _random_namespace()
    sent_path = sent_log_path(user_id)
    log_path = disparo_log_path(user_id, job_id)

    # Load already-sent (skip if skip_log)
    already_sent: set = set()
    if not skip_log and os.path.exists(sent_path):
        with open(sent_path, "r", encoding="utf-8") as f:
            already_sent = {ln.strip() for ln in f if ln.strip()}

    # Read CSV
    try:
        rows = []
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                rows.append(dict(row))
    except Exception as exc:
        _finish("error", f"Erro ao ler CSV: {exc}")
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
            phone, phone_number_id, token, template_name, params, namespace
        )
        return phone, success, msg

    # ── main pool loop ─────────────────────────────────────────────────
    try:
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
                    "ts":      datetime.utcnow().strftime("%H:%M:%S"),
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
                      template_name: str, param_map: list,
                      max_workers: int = 1,
                      skip_log: bool = False) -> int:
    csv_path = os.path.join(csvs_dir(user_id), csv_filename)

    with app.app_context():
        job = DisparoJob(user_id=user_id, status="queued")
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    t = threading.Thread(
        target=_run_disparo,
        args=(app, job_id, user_id, csv_path, phone_col,
              phone_number_id, token, template_name, param_map,
              max_workers, skip_log),
        daemon=True,
    )
    t.start()
    return job_id
