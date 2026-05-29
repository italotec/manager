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
