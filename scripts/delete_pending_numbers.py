"""
Delete PENDING / not-connected WhatsApp phone numbers via Meta's INTERNAL
Business Manager GraphQL mutation (useDeleteWhatsAppPhoneNumberMutation).

Why this exists: the public Graph API `DELETE /{phone_id}` refuses unregistered
(PENDING) numbers ("does not support this operation"). The WhatsApp Manager UI
instead calls business.facebook.com/api/graphql with the mutation
`xfb_offboard_whatsapp_business_api_phone_number`, which DOES delete PENDING
numbers — but it is browser-session authenticated and requires a freshly
password-confirmed sensitive-op token (#PWD_BROWSER blob).

Each WABA is linked to its own AdsPower profile (its own FB account), so the
work is grouped per profile. For each profile this tool:
  1. Reads the saved FB account password from the AdsPower Local API.
  2. Opens the profile (logged-in FB session) and harvests live session data
     from the page (fb_dtsg, lsd, actor_id) + the password-encryption pubkey/keyId
     via CDP.
  3. Encrypts the password into a fresh #PWD_BROWSER:5 blob (Python, libsodium
     sealed-box + AES-256-GCM — verified to match Meta's format).
  4. Fires the delete mutation FROM INSIDE the browser (CDP Runtime.evaluate ->
     fetch), so cookies + the profile's proxy/IP are used (avoids checkpoints).

Dry-run by default. Pass --apply to actually delete.

Usage:
  # See per-profile grouping (no browser opened):
  py scripts/delete_pending_numbers.py --ids-file scripts/pending.json

  # Verify harvest on ONE profile (opens it, no delete):
  py scripts/delete_pending_numbers.py --ids-file scripts/pending.json --profile k1dbx8ht

  # Delete one number on one profile (test):
  py scripts/delete_pending_numbers.py --ids-file scripts/pending.json --profile k1dbx8ht --limit 1 --apply

  # Full run, all profiles:
  py scripts/delete_pending_numbers.py --ids-file scripts/pending.json --apply
"""

import argparse
import base64
import json
import os
import struct
import sys
import time
from collections import OrderedDict, defaultdict

import requests
import websocket  # websocket-client

from nacl.bindings import crypto_box_seal
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEFAULT_BASE = "http://local.adspower.net:50360"
APP_ID = "436761779744620"            # WhatsApp Manager app id (from HAR)
DELETE_DOC_ID = "10009486335757444"   # useDeleteWhatsAppPhoneNumberMutation
FRIENDLY = "useDeleteWhatsAppPhoneNumberMutation"
MANAGER_URL = "https://business.facebook.com/latest/whatsapp_manager/phone_numbers/?tab=phone-numbers"


# --------------------------------------------------------------------------
# FB password encryption (#PWD_BROWSER:5)  — layout verified against a real blob:
# base64( [0x01][keyId][LE16 sealed_len=80][sealed 80B][gcm_tag 16B][ciphertext] )
# AES-256-GCM, IV = 12 zero bytes, AAD = ascii(unix_time). sealed = crypto_box_seal(symkey, pubkey).
# --------------------------------------------------------------------------
def encrypt_fb_password(password: str, pubkey_hex: str, key_id: int, t: int | None = None) -> str:
    if t is None:
        t = int(time.time())
    sym = os.urandom(32)
    sealed = crypto_box_seal(sym, bytes.fromhex(pubkey_hex))
    out = AESGCM(sym).encrypt(b"\x00" * 12, password.encode("utf-8"), str(t).encode("ascii"))
    ct, tag = out[:-16], out[-16:]
    buf = bytes([1, key_id]) + struct.pack("<H", len(sealed)) + sealed + tag + ct
    return f"#PWD_BROWSER:5:{t}:{base64.b64encode(buf).decode()}"


def jazoest_of(fb_dtsg: str) -> str:
    return "2" + str(sum(ord(c) for c in fb_dtsg))


# --------------------------------------------------------------------------
# Minimal CDP client with flat-session routing (navigate + evaluate in a page).
# --------------------------------------------------------------------------
class CDP:
    def __init__(self, ws_url: str, timeout: int = 60):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._id = 0

    def _send(self, method, params=None, session_id=None):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        want = self._id
        while True:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == want:
                if "error" in resp:
                    raise RuntimeError(f"CDP {method}: {resp['error']}")
                return resp.get("result") or {}

    def open_page(self, url: str) -> str:
        """Create a fresh tab navigated to url, attach, wait for it to load."""
        created = self._send("Target.createTarget", {"url": url})
        sess = self._send("Target.attachToTarget", {"targetId": created["targetId"], "flatten": True})
        session_id = sess["sessionId"]
        self._send("Page.enable", session_id=session_id)
        self._send("Runtime.enable", session_id=session_id)
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                ready = self.evaluate(
                    session_id,
                    "document.readyState==='complete' && location.hostname.indexOf('facebook.com')>=0",
                )
            except Exception:
                ready = False
            if ready is True:
                time.sleep(2.5)  # let bootstrap JSON settle
                return session_id
        raise RuntimeError("navigation/load timeout")

    def evaluate(self, session_id: str, expr: str, await_promise: bool = False):
        res = self._send("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        }, session_id=session_id)
        if res.get("exceptionDetails"):
            raise RuntimeError(f"JS exception: {json.dumps(res['exceptionDetails'])[:400]}")
        return res.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# AdsPower Local API
# --------------------------------------------------------------------------
def adspower_get(base: str, path: str, **params) -> dict:
    r = requests.get(f"{base}{path}", params=params, timeout=120)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"AdsPower {path}: {body.get('msg', body)}")
    return body.get("data") or {}


def adspower_account(base: str, profile_id: str) -> tuple[str, str]:
    """Return (username, password) saved in the AdsPower profile."""
    data = adspower_get(base, "/api/v1/user/list", user_id=profile_id)
    lst = data.get("list") or []
    if not lst:
        return "", ""
    item = lst[0]
    return (item.get("username") or "").strip(), (item.get("password") or "")


def adspower_start(base: str, profile_id: str) -> str:
    data = adspower_get(base, "/api/v1/browser/start", user_id=profile_id)
    ws_url = ((data.get("ws") or {}).get("puppeteer"))
    if not ws_url:
        raise RuntimeError(f"No CDP (puppeteer) endpoint for {profile_id}: {data}")
    return ws_url


def adspower_stop(base: str, profile_id: str):
    try:
        adspower_get(base, "/api/v1/browser/stop", user_id=profile_id)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Session harvest — runs JS in the manager page to read live tokens + pubkey.
# --------------------------------------------------------------------------
HARVEST_JS = r"""
(async () => {
  const out = {fb_dtsg:null, lsd:null, actor_id:null, keyId:null, publicKey:null,
               hasRequire:(typeof require==='function'), url:location.href, err:null};
  const html = document.documentElement.outerHTML;
  const m = (re) => { const x = html.match(re); return x ? x[1] : null; };
  // fb_dtsg / lsd: prefer require, fall back to bootstrap JSON regex
  try { if (typeof require==='function') out.fb_dtsg = require('DTSGInitialData').token; } catch(e){}
  if (!out.fb_dtsg) out.fb_dtsg = m(/"DTSGInitialData",\[\],\{"token":"([^"]+)"/) || m(/name=\\?"fb_dtsg\\?" value=\\?"([^"\\]+)/);
  try { if (typeof require==='function') out.lsd = require('LSD').token; } catch(e){}
  if (!out.lsd) out.lsd = m(/"LSD",\[\],\{"token":"([^"]+)"/);
  out.actor_id = m(/"USER_ID":"(\d+)"/) || m(/"ACCOUNT_ID":"(\d+)"/);
  // password encryption key: try the provider module, then bootstrap regex
  try {
    const prov = require('XBrowserNativePasswordEncryptionKeyProvider');
    const getter = prov.getKeyProvider ? prov.getKeyProvider() : prov;
    await new Promise((resolve) => {
      let done=false;
      const cb = (keyId, publicKey) => { if(done) return; done=true; out.keyId=keyId; out.publicKey=publicKey; resolve(); };
      try { getter.get(cb); } catch(e){ out.err='get:'+e.message; resolve(); }
      setTimeout(()=>{ if(!done){ out.err=(out.err||'')+' keytimeout'; resolve(); } }, 8000);
    });
  } catch(e){ out.err=(out.err||'')+' noprov:'+e.message; }
  if (!out.publicKey) out.publicKey = m(/"public_key":"([0-9a-f]{64})"/) || m(/"publicKey":"([0-9a-f]{64})"/);
  if (out.keyId==null){ const k = m(/"key_id":(\d+)/) || m(/"keyId":(\d+)/); if(k) out.keyId = parseInt(k,10); }
  return JSON.stringify(out);
})()
"""


def harvest(cdp: CDP, session_id: str) -> dict:
    raw = cdp.evaluate(session_id, HARVEST_JS, await_promise=True)
    return json.loads(raw) if raw else {}


# --------------------------------------------------------------------------
# In-browser delete (fetch runs in page context -> profile cookies + proxy/IP).
# --------------------------------------------------------------------------
def delete_in_browser(cdp: CDP, session_id: str, *, fb_dtsg: str, lsd: str, actor_id: str,
                      phone_id: str, encrypted_pwd: str) -> dict:
    variables = {
        "input": {
            "actor_id": actor_id,
            "client_mutation_id": "1",
            "app_id": APP_ID,
            "log_session_id": f"WBxP--{int(time.time())}-{os.getpid()}",
            "phone_number_id": str(phone_id),
            "password": {"sensitive_string_value": encrypted_pwd},
            "reason": "",
            "source_surface": "WHATSAPP_MANAGER",
        }
    }
    form = OrderedDict([
        ("av", actor_id),
        ("__user", actor_id),
        ("__a", "1"),
        ("fb_dtsg", fb_dtsg),
        ("jazoest", jazoest_of(fb_dtsg)),
        ("lsd", lsd),
        ("fb_api_caller_class", "RelayModern"),
        ("fb_api_req_friendly_name", FRIENDLY),
        ("variables", json.dumps(variables)),
        ("server_timestamps", "true"),
        ("doc_id", DELETE_DOC_ID),
    ])
    body = "&".join(f"{requests.utils.quote(k, safe='')}={requests.utils.quote(v, safe='')}"
                    for k, v in form.items())
    js = (
        "(async()=>{const r=await fetch('/api/graphql/',{method:'POST',"
        "headers:{'content-type':'application/x-www-form-urlencoded',"
        f"'x-fb-friendly-name':{json.dumps(FRIENDLY)},'x-fb-lsd':{json.dumps(lsd)}}},"
        f"body:{json.dumps(body)},credentials:'include'}});"
        "return await r.text();})()"
    )
    text = cdp.evaluate(session_id, js, await_promise=True)
    try:
        j = json.loads(text)
    except Exception:
        return {"ok": False, "raw": (text or "")[:300]}
    status = (((j.get("data") or {}).get("xfb_offboard_whatsapp_business_api_phone_number") or {})
              .get("current_status") or {}).get("status")
    if status == "DELETED":
        return {"ok": True, "status": status}
    return {"ok": False, "raw": json.dumps(j)[:300]}


def load_groups(path: str) -> "OrderedDict[str, list]":
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    groups: "OrderedDict[str, list]" = OrderedDict()
    for item in data:
        if not isinstance(item, dict) or not item.get("phone_id"):
            continue
        prof = (item.get("adspower_profile_id") or "").strip()
        groups.setdefault(prof, []).append(item)
    return groups


def process_profile(base: str, profile_id: str, items: list, *, apply: bool, limit: int,
                    sleep: float, keep_open: bool, pubkey_cache: dict) -> tuple[int, int]:
    """Returns (deleted, failed) for this profile."""
    if limit:
        items = items[:limit]

    username, password = adspower_account(base, profile_id)
    print(f"\n=== Profile {profile_id}  (acct {username or '?'})  — {len(items)} number(s) ===")
    if apply and not password:
        print("  SKIP: no saved password in AdsPower profile.")
        return 0, len(items)

    # Start clean: stop any stale/half-open session left from a prior interrupted run.
    adspower_stop(base, profile_id)
    time.sleep(2.0)  # let it fully close; AdsPower API is rate-limited (~1/s)
    try:
        ws_url = adspower_start(base, profile_id)
    except Exception as e:
        print(f"  SKIP: could not open profile: {e}")
        return 0, len(items)

    cdp = CDP(ws_url)
    ok = err = 0
    try:
        try:
            session_id = cdp.open_page(MANAGER_URL)
        except RuntimeError:
            # one retry: reopen the tab (transient slow load / redirect)
            print("  retry: navigation timed out, retrying once...")
            session_id = cdp.open_page(MANAGER_URL)
        sess = harvest(cdp, session_id)
        fb_dtsg, lsd = sess.get("fb_dtsg"), sess.get("lsd")
        actor_id = sess.get("actor_id") or username
        pubkey = os.getenv("FB_PUBKEY") or sess.get("publicKey") or pubkey_cache.get("pubkey")
        keyid = os.getenv("FB_KEYID")
        keyid = int(keyid) if keyid else (sess.get("keyId") if sess.get("keyId") is not None else pubkey_cache.get("keyid"))
        if pubkey and keyid is not None:
            pubkey_cache["pubkey"], pubkey_cache["keyid"] = pubkey, keyid

        print(f"  harvest: require={sess.get('hasRequire')} url={sess.get('url','')[:60]}")
        print(f"  harvest: fb_dtsg={'OK' if fb_dtsg else 'MISSING'} lsd={'OK' if lsd else 'MISSING'} "
              f"actor={actor_id} keyId={keyid} pubkey={'OK' if pubkey else 'MISSING'} {sess.get('err') or ''}")

        if not (fb_dtsg and lsd and actor_id):
            print("  SKIP: incomplete session harvest (profile logged in to Business Manager?).")
            return 0, len(items)

        if not apply:
            for it in items:
                print(f"    would delete  {it['phone_id']}  {it.get('display','')}  {it.get('status','')}")
            return 0, 0

        if not (pubkey and keyid is not None):
            print("  SKIP: missing password pubkey/keyId (set FB_PUBKEY/FB_KEYID to override).")
            return 0, len(items)

        for it in items:
            pid = str(it["phone_id"])
            blob = encrypt_fb_password(password, pubkey, int(keyid))  # fresh timestamp per call
            res = delete_in_browser(cdp, session_id, fb_dtsg=fb_dtsg, lsd=lsd,
                                    actor_id=actor_id, phone_id=pid, encrypted_pwd=blob)
            if res.get("ok"):
                ok += 1
                print(f"    OK  {pid}  {it.get('display','')}")
            else:
                err += 1
                print(f"    ERR {pid}  {it.get('display','')}: {res.get('raw') or res.get('status')}")
            time.sleep(sleep)
        return ok, err
    finally:
        cdp.close()
        if not keep_open:
            adspower_stop(base, profile_id)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", required=True, help="JSON dump from cleanup_disconnected_numbers.py --dump")
    ap.add_argument("--profile", default="", help="Only process this AdsPower profile id")
    ap.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="Per-profile cap on numbers (0=all)")
    ap.add_argument("--keep-open", action="store_true", help="Leave each AdsPower browser open")
    ap.add_argument("--sleep", type=float, default=1.5, help="Seconds between deletes")
    args = ap.parse_args()

    base = (os.getenv("ADSPOWER_BASE") or DEFAULT_BASE).rstrip("/")
    groups = load_groups(args.ids_file)
    if args.profile:
        groups = OrderedDict((k, v) for k, v in groups.items() if k == args.profile)
        if not groups:
            print(f"No numbers for profile {args.profile} in {args.ids_file}")
            return 1

    total = sum(len(v) for v in groups.values())
    print(f"{'APPLY' if args.apply else 'DRY RUN'} | {len(groups)} profile(s), {total} number(s) | base={base}")

    # Pure grouping view when dry-run with no specific profile (don't open browsers).
    if not args.apply and not args.profile:
        for prof, items in groups.items():
            print(f"  {prof or '(none)'}: {len(items)}")
        print("\nDry-run grouping only. Use --profile <id> to test harvest on one profile, "
              "or --apply to delete.")
        return 0

    pubkey_cache: dict = {}
    tot_ok = tot_err = 0
    failed_profiles = []
    for prof, items in groups.items():
        if not prof:
            print(f"\n=== (no profile id) — {len(items)} number(s): cannot delete without a session, skipping ===")
            tot_err += len(items)
            continue
        try:
            ok, err = process_profile(base, prof, items, apply=args.apply, limit=args.limit,
                                      sleep=args.sleep, keep_open=args.keep_open, pubkey_cache=pubkey_cache)
        except Exception as e:
            # One bad profile (nav timeout, checkpoint, AdsPower hiccup) must not abort the rest.
            print(f"  EXC: profile {prof} failed: {type(e).__name__}: {str(e)[:200]}")
            adspower_stop(base, prof)
            ok, err = 0, len(items[: args.limit] if args.limit else items)
            failed_profiles.append(prof)
        tot_ok += ok
        tot_err += err

    if args.apply:
        print(f"\n==== DONE: deleted={tot_ok} failed={tot_err} total={total} ====")
        if failed_profiles:
            print(f"Profiles to retry ({len(failed_profiles)}): {' '.join(failed_profiles)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
