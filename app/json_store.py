import os
import json
import time
from typing import Dict, Any

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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def upsert_waba(user_id: int, waba_id: str, token: str) -> None:
    data = load_user_bms(user_id)
    key = str(waba_id).strip()
    if not key:
        return

    entry = data.get(key, {}) if isinstance(data.get(key), dict) else {}
    entry["waba_id"] = key
    entry["token"] = token
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
