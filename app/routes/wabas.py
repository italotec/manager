from flask import Blueprint, request, redirect, url_for, flash
from flask_login import login_required, current_user
from ..json_store import upsert_waba, ensure_user_bms_file
from ..services.meta import subscribe_waba_webhook
from ..config import Config

bp = Blueprint("wabas", __name__, url_prefix="/wabas")

@bp.route("/add", methods=["POST"])
@login_required
def add():
    waba_id = (request.form.get("waba_id") or "").strip()
    token = (request.form.get("token") or "").strip()

    if not waba_id or not token:
        flash("Informe WABA ID e Token.", "error")
        return redirect(url_for("dashboard.dashboard"))

    ensure_user_bms_file(current_user.id)

    adspower_profile_id = (request.form.get("adspower_profile_id") or "").strip()

    # Write/update in user's bms.json
    upsert_waba(current_user.id, waba_id=waba_id, token=token, adspower_profile_id=adspower_profile_id)

    # Subscribe app to webhook events for this WABA
    ok, err = subscribe_waba_webhook(Config.META_API_VERSION, token, waba_id)
    if ok:
        flash("WABA adicionado e webhook ativado com sucesso.", "success")
    else:
        flash(f"WABA adicionado, mas falha ao ativar webhook: {err}", "warning")

    return redirect(url_for("dashboard.dashboard"))
