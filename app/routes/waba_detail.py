from flask import Blueprint, render_template, request, jsonify, abort
from flask_login import login_required, current_user

from ..json_store import load_user_bms
from ..config import Config
from ..services.meta import get_templates, create_template, get_waba_analytics
from ..services.template_payload import build_template_payload as _build_template_payload, clone_template_payload as _clone_template_payload

bp = Blueprint("waba_detail", __name__)


def _get_waba_or_404(user_id: int, waba_id: str):
    bms = load_user_bms(user_id)
    entry = bms.get(str(waba_id))
    if not entry or not isinstance(entry, dict):
        abort(404)
    return entry, bms


@bp.route("/waba/<waba_id>")
@login_required
def detail(waba_id):
    entry, bms = _get_waba_or_404(current_user.id, waba_id)
    snap = entry.get("snapshot", {}) or {}
    waba_name = snap.get("waba_name") or waba_id
    phone_numbers = snap.get("phone_numbers") or []

    token = entry.get("token", "")
    templates = []
    tpl_error = None
    if token:
        templates, tpl_error = get_templates(Config.META_API_VERSION, token, waba_id)
    templates = templates or []

    # All user WABAs for duplication target selection
    all_wabas = []
    for wid, data in bms.items():
        if not isinstance(data, dict):
            continue
        s = data.get("snapshot", {}) or {}
        all_wabas.append({
            "waba_id": wid,
            "name": s.get("waba_name") or wid,
        })

    return render_template(
        "waba_detail.html",
        waba_id=waba_id,
        waba_name=waba_name,
        templates=templates,
        tpl_error=tpl_error,
        all_wabas=all_wabas,
        phone_numbers=phone_numbers,
    )


@bp.route("/waba/<waba_id>/analytics", methods=["GET"])
@login_required
def analytics(waba_id):
    """Return analytics JSON for a date range (unix timestamps via query params)."""
    entry, _ = _get_waba_or_404(current_user.id, waba_id)
    token = entry.get("token", "")
    if not token:
        return jsonify({"error": "Token não encontrado."}), 400

    start_ts = request.args.get("start", type=int)
    end_ts = request.args.get("end", type=int)
    if not start_ts or not end_ts:
        return jsonify({"error": "Parâmetros start e end são obrigatórios."}), 400

    data, err = get_waba_analytics(Config.META_API_VERSION, token, waba_id, start_ts, end_ts)
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"analytics": data})


@bp.route("/waba/<waba_id>/conversations")
@login_required
def conversations(waba_id):
    """Return list of conversations (contacts) for a phone number."""
    _get_waba_or_404(current_user.id, waba_id)
    phone_number_id = request.args.get("phone_number_id", "").strip()
    if not phone_number_id:
        return jsonify({"error": "phone_number_id required"}), 400

    from ..services.chat_service import get_conversations
    return jsonify({"conversations": get_conversations(waba_id, phone_number_id)})


@bp.route("/waba/<waba_id>/messages/<contact_wa_id>")
@login_required
def message_history(waba_id, contact_wa_id):
    """Return message history for a specific conversation."""
    _get_waba_or_404(current_user.id, waba_id)
    phone_number_id = request.args.get("phone_number_id", "").strip()
    if not phone_number_id:
        return jsonify({"error": "phone_number_id required"}), 400

    before_id = request.args.get("before_id", type=int)
    from ..services.chat_service import get_message_history
    return jsonify({"messages": get_message_history(
        waba_id, phone_number_id, contact_wa_id, before_id=before_id
    )})


@bp.route("/waba/<waba_id>/messages/send", methods=["POST"])
@login_required
def send_message(waba_id):
    """Send a text or image message to a contact."""
    entry, _ = _get_waba_or_404(current_user.id, waba_id)
    token = entry.get("token", "")
    if not token:
        return jsonify({"error": "Token não encontrado."}), 400

    data            = request.get_json(silent=True) or {}
    phone_number_id = (data.get("phone_number_id") or "").strip()
    to_wa_id        = (data.get("to") or "").strip()
    msg_type        = (data.get("type") or "text").strip()
    body            = (data.get("body") or "").strip()
    image_url       = (data.get("image_url") or "").strip()
    caption         = (data.get("caption") or "").strip()

    if not phone_number_id or not to_wa_id:
        return jsonify({"error": "phone_number_id e to são obrigatórios."}), 400

    from ..services.chat_service import send_text_message, send_image_message, save_message
    from ..models import ChatMessage

    if msg_type == "image":
        if not image_url:
            return jsonify({"error": "image_url obrigatória para tipo image."}), 400
        success, result = send_image_message(token, phone_number_id, to_wa_id, image_url, caption)
        save_body = caption or "[imagem]"
    else:
        if not body:
            return jsonify({"error": "body obrigatório para tipo text."}), 400
        success, result = send_text_message(token, phone_number_id, to_wa_id, body)
        save_body = body

    if not success:
        return jsonify({"error": result}), 502

    # Look up existing contact name from DB
    existing = ChatMessage.query.filter_by(
        waba_id=waba_id, phone_number_id=phone_number_id, contact_wa_id=to_wa_id,
    ).first()
    contact_name = existing.contact_name if existing else to_wa_id

    save_message(
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        contact_wa_id=to_wa_id,
        contact_name=contact_name,
        direction="out",
        msg_type=msg_type,
        body=save_body,
        media_url=image_url if msg_type == "image" else "",
        wamid=result,
        status="sent",
    )
    return jsonify({"ok": True, "wamid": result})


@bp.route("/waba/<waba_id>/webhooks")
@login_required
def webhooks(waba_id):
    """Return paginated webhook payloads received for this WABA (newest first)."""
    import json as _json
    from ..models import WebhookLog

    _get_waba_or_404(current_user.id, waba_id)

    per_page = 50
    page = request.args.get("page", 1, type=int)
    total = WebhookLog.query.filter_by(waba_id=waba_id).count()
    logs = (
        WebhookLog.query
        .filter_by(waba_id=waba_id)
        .order_by(WebhookLog.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    items = []
    for log in logs:
        try:
            payload = _json.loads(log.payload_json)
        except Exception:
            payload = {}
        try:
            field = payload["entry"][0]["changes"][0]["field"]
        except (KeyError, IndexError, TypeError):
            field = "—"
        items.append({
            "id": log.id,
            "field": field,
            "created_at": log.created_at.isoformat(),
            "payload": payload,
        })

    return jsonify({"webhooks": items, "page": page, "total": total, "per_page": per_page})


@bp.route("/waba/<waba_id>/templates/create", methods=["POST"])
@login_required
def template_create(waba_id):
    entry, _ = _get_waba_or_404(current_user.id, waba_id)
    token = entry.get("token", "")
    if not token:
        return jsonify({"error": "Token não encontrado para esta WABA."}), 400

    data = request.get_json(silent=True) or {}
    payload = _build_template_payload(data)
    if isinstance(payload, tuple):          # error
        return jsonify({"error": payload[0]}), 400

    result, err = create_template(Config.META_API_VERSION, token, waba_id, payload)
    if err:
        return jsonify({"error": err}), 502
    return jsonify({"ok": True, "result": result})


@bp.route("/waba/<waba_id>/templates/duplicate", methods=["POST"])
@login_required
def template_duplicate(waba_id):
    """
    Duplicate a template N times (with different names) to one or more target WABAs.
    Body: {
        "template_name": str,
        "base_name": str,        # prefix for generated names
        "copies": int,           # 1-20
        "target_waba_ids": [str] # one or more
    }
    """
    data = request.get_json(silent=True) or {}
    template_name = (data.get("template_name") or "").strip()
    base_name     = (data.get("base_name") or "").strip()
    copies        = max(1, min(int(data.get("copies") or 1), 20))
    target_ids    = data.get("target_waba_ids") or []

    if not template_name or not base_name or not target_ids:
        return jsonify({"error": "Campos obrigatórios faltando."}), 400

    # Load source template from the origin WABA
    src_entry, bms = _get_waba_or_404(current_user.id, waba_id)
    src_token = src_entry.get("token", "")
    if not src_token:
        return jsonify({"error": "Token não encontrado na WABA de origem."}), 400

    src_templates, err = get_templates(Config.META_API_VERSION, src_token, waba_id)
    if err:
        return jsonify({"error": f"Erro ao buscar template: {err}"}), 502

    src_tpl = next((t for t in (src_templates or []) if t.get("name") == template_name), None)
    if not src_tpl:
        return jsonify({"error": f"Template '{template_name}' não encontrado."}), 404

    results = []
    for target_wid in target_ids:
        target_entry = bms.get(str(target_wid))
        if not target_entry or not isinstance(target_entry, dict):
            results.append({"waba_id": target_wid, "copies": [], "error": "WABA não encontrada."})
            continue

        target_token = target_entry.get("token", "")
        if not target_token:
            results.append({"waba_id": target_wid, "copies": [], "error": "Token não encontrado."})
            continue

        # Discover the highest numeric suffix already used for base_name in this WABA
        existing, _ = get_templates(Config.META_API_VERSION, target_token, target_wid)
        max_n = _max_suffix(base_name, existing or [])
        start = max(max_n + 1, 2)   # never start below 02

        copy_results = []
        for i in range(start, start + copies):
            new_name = f"{base_name}{i:02d}"
            payload = _clone_template_payload(src_tpl, new_name)
            res, err = create_template(Config.META_API_VERSION, target_token, target_wid, payload)
            if err:
                copy_results.append({"name": new_name, "ok": False, "error": err})
            else:
                copy_results.append({"name": new_name, "ok": True})

        results.append({"waba_id": target_wid, "copies": copy_results, "error": None})

    return jsonify({"results": results})


# ── helpers ───────────────────────────────────────────────────────────────────

import re as _re

def _max_suffix(base_name: str, templates: list) -> int:
    """Return the highest numeric suffix N found in template names like '{base_name}N'."""
    pattern = _re.compile(r'^' + _re.escape(base_name) + r'(\d+)$')
    max_n = 1
    for t in templates:
        m = pattern.match(t.get("name", ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n
