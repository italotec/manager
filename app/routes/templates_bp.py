"""Blueprint: Templates — model library + bulk WABA template creation."""
from __future__ import annotations
import json

from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user

from ..models import TemplateModel
from .. import db
from ..services.template_payload import build_template_payload

bp = Blueprint("templates", __name__, url_prefix="/templates")


@bp.route("")
@login_required
def templates_page():
    models = (
        TemplateModel.query
        .filter_by(user_id=current_user.id)
        .order_by(TemplateModel.created_at.desc())
        .all()
    )
    return render_template("template_models.html", models=models)


@bp.route("/list")
@login_required
def list_models():
    models = (
        TemplateModel.query
        .filter_by(user_id=current_user.id)
        .order_by(TemplateModel.name)
        .all()
    )
    return jsonify({"models": [m.to_dict() for m in models]})


@bp.route("/add", methods=["POST"])
@login_required
def add_model():
    data = request.get_json(silent=True) or {}
    payload = build_template_payload(data)
    if isinstance(payload, tuple):
        return jsonify({"ok": False, "error": payload[0]}), 400

    model = TemplateModel(
        user_id=current_user.id,
        name=payload["name"],
        category=payload["category"],
        language=payload["language"],
        payload_json=json.dumps({
            "category":   payload["category"],
            "language":   payload["language"],
            "components": payload["components"],
        }),
    )
    db.session.add(model)
    db.session.commit()
    return jsonify({"ok": True, "model": model.to_dict()})


@bp.route("/delete", methods=["POST"])
@login_required
def delete_models():
    data = request.get_json(silent=True) or {}
    model_ids = data.get("model_ids") or []
    if not isinstance(model_ids, list) or not model_ids:
        return jsonify({"ok": False, "error": "Nenhum modelo selecionado"}), 400

    deleted = (
        TemplateModel.query
        .filter(TemplateModel.user_id == current_user.id, TemplateModel.id.in_(model_ids))
        .delete(synchronize_session=False)
    )
    db.session.commit()
    return jsonify({"ok": True, "deleted": deleted})


@bp.route("/bulk-create-start", methods=["POST"])
@login_required
def bulk_create_start():
    from ..services.template_service import start_template_job

    data = request.get_json(silent=True) or {}
    model_id = data.get("model_id")
    quantity = int(data.get("quantity") or 1)
    waba_ids = data.get("waba_ids") or []

    if not model_id:
        return jsonify({"ok": False, "error": "Selecione um modelo de template."}), 400
    if quantity < 1 or quantity > 500:
        return jsonify({"ok": False, "error": "Quantidade deve ser entre 1 e 500."}), 400
    if not isinstance(waba_ids, list) or not waba_ids:
        return jsonify({"ok": False, "error": "Selecione pelo menos 1 WABA."}), 400

    model = TemplateModel.query.filter_by(id=model_id, user_id=current_user.id).first()
    if not model:
        return jsonify({"ok": False, "error": "Modelo não encontrado."}), 404

    try:
        job_id = start_template_job(current_user.id, model_id, quantity, waba_ids)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/job/<int:job_id>")
@login_required
def job_status(job_id: int):
    from ..services.template_service import get_job
    state = get_job(job_id)
    if state is None:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(state)


@bp.route("/job/<int:job_id>/stop", methods=["POST"])
@login_required
def stop_job(job_id: int):
    from ..services.template_service import request_stop
    request_stop(job_id)
    return jsonify({"ok": True})
