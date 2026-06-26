from __future__ import annotations

import threading
import time

from ..models import InfoSnapshot, User
from .info_report import compute_bm_metrics, _today_window

_lock = threading.Lock()
_started = False


def _get_report_user() -> User | None:
    """Return the admin with a Prosperidade key set (the df admin)."""
    return User.query.filter_by(is_admin=True).filter(
        User.prosperidade_api_key.isnot(None),
        User.prosperidade_api_key != "",
    ).first()


def refresh_snapshot() -> None:
    """Compute today's BM metrics and persist a new row only if any value changed."""
    from .. import db

    user = _get_report_user()
    if not user:
        return

    start_ts, end_ts, _ = _today_window()
    try:
        metrics = compute_bm_metrics(start_ts, end_ts, user.id)
    except Exception as exc:
        print(f"[INFO_REFRESH] compute_bm_metrics failed: {exc}", flush=True)
        return

    bms  = metrics["wabas_disparadas"]
    sent = metrics["total_sent"]
    delv = metrics["total_delivered"]

    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y-%m-%d")

    latest = (
        InfoSnapshot.query
        .filter_by(user_id=user.id, day=today)
        .order_by(InfoSnapshot.id.desc())
        .first()
    )

    if latest and latest.bms_disparadas == bms and latest.total_sent == sent and latest.total_delivered == delv:
        return

    snap = InfoSnapshot(
        user_id=user.id,
        day=today,
        bms_disparadas=bms,
        total_sent=sent,
        total_delivered=delv,
    )
    db.session.add(snap)
    db.session.commit()
    print(f"[INFO_REFRESH] saved snapshot: bms={bms} sent={sent} delv={delv}", flush=True)


def _db_maintenance() -> None:
    """Periodic housekeeping: prune old rows and checkpoint the WAL.

    Keeps ChatMessage under control (unbounded growth slows all chat queries)
    and prevents the WAL file from growing large (large WAL = slow reads because
    every SELECT must scan it for recent writes).
    """
    from .. import db
    from ..models import ChatMessage
    from datetime import datetime, timedelta

    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        deleted = (
            db.session.query(ChatMessage)
            .filter(ChatMessage.timestamp < cutoff)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        if deleted:
            print(f"[DB_MAINT] pruned {deleted} chat_message rows older than 30 days", flush=True)
    except Exception as exc:
        db.session.rollback()
        print(f"[DB_MAINT] chat_message prune error: {exc}", flush=True)

    try:
        db.session.execute(db.text("PRAGMA wal_checkpoint(PASSIVE)"))
        db.session.commit()
    except Exception as exc:
        print(f"[DB_MAINT] WAL checkpoint error: {exc}", flush=True)


def ensure_refresher(app) -> None:
    """Start the background BM-metrics refresh thread (idempotent — safe to call multiple times)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    def _loop():
        interval = app.config.get("INFO_REFRESH_INTERVAL_SECONDS", 300)
        # run once immediately so data exists on first /info after restart
        with app.app_context():
            try:
                refresh_snapshot()
            except Exception as exc:
                print(f"[INFO_REFRESH] initial refresh error: {exc}", flush=True)
            try:
                _db_maintenance()
            except Exception as exc:
                print(f"[DB_MAINT] initial maintenance error: {exc}", flush=True)
        while True:
            time.sleep(interval)
            with app.app_context():
                try:
                    refresh_snapshot()
                except Exception as exc:
                    print(f"[INFO_REFRESH] refresh error: {exc}", flush=True)
                try:
                    _db_maintenance()
                except Exception as exc:
                    print(f"[DB_MAINT] maintenance error: {exc}", flush=True)

    t = threading.Thread(target=_loop, daemon=True, name="info-refresher")
    t.start()
