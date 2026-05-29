import threading
import time
from typing import Set

_open_pids: Set[str] = set()
_lock = threading.Lock()
_started = False


def _poll_loop():
    from .adspower import AdsPowerClient
    client = AdsPowerClient()
    while True:
        time.sleep(5)
        with _lock:
            pids = list(_open_pids)
        if not pids:
            continue
        still_open = {pid for pid in pids if client.is_browser_active(pid) == "active"}
        with _lock:
            _open_pids.intersection_update(still_open)


def _ensure_started():
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()


def register_open(profile_id: str) -> None:
    """Mark a profile as open and ensure the poll daemon is running."""
    _ensure_started()
    with _lock:
        _open_pids.add(profile_id)


def get_open_profiles() -> Set[str]:
    """Return a snapshot of currently-open profile IDs."""
    with _lock:
        return set(_open_pids)
