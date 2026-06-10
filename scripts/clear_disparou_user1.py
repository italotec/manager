"""
One-time script: clear all "Disparou" fields from every WABA of user 1.
Run from the app root: python scripts/clear_disparou_user1.py
"""
import json
import os

FIELDS = ("disparou_at", "disparo_events", "ultimo_disparo")

path = os.path.join(os.getcwd(), "instance", "users", "1", "bms.json")

with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

scanned = 0
cleared = 0

for waba_id, entry in data.items():
    if not isinstance(entry, dict):
        continue
    snap = entry.get("snapshot")
    if not isinstance(snap, dict):
        continue
    scanned += 1
    for field in FIELDS:
        if field in snap:
            del snap[field]
            cleared += 1

with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=4, ensure_ascii=False)

print(f"Done. WABAs scanned: {scanned}, fields removed: {cleared}")
