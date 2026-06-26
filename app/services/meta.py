import time
import requests

def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

_RL_CODES = {4, 80007, 130429, 131056, 368}

def _get(url: str, token: str, max_retries: int = 3, backoff_base: float = 4.0, backoff_cap: float = 60.0):
    """GET with exponential-backoff retry on 429 / transient Meta errors."""
    attempt = 0
    while True:
        try:
            r = requests.get(url, headers=_auth_headers(token), timeout=30)
            txt = (r.text or "").strip()
            try:
                j = r.json()
            except Exception:
                j = None

            # Detect rate-limiting / transient errors
            rate_limited = False
            retry_after = 0

            if r.status_code == 429:
                rate_limited = True
                retry_after = int(r.headers.get("Retry-After", 0) or 0)
            elif isinstance(j, dict) and "error" in j:
                err = j["error"]
                if isinstance(err, dict):
                    code = err.get("code")
                    is_transient = bool(err.get("is_transient"))
                    msg = (err.get("message") or "").lower()
                    if code in _RL_CODES or is_transient or "rate limit" in msg or "too many" in msg:
                        rate_limited = True
                        retry_after = int(r.headers.get("Retry-After", 0) or 0)

            if rate_limited and attempt < max_retries:
                sleep = retry_after if retry_after > 0 else min(backoff_base ** attempt, backoff_cap)
                time.sleep(sleep)
                attempt += 1
                continue

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

def get_phone_messaging_limit(api_version: str, token: str, phone_id: str):
    url = f"https://graph.facebook.com/{api_version}/{phone_id}?fields=whatsapp_business_manager_messaging_limit"
    status, j, snippet = _get(url, token)
    if status != 200 or not isinstance(j, dict) or "error" in j:
        return None
    return j.get("whatsapp_business_manager_messaging_limit")

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


def create_template_rl(api_version: str, token: str, waba_id: str, payload: dict):
    """Like create_template but also signals rate-limiting.

    Returns (result, err, rate_limited, retry_after_seconds).
    retry_after_seconds is 0 when not rate-limited; caller should apply 4^retry_count backoff.
    """
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

        # Detect rate limiting
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", 0) or 0)
            return None, f"HTTP 429", True, retry_after

        if isinstance(j, dict) and "error" in j:
            err = j["error"]
            code = err.get("code") if isinstance(err, dict) else None
            is_transient = bool(err.get("is_transient")) if isinstance(err, dict) else False
            msg = (err.get("message") or "") if isinstance(err, dict) else str(err)
            if code in _RL_CODES or is_transient or "rate limit" in msg.lower() or "too many" in msg.lower():
                retry_after = int(r.headers.get("Retry-After", 0) or 0)
                return None, f"Meta error {code}: {msg[:300]}", True, retry_after
            return None, f"Meta error: {str(j.get('error'))[:800]}", False, 0

        if r.status_code not in (200, 201) or not isinstance(j, dict):
            return None, f"HTTP {r.status_code}: {(r.text or '')[:800]}", False, 0

        return j, None, False, 0
    except Exception as e:
        return None, str(e)[:800], False, 0


def templates_status_summary(templates: list[dict]) -> dict:
    out = {"APPROVED": 0, "PENDING": 0, "PAUSED": 0, "REJECTED": 0, "DISABLED": 0, "OTHER": 0}
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

def delete_phone_number(api_version: str, token: str, phone_id: str, proxy_str: str | None = None):
    """DELETE a phone number from its WABA. Returns (ok: bool, err: str | None)."""
    s = _session_with_proxy(proxy_str)
    url = f"https://graph.facebook.com/{api_version}/{phone_id}"
    try:
        r = s.delete(url, headers=_auth_headers(token), timeout=30)
        j = r.json() if r.text else {}
        if r.status_code == 200 and (j.get("success") or j == {}):
            return True, None
        err = j.get("error", {}) if isinstance(j, dict) else {}
        return False, err.get("message") or f"HTTP {r.status_code}: {(r.text or '')[:300]}"
    except Exception as e:
        return False, str(e)[:400]


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


# ── Profile picture helpers ────────────────────────────────────────────────

# Cache app_id per token to avoid repeated /app calls inside one job run.
_app_id_cache: dict[str, str] = {}

def get_app_id(api_version: str, token: str, fallback: str = "") -> str:
    """Return the Facebook App ID associated with this token.

    Tries GET /{ver}/app first (works for system-user tokens issued from the
    app). Falls back to the supplied `fallback` (i.e. META_APP_ID env var)
    when the call fails or the token has no app node.
    """
    if token in _app_id_cache:
        return _app_id_cache[token]
    try:
        url = f"https://graph.facebook.com/{api_version}/app"
        r = requests.get(url, headers=_auth_headers(token), timeout=15)
        j = r.json() if r.text else {}
        app_id = str(j.get("id") or "").strip()
    except Exception:
        app_id = ""
    result = app_id or fallback
    if result:
        _app_id_cache[token] = result
    return result


def upload_resumable(api_version: str, app_id: str, token: str,
                     file_bytes: bytes, mime: str = "image/jpeg") -> tuple[str, str | None]:
    """Upload bytes via the Resumable Upload API and return (handle, err).

    Two-step:
      1. POST /{ver}/{app_id}/uploads?file_length=...&file_type=... → upload session id
      2. POST /{ver}/{session_id} with raw bytes → file handle string
    """
    try:
        # Step 1: create upload session
        url1 = (
            f"https://graph.facebook.com/{api_version}/{app_id}/uploads"
            f"?file_length={len(file_bytes)}&file_type={mime}"
        )
        r1 = requests.post(url1, headers=_auth_headers(token), timeout=30)
        j1 = r1.json() if r1.text else {}
        session_id = str(j1.get("id") or "").strip()
        if not session_id or r1.status_code not in (200, 201):
            err = (j1.get("error") or {})
            return "", (err.get("message") if isinstance(err, dict) else str(err)) or f"HTTP {r1.status_code}"

        # Step 2: upload the bytes
        url2 = f"https://graph.facebook.com/{api_version}/{session_id}"
        headers2 = {
            "Authorization": f"OAuth {token}",
            "file_offset": "0",
            "Content-Type": mime,
        }
        r2 = requests.post(url2, headers=headers2, data=file_bytes, timeout=60)
        j2 = r2.json() if r2.text else {}
        handle = str(j2.get("h") or "").strip()
        if not handle or r2.status_code not in (200, 201):
            err = (j2.get("error") or {})
            return "", (err.get("message") if isinstance(err, dict) else str(err)) or f"HTTP {r2.status_code}"

        return handle, None
    except Exception as e:
        return "", str(e)[:400]


def set_profile_picture(api_version: str, token: str,
                        phone_id: str, handle: str) -> tuple[bool, str | None]:
    """POST whatsapp_business_profile to set profile_picture_handle for a phone number."""
    url = f"https://graph.facebook.com/{api_version}/{phone_id}/whatsapp_business_profile"
    payload = {"messaging_product": "whatsapp", "profile_picture_handle": handle}
    try:
        r = requests.post(url, headers={**_auth_headers(token), "Content-Type": "application/json"},
                          json=payload, timeout=30)
        j = r.json() if r.text else {}
        if r.status_code == 200 and (j.get("success") or j.get("id")):
            return True, None
        err = j.get("error", {}) if isinstance(j, dict) else {}
        msg = (err.get("message") if isinstance(err, dict) else str(err)) or f"HTTP {r.status_code}"
        return False, msg
    except Exception as e:
        return False, str(e)[:400]
