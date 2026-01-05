import time
from flask import (
    Blueprint,
    render_template,
    current_app,
    redirect,
    url_for,
    flash,
    request,
)
from flask_login import login_required, current_user

from ..json_store import (
    ensure_user_bms_file,
    load_user_bms,
    update_snapshot,
    bms_path,
)
from ..services.meta import (
    get_waba_name,
    get_phone_numbers,
    get_templates,
    templates_status_summary,
)

bp = Blueprint("dashboard", __name__)

API_BLOCKED_MARK = "API access blocked."

@bp.route("/", methods=["GET"])
@login_required
def dashboard():
    ensure_user_bms_file(current_user.id)
    bms = load_user_bms(current_user.id)

    rows = []
    for key, data in (bms or {}).items():
        if not isinstance(data, dict):
            continue

        waba_id = str(data.get("waba_id") or key).strip()
        snap = data.get("snapshot", {}) or {}

        rows.append({
            "waba_id": waba_id,
            "waba_name": snap.get("waba_name") or "—",
            "phone_numbers": snap.get("phone_numbers") or [],
            "t": snap.get("template_counts") or {
                "APPROVED": 0,
                "PAUSED": 0,
                "DISABLED": 0,
                "OTHER": 0,
            },
            "last_sync_at": snap.get("last_sync_at") or 0,
            "status_label": snap.get("status_label") or "",
            "last_error": snap.get("last_error") or "",
            "last_add_phone_error": data.get("last_add_phone_error") or "",
        })

    job_id = request.args.get("job", "")
    return render_template(
        "dashboard.html",
        title="Gerenciador de BM's",
        rows=rows,
        job_id=job_id,
    )


@bp.route("/sync", methods=["POST"])
@login_required
def sync_now():
    ensure_user_bms_file(current_user.id)
    bms = load_user_bms(current_user.id)
    api_version = current_app.config["META_API_VERSION"]

    if not bms:
        flash("Você não tem WABAs cadastrados.", "error")
        return redirect(url_for("dashboard.dashboard"))

    synced = 0
    blocked = 0
    errors = 0

    for key, data in bms.items():
        if not isinstance(data, dict):
            continue

        waba_id = str(data.get("waba_id") or key).strip()
        token = (data.get("token") or "").strip()
        if not waba_id or not token:
            continue

        # --- META CALLS ---
        waba_name, err_name = get_waba_name(api_version, token, waba_id)
        phones, err_phones = get_phone_numbers(api_version, token, waba_id)
        templates, err_tpl = get_templates(api_version, token, waba_id)

        all_errors = " ".join(e for e in (err_name, err_phones, err_tpl) if e)

        # 🟡 CASE: Developers app blocked
        if API_BLOCKED_MARK in all_errors:
            update_snapshot(
                current_user.id,
                waba_id,
                waba_name="—",
                phone_numbers=[],
                template_counts={
                    "APPROVED": 0,
                    "PAUSED": 0,
                    "DISABLED": 0,
                    "OTHER": 0,
                },
                last_error="",
                status_label="Developers Travado",
                last_sync_at=int(time.time()),
            )
            blocked += 1
            continue

        # 🔴 Other errors
        if all_errors:
            update_snapshot(
                current_user.id,
                waba_id,
                waba_name=waba_name or "—",
                phone_numbers=phones or [],
                template_counts=templates_status_summary(templates or []),
                last_error=all_errors[:900],
                status_label="Erro",
                last_sync_at=int(time.time()),
            )
            errors += 1
            continue

        # 🟢 OK
        update_snapshot(
            current_user.id,
            waba_id,
            waba_name=waba_name or "—",
            phone_numbers=phones or [],
            template_counts=templates_status_summary(templates or []),
            last_error="",
            status_label="OK",
            last_sync_at=int(time.time()),
        )
        synced += 1

    flash(
        f"Atualizado • OK: {synced} • Developers travado: {blocked} • Erros: {errors}",
        "success" if synced else "error",
    )
    return redirect(url_for("dashboard.dashboard"))
