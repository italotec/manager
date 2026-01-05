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

def get_phone_numbers(api_version: str, token: str, waba_id: str):
    url = f"https://graph.facebook.com/{api_version}/{waba_id}/phone_numbers"
    status, j, snippet = _get(url, token)
    if status != 200 or not isinstance(j, dict):
        return [], f"HTTP {status}: {snippet}"
    if "error" in j:
        return [], f"Meta error: {str(j.get('error'))[:800]}"
    return (j.get("data") or []), None

def get_templates(api_version: str, token: str, waba_id: str):
    url = f"https://graph.facebook.com/{api_version}/{waba_id}/message_templates"
    status, j, snippet = _get(url, token)
    if status != 200 or not isinstance(j, dict):
        return [], f"HTTP {status}: {snippet}"
    if "error" in j:
        return [], f"Meta error: {str(j.get('error'))[:800]}"
    return (j.get("data") or []), None

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

def _session_with_proxy(proxy_str: str | None):
    s = requests.Session()
    if proxy_str:
        ip, port, user, pwd = proxy_str.split(":")
        proxy_url = f"http://{user}:{pwd}@{ip}:{port}"
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
