"""
Handlers for Meta WABA webhook fields that update the stored snapshot:
  - message_template_status_update  → template_status_map + template_counts
  - account_update                  → phone_numbers list + status_label (+ status_detail)
  - phone_number_quality_update     → messaging_limit_tier
"""
import re
from ..json_store import find_users_with_waba, patch_snapshot
from .meta import templates_status_summary


def _digits(s: str) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _format_phone(phone: str) -> str:
    """Format raw webhook digits like Meta's display_phone_number.

    Brazil (CC 55): "5574923842261" → "+55 74 92384-2261"
                    "551633334444"  → "+55 16 3333-4444"   (landline, 8-digit subscriber)
    Anything else falls back to "+<digits>".
    """
    d = _digits(phone)
    if not d:
        return phone
    if d.startswith("55") and len(d) in (12, 13):
        cc, ddd, sub = d[:2], d[2:4], d[4:]
        return f"+{cc} {ddd} {sub[:-4]}-{sub[-4:]}"
    return f"+{d}"


# ── template status ───────────────────────────────────────────────────────────

_DELETED_EVENTS = {"DELETED", "PENDING_DELETION"}


def apply_template_status_event(waba_id: str, value: dict) -> None:
    """
    value fields (from message_template_status_update):
      message_template_id, message_template_name, message_template_language,
      message_template_category, event (APPROVED|REJECTED|PENDING|PAUSED|DISABLED|
      PENDING_DELETION|DELETED|FLAGGED|APPEAL_REQUESTED|IN_APPEAL|REINSTATED),
      reason (nullable string)
    """
    tpl_id   = str(value.get("message_template_id") or "").strip()
    tpl_name = value.get("message_template_name") or ""
    lang     = value.get("message_template_language") or ""
    category = value.get("message_template_category") or ""
    event    = (value.get("event") or "").upper()

    if not tpl_id or not event:
        return

    for user_id in find_users_with_waba(waba_id):
        from ..json_store import load_user_bms, save_user_bms
        data = load_user_bms(user_id)
        key  = str(waba_id).strip()
        if key not in data or not isinstance(data.get(key), dict):
            continue

        entry = data[key]
        snap  = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}

        tpl_map: dict = snap.get("template_status_map") or {}

        if event in _DELETED_EVENTS:
            tpl_map.pop(tpl_id, None)
        else:
            tpl_map[tpl_id] = {
                "id":       tpl_id,
                "name":     tpl_name,
                "language": lang,
                "category": category,
                "status":   event,
            }

        counts = templates_status_summary(list(tpl_map.values()))
        snap["template_status_map"] = tpl_map
        snap["template_counts"]     = counts
        entry["snapshot"] = snap
        data[key] = entry
        save_user_bms(user_id, data)


# ── account update → phone_numbers + status_label ────────────────────────────

def _apply_phone_membership(waba_id: str, phone: str, added: bool) -> None:
    """Append or remove a phone entry in snapshot.phone_numbers without a Meta API call."""
    target = _digits(phone)
    if not target:
        return

    for user_id in find_users_with_waba(waba_id):
        from ..json_store import load_user_bms, save_user_bms
        data = load_user_bms(user_id)
        key = str(waba_id).strip()
        if key not in data or not isinstance(data.get(key), dict):
            continue

        entry = data[key]
        snap = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}
        phones = list(snap.get("phone_numbers") or [])

        if added:
            already = any(_digits(p.get("display_phone_number")) == target for p in phones)
            if not already:
                # Pending (yellow): the number was added to the WABA but is not yet
                # registered/connected. It turns green only after a successful register.
                phones.append({
                    "id": "",
                    "display_phone_number": _format_phone(phone),
                    "verified_name": "",
                    "quality_rating": "",
                    "status": "PENDING",
                })
            else:
                continue
        else:
            new_phones = [p for p in phones if _digits(p.get("display_phone_number")) != target]
            if len(new_phones) == len(phones):
                continue
            phones = new_phones

        snap["phone_numbers"] = phones
        entry["snapshot"] = snap
        data[key] = entry
        save_user_bms(user_id, data)


def mark_phone_connected(user_id: int, waba_id: str, phone_id: str = "", phone: str = "") -> None:
    """Flip a phone to CONNECTED (green) in snapshot.phone_numbers after a successful register.

    Matches an existing entry by phone_id or by digit-normalized number; inserts a new
    connected entry if none matches (e.g. the PHONE_NUMBER_ADDED webhook hasn't arrived yet).
    """
    from ..json_store import load_user_bms, save_user_bms
    pid = str(phone_id or "").strip()
    target = _digits(phone)
    if not pid and not target:
        return

    data = load_user_bms(user_id)
    key = str(waba_id).strip()
    if key not in data or not isinstance(data.get(key), dict):
        return

    entry = data[key]
    snap = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}
    phones = list(snap.get("phone_numbers") or [])

    matched = None
    for p in phones:
        if pid and str(p.get("id") or "") == pid:
            matched = p
            break
        if target and _digits(p.get("display_phone_number")) == target:
            matched = p
            break

    if matched is not None:
        matched["status"] = "CONNECTED"
        if pid and not matched.get("id"):
            matched["id"] = pid
    else:
        phones.append({
            "id": pid,
            "display_phone_number": _format_phone(phone) if target else "",
            "verified_name": "",
            "quality_rating": "",
            "status": "CONNECTED",
        })

    snap["phone_numbers"] = phones
    entry["snapshot"] = snap
    data[key] = entry
    save_user_bms(user_id, data)


# Labels that this webhook is allowed to flip back to OK
_WEBHOOK_BAD_LABELS = {"PERMANENTE", "RESTRITA", "ANALISANDO"}

# ── RETENÇÃO: payment-restriction failed sends ────────────────────────────────

_RETENCAO_LABEL        = "RETENÇÃO"
_RETENCAO_OVERWRITABLE = {"", "OK", "PROBLEMA CARTÃO"}
# Error code 131042 ("Business eligibility payment issue") is shared by several
# distinct problems (payment restricted, currency not configured, …) — so we must
# match the human-readable details text, not just the code.
_PAYMENT_RESTRICTED_CODE   = 131042
_PAYMENT_RESTRICTED_PHRASE = "payment has been restricted"


def _is_payment_restricted(error: dict) -> bool:
    if not isinstance(error, dict) or error.get("code") != _PAYMENT_RESTRICTED_CODE:
        return False
    details = ((error.get("error_data") or {}).get("details") or "").lower()
    return _PAYMENT_RESTRICTED_PHRASE in details


def _set_retencao(waba_id: str) -> None:
    for user_id in find_users_with_waba(waba_id):
        from ..json_store import load_user_bms, save_user_bms
        data = load_user_bms(user_id)
        key  = str(waba_id).strip()
        if key not in data or not isinstance(data.get(key), dict):
            continue
        entry = data[key]
        snap  = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}
        current = snap.get("status_label", "")
        if current == _RETENCAO_LABEL or current not in _RETENCAO_OVERWRITABLE:
            continue
        snap["retencao_prev_label"] = current
        snap["status_label"]        = _RETENCAO_LABEL
        entry["snapshot"] = snap
        data[key] = entry
        save_user_bms(user_id, data)


def _clear_retencao(waba_id: str) -> None:
    for user_id in find_users_with_waba(waba_id):
        from ..json_store import load_user_bms, save_user_bms
        data = load_user_bms(user_id)
        key  = str(waba_id).strip()
        if key not in data or not isinstance(data.get(key), dict):
            continue
        entry = data[key]
        snap  = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}
        if snap.get("status_label") != _RETENCAO_LABEL:
            continue
        prev = snap.pop("retencao_prev_label", "") or "OK"
        snap["status_label"] = prev
        entry["snapshot"] = snap
        data[key] = entry
        save_user_bms(user_id, data)


def apply_message_status_event(waba_id: str, status_obj: dict) -> None:
    """Set or clear RETENÇÃO based on an outgoing-message status callback."""
    status_v = (status_obj.get("status") or "").lower()
    if status_v == "failed":
        errors = status_obj.get("errors") or []
        if any(_is_payment_restricted(e) for e in errors):
            _set_retencao(waba_id)
    elif status_v in ("sent", "delivered", "read"):
        _clear_retencao(waba_id)


def apply_account_update(waba_id: str, value: dict) -> None:
    """
    value fields:
      phone_number, event (DISABLED_UPDATE|ACCOUNT_RESTRICTION|ACCOUNT_VIOLATION|
      ACCOUNT_DELETED|VERIFIED_ACCOUNT|…),
      ban_info.waba_ban_state (DISABLE|SCHEDULE_FOR_DISABLE|REINSTATE),
      restriction_info[].restriction_type,
      violation_info.violation_type
    """
    event    = (value.get("event") or "").upper()

    if event in ("PHONE_NUMBER_ADDED", "PHONE_NUMBER_REMOVED", "PHONE_NUMBER_DELETED"):
        phone = (value.get("phone_number") or "").strip()
        if phone:
            _apply_phone_membership(waba_id, phone, added=(event == "PHONE_NUMBER_ADDED"))
        return

    ban_info = value.get("ban_info") or {}
    ban_state = (ban_info.get("waba_ban_state") or "").upper()

    new_label  = None
    new_detail = None

    if event == "DISABLED_UPDATE":
        if ban_state in ("DISABLE", "SCHEDULE_FOR_DISABLE"):
            new_label  = "PERMANENTE"
            new_detail = ban_state
        elif ban_state == "REINSTATE":
            new_label = "OK"

    elif event == "ACCOUNT_DELETED":
        new_label = "PERMANENTE"

    elif event == "ACCOUNT_RESTRICTION":
        new_label = "RESTRITA"
        restrictions = value.get("restriction_info") or []
        if isinstance(restrictions, list) and restrictions:
            types = [r.get("restriction_type") for r in restrictions if r.get("restriction_type")]
            new_detail = ", ".join(types) if types else None
        elif isinstance(restrictions, dict):
            new_detail = restrictions.get("restriction_type")

    elif event == "ACCOUNT_VIOLATION":
        new_label  = "RESTRITA"
        vinfo      = value.get("violation_info") or {}
        new_detail = vinfo.get("violation_type")

    elif event in ("VERIFIED_ACCOUNT",):
        new_label = "OK"

    # "under review" / review-requested signals
    elif "review" in event.lower():
        new_label = "ANALISANDO"

    if new_label is None:
        return

    for user_id in find_users_with_waba(waba_id):
        from ..json_store import load_user_bms, save_user_bms
        data = load_user_bms(user_id)
        key  = str(waba_id).strip()
        if key not in data or not isinstance(data.get(key), dict):
            continue

        entry = data[key]
        snap  = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}

        current_label = snap.get("status_label", "")

        # Positive events only recover from webhook-set bad states
        if new_label == "OK" and current_label not in _WEBHOOK_BAD_LABELS:
            continue

        snap["status_label"] = new_label
        if new_detail is not None:
            snap["status_detail"] = new_detail

        entry["snapshot"] = snap
        data[key] = entry
        save_user_bms(user_id, data)


# ── phone quality → messaging_limit_tier ─────────────────────────────────────

def apply_phone_quality_update(waba_id: str, value: dict) -> None:
    """
    value fields:
      display_phone_number, phone_number_id,
      event (UPGRADE|DOWNGRADE|FLAGGED|UNFLAGGED),
      current_limit (TIER_250|TIER_1K|TIER_10K|TIER_100K|TIER_UNLIMITED)
    """
    current_limit = (value.get("current_limit") or "").strip()
    if not current_limit:
        return

    for user_id in find_users_with_waba(waba_id):
        patch_snapshot(user_id, waba_id, messaging_limit_tier=current_limit)
