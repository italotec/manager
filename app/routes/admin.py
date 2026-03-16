from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
_SP = ZoneInfo("America/Sao_Paulo")
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
import requests as _requests
from .. import db
from ..models import User, BalanceTx, Waba, Proxy
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

    now = datetime.now(_SP)
    start_today = datetime(now.year, now.month, now.day, tzinfo=_SP)
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


# ── Webhook logs ──────────────────────────────────────────────────────────────

@bp.route("/webhook-logs")
@login_required
def webhook_logs():
    from ..models import WebhookLog, AppSetting
    per_page = 50
    page = request.args.get("page", 1, type=int)
    total = WebhookLog.query.count()
    logs = (
        WebhookLog.query
        .order_by(WebhookLog.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    setting = db.session.get(AppSetting, "webhook_logging_enabled")
    enabled = setting is not None and setting.value == "1"
    return render_template(
        "admin_webhook_logs.html",
        title="Admin • Webhook Logs",
        logs=logs,
        enabled=enabled,
        page=page,
        total=total,
        per_page=per_page,
    )


@bp.route("/webhook-toggle", methods=["POST"])
@login_required
def webhook_toggle():
    from ..models import AppSetting
    setting = db.session.get(AppSetting, "webhook_logging_enabled")
    if not setting:
        setting = AppSetting(key="webhook_logging_enabled", value="1")
        db.session.add(setting)
    else:
        setting.value = "1" if setting.value != "1" else "0"
    db.session.commit()
    flash(f"Webhook logging {'ativado' if setting.value == '1' else 'desativado'}.", "success")
    return redirect(url_for("admin.webhook_logs"))


@bp.route("/webhook-logs/clear", methods=["POST"])
@login_required
def webhook_logs_clear():
    from ..models import WebhookLog
    WebhookLog.query.delete()
    db.session.commit()
    flash("Logs limpos.", "success")
    return redirect(url_for("admin.webhook_logs"))


# ── Proxy management ──────────────────────────────────────────────────────────

@bp.route("/proxies")
@login_required
def proxies():
    all_proxies = Proxy.query.order_by(Proxy.created_at.asc()).all()
    return render_template("admin_proxies.html", title="Admin • Proxies", proxies=all_proxies)


@bp.route("/proxies/add", methods=["POST"])
@login_required
def proxy_add():
    proxy_str  = (request.form.get("proxy_str")  or "").strip()
    label      = (request.form.get("label")      or "").strip()
    proxy_type = (request.form.get("proxy_type") or "http").strip()

    if proxy_type not in ("http", "socks5"):
        proxy_type = "http"

    if not proxy_str:
        flash("Informe o proxy.", "error")
        return redirect(url_for("admin.proxies"))

    parts = proxy_str.split(":")
    if len(parts) != 4:
        flash("Formato inválido. Use ip:porta:usuario:senha.", "error")
        return redirect(url_for("admin.proxies"))

    db.session.add(Proxy(proxy_str=proxy_str, proxy_type=proxy_type, label=label))
    db.session.commit()
    flash("Proxy adicionado.", "success")
    return redirect(url_for("admin.proxies"))


@bp.route("/proxies/<int:proxy_id>/delete", methods=["POST"])
@login_required
def proxy_delete(proxy_id: int):
    p = db.session.get(Proxy, proxy_id)
    if p:
        db.session.delete(p)
        db.session.commit()
        flash("Proxy removido.", "success")
    return redirect(url_for("admin.proxies"))


@bp.route("/proxies/<int:proxy_id>/test", methods=["POST"])
@login_required
def proxy_test(proxy_id: int):
    p = db.session.get(Proxy, proxy_id)
    if not p:
        return jsonify({"ok": False, "error": "Proxy não encontrado."}), 404

    try:
        ip, port, user, pwd = p.proxy_str.split(":")
        proxy_url = f"{p.proxy_type}://{user}:{pwd}@{ip}:{port}"
        proxies = {"http": proxy_url, "https": proxy_url}
        r = _requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=10)
        data = r.json()
        return jsonify({"ok": True, "ip": data.get("ip", "?")})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:200]})
