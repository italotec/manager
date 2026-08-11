import os
from datetime import timedelta
from pathlib import Path

# Load a local .env for development. Guarded: python-dotenv is optional, and a
# missing package must never take the app down. override=False so the real
# process environment (systemd EnvironmentFile=/etc/manager.env in production)
# always wins over any .env file.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///app.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_size": 50,
        "max_overflow": 100,
        "pool_timeout": 30,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
        # Wait up to 30s for a SQLite write lock instead of failing instantly with
        # "database is locked" when many disparo jobs commit in parallel.
        "connect_args": {"timeout": 30},
    }

    # Session / login persistence — keeps the user logged in across
    # browser restarts and dev-server reloads (fixes constant logouts).
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SAMESITE = "Lax"
    # Cookies must work over plain http on localhost, so don't force Secure.
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False

    # SMS24H (SMS-Activate protocol)
    SMS24H_API_KEY = os.getenv("SMS24H_API_KEY", "0a8b463bee4645a9cfccb45cde49472b")
    SMS24H_BASE_URL = "https://api.sms24h.org/stubs/handler_api"

    # HeroSMS (SMS-Activate protocol compatible — same actions/responses as SMS24H)
    HERO_SMS_BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
    HERO_SMS_API_KEY  = os.getenv("HERO_SMS_API_KEY", "")
    HERO_SMS_PRICE_USD = os.getenv("HERO_SMS_PRICE_USD", "0.8225")

    # Active provider default; admin can override at runtime via AppSetting "sms_provider"
    SMS_PROVIDER = os.getenv("SMS_PROVIDER", "sms24h")

    # META
    META_API_VERSION = os.getenv("META_API_VERSION", "v18.0")
    META_UPLOAD_API_VERSION = os.getenv("META_UPLOAD_API_VERSION", "v21.0")
    # Message-sending endpoints only (/{phone_number_id}/messages).
    META_SEND_API_VERSION = os.getenv("META_SEND_API_VERSION", "v26.0")
    META_REGISTER_PIN = os.getenv("META_REGISTER_PIN", "123456")
    META_APP_ID = os.getenv("META_APP_ID", "")
    WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "my-webhook-verify-token-change-me")

    # Flow defaults (same as your script)
    SERVICE = "wa"
    COUNTRY = "73"
    OPERATOR = "any"
    CODE_METHOD = "SMS"
    LANGUAGE = "pt"

    MAX_TENTATIVAS_POR_WABA = int(os.getenv("MAX_TENTATIVAS_POR_WABA", "5"))
    TEMPO_MAX_ESPERA_OTP = int(os.getenv("TEMPO_MAX_ESPERA_OTP", "120"))
    OTP_LOCK_HOURS = int(os.getenv("OTP_LOCK_HOURS", "3"))

    # Cost: R$8 per OTP received
    OTP_COST_CENTS = int(os.getenv("OTP_COST_CENTS", "800"))

    # AdsPower local API
    ADSPOWER_BASE = os.getenv("ADSPOWER_BASE", "http://local.adspower.net:50325")

    # Proxies (optional)
    # format: ip:port:user:pass separated by commas
    PROXIES_RAW = [p.strip() for p in os.getenv("PROXIES_RAW", "").split(",") if p.strip()]

    # Evolution API (WhatsApp bot)
    EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "https://evolution.verifywaba.store")
    EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "evolution_api_key_change_me")
    EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "Prosperidade Bot")
    EVOLUTION_WEBHOOK_SECRET = os.getenv("EVOLUTION_WEBHOOK_SECRET", "")
    # Comma-separated E.164 numbers allowed to receive /info replies (e.g. "5511999998888")
    INFO_BOT_ALLOWED_NUMBERS = [
        n.strip() for n in os.getenv("INFO_BOT_ALLOWED_NUMBERS", "").split(",") if n.strip()
    ]
    # How often (seconds) the background refresher re-computes BM metrics and saves to InfoSnapshot
    INFO_REFRESH_INTERVAL_SECONDS = int(os.getenv("INFO_REFRESH_INTERVAL_SECONDS", "300"))

    # Withdrawal (/resumir command) — fixed PIX withdraw target
    WITHDRAW_BANK_ACCOUNT_ID = os.getenv("WITHDRAW_BANK_ACCOUNT_ID", "521d3ec5-804e-4899-8f5a-ee2242a1bd2d")
    WITHDRAW_PASSWORD        = os.getenv("WITHDRAW_PASSWORD", "Milhao@25")

    # Manager Lite replication — Manager is the source of truth for users + WABAs.
    # LITE_SYNC_TOKEN must match Manager Lite's LITE_SYNC_TOKEN.
    LITE_SYNC_ENABLED = os.getenv("LITE_SYNC_ENABLED", "1") == "1"
    LITE_BASE_URL = os.getenv("LITE_BASE_URL", "http://127.0.0.1:5012")
    LITE_SYNC_TOKEN = os.getenv("LITE_SYNC_TOKEN", "85USGKdojLVSVNRCYjJNY3HkKRg9q281hKb6ZU3Rnc")
    # Fallback/default reconciliation interval; overridable at runtime via the
    # AppSetting "lite_sync_interval_seconds" (editable in Admin).
    LITE_SYNC_INTERVAL_SECONDS = int(os.getenv("LITE_SYNC_INTERVAL_SECONDS", "600"))
