from datetime import datetime
from zoneinfo import ZoneInfo
from flask_login import UserMixin

_SP = ZoneInfo("America/Sao_Paulo")


def _now_sp():
    return datetime.now(_SP)
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

    created_at = db.Column(db.DateTime, default=_now_sp, nullable=False)

class BalanceTx(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    amount_cents = db.Column(db.Integer, nullable=False)  # negative = debit
    reason = db.Column(db.String(255), nullable=False)

    waba_id = db.Column(db.String(64), default="", nullable=False)
    phone_number_id = db.Column(db.String(64), default="", nullable=False)

    created_at = db.Column(db.DateTime, default=_now_sp, nullable=False)

class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    type = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(32), default="queued", nullable=False)  # queued/running/done/error

    total = db.Column(db.Integer, default=0, nullable=False)
    done = db.Column(db.Integer, default=0, nullable=False)

    current_label = db.Column(db.String(255), default="", nullable=False)
    last_message = db.Column(db.Text, default="", nullable=False)

    created_at = db.Column(db.DateTime, default=_now_sp, nullable=False)


class DisparoJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    status = db.Column(db.String(32), default="queued", nullable=False)  # queued/running/done/error/stopped

    total = db.Column(db.Integer, default=0, nullable=False)
    sent = db.Column(db.Integer, default=0, nullable=False)
    failed = db.Column(db.Integer, default=0, nullable=False)
    skipped = db.Column(db.Integer, default=0, nullable=False)

    last_message = db.Column(db.Text, default="", nullable=False)
    stop_requested = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=_now_sp, nullable=False)


class ListaJob(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status        = db.Column(db.String(32), default="queued", nullable=False)   # queued/running/done/error/stopped
    mode          = db.Column(db.String(16), default="dedup_validate", nullable=False)  # dedup_only / dedup_validate
    original_file = db.Column(db.String(255), default="", nullable=False)
    phone_column  = db.Column(db.String(128), default="", nullable=False)
    total         = db.Column(db.Integer, default=0, nullable=False)
    has_whatsapp  = db.Column(db.Integer, default=0, nullable=False)
    no_whatsapp   = db.Column(db.Integer, default=0, nullable=False)
    errors        = db.Column(db.Integer, default=0, nullable=False)
    max_workers   = db.Column(db.Integer, default=30, nullable=False)
    last_message  = db.Column(db.Text, default="", nullable=False)
    stop_requested = db.Column(db.Boolean, default=False, nullable=False)
    created_at    = db.Column(db.DateTime, default=_now_sp, nullable=False)


class ChatMessage(db.Model):
    id              = db.Column(db.Integer,     primary_key=True)
    waba_id         = db.Column(db.String(64),  nullable=False, index=True)
    phone_number_id = db.Column(db.String(64),  nullable=False, index=True)
    contact_wa_id   = db.Column(db.String(32),  nullable=False, index=True)
    contact_name    = db.Column(db.String(255), default="", nullable=False)
    direction       = db.Column(db.String(4),   nullable=False)            # "in" or "out"
    msg_type        = db.Column(db.String(32),  default="text", nullable=False)
    body            = db.Column(db.Text,        default="", nullable=False)
    media_url       = db.Column(db.Text,        default="", nullable=False)
    wamid           = db.Column(db.String(128), default="", nullable=False, index=True)
    status          = db.Column(db.String(16),  default="sent", nullable=False)
    timestamp       = db.Column(db.DateTime,    default=_now_sp,   nullable=False)
    __table_args__  = (db.Index("ix_chat_conv", "waba_id", "phone_number_id", "contact_wa_id"),)


class WebhookLog(db.Model):
    id           = db.Column(db.Integer,    primary_key=True)
    waba_id      = db.Column(db.String(64), default="", nullable=False)
    payload_json = db.Column(db.Text,       nullable=False)
    created_at   = db.Column(db.DateTime,   default=_now_sp, nullable=False)


class AppSetting(db.Model):
    """Generic key-value store for persistent admin settings."""
    key   = db.Column(db.String(64),  primary_key=True)
    value = db.Column(db.String(255), default="", nullable=False)


class Proxy(db.Model):
    """HTTP proxy entries managed through the admin panel."""
    id         = db.Column(db.Integer,     primary_key=True)
    proxy_str  = db.Column(db.String(255), nullable=False)              # ip:port:user:pass
    proxy_type = db.Column(db.String(16),  default="http", nullable=False)  # http / socks5
    label      = db.Column(db.String(128), default="", nullable=False)
    created_at = db.Column(db.DateTime,    default=_now_sp, nullable=False)
