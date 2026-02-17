import json
from flask import Blueprint, request, current_app
from .. import db
from ..models import WebhookLog, AppSetting
from ..services.chat_service import save_message, update_message_status

bp = Blueprint("webhook", __name__)


@bp.route("/webhook", methods=["GET"])
def verify():
    """Meta webhook verification handshake."""
    mode      = request.args.get("hub.mode", "")
    token     = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    verify_token = current_app.config.get("WEBHOOK_VERIFY_TOKEN", "")
    if mode == "subscribe" and token == verify_token:
        return challenge, 200
    return "Forbidden", 403


@bp.route("/webhook", methods=["POST"])
def receive():
    """Receive incoming WhatsApp messages and status updates from Meta."""
    payload = request.get_json(silent=True)
    if not payload:
        return "OK", 200

    # Log raw payload if admin has enabled it
    _maybe_log(payload)

    for entry in (payload.get("entry") or []):
        waba_id = str(entry.get("id") or "")
        if not waba_id:
            continue

        for change in (entry.get("changes") or []):
            value = change.get("value") or {}
            if change.get("field") != "messages":
                continue

            metadata       = value.get("metadata") or {}
            phone_number_id = metadata.get("phone_number_id", "")

            # Build wa_id → name lookup from contacts array
            contact_map: dict[str, str] = {}
            for c in (value.get("contacts") or []):
                wa_id = c.get("wa_id", "")
                name  = (c.get("profile") or {}).get("name", "")
                if wa_id:
                    contact_map[wa_id] = name

            # ── Incoming messages (customer → us) ────────────────────────
            for msg in (value.get("messages") or []):
                from_wa   = msg.get("from", "")
                wamid     = msg.get("id", "")
                msg_type  = msg.get("type", "text")
                cname     = contact_map.get(from_wa, "")

                body      = ""
                media_url = ""

                if msg_type == "text":
                    body = (msg.get("text") or {}).get("body", "")
                elif msg_type == "image":
                    img       = msg.get("image") or {}
                    body      = img.get("caption", "")
                    media_url = img.get("id", "")   # media ID (not a public URL)
                elif msg_type == "button":
                    body = (msg.get("button") or {}).get("text", "")
                elif msg_type == "interactive":
                    inter = msg.get("interactive") or {}
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    body  = reply.get("title", f"[{msg_type}]")
                else:
                    body = f"[{msg_type}]"

                save_message(
                    waba_id=waba_id,
                    phone_number_id=phone_number_id,
                    contact_wa_id=from_wa,
                    contact_name=cname,
                    direction="in",
                    msg_type=msg_type,
                    body=body,
                    media_url=media_url,
                    wamid=wamid,
                    status="received",
                )

            # ── Status updates for our outgoing messages ─────────────────
            for status_obj in (value.get("statuses") or []):
                wamid     = status_obj.get("id", "")
                status_v  = status_obj.get("status", "")
                if wamid and status_v in ("sent", "delivered", "read"):
                    update_message_status(wamid, status_v)

    return "OK", 200


# ── helpers ───────────────────────────────────────────────────────────────────

def _maybe_log(payload: dict):
    """Save raw webhook payload if logging is toggled on by admin."""
    try:
        setting = db.session.get(AppSetting, "webhook_logging_enabled")
        if not setting or setting.value != "1":
            return

        waba_id = ""
        for entry in (payload.get("entry") or []):
            waba_id = str(entry.get("id") or "")
            break

        log = WebhookLog(
            waba_id=waba_id,
            payload_json=json.dumps(payload, ensure_ascii=False)[:50_000],
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        db.session.rollback()
