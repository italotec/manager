import os
import json

from flask import (
    Blueprint, render_template, request,
    redirect, url_for, flash, jsonify, current_app,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from .. import db
from ..models import DisparoJob
from ..json_store import load_user_bms
from ..services.meta import get_templates as meta_get_templates
from ..config import Config
from ..services.disparar_service import (
    start_disparo_job,
    csvs_dir,
    sent_log_path,
    disparo_log_path,
    get_csv_columns,
    get_csv_preview,
)

bp = Blueprint("disparar", __name__)

ALLOWED_EXT = {"csv"}


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

        result.append({
            "waba_id": waba_id,
            "name": snap.get("waba_name") or waba_id,
            "token": token,
            "phones": phones,
        })
    return result


def _sent_count(user_id: int) -> int:
    sp = sent_log_path(user_id)
    if not os.path.exists(sp):
        return 0
    with open(sp, "r", encoding="utf-8") as f:
        return sum(1 for ln in f if ln.strip())


# ── main page ─────────────────────────────────────────────────────────────────

@bp.route("/disparar")
@login_required
def disparar_page():
    user_id = current_user.id
    csv_d = csvs_dir(user_id)

    csv_files = []
    for fn in sorted(os.listdir(csv_d)):
        if not fn.lower().endswith(".csv"):
            continue
        path = os.path.join(csv_d, fn)
        try:
            cols = get_csv_columns(path)
            size = os.path.getsize(path)
            # count rows (minus header)
            with open(path, "r", encoding="utf-8-sig") as f:
                row_count = sum(1 for _ in f) - 1
        except Exception:
            cols = []
            size = 0
            row_count = 0
        csv_files.append({
            "name": fn,
            "columns": cols,
            "size_kb": round(size / 1024, 1),
            "row_count": max(0, row_count),
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
        flash("Arquivo inválido. Envie um .csv.", "error")
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


@bp.route("/disparar/csv/<filename>/columns")
@login_required
def csv_columns(filename):
    fn = secure_filename(filename)
    path = os.path.join(csvs_dir(current_user.id), fn)
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    try:
        cols = get_csv_columns(path)
        preview = get_csv_preview(path, n=2)
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
            "name":     t.get("name", ""),
            "category": t.get("category", ""),
            "status":   t.get("status", ""),
            "language": t.get("language", ""),
            "body":     body_text[:120],
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


# ── start job ─────────────────────────────────────────────────────────────────

@bp.route("/disparar/start", methods=["POST"])
@login_required
def start_disparo():
    data = request.get_json(silent=True) or {}

    csv_filename    = (data.get("csv_filename")    or "").strip()
    phone_col       = (data.get("phone_col")       or "").strip()
    phone_number_id = (data.get("phone_number_id") or "").strip()
    token           = (data.get("token")           or "").strip()
    template_name   = (data.get("template_name")   or "").strip()
    param_map       = data.get("param_map", [])
    max_workers     = int(data.get("max_workers") or 1)
    max_workers     = max(1, min(max_workers, 20))  # clamp 1–20

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
        param_map,
        max_workers,
    )
    return jsonify({"job_id": job_id})


# ── job status ────────────────────────────────────────────────────────────────

@bp.route("/disparar/job/<int:job_id>/status")
@login_required
def job_status(job_id):
    job = db.session.get(DisparoJob, job_id)
    if not job or job.user_id != current_user.id:
        return jsonify({"error": "not found"}), 404

    processed = job.sent + job.failed
    remaining = max(0, (job.total - job.skipped) - processed)
    pct = round(processed / max(1, job.total - job.skipped) * 100)

    return jsonify({
        "status":       job.status,
        "total":        job.total,
        "sent":         job.sent,
        "failed":       job.failed,
        "skipped":      job.skipped,
        "remaining":    remaining,
        "pct":          pct,
        "last_message": job.last_message,
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
    job = db.session.get(DisparoJob, job_id)
    if not job or job.user_id != current_user.id:
        return jsonify({"error": "not found"}), 404
    job.stop_requested = True
    db.session.commit()
    return jsonify({"ok": True})
