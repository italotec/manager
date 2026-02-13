from flask import Blueprint, render_template, request, jsonify, abort
from flask_login import login_required, current_user

from ..json_store import load_user_bms
from ..config import Config
from ..services.meta import get_templates, create_template

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
    )


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


def _build_template_payload(data: dict):
    """Build Meta API payload from form data. Returns payload dict or (error_str,) tuple."""
    name     = (data.get("name") or "").strip().lower().replace(" ", "_")
    category = (data.get("category") or "").strip().upper()
    language = (data.get("language") or "en").strip()
    body_text = (data.get("body_text") or "").strip()

    if not name:
        return ("Nome do template é obrigatório.",)
    if category not in ("UTILITY", "MARKETING", "AUTHENTICATION"):
        return ("Categoria inválida.",)
    if not body_text:
        return ("Texto do BODY é obrigatório.",)

    components = []

    # HEADER (optional)
    header_type = (data.get("header_type") or "").strip().upper()
    header_text = (data.get("header_text") or "").strip()
    if header_type == "TEXT" and header_text:
        components.append({"type": "HEADER", "format": "TEXT", "text": header_text})

    # BODY (required)
    body_comp: dict = {"type": "BODY", "text": body_text}
    body_examples = data.get("body_examples") or []   # list of sample strings per {{N}}
    if body_examples:
        body_comp["example"] = {"body_text": [body_examples]}
    components.append(body_comp)

    # FOOTER (optional)
    footer_text = (data.get("footer_text") or "").strip()
    if footer_text:
        components.append({"type": "FOOTER", "text": footer_text})

    # BUTTONS (optional)
    buttons = data.get("buttons") or []
    if buttons:
        btn_list = []
        for b in buttons:
            btype = (b.get("type") or "").strip().upper()
            btext = (b.get("text") or "").strip()
            if btype == "QUICK_REPLY" and btext:
                btn_list.append({"type": "QUICK_REPLY", "text": btext})
            elif btype == "URL" and btext:
                btn_list.append({"type": "URL", "text": btext, "url": (b.get("url") or "").strip()})
        if btn_list:
            components.append({"type": "BUTTONS", "buttons": btn_list})

    return {
        "name": name,
        "category": category,
        "language": language,
        "components": components,
    }


def _clone_template_payload(src: dict, new_name: str) -> dict:
    """Build a create-payload from an existing template object."""
    import copy
    components = copy.deepcopy(src.get("components") or [])
    # Remove read-only fields that the API rejects on create
    for comp in components:
        comp.pop("id", None)
    return {
        "name": new_name.lower().replace(" ", "_"),
        "category": src.get("category", "UTILITY"),
        "language": src.get("language", "en"),
        "components": components,
    }
