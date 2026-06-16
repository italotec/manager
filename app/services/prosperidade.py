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
