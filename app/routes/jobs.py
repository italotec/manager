from flask import Blueprint, request, redirect, url_for, jsonify, flash
from flask_login import login_required, current_user
from ..models import Job
from .. import db
from ..jobs import start_add_phone_job, start_aceitar_bm_job

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

@bp.route("/start/add-phone", methods=["POST"])
@login_required
def start_add_phone():
    waba_ids = request.form.getlist("waba_ids")
    waba_ids = [w.strip() for w in waba_ids if w.strip()]

    if not waba_ids:
        flash("Selecione pelo menos 1 WABA.", "error")
        return redirect(url_for("dashboard.dashboard"))

    job_id = start_add_phone_job(current_user.id, waba_ids)
    return redirect(url_for("dashboard.dashboard", job=job_id))

@bp.route("/start/aceitar-bm", methods=["POST"])
@login_required
def start_aceitar_bm():
    """
    JSON body:
      { "invitations": [ { "profile_id": "k1dg42ii", "invitation_url": "https://..." }, ... ] }
    OR form fields:
      profile_id=k1dg42ii&invitation_url=https://...  (single invitation)
    """
    if request.is_json:
        data = request.get_json(force=True) or {}
        invitations = data.get("invitations", [])
        if not invitations:
            # also allow flat single-item JSON
            pid = data.get("profile_id", "").strip()
            url = data.get("invitation_url", "").strip()
            if pid and url:
                invitations = [{"profile_id": pid, "invitation_url": url}]
    else:
        pid = request.form.get("profile_id", "").strip()
        url = request.form.get("invitation_url", "").strip()
        invitations = [{"profile_id": pid, "invitation_url": url}] if pid and url else []

    if not invitations:
        return jsonify({"ok": False, "error": "Forneça profile_id e invitation_url"}), 400

    job_id = start_aceitar_bm_job(current_user.id, invitations)
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/<int:job_id>/status", methods=["GET"])
@login_required
def job_status(job_id: int):
    job = db.session.get(Job, job_id)
    if not job or job.user_id != current_user.id:
        return jsonify({"error": "not_found"}), 404

    return jsonify({
        "id": job.id,
        "status": job.status,
        "total": job.total,
        "done": job.done,
        "current_label": job.current_label,
        "last_message": job.last_message,
    })
