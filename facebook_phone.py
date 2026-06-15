# -*- coding: utf-8 -*-
"""
facebook_phone.py — add a virtual phone number to a WABA via an already-logged-in,
CDP-attached Playwright `page`.

Mirrors facebook_card.py: entry point is add_phone_via_cdp(page, ...) → dict.

The mutation (`useWhatsAppBusinessVirtualNumberCreationMutation_PhoneProfileCreationMutation`,
doc_id 30579457621638005) was reverse-engineered from a HAR and verified live via Playwright MCP.

Key constraint discovered during testing:
  app_id MUST be "225181538219344" (Meta's WA-Business platform constant).
  Using the session's CurrentUserInitialData.app_id fails with noncoercible_variable_value.
"""
from __future__ import annotations

_FRIENDLY = "useWhatsAppBusinessVirtualNumberCreationMutation_PhoneProfileCreationMutation"
_DOC_ID   = "30579457621638005"
_APP_ID   = "225181538219344"  # Meta WA-Business platform constant — do NOT swap for session app_id

ADD_PHONE_JS = r"""
async ({wabaId, businessId, displayNameFallback}) => {
  const req = (n) => require(n);
  let dtsg, lsd, uid;
  try {
    dtsg = req("DTSGInitialData").token;
    lsd  = req("LSD").token;
    uid  = req("CurrentUserInitialData").USER_ID;
  } catch (e) {
    return {ok: false, stage: "auth", error: String(e)};
  }

  const charSum = Array.from(dtsg).reduce((s, c) => s + c.charCodeAt(0), 0);
  const jazoest = "2" + (charSum + 50);

  // Harvest display_name + business_profile from the Relay store
  let displayName = displayNameFallback || "";
  let profilePictureUrl = "";
  let websiteUrl = "";
  try {
    const relay = req("RelayFBEnvironment");
    const src   = relay && relay.getStore && relay.getStore().getSource();
    const get   = (id) => src && (src.get ? src.get(id) : (src.__records||{})[id]);

    const wabaNode = get(wabaId);
    if (wabaNode) {
      displayName      = wabaNode.name || displayName;
      profilePictureUrl = wabaNode.profile_picture_url || "";
    }

    // business profile is a ref hanging off the business node
    const bizNode = get(businessId);
    if (bizNode) {
      const bpRef = (bizNode.business_profile || {})["__ref"];
      if (bpRef) {
        const bp = get(bpRef);
        if (bp) {
          websiteUrl    = bp.website_url || "";
        }
      }
    }
  } catch (_) {}

  const businessProfile = {
    description: "",
    vertical_class: "APPAREL",
    website: websiteUrl,
    profile_picture_url: profilePictureUrl,
    websites: websiteUrl ? [websiteUrl] : [],
  };

  const variables = {
    input: {
      actor_id: uid,
      client_mutation_id: "1",
      log_session_id: "WBxP--" + Math.floor(Math.random()*1e9) + "-" + Math.floor(Math.random()*1e9),
      app_id: "225181538219344",
      waba_id: wabaId,
      onboarding_source: "MBS_SETTINGS",
      device_platform: "WEB",
      display_name: displayName,
      business_profile: businessProfile,
      uo_logging_context: null,
      business_id: businessId,
    }
  };

  const body = new URLSearchParams({
    av: uid, __user: uid, __a: "1",
    fb_dtsg: dtsg, jazoest, lsd,
    __comet_req: "11",
    fb_api_caller_class: "RelayModern",
    fb_api_req_friendly_name: "useWhatsAppBusinessVirtualNumberCreationMutation_PhoneProfileCreationMutation",
    server_timestamps: "true",
    variables: JSON.stringify(variables),
    doc_id: "30579457621638005",
  });

  let resp;
  try {
    resp = await fetch(
      "/api/graphql/?_callFlowletID=5912&_triggerFlowletID=5906&qpl_active_e2e_trace_ids=",
      {
        method: "POST",
        headers: {
          "content-type": "application/x-www-form-urlencoded",
          "x-fb-lsd": lsd,
          "x-fb-friendly-name": "useWhatsAppBusinessVirtualNumberCreationMutation_PhoneProfileCreationMutation",
          "x-asbd-id": "359341",
        },
        body: body.toString(),
      }
    );
  } catch (e) {
    return {ok: false, stage: "fetch", error: String(e)};
  }

  let text;
  try { text = await resp.text(); } catch (e) {
    return {ok: false, stage: "read", error: String(e)};
  }

  // FB sometimes prepends "for (;;);" as CSRF protection
  if (text.startsWith("for (;;);")) text = text.slice(9);

  let parsed;
  try { parsed = JSON.parse(text); } catch (e) {
    return {ok: false, stage: "parse", error: "JSON parse failed: " + text.slice(0, 200)};
  }

  if (parsed.errors && parsed.errors.length) {
    const e = parsed.errors[0];
    return {
      ok: false, stage: "graphql",
      error: e.summary || e.description || e.message || JSON.stringify(e),
    };
  }
  if (parsed.error) {
    return {ok: false, stage: "graphql", error: String(parsed.error) + " " + (parsed.errorSummary||"")};
  }

  const result = (parsed.data || {}).xfb_create_whatsapp_business_virtual_phone_number;
  if (!result) {
    return {ok: false, stage: "graphql", error: "No result field. Body: " + text.slice(0, 300)};
  }

  return {
    ok: Boolean(result.success),
    stage: "done",
    display_phone_number: result.display_phone_number || "",
    current_status_id: result.current_status_id || "",
    display_name: displayName,
    error: result.success ? "" : "success=false",
  };
}
"""


def add_phone_via_cdp(
    page,
    business_id: str,
    waba_id: str,
    display_name_fallback: str = "",
    log=print,
) -> dict:
    """Add a virtual phone number to a WABA via a live CDP-attached Playwright page.

    The page must already be logged in to business.facebook.com (opened via AdsPower).
    Returns {ok, display_phone_number, current_status_id, display_name, stage, error}.
    """
    url = (
        f"https://business.facebook.com/latest/settings/whatsapp_account/"
        f"?business_id={business_id}"
        f"&selected_asset_id={waba_id}"
        f"&selected_asset_type=whatsapp-business-account"
    )
    log(f"[PHONE {waba_id}] Navegando para WABA settings…")
    try:
        page.goto(url, timeout=45_000)
        page.wait_for_load_state("networkidle", timeout=25_000)
    except Exception as exc:
        return {"ok": False, "stage": "navigate", "error": str(exc)[:300]}

    log(f"[PHONE {waba_id}] Disparando mutação de número virtual…")
    try:
        result = page.evaluate(
            ADD_PHONE_JS,
            {
                "wabaId": waba_id,
                "businessId": business_id,
                "displayNameFallback": display_name_fallback,
            },
        )
    except Exception as exc:
        return {"ok": False, "stage": "evaluate", "error": str(exc)[:300]}

    ok = bool(result.get("ok"))
    phone = result.get("display_phone_number", "")
    log(
        f"[PHONE {waba_id}] ok={ok} phone={phone} "
        f"stage={result.get('stage')} err={result.get('error','')[:120]}"
    )
    return result
