from __future__ import annotations

import threading
import time

import requests

from ..models import User
from ..json_store import load_user_bms

_dirty: set[int] = set()
_dlock = threading.Lock()
_started = False

_WABA_FIELDS = (
    "waba_id", "token", "phone_number_id",
    "adspower_profile_id", "business_manager_id", "payment_account_id", "remarks",
    "serial_number",
)

_MIN_INTERVAL_SECONDS = 60


def mark_dirty(user_id: int) -> None:
    """Queue a user for the next debounced push. Safe to call from any thread."""
    try:
        with _dlock:
            _dirty.add(int(user_id))
    except Exception:
        pass


def _get_interval_seconds(app) -> int:
    """Admin-editable reconciliation interval, read fresh every cycle."""
    try:
        from .. import db
        from ..models import AppSetting
        with app.app_context():
            row = db.session.get(AppSetting, "lite_sync_interval_seconds")
            if row and row.value:
                return max(_MIN_INTERVAL_SECONDS, int(row.value))
    except Exception:
        pass
    return max(_MIN_INTERVAL_SECONDS, app.config.get("LITE_SYNC_INTERVAL_SECONDS", 600))


def _build_user_payload(user: User) -> dict:
    bms = load_user_bms(user.id)
    wabas = []
    for entry in bms.values():
        if not isinstance(entry, dict) or not entry.get("waba_id"):
            continue
        w = {f: entry.get(f, "") for f in _WABA_FIELDS}
        w["snapshot"] = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}
        wabas.append(w)
    return {
        "source_id": user.id,
        "username": user.username,
        "password_hash": user.password_hash,
        "is_banned": bool(user.is_banned),
        "wabas": wabas,
    }


def push_users(app, users: list[User]) -> None:
    """Best-effort push. Failures are logged and healed by the next reconciliation."""
    if not users:
        return
    if not app.config.get("LITE_SYNC_ENABLED"):
        return
    token = app.config.get("LITE_SYNC_TOKEN") or ""
    if not token:
        print("[LITE_SYNC] LITE_SYNC_TOKEN not configured — skipping push.", flush=True)
        return

    payload = {"users": [_build_user_payload(u) for u in users]}
    url = app.config["LITE_BASE_URL"].rstrip("/") + "/api/v1/sync/users"
    try:
        resp = requests.post(url, json=payload, headers={"X-Sync-Token": token}, timeout=10)
        if resp.status_code >= 300:
            print(f"[LITE_SYNC] push rejected ({resp.status_code}): {resp.text[:300]}", flush=True)
    except Exception as exc:
        print(f"[LITE_SYNC] push failed (will heal at next reconcile): {exc}", flush=True)


def push_user(app, user_id: int) -> None:
    from .. import db
    with app.app_context():
        user = db.session.get(User, user_id)
        if user:
            push_users(app, [user])


def backfill_all(app) -> None:
    """Full reconciliation: push every user. Idempotent, self-healing."""
    with app.app_context():
        push_users(app, User.query.all())


def _flusher_loop(app) -> None:
    while True:
        time.sleep(5)
        with _dlock:
            ids = list(_dirty)
            _dirty.clear()
        if not ids:
            continue
        with app.app_context():
            users = User.query.filter(User.id.in_(ids)).all()
            push_users(app, users)


def _reconciliation_loop(app) -> None:
    # Run once shortly after boot so Lite is populated even if it was down at startup.
    time.sleep(10)
    while True:
        try:
            backfill_all(app)
        except Exception as exc:
            print(f"[LITE_SYNC] reconciliation error: {exc}", flush=True)
        time.sleep(_get_interval_seconds(app))


def ensure_lite_sync(app) -> None:
    """Start the debounced-push and reconciliation threads (idempotent)."""
    global _started
    with _dlock:
        if _started:
            return
        _started = True

    if not app.config.get("LITE_SYNC_ENABLED"):
        print("[LITE_SYNC] disabled via LITE_SYNC_ENABLED — not starting threads.", flush=True)
        return

    threading.Thread(target=_flusher_loop, args=(app,), daemon=True, name="lite-sync-flusher").start()
    threading.Thread(target=_reconciliation_loop, args=(app,), daemon=True, name="lite-sync-reconciler").start()
