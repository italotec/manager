from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from .. import db
from ..models import User, LoginLog


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or ""

bp = Blueprint("auth", __name__)

@bp.route("/login", methods=["GET"])
def login_get():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.dashboard"))
    return render_template("login.html", title="Login")

@bp.route("/login", methods=["POST"])
def login_post():
    u = request.form.get("username", "").strip()
    p = request.form.get("password", "").strip()

    user = User.query.filter_by(username=u).first()
    if not user or not user.check_password(p):
        flash("Login inválido.", "error")
        return redirect(url_for("auth.login_get"))

    if user.is_banned:
        flash("Sua conta está banida. Fale com o suporte.", "error")
        return redirect(url_for("auth.login_get"))

    session.permanent = True
    login_user(user, remember=True)

    try:
        db.session.add(LoginLog(
            user_id=user.id,
            ip_address=_client_ip(),
            user_agent=request.headers.get("User-Agent", "")[:512],
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()

    return redirect(url_for("dashboard.dashboard"))

@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login_get"))
