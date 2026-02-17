import time
from flask import (
    Blueprint,
    render_template,
    current_app,
    redirect,
    url_for,
    flash,
    request,
    jsonify,
)
from flask_login import login_required, current_user

from ..json_store import (
    ensure_user_bms_file,
    load_user_bms,
    save_user_bms,
    update_snapshot,
)
from ..services.meta import (
    get_waba_name,
    get_phone_numbers,
    get_phone_numbers_health,
    get_templates,
    templates_status_summary,
    evaluate_health,
    pick_test_template,
    send_test_message,
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

        waba_id = str(data.get("waba_id") or "").strip()
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

        waba_id = str(data.get("waba_id") or "").strip()
        token = (data.get("token") or "").strip()
        if not waba_id or not token:
            continue

        waba_name, err_name = get_waba_name(api_version, token, waba_id)
        phones, err_phones = get_phone_numbers(api_version, token, waba_id)
        templates, err_tpl = get_templates(api_version, token, waba_id)

        all_errors = " ".join(e for e in (err_name, err_phones, err_tpl) if e)

        # Keep previously saved name when API is blocked
        prev_snap = data.get("snapshot", {}) or {}
        prev_name = prev_snap.get("waba_name") or "—"

        if API_BLOCKED_MARK in all_errors:
            update_snapshot(
                current_user.id,
                waba_id,
                waba_name=waba_name or prev_name,
                phone_numbers=[],
                template_counts={"APPROVED": 0, "PAUSED": 0, "DISABLED": 0, "OTHER": 0},
                last_error="",
                status_label="Developers Travado",
                last_sync_at=int(time.time()),
            )
            blocked += 1
            continue

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

        # Fetch health status
        health_phones, _ = get_phone_numbers_health(api_version, token, waba_id)
        health_label = evaluate_health(health_phones) if health_phones else "OK"

        # Send test message to detect generic errors
        if health_label == "OK" and phones and templates:
            test_tpl = pick_test_template(templates)
            first_phone_id = phones[0].get("id") if phones else None
            if test_tpl and first_phone_id:
                test_ok, test_resp = send_test_message(token, first_phone_id, test_tpl)
                if not test_ok and "#135000" in test_resp:
                    health_label = "ERRO GENERIC"

        update_snapshot(
            current_user.id,
            waba_id,
            waba_name=waba_name or "—",
            phone_numbers=phones or [],
            template_counts=templates_status_summary(templates or []),
            last_error="",
            status_label=health_label,
            last_sync_at=int(time.time()),
        )
        synced += 1

    flash(
        f"Atualizado • OK: {synced} • Developers travado: {blocked} • Erros: {errors}",
        "success" if synced else "error",
    )
    return redirect(url_for("dashboard.dashboard"))

@bp.route("/export-selected", methods=["POST"])
@login_required
def export_selected():
    """
    Gera EXATAMENTE no formato:

    {
        "<KEY ORIGINAL DO bms.json>": {
            "waba_id": "...",
            "phone_number_id": "...",
            "token": "...",
            "templates": [""]
        }
    }
    """
    ensure_user_bms_file(current_user.id)
    bms = load_user_bms(current_user.id)

    payload = request.get_json(silent=True) or {}
    waba_ids = payload.get("waba_ids") or []
    if not isinstance(waba_ids, list):
        return jsonify({"error": "invalid_payload"}), 400

    out = {}

    for original_key, entry in bms.items():
        if not isinstance(entry, dict):
            continue

        waba_id = str(entry.get("waba_id") or "").strip()
        if not waba_id:
            continue

        # só exporta os selecionados
        if waba_id not in waba_ids:
            continue

        # ORDEM IMPORTA ↓↓↓
        out[original_key] = {
            "waba_id": waba_id,
            "phone_number_id": str(entry.get("phone_number_id") or ""),
            "token": str(entry.get("token") or ""),
            "templates": [""],  # SEMPRE EM BRANCO
        }

    return jsonify(out)


@bp.route("/delete-wabas", methods=["POST"])
@login_required
def delete_wabas():
    payload = request.get_json(silent=True) or {}
    waba_ids = payload.get("waba_ids") or []
    if not isinstance(waba_ids, list) or not waba_ids:
        return jsonify({"error": "invalid_payload"}), 400

    bms = load_user_bms(current_user.id)
    deleted = 0
    for key in list(bms.keys()):
        entry = bms.get(key)
        if isinstance(entry, dict):
            wid = str(entry.get("waba_id") or "").strip()
            if wid in waba_ids or key in waba_ids:
                del bms[key]
                deleted += 1

    save_user_bms(current_user.id, bms)
    return jsonify({"deleted": deleted})

