import os
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
from .. import db
from werkzeug.utils import secure_filename

from ..json_store import (
    ensure_user_bms_file,
    load_user_bms,
    save_user_bms,
    save_waba_remarks,
)
from ..services.meta import register_number
from ..services.sync_service import (
    start_sync_job,
    get_job as get_sync_job,
    request_stop as sync_request_stop,
)

bp = Blueprint("dashboard", __name__)


@bp.route("/api-settings")
@login_required
def api_page():
    return render_template("api.html", title="API")


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
            "last_add_phone_debug": data.get("last_add_phone_debug") or [],
            "ever_had_erro_generic": snap.get("ever_had_erro_generic", False),
            "disparou": bool(snap.get("disparou_at")) and (time.time() - (snap.get("disparou_at") or 0)) < 86400,
            "ultimo_disparo": snap.get("ultimo_disparo") or "",
            "remarks": data.get("remarks") or "",
            "adspower_profile_id": data.get("adspower_profile_id") or "",
            "messaging_limit_tier": snap.get("messaging_limit_tier"),
        })

    job_id = request.args.get("job", "")
    return render_template(
        "dashboard.html",
        title="Gerenciador de BM's",
        rows=rows,
        job_id=job_id,
    )

@bp.route("/sync-start", methods=["POST"])
@login_required
def sync_start():
    ensure_user_bms_file(current_user.id)
    bms = load_user_bms(current_user.id)
    if not bms:
        return jsonify({"ok": False, "error": "Você não tem WABAs cadastrados."}), 400
    api_version = current_app.config["META_API_VERSION"]
    job_id = start_sync_job(current_user.id, api_version)
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/sync/job/<int:job_id>")
@login_required
def sync_job_status(job_id: int):
    state = get_sync_job(job_id)
    if state is None:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(state)


@bp.route("/sync/job/<int:job_id>/stop", methods=["POST"])
@login_required
def sync_job_stop(job_id: int):
    sync_request_stop(job_id)
    return jsonify({"ok": True})

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


@bp.route("/waba/<waba_id>/remarks", methods=["POST"])
@login_required
def save_remarks(waba_id):
    text = (request.get_json(silent=True) or {}).get("text", "")
    save_waba_remarks(current_user.id, waba_id, text)
    return jsonify({"ok": True})


@bp.route("/travar-start", methods=["POST"])
@login_required
def travar_start():
    import random
    from ..services.disparar_service import start_disparo_job, csvs_dir

    data = request.get_json(silent=True) or {}
    waba_ids     = data.get("waba_ids") or []
    csv_filename = (data.get("csv_filename") or "").strip()
    phone_col    = (data.get("phone_col")    or "").strip()
    param_map    = data.get("param_map") or []

    if not waba_ids or not csv_filename or not phone_col:
        return jsonify({"error": "Campos obrigatórios faltando."}), 400

    csv_path = os.path.join(csvs_dir(current_user.id), secure_filename(csv_filename))
    if not os.path.exists(csv_path):
        return jsonify({"error": f"CSV '{csv_filename}' não encontrado."}), 404

    api_version = current_app.config["META_API_VERSION"]
    bms = load_user_bms(current_user.id)
    job_ids = []
    errors  = []

    for waba_id in waba_ids:
        entry = bms.get(str(waba_id))
        if not isinstance(entry, dict):
            errors.append(f"{waba_id}: não encontrado no bms.json")
            continue

        token = (entry.get("token") or "").strip()
        snap  = entry.get("snapshot", {}) or {}
        phone_numbers = snap.get("phone_numbers") or []

        phone_number_id = ""
        if phone_numbers:
            phone_number_id = phone_numbers[0].get("id", "")
        if not phone_number_id:
            phone_number_id = (entry.get("phone_number_id") or "").strip()

        if not token:
            errors.append(f"{waba_id}: token vazio")
            continue
        if not phone_number_id:
            errors.append(f"{waba_id}: sem phone_number_id (sincronize o dashboard)")
            continue

        # Fetch templates and pick a random APPROVED one
        templates, err_tpl = get_templates(api_version, token, waba_id)
        if err_tpl or not templates:
            errors.append(f"{waba_id}: erro ao buscar templates — {err_tpl or 'lista vazia'}")
            continue

        approved = [t for t in templates if t.get("status") == "APPROVED"]
        if not approved:
            errors.append(f"{waba_id}: nenhum template APPROVED disponível")
            continue

        chosen = random.choice(approved)
        template_name     = chosen.get("name", "")
        template_language = chosen.get("language", "pt")

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
            1,        # max_workers
            False,    # skip_log
            waba_id,  # waba_id — used to stamp ultimo_disparo on finish
        )
        job_ids.append({
            "waba_id":  waba_id,
            "job_id":   job_id,
            "template": template_name,
            "language": template_language,
        })

    return jsonify({"job_ids": job_ids, "errors": errors})


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


@bp.route("/regenerate-api-key", methods=["POST"])
@login_required
def regenerate_api_key():
    current_user.generate_api_key()
    db.session.commit()
    flash("Nova chave de API gerada com sucesso.", "success")
    return redirect(url_for("dashboard.api_page"))


@bp.route("/open-profiles")
@login_required
def open_profiles():
    from .agent_ws import is_agent_connected, get_open_profiles as agent_open_profiles
    if is_agent_connected(current_user.id):
        return jsonify({"open_profile_ids": list(agent_open_profiles(current_user.id))})
    from ..services.browser_status_poller import get_open_profiles
    return jsonify({"open_profile_ids": list(get_open_profiles())})


@bp.route("/wabas/<waba_id>/open-adspower", methods=["POST"])
@login_required
def open_adspower(waba_id):
    bms = load_user_bms(current_user.id)
    entry = bms.get(str(waba_id))
    if not isinstance(entry, dict):
        return jsonify({"ok": False, "error": "WABA não encontrada."}), 404

    profile_id = (entry.get("adspower_profile_id") or "").strip()
    if not profile_id:
        return jsonify({"ok": False, "error": "Esta WABA não tem um perfil AdsPower vinculado."}), 400

    from .agent_ws import is_agent_connected, push_to_agent
    if is_agent_connected(current_user.id):
        push_to_agent(current_user.id, {
            "type": "open_browser",
            "profile_id": profile_id,
            "cmd_id": None,
        })
        return jsonify({"ok": True})

    try:
        from ..services.adspower import AdsPowerClient
        from ..services.browser_status_poller import register_open
        AdsPowerClient(current_app.config["ADSPOWER_BASE"]).open_browser(profile_id)
        register_open(profile_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:400]}), 500


@bp.route("/register-phones", methods=["POST"])
@login_required
def register_phones():
    """
    Register pending (non-CONNECTED) phone numbers for selected WABAs.
    Expects JSON: { waba_ids: [...], pin: "123456" }
    Returns JSON: { results: [{waba_id, phone_id, phone, ok, msg}, ...] }
    """
    payload = request.get_json(silent=True) or {}
    waba_ids = payload.get("waba_ids") or []
    pin = (payload.get("pin") or "123456").strip()

    if not isinstance(waba_ids, list) or not waba_ids:
        return jsonify({"error": "Selecione pelo menos 1 WABA."}), 400

    api_version = current_app.config["META_API_VERSION"]
    bms = load_user_bms(current_user.id)
    results = []
    dirty = False

    for waba_id in waba_ids:
        entry = bms.get(str(waba_id))
        if not isinstance(entry, dict):
            results.append({
                "waba_id": waba_id, "phone_id": "", "phone": "",
                "ok": False, "msg": "WABA não encontrada",
            })
            continue

        token = (entry.get("token") or "").strip()
        snap = entry.get("snapshot", {}) or {}
        phone_numbers = snap.get("phone_numbers") or []

        pending = [p for p in phone_numbers if (p.get("status") or "").upper() != "CONNECTED"]

        if not pending:
            results.append({
                "waba_id": waba_id, "phone_id": "", "phone": "",
                "ok": True, "msg": "Todos os números já estão registrados",
            })
            continue

        for p in pending:
            phone_id = p.get("id", "")
            display = p.get("display_phone_number", phone_id)
            if not phone_id:
                # Number added via webhook (no phone_number_id yet). Needs a sync first.
                results.append({
                    "waba_id": waba_id, "phone_id": "", "phone": display,
                    "ok": False, "msg": "Sincronize o dashboard antes de registrar (sem phone_number_id).",
                })
                continue
            try:
                r = register_number(api_version, token, phone_id, pin, None)
                try:
                    j = r.json()
                except Exception:
                    j = {}
                if r.status_code == 200 and j.get("success"):
                    p["status"] = "CONNECTED"   # turn the row green (persisted below)
                    dirty = True
                    results.append({
                        "waba_id": waba_id, "phone_id": phone_id, "phone": display,
                        "ok": True, "msg": "Registrado com sucesso",
                    })
                else:
                    err = j.get("error", {})
                    msg = err.get("message") or f"HTTP {r.status_code}"
                    results.append({
                        "waba_id": waba_id, "phone_id": phone_id, "phone": display,
                        "ok": False, "msg": msg,
                    })
            except Exception as e:
                results.append({
                    "waba_id": waba_id, "phone_id": phone_id, "phone": display,
                    "ok": False, "msg": str(e)[:300],
                })

    if dirty:
        save_user_bms(current_user.id, bms)

    return jsonify({"results": results})

