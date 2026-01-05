from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from . import db, login_manager

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    # balance in cents (R$)
    balance_cents = db.Column(db.Integer, default=0, nullable=False)

    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_banned = db.Column(db.Boolean, default=False, nullable=False)  # NEW

    wabas = db.relationship("Waba", backref="user", lazy=True, cascade="all, delete-orphan")

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

class Waba(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    name_label = db.Column(db.String(255), nullable=False)

    waba_id = db.Column(db.String(64), nullable=False)
    token = db.Column(db.Text, nullable=False)

    phone_number_id = db.Column(db.String(64), default="", nullable=False)

    pending_phone_number_id = db.Column(db.String(64), default="", nullable=False)
    sms24h_activation_id = db.Column(db.String(64), default="", nullable=False)
    sms24h_full_phone = db.Column(db.String(64), default="", nullable=False)
    otp_received = db.Column(db.Boolean, default=False, nullable=False)
    otp_received_at = db.Column(db.Integer, default=0, nullable=False)  # epoch seconds

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class BalanceTx(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    amount_cents = db.Column(db.Integer, nullable=False)  # negative = debit
    reason = db.Column(db.String(255), nullable=False)

    waba_id = db.Column(db.String(64), default="", nullable=False)
    phone_number_id = db.Column(db.String(64), default="", nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    type = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(32), default="queued", nullable=False)  # queued/running/done/error

    total = db.Column(db.Integer, default=0, nullable=False)
    done = db.Column(db.Integer, default=0, nullable=False)

    current_label = db.Column(db.String(255), default="", nullable=False)
    last_message = db.Column(db.Text, default="", nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
