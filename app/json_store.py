import os
import json
import time
import threading
import tempfile
from typing import Dict, Any

if os.name == "nt":
    import msvcrt

    def _lock_file(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock_file(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

# Serializes read-modify-write on bms.json across threads. Without it, concurrent
# disparo jobs (multi-BM batch) clobber each other's snapshot updates and can
# corrupt the file.
_WRITE_LOCK = threading.RLock()

def user_dir(user_id: int) -> str:
    base = os.path.join(os.getcwd(), "instance", "users", str(user_id))
    os.makedirs(base, exist_ok=True)
    return base

def bms_path(user_id: int) -> str:
    return os.path.join(user_dir(user_id), "bms.json")

def ensure_user_bms_file(user_id: int) -> str:
    path = bms_path(user_id)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=4, ensure_ascii=False)
    return path

def load_user_bms(user_id: int) -> Dict[str, Any]:
    path = ensure_user_bms_file(user_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}

def save_user_bms(user_id: int, data: Dict[str, Any]) -> None:
    path = ensure_user_bms_file(user_id)
    dir_name = os.path.dirname(path)
    lock_path = path + ".lock"
    # fcntl.flock serializes concurrent writers across processes; the threading
    # RLock above serializes within a process. Both are needed.
    with open(lock_path, "w") as lock_file:
        lock_file.write("x")
        lock_file.flush()
        _lock_file(lock_file)
        try:
            # mkstemp gives each writer its own unique temp file so concurrent
            # writes never interleave into a shared .tmp file.
            fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            _unlock_file(lock_file)

    # This is the single choke point for every WABA/snapshot/status mutation
    # (manual add, webhook status change, Meta sync-job refresh), so it's the
    # right place to trigger a Manager Lite replication push. Import is lazy
    # to avoid a circular import (lite_sync imports load_user_bms from here),
    # and the whole thing is best-effort — a sync hiccup must never break a
    # WABA write.
    try:
        from .services import lite_sync
        lite_sync.mark_dirty(user_id)
    except Exception:
        pass

def upsert_waba(user_id: int, waba_id: str, token: str, adspower_profile_id: str = "",
                business_manager_id: str = "", payment_account_id: str = "") -> None:
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        key = str(waba_id).strip()
        if not key:
            return

        entry = data.get(key, {}) if isinstance(data.get(key), dict) else {}
        entry["waba_id"] = key
        entry["token"] = token
        entry["adspower_profile_id"] = adspower_profile_id or entry.get("adspower_profile_id", "")
        entry["business_manager_id"] = business_manager_id or entry.get("business_manager_id", "")
        entry["payment_account_id"] = payment_account_id or entry.get("payment_account_id", "")
        entry.setdefault("phone_number_id", "")
        entry.setdefault("templates", [])

        snap = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}
        snap.setdefault("waba_name", "")
        snap.setdefault("phone_numbers", [])
        snap.setdefault("template_counts", {"APPROVED": 0, "PAUSED": 0, "DISABLED": 0, "OTHER": 0})
        snap.setdefault("last_sync_at", 0)
        snap.setdefault("last_error", "")

        entry["snapshot"] = snap
        data[key] = entry
        save_user_bms(user_id, data)

def update_snapshot(user_id: int, waba_id: str, **fields) -> None:
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        key = str(waba_id).strip()
        if key not in data or not isinstance(data.get(key), dict):
            return

        entry = data[key]
        snap = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}

        for k, v in fields.items():
            snap[k] = v

        if "last_sync_at" not in fields:
            snap["last_sync_at"] = int(time.time())

        entry["snapshot"] = snap
        data[key] = entry
        save_user_bms(user_id, data)


def save_waba_remarks(user_id: int, waba_id: str, text: str) -> None:
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        key = str(waba_id).strip()
        if key in data and isinstance(data.get(key), dict):
            data[key]["remarks"] = text
            save_user_bms(user_id, data)


def patch_snapshot(user_id: int, waba_id: str, **fields) -> None:
    """Update specific snapshot fields without touching last_sync_at."""
    with _WRITE_LOCK:
        data = load_user_bms(user_id)
        key = str(waba_id).strip()
        if key not in data or not isinstance(data.get(key), dict):
            return

        entry = data[key]
        snap = entry.get("snapshot", {}) if isinstance(entry.get("snapshot"), dict) else {}

        for k, v in fields.items():
            snap[k] = v

        entry["snapshot"] = snap
        data[key] = entry
        save_user_bms(user_id, data)


def photos_dir(user_id: int) -> str:
    path = os.path.join(user_dir(user_id), "photos")
    os.makedirs(path, exist_ok=True)
    return path


def find_users_with_waba(waba_id: str) -> list:
    """Return list of user_ids whose bms.json contains the given WABA id."""
    key = str(waba_id).strip()
    base = os.path.join(os.getcwd(), "instance", "users")
    if not os.path.isdir(base):
        return []
    result = []
    for name in os.listdir(base):
        if not name.isdigit():
            continue
        user_id = int(name)
        bms = load_user_bms(user_id)
        if key in bms and isinstance(bms.get(key), dict):
            result.append(user_id)
    return result
