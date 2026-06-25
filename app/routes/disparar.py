import os
import json
import csv as _csv
import time
import re
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import (
    Blueprint, render_template, request,
    redirect, url_for, flash, jsonify, current_app,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from .. import db
from ..models import DisparoJob
from ..json_store import load_user_bms
from ..services.meta import get_templates as meta_get_templates, _count_body_vars as _count_tpl_vars
from ..config import Config
from ..services.disparar_service import (
    start_disparo_job,
    csvs_dir,
    sent_log_path,
    disparo_log_path,
    get_csv_columns,
    get_csv_preview,
    get_live_state,
    request_stop,
    _read_rows,
    iter_rows,
)
from ..services.disparo_multi import (
    start_batch,
    start_travar_broadcast,
    batch_status as _batch_status,
    batch_stop as _batch_stop,
    build_pool,
    allocate,
    _resolve_tier,
    tier_to_int,
    TIER_VALUES,
)

bp = Blueprint("disparar", __name__)

ALLOWED_EXT = {"csv", "xlsx"}


def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


# ── helpers ───────────────────────────────────────────────────────────────────

def _wabas_with_phones(user_id: int) -> list:
    """
    Return list of WABAs that have at least one phone number in the snapshot,
    ready to use as sender options.
    """
    bms = load_user_bms(user_id)
    result = []
    for waba_id, data in bms.items():
        if not isinstance(data, dict):
            continue
        snap = data.get("snapshot", {}) or {}
        token = data.get("token", "")
        phone_numbers = snap.get("phone_numbers", []) or []

        phones = []
        for p in phone_numbers:
            pid = p.get("id", "")
            display = p.get("display_phone_number", pid)
            if pid:
                phones.append({"phone_number_id": pid, "display": display})

        # Also include the registered phone_number_id if not already present
        reg = data.get("phone_number_id", "")
        if reg and not any(ph["phone_number_id"] == reg for ph in phones):
            phones.append({"phone_number_id": reg, "display": reg})

        tier = snap.get("messaging_limit_tier") or ""
        health_ok_at = snap.get("health_test_ok_at") or 0
        health_ok = bool(health_ok_at) and (time.time() - health_ok_at) < 86400
        disparou_at = snap.get("disparou_at") or 0
        disparou = bool(disparou_at) and (time.time() - disparou_at) < 86400
        result.append({
            "waba_id": waba_id,
            "name": snap.get("waba_name") or waba_id,
            "token": token,
            "phones": phones,
            "tier": tier,
            "health_ok": health_ok,
            "disparou": disparou,
        })
    return result


def _load_sent_set(user_id: int) -> set:
    sp = sent_log_path(user_id)
    if not os.path.exists(sp):
        return set()
    with open(sp, "r", encoding="utf-8") as f:
        return {ln.strip() for ln in f if ln.strip()}


def _sent_count(user_id: int) -> int:
    return len(_load_sent_set(user_id))


# ── main page ─────────────────────────────────────────────────────────────────

@bp.route("/disparar")
@login_required
def disparar_page():
    user_id = current_user.id
    csv_d = csvs_dir(user_id)

    sent_set = _load_sent_set(user_id)

    csv_files = []
    for fn in sorted(os.listdir(csv_d)):
        if not fn.lower().endswith((".csv", ".xlsx")):
            continue
        path = os.path.join(csv_d, fn)
        try:
            size = os.path.getsize(path)
            # Stream the file row-by-row: never hold the whole thing in RAM. Loading
            # a 1M-row file into a list here cost 1-2 GB on every page view → OOM.
            cols = []
            row_count = 0
            sent_in_csv = 0
            for row in iter_rows(path):
                if not cols:
                    cols = list(row.keys())
                row_count += 1
                if any(str(v).strip() in sent_set for v in row.values()):
                    sent_in_csv += 1
        except Exception:
            cols = []
            size = 0
            row_count = 0
            sent_in_csv = 0
        sent_pct = round(sent_in_csv / row_count * 100) if row_count else 0
        csv_files.append({
            "name": fn,
            "columns": cols,
            "size_kb": round(size / 1024, 1),
            "row_count": max(0, row_count),
            "sent_pct": sent_pct,
            "sent_count": sent_in_csv,
        })

    wabas = _wabas_with_phones(user_id)

    return render_template(
        "disparar.html",
        csv_files=csv_files,
        wabas=wabas,
        sent_count=_sent_count(user_id),
    )


# ── CSV management ────────────────────────────────────────────────────────────

@bp.route("/disparar/upload-csv", methods=["POST"])
@login_required
def upload_csv():
    f = request.files.get("csv_file")
    if not f or not f.filename or not _allowed(f.filename):
        flash("Arquivo inválido. Envie um .csv ou .xlsx.", "error")
        return redirect(url_for("disparar.disparar_page"))

    fn = secure_filename(f.filename)
    csv_d = csvs_dir(current_user.id)
    f.save(os.path.join(csv_d, fn))
    flash(f"CSV '{fn}' enviado com sucesso.", "success")
    return redirect(url_for("disparar.disparar_page"))


@bp.route("/disparar/csv/<filename>/delete", methods=["POST"])
@login_required
def delete_csv(filename):
    fn = secure_filename(filename)
    path = os.path.join(csvs_dir(current_user.id), fn)
    if os.path.exists(path):
        os.remove(path)
        flash(f"CSV '{fn}' removido.", "success")
    return redirect(url_for("disparar.disparar_page"))


@bp.route("/disparar/wabas")
@login_required
def disparar_wabas():
    return jsonify({"wabas": _wabas_with_phones(current_user.id)})


@bp.route("/disparar/csv-list")
@login_required
def csv_list():
    csv_d = csvs_dir(current_user.id)
    files = []
    for fn in sorted(os.listdir(csv_d)):
        if fn.lower().endswith((".csv", ".xlsx")):
            try:
                cols = get_csv_columns(os.path.join(csv_d, fn))
            except Exception:
                cols = []
            files.append({"name": fn, "columns": cols})
    return jsonify({"files": files})


@bp.route("/disparar/csv/<filename>/columns")
@login_required
def csv_columns(filename):
    fn = secure_filename(filename)
    path = os.path.join(csvs_dir(current_user.id), fn)
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    has_header = request.args.get("has_header", "1") != "0"
    try:
        cols = get_csv_columns(path, has_header=has_header)
        preview = get_csv_preview(path, n=2, has_header=has_header)
        return jsonify({"columns": cols, "preview": preview})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── template list (from Meta API) ────────────────────────────────────────────

@bp.route("/disparar/waba/<waba_id>/templates")
@login_required
def waba_templates(waba_id):
    bms = load_user_bms(current_user.id)
    waba = bms.get(str(waba_id))
    if not waba or not isinstance(waba, dict):
        return jsonify({"error": "WABA não encontrada."}), 404

    token = waba.get("token", "")
    if not token:
        return jsonify({"error": "Token não encontrado para esta WABA."}), 400

    templates, err = meta_get_templates(Config.META_API_VERSION, token, waba_id)
    if err:
        return jsonify({"error": err}), 502

    # Return only the fields the UI needs
    result = []
    for t in templates:
        body_text = ""
        for comp in (t.get("components") or []):
            if comp.get("type") == "BODY":
                body_text = comp.get("text", "")
                break
        result.append({
            "name":      t.get("name", ""),
            "category":  t.get("category", ""),
            "status":    t.get("status", ""),
            "language":  t.get("language", ""),
            "body":      body_text[:120],
            "var_count": _count_tpl_vars(t),
        })

    return jsonify({"templates": result})


# ── sent-log management ───────────────────────────────────────────────────────

@bp.route("/disparar/sent-log/clear", methods=["POST"])
@login_required
def clear_sent_log():
    sp = sent_log_path(current_user.id)
    open(sp, "w", encoding="utf-8").close()
    flash("Lista de já-enviados limpa com sucesso.", "success")
    return redirect(url_for("disparar.disparar_page"))


def _ordered_sent_lines(user_id: int) -> list:
    sp = sent_log_path(user_id)
    if not os.path.exists(sp):
        return []
    with open(sp, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


@bp.route("/disparar/csv/<filename>/clear-sent", methods=["POST"])
@login_required
def clear_csv_sent(filename):
    fn = secure_filename(filename)
    path = os.path.join(csvs_dir(current_user.id), fn)
    if not os.path.exists(path):
        flash("CSV não encontrado.", "error")
        return redirect(url_for("disparar.disparar_page"))

    phone_col  = (request.form.get("phone_col") or "").strip()
    has_header = request.form.get("has_header", "1") != "0"
    if not phone_col:
        flash("Selecione a coluna do telefone.", "error")
        return redirect(url_for("disparar.disparar_page"))

    try:
        rows = _read_rows(path, has_header=has_header)
    except Exception as exc:
        flash(f"Erro ao ler arquivo: {exc}", "error")
        return redirect(url_for("disparar.disparar_page"))

    csv_phones = {str(r.get(phone_col, "")).strip()
                  for r in rows if str(r.get(phone_col, "")).strip()}

    lines = _ordered_sent_lines(current_user.id)
    remaining = [ln for ln in lines if ln not in csv_phones]
    removed = len(lines) - len(remaining)

    if removed:
        with open(sent_log_path(current_user.id), "w", encoding="utf-8") as f:
            f.write("\n".join(remaining) + ("\n" if remaining else ""))

    flash(f"{removed} número(s) de '{fn}' removido(s) da lista de já-enviados.", "success")
    return redirect(url_for("disparar.disparar_page"))


# ── start job ─────────────────────────────────────────────────────────────────

@bp.route("/disparar/start", methods=["POST"])
@login_required
def start_disparo():
    data = request.get_json(silent=True) or {}

    csv_filename      = (data.get("csv_filename")      or "").strip()
    phone_col         = (data.get("phone_col")         or "").strip()
    phone_number_id   = (data.get("phone_number_id")   or "").strip()
    token             = (data.get("token")             or "").strip()
    template_name     = (data.get("template_name")     or "").strip()
    template_language = (data.get("template_language") or "en").strip()
    param_map         = data.get("param_map", [])
    _w = data.get("max_workers")
    max_workers       = int(_w) if _w is not None else 1
    if max_workers != 0:
        max_workers = max(1, min(max_workers, 500))  # 0 = async MAX mode
    skip_log          = bool(data.get("skip_log"))
    max_leads         = int(data.get("max_leads") or 0)   # 0 = no limit
    waba_id           = (data.get("waba_id") or "").strip()
    has_header        = data.get("has_header", True)

    if not all([csv_filename, phone_col, phone_number_id, token, template_name]):
        return jsonify({"error": "Campos obrigatórios faltando."}), 400

    csv_path = os.path.join(csvs_dir(current_user.id), secure_filename(csv_filename))
    if not os.path.exists(csv_path):
        return jsonify({"error": "CSV não encontrado."}), 404

    job_id = start_disparo_job(
        current_app._get_current_object(),
        current_user.id,
        csv_filename,
        phone_col,
        phone_number_id,
        token,
        template_name,
        template_language,
        param_map,
        max_workers,
        skip_log,
        waba_id,
        has_header,
        max_leads,
    )
    return jsonify({"job_id": job_id})


# ── job status ────────────────────────────────────────────────────────────────

@bp.route("/disparar/job/<int:job_id>/status")
@login_required
def job_status(job_id):
    # Try RAM first (live job), fall back to DB (finished job)
    live = get_live_state(job_id)
    if live:
        total   = live["total"]
        sent    = live["sent"]
        failed  = live["failed"]
        skipped = live["skipped"]
        status  = live["status"]
        last_message = live["last_message"]
    else:
        job = db.session.get(DisparoJob, job_id)
        if not job or job.user_id != current_user.id:
            return jsonify({"error": "not found"}), 404
        total   = job.total
        sent    = job.sent
        failed  = job.failed
        skipped = job.skipped
        status  = job.status
        last_message = job.last_message

    processed = sent + failed
    remaining = max(0, (total - skipped) - processed)
    pct = round(processed / max(1, total - skipped) * 100)

    return jsonify({
        "status":       status,
        "total":        total,
        "sent":         sent,
        "failed":       failed,
        "skipped":      skipped,
        "remaining":    remaining,
        "pct":          pct,
        "last_message": last_message,
    })


# ── job logs (real-time tail) ─────────────────────────────────────────────────

@bp.route("/disparar/job/<int:job_id>/logs")
@login_required
def job_logs(job_id):
    job = db.session.get(DisparoJob, job_id)
    if not job or job.user_id != current_user.id:
        return jsonify({"error": "not found"}), 404

    offset = int(request.args.get("offset", 0))
    log_path = disparo_log_path(current_user.id, job_id)

    entries = []
    new_offset = offset
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        slice_ = all_lines[offset:]
        new_offset = offset + len(slice_)
        for ln in slice_:
            try:
                entries.append(json.loads(ln.strip()))
            except Exception:
                pass

    return jsonify({"entries": entries, "new_offset": new_offset})


# ── stop job ──────────────────────────────────────────────────────────────────

@bp.route("/disparar/job/<int:job_id>/stop", methods=["POST"])
@login_required
def stop_job(job_id):
    if request_stop(job_id):
        return jsonify({"ok": True})
    # Fallback: job might not be live (already finished or stuck)
    return jsonify({"ok": False, "msg": "Job not running"})


# ── batch preview ─────────────────────────────────────────────────────────────

@bp.route("/disparar/batch/preview", methods=["POST"])
@login_required
def batch_preview():
    """
    Given files_spec, wabas_spec, and overage_pct, returns allocation preview:
    pool_size, per-BM quota, stripped list, leftover — without starting any job.
    """
    data = request.get_json(silent=True) or {}
    files_spec   = data.get("files_spec", [])
    wabas_spec   = data.get("wabas_spec", [])
    overage_pct  = float(data.get("overage_pct", 0))
    skip_log     = bool(data.get("skip_log", False))

    if not files_spec or not wabas_spec:
        return jsonify({"error": "files_spec and wabas_spec required"}), 400

    # Resolve tiers — trust tier_str from frontend first, fallback to snapshot
    resolved = []
    for spec in wabas_spec:
        tier = spec.get("tier_str") or _resolve_tier(current_user.id, spec.get("waba_id", ""), spec.get("token", ""))
        if tier_to_int(tier) is None:
            continue
        resolved.append({**spec, "tier_str": tier})

    if not resolved:
        return jsonify({"error": "Nenhum BM com limite de envio válido encontrado."}), 400

    pool = build_pool(current_user.id, files_spec, skip_log)
    alloc = allocate(resolved, len(pool), overage_pct)

    return jsonify({
        "pool_size":      alloc["pool_size"],
        "total_capacity": alloc["total_capacity"],
        "leftover":       alloc["leftover"],
        "stripped":       alloc["stripped"],
        "assignments": [
            {
                "waba_id":  a["waba_id"],
                "name":     a["name"],
                "tier_str": a.get("tier_str"),
                "quota":    a["quota"],
            }
            for a in alloc["assignments"]
        ],
        "error": alloc.get("error"),
    })


# ── batch start ───────────────────────────────────────────────────────────────

@bp.route("/disparar/batch/start", methods=["POST"])
@login_required
def batch_start():
    data = request.get_json(silent=True) or {}

    files_spec     = data.get("files_spec", [])
    wabas_spec     = data.get("wabas_spec", [])
    template_mode  = (data.get("template_mode") or "same").strip()
    templates_cfg  = data.get("templates_cfg", {})
    overage_pct    = float(data.get("overage_pct", 0))
    _w             = data.get("max_workers")
    max_workers    = int(_w) if _w is not None else 1
    if max_workers != 0:
        max_workers = max(1, min(max_workers, 500))
    skip_log       = bool(data.get("skip_log", False))

    if not files_spec:
        return jsonify({"error": "Selecione pelo menos um arquivo."}), 400
    if not wabas_spec:
        return jsonify({"error": "Selecione pelo menos um BM."}), 400

    result = start_batch(
        app=current_app._get_current_object(),
        user_id=current_user.id,
        wabas_spec=wabas_spec,
        files_spec=files_spec,
        template_mode=template_mode,
        templates_cfg=templates_cfg,
        overage_pct=overage_pct,
        max_workers=max_workers,
        skip_log=skip_log,
    )

    if result.get("error") == "insufficient_leads":
        return jsonify({"error": "Leads insuficientes para preencher nem o primeiro BM com a sobra configurada."}), 400
    if result.get("error") == "no_valid_bms":
        return jsonify({"error": "Nenhum BM selecionado tem limite de envio definido. Sincronize o Dashboard."}), 400
    if result.get("error"):
        return jsonify({"error": result["error"]}), 400

    return jsonify(result)


# ── travar broadcast ─────────────────────────────────────────────────────────

def _pick_connected_br_phone(phone_numbers: list) -> str:
    """Return the id of the first CONNECTED Brazilian (+55) number, or ''."""
    for p in phone_numbers:
        digits = re.sub(r"\D", "", str(p.get("display_phone_number") or ""))
        if digits.startswith("55") and (p.get("status") or "").upper() == "CONNECTED":
            return p.get("id", "") or ""
    return ""


@bp.route("/disparar/travar/start", methods=["POST"])
@login_required
def travar_start():
    data = request.get_json(silent=True) or {}
    waba_ids     = data.get("waba_ids") or []
    csv_filename = (data.get("csv_filename") or "").strip()
    phone_col    = (data.get("phone_col") or "").strip()
    param_map    = data.get("param_map") or []
    max_workers  = int(data.get("max_workers") or 1)
    has_header   = data.get("has_header", True)

    if not waba_ids or not csv_filename or not phone_col:
        return jsonify({"error": "Campos obrigatórios faltando."}), 400

    csv_path = os.path.join(csvs_dir(current_user.id), secure_filename(csv_filename))
    if not os.path.exists(csv_path):
        return jsonify({"error": f"CSV '{csv_filename}' não encontrado."}), 404

    api_version = current_app.config["META_API_VERSION"]
    bms = load_user_bms(current_user.id)
    user_id = current_user.id

    def _fetch_templates(waba_id):
        entry = bms.get(str(waba_id))
        if not isinstance(entry, dict):
            return waba_id, None, f"{waba_id}: não encontrado no bms.json"
        token = (entry.get("token") or "").strip()
        snap = entry.get("snapshot", {}) or {}
        phone_numbers = snap.get("phone_numbers") or []
        waba_name = snap.get("name") or entry.get("name") or str(waba_id)
        phone_number_id = _pick_connected_br_phone(phone_numbers)
        if not token:
            return waba_id, None, f"{waba_id}: token vazio"
        if not phone_number_id:
            return waba_id, None, f"{waba_id}: sem número brasileiro (+55) conectado"
        try:
            templates, err_tpl = meta_get_templates(api_version, token, waba_id)
        except Exception as exc:
            return waba_id, None, f"{waba_id}: erro ao buscar templates — {exc}"
        if err_tpl or not templates:
            return waba_id, None, f"{waba_id}: {err_tpl or 'lista vazia'}"
        approved = [t for t in templates if t.get("status") == "APPROVED"]
        if not approved:
            return waba_id, None, f"{waba_id}: nenhum template APPROVED disponível"
        chosen = random.choice(approved)
        return waba_id, {
            "waba_id": waba_id,
            "name": waba_name,
            "phone_number_id": phone_number_id,
            "token": token,
            "template_name": chosen.get("name", ""),
            "template_language": chosen.get("language", "pt"),
        }, None

    errors = []
    wabas_resolved = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch_templates, wid): wid for wid in waba_ids}
        for future in as_completed(futures):
            try:
                waba_id, spec, err = future.result()
            except Exception as exc:
                wid = futures[future]
                errors.append(f"{wid}: erro interno — {exc}")
                continue
            if err:
                errors.append(err)
            else:
                wabas_resolved.append(spec)

    if not wabas_resolved:
        return jsonify({"batch_id": None, "children": [], "errors": errors}), 400

    try:
        rows = _read_rows(csv_path, has_header=has_header)
    except Exception as exc:
        return jsonify({"error": f"Erro ao ler CSV: {exc}"}), 500

    if not rows:
        return jsonify({"error": "O CSV está vazio ou sem linhas de dados."}), 400

    result = start_travar_broadcast(
        app=current_app._get_current_object(),
        user_id=user_id,
        wabas_resolved=wabas_resolved,
        rows=rows,
        phone_col=phone_col,
        param_map=param_map,
        max_workers=max_workers,
        skip_log=True,
    )

    return jsonify({**result, "errors": errors})


# ── batch status ──────────────────────────────────────────────────────────────

@bp.route("/disparar/batch/<batch_id>/status")
@login_required
def batch_status_route(batch_id):
    status = _batch_status(current_user.id, batch_id)
    if status is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(status)


# ── batch stop ────────────────────────────────────────────────────────────────

@bp.route("/disparar/batch/<batch_id>/stop", methods=["POST"])
@login_required
def batch_stop_route(batch_id):
    ok = _batch_stop(current_user.id, batch_id)
    return jsonify({"ok": ok})
