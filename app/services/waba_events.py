"""
Handlers for Meta WABA webhook fields that update the stored snapshot:
  - message_template_status_update  → template_status_map + template_counts
  - account_update                  → status_label (+ status_detail)
  - phone_number_quality_update     → messaging_limit_tier
"""
from ..json_store import find_users_with_waba, patch_snapshot
from .meta import templates_status_summary


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


# ── account update → status_label ────────────────────────────────────────────

# Labels that this webhook is allowed to flip back to OK
_WEBHOOK_BAD_LABELS = {"PERMANENTE", "RESTRITA", "ANALISANDO"}


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
