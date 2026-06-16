import requests
from urllib.parse import quote
from flask import current_app

_TIMEOUT = 30


def send_text(number: str, text: str) -> tuple[bool, str | None]:
    """Send a plain-text WhatsApp message via Evolution API."""
    base = current_app.config.get("EVOLUTION_API_URL", "").rstrip("/")
    instance = current_app.config.get("EVOLUTION_INSTANCE", "")
    api_key = current_app.config.get("EVOLUTION_API_KEY", "")

    if not base or not instance or not api_key:
        return False, "Evolution API não configurada (verifique EVOLUTION_API_URL, EVOLUTION_INSTANCE, EVOLUTION_API_KEY)"

    try:
        resp = requests.post(
            f"{base}/message/sendText/{quote(instance, safe='')}",
            json={"number": number, "text": text},
            headers={"apikey": api_key, "Content-Type": "application/json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return True, None
    except requests.RequestException as e:
        return False, f"Evolution send error: {e}"
