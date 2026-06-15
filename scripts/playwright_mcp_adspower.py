"""
MCP launcher: boots an AdsPower profile then runs Playwright MCP connected via CDP.
Profile configured by ADSPOWER_PROFILE_ID env var (set in .mcp.json).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import requests

ADSPOWER_BASE = os.environ.get("ADSPOWER_BASE", "http://127.0.0.1:50360")


def open_browser(profile_id: str) -> dict:
    r = requests.get(
        f"{ADSPOWER_BASE}/api/v1/browser/start",
        params={"user_id": profile_id},
        timeout=30,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"AdsPower error: {data}")
    return data.get("data", {})


def main() -> int:
    profile_id = os.environ.get("ADSPOWER_PROFILE_ID", "").strip()
    if not profile_id:
        print("ADSPOWER_PROFILE_ID env var is required.", file=sys.stderr)
        return 2

    print(f"[playwright-adspower] Profile: {profile_id}", file=sys.stderr)

    try:
        info = open_browser(profile_id)
    except Exception as e:
        print(f"Failed to open AdsPower browser: {e}", file=sys.stderr)
        return 3

    cdp_url = (info.get("ws") or {}).get("puppeteer")
    if not cdp_url:
        print(f"No CDP URL returned for {profile_id}: {info}", file=sys.stderr)
        return 4

    print(f"[playwright-adspower] CDP: {cdp_url}", file=sys.stderr)

    npx = shutil.which("npx")
    if not npx:
        print("npx not found — install Node.js", file=sys.stderr)
        return 5

    output_dir = Path(__file__).parent.parent / ".playwright-mcp" / profile_id
    output_dir.mkdir(parents=True, exist_ok=True)

    return subprocess.call([
        npx, "@playwright/mcp@latest",
        "--cdp-endpoint", cdp_url,
        "--output-dir", str(output_dir),
    ])


if __name__ == "__main__":
    sys.exit(main())
