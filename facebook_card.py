# -*- coding: utf-8 -*-
"""
facebook_card.py — add a credit card to a WABA's Facebook billing, over an
already-logged-in, CDP-attached Playwright `page`.

This is the production port of scripts/test_add_card_api.py (VERIFIED WORKING):
instead of a fresh Chromium + injected cookies, it drives the live AdsPower
profile page that agent_core has already attached to. The card data goes
straight into Facebook's own in-page crypto (BillingPTTUtils.generatePTT) — no
DOM form typing — then the save + attach GraphQL mutations are fired with fetch.

Entry point:
    add_card_via_cdp(page, card, business_id, log) -> dict
        card = {"number","exp_month","exp_year","csc","name"}
        returns {"ok":bool, "code":int|None, "credential_id":str|None,
                 "stage":str, "error":str}

See docs/billing_card_e2ee.md for the full reverse-engineering.
"""
from __future__ import annotations

import re

# ── GraphQL doc_ids / friendly names (from the verified HAR) ──────────────────
_RESOLVE_BM_DOC_ID   = "36360602320204776"
_SAVE_DOC_ID         = "25934943219457748"
_SAVE_FRIENDLY       = "BillingSaveCardCredentialStateMutation"
_ATTACH_DOC_ID       = "25126279877041501"
_DECISION_DOC_ID     = "24327651843543916"
_SET_LOCATION_DOC_ID = "33020210320959428"


# ── Resolve the BM/business payment account that OWNS a WABA payment account ──
RESOLVE_BM_JS = r"""
async ({wabaAccount, docId}) => {
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
    variables: JSON.stringify(variables), doc_id: docId,
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

# ── Mint platform_trust_token via FB's own crypto, then fire the save mutation ─
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
      currency: "USD",
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
    body: t.slice(0, 800), ...log,
  };
}
"""

# ── Attach an already-saved BM card to the WABA payment account (no crypto) ────
ATTACH_JS = r"""
async ({credentialId, wabaLegacyAccountId, businessId, docId}) => {
  let dtsg="", lsd="", uid="";
  try { dtsg = require("DTSGInitialData").token; } catch(_) {}
  try { lsd  = require("LSD").token; } catch(_) {}
  try { uid  = require("CurrentUserInitialData").USER_ID; } catch(_) {}
  if (!dtsg) { const i=document.querySelector('input[name="fb_dtsg"]'); if(i) dtsg=i.value; }
  if (!uid)  { const m=document.cookie.match(/c_user=(\d+)/); if(m) uid=m[1]; }
  const sess = "upl_wizard_" + Date.now();
  const flow = "upl_" + Math.floor(Date.now()/1000);
  const variables = {
    input: {
      payment_legacy_account_id: wabaLegacyAccountId,
      shared_biz_credential_id: credentialId,
      upl_logging_data: {
        billing_notification_id: "", context: "billingaddpm",
        credential_id: credentialId, credential_type: "CREDIT_CARD",
        entry_point: "BILLING_HUB", external_flow_id: flow, user_session_id: flow,
        business_id: businessId, wizard_config_name: "SELECT_PAYMENT_METHOD",
        wizard_name: "ADD_PM", wizard_screen_name: "bm_payment_methods_state_display",
        wizard_session_id: sess,
      },
      actor_id: uid,
      client_mutation_id: "1",
    },
    includeCreateNewFromOldFragment: false,
  };
  const charSum = Array.from(dtsg).reduce((s,c)=>s+c.charCodeAt(0),0);
  const jazoest = "2" + (charSum + 50);
  const body = new URLSearchParams({
    av: uid, __user: uid, __a: "1", fb_dtsg: dtsg, jazoest, lsd,
    __comet_req: "15", fb_api_caller_class: "RelayModern",
    fb_api_req_friendly_name: "BillingSaveSharedBizCardStateMutation",
    server_timestamps: "true", variables: JSON.stringify(variables), doc_id: docId,
  });
  const r = await fetch("/api/graphql/", {
    method: "POST",
    headers: { "content-type":"application/x-www-form-urlencoded", "x-fb-lsd": lsd,
               "x-fb-friendly-name": "BillingSaveSharedBizCardStateMutation" },
    body: body.toString(),
  });
  const t = await r.text();
  let p = {}; try { p = JSON.parse(t); } catch(_) {}
  const ok = !p.errors && !!(p.data && p.data.xfb_billing_save_shared_biz_card);
  return { ok, status: r.status, body: t.slice(0, 400) };
}
"""


# ── Detect + set timezone/country/currency on the WABA billing account ────────
# Called AFTER card is saved at the BM level, BEFORE attaching to the WABA.
# Source: setar fuso e moeda.har — BillingCountryCurrencyDecisionStateQuery +
#         BillingAccountInformationUtilsUpdateAccountMutation.
SET_LOCATION_CURRENCY_JS = r"""
async ({wabaAccount, decisionDocId, setDocId}) => {
  let dtsg="", lsd="", uid="";
  try { dtsg = require("DTSGInitialData").token; } catch(_) {}
  try { lsd  = require("LSD").token; } catch(_) {}
  try { uid  = require("CurrentUserInitialData").USER_ID; } catch(_) {}

  function makeBody(extra) {
    const charSum = Array.from(dtsg).reduce((s,c)=>s+c.charCodeAt(0),0);
    const jazoest = "2" + (charSum + 50);
    return new URLSearchParams({
      av: uid, __user: uid, __a: "1", fb_dtsg: dtsg, jazoest, lsd,
      __comet_req: "15", fb_api_caller_class: "RelayModern",
      server_timestamps: "true", ...extra,
    });
  }

  // ── 1. Check whether location/currency is already set ─────────────────────
  let needsSet = true;
  try {
    const decBody = makeBody({
      fb_api_req_friendly_name: "BillingCountryCurrencyDecisionStateQuery",
      variables: JSON.stringify({paymentAccountID: wabaAccount}),
      doc_id: decisionDocId,
    });
    const decR = await fetch("/api/graphql/", {
      method: "POST",
      headers: {"content-type":"application/x-www-form-urlencoded","x-fb-lsd":lsd,
                "x-fb-friendly-name":"BillingCountryCurrencyDecisionStateQuery"},
      body: decBody.toString(),
    });
    const decT = await decR.text();
    let decP = {}; try { decP = JSON.parse(decT); } catch(_) {}
    const ba = ((decP.data || {}).payment_account || {}).billable_account || {};
    if (ba.currency && ba.currency !== null) {
      return {ok: true, skipped: true, reason: "already set: " + ba.currency};
    }
    if (ba.can_update_currency_timezone === false) {
      return {ok: true, skipped: true, reason: "can_update_currency_timezone=false"};
    }
  } catch(e) {
    return {ok: false, stage: "location_check", error: String(e)};
  }

  // ── 2. Set BR country / America/Sao_Paulo timezone / USD currency ──────────
  const now = Date.now();
  const flow = "upl_" + Math.floor(now/1000) + "_" + Math.random().toString(36).slice(2);
  const sess = "upl_wizard_" + now + "_" + Math.random().toString(36).slice(2);

  const variables = {
    input: {
      billable_account_payment_legacy_account_id: wabaAccount,
      currency: "USD",
      device_country: null,
      tax: {business_address: {country_code: "BR"}},
      timezone: "America/Sao_Paulo",
      upl_logging_data: {
        billing_notification_id: "",
        context: "billingaddpm",
        entry_point: "BILLING_HUB",
        external_flow_id: flow,
        user_session_id: flow,
        target_name: "BillingAccountInformationUtilsUpdateAccountMutation",
        wizard_config_name: "ADD_PM",
        wizard_name: "ADD_PM",
        wizard_screen_name: "country_currency_state_display",
        wizard_session_id: sess,
      },
      actor_id: uid,
      client_mutation_id: "5",
    },
    includeCreateNewFromOldFragment: false,
  };

  try {
    const setBody = makeBody({
      fb_api_req_friendly_name: "BillingAccountInformationUtilsUpdateAccountMutation",
      variables: JSON.stringify(variables),
      doc_id: setDocId,
    });
    const setR = await fetch("/api/graphql/", {
      method: "POST",
      headers: {"content-type":"application/x-www-form-urlencoded","x-fb-lsd":lsd,
                "x-fb-friendly-name":"BillingAccountInformationUtilsUpdateAccountMutation"},
      body: setBody.toString(),
    });
    const setT = await setR.text();
    let setP = {}; try { setP = JSON.parse(setT); } catch(_) {}
    if (setP.errors) {
      const summary = (setP.errors[0] || {}).summary || setT.slice(0, 200);
      return {ok: false, stage: "location_set", error: summary};
    }
    if (!(setP.data && setP.data.billable_account_update)) {
      return {ok: false, stage: "location_set", error: "mutation returned no data: " + setT.slice(0,200)};
    }
    return {ok: true, skipped: false};
  } catch(e) {
    return {ok: false, stage: "location_set", error: String(e)};
  }
}
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_card(card: dict) -> dict:
    num = re.sub(r"\D", "", str(card.get("number", "")))
    mm  = str(card.get("exp_month", "")).strip()
    yy  = str(card.get("exp_year", "")).strip()
    month = str(int(mm)) if mm.isdigit() else mm          # "06" -> "6"
    year  = yy if len(yy) == 4 else ("20" + yy if len(yy) == 2 else yy)
    return {
        "number": num, "csc": str(card.get("csc", "")), "name": card.get("name", ""),
        "exp_month": month, "exp_year": year,
        "bin": num[:8], "last_4": num[-4:],
    }


def _click_first(page, getters, timeout=8000) -> bool:
    for g in getters:
        try:
            loc = g(page).first
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()
            return True
        except Exception:
            continue
    return False


def _parse_fb_error(body: str) -> tuple[int | None, str]:
    """Extract (code, summary) from a FB GraphQL error body."""
    code = None
    m = re.search(r'"code":(\d+)', body or "")
    if m:
        try:
            code = int(m.group(1))
        except Exception:
            code = None
    summary = ""
    ms = re.search(r'"summary":"([^"]+)"', body or "")
    if ms:
        # decode the \uXXXX escapes FB uses for accented chars
        try:
            summary = ms.group(1).encode().decode("unicode_escape")
        except Exception:
            summary = ms.group(1)
    return code, summary


# ── Main entry ──────────────────────────────────────────────────────────────

def add_card_via_cdp(page, card: dict, business_id: str, waba_id: str = "", log=print) -> dict:
    """Navigate to the WABA billing hub, resolve payment accounts, mint PTT,
    save the card at BM level, and attach it to the WABA.

    waba_id (asset_id) lets FB auto-redirect with payment_account_id in the URL,
    which we extract directly — no wizard-sniffing required for the WABA account.
    Returns {ok, code, credential_id, stage, error}.
    """
    c = _parse_card(card)
    log(f"[CARD] parsed bin={c['bin']} last4={c['last_4']} exp={c['exp_month']}/{c['exp_year']}")

    try:
        # ── Step 1: Navigate to the WABA billing page ─────────────────────────
        # With asset_id, FB auto-redirects and appends payment_account_id to the
        # URL — that IS the WABA payment account, no request-sniffing needed.
        billing_url = (
            "https://business.facebook.com/latest/billing_hub/payment_methods/"
            f"?business_id={business_id}&placement=whatsapp_ads"
            + (f"&asset_id={waba_id}" if waba_id else "")
        )
        page.goto(billing_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            page.wait_for_timeout(4000)

        # ── Step 2: Extract WABA payment account from redirected URL ──────────
        waba_account = ""
        final_url = page.url()
        m_url = re.search(r'[?&]payment_account_id=(\d+)', final_url)
        if m_url:
            waba_account = m_url.group(1)
            log(f"[CARD] waba_account={waba_account} (from URL)")
        else:
            log(f"[CARD] no payment_account_id in URL: {final_url}")

        # ── Step 3: Resolve the BM payment account from the WABA account ──────
        bm_account = ""

        def _resolve_bm(acct: str) -> str:
            """Returns bm_account or '' on failure. Initializes billing first if needed."""
            try:
                res = page.evaluate(RESOLVE_BM_JS, {"wabaAccount": acct, "docId": _RESOLVE_BM_DOC_ID})
            except Exception as e:
                log(f"[CARD] resolve_bm error {acct}: {e}")
                return ""
            body_snip = str(res.get("body", ""))
            log(f"[CARD] resolve_bm {acct} → bm={res.get('bm')} body={body_snip[:150]}")
            if res.get("bm"):
                return res["bm"]
            # billable_account:null → billing not initialized yet; set location first
            if "billable_account" in body_snip and "null" in body_snip:
                log(f"[CARD] billable_account null — initializing billing for {acct}")
                try:
                    loc_pre = page.evaluate(SET_LOCATION_CURRENCY_JS, {
                        "wabaAccount": acct,
                        "decisionDocId": _DECISION_DOC_ID,
                        "setDocId": _SET_LOCATION_DOC_ID,
                    })
                    log(f"[CARD] billing init ok={loc_pre.get('ok')} "
                        f"skipped={loc_pre.get('skipped')} err={loc_pre.get('error','')}")
                except Exception as e:
                    log(f"[CARD] billing init error: {e}")
                try:
                    res2 = page.evaluate(RESOLVE_BM_JS, {"wabaAccount": acct, "docId": _RESOLVE_BM_DOC_ID})
                    log(f"[CARD] resolve_bm retry {acct} → bm={res2.get('bm')} "
                        f"body={str(res2.get('body',''))[:150]}")
                    return res2.get("bm") or ""
                except Exception as e:
                    log(f"[CARD] resolve_bm retry error: {e}")
            return ""

        if waba_account:
            bm_account = _resolve_bm(waba_account)

        if not bm_account:
            return {"ok": False, "stage": "resolve", "code": None, "credential_id": None,
                    "error": f"Não foi possível resolver a conta de pagamento (BM). "
                             f"waba_account={waba_account}"}

        log(f"[CARD] WABA={waba_account} → BM={bm_account}")

        # ── Step 4: Open billing wizard to load BillingPTTUtils crypto bundle ─
        # Click the BM-level "Adicionar"/"Add" button (short text, no data-testid).
        btn_opened = _click_first(page, [
            lambda p: p.get_by_role("button", name="Adicionar"),
            lambda p: p.get_by_role("button", name="Add"),
            lambda p: p.get_by_role("button", name=re.compile(r"^(Adicionar|Add|Agregar|Ajouter)$", re.I)),
        ], timeout=10000)
        log(f"[CARD] billing_btn_opened={btn_opened}")
        page.wait_for_timeout(1500)

        # Advance the dialog to the card form step (loads BillingPTTUtils).
        # The dialog's last button is always "Avançar/Next" — language-agnostic.
        try:
            dialog = page.locator('[role="dialog"][aria-modal="true"]')
            dialog.wait_for(state="visible", timeout=8000)
            dialog.locator('div[role="button"], button').last().click()
            page.wait_for_timeout(3000)
            log(f"[CARD] advanced to card form (BillingPTTUtils loaded)")
        except Exception as e:
            log(f"[CARD] dialog advance error (may still work): {e}")

        # ── Step 5: Mint PTT + save card at BM level ──────────────────────────
        save = page.evaluate(MINT_AND_SAVE_JS, {
            "card": c,
            "paymentAccountID": bm_account,
            "docId": _SAVE_DOC_ID,
            "friendly": _SAVE_FRIENDLY,
            "businessId": business_id,
        })
        log(f"[CARD] save ok={save.get('ok')} stage={save.get('stage')} "
            f"cred={save.get('credential_id')}")

        if not save.get("ok"):
            code, summary = _parse_fb_error(save.get("body", ""))
            err = summary or save.get("error", "") or "Falha ao salvar o cartão"
            return {"ok": False, "stage": save.get("stage", "save"), "code": code,
                    "credential_id": None, "error": err}

        credential_id = save.get("credential_id")
        if not credential_id:
            return {"ok": False, "stage": "save", "code": None, "credential_id": None,
                    "error": "Cartão salvo mas sem credential_id"}

        # ── Step 6: Set timezone/country/currency on the WABA account ─────────
        # Idempotent — skips if already configured (including when we set it in Step 3).
        loc = page.evaluate(SET_LOCATION_CURRENCY_JS, {
            "wabaAccount": waba_account,
            "decisionDocId": _DECISION_DOC_ID,
            "setDocId": _SET_LOCATION_DOC_ID,
        })
        log(f"[CARD] location ok={loc.get('ok')} skipped={loc.get('skipped')} "
            f"reason={loc.get('reason','')}")
        if not loc.get("ok"):
            return {"ok": False, "stage": loc.get("stage", "location"), "code": None,
                    "credential_id": credential_id,
                    "error": loc.get("error", "Falha ao definir país/moeda/fuso")}

        # ── Step 7: Attach the saved card to the WABA ─────────────────────────
        attach = page.evaluate(ATTACH_JS, {
            "credentialId": credential_id,
            "wabaLegacyAccountId": waba_account,
            "businessId": business_id,
            "docId": _ATTACH_DOC_ID,
        })
        log(f"[CARD] attach ok={attach.get('ok')} status={attach.get('status')}")

        if not attach.get("ok"):
            code, summary = _parse_fb_error(attach.get("body", ""))
            return {"ok": False, "stage": "attach", "code": code,
                    "credential_id": credential_id,
                    "error": summary or "Cartão salvo mas falha ao vincular à WABA"}

        return {"ok": True, "stage": "attach", "code": None,
                "credential_id": credential_id, "error": ""}

    except Exception as e:
        return {"ok": False, "stage": "exception", "code": None,
                "credential_id": None, "error": str(e)[:500]}
