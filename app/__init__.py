from flask import Flask, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user, logout_user
from .config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login_get"

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)

    # Blueprints
    from .routes.auth import bp as auth_bp
    from .routes.dashboard import bp as dashboard_bp
    from .routes.wabas import bp as wabas_bp
    from .routes.jobs import bp as jobs_bp
    from .routes.admin import bp as admin_bp
    from .routes.billing import bp as billing_bp

    app.register_blueprint(billing_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(wabas_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(admin_bp)

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

        # Seed admin df/df
        from .models import User
        admin = User.query.filter_by(username="df").first()
        if not admin:
            admin = User(username="df", is_admin=True, is_banned=False, balance_cents=0)
            admin.set_password("df")
            db.session.add(admin)
            db.session.commit()

    return app
