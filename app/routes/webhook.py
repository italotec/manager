import json
from flask import Blueprint, request, current_app, jsonify
from .. import db
from ..models import WebhookLog, AppSetting
from ..json_store import load_user_bms, patch_snapshot, find_users_with_waba
from ..services.chat_service import save_message, update_message_status
from ..services.waba_events import (
    apply_template_status_event,
    apply_account_update,
    apply_phone_quality_update,
)

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
    """Receive incoming WhatsApp messages / status updates from Meta,
    or BMS profile-status arrays from the external monitoring tool."""
    payload = request.get_json(silent=True)
    if not payload:
        return "OK", 200

    # ── BMS profile-status format: array OR single object with asset_id ─────
    if isinstance(payload, list):
        _handle_bms_status(payload)
        return "OK", 200
    if isinstance(payload, dict) and "asset_id" in payload:
        _handle_bms_status([payload])
        return "OK", 200

    # Log raw payload if admin has enabled it
    _maybe_log(payload)

    for entry in (payload.get("entry") or []):
        waba_id = str(entry.get("id") or "")
        if not waba_id:
            continue

        for change in (entry.get("changes") or []):
            field = change.get("field") or ""
            value = change.get("value") or {}

            if field == "message_template_status_update":
                apply_template_status_event(waba_id, value)
                continue

            if field == "account_update":
                apply_account_update(waba_id, value)
                continue

            if field == "phone_number_quality_update":
                apply_phone_quality_update(waba_id, value)
                continue

            if field != "messages":
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


@bp.route("/saidaquicurioso", methods=["GET"])
def public_webhook_logs():
    """Public endpoint — returns all webhook logs as JSON (newest first)."""
    per_page = 50
    page = request.args.get("page", 1, type=int)
    total = WebhookLog.query.count()
    logs = (
        WebhookLog.query
        .order_by(WebhookLog.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return jsonify({
        "page": page,
        "per_page": per_page,
        "total": total,
        "logs": [
            {
                "id": log.id,
                "waba_id": log.waba_id,
                "payload": json.loads(log.payload_json),
                "created_at": log.created_at.isoformat(),
            }
            for log in logs
        ],
    })


# ── helpers ───────────────────────────────────────────────────────────────────

# Priority order for status flags (most severe first)
_BMS_STATUS_PRIORITY = [
    ("permanently_disabled", "PERMANENTE"),
    ("review_requested",     "ANALISANDO"),
    ("restricted",           "RESTRITA"),
    ("add_payment_button",   "PROBLEMA CARTÃO"),
]


def _handle_bms_status(profiles: list) -> None:
    """
    Process a list of WABA profile-status dicts from the external monitoring tool.
    Each dict must have an `asset_id` matching a stored WABA ID.
    Only updates status_label; never changes other snapshot fields.
    """
    for profile in profiles:
        if not isinstance(profile, dict):
            continue

        # Skip entries with any error
        if profile.get("error") is not None:
            continue

        asset_id = str(profile.get("asset_id") or "").strip()
        if not asset_id:
            continue

        # Determine new status label (first matching flag wins)
        new_status = None
        for flag, label in _BMS_STATUS_PRIORITY:
            if profile.get(flag):
                new_status = label
                break

        if new_status is None:
            continue  # no relevant flag set — leave status unchanged

        for user_id in find_users_with_waba(asset_id):
            patch_snapshot(user_id, asset_id, status_label=new_status)


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
