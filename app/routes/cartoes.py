"""Blueprint: Cartões — card management + bulk WABA card-add."""
from __future__ import annotations
import re

from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user

from ..models import Card
from .. import db
from ..services.card_brand import detect_brand, luhn_valid

bp = Blueprint("cartoes", __name__, url_prefix="/cartoes")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_expiry(raw: str) -> tuple[str, str]:
    """Parse 'MM/YY' or 'MM/YYYY' → (month_str, year_4_str)."""
    parts = re.split(r"[/\-]", raw.strip())
    if len(parts) != 2:
        return "", ""
    mm, yy = parts[0].strip(), parts[1].strip()
    month = str(int(mm)) if mm.isdigit() else ""
    year = yy if len(yy) == 4 else ("20" + yy if len(yy) == 2 else "")
    return month, year


def _create_card(user_id: int, number: str, expiry: str, csc: str, name: str) -> tuple[Card | None, str]:
    """Validate and create a Card. Returns (card, error_str)."""
    digits = re.sub(r"\D", "", number)
    if len(digits) < 13 or len(digits) > 19:
        return None, "Número de cartão inválido"
    if not luhn_valid(digits):
        return None, "Número de cartão inválido (Luhn)"

    month, year = _parse_expiry(expiry)
    if not month or not year:
        return None, "Validade inválida (use MM/AA ou MM/AAAA)"

    csc_clean = re.sub(r"\D", "", csc)
    if len(csc_clean) < 3:
        return None, "CVV inválido"

    brand = detect_brand(digits)
    card = Card(
        user_id=user_id,
        number=digits,
        exp_month=month,
        exp_year=year,
        csc=csc_clean,
        holder_name=(name or "").strip()[:128],
        brand=brand,
        bin=digits[:8],
        last4=digits[-4:],
    )
    db.session.add(card)
    db.session.commit()
    return card, ""


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route("")
@login_required
def cartoes_page():
    cards = (
        Card.query
        .filter_by(user_id=current_user.id)
        .order_by(Card.created_at.desc())
        .all()
    )
    return render_template("cartoes.html", cards=cards)


@bp.route("/list")
@login_required
def list_cards():
    cards = Card.query.filter_by(user_id=current_user.id).order_by(Card.created_at.desc()).all()
    return jsonify({"cards": [c.to_dict() for c in cards]})


@bp.route("/add", methods=["POST"])
@login_required
def add_card():
    payload = request.get_json(silent=True) or {}

    # Bulk paste: list of {number, expiry, csc, name} or raw lines "num|MM/YY|cvv|name"
    bulk_lines = payload.get("bulk") or ""
    if bulk_lines:
        added, errors = [], []
        for raw_line in bulk_lines.strip().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = [p.strip() for p in re.split(r"[|;,\t]", line)]
            if len(parts) < 3:
                errors.append(f"Linha ignorada (formato inválido): {line[:40]}")
                continue
            num = parts[0]
            exp = parts[1]
            csc = parts[2]
            name = parts[3] if len(parts) > 3 else ""
            card, err = _create_card(current_user.id, num, exp, csc, name)
            if err:
                errors.append(f"{num[-4:]}: {err}")
            else:
                added.append(card.to_dict())
        return jsonify({"ok": True, "added": added, "errors": errors})

    # Single card
    number = payload.get("number", "")
    expiry = payload.get("expiry", "")
    csc = payload.get("csc", "")
    name = payload.get("name", "")
    if not number or not expiry or not csc:
        return jsonify({"ok": False, "error": "Campos obrigatórios: número, validade, CVV"}), 400

    card, err = _create_card(current_user.id, number, expiry, csc, name)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "card": card.to_dict()})


@bp.route("/delete", methods=["POST"])
@login_required
def delete_cards():
    payload = request.get_json(silent=True) or {}
    card_ids = payload.get("card_ids") or []
    if not isinstance(card_ids, list) or not card_ids:
        return jsonify({"ok": False, "error": "Nenhum cartão selecionado"}), 400

    deleted = (
        Card.query
        .filter(Card.user_id == current_user.id, Card.id.in_(card_ids))
        .delete(synchronize_session=False)
    )
    db.session.commit()
    return jsonify({"ok": True, "deleted": deleted})


@bp.route("/bulk-add-start", methods=["POST"])
@login_required
def bulk_add_start():
    from ..services.card_service import start_card_job
    from ..routes.agent_ws import is_agent_connected

    if not is_agent_connected(current_user.id):
        return jsonify({"ok": False, "error": "Agente não conectado. Conecte o cliente local primeiro."}), 400

    payload = request.get_json(silent=True) or {}
    waba_ids = payload.get("waba_ids") or []
    if not isinstance(waba_ids, list) or not waba_ids:
        return jsonify({"ok": False, "error": "Selecione pelo menos 1 WABA"}), 400

    # Quick check: any cards available at all?
    available = Card.query.filter_by(user_id=current_user.id, status="active").count()
    if available == 0:
        return jsonify({"ok": False, "error": "Nenhum cartão disponível. Adicione cartões primeiro."}), 400

    job_id = start_card_job(current_user.id, waba_ids)
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/job/<int:job_id>")
@login_required
def job_status(job_id: int):
    from ..services.card_service import get_job
    state = get_job(job_id)
    if state is None:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(state)


@bp.route("/job/<int:job_id>/stop", methods=["POST"])
@login_required
def stop_job(job_id: int):
    from ..services.card_service import request_stop
    request_stop(job_id)
    return jsonify({"ok": True})
