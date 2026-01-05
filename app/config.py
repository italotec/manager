import os

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///app.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # SMS24H
    SMS24H_API_KEY = os.getenv("SMS24H_API_KEY", "0a8b463bee4645a9cfccb45cde49472b")
    SMS24H_BASE_URL = "https://api.sms24h.org/stubs/handler_api"

    # META
    META_API_VERSION = os.getenv("META_API_VERSION", "v18.0")

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

    # Proxies (optional)
    # format: ip:port:user:pass separated by commas
    PROXIES_RAW = [p.strip() for p in os.getenv("PROXIES_RAW", "").split(",") if p.strip()]
