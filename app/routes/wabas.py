from flask import Blueprint, request, redirect, url_for, flash, jsonify, abort
from flask_login import login_required, current_user
from ..json_store import upsert_waba, update_waba, ensure_user_bms_file, load_user_bms
from ..services.meta import subscribe_waba_webhook
from ..services.sync_service import start_sync_job
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
    business_manager_id = (request.form.get("business_manager_id") or "").strip()
    payment_account_id = (request.form.get("payment_account_id") or "").strip()

    # Write/update in user's bms.json
    upsert_waba(current_user.id, waba_id=waba_id, token=token,
                adspower_profile_id=adspower_profile_id,
                business_manager_id=business_manager_id,
                payment_account_id=payment_account_id)

    # Subscribe app to webhook events for this WABA
    ok, err = subscribe_waba_webhook(Config.META_API_VERSION, token, waba_id)
    if ok:
        flash("WABA adicionado e webhook ativado com sucesso.", "success")
    else:
        flash(f"WABA adicionado, mas falha ao ativar webhook: {err}", "warning")

    return redirect(url_for("dashboard.dashboard"))


@bp.route("/<waba_id>/data")
@login_required
def data(waba_id):
    bms = load_user_bms(current_user.id)
    entry = bms.get(str(waba_id))
    if not entry or not isinstance(entry, dict):
        abort(404)
    return jsonify({
        "waba_id": entry.get("waba_id", ""),
        "token": entry.get("token", ""),
        "adspower_profile_id": entry.get("adspower_profile_id", ""),
        "business_manager_id": entry.get("business_manager_id", ""),
        "payment_account_id": entry.get("payment_account_id", ""),
    })


@bp.route("/edit", methods=["POST"])
@login_required
def edit():
    original_waba_id = (request.form.get("original_waba_id") or "").strip()
    waba_id = (request.form.get("waba_id") or "").strip()
    token = (request.form.get("token") or "").strip()

    if not original_waba_id or not waba_id or not token:
        flash("Informe WABA ID e Token.", "error")
        return redirect(url_for("dashboard.dashboard"))

    adspower_profile_id = (request.form.get("adspower_profile_id") or "").strip()
    business_manager_id = (request.form.get("business_manager_id") or "").strip()
    payment_account_id = (request.form.get("payment_account_id") or "").strip()

    ok, err = update_waba(current_user.id, original_waba_id, waba_id, token,
                          adspower_profile_id=adspower_profile_id,
                          business_manager_id=business_manager_id,
                          payment_account_id=payment_account_id)
    if not ok:
        flash(err, "error")
        return redirect(url_for("dashboard.dashboard"))

    webhook_ok, webhook_err = subscribe_waba_webhook(Config.META_API_VERSION, token, waba_id)
    start_sync_job(current_user.id, Config.META_API_VERSION)

    if webhook_ok:
        flash("WABA atualizado — sincronizando dados da Meta.", "success")
    else:
        flash(f"WABA atualizado, mas falha ao ativar webhook: {webhook_err}", "warning")

    return redirect(url_for("dashboard.dashboard"))
