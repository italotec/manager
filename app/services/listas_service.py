"""
listas_service.py — WhatsApp phone-number validation & dedup service.

Two modes
---------
  dedup_only      : Remove duplicate rows by phone column → single output file.
  dedup_validate  : Dedup first, then validate each number via WhatsApp
                    webhook method → 3 output files (has_wa / no_wa / errors).

Background execution
--------------------
  Jobs run in daemon threads (same pattern as disparar_service.py).
  In-memory state dict avoids DB writes during processing.
  Checkpoint JSON enables resume after server restart.
"""

import csv
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter

from .. import db
from ..models import ListaJob, WebhookLog, AppSetting

_SP = ZoneInfo("America/Sao_Paulo")
_LOCK = threading.Lock()
_tls  = threading.local()

# ── in-memory job state ───────────────────────────────────────────────────────
# {job_id: {status, mode, total, has_wa, no_wa, errors, last_message, stop_requested}}
_live_jobs: dict[int, dict] = {}


def get_live_state(job_id: int) -> dict | None:
    return _live_jobs.get(job_id)


def request_stop(job_id: int) -> bool:
    state = _live_jobs.get(job_id)
    if state:
        state["stop_requested"] = True
        return True
    return False


# ── path helpers ──────────────────────────────────────────────────────────────

def listas_dir(user_id: int) -> str:
    path = os.path.join(os.getcwd(), "instance", "users", str(user_id), "listas")
    os.makedirs(path, exist_ok=True)
    return path


def checkpoint_path(user_id: int, job_id: int) -> str:
    return os.path.join(listas_dir(user_id), f"checkpoint_{job_id}.json")


def result_path(user_id: int, job_id: int, kind: str, ext: str) -> str:
    """kind: deduped | whatsapp | no_whatsapp | errors"""
    return os.path.join(listas_dir(user_id), f"job_{job_id}_{kind}{ext}")


# ── file I/O helpers ──────────────────────────────────────────────────────────

def _read_file(path: str) -> tuple[list[dict], str]:
    """Return (rows_as_dicts, ext). ext is '.csv' or '.xlsx'."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        import openpyxl
        # Do NOT use read_only=True — it is unreliable for files from
        # non-Excel apps (Google Sheets, LibreOffice) and can return ws=None.
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        if ws is None:
            wb.close()
            return [], ext
        all_rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not all_rows:
            return [], ext
        headers = [str(c) if c is not None else "" for c in all_rows[0]]
        data_rows = all_rows[1:]
        rows = [dict(zip(headers, [str(v) if v is not None else "" for v in row]))
                for row in data_rows]
        return rows, ext
    else:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        return rows, ".csv"


def _read_file_info(path: str) -> tuple[list[str], int]:
    """
    Efficiently return (column_names, row_count) without loading all data
    into memory. Used for the file listing on the main page.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        if ws is None:
            # Fallback: open without read_only
            wb.close()
            wb = openpyxl.load_workbook(path, data_only=True)
            ws = wb.active
        if ws is None:
            wb.close()
            return [], 0
        headers: list[str] = []
        row_count = 0
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                headers = [str(c) if c is not None else "" for c in row]
            else:
                row_count += 1
        wb.close()
        return headers, row_count
    else:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            headers_row = next(reader, None)
            if headers_row is None:
                return [], 0
            headers = headers_row
            row_count = sum(1 for _ in reader)
        return headers, row_count


def _write_file(rows: list[dict], path: str, ext: str) -> None:
    if not rows:
        # write empty file with headers if we know them, otherwise skip
        return
    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        headers = list(rows[0].keys())
        ws.append(headers)
        for row in rows:
            ws.append([row.get(h, "") for h in headers])
        wb.save(path)
    else:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


# ── phone normalization ───────────────────────────────────────────────────────

def normalize_phone(phone: str, country_code: str = "55") -> str | None:
    digits = re.sub(r"\D", "", str(phone).strip())
    if not digits:
        return None
    if digits.startswith(country_code) and len(digits) >= len(country_code) + 10:
        return digits
    return f"{country_code}{digits}"


# ── checkpoint ────────────────────────────────────────────────────────────────

def _load_checkpoint(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_checkpoint(path: str, data: dict) -> None:
    with _LOCK:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)


# ── Meta API send ─────────────────────────────────────────────────────────────

def _get_session() -> requests.Session:
    if not hasattr(_tls, "session"):
        s = requests.Session()
        s.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=1))
        _tls.session = s
    return _tls.session


def _send_validation_msg(phone: str, token: str, phone_number_id: str,
                          template_name: str, template_language: str,
                          template_body: str,
                          api_version: str = "v18.0") -> str:
    """Send a template message for WA validation. Returns wamid on success, raises on failure."""
    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": template_language},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": template_body}],
                }
            ],
        },
    }
    for attempt in range(1, 4):
        try:
            r = _get_session().post(url, headers=headers, json=payload, timeout=30)
            r.raise_for_status()
            return r.json()["messages"][0]["id"]
        except Exception as exc:
            if attempt == 3:
                raise
            time.sleep(2)


# ── webhook log parsing ───────────────────────────────────────────────────────

_TITLE_HAS_WA = "Business eligibility payment issue"
_TITLE_NO_WA  = "Message undeliverable"


def _extract_statuses(payload_json: str) -> list[dict]:
    """Pull all status entries out of a raw webhook payload JSON string."""
    statuses = []
    try:
        data = json.loads(payload_json)
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                for status in change.get("value", {}).get("statuses", []):
                    statuses.append(status)
    except (json.JSONDecodeError, AttributeError):
        pass
    return statuses


def _classify(status: dict) -> bool | None:
    for error in status.get("errors", []):
        title = error.get("title", "")
        if title == _TITLE_HAS_WA:
            return True
        if title == _TITLE_NO_WA:
            return False
    return None


def _poll_webhook_logs(app, pending_wamids: set, batch_start_time: datetime,
                       poll_attempts: int, poll_interval: int) -> dict[str, bool | None]:
    """
    Query WebhookLog table directly for entries since batch_start_time.
    Returns {wamid: True/False/None} for resolved wamids.
    """
    resolved = {}
    remaining = set(pending_wamids)

    for attempt in range(poll_attempts):
        with app.app_context():
            logs = WebhookLog.query.filter(
                WebhookLog.created_at >= batch_start_time
            ).all()

        for log in logs:
            for status in _extract_statuses(log.payload_json):
                wamid = status.get("id")
                if wamid in remaining:
                    result = _classify(status)
                    if result is not None:
                        resolved[wamid] = result
                        remaining.discard(wamid)

        if not remaining:
            break

        if attempt < poll_attempts - 1:
            time.sleep(poll_interval)

    return resolved


# ── admin config loader ───────────────────────────────────────────────────────

def _get_setting(app, key: str, default: str = "") -> str:
    with app.app_context():
        row = db.session.get(AppSetting, key)
        return row.value if row else default


def _load_admin_config(app) -> dict:
    return {
        "token":             _get_setting(app, "listas_waba_token"),
        "phone_number_id":   _get_setting(app, "listas_phone_number_id"),
        "template_name":     _get_setting(app, "listas_template_name", "atualizacao_cadastro"),
        "template_language": _get_setting(app, "listas_template_language", "en"),
        "template_body":     _get_setting(app, "listas_template_body", "boa tarde, tudo bem?"),
        "batch_size":        int(_get_setting(app, "listas_batch_size", "1000")),
        "country_code":      _get_setting(app, "listas_country_code", "55"),
        "webhook_wait":      int(_get_setting(app, "listas_webhook_wait", "10")),
        "webhook_poll_attempts": int(_get_setting(app, "listas_webhook_poll_attempts", "6")),
        "webhook_poll_interval": int(_get_setting(app, "listas_webhook_poll_interval", "5")),
    }


# ── main background job ───────────────────────────────────────────────────────

def _run_lista_job(app, job_id: int, user_id: int,
                   file_path: str, phone_column: str,
                   max_workers: int, mode: str):
    state = {
        "status": "running",
        "mode": mode,
        "total": 0,
        "has_wa": 0,
        "no_wa": 0,
        "errors": 0,
        "last_message": "Iniciando...",
        "stop_requested": False,
    }
    _live_jobs[job_id] = state

    def _finish(status: str, msg: str):
        state["status"] = status
        state["last_message"] = msg
        with app.app_context():
            job = db.session.get(ListaJob, job_id)
            if job:
                job.status      = status
                job.total       = state["total"]
                job.has_whatsapp = state["has_wa"]
                job.no_whatsapp = state["no_wa"]
                job.errors      = state["errors"]
                job.last_message = msg
                db.session.commit()
        _live_jobs.pop(job_id, None)

    # ── 1. Read file ──────────────────────────────────────────────────────────
    try:
        rows, ext = _read_file(file_path)
    except Exception as exc:
        _finish("error", f"Erro ao ler arquivo: {exc}")
        return

    if not rows:
        _finish("error", "Arquivo vazio.")
        return

    if phone_column not in rows[0]:
        _finish("error", f"Coluna '{phone_column}' não encontrada.")
        return

    # ── 2. Normalize phones ───────────────────────────────────────────────────
    cfg = _load_admin_config(app) if mode == "dedup_validate" else {"country_code": "55"}
    country_code = cfg.get("country_code", "55")

    for row in rows:
        row["_normalized"] = normalize_phone(row.get(phone_column, ""), country_code)

    # ── 3. Dedup by normalized phone ──────────────────────────────────────────
    state["last_message"] = "Deduplicando..."
    seen: set = set()
    deduped_rows = []
    for row in rows:
        key = row["_normalized"] or row.get(phone_column, "")
        if key not in seen:
            seen.add(key)
            deduped_rows.append(row)

    duplicates_removed = len(rows) - len(deduped_rows)
    state["total"] = len(deduped_rows)

    # ── 4. dedup_only mode ────────────────────────────────────────────────────
    if mode == "dedup_only":
        state["last_message"] = f"Salvando {len(deduped_rows)} linhas únicas..."
        clean_rows = [{k: v for k, v in r.items() if k != "_normalized"} for r in deduped_rows]
        out_path = result_path(user_id, job_id, "deduped", ext)
        try:
            _write_file(clean_rows, out_path, ext)
        except Exception as exc:
            _finish("error", f"Erro ao salvar arquivo: {exc}")
            return
        _finish("done", f"Concluído — {len(deduped_rows)} únicos, {duplicates_removed} duplicatas removidas.")
        return

    # ── 5. Validate mode: check admin config ──────────────────────────────────
    if not cfg["token"] or not cfg["phone_number_id"]:
        _finish("error", "Configuração de validação incompleta. Peça ao admin para configurar.")
        return

    # ── 6. Load checkpoint ────────────────────────────────────────────────────
    ckpt_path  = checkpoint_path(user_id, job_id)
    checkpoint = _load_checkpoint(ckpt_path)

    valid_mask = [r for r in deduped_rows if r["_normalized"] is not None]
    pending = [r for r in valid_mask if r["_normalized"] not in checkpoint]

    # Count error rows (no valid phone)
    invalid_rows = [r for r in deduped_rows if r["_normalized"] is None]
    state["errors"] = len(invalid_rows)

    # Restore counts from checkpoint
    for phone, result in checkpoint.items():
        if result is True:
            state["has_wa"] += 1
        elif result is False:
            state["no_wa"] += 1
        else:
            state["errors"] += 1

    batch_size   = cfg["batch_size"]
    batches      = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    total_batches = len(batches)

    state["last_message"] = f"0/{len(pending)} números validados..."

    # ── 7. Batch loop ─────────────────────────────────────────────────────────
    for batch_idx, batch in enumerate(batches, 1):
        if state["stop_requested"]:
            break

        wamid_to_phone: dict[str, str] = {}
        batch_start = datetime.now(_SP)

        # Send template messages in parallel
        def _send_one(row):
            phone = row["_normalized"]
            try:
                wamid = _send_validation_msg(
                    phone,
                    cfg["token"], cfg["phone_number_id"],
                    cfg["template_name"], cfg["template_language"],
                    cfg["template_body"],
                )
                return wamid, phone, None
            except Exception as exc:
                return None, phone, str(exc)

        state["last_message"] = f"Batch {batch_idx}/{total_batches}: enviando {len(batch)} mensagens..."

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_send_one, row): row for row in batch}
            for future in as_completed(futures):
                wamid, phone, err = future.result()
                if wamid:
                    wamid_to_phone[wamid] = phone
                else:
                    checkpoint[phone] = None   # send failed → error
                    state["errors"] += 1

        # Wait for webhooks
        state["last_message"] = f"Batch {batch_idx}/{total_batches}: aguardando webhooks ({cfg['webhook_wait']}s)..."
        time.sleep(cfg["webhook_wait"])

        # Poll webhook logs
        state["last_message"] = f"Batch {batch_idx}/{total_batches}: coletando resultados..."
        resolved = _poll_webhook_logs(
            app,
            set(wamid_to_phone.keys()),
            batch_start,
            cfg["webhook_poll_attempts"],
            cfg["webhook_poll_interval"],
        )

        # Map results back to phones
        for wamid, phone in wamid_to_phone.items():
            result = resolved.get(wamid)   # True / False / None
            checkpoint[phone] = result
            if result is True:
                state["has_wa"] += 1
            elif result is False:
                state["no_wa"] += 1
            else:
                state["errors"] += 1

        _save_checkpoint(ckpt_path, checkpoint)

        done_so_far = state["has_wa"] + state["no_wa"] + state["errors"]
        state["last_message"] = (
            f"Batch {batch_idx}/{total_batches} concluído — "
            f"{done_so_far}/{state['total']} validados"
        )

    # ── 8. Write output files ─────────────────────────────────────────────────
    if state["stop_requested"]:
        _finish("stopped", "Validação interrompida pelo usuário.")
        return

    state["last_message"] = "Gerando arquivos de resultado..."

    # Build lookup: normalized_phone → result
    phone_result = dict(checkpoint)

    # Build per-row clean output (strip _normalized)
    has_wa_rows, no_wa_rows, error_rows = [], [], []
    for row in deduped_rows:
        clean = {k: v for k, v in row.items() if k != "_normalized"}
        phone = row["_normalized"]
        result = phone_result.get(phone)
        if result is True:
            has_wa_rows.append(clean)
        elif result is False:
            no_wa_rows.append(clean)
        else:
            error_rows.append(clean)

    # Also add originally invalid rows (None normalized phone) to errors
    for row in invalid_rows:
        error_rows.append({k: v for k, v in row.items() if k != "_normalized"})

    try:
        if has_wa_rows:
            _write_file(has_wa_rows, result_path(user_id, job_id, "whatsapp", ext), ext)
        if no_wa_rows:
            _write_file(no_wa_rows, result_path(user_id, job_id, "no_whatsapp", ext), ext)
        if error_rows:
            _write_file(error_rows, result_path(user_id, job_id, "errors", ext), ext)
    except Exception as exc:
        _finish("error", f"Erro ao salvar resultados: {exc}")
        return

    _finish(
        "done",
        f"Concluído — Com WA: {state['has_wa']} | Sem WA: {state['no_wa']} | Erros: {state['errors']}",
    )


# ── public API ────────────────────────────────────────────────────────────────

def start_lista_job(app, user_id: int, filename: str,
                    phone_column: str, max_workers: int, mode: str) -> int:
    file_path = os.path.join(listas_dir(user_id), filename)

    with app.app_context():
        job = ListaJob(
            user_id=user_id,
            status="queued",
            mode=mode,
            original_file=filename,
            phone_column=phone_column,
            max_workers=max_workers,
        )
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    t = threading.Thread(
        target=_run_lista_job,
        args=(app, job_id, user_id, file_path, phone_column, max_workers, mode),
        daemon=True,
    )
    t.start()
    return job_id
