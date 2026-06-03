"""Shared helper: build and clone Meta message-template payloads."""
from __future__ import annotations
import re as _re


def build_template_payload(data: dict):
    """Build Meta API create payload from form data.

    Returns the payload dict on success, or a (error_str,) tuple on validation failure.
    """
    name      = (data.get("name") or "").strip().lower().replace(" ", "_")
    category  = (data.get("category") or "").strip().upper()
    language  = (data.get("language") or "en").strip()
    body_text = (data.get("body_text") or "").strip()

    if not name:
        return ("Nome do template é obrigatório.",)
    if category not in ("UTILITY", "MARKETING", "AUTHENTICATION"):
        return ("Categoria inválida.",)
    if not body_text:
        return ("Texto do BODY é obrigatório.",)

    components: list[dict] = []

    header_type = (data.get("header_type") or "").strip().upper()
    header_text = (data.get("header_text") or "").strip()
    if header_type == "TEXT" and header_text:
        components.append({"type": "HEADER", "format": "TEXT", "text": header_text})

    body_comp: dict = {"type": "BODY", "text": body_text}
    body_examples = data.get("body_examples") or []
    if body_examples:
        body_comp["example"] = {"body_text": [body_examples]}
    components.append(body_comp)

    footer_text = (data.get("footer_text") or "").strip()
    if footer_text:
        components.append({"type": "FOOTER", "text": footer_text})

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


def clone_template_payload(src: dict, new_name: str) -> dict:
    """Build a create-payload from an existing template object (from Meta API)."""
    import copy
    components = copy.deepcopy(src.get("components") or [])
    for comp in components:
        comp.pop("id", None)
    return {
        "name": new_name.lower().replace(" ", "_"),
        "category": src.get("category", "UTILITY"),
        "language": src.get("language", "en"),
        "components": components,
    }
