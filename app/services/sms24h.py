import requests

def _session_with_proxy(proxy_str: str | None):
    s = requests.Session()
    if proxy_str:
        ip, port, user, pwd = proxy_str.split(":")
        proxy_url = f"http://{user}:{pwd}@{ip}:{port}"
        s.proxies.update({"http": proxy_url, "https": proxy_url})
    return s

def sms24h_get_number(api_key: str, base_url: str, service: str, country: str, operator: str, proxy_str: str | None):
    s = _session_with_proxy(proxy_str)
    params = {"api_key": api_key, "action": "getNumber", "service": service, "country": country}
    if operator and operator.strip():
        params["operator"] = operator.strip()

    r = s.get(base_url, params=params, timeout=30)
    text = (r.text or "").strip()

    if text.startswith("ACCESS_NUMBER"):
        # ACCESS_NUMBER:activation_id:full_phone
        parts = text.split(":")
        if len(parts) >= 3:
            return parts[1], parts[2]
    return None, None

def sms24h_get_status(api_key: str, base_url: str, activation_id: str, proxy_str: str | None) -> str:
    s = _session_with_proxy(proxy_str)
    params = {"api_key": api_key, "action": "getStatus", "id": activation_id}
    r = s.get(base_url, params=params, timeout=30)
    return (r.text or "").strip()

def sms24h_cancel(api_key: str, base_url: str, activation_id: str, proxy_str: str | None) -> None:
    s = _session_with_proxy(proxy_str)
    params = {"api_key": api_key, "action": "setStatus", "id": activation_id, "status": "8"}
    s.get(base_url, params=params, timeout=30)
