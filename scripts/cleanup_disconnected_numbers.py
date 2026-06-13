"""
One-off cleanup: delete unregistered phone numbers from Meta WABAs.

Targets numbers whose status is PENDING, UNVERIFIED, DISCONNECTED, or UNKNOWN —
i.e. numbers that were added but never fully registered. CONNECTED, FLAGGED,
RATE_LIMITED, RESTRICTED, BANNED are left untouched.

Usage (from repo root on the VPS):
    python scripts/cleanup_disconnected_numbers.py --user-id 1          # dry-run
    python scripts/cleanup_disconnected_numbers.py --user-id 1 --apply  # delete for real
"""

import argparse
import json
import os
import sys

# Allow running from repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.json_store import load_user_bms, save_user_bms
from app.services.meta import get_phone_numbers, delete_phone_number

DELETABLE_STATUSES = {"PENDING", "UNVERIFIED", "DISCONNECTED", "UNKNOWN"}


def main():
    parser = argparse.ArgumentParser(description="Delete unregistered phone numbers from WABAs.")
    parser.add_argument("--user-id", type=int, default=1, help="User ID (default: 1)")
    parser.add_argument("--apply", action="store_true", help="Actually delete (default is dry-run)")
    parser.add_argument("--api-version", default=os.environ.get("META_API_VERSION", "v23.0"))
    parser.add_argument("--dump", default="", help="Write deletable ids to this JSON file (for delete_pending_numbers.py)")
    args = parser.parse_args()

    user_id = args.user_id
    apply = args.apply
    api_version = args.api_version

    bms = load_user_bms(user_id)
    if not bms:
        print(f"No BMS data found for user {user_id}.")
        return

    print(f"{'[APPLY]' if apply else '[DRY RUN]'} Scanning WABAs for user {user_id} (api={api_version})\n")

    to_delete = []  # list of (waba_id, token, phone_id, display, status)

    for key, data in bms.items():
        if not isinstance(data, dict):
            continue
        waba_id = str(data.get("waba_id") or key).strip()
        token = (data.get("token") or "").strip()
        if not token:
            print(f"  WABA {waba_id}: no token, skipping")
            continue

        phones, err = get_phone_numbers(api_version, token, waba_id)
        if err:
            print(f"  WABA {waba_id}: ERROR fetching phones: {err}")
            continue

        waba_name = (data.get("snapshot") or {}).get("waba_name") or waba_id
        ads_id = (data.get("adspower_profile_id") or "").strip()
        bm_id = (data.get("business_manager_id") or "").strip()
        deletable = [p for p in phones if (p.get("status") or "").upper() in DELETABLE_STATUSES]
        keepers = [p for p in phones if (p.get("status") or "").upper() not in DELETABLE_STATUSES]

        if not deletable:
            print(f"  WABA {waba_id} ({waba_name}): {len(phones)} phone(s), none deletable")
            continue

        print(f"  WABA {waba_id} ({waba_name}): {len(phones)} phone(s), {len(deletable)} to delete, {len(keepers)} to keep [ads={ads_id or '-'}]")
        for p in deletable:
            disp = p.get("display_phone_number", "?")
            st = p.get("status", "?")
            pid = p.get("id", "?")
            print(f"    -> DELETE  id={pid}  {disp}  status={st}")
            to_delete.append((waba_id, token, pid, disp, st, key, ads_id, bm_id))
        for p in keepers:
            disp = p.get("display_phone_number", "?")
            st = p.get("status", "?")
            pid = p.get("id", "?")
            print(f"    -- KEEP    id={pid}  {disp}  status={st}")

    print(f"\nTotal to delete: {len(to_delete)}")

    if args.dump:
        dump = [
            {"phone_id": pid, "waba_id": waba_id, "display": disp, "status": status,
             "adspower_profile_id": ads_id, "business_manager_id": bm_id}
            for (waba_id, token, pid, disp, status, bms_key, ads_id, bm_id) in to_delete
        ]
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump(dump, f, indent=2, ensure_ascii=False)
        print(f"Wrote {len(dump)} deletable ids to {args.dump}")

    if not apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply to execute.")
        return

    if not to_delete:
        print("Nothing to delete.")
        return

    print("\nDeleting...")
    deleted_ids = set()
    for waba_id, token, phone_id, disp, status, bms_key, ads_id, bm_id in to_delete:
        ok, err = delete_phone_number(api_version, token, phone_id)
        if ok:
            print(f"  OK  {phone_id}  {disp}  ({status})")
            deleted_ids.add((bms_key, phone_id))
        else:
            print(f"  ERR {phone_id}  {disp}  ({status}): {err}")

    # Clear top-level phone_number_id if it pointed at a deleted number.
    if deleted_ids:
        bms = load_user_bms(user_id)
        changed = False
        for bms_key, phone_id in deleted_ids:
            entry = bms.get(bms_key)
            if isinstance(entry, dict) and entry.get("phone_number_id") == phone_id:
                entry["phone_number_id"] = ""
                bms[bms_key] = entry
                changed = True
        if changed:
            save_user_bms(user_id, bms)
            print("\nUpdated bms.json: cleared phone_number_id for deleted numbers.")

    print("\nDone.")


if __name__ == "__main__":
    main()
