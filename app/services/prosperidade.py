import requests

BASE = "https://gateway.prosperidadepayments.com.br/api/v1"
_TIMEOUT = 30
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def get_sales_statistics(api_key: str, start_date: str, end_date: str) -> tuple[dict | None, str | None]:
    """
    Fetch sales statistics from Prosperidade Payments.
    start_date / end_date: ISO-8601 datetime, e.g. "2025-06-16T00:00:00".
    Authorization: raw API key directly in header (no Bearer, no JWT exchange).
    Money values in the response are in CENTS — divide by 100 before displaying.
    """
    try:
        resp = requests.get(
            f"{BASE}/sales.getStatistics",
            params={"startDate": start_date, "endDate": end_date},
            headers={"Authorization": api_key, "User-Agent": _UA},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json(), None
    except requests.RequestException as e:
        return None, f"Prosperidade stats error: {e}"


def get_balance(api_key: str) -> tuple[dict | None, str | None]:
    """GET /withdraw.getBalance — returns availableBalance/pendingBalance/... (in CENTS)."""
    resp = None
    try:
        resp = requests.get(
            f"{BASE}/withdraw.getBalance",
            headers={"Authorization": api_key, "User-Agent": _UA},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json(), None
    except requests.HTTPError as e:
        detail = _extract_error(resp)
        return None, f"Prosperidade balance error: {e} | {detail}"
    except requests.RequestException as e:
        return None, f"Prosperidade balance error: {e}"


def request_withdraw(
    api_key: str,
    amount_reais: float,
    bank_account_id: str,
    wtype: str,
    password: str,
) -> tuple[dict | None, str | None]:
    """POST /withdraw.requestWithdraw — amount in REAIS (gateway rejects amounts in cents)."""
    resp = None
    try:
        resp = requests.post(
            f"{BASE}/withdraw.requestWithdraw",
            json={"amount": amount_reais, "bankAccountId": bank_account_id, "type": wtype, "password": password},
            headers={"Authorization": api_key, "User-Agent": _UA},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json(), None
    except requests.HTTPError as e:
        detail = _extract_error(resp)
        return None, f"Prosperidade withdraw error: {e} | {detail}"
    except requests.RequestException as e:
        return None, f"Prosperidade withdraw error: {e}"


def _extract_error(resp) -> str:
    if resp is None:
        return ""
    try:
        body = resp.json()
        issues = "; ".join(i.get("message", "") for i in (body.get("issues") or []))
        return body.get("message", "") + (f" [{issues}]" if issues else "")
    except Exception:
        return (resp.text or "")[:300]
