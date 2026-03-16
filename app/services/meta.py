import requests

def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

def _get(url: str, token: str):
    try:
        r = requests.get(url, headers=_auth_headers(token), timeout=30)
        txt = (r.text or "").strip()
        try:
            j = r.json()
        except Exception:
            j = None
        return r.status_code, j, txt[:800]
    except Exception as e:
        return None, None, str(e)[:800]

def subscribe_waba_webhook(api_version: str, token: str, waba_id: str):
    """Subscribe the app to webhook events for a WABA (POST /WABA-ID/subscribed_apps)."""
    url = f"https://graph.facebook.com/{api_version}/{waba_id}/subscribed_apps"
    try:
        r = requests.post(url, headers=_auth_headers(token), timeout=30)
        j = r.json() if r.text else {}
        if r.status_code == 200 and j.get("success"):
            return True, None
        err = j.get("error", {})
        return False, err.get("message") or f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)[:400]


def get_waba_info(api_version: str, token: str, waba_id: str):
    url = f"https://graph.facebook.com/{api_version}/{waba_id}"
    status, j, snippet = _get(url, token)
    if status != 200 or not isinstance(j, dict):
        return None, f"HTTP {status}: {snippet}"
    if "error" in j:
        return None, f"Meta error: {str(j.get('error'))[:800]}"
    return j, None

def get_waba_name(api_version: str, token: str, waba_id: str):
    info, err = get_waba_info(api_version, token, waba_id)
    if err:
        return None, err
    name = info.get("name")
    return name, None

def get_waba_analytics(api_version: str, token: str, waba_id: str,
                       start_ts: int, end_ts: int, granularity: str = "DAY"):
    """Fetch WABA analytics between two unix timestamps."""
    url = (
        f"https://graph.facebook.com/{api_version}/{waba_id}"
        f"?fields=analytics.start({start_ts}).end({end_ts}).granularity({granularity})"
    )
    status, j, snippet = _get(url, token)
    if status != 200 or not isinstance(j, dict):
        return None, f"HTTP {status}: {snippet}"
    if "error" in j:
        return None, f"Meta error: {str(j.get('error'))[:800]}"
    return j.get("analytics"), None


def get_phone_numbers(api_version: str, token: str, waba_id: str):
    url = (
        f"https://graph.facebook.com/{api_version}/{waba_id}/phone_numbers"
        f"?fields=id,display_phone_number,verified_name,quality_rating,status"
    )
    status, j, snippet = _get(url, token)
    if status != 200 or not isinstance(j, dict):
        return [], f"HTTP {status}: {snippet}"
    if "error" in j:
        return [], f"Meta error: {str(j.get('error'))[:800]}"
    return (j.get("data") or []), None


def get_phone_numbers_health(api_version: str, token: str, waba_id: str):
    """Fetch phone numbers with health_status fields."""
    url = (
        f"https://graph.facebook.com/{api_version}/{waba_id}/phone_numbers"
        f"?fields=id,is_official_business_account,display_phone_number,verified_name,status,health_status"
    )
    status, j, snippet = _get(url, token)
    if status != 200 or not isinstance(j, dict):
        return [], f"HTTP {status}: {snippet}"
    if "error" in j:
        return [], f"Meta error: {str(j.get('error'))[:800]}"
    return (j.get("data") or []), None


def evaluate_health(phones_data: list) -> str:
    """
    Analyze health_status from phone_numbers response.

    Hierarchy (first match wins):
        WABA blocked/banned   → "DESATIVADA"
        Payment method error  → "PROBLEMA CARTÃO"
        Phone/business limit  → "LIMITADA"
        Otherwise             → "OK"
    """
    waba_blocked = False
    payment_error = False
    phone_limited = False

    for phone in phones_data:
        hs = phone.get("health_status") or {}
        for entity in (hs.get("entities") or []):
            etype = (entity.get("entity_type") or "").upper()
            can_send = (entity.get("can_send_message") or "").upper()
            errors = entity.get("errors") or []
            error_descs = [e.get("error_description", "") for e in errors]

            if etype == "WABA":
                for desc in error_descs:
                    if "payment method" in desc.lower():
                        payment_error = True
                    if "WABA is banned" in desc:
                        waba_blocked = True
                if can_send == "BLOCKED" and not payment_error:
                    waba_blocked = True

            if etype == "PHONE_NUMBER":
                for desc in error_descs:
                    if "reached the limit" in desc:
                        phone_limited = True

            if etype == "BUSINESS":
                for desc in error_descs:
                    if "reached the limit" in desc:
                        phone_limited = True

    if waba_blocked:
        return "DESATIVADA"
    if payment_error:
        return "PROBLEMA CARTÃO"
    if phone_limited:
        return "LIMITADA"
    return "OK"

def pick_test_template(templates: list) -> dict | None:
    """Pick an APPROVED UTILITY template for testing. Returns the template dict or None."""
    for t in templates:
        if (t.get("status") or "").upper() == "APPROVED" and (t.get("category") or "").upper() == "UTILITY":
            return t
    # Fallback: any APPROVED template
    for t in templates:
        if (t.get("status") or "").upper() == "APPROVED":
            return t
    return None


def _count_body_vars(template: dict) -> int:
    """Count the number of {{N}} variables in the template BODY component."""
    import re
    for comp in (template.get("components") or []):
        if (comp.get("type") or "").upper() == "BODY":
            text = comp.get("text") or ""
            matches = re.findall(r"\{\{(\d+)\}\}", text)
            return len(set(matches))
    return 0


def send_test_message(token: str, phone_number_id: str, template: dict) -> tuple[bool, str]:
    """
    Send a test message to 5599999999 using the given template.
    Returns (success: bool, raw_response_text: str).
    """
    tpl_name = template.get("name", "")
    tpl_lang = template.get("language", "en")
    var_count = _count_body_vars(template)

    components = []
    if var_count > 0:
        components.append({
            "type": "body",
            "parameters": [
                {"type": "text", "text": "teste"}
                for _ in range(var_count)
            ],
        })

    payload = {
        "messaging_product": "whatsapp",
        "type": "template",
        "to": "5599999999",
        "template": {
            "name": tpl_name,
            "language": {"code": tpl_lang},
            "components": components,
        },
    }

    url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        return r.status_code == 200, (r.text or "")[:800]
    except Exception as e:
        return False, str(e)[:800]


def get_templates(api_version: str, token: str, waba_id: str):
    url = f"https://graph.facebook.com/{api_version}/{waba_id}/message_templates"
    status, j, snippet = _get(url, token)
    if status != 200 or not isinstance(j, dict):
        return [], f"HTTP {status}: {snippet}"
    if "error" in j:
        return [], f"Meta error: {str(j.get('error'))[:800]}"
    return (j.get("data") or []), None

def create_template(api_version: str, token: str, waba_id: str, payload: dict):
    url = f"https://graph.facebook.com/{api_version}/{waba_id}/message_templates"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        try:
            j = r.json()
        except Exception:
            j = None
        if r.status_code not in (200, 201) or not isinstance(j, dict):
            snippet = (r.text or "")[:800]
            return None, f"HTTP {r.status_code}: {snippet}"
        if "error" in j:
            return None, f"Meta error: {str(j.get('error'))[:800]}"
        return j, None
    except Exception as e:
        return None, str(e)[:800]


def templates_status_summary(templates: list[dict]) -> dict:
    out = {"APPROVED": 0, "PAUSED": 0, "DISABLED": 0, "OTHER": 0}
    for t in templates:
        st = (t.get("status") or "").upper()
        if st in out:
            out[st] += 1
        else:
            out["OTHER"] += 1
    return out

# --- functions used by add-phone flow (unchanged signatures) ---

def _session_with_proxy(proxy_url: str | None):
    """proxy_url: full URL like http://user:pass@ip:port or socks5://user:pass@ip:port"""
    s = requests.Session()
    if proxy_url:
        s.proxies.update({"http": proxy_url, "https": proxy_url})
    return s

def add_phone_number(api_version: str, token: str, waba_id: str, cc: str, local_number: str, verified_name: str, proxy_str: str | None):
    s = _session_with_proxy(proxy_str)
    url = f"https://graph.facebook.com/{api_version}/{waba_id}/phone_numbers"
    payload = {"cc": cc, "phone_number": local_number, "verified_name": verified_name}
    r = s.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30
    )
    return r

def request_code(api_version: str, token: str, phone_id: str, code_method: str, language: str, proxy_str: str | None):
    s = _session_with_proxy(proxy_str)
    url = f"https://graph.facebook.com/{api_version}/{phone_id}/request_code"
    payload = {"code_method": code_method, "language": language}
    r = s.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=30)
    return r

def verify_code(api_version: str, token: str, phone_id: str, code: str, proxy_str: str | None):
    s = _session_with_proxy(proxy_str)
    url = f"https://graph.facebook.com/{api_version}/{phone_id}/verify_code"
    payload = {"code": code}
    r = s.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=30)
    return r

def register_number(api_version: str, token: str, phone_id: str, pin: str, proxy_str: str | None):
    s = _session_with_proxy(proxy_str)
    url = f"https://graph.facebook.com/{api_version}/{phone_id}/register"
    payload = {"messaging_product": "whatsapp", "pin": pin}
    r = s.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=30)
    return r
