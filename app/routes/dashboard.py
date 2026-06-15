import os
import re
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
from ..services.meta import templates_status_summary, get_templates
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
from ..services.health_test_service import (
    start_health_test_job,
    get_job as get_health_test_job,
)

bp = Blueprint("dashboard", __name__)


def _pick_connected_br_phone(phone_numbers: list) -> str:
    """Return the id of the first CONNECTED Brazilian (+55) number, or ''."""
    for p in phone_numbers:
        digits = re.sub(r"\D", "", str(p.get("display_phone_number") or ""))
        if digits.startswith("55") and (p.get("status") or "").upper() == "CONNECTED":
            return p.get("id", "") or ""
    return ""


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

        tpl_map = snap.get("template_status_map")
        t_counts = (
            templates_status_summary(list(tpl_map.values()))
            if isinstance(tpl_map, dict) and tpl_map
            else snap.get("template_counts")
        )
        rows.append({
            "waba_id": waba_id,
            "waba_name": snap.get("waba_name") or "—",
            "phone_numbers": snap.get("phone_numbers") or [],
            "t": t_counts or {
                "APPROVED": 0,
                "PENDING": 0,
                "PAUSED": 0,
                "REJECTED": 0,
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
            "health_ok": bool(snap.get("health_test_ok_at")) and (time.time() - (snap.get("health_test_ok_at") or 0)) < 86400,
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

@bp.route("/dashboard/analytics", methods=["GET"])
@login_required
def dashboard_analytics():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..services.meta import get_waba_analytics

    start_ts = request.args.get("start", type=int)
    end_ts = request.args.get("end", type=int)
    if not start_ts or not end_ts:
        return jsonify({"error": "Parâmetros start e end são obrigatórios."}), 400

    bms = load_user_bms(current_user.id) or {}
    api_version = current_app.config["META_API_VERSION"]

    wabas_disparadas = 0
    total_sent = 0
    total_delivered = 0
    errors = []

    entries = [(wid, data) for wid, data in bms.items() if isinstance(data, dict)]

    # Count disparadas (local, no API) — disparou_at within [start, end]
    for _wid, data in entries:
        snap = data.get("snapshot", {}) or {}
        d_at = snap.get("disparou_at")
        if d_at and start_ts <= d_at <= end_ts:
            wabas_disparadas += 1

    # Fetch sent/delivered in parallel
    def _fetch(wid, data):
        token = (data.get("token") or "").strip()
        if not token:
            return 0, 0, None
        analytics, err = get_waba_analytics(api_version, token, wid, start_ts, end_ts)
        if err:
            return 0, 0, f"{wid}: {err}"
        points = (analytics or {}).get("data_points") or []
        sent = sum(p.get("sent", 0) for p in points)
        delivered = sum(p.get("delivered", 0) for p in points)
        return sent, delivered, None

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch, wid, data): wid for wid, data in entries}
        for future in as_completed(futures):
            try:
                sent, delivered, err = future.result()
                total_sent += sent
                total_delivered += delivered
                if err:
                    errors.append(err)
            except Exception as exc:
                errors.append(f"{futures[future]}: {exc}")

    return jsonify({
        "wabas_disparadas": wabas_disparadas,
        "total_sent": total_sent,
        "total_delivered": total_delivered,
        "waba_count": len(entries),
        "errors": errors,
    })


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

@bp.route("/health-test/start", methods=["POST"])
@login_required
def health_test_start():
    payload = request.get_json(silent=True) or {}
    waba_ids = payload.get("waba_ids") or []
    if not isinstance(waba_ids, list) or not waba_ids:
        return jsonify({"error": "waba_ids inválido"}), 400

    test_phone = current_user.test_phone or ""
    if not test_phone:
        return jsonify({"error": "Telefone de teste não configurado. Acesse Minha Conta e salve um número."}), 400

    api_version = current_app.config.get("META_API_VERSION", "v23.0")
    job_id = start_health_test_job(current_user.id, waba_ids, test_phone, api_version)
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/health-test/job/<int:job_id>")
@login_required
def health_test_job_status(job_id: int):
    state = get_health_test_job(job_id)
    if state is None:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(state)


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
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..services.disparar_service import csvs_dir, _read_rows
    from ..services.travar_service import start_travar_job

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
    user_id = current_user.id

    # Phase 1: fetch templates from Meta in parallel (pure HTTP, no DB writes)
    def _fetch_templates(waba_id):
        entry = bms.get(str(waba_id))
        if not isinstance(entry, dict):
            return waba_id, None, None, None, None, f"{waba_id}: não encontrado no bms.json"

        token = (entry.get("token") or "").strip()
        snap  = entry.get("snapshot", {}) or {}
        phone_numbers = snap.get("phone_numbers") or []
        waba_name = snap.get("name") or entry.get("name") or str(waba_id)

        phone_number_id = _pick_connected_br_phone(phone_numbers)

        if not token:
            return waba_id, None, None, None, None, f"{waba_id}: token vazio"
        if not phone_number_id:
            return waba_id, None, None, None, None, (
                f"{waba_id}: sem número brasileiro (+55) conectado — WABA ignorada"
            )

        try:
            templates, err_tpl = get_templates(api_version, token, waba_id)
        except Exception as exc:
            return waba_id, None, None, None, None, f"{waba_id}: erro ao buscar templates — {exc}"

        if err_tpl or not templates:
            return waba_id, None, None, None, None, f"{waba_id}: erro ao buscar templates — {err_tpl or 'lista vazia'}"

        approved = [t for t in templates if t.get("status") == "APPROVED"]
        if not approved:
            return waba_id, None, None, None, None, f"{waba_id}: nenhum template APPROVED disponível"

        chosen = random.choice(approved)
        return waba_id, phone_number_id, token, chosen, waba_name, None

    errors = []
    fetch_results = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch_templates, wid): wid for wid in waba_ids}
        for future in as_completed(futures):
            try:
                fetch_results.append(future.result())
            except Exception as exc:
                wid = futures[future]
                errors.append(f"{wid}: erro interno — {exc}")

    # Phase 2: build waba_specs and read CSV rows once (no sent_log filtering)
    waba_specs = []
    for waba_id, phone_number_id, token, chosen, waba_name, err in fetch_results:
        if err:
            errors.append(err)
            continue
        waba_specs.append({
            "waba_id": waba_id,
            "waba_name": waba_name or str(waba_id),
            "phone_number_id": phone_number_id,
            "token": token,
            "template_name": chosen.get("name", ""),
            "template_language": chosen.get("language", "pt"),
        })

    if not waba_specs:
        return jsonify({"job_id": None, "wabas": [], "errors": errors}), 400

    try:
        rows = _read_rows(csv_path)
    except Exception as exc:
        return jsonify({"error": f"Erro ao ler CSV: {exc}"}), 500

    if not rows:
        return jsonify({"error": "O CSV está vazio ou sem linhas de dados."}), 400

    job_id = start_travar_job(user_id, waba_specs, rows, phone_col, param_map)

    wabas_out = [
        {"waba_id": s["waba_id"], "waba_name": s["waba_name"],
         "template": s["template_name"], "language": s["template_language"]}
        for s in waba_specs
    ]
    return jsonify({"job_id": job_id, "wabas": wabas_out, "errors": errors})


@bp.route("/travar/job/<int:job_id>")
@login_required
def travar_job_status(job_id: int):
    from ..services.travar_service import get_job
    state = get_job(job_id)
    if state is None:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(state)


@bp.route("/travar/job/<int:job_id>/stop", methods=["POST"])
@login_required
def travar_job_stop(job_id: int):
    from ..services.travar_service import request_stop
    request_stop(job_id)
    return jsonify({"ok": True})


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


# ── Virtual phone number (admin-only, CDP/AdsPower) ───────────────────────────

@bp.route("/add-virtual-phone/start", methods=["POST"])
@login_required
def add_virtual_phone_start():
    if not current_user.is_admin:
        return jsonify({"ok": False, "error": "Acesso restrito a administradores"}), 403

    from ..routes.agent_ws import is_agent_connected
    if not is_agent_connected(current_user.id):
        return jsonify({"ok": False, "error": "Agente não conectado. Conecte o cliente local primeiro."}), 400

    from ..services.virtual_phone_service import start_virtual_phone_job

    payload = request.get_json(silent=True) or {}
    waba_ids = payload.get("waba_ids") or []
    if not isinstance(waba_ids, list) or not waba_ids:
        return jsonify({"ok": False, "error": "Selecione pelo menos 1 WABA"}), 400

    job_id = start_virtual_phone_job(current_user.id, waba_ids)
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/add-virtual-phone/job/<int:job_id>", methods=["GET"])
@login_required
def add_virtual_phone_job_status(job_id: int):
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403

    from ..services.virtual_phone_service import get_job
    state = get_job(job_id)
    if state is None:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(state)


@bp.route("/add-virtual-phone/job/<int:job_id>/stop", methods=["POST"])
@login_required
def add_virtual_phone_job_stop(job_id: int):
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403

    from ..services.virtual_phone_service import request_stop
    request_stop(job_id)
    return jsonify({"ok": True})

