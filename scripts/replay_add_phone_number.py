# -*- coding: utf-8 -*-
"""
Replay the "Usar nome de exibição" mutation that provisions a virtual phone number on a WABA.
GraphQL: useWhatsAppBusinessVirtualNumberCreationMutation_PhoneProfileCreationMutation
Field:   xfb_create_whatsapp_business_virtual_phone_number

WARNING: every successful run provisions a REAL new virtual number on the WABA — not idempotent.
If you get a login/fb_dtsg error, refresh COOKIES + FB_DTSG from a fresh browser session/HAR.

Usage:
    py scripts/replay_add_phone_number.py
"""
from __future__ import annotations
import json
import sys
import requests

# ── CONFIG — edit these before each run ──────────────────────────────────────

COOKIES = (
    "locale=es_LA; datr=DhgMaX-CCCl6-MXr6BWON57j; sb=I2cnarSJJ-TCfK0lMLKNDOPz; "
    "c_user=61583527614588; ps_l=1; ps_n=1; "
    "xs=39%3AsJ1S4I5amqS2fQ%3A2%3A1780967324%3A-1%3A-1%3A%3AAcxD9IpCPy2A5TPp0zGRVuTfFO0aY06bbM0wvxZqeT8; "
    "fr=1PzThykrI0bYN0cFL.AWcnRYeY2CHK8lBG0tGl0lpxFtDcrDRFG9hsD1c7jDHGfkXfILA.BqKzRw..AAA.0.0.BqKzTF.AWc8pPTgah9G6G-ibivSO6w8I28; "
    'presence=C%7B%22t3%22%3A%5B%5D%2C%22utc3%22%3A1781216486483%2C%22v%22%3A1%7D; '
    "wd=1440x765; "
    'alsfid={"id":"f86697d","timestamp":1781217647811}'
)

FB_DTSG  = "NAfxd17wlrpW_GMCrPi2grBuIJfodzUFPdwqZytSDDxdq8MZ0TTQivg:39:1780967324"
LSD      = "IH2PgnqI6WtQmxFJwftOkz"
USER_ID  = "61583527614588"

# Mutation inputs
WABA_ID      = "1513326770168573"
BUSINESS_ID  = "2896934180669209"
APP_ID       = "225181538219344"  # platform constant for WA Business integration in Meta BM
DISPLAY_NAME = "Luciane Teixeira"

BUSINESS_PROFILE = {
    "description": "",
    "vertical_class": "APPAREL",
    "website": "https://lucianeteixeira-bm.projetobm26.workers.dev/",
    "profile_picture_url": (
        "https://scontent.fbps3-1.fna.fbcdn.net/v/t40.76095-1/"
        "467231689_27441308728847186_1856204049307863369_n.png"
        "?stp=dst-png_s200x200&_nc_cat=107&ccb=1-7&_nc_sid=4cd98e"
        "&_nc_eui2=AeFGOKlEYaiNiV5iaDtge4DWPm0Djm5m13c-bQOObmbXdwJE_HS_jdRkA8cahtCl3w9Ikf8AqYHQkSxTmHracigv"
        "&_nc_ohc=LnkX0nBa9SwQ7kNvwHEAy0f&_nc_oc=AdptmRsb-AEgqTYV8I_sTqu-tk9hBVj-Syn-bBEY0Bj9p2BP8rzmbzQkApbCEwtK68Q"
        "&_nc_zt=24&_nc_ht=scontent.fbps3-1.fna&_nc_gid=8yTJ7Jyr-qCXX_sasxPDlQ"
        "&_nc_ss=7b2a8&oh=00_Af-LycEDmX1m-UqL5zgIS9YEoN0gTeGbzRlK7MQB53PSQw&oe=6A31243C"
    ),
    "websites": ["https://lucianeteixeira-bm.projetobm26.workers.dev/"],
}

# ── END CONFIG ────────────────────────────────────────────────────────────────

_FRIENDLY = "useWhatsAppBusinessVirtualNumberCreationMutation_PhoneProfileCreationMutation"
_DOC_ID   = "30579457621638005"
_URL      = "https://business.facebook.com/api/graphql/?_callFlowletID=5912&_triggerFlowletID=5906&qpl_active_e2e_trace_ids="


def _jazoest(dtsg: str) -> str:
    return "2" + str(sum(ord(c) for c in dtsg) + 50)


def main() -> None:
    variables = {
        "input": {
            "actor_id": USER_ID,
            "client_mutation_id": "1",
            "log_session_id": "WBxP--656817535-2032488711",
            "app_id": APP_ID,
            "waba_id": WABA_ID,
            "onboarding_source": "MBS_SETTINGS",
            "device_platform": "WEB",
            "display_name": DISPLAY_NAME,
            "business_profile": BUSINESS_PROFILE,
            "uo_logging_context": None,
            "business_id": BUSINESS_ID,
        }
    }

    body = {
        "av": USER_ID,
        "__user": USER_ID,
        "__a": "1",
        "fb_dtsg": FB_DTSG,
        "jazoest": _jazoest(FB_DTSG),
        "lsd": LSD,
        "__comet_req": "11",
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": _FRIENDLY,
        "server_timestamps": "true",
        "variables": json.dumps(variables, separators=(",", ":")),
        "doc_id": _DOC_ID,
    }

    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "x-fb-lsd": LSD,
        "x-fb-friendly-name": _FRIENDLY,
        "origin": "https://business.facebook.com",
        "referer": (
            f"https://business.facebook.com/latest/settings/whatsapp_account/"
            f"?business_id={BUSINESS_ID}&selected_asset_id={WABA_ID}"
            f"&selected_asset_type=whatsapp-business-account"
        ),
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "x-asbd-id": "359341",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "cookie": COOKIES,
    }

    print(f"Firing {_FRIENDLY} ...")
    resp = requests.post(_URL, data=body, headers=headers, timeout=30)
    print(f"HTTP {resp.status_code}")

    text = resp.text
    # FB sometimes prepends "for (;;);" as CSRF protection
    if text.startswith("for (;;);"):
        text = text[9:]

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        print("Could not parse response as JSON:")
        print(text[:500])
        sys.exit(1)

    # Surface errors first
    if "errors" in payload:
        print("GraphQL errors:")
        for e in payload["errors"]:
            print(" ", e)
        sys.exit(1)

    result = (
        payload.get("data", {})
        .get("xfb_create_whatsapp_business_virtual_phone_number")
    )

    if result is None:
        print("Unexpected response shape:")
        print(json.dumps(payload, indent=2)[:800])
        sys.exit(1)

    success = result.get("success", False)
    print(f"success:          {success}")
    print(f"display_phone:    {result.get('display_phone_number')}")
    print(f"current_status_id:{result.get('current_status_id')}")

    if not success:
        print("\nFull response:")
        print(json.dumps(payload, indent=2)[:800])
        sys.exit(1)


if __name__ == "__main__":
    main()
