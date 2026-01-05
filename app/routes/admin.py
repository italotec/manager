from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from .. import db
from ..models import User, BalanceTx, Waba
from ..json_store import ensure_user_bms_file

bp = Blueprint("admin", __name__, url_prefix="/admin")

def _is_admin():
    return current_user.is_authenticated and bool(getattr(current_user, "is_admin", False))

@bp.before_request
def guard():
    if not _is_admin():
        return redirect(url_for("dashboard.dashboard"))

@bp.route("/users", methods=["GET"])
@login_required
def admin_users():
    users = User.query.order_by(User.is_admin.desc(), User.id.asc()).all()

    now = datetime.utcnow()
    start_today = datetime(now.year, now.month, now.day)
    start_week = now - timedelta(days=7)

    stats = {}
    for u in users:
        # Opção A: OTP debitados
        q = BalanceTx.query.filter_by(user_id=u.id).filter(BalanceTx.reason.like("OTP recebido%"))
        total = q.count()
        today = q.filter(BalanceTx.created_at >= start_today).count()
        week = q.filter(BalanceTx.created_at >= start_week).count()
        stats[u.id] = {"today": today, "week": week, "total": total}

    return render_template("admin_users.html", title="Admin • Usuários", users=users, stats=stats)

@bp.route("/users/create", methods=["POST"])
@login_required
def admin_create_user():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not username or not password:
        flash("Informe username e password.", "error")
        return redirect(url_for("admin.admin_users"))

    if User.query.filter_by(username=username).first():
        flash("Usuário já existe.", "error")
        return redirect(url_for("admin.admin_users"))

    u = User(username=username, is_admin=False, is_banned=False, balance_cents=0)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()

    # Create per-user bms.json file
    ensure_user_bms_file(u.id)

    flash("Usuário criado com sucesso.", "success")
    return redirect(url_for("admin.admin_users"))

@bp.route("/users/<int:user_id>/toggle-ban", methods=["POST"])
@login_required
def admin_toggle_ban(user_id: int):
    u = db.session.get(User, user_id)
    if not u:
        flash("Usuário não encontrado.", "error")
        return redirect(url_for("admin.admin_users"))

    if u.is_admin:
        flash("Não é permitido banir admin.", "error")
        return redirect(url_for("admin.admin_users"))

    u.is_banned = not u.is_banned
    db.session.commit()

    flash("Status atualizado.", "success")
    return redirect(url_for("admin.admin_users"))

@bp.route("/users/<int:user_id>", methods=["GET"])
@login_required
def admin_user_detail(user_id: int):
    u = db.session.get(User, user_id)
    if not u:
        flash("Usuário não encontrado.", "error")
        return redirect(url_for("admin.admin_users"))

    txs = (
        BalanceTx.query.filter_by(user_id=u.id)
        .order_by(BalanceTx.created_at.desc())
        .limit(50)
        .all()
    )

    # NOTE: Waba table may be unused now for listing, but keep it for your flow.
    wabas = Waba.query.filter_by(user_id=u.id).order_by(Waba.created_at.desc()).all()

    return render_template(
        "admin_user_detail.html",
        title=f"Admin • {u.username}",
        u=u,
        txs=txs,
        wabas=wabas,
    )

@bp.route("/users/<int:user_id>/balance", methods=["POST"])
@login_required
def admin_adjust_balance(user_id: int):
    u = db.session.get(User, user_id)
    if not u:
        flash("Usuário não encontrado.", "error")
        return redirect(url_for("admin.admin_users"))

    op = (request.form.get("op") or "add").strip()  # add/remove

    try:
        amount_reais = float((request.form.get("amount") or "0").replace(",", "."))
    except Exception:
        flash("Valor inválido.", "error")
        return redirect(url_for("admin.admin_user_detail", user_id=user_id))

    cents = int(round(amount_reais * 100))
    if cents <= 0:
        flash("Informe um valor maior que 0.", "error")
        return redirect(url_for("admin.admin_user_detail", user_id=user_id))

    if op == "remove":
        if u.balance_cents < cents:
            flash("Saldo insuficiente para remover.", "error")
            return redirect(url_for("admin.admin_user_detail", user_id=user_id))
        u.balance_cents -= cents
        tx = BalanceTx(user_id=u.id, amount_cents=-cents, reason="Ajuste Admin: remoção")
    else:
        u.balance_cents += cents
        tx = BalanceTx(user_id=u.id, amount_cents=cents, reason="Ajuste Admin: adição")

    db.session.add(tx)
    db.session.commit()

    flash("Saldo atualizado.", "success")
    return redirect(url_for("admin.admin_user_detail", user_id=user_id))
