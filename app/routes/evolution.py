import threading
from flask import Blueprint, request, current_app

bp = Blueprint("evolution", __name__)


def _extract_message(payload: dict) -> tuple[str | None, str | None]:
    """
    Parse a MESSAGES_UPSERT event from Evolution API.
    Returns (reply_to_jid, text) or (None, None) if not applicable.
    reply_to_jid is the full JID to reply to (group JID for groups, number JID for DMs).
    """
    data = payload.get("data") or {}

    # Ignore messages sent by the bot itself
    key = data.get("key") or {}
    if key.get("fromMe"):
        return None, None

    remote_jid: str = key.get("remoteJid") or ""
    if not remote_jid:
        return None, None

    # For groups, reply to the group JID so everyone sees the answer.
    # For DMs, remote_jid is "55...@s.whatsapp.net"; strip the suffix for Evolution sendText.
    if "@g.us" in remote_jid:
        reply_to = remote_jid  # keep full group JID
    else:
        reply_to = remote_jid.split("@")[0]  # just the number digits

    message = data.get("message") or {}
    text = (
        message.get("conversation")
        or (message.get("extendedTextMessage") or {}).get("text")
        or ""
    ).strip()

    return reply_to, text


def _handle_info(app, sender: str) -> None:
    """Build the /info report and send it back. Runs in a background thread."""
    with app.app_context():
        from ..services.info_report import build_info_report
        from ..services.evolution import send_text

        try:
            report = build_info_report()
        except Exception as e:
            report = f"❌ Erro ao gerar relatório: {e}"

        ok, err = send_text(sender, report)
        if not ok:
            try:
                print(f"[EVOLUTION] send_text failed for {sender}: {err}", flush=True)
            except Exception:
                pass


@bp.route("/webhook/evolution", methods=["POST"])
def evolution_webhook():
    """Public endpoint — receives MESSAGES_UPSERT events from Evolution API."""
    payload = request.get_json(silent=True)
    if not payload:
        return "OK", 200

    # Optional shared-secret check
    secret = current_app.config.get("EVOLUTION_WEBHOOK_SECRET", "")
    if secret:
        received = request.headers.get("X-Evolution-Secret", "")
        if received != secret:
            return "Forbidden", 403

    event = payload.get("event") or ""
    if event != "MESSAGES_UPSERT":
        return "OK", 200

    reply_to, text = _extract_message(payload)
    if not reply_to or not text:
        return "OK", 200

    if text.lower() != "/info":
        return "OK", 200

    app = current_app._get_current_object()
    threading.Thread(target=_handle_info, args=(app, reply_to), daemon=True).start()

    return "OK", 200
