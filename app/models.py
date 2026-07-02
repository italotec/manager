import secrets
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
    can_virtual_phone = db.Column(db.Boolean, default=False, nullable=False)

    api_key = db.Column(db.String(64), unique=True, nullable=True, index=True)
    agent_token = db.Column(db.String(64), unique=True, nullable=True, index=True)
    test_phone = db.Column(db.String(32), nullable=True)
    prosperidade_api_key = db.Column(db.String(255), nullable=True)

    wabas = db.relationship("Waba", backref="user", lazy=True, cascade="all, delete-orphan")

    def generate_api_key(self):
        self.api_key = secrets.token_urlsafe(32)

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)

    @property
    def virtual_phone_allowed(self) -> bool:
        return bool(self.is_admin or self.can_virtual_phone)

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

    waba_id = db.Column(db.String(64), default="", nullable=False)
    skip_log = db.Column(db.Boolean, default=False, nullable=False)

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
    timestamp       = db.Column(db.DateTime,    default=_now_sp,   nullable=False, index=True)
    __table_args__  = (db.Index("ix_chat_conv", "waba_id", "phone_number_id", "contact_wa_id"),)


class WebhookLog(db.Model):
    id           = db.Column(db.Integer,    primary_key=True)
    waba_id      = db.Column(db.String(64), default="", nullable=False, index=True)
    payload_json = db.Column(db.Text,       nullable=False)
    created_at   = db.Column(db.DateTime,   default=_now_sp, nullable=False, index=True)


class ListaWebhook(db.Model):
    """Status webhooks do número de validação de listas, indexados por wamid.
    Isolado do WebhookLog geral para não sofrer flood/poda dos demais webhooks."""
    id          = db.Column(db.Integer,     primary_key=True)
    wamid       = db.Column(db.String(128), nullable=False, index=True)
    status_json = db.Column(db.Text,        nullable=False)
    created_at  = db.Column(db.DateTime,    default=_now_sp, nullable=False, index=True)


class LoginLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    ip_address = db.Column(db.String(64), default="", nullable=False)
    user_agent = db.Column(db.String(512), default="", nullable=False)
    created_at = db.Column(db.DateTime, default=_now_sp, nullable=False)


class InfoSnapshot(db.Model):
    """Cached BM metrics for the /info bot. One row per change event."""
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    day             = db.Column(db.String(10), nullable=False, index=True)  # "YYYY-MM-DD" SP tz
    bms_disparadas  = db.Column(db.Integer, default=0, nullable=False)
    total_sent      = db.Column(db.Integer, default=0, nullable=False)
    total_delivered = db.Column(db.Integer, default=0, nullable=False)
    created_at      = db.Column(db.DateTime, default=_now_sp, nullable=False)


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


class TemplateModel(db.Model):
    """Reusable WhatsApp message-template definition (model library)."""
    id           = db.Column(db.Integer,     primary_key=True)
    user_id      = db.Column(db.Integer,     db.ForeignKey("user.id"), nullable=False, index=True)
    name         = db.Column(db.String(128), nullable=False)   # base name, e.g. "template"
    category     = db.Column(db.String(32),  nullable=False, default="UTILITY")
    language     = db.Column(db.String(16),  nullable=False, default="pt_BR")
    payload_json = db.Column(db.Text,        nullable=False, default="{}")
    created_at   = db.Column(db.DateTime,    default=_now_sp, nullable=False)

    def to_dict(self):
        return {
            "id":         self.id,
            "name":       self.name,
            "category":   self.category,
            "language":   self.language,
            "created_at": self.created_at.strftime("%d/%m/%Y") if self.created_at else "",
        }


class Card(db.Model):
    """Credit/debit card stored per user for bulk WABA billing attachment."""
    import json as _json

    id            = db.Column(db.Integer,     primary_key=True)
    user_id       = db.Column(db.Integer,     db.ForeignKey("user.id"), nullable=False, index=True)
    number        = db.Column(db.String(20),  nullable=False)   # full PAN, plaintext
    exp_month     = db.Column(db.String(2),   nullable=False)   # "6"
    exp_year      = db.Column(db.String(4),   nullable=False)   # "2032"
    csc           = db.Column(db.String(4),   nullable=False)
    holder_name   = db.Column(db.String(128), nullable=False, default="")
    brand         = db.Column(db.String(16),  nullable=False, default="unknown")
    bin           = db.Column(db.String(8),   nullable=False, default="")
    last4         = db.Column(db.String(4),   nullable=False, default="")
    # JSON list of waba_id strings (distinct WABAs this card has been attached to)
    used_waba_ids = db.Column(db.Text,        nullable=False, default="[]")
    status        = db.Column(db.String(16),  nullable=False, default="active")  # active|overused|invalid
    last_error    = db.Column(db.Text,        nullable=False, default="")
    created_at    = db.Column(db.DateTime,    default=_now_sp, nullable=False)

    @property
    def usage_count(self):
        import json
        try:
            return len(json.loads(self.used_waba_ids or "[]"))
        except Exception:
            return 0

    @property
    def remaining(self):
        return max(0, 5 - self.usage_count)

    @property
    def is_available(self):
        return self.status == "active" and self.remaining > 0

    def mark_used(self, waba_id: str):
        import json
        try:
            ids = json.loads(self.used_waba_ids or "[]")
        except Exception:
            ids = []
        if waba_id not in ids:
            ids.append(waba_id)
        self.used_waba_ids = json.dumps(ids)

    def to_dict(self):
        return {
            "id": self.id,
            "brand": self.brand,
            "last4": self.last4,
            "bin": self.bin,
            "exp_month": self.exp_month,
            "exp_year": self.exp_year,
            "holder_name": self.holder_name,
            "usage_count": self.usage_count,
            "remaining": self.remaining,
            "status": self.status,
            "last_error": self.last_error,
            "created_at": self.created_at.strftime("%d/%m/%Y") if self.created_at else "",
        }


class PhotoModel(db.Model):
    """Saved profile picture — reusable across WABAs."""
    __tablename__ = "photo_model"
    id         = db.Column(db.Integer,     primary_key=True)
    user_id    = db.Column(db.Integer,     db.ForeignKey("user.id"), nullable=False, index=True)
    name       = db.Column(db.String(128), nullable=False)
    filename   = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime,    default=_now_sp, nullable=False)

    def to_dict(self):
        return {
            "id":         self.id,
            "name":       self.name,
            "url":        f"/photos/{self.id}/file",
            "created_at": self.created_at.strftime("%d/%m/%Y %H:%M") if self.created_at else "",
        }
