import time
import requests

BASE = "https://gateway.prosperidadepayments.com.br/api/v1"
_TIMEOUT = 30

# Simple in-memory bearer cache: api_key -> (token, expires_at)
_token_cache: dict[str, tuple[str, float]] = {}
_TOKEN_TTL = 600  # 10 minutes


def _get_bearer(api_key: str) -> tuple[str | None, str | None]:
    now = time.monotonic()
    cached = _token_cache.get(api_key)
    if cached and now < cached[1]:
        return cached[0], None

    try:
        resp = requests.post(
            f"{BASE}/auth.apiKeySeller",
            json={"apiKey": api_key},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return None, f"Prosperidade auth error: {e}"

    token = data.get("token") or data.get("accessToken")
    if not token:
        return None, f"Prosperidade: token não encontrado na resposta: {data}"

    _token_cache[api_key] = (token, now + _TOKEN_TTL)
    return token, None


def get_sales_statistics(api_key: str, start_date: str, end_date: str) -> tuple[dict | None, str | None]:
    """
    start_date / end_date: ISO-8601 strings, e.g. "2025-06-16T00:00:00" or "2025-06-16".
    Returns (stats_dict, error_str).
    """
    token, err = _get_bearer(api_key)
    if err:
        return None, err

    try:
        resp = requests.get(
            f"{BASE}/sales.getStatistics",
            params={"startDate": start_date, "endDate": end_date},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json(), None
    except requests.RequestException as e:
        # Invalidate cached token on 401
        if hasattr(e, "response") and e.response is not None and e.response.status_code == 401:
            _token_cache.pop(api_key, None)
        return None, f"Prosperidade stats error: {e}"
