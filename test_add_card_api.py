"""
PHASE 1, API VERSION (browser-assisted).

Instead of typing into the DOM card form, this mints the `platform_trust_token` by calling
Facebook's OWN in-page crypto (`BillingPTTUtils.generatePTT`, which lazy-loads
`modularGeneratePTT`, fetches the server encryption key, does ECDH-ES/A256GCM and builds the
binary container for us), then fires `BillingSaveCardCredentialStateMutation` via fetch().
See docs/billing_card_e2ee.md for the full reverse-engineering.

Why still a browser: `generatePTT` needs a live Relay environment (`RelayFBEnvironment`) and the
billing crypto bundle, which only load on a real billing page. No DOM form-filling happens — the
card data goes straight into FB's encryptor. Phase 2 (attach to WABA) reuses the plain API call.

    py scripts/test_add_card_api.py

Edit card details / IDs in test_add_card.py (shared config). This fires a REAL mutation — use a
test card.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

import importlib.util
from urllib.parse import unquote
_spec = importlib.util.spec_from_file_location(
    "tac", str(Path(__file__).parent / "test_add_card.py"))
tac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tac)

SHOT_DIR = Path(__file__).parent / "_card_test_shots"

# Which payment account to add the card to. HAR added at the BM account, then attached to WABA.
# Phase 1 (save new card) needs the BM/business account — NOT the WABA account from the URL.
# Set this once you know it; if None, the script discovers candidates from wizard traffic.
CARD_PAYMENT_ACCOUNT_ID = None        # e.g. "2074283953119105"-style BM account
_WABA_ACCOUNT = tac.PAYMENT_ACCOUNT_ID  # the URL's account (WABA) — to exclude from candidates


def _parse_card():
    num = re.sub(r"\D", "", tac.CARD_NUMBER)
    mm, yy = tac.CARD_EXPIRY.split("/")
    month = str(int(mm))                       # "06" -> "6" (matches HAR)
    year = yy if len(yy) == 4 else "20" + yy   # "32" -> "2032"
    return {
        "number": num, "csc": tac.CARD_CSC, "name": tac.CARD_NAME,
        "exp_month": month, "exp_year": year,
        "bin": num[:8], "last_4": num[-4:],
    }


# Resolve the BM/business payment account that OWNS the WABA account, via
# BillingAddCreditCardScreenQuery -> payment_account.billable_account.owner_business_payment_account.id
RESOLVE_BM_JS = r"""
async ({wabaAccount}) => {
  const dtsg = require("DTSGInitialData").token;
  const lsd  = require("LSD").token;
  const uid  = require("CurrentUserInitialData").USER_ID;
  const charSum = Array.from(dtsg).reduce((s,c)=>s+c.charCodeAt(0),0);
  const jazoest = "2" + (charSum + 50);
  const variables = { paymentAccountID: wabaAccount, country:null, currency:null, intent:null };
  const body = new URLSearchParams({
    av: uid, __user: uid, __a: "1", fb_dtsg: dtsg, jazoest, lsd,
    __comet_req: "15", fb_api_caller_class: "RelayModern",
    fb_api_req_friendly_name: "BillingAddCreditCardScreenQuery", server_timestamps: "true",
    variables: JSON.stringify(variables), doc_id: "36360602320204776",
  });
  const r = await fetch("/api/graphql/", {
    method: "POST",
    headers: { "content-type":"application/x-www-form-urlencoded",
               "x-fb-lsd": lsd, "x-fb-friendly-name": "BillingAddCreditCardScreenQuery" },
    body: body.toString(),
  });
  const t = await r.text();
  const m = t.match(/"owner_business_payment_account":\{"id":"(\d+)"/);
  const self = t.match(/"payment_account":\{[^}]*?"id":"(\d+)"/);
  return { bm: m ? m[1] : null, self: self ? self[1] : null, body: t.slice(0, 300) };
}
"""

# Runs entirely in-page: mint PTT via FB's code, then fire the save mutation.
MINT_AND_SAVE_JS = r"""
async ({card, paymentAccountID, docId, friendly, businessId}) => {
  const req = (n) => require(n);
  const log = {};
  let dtsg, lsd, uid;
  try {
    dtsg = req("DTSGInitialData").token;
    lsd  = req("LSD").token;
    uid  = req("CurrentUserInitialData").USER_ID;
  } catch (e) { return {ok:false, stage:"auth", error:String(e)}; }

  // ---- mint platform_trust_token using FB's own crypto ----
  let ptt;
  try {
    const BillingPTTUtils = req("BillingPTTUtils");
    const env = req("RelayFBEnvironment");
    const billingRelay = { environment: env };
    const input = {
      paymentType: "BILLING_WIZARD",
      authData: {
        credit_card: "$e2ee", csc: "$e2ee",
        expiry_month: card.exp_month, expiry_year: card.exp_year,
      },
      secretPayload: { credit_card: card.number, csc: card.csc },
      authInputOperation: "ADD_CARD",
      paymentAccountID: paymentAccountID,
    };
    // generatePTT(input, "wizard", true, true, l, s, billingRelay, false, false, true)
    ptt = await BillingPTTUtils.generatePTT(
      input, "wizard", true, true, undefined, undefined, billingRelay, false, false, true);
  } catch (e) {
    return {ok:false, stage:"ptt", error: String(e && e.message || e)};
  }
  if (!ptt) return {ok:false, stage:"ptt", error:"empty token"};
  log.ptt_len = ptt.length;

  // ---- fire BillingSaveCardCredentialStateMutation ----
  const variables = {
    input: {
      billing_address: { country_code: "BR" },
      card_data: {
        bin: card.bin,
        cardholder_name: card.name,
        credit_card_number: { sensitive_string_value: "$e2ee" },
        csc: { sensitive_string_value: "$e2ee" },
        expiry_month: card.exp_month,
        expiry_year: card.exp_year,
        last_4: card.last_4,
      },
      client_info: { color_depth:"32", java_enabled:false, screen_height:"765", screen_width:"1440" },
      currency: "BRL",
      network_tokenization_consent_given: false,
      payment_account_id: paymentAccountID,
      payment_intent: "ADD_PM",
      platform_trust_token: ptt,
      recurring_payment_consent_given: false,
      set_default: false,
      share_to_child_payment_account_id: null,
      skip_cvv_for_eea_save: false,
      upl_logging_data: {
        billing_notification_id: "", context: "billingcreditcard",
        credential_type: "NEW_CREDIT_CARD", entry_point: "BILLING_HUB",
        business_id: businessId, target_name: "useBillingAddCreditCardMutation",
        wizard_config_name: "SAVE_CARD_CREDENTIAL", wizard_name: "ADD_PM_BM",
        wizard_screen_name: "add_credit_card_state_display",
      },
      actor_id: uid,
      client_mutation_id: "1",
    },
    getRiskVerificationInfoForAllCredentialsOnPaymentAccount: true,
    paymentAccountID: paymentAccountID,
    includeCreateNewFromOldFragment: false,
    country: null, currency: null, intent: null,
  };

  const charSum = Array.from(dtsg).reduce((s,c)=>s+c.charCodeAt(0),0);
  const jazoest = "2" + (charSum + 50);
  const body = new URLSearchParams({
    av: uid, __user: uid, __a: "1", fb_dtsg: dtsg, jazoest, lsd,
    __comet_req: "15", fb_api_caller_class: "RelayModern",
    fb_api_req_friendly_name: friendly, server_timestamps: "true",
    variables: JSON.stringify(variables), doc_id: docId,
  });
  let t, status;
  try {
    const r = await fetch("/api/graphql/", {
      method: "POST",
      headers: { "content-type":"application/x-www-form-urlencoded",
                 "x-fb-lsd": lsd, "x-fb-friendly-name": friendly },
      body: body.toString(),
    });
    status = r.status; t = await r.text();
  } catch (e) { return {ok:false, stage:"fetch", error:String(e), ...log}; }

  let p = {}; try { p = JSON.parse(t); } catch(_) {}
  const mm = t.match(/"credential_id":"(\d+)"/);
  return {
    ok: !p.errors && t.indexOf("xfb_billing_save_card_credential") !== -1,
    stage: "save", status, credential_id: mm ? mm[1] : null,
    body: t.slice(0, 600), ...log,
  };
}
"""


_ACCT_RE = re.compile(
    r'"(payment_account_id|target_account_id|payment_legacy_account_id)"\s*:\s*"?(\d{6,})"?')


def run():
    card = _parse_card()
    print(f"[api] card parsed: bin={card['bin']} last4={card['last_4']} "
          f"exp={card['exp_month']}/{card['exp_year']}")

    # Collect account ids the wizard itself references (to find the BM account).
    seen_accounts = {}  # id -> set(field names)

    def on_request(req):
        if "/api/graphql" not in req.url:
            return
        try:
            body = req.post_data or ""
        except Exception:
            return
        for field, acct in _ACCT_RE.findall(unquote(body)):
            seen_accounts.setdefault(acct, set()).add(field)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=tac.HEADLESS, slow_mo=tac.SLOWMO_MS,
                                     args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="pt-BR", viewport={"width": 1440, "height": 800})
        tac.inject_cookies(ctx, tac.COOKIES)
        page = ctx.new_page()
        page.on("request", on_request)

        print("[api] opening billing hub + add-card wizard (to load FB crypto modules)…")
        page.goto(tac.BILLING_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)

        tac.click_first(page, [
            lambda p: p.get_by_role("button", name="Adicionar forma de pagamento"),
            lambda p: p.get_by_role("button", name="Add payment method"),
            lambda p: p.get_by_text("Adicionar forma de pagamento", exact=False),
        ], "Add payment method", timeout=15000)
        page.wait_for_timeout(2000)

        # If the (permanent) location/currency screen shows, set BR/BRL so the account is
        # configured before we add a card. Skipped automatically if absent.
        loc = page.get_by_text("Selecione a localização e a moeda", exact=False)
        if loc.count() and loc.first.is_visible():
            print("[api] location/currency screen — setting Brasil / Real")
            tac.select_dropdown(page, [
                lambda p: p.get_by_role("combobox", name="País/região"),
                lambda p: p.get_by_text("País/região", exact=False),
            ], tac.COUNTRY, "país")
            page.wait_for_timeout(700)
            tac.select_dropdown(page, [
                lambda p: p.get_by_role("combobox", name="Moeda"),
                lambda p: p.get_by_text("Moeda", exact=False),
            ], tac.CURRENCY, "moeda")
            page.wait_for_timeout(700)
            tac.click_first(page, [
                lambda p: p.get_by_role("button", name="Avançar"),
                lambda p: p.get_by_role("button", name="Continuar"),
            ], "Avançar (location)", timeout=8000)
            page.wait_for_timeout(2000)

        tac.click_first(page, [
            lambda p: p.get_by_text("Cartão de crédito ou débito", exact=False),
            lambda p: p.get_by_text("Credit or debit card", exact=False),
            lambda p: p.get_by_text("Cartão de crédito", exact=False),
        ], "Credit/debit card option", timeout=8000)
        page.wait_for_timeout(4000)
        SHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOT_DIR / "api_before_mint.png"))

        # ---- resolve the BM/business account that owns this WABA account ----
        if CARD_PAYMENT_ACCOUNT_ID:
            acct_id = CARD_PAYMENT_ACCOUNT_ID
            print(f"[api] using CARD_PAYMENT_ACCOUNT_ID override: {acct_id}")
        else:
            print(f"[api] resolving BM account that owns WABA {_WABA_ACCOUNT}…")
            res = page.evaluate(RESOLVE_BM_JS, {"wabaAccount": _WABA_ACCOUNT})
            print(f"[api]   owner_business_payment_account = {res.get('bm')}  "
                  f"(self payment_account = {res.get('self')})")
            acct_id = res.get("bm") or _WABA_ACCOUNT
            if not res.get("bm"):
                print(f"[api]   !! BM account not found — body: {res.get('body')}")
                print(f"[api]   falling back to WABA account: {acct_id}")

        print(f"[api] minting platform_trust_token via FB JS + firing save (acct={acct_id})…")
        result = page.evaluate(MINT_AND_SAVE_JS, {
            "card": card,
            "paymentAccountID": acct_id,
            "docId": "25934943219457748",
            "friendly": "BillingSaveCardCredentialStateMutation",
            "businessId": tac.BUSINESS_ID,
        })

        print("\n" + "=" * 70)
        print("[api] RESULT:")
        print(json.dumps(result, indent=2)[:1600])
        print("=" * 70)

        if result.get("ok"):
            print(f"\n[api] ✅ PHASE 1 (API) succeeded — credential_id={result.get('credential_id')}")
            if tac.DO_ATTACH_TO_WABA and result.get("credential_id"):
                print(f"[api] attaching to WABA {tac.WABA_LEGACY_ACCOUNT_ID}…")
                attach = tac.attach_card_to_waba(
                    page, result["credential_id"], tac.WABA_LEGACY_ACCOUNT_ID, tac.BUSINESS_ID)
                print("[api] attach:", json.dumps(attach)[:400])
        else:
            print(f"\n[api] ❌ failed at stage={result.get('stage')} — see body/error above.")

        if not tac.HEADLESS:
            page.wait_for_timeout(15_000)
        browser.close()


if __name__ == "__main__":
    run()
