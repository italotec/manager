"""
Test: add a credit card to a WhatsApp/Business payment account via the live
Facebook Billing Hub UI, using a raw cookie string for auth.

WHY THIS IS UI-BASED (and not a pure `requests` replay)
-------------------------------------------------------
The HAR (`adc cartao.har`) shows the real "save card" mutation
(`BillingSaveCardCredentialStateMutation`) sends the PAN/CVV as:

    "credit_card_number": {"sensitive_string_value": "$e2ee"}
    "csc":                {"sensitive_string_value": "$e2ee"}

`$e2ee` is a placeholder. Facebook's payment SDK substitutes it AT SEND TIME with
a client-side-encrypted blob (ECIES against the EC P-256 public key returned by
`PaymentsCometGetServerEncryptionKeyMutation` -> `trust_chain` leaf cert), plus a
`platform_trust_token` device-attestation JWT. Neither the encryption envelope nor
the trust token can be reproduced by a standalone script, and the encryption key is
minted fresh per account/session. So the ONLY reliable way to add a card to another
account is to let Facebook's own in-page JS do the crypto — i.e. drive the real UI.

This script injects the provided cookies into a fresh Chromium context, opens the
billing hub, and walks the "Add payment method -> Credit/debit card" wizard,
screenshotting every step so you can see exactly where it succeeds or stops.

USAGE
-----
    py scripts/test_add_card.py

Edit the CONFIG block below (cookies, IDs, card details). Set HEADLESS=False to watch.
"""

from __future__ import annotations
import re
import sys
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------------------------------------------------------- #
# CONFIG — edit these                                                           #
# ----------------------------------------------------------------------------- #
COOKIES = (
    "datr=NvKxaemSQhTojQ6SZ4ESeKbl; sb=vNYdajkh6jx0rsW5PWYzip1C; ps_l=1; ps_n=1; "
    "c_user=61579689621059; "
    "presence=C%7B%22t3%22%3A%5B%5D%2C%22utc3%22%3A1780346018499%2C%22v%22%3A1%7D; "
    "fr=1bo0BvtY5Fo8cDiGe.AWfJFavlEKy0BHuUy_nb38zOtgHbP2tGmVnZu1xwP0zS32xJEnw.BqHjrz..AAA.0.0.BqHjrz.AWc0W__tmlZ3ErrqrxvHqGZrJHY; "
    "xs=34%3AQneLjz32ipnccA%3A2%3A1780340439%3A-1%3A-1%3A%3AAcw2Wh1GX6giRP79KTNTddiwvpfhXOsM-wGk0CgnaQ; "
    "wd=479x737; "
    'alsfid={"id":"fcacd79c2","timestamp":1780367937356.8}'
)

BUSINESS_ID = "1513112217151903"
PAYMENT_ACCOUNT_ID = "2082799432330638"
ASSET_ID = "2441091549713705"
EXTERNAL_FLOW_ID = "SU-1780367974922-1796362362-2003047987"
PLACEMENT = "whatsapp_ads"

# Phase 2 — the WABA payment account the BM card gets attached to
# (BillingSaveSharedBizCardStateMutation -> input.payment_legacy_account_id).
# Defaults to the URL's payment_account_id; override if the attach errors.
WABA_LEGACY_ACCOUNT_ID = PAYMENT_ACCOUNT_ID
# Set False to only add the card at BM level and skip the WABA attach.
DO_ATTACH_TO_WABA = True

# Card details to add (HARDCODE A TEST CARD HERE)
CARD_NUMBER = "5246742056663073"   # full PAN, no spaces
CARD_EXPIRY = "06/32"               # MM/YY
CARD_CSC = "686"                    # CVC/CVV
CARD_NAME = "Antonio Jacomini"      # cardholder name

# Location/currency screen — THIS IS PERMANENT for the account ("não poderão ser
# alteradas após serem definidas"). For a BR card you want Brasil + Real brasileiro.
COUNTRY = "Brasil"                  # País/região
CURRENCY = "Real brasileiro"        # Moeda  (matches "Real brasileiro (BRL)")

HEADLESS = False        # False = watch the browser do it
SLOWMO_MS = 120         # slow each action so you can follow along
SHOT_DIR = Path(__file__).parent / "_card_test_shots"

# Entry point = the payment_methods page (NOT accounts/details).
BILLING_URL = (
    f"https://business.facebook.com/latest/billing_hub/payment_methods/"
    f"?business_id={BUSINESS_ID}&asset_id={ASSET_ID}"
    f"&placement={PLACEMENT}&payment_account_id={PAYMENT_ACCOUNT_ID}"
)

# ----------------------------------------------------------------------------- #
# Helpers                                                                        #
# ----------------------------------------------------------------------------- #
_step = 0


def shot(page, label: str):
    global _step
    _step += 1
    SHOT_DIR.mkdir(exist_ok=True)
    p = SHOT_DIR / f"{_step:02d}_{label}.png"
    try:
        page.screenshot(path=str(p), full_page=False)
        print(f"   [shot] {p.name}")
    except Exception as ex:
        print(f"   [shot failed] {ex}")


def log(msg: str):
    print(f"[card-test] {msg}", flush=True)


def inject_cookies(ctx, cookies_str: str):
    cookie_list = []
    for part in cookies_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookie_list.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".facebook.com",
            "path": "/",
        })
    ctx.add_cookies(cookie_list)
    log(f"injected {len(cookie_list)} cookies")


def click_first(page, selectors, what: str, timeout=8000) -> bool:
    """Try a list of locators; click the first visible match. Returns True on click."""
    for sel in selectors:
        try:
            loc = sel(page) if callable(sel) else page.locator(sel)
            loc = loc.first
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()
            log(f"clicked {what}  via  {sel if isinstance(sel, str) else '<callable>'}")
            return True
        except Exception:
            continue
    log(f"!! could not find/click: {what}")
    return False


def fill_first(page, selectors, value, what: str, timeout=8000) -> bool:
    for sel in selectors:
        try:
            loc = sel(page) if callable(sel) else page.locator(sel)
            loc = loc.first
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()
            # press_sequentially — React inputs ignore .fill()/.value=
            loc.press_sequentially(value, delay=60)
            log(f"filled {what}")
            return True
        except Exception:
            continue
    log(f"!! could not find/fill: {what}")
    return False


def select_dropdown(page, trigger_selectors, option_text, what: str, timeout=8000) -> bool:
    """FB billing dropdowns: click the trigger to open the listbox, optionally type
    into the search box, then click the option whose text matches `option_text`.
    Falls back to native <select> select_option if present."""
    # 1) try native <select> first (rare here, but cheap)
    for sel in trigger_selectors:
        if not isinstance(sel, str):
            continue
        try:
            loc = page.locator(sel).first
            if loc.evaluate("el => el.tagName.toLowerCase()") == "select":
                loc.select_option(label=option_text)
                log(f"selected {what} = {option_text} (native select)")
                return True
        except Exception:
            continue
    # 2) custom combobox: open -> (search) -> click option
    for sel in trigger_selectors:
        try:
            trig = (sel(page) if callable(sel) else page.locator(sel)).first
            trig.wait_for(state="visible", timeout=timeout)
            trig.click()
            page.wait_for_timeout(600)
            # type into a search box if one appeared
            try:
                box = page.locator(
                    'input[type="text"]:visible, input[type="search"]:visible, '
                    'input[role="combobox"]:visible'
                ).last
                if box.is_visible(timeout=1500):
                    box.fill("")
                    box.press_sequentially(option_text, delay=50)
                    page.wait_for_timeout(800)
            except Exception:
                pass
            # click the matching option
            opt = page.get_by_role("option", name=option_text, exact=False).first
            try:
                opt.wait_for(state="visible", timeout=3000)
            except Exception:
                opt = page.get_by_text(option_text, exact=False).first
            opt.click()
            log(f"selected {what} = {option_text}")
            return True
        except Exception:
            continue
    log(f"!! could not select {what} = {option_text}")
    return False


def attach_card_to_waba(page, credential_id: str, waba_legacy_account_id: str,
                        business_id: str) -> dict:
    """PHASE 2: attach an already-saved BM card to the WABA payment account via the
    internal GraphQL `BillingSaveSharedBizCardStateMutation`. No card encryption is
    involved — just IDs — so we fire it straight from the live page (cookies auto-
    attach; fb_dtsg/lsd/uid are minted live). Mirrors HAR entry 212."""
    sess = f"upl_wizard_{int(time.time() * 1000)}_{uuid.uuid4()}"
    flow = f"upl_{int(time.time())}_{uuid.uuid4()}"
    variables = {
        "input": {
            "payment_legacy_account_id": waba_legacy_account_id,
            "shared_biz_credential_id": credential_id,
            "upl_logging_data": {
                "billing_notification_id": "",
                "context": "billingaddpm",
                "credential_id": credential_id,
                "credential_type": "CREDIT_CARD",
                "entry_point": "BILLING_HUB",
                "external_flow_id": flow,
                "user_session_id": flow,
                "business_id": business_id,
                "wizard_config_name": "SELECT_PAYMENT_METHOD",
                "wizard_name": "ADD_PM",
                "wizard_screen_name": "bm_payment_methods_state_display",
                "wizard_session_id": sess,
            },
            "client_mutation_id": "1",
        },
        "includeCreateNewFromOldFragment": False,
    }

    js = """async (vars) => {
      let dtsg="", lsd="", uid="";
      try { dtsg = require("DTSGInitialData").token; } catch(_) {}
      try { lsd  = require("LSD").token; } catch(_) {}
      try { uid  = require("CurrentUserInitialData").USER_ID; } catch(_) {}
      if (!dtsg) { const i=document.querySelector('input[name="fb_dtsg"]'); if(i) dtsg=i.value; }
      if (!dtsg) { const m=document.documentElement.innerHTML.match(/"DTSGInitialData",\\[\\],\\{"token":"(.*?)"/); if(m) dtsg=m[1]; }
      if (!lsd)  { const m=document.documentElement.innerHTML.match(/"LSD",\\[\\],\\{"token":"(.*?)"/); if(m) lsd=m[1]; }
      if (!uid)  { const m=document.cookie.match(/c_user=(\\d+)/); if(m) uid=m[1]; }
      vars.input.actor_id = uid;
      const charSum = Array.from(dtsg).reduce((s,c)=>s+c.charCodeAt(0),0);
      const jazoest = "2" + (charSum + 50);
      const body = new URLSearchParams({
        av: uid, __user: uid, __a: "1", fb_dtsg: dtsg, jazoest, lsd,
        __comet_req: "15", fb_api_caller_class: "RelayModern",
        fb_api_req_friendly_name: "BillingSaveSharedBizCardStateMutation",
        server_timestamps: "true",
        variables: JSON.stringify(vars),
        doc_id: "25126279877041501",
      });
      const r = await fetch("/api/graphql/", {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded",
                   "x-fb-lsd": lsd,
                   "x-fb-friendly-name": "BillingSaveSharedBizCardStateMutation" },
        body: body.toString(),
      });
      const t = await r.text();
      let p = {}; try { p = JSON.parse(t); } catch(_) {}
      const ok = !p.errors &&
        !!(p.data && p.data.xfb_billing_save_shared_biz_card);
      return { ok, status: r.status, body: t.slice(0, 400) };
    }"""
    try:
        return page.evaluate(js, variables)
    except Exception as ex:
        return {"ok": False, "status": "exception", "body": str(ex)}


# ----------------------------------------------------------------------------- #
# Main flow                                                                      #
# ----------------------------------------------------------------------------- #
def run():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOWMO_MS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            locale="pt-BR",
            viewport={"width": 1440, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        inject_cookies(ctx, COOKIES)
        page = ctx.new_page()

        log(f"navigating to billing hub:\n   {BILLING_URL}")
        page.goto(BILLING_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)
        shot(page, "billing_hub_loaded")

        # sanity: are we logged in as the right user?
        uid = page.evaluate(
            "() => { const m = document.cookie.match(/c_user=(\\d+)/); return m ? m[1] : null; }"
        )
        log(f"session c_user = {uid}  (expected {[c.split('=')[1] for c in COOKIES.split(';') if 'c_user' in c][0].strip()})")
        if uid is None:
            log("!! not logged in — cookies rejected or expired. Stopping.")
            shot(page, "NOT_LOGGED_IN")
            browser.close()
            return

        # --- Step 1: open "Add payment method" -------------------------------- #
        log("Step 1: open 'Add payment method'")
        click_first(
            page,
            [
                lambda p: p.get_by_role("button", name="Adicionar forma de pagamento"),
                lambda p: p.get_by_role("button", name="Add payment method"),
                lambda p: p.get_by_text("Adicionar forma de pagamento", exact=False),
                lambda p: p.get_by_text("Add payment method", exact=False),
                '[aria-label*="Adicionar forma de pagamento"]',
                '[aria-label*="Add payment method"]',
                'div[role="button"]:has-text("Adicionar")',
            ],
            "Add payment method",
            timeout=15000,
        )
        page.wait_for_timeout(2500)
        shot(page, "after_add_payment_method")

        # --- Step 1b: location & currency screen (PERMANENT) ------------------ #
        # Modal "Adicionar dados de pagamento -> Selecione a localização e a moeda".
        # Only appears for accounts with no country/currency set yet. Skip if absent.
        loc_modal = page.get_by_text("Selecione a localização e a moeda", exact=False)
        if loc_modal.count() and loc_modal.first.is_visible():
            log("Step 1b: location/currency screen detected — setting country/currency")
            select_dropdown(
                page,
                [
                    lambda p: p.get_by_role("combobox", name="País/região"),
                    lambda p: p.locator('label:has-text("País/região")'),
                    lambda p: p.get_by_text("País/região", exact=False),
                    'div[role="button"]:has-text("País/região")',
                ],
                COUNTRY, "país/região",
            )
            page.wait_for_timeout(800)
            select_dropdown(
                page,
                [
                    lambda p: p.get_by_role("combobox", name="Moeda"),
                    lambda p: p.locator('label:has-text("Moeda")'),
                    lambda p: p.get_by_text("Moeda", exact=False),
                    'div[role="button"]:has-text("Moeda")',
                ],
                CURRENCY, "moeda",
            )
            page.wait_for_timeout(800)
            shot(page, "location_currency_set")
            click_first(
                page,
                [
                    lambda p: p.get_by_role("button", name="Avançar"),
                    lambda p: p.get_by_role("button", name="Continuar"),
                    lambda p: p.get_by_role("button", name="Next"),
                ],
                "Avançar (location/currency)",
                timeout=10000,
            )
            page.wait_for_timeout(2500)
            shot(page, "after_location_currency")
        else:
            log("Step 1b: no location/currency screen (already set) — continuing")

        # --- Step 2: choose Credit / debit card ------------------------------- #
        log("Step 2: choose 'Credit/debit card'")
        click_first(
            page,
            [
                lambda p: p.get_by_text("Cartão de crédito ou débito", exact=False),
                lambda p: p.get_by_text("Credit or debit card", exact=False),
                lambda p: p.get_by_text("Cartão de crédito", exact=False),
                lambda p: p.get_by_role("radio", name="Cartão de crédito ou débito"),
                '[aria-label*="Cartão de crédito"]',
                'div[role="button"]:has-text("Cartão de crédito")',
            ],
            "Credit/debit card option",
            timeout=10000,
        )
        page.wait_for_timeout(1500)
        # some flows need a "Next/Continue" after picking the type
        click_first(
            page,
            [
                lambda p: p.get_by_role("button", name="Avançar"),
                lambda p: p.get_by_role("button", name="Continuar"),
                lambda p: p.get_by_role("button", name="Next"),
                lambda p: p.get_by_role("button", name="Continue"),
            ],
            "Next after card type (optional)",
            timeout=5000,
        )
        page.wait_for_timeout(2500)
        shot(page, "card_form")

        # --- Step 3: fill card fields ----------------------------------------- #
        # Real form (verified k1cvmnwe billing): inputs use pt-BR PLACEHOLDERS, and
        # NAME is the first field. Lead with get_by_placeholder; keep old fallbacks.
        log("Step 3: fill card fields")
        fill_first(
            page,
            [
                lambda p: p.get_by_placeholder("Número do cartão"),
                'input[autocomplete="cc-number"]',
                'input[name="cardNumber"]',
                'input[aria-label*="Número do cartão"]',
                'input[aria-label*="Card number"]',
            ],
            CARD_NUMBER,
            "card number",
            timeout=4000,
        )
        fill_first(
            page,
            [
                lambda p: p.get_by_placeholder("MM/AA"),
                lambda p: p.get_by_placeholder("MM/YY"),
                'input[autocomplete="cc-exp"]',
                'input[name="expiry"]',
                'input[placeholder*="MM"]',
                'input[aria-label*="Validade"]',
            ],
            CARD_EXPIRY,
            "expiry",
            timeout=4000,
        )
        fill_first(
            page,
            [
                lambda p: p.get_by_placeholder("Código de segurança"),
                lambda p: p.get_by_placeholder("Security code"),
                'input[autocomplete="cc-csc"]',
                'input[name="csc"]',
                'input[name="cvv"]',
                'input[aria-label*="CVV"]',
            ],
            CARD_CSC,
            "CSC/CVV",
            timeout=4000,
        )
        fill_first(
            page,
            [
                lambda p: p.get_by_placeholder("Nome no cartão"),
                lambda p: p.get_by_placeholder("Name on card"),
                'input[autocomplete="cc-name"]',
                'input[name="cardHolderName"]',
                'input[aria-label*="Nome no cartão"]',
            ],
            CARD_NAME,
            "cardholder name",
            timeout=4000,
        )
        shot(page, "card_form_filled")

        # --- Step 4: submit (PHASE 1 — save card at BM level) ----------------- #
        log("Step 4: submit card (phase 1 — save at BM level)")
        # Listen for the save mutation response so we can grab the credential_id.
        result = {"saved": None, "body": "", "credential_id": None}

        def on_response(resp):
            try:
                if "/api/graphql" in resp.url:
                    fn = resp.request.headers.get("x-fb-friendly-name", "")
                    if fn == "BillingSaveCardCredentialStateMutation":
                        body = resp.text()
                        result["body"] = body[:800]
                        result["saved"] = (
                            "xfb_billing_save_card_credential" in body
                            and '"errors"' not in body
                        )
                        m = re.search(r'"credential_id":"(\d+)"', body)
                        if m:
                            result["credential_id"] = m.group(1)
                        log(f"<< save mutation responded "
                            f"(saved={result['saved']}, credential_id={result['credential_id']})")
            except Exception:
                pass

        page.on("response", on_response)

        click_first(
            page,
            [
                lambda p: p.get_by_role("button", name="Salvar"),
                lambda p: p.get_by_role("button", name="Save"),
                lambda p: p.get_by_role("button", name="Avançar"),
                lambda p: p.get_by_role("button", name="Continuar"),
                lambda p: p.get_by_role("button", name="Adicionar"),
                'div[role="button"]:has-text("Salvar")',
            ],
            "submit/save card",
            timeout=10000,
        )

        # wait for the mutation to come back
        deadline = time.time() + 25
        while result["saved"] is None and time.time() < deadline:
            page.wait_for_timeout(500)
        page.wait_for_timeout(2000)
        shot(page, "after_submit")

        if result["saved"] is not True:
            print("\n" + "=" * 70)
            if result["saved"] is False:
                log("PHASE 1 ❌ save mutation returned an error — not attaching.")
                print(result["body"])
            else:
                log("PHASE 1 ⚠️ could not confirm card save — check screenshots.")
            print("=" * 70)
            if not HEADLESS:
                page.wait_for_timeout(20_000)
            browser.close()
            return

        log(f"PHASE 1 ✅ card saved at BM level (credential_id={result['credential_id']})")

        # --- PHASE 2 — attach the BM card to the WABA account ----------------- #
        if not DO_ATTACH_TO_WABA:
            log("DO_ATTACH_TO_WABA=False — skipping WABA attach.")
        elif not result["credential_id"]:
            log("PHASE 2 ⚠️ no credential_id captured from the save response — cannot attach.")
        else:
            log(f"Step 5: attach card to WABA account {WABA_LEGACY_ACCOUNT_ID} (phase 2)")
            attach = attach_card_to_waba(
                page, result["credential_id"], WABA_LEGACY_ACCOUNT_ID, BUSINESS_ID
            )
            shot(page, "after_attach")
            print("\n" + "=" * 70)
            if attach.get("ok"):
                log("PHASE 2 ✅ card attached to WABA account "
                    "(BillingSaveSharedBizCardStateMutation OK)")
            else:
                log("PHASE 2 ❌ attach failed")
                log(f"   status={attach.get('status')}  body={attach.get('body')}")
            print("=" * 70)

        if not HEADLESS:
            log("leaving browser open 20s for inspection...")
            page.wait_for_timeout(20_000)
        browser.close()


if __name__ == "__main__":
    try:
        run()
    except PWTimeout as ex:
        print(f"[card-test] TIMEOUT: {ex}")
        sys.exit(1)
