import threading
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Blueprint, request, current_app, jsonify

_SP = ZoneInfo("America/Sao_Paulo")
_recent_payloads: deque = deque(maxlen=100)
_buf_lock = threading.Lock()
_last_send: dict | None = None

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

    # Reply to remoteJid directly for all cases:
    # - @g.us  → group chat (reply visible to the group)
    # - @lid   → Evolution 2.3.x resolves LID to the real phone number
    # - @s.whatsapp.net → plain DM
    reply_to = remote_jid

    message = data.get("message") or {}
    text = (
        message.get("conversation")
        or (message.get("extendedTextMessage") or {}).get("text")
        or ""
    ).strip()

    return reply_to, text


def _handle_info(app, reply_to: str) -> None:
    """Build the /info report and send it back. Runs in a background thread."""
    global _last_send
    with app.app_context():
        from ..services.info_report import build_info_report
        from ..services.evolution import send_text

        try:
            report = build_info_report()
        except Exception as e:
            report = f"❌ Erro ao gerar relatório: {e}"

        ok, err = send_text(reply_to, report)
        _last_send = {
            "at": datetime.now(_SP).isoformat(),
            "reply_to": reply_to,
            "ok": ok,
            "error": err,
            "preview": report[:300],
        }
        if not ok:
            try:
                print(f"[EVOLUTION] send_text failed for {reply_to}: {err}", flush=True)
            except Exception:
                pass


@bp.route("/webhook/evolution", methods=["POST"])
def evolution_webhook():
    """Public endpoint — receives MESSAGES_UPSERT events from Evolution API."""
    payload = request.get_json(silent=True)
    if not payload:
        return "OK", 200

    with _buf_lock:
        _recent_payloads.appendleft({
            "received_at": datetime.now(_SP).isoformat(),
            "event": payload.get("event", ""),
            "payload": payload,
        })

    # Optional shared-secret check
    secret = current_app.config.get("EVOLUTION_WEBHOOK_SECRET", "")
    if secret:
        received = request.headers.get("X-Evolution-Secret", "")
        if received != secret:
            return "Forbidden", 403

    # Evolution sends the event name as "messages.upsert" (lowercase, dotted) in the
    # webhook body, even though it's registered as MESSAGES_UPSERT. Normalize before comparing.
    event = (payload.get("event") or "").upper().replace(".", "_")
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


@bp.route("/webhook/evolution/logs", methods=["GET"])
def evolution_logs():
    """Public endpoint — returns the last 100 Evolution webhook payloads (newest first)."""
    with _buf_lock:
        logs = list(_recent_payloads)
    return jsonify({
        "count": len(logs),
        "maxlen": _recent_payloads.maxlen,
        "last_send": _last_send,
        "logs": logs,
    })
