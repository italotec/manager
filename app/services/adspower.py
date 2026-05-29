import threading
import time
import requests

_throttle_lock = threading.Lock()
_last_call_ts: float = 0.0
_MIN_INTERVAL = 1.1  # seconds between consecutive AdsPower API calls


class AdsPowerClient:
    def __init__(self, base: str = "http://local.adspower.net:50325"):
        self.base = base.rstrip("/")
        self.session = requests.Session()

    def _get(self, path: str, **params):
        global _last_call_ts
        with _throttle_lock:
            gap = time.time() - _last_call_ts
            if gap < _MIN_INTERVAL:
                time.sleep(_MIN_INTERVAL - gap)
            _last_call_ts = time.time()

        r = self.session.get(f"{self.base}{path}", params=params, timeout=15)
        r.raise_for_status()
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"AdsPower error [{path}]: {body.get('msg', body)}")
        return body.get("data") or {}

    def open_browser(self, profile_id: str) -> dict:
        """Start the AdsPower profile browser. Returns the data dict (ws, debug_port, etc.)."""
        return self._get("/api/v1/browser/start", user_id=profile_id)

    def is_browser_active(self, profile_id: str) -> str:
        """Return 'active', 'inactive', or 'error'. Never raises."""
        try:
            with _throttle_lock:
                gap = time.time() - _last_call_ts
                if gap < _MIN_INTERVAL:
                    time.sleep(_MIN_INTERVAL - gap)
                _last_call_ts = time.time()

            r = self.session.get(
                f"{self.base}/api/v1/browser/active",
                params={"user_id": profile_id},
                timeout=10,
            )
            if not r.ok:
                return "error"
            body = r.json()
            if body.get("code") != 0:
                return "error"
            status = (body.get("data") or {}).get("status", "")
            if status == "Active":
                return "active"
            if status == "Inactive":
                return "inactive"
            return "error"
        except Exception:
            return "error"
