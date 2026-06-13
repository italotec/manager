import threading
import time
import requests

_throttle_lock = threading.Lock()
_last_call_ts: float = 0.0
_MIN_INTERVAL = 1.1  # seconds between consecutive AdsPower API calls


class AdsPowerClient:
    def __init__(self, base: str = "http://127.0.0.1:2601"):
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

    def stop_browser(self, profile_id: str) -> None:
        """Stop the AdsPower profile browser. Never raises."""
        try:
            self._get("/api/v1/browser/stop", user_id=profile_id)
        except Exception:
            pass

    def list_group_profiles(self, group_id: str, page_size: int = 200) -> list[dict]:
        """Return all profiles belonging to a group (ordered as AdsPower returns them)."""
        data = self._get(
            "/api/v1/user/list", group_id=group_id, page=1, page_size=page_size
        )
        return data.get("list") or []

    def get_profile(self, profile_id: str) -> dict:
        """Return the profile dict for a single user_id (includes password, remark, etc.)."""
        data = self._get("/api/v1/user/list", user_id=profile_id, page=1, page_size=1)
        lst = data.get("list") or []
        if not lst:
            raise RuntimeError(f"Profile {profile_id} not found in AdsPower")
        return lst[0]

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
