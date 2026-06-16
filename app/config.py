import os
from datetime import timedelta

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
    EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
    EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "")
    EVOLUTION_WEBHOOK_SECRET = os.getenv("EVOLUTION_WEBHOOK_SECRET", "")
    # Comma-separated E.164 numbers allowed to receive /info replies (e.g. "5511999998888")
    INFO_BOT_ALLOWED_NUMBERS = [
        n.strip() for n in os.getenv("INFO_BOT_ALLOWED_NUMBERS", "").split(",") if n.strip()
    ]
