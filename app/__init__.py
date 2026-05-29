from flask import Flask, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user, logout_user
from flask_sock import Sock
from .config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login_get"
sock = Sock()

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)
    sock.init_app(app)

    # Blueprints
    from .routes.auth import bp as auth_bp
    from .routes.dashboard import bp as dashboard_bp
    from .routes.wabas import bp as wabas_bp
    from .routes.jobs import bp as jobs_bp
    from .routes.admin import bp as admin_bp
    from .routes.billing import bp as billing_bp
    from .routes.disparar import bp as disparar_bp
    from .routes.waba_detail import bp as waba_detail_bp
    from .routes.webhook import bp as webhook_bp
    from .routes.listas import bp as listas_bp
    from .routes.api import bp as api_bp
    from .routes.docs import bp as docs_bp
    from .routes.agent_ws import bp as agent_ws_bp, handle_ws
    from .routes.account import bp as account_bp

    app.register_blueprint(billing_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(wabas_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(disparar_bp)
    app.register_blueprint(waba_detail_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(listas_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(docs_bp)
    app.register_blueprint(agent_ws_bp)
    app.register_blueprint(account_bp)

    @sock.route("/agent/ws")
    def agent_ws_route(ws):
        handle_ws(ws)

    # Make balance available to all templates
    @app.context_processor
    def inject_globals():
        bal = 0
        if current_user.is_authenticated:
            bal = getattr(current_user, "balance_cents", 0) or 0
        return {"balance_cents": bal}

    # Block banned users everywhere (force logout)
    @app.before_request
    def block_banned():
        if current_user.is_authenticated and getattr(current_user, "is_banned", False):
            logout_user()
            flash("Sua conta está banida. Fale com o suporte.", "error")
            return redirect(url_for("auth.login_get"))

    with app.app_context():
        from . import models  # noqa
        db.create_all()

        # Enable WAL mode — allows concurrent reads while writing
        db.session.execute(db.text("PRAGMA journal_mode=WAL"))
        db.session.commit()

        # Add new columns to existing DBs (create_all won't add new columns)
        cols = [c["name"] for c in db.inspect(db.engine).get_columns("user")]
        if "api_key" not in cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN api_key VARCHAR(64)"))
            db.session.commit()
        if "agent_token" not in cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN agent_token VARCHAR(64)"))
            db.session.commit()

        # Clean up jobs that were left "running"/"queued" by a previous restart
        from .models import DisparoJob, ListaJob
        stuck = DisparoJob.query.filter(DisparoJob.status.in_(["running", "queued"])).all()
        for j in stuck:
            j.status = "stopped"
            j.last_message = "Interrompido: servidor reiniciou."
        stuck_listas = ListaJob.query.filter(ListaJob.status.in_(["running", "queued"])).all()
        for j in stuck_listas:
            j.status = "stopped"
            j.last_message = "Interrompido: servidor reiniciou."
        if stuck or stuck_listas:
            db.session.commit()

        # Seed admin df/df
        from .models import User
        import secrets as _secrets
        admin = User.query.filter_by(username="df").first()
        if not admin:
            admin = User(username="df", is_admin=True, is_banned=False, balance_cents=0)
            admin.set_password("df")
            db.session.add(admin)
            db.session.commit()

        # Backfill api_key for any user that doesn't have one yet
        users_without_key = User.query.filter(
            (User.api_key == None) | (User.api_key == "")  # noqa: E711
        ).all()
        for u in users_without_key:
            u.api_key = _secrets.token_urlsafe(32)
        if users_without_key:
            db.session.commit()

    return app
