import json
import queue
import threading
import time
from flask import Blueprint, request, current_app, jsonify
from .. import db
from ..models import WebhookLog, ListaWebhook, AppSetting
from ..json_store import load_user_bms, patch_snapshot, find_users_with_waba
from ..services.chat_service import save_message, update_message_status
from ..services.health_test_service import mark_health_test
from ..services.waba_events import (
    apply_template_status_event,
    apply_account_update,
    apply_phone_quality_update,
    apply_message_status_event,
)

bp = Blueprint("webhook", __name__)

# ── listas phone-number-id cache ─────────────────────────────────────────────
_listas_pnid_cache: str | None = None
_listas_pnid_fetched_at: float = 0.0
_LISTAS_PNID_TTL = 60  # seconds


def _get_listas_pnid() -> str:
    global _listas_pnid_cache, _listas_pnid_fetched_at
    now = time.monotonic()
    if now - _listas_pnid_fetched_at < _LISTAS_PNID_TTL and _listas_pnid_cache is not None:
        return _listas_pnid_cache
    try:
        row = db.session.get(AppSetting, "listas_phone_number_id")
        _listas_pnid_cache = (row.value if row else "") or ""
    except Exception:
        _listas_pnid_cache = ""
    _listas_pnid_fetched_at = now
    return _listas_pnid_cache


def _is_listas_payload(payload: dict, pnid: str) -> bool:
    """True if any change in the payload has field==messages from the listas phone number."""
    for entry in (payload.get("entry") or []):
        for change in (entry.get("changes") or []):
            if change.get("field") != "messages":
                continue
            if (change.get("value") or {}).get("metadata", {}).get("phone_number_id") == pnid:
                return True
    return False


_listas_insert_counter = 0


def _store_listas_statuses(payload: dict) -> None:
    """Insert one ListaWebhook row per status object from a listas validation payload."""
    global _listas_insert_counter
    try:
        for entry in (payload.get("entry") or []):
            for change in (entry.get("changes") or []):
                if change.get("field") != "messages":
                    continue
                for status in (change.get("value") or {}).get("statuses") or []:
                    wamid = status.get("id")
                    if not wamid:
                        continue
                    db.session.add(ListaWebhook(
                        wamid=wamid,
                        status_json=json.dumps(status, ensure_ascii=False),
                    ))
                    _listas_insert_counter += 1

        db.session.flush()

        # Prune rows older than 24 h every 500 inserts
        if _listas_insert_counter % 500 == 0:
            from datetime import datetime, timedelta
            cutoff = datetime.utcnow() - timedelta(hours=24)
            db.session.query(ListaWebhook).filter(
                ListaWebhook.created_at < cutoff
            ).delete(synchronize_session=False)

        db.session.commit()
    except Exception:
        db.session.rollback()

# ── Async webhook processing ────────────────────────────────────────────────────
# Meta floods this endpoint (~16 req/s of message/status events). Processing each
# request inline did several DB writes against the (single-writer) SQLite DB before
# returning 200, so under load request threads + pool connections piled up and
# starved everything else (including add-phone job threads). We now ACK 200 instantly
# and process payloads on a small, fixed pool of background workers — decoupling
# Meta's arrival rate from our DB write throughput and keeping request threads short.

_WORKER_COUNT = 2
_QUEUE_MAXSIZE = 20000
_work_queue: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAXSIZE)
_workers_started = False
_workers_lock = threading.Lock()
_dropped = 0


def _worker_loop(app):
    while True:
        payload = _work_queue.get()
        try:
            with app.app_context():
                process_webhook_payload(payload)
        except Exception as e:
            try:
                print(f"[WEBHOOK] worker error: {type(e).__name__}: {e}", flush=True)
            except Exception:
                pass
        finally:
            _work_queue.task_done()


def _ensure_workers(app):
    global _workers_started
    if _workers_started:
        return
    with _workers_lock:
        if _workers_started:
            return
        for _ in range(_WORKER_COUNT):
            threading.Thread(
                target=_worker_loop, args=(app,), daemon=True
            ).start()
        _workers_started = True


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
    """ACK Meta instantly; hand the payload to a background worker for DB work."""
    global _dropped
    payload = request.get_json(silent=True)
    if not payload:
        return "OK", 200

    _ensure_workers(current_app._get_current_object())
    try:
        _work_queue.put_nowait(payload)
    except queue.Full:
        _dropped += 1
        try:
            print(f"[WEBHOOK] queue full — dropped (total={_dropped})", flush=True)
        except Exception:
            pass
    return "OK", 200


def process_webhook_payload(payload):
    """Process a single webhook payload. Runs inside an app context on a worker thread.

    Receives incoming WhatsApp messages / status updates from Meta, or BMS
    profile-status arrays from the external monitoring tool.
    """
    # ── BMS profile-status format: array OR single object with asset_id ─────
    if isinstance(payload, list):
        _handle_bms_status(payload)
        return
    if isinstance(payload, dict) and "asset_id" in payload:
        _handle_bms_status([payload])
        return

    # ── Listas validation statuses — dedicated store, bypasses general log ────
    pnid = _get_listas_pnid()
    if pnid and _is_listas_payload(payload, pnid):
        _store_listas_statuses(payload)
        return

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
                if wamid and status_v in ("sent", "delivered") and waba_id:
                    try:
                        mark_health_test(waba_id, wamid)
                    except Exception:
                        pass
                if waba_id:
                    try:
                        apply_message_status_event(waba_id, status_obj)
                    except Exception:
                        pass


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


_log_counter = 0


def _maybe_log(payload: dict):
    """Save raw webhook payload — one row per WABA entry, always on."""
    global _log_counter
    try:
        payload_str = json.dumps(payload, ensure_ascii=False)[:50_000]

        for entry in (payload.get("entry") or []):
            waba_id = str(entry.get("id") or "").strip()
            if not waba_id:
                continue
            db.session.add(WebhookLog(waba_id=waba_id, payload_json=payload_str))

        db.session.flush()

        # Prune rows older than 48 h every 2 000 calls (~every 2 min at 16/s).
        # A single range-DELETE on the indexed created_at column is O(log n) and
        # replaces the old per-WABA NOT-IN subquery that did full-table scans.
        _log_counter += 1
        if _log_counter % 2000 == 0:
            from datetime import datetime, timedelta
            cutoff = datetime.utcnow() - timedelta(hours=48)
            db.session.query(WebhookLog).filter(
                WebhookLog.created_at < cutoff
            ).delete(synchronize_session=False)

        db.session.commit()
    except Exception:
        db.session.rollback()
