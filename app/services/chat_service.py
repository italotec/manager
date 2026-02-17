"""
chat_service.py — sending, storing and querying chat messages.
"""
import requests
from datetime import datetime
from sqlalchemy import func, desc

from .. import db
from ..models import ChatMessage, User
from ..json_store import load_user_bms

_API_VERSION = "v23.0"


# ── WABA lookup (for webhook processing, no session available) ─────────────────

def find_waba_owner(waba_id: str):
    """
    Scan all users' bms.json to find who owns this waba_id.
    Returns (user_id, token) or (None, None).
    """
    users = User.query.all()
    for user in users:
        bms = load_user_bms(user.id)
        entry = bms.get(str(waba_id))
        if entry and isinstance(entry, dict):
            return user.id, entry.get("token", "")
    return None, None


# ── Sending ───────────────────────────────────────────────────────────────────

def send_text_message(token: str, phone_number_id: str, to_wa_id: str, body: str):
    """Send a plain text message. Returns (success, wamid_or_error_str)."""
    url = f"https://graph.facebook.com/{_API_VERSION}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "text",
        "text": {"body": body},
    }
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if r.status_code == 200:
            j = r.json()
            wamid = ((j.get("messages") or [{}])[0]).get("id", "")
            return True, wamid
        return False, (r.text or "")[:500]
    except Exception as e:
        return False, str(e)[:500]


def send_image_message(token: str, phone_number_id: str, to_wa_id: str,
                       image_url: str, caption: str = ""):
    """Send an image by public URL. Returns (success, wamid_or_error_str)."""
    url = f"https://graph.facebook.com/{_API_VERSION}/{phone_number_id}/messages"
    image_obj: dict = {"link": image_url}
    if caption:
        image_obj["caption"] = caption
    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "image",
        "image": image_obj,
    }
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if r.status_code == 200:
            j = r.json()
            wamid = ((j.get("messages") or [{}])[0]).get("id", "")
            return True, wamid
        return False, (r.text or "")[:500]
    except Exception as e:
        return False, str(e)[:500]


# ── Persistence ───────────────────────────────────────────────────────────────

def save_message(waba_id: str, phone_number_id: str, contact_wa_id: str,
                 contact_name: str, direction: str, msg_type: str,
                 body: str, media_url: str = "", wamid: str = "",
                 status: str = "sent"):
    """Write a ChatMessage row and commit. Returns the saved ChatMessage."""
    msg = ChatMessage(
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        contact_wa_id=contact_wa_id,
        contact_name=contact_name,
        direction=direction,
        msg_type=msg_type,
        body=body,
        media_url=media_url,
        wamid=wamid,
        status=status,
        timestamp=datetime.utcnow(),
    )
    db.session.add(msg)
    db.session.commit()
    return msg


def update_message_status(wamid: str, new_status: str):
    """Advance a sent message's delivery status. Never goes backwards."""
    if not wamid:
        return
    _order = {"sent": 0, "delivered": 1, "read": 2}
    msg = ChatMessage.query.filter_by(wamid=wamid).first()
    if msg and _order.get(new_status, -1) > _order.get(msg.status, -1):
        msg.status = new_status
        db.session.commit()

    # Also update contact_name if we received a newer name via a status webhook
    # (status webhooks don't carry name, so nothing to update here)


# ── Queries ───────────────────────────────────────────────────────────────────

def get_conversations(waba_id: str, phone_number_id: str) -> list:
    """
    Return a list of contacts sorted by most-recent message first.
    Each entry: {contact_wa_id, contact_name, last_body, last_timestamp,
                 direction, msg_type}
    """
    # Latest message id per contact
    subq = (
        db.session.query(
            ChatMessage.contact_wa_id,
            func.max(ChatMessage.id).label("max_id"),
        )
        .filter_by(waba_id=waba_id, phone_number_id=phone_number_id)
        .group_by(ChatMessage.contact_wa_id)
        .subquery()
    )

    rows = (
        db.session.query(ChatMessage)
        .join(subq, ChatMessage.id == subq.c.max_id)
        .order_by(desc(ChatMessage.timestamp))
        .all()
    )

    return [
        {
            "contact_wa_id":   m.contact_wa_id,
            "contact_name":    m.contact_name or m.contact_wa_id,
            "last_body":       (m.body or "")[:80],
            "last_timestamp":  m.timestamp.isoformat() + "Z",
            "direction":       m.direction,
            "msg_type":        m.msg_type,
        }
        for m in rows
    ]


def get_message_history(waba_id: str, phone_number_id: str, contact_wa_id: str,
                        limit: int = 100, before_id: int = None) -> list:
    """
    Return message history for a conversation, oldest-first.
    Supports cursor-based pagination via before_id.
    """
    q = ChatMessage.query.filter_by(
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        contact_wa_id=contact_wa_id,
    )
    if before_id:
        q = q.filter(ChatMessage.id < before_id)

    messages = q.order_by(ChatMessage.timestamp.desc()).limit(limit).all()
    messages.reverse()   # chronological order for display

    return [
        {
            "id":           m.id,
            "direction":    m.direction,
            "msg_type":     m.msg_type,
            "body":         m.body,
            "media_url":    m.media_url,
            "status":       m.status,
            "timestamp":    m.timestamp.isoformat() + "Z",
            "contact_name": m.contact_name,
        }
        for m in messages
    ]
