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
from werkzeug.utils import secure_filename

from ..json_store import (
    ensure_user_bms_file,
    load_user_bms,
    save_user_bms,
    save_waba_remarks,
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
    register_number,
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
            "ever_had_erro_generic": snap.get("ever_had_erro_generic", False),
            "ultimo_disparo": snap.get("ultimo_disparo") or "",
            "remarks": data.get("remarks") or "",
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

        # Statuses set by the external webhook tool — API sync must not overwrite them.
        # Exception: "DESATIVADA" is NOT protected so the API can recover it to "OK"
        # when the health check no longer shows it as disabled.
        _WEBHOOK_PROTECTED = {"PERMANENTE", "ANALISANDO", "RESTRITA"}
        current_status = prev_snap.get("status_label", "")

        def _effective_status(api_status: str) -> str:
            """Return api_status unless the current status is webhook-protected."""
            return current_status if current_status in _WEBHOOK_PROTECTED else api_status

        if API_BLOCKED_MARK in all_errors:
            update_snapshot(
                current_user.id,
                waba_id,
                waba_name=waba_name or prev_name,
                phone_numbers=[],
                template_counts={"APPROVED": 0, "PAUSED": 0, "DISABLED": 0, "OTHER": 0},
                last_error="",
                status_label=_effective_status("Developers Travado"),
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
                status_label=_effective_status("Erro"),
                last_sync_at=int(time.time()),
            )
            errors += 1
            continue

        # Fetch health status
        health_phones, _ = get_phone_numbers_health(api_version, token, waba_id)
        health_label = evaluate_health(health_phones) if health_phones else "OK"

        # Send test message to detect generic errors (ERRO GENERIC > PROBLEMA CARTÃO)
        if health_label in ("OK", "PROBLEMA CARTÃO") and phones and templates:
            test_tpl = pick_test_template(templates)
            first_phone_id = phones[0].get("id") if phones else None
            if test_tpl and first_phone_id:
                test_ok, test_resp = send_test_message(token, first_phone_id, test_tpl)
                if not test_ok and "#135000" in test_resp:
                    health_label = "ERRO GENERIC"

        # ── tracking fields ──────────────────────────────────────────────
        ever_erro_generic = prev_snap.get("ever_had_erro_generic", False)
        if health_label == "ERRO GENERIC":
            ever_erro_generic = True

        update_snapshot(
            current_user.id,
            waba_id,
            waba_name=waba_name or "—",
            phone_numbers=phones or [],
            template_counts=templates_status_summary(templates or []),
            last_error="",
            status_label=_effective_status(health_label),
            last_sync_at=int(time.time()),
            ever_had_erro_generic=ever_erro_generic,
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
            try:
                r = register_number(api_version, token, phone_id, pin, None)
                try:
                    j = r.json()
                except Exception:
                    j = {}
                if r.status_code == 200 and j.get("success"):
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

    return jsonify({"results": results})

