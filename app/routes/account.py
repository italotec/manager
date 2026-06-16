from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from .. import db
from ..models import User

bp = Blueprint("account", __name__)


@bp.route("/conta")
@login_required
def account_page():
    return render_template("account.html", title="Minha Conta")


@bp.route("/conta/token", methods=["POST"])
@login_required
def save_token():
    token = request.form.get("agent_token", "").strip()

    if token:
        conflict = User.query.filter(
            User.agent_token == token,
            User.id != current_user.id,
        ).first()
        if conflict:
            flash("Este token já está em uso por outro usuário.", "error")
            return redirect(url_for("account.account_page"))
        current_user.agent_token = token
    else:
        current_user.agent_token = None

    db.session.commit()
    flash("Token salvo com sucesso.", "success")
    return redirect(url_for("account.account_page"))


@bp.route("/conta/prosperidade", methods=["POST"])
@login_required
def save_prosperidade_key():
    key = request.form.get("prosperidade_api_key", "").strip()
    current_user.prosperidade_api_key = key or None
    db.session.commit()
    flash("Chave da Prosperidade salva com sucesso.", "success")
    return redirect(url_for("account.account_page"))


@bp.route("/conta/telefone", methods=["POST"])
@login_required
def save_test_phone():
    raw = request.form.get("test_phone", "").strip()
    digits = "".join(c for c in raw if c.isdigit())
    current_user.test_phone = digits or None
    db.session.commit()
    flash("Telefone de teste salvo com sucesso.", "success")
    return redirect(url_for("account.account_page"))
