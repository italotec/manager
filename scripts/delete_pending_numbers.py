"""
Delete PENDING / not-connected WhatsApp phone numbers via Meta's INTERNAL
Business Manager GraphQL mutation (useDeleteWhatsAppPhoneNumberMutation).

Why this exists: the public Graph API `DELETE /{phone_id}` refuses unregistered
(PENDING) numbers ("does not support this operation"). The WhatsApp Manager UI
instead calls business.facebook.com/api/graphql with the mutation
`xfb_offboard_whatsapp_business_api_phone_number`, which DOES delete PENDING
numbers — but it is browser-session authenticated and requires a freshly
password-confirmed sensitive-op token (#PWD_BROWSER blob).

This tool:
  1. Opens an AdsPower profile (the logged-in FB session) via the Local API.
  2. Harvests the live session data from the page (fb_dtsg, lsd, actor_id) and
     the current password-encryption public key + keyId, all via CDP.
  3. Encrypts the FB account password into a fresh #PWD_BROWSER:5 blob (Python,
     using libsodium sealed-box + AES-256-GCM — verified to match Meta's format).
  4. For each phone_number_id, fires the delete mutation FROM INSIDE the browser
     (CDP Runtime.evaluate -> fetch), so cookies + the profile's proxy/IP are used
     (avoids tripping a security checkpoint).

Dry-run by default. Pass --apply to actually delete.

Required env:
  FB_PASSWORD            FB account password for the logged-in actor.
Optional:
  ADSPOWER_BASE          default http://local.adspower.net:50325
  FB_PUBKEY / FB_KEYID   override password pubkey/keyId if auto-harvest fails.

Usage:
  set FB_PASSWORD=...   (PowerShell:  $env:FB_PASSWORD="...")
  py scripts/delete_pending_numbers.py --profile <adspower_id> --business-id 2077282286473604 --ids-file pending.json
  py scripts/delete_pending_numbers.py --profile <id> --business-id <bid> --ids-file pending.json --limit 1 --apply
"""

import argparse
import base64
import json
import os
import struct
import sys
import time

import requests
import websocket  # websocket-client

from nacl.bindings import crypto_box_seal
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEFAULT_BASE = "http://local.adspower.net:50325"
APP_ID = "436761779744620"            # WhatsApp Manager app id (from HAR)
DELETE_DOC_ID = "10009486335757444"   # useDeleteWhatsAppPhoneNumberMutation
FRIENDLY = "useDeleteWhatsAppPhoneNumberMutation"


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

    def attach_page(self, url_hint: str | None = None) -> str:
        targets = self._send("Target.getTargets").get("targetInfos", [])
        page = None
        for t in targets:
            if t.get("type") == "page" and (not url_hint or url_hint in (t.get("url") or "")):
                page = t
                break
        if not page:
            page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            created = self._send("Target.createTarget", {"url": "about:blank"})
            page = {"targetId": created["targetId"]}
        sess = self._send("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})
        return sess["sessionId"]

    def navigate(self, session_id: str, url: str):
        self._send("Page.enable", session_id=session_id)
        self._send("Runtime.enable", session_id=session_id)
        self._send("Page.navigate", {"url": url}, session_id=session_id)
        # wait for the page to be interactive and the FB module loader present
        deadline = time.time() + 45
        while time.time() < deadline:
            time.sleep(1.5)
            r = self.evaluate(session_id, "document.readyState==='complete' && typeof require==='function'")
            if r is True:
                time.sleep(2)
                return
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
def adspower_start(base: str, profile_id: str) -> str:
    r = requests.get(f"{base}/api/v1/browser/start", params={"user_id": profile_id}, timeout=120)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"AdsPower start error: {body.get('msg', body)}")
    ws_url = ((body.get("data") or {}).get("ws") or {}).get("puppeteer")
    if not ws_url:
        raise RuntimeError(f"No CDP (puppeteer) endpoint: {body}")
    return ws_url


def adspower_stop(base: str, profile_id: str):
    try:
        requests.get(f"{base}/api/v1/browser/stop", params={"user_id": profile_id}, timeout=30)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Session harvest — runs JS in the manager page to read live tokens + pubkey.
# --------------------------------------------------------------------------
HARVEST_JS = r"""
(async () => {
  const out = {fb_dtsg:null, lsd:null, actor_id:null, keyId:null, publicKey:null, err:null};
  try { out.fb_dtsg = require('DTSGInitialData').token; } catch(e){}
  try { out.lsd = require('LSD').token; } catch(e){}
  try {
    const cu = require('CurrentUserInitialData');
    out.actor_id = cu.ACCOUNT_ID || cu.USER_ID || null;
  } catch(e){}
  // Password encryption key provider (sensitive-op re-auth). Try the known module.
  try {
    const prov = require('XBrowserNativePasswordEncryptionKeyProvider');
    const getter = prov.getKeyProvider ? prov.getKeyProvider() : prov;
    await new Promise((resolve) => {
      let done=false;
      const cb = (keyId, publicKey) => { if(done) return; done=true; out.keyId=keyId; out.publicKey=publicKey; resolve(); };
      try { getter.get(cb); } catch(e){ out.err='get() failed: '+e.message; resolve(); }
      setTimeout(()=>{ if(!done){ out.err=(out.err||'')+' key timeout'; resolve(); } }, 8000);
    });
  } catch(e){ out.err='no key provider: '+e.message; }
  return JSON.stringify(out);
})()
"""


def harvest(cdp: CDP, session_id: str) -> dict:
    raw = cdp.evaluate(session_id, HARVEST_JS, await_promise=True)
    data = json.loads(raw) if raw else {}
    return data


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
            "phone_number_id": str(phone_id),
            "password": {"sensitive_string_value": encrypted_pwd},
            "reason": "",
            "source_surface": "WHATSAPP_MANAGER",
        }
    }
    form = {
        "av": actor_id,
        "__user": actor_id,
        "__a": "1",
        "fb_dtsg": fb_dtsg,
        "jazoest": jazoest_of(fb_dtsg),
        "lsd": lsd,
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": FRIENDLY,
        "variables": json.dumps(variables),
        "server_timestamps": "true",
        "doc_id": DELETE_DOC_ID,
    }
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


def load_ids(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for item in data:
        if isinstance(item, str):
            out.append({"phone_id": item})
        elif isinstance(item, dict) and item.get("phone_id"):
            out.append(item)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, help="AdsPower profile id (logged-in FB session)")
    ap.add_argument("--business-id", required=True, help="Business manager id for the manager page context")
    ap.add_argument("--ids-file", required=True, help="JSON list of phone ids (or objects with phone_id)")
    ap.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N ids (0=all)")
    ap.add_argument("--keep-open", action="store_true", help="Leave AdsPower browser open at the end")
    ap.add_argument("--sleep", type=float, default=1.5, help="Seconds between deletes")
    args = ap.parse_args()

    base = (os.getenv("ADSPOWER_BASE") or DEFAULT_BASE).rstrip("/")
    password = os.getenv("FB_PASSWORD", "")
    if args.apply and not password:
        print("ERROR: FB_PASSWORD env var required for --apply.")
        return 2

    ids = load_ids(args.ids_file)
    if args.limit:
        ids = ids[: args.limit]
    print(f"Loaded {len(ids)} phone id(s) to process. Mode: {'APPLY' if args.apply else 'DRY RUN'}")

    print(f"Opening AdsPower profile {args.profile} ...")
    ws_url = adspower_start(base, args.profile)
    cdp = CDP(ws_url)
    try:
        session_id = cdp.attach_page()
        url = (f"https://business.facebook.com/latest/whatsapp_manager/phone_numbers/"
               f"?business_id={args.business_id}&tab=phone-numbers")
        print(f"Navigating to WhatsApp Manager ({args.business_id}) ...")
        cdp.navigate(session_id, url)

        sess = harvest(cdp, session_id)
        fb_dtsg = sess.get("fb_dtsg")
        lsd = sess.get("lsd")
        actor_id = sess.get("actor_id")
        pubkey = os.getenv("FB_PUBKEY") or sess.get("publicKey")
        keyid = os.getenv("FB_KEYID")
        keyid = int(keyid) if keyid else sess.get("keyId")

        print("Harvested session:")
        print(f"  fb_dtsg : {'OK' if fb_dtsg else 'MISSING'}")
        print(f"  lsd     : {'OK' if lsd else 'MISSING'}")
        print(f"  actor_id: {actor_id}")
        print(f"  keyId   : {keyid}")
        print(f"  pubkey  : {'OK ('+str(len(pubkey))+' hex)' if pubkey else 'MISSING'}  {sess.get('err') or ''}")

        if not (fb_dtsg and lsd and actor_id):
            print("ERROR: could not harvest fb_dtsg/lsd/actor_id from the page. Is the profile logged in to Business Manager?")
            return 3

        if not args.apply:
            print("\nDRY RUN — would delete:")
            for it in ids:
                print(f"  {it['phone_id']}  {it.get('display','')}  {it.get('status','')}")
            print(f"\nTotal: {len(ids)}. Re-run with --apply (and FB_PASSWORD set) to delete.")
            return 0

        if not (pubkey and keyid is not None):
            print("ERROR: missing password pubkey/keyId. Set FB_PUBKEY/FB_KEYID env (grab from devtools) and retry.")
            return 4

        ok = err = 0
        print("\nDeleting...")
        for it in ids:
            pid = str(it["phone_id"])
            blob = encrypt_fb_password(password, pubkey, int(keyid))  # fresh timestamp each call
            res = delete_in_browser(cdp, session_id, fb_dtsg=fb_dtsg, lsd=lsd,
                                    actor_id=actor_id, phone_id=pid, encrypted_pwd=blob)
            if res.get("ok"):
                ok += 1
                print(f"  OK  {pid}  {it.get('display','')}")
            else:
                err += 1
                print(f"  ERR {pid}  {it.get('display','')}: {res.get('raw') or res.get('status')}")
            time.sleep(args.sleep)
        print(f"\nDone. deleted={ok} failed={err} total={len(ids)}")
        return 0
    finally:
        cdp.close()
        if not args.keep_open:
            adspower_stop(base, args.profile)
            print("AdsPower browser stopped.")
        else:
            print("Left AdsPower browser open (--keep-open).")


if __name__ == "__main__":
    raise SystemExit(main())
