"""
Playwright-based automation to accept a Facebook Business Manager invitation.

Flow:
  1. Open AdsPower profile browser (CDP)
  2. Navigate to invitation URL
  3. If OTP dialog appears → fetch code from tempmail.plus → verify
  4. Fill random name → step through 3-page form
  5. Handle password re-auth dialog if prompted
  6. Confirm landing on Meta Business Suite
"""
import os
import re
import random
import time
import logging
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .adspower import AdsPowerClient

log = logging.getLogger(__name__)

_OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / ".playwright-mcp" / "aceitar_bm"
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

_FIRST_NAMES = [
    "Nathan", "Lucas", "Gabriel", "Mateus", "Felipe", "Pedro", "Rafael",
    "Bruno", "Diego", "André", "Carlos", "Eduardo", "Fernando", "Gustavo",
    "Henrique", "Igor", "João", "Leandro", "Marcos", "Nicolas",
]
_LAST_NAMES = [
    "Carver", "Silva", "Santos", "Oliveira", "Souza", "Costa", "Ferreira",
    "Pereira", "Alves", "Lima", "Gomes", "Ribeiro", "Martins", "Rocha",
    "Almeida", "Cardoso", "Nunes", "Mendes", "Barros", "Teixeira",
]

ADSPOWER_BASE = os.environ.get("ADSPOWER_BASE", "http://127.0.0.1:50360")


def _random_name() -> tuple[str, str]:
    return random.choice(_FIRST_NAMES), random.choice(_LAST_NAMES)


def _wait_for_invite_form(page, timeout: int = 25) -> None:
    """Poll until the invitation form is ready: OTP code box, name fields, or the
    already-accepted 'Continue to business tools' button. Returns when any appears
    (or on timeout — callers handle the absent case)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.get_by_role("textbox", name=re.compile("verification code", re.I)).count():
                return
            if page.get_by_role("textbox", name="First name").count():
                return
            if page.get_by_role("button", name=re.compile("continue to business tools", re.I)).count():
                return
            # positional fallback: 2+ usable text inputs
            usable = [e for e in page.query_selector_all('input[type="text"]')
                      if _safe(lambda: e.is_visible() and e.is_enabled())]
            if len(usable) >= 2:
                return
        except Exception:
            pass
        page.wait_for_timeout(1_000)


def _safe(fn):
    try:
        return fn()
    except Exception:
        return False


def _fill_name_fields(page, first: str, last: str) -> bool:
    """Fill First/Last name. Tries accessible-name locators, falls back to the
    first two visible+enabled text inputs (placeholders are empty on this form)."""
    # Preferred: accessible name (works EN; harmless if absent)
    try:
        fn = page.get_by_role("textbox", name="First name")
        ln = page.get_by_role("textbox", name="Last name")
        if fn.count() and ln.count():
            fn.first.fill(first)
            ln.first.fill(last)
            return True
    except Exception:
        pass

    # Fallback: positional — first two usable text inputs (email field is disabled)
    usable = []
    for el in page.query_selector_all('input[type="text"]'):
        try:
            if el.is_visible() and el.is_enabled():
                usable.append(el)
        except Exception:
            continue
    if len(usable) >= 2:
        usable[0].fill(first)
        usable[1].fill(last)
        return True
    return False


def accept_bm_invitation(
    profile_id: str,
    invitation_url: str,
    log_fn=None,
    close_when_done: bool = False,
) -> dict:
    """
    Accept a BM invitation for an AdsPower profile.

    The Facebook password is fetched automatically from the AdsPower profile.
    Returns {"ok": True, "url": <final_url>} or {"ok": False, "error": <msg>}.
    log_fn(msg) is called with progress updates if provided.
    """
    def _log(msg: str):
        log.info("[aceitar_bm:%s] %s", profile_id, msg)
        if log_fn:
            log_fn(msg)

    adspower = AdsPowerClient(base=ADSPOWER_BASE)

    # Fetch password from AdsPower profile
    try:
        profile = adspower.get_profile(profile_id)
        fb_password = profile.get("password", "")
        if not fb_password:
            return {"ok": False, "error": "No password found in AdsPower profile"}
    except Exception as e:
        return {"ok": False, "error": f"AdsPower get_profile: {e}"}

    try:
        browser_data = adspower.open_browser(profile_id)
    except Exception as e:
        return {"ok": False, "error": f"AdsPower open_browser: {e}"}

    cdp_url = (browser_data.get("ws") or {}).get("puppeteer")
    if not cdp_url:
        return {"ok": False, "error": f"No CDP URL returned for profile {profile_id}"}

    _log(f"CDP: {cdp_url}")

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0]
        page = context.new_page()

        try:
            # ── Step 1: navigate to invitation ──────────────────────────────
            _log("Navigating to invitation URL…")
            page.goto(invitation_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2_000)

            # ── Step 1a: security checkpoint ("confirm you're human") ────────
            # Triggered by automation velocity. Try one soft "Continue"; if it
            # doesn't clear, flag for manual review (don't hammer — risks a ban).
            if "checkpoint" in page.url:
                _log("Facebook checkpoint detected — attempting soft continue…")
                for _ in range(2):
                    try:
                        btn = page.get_by_text(re.compile(r"^continue$", re.I))
                        if btn.count() and btn.first.is_visible():
                            btn.first.click()
                            page.wait_for_timeout(4_000)
                    except Exception:
                        pass
                    if "checkpoint" not in page.url:
                        break
                if "checkpoint" in page.url:
                    return {"ok": False, "error": "CHECKPOINT — account flagged 'confirm you're human'; needs manual review"}

            # ── Step 1b: Business gateway ("Continue with Facebook") ─────────
            # Profiles that haven't used business.facebook.com land on a gateway
            # page first. Click through with the already-logged-in FB session.
            # The button can take several seconds to render (slower on later
            # profiles once AdsPower has cycled browsers), so poll for it.
            if "loginpage" in page.url or "/login" in page.url:
                _log("Business gateway — clicking 'Continue with Facebook'…")
                clicked = False
                gw_deadline = time.time() + 25
                while time.time() < gw_deadline and not clicked:
                    for sel in (
                        page.get_by_role("button", name=re.compile("continue with facebook", re.I)),
                        page.get_by_text(re.compile("continue with facebook", re.I)),
                    ):
                        try:
                            if sel.count() and sel.first.is_visible():
                                sel.first.click()
                                clicked = True
                                break
                        except Exception:
                            continue
                    if not clicked:
                        page.wait_for_timeout(1_500)
                if not clicked:
                    return {"ok": False, "error": "Gateway shown but 'Continue with Facebook' not found (after 25s)"}
                page.wait_for_load_state("domcontentloaded")

            # Wait for the invite form to be ready (OTP box, name fields, or the
            # already-accepted button) before reading state — avoids false
            # "name fields not found" on slow loads.
            _wait_for_invite_form(page, timeout=25)

            # ── Step 2: OTP dialog (confirm business email) ──────────────────
            otp_box = page.get_by_role("textbox", name=re.compile("verification code", re.I))
            otp_present = False
            try:
                otp_present = otp_box.count() > 0 and otp_box.first.is_visible()
            except Exception:
                otp_present = False

            if otp_present:
                # Email shown in the disabled "Business email" field
                email_addr = ""
                try:
                    email_box = page.get_by_role("textbox", name=re.compile("business email", re.I))
                    if email_box.count():
                        email_addr = (email_box.first.input_value() or "").strip()
                except Exception:
                    email_addr = ""
                if not email_addr:
                    try:
                        body = page.locator("[role=dialog]").first.inner_text(timeout=5_000)
                    except Exception:
                        body = page.content()
                    m = re.search(r'[\w.+\-]+@[\w.\-]+', body)
                    email_addr = m.group(0) if m else ""

                if not email_addr:
                    return {"ok": False, "error": "OTP required but business email not found"}

                _log(f"OTP email: {email_addr} — fetching code from tempmail.plus…")
                code = _get_otp_tempmail(context, email_addr, _log, timeout=120)
                if not code:
                    return {"ok": False, "error": f"OTP code for {email_addr} not received in time"}

                _log(f"OTP code: {code}")
                otp_box.first.fill(code)
                page.get_by_role("button", name=re.compile(r"^verify$", re.I)).first.click()
                page.wait_for_timeout(3_000)

            # ── Already accepted? (retry of a completed invite) ──────────────
            # A previously-accepted invite shows a single "Continue to business
            # tools" button instead of the name form.
            try:
                done_btn = page.get_by_role(
                    "button", name=re.compile("continue to business tools", re.I)
                )
                if done_btn.count() and done_btn.first.is_visible():
                    _log("Invite already accepted — nothing to do.")
                    return {"ok": True, "url": page.url, "note": "already_accepted"}
            except Exception:
                pass

            # ── Step 3: fill name (step 1 of 3) ────────────────────────────
            first, last = _random_name()
            _log(f"Filling name: {first} {last}")
            if not _fill_name_fields(page, first, last):
                return {"ok": False, "error": "Name fields not found — unexpected page state"}

            page.get_by_role("button", name=re.compile(r"^continue$", re.I)).first.click()
            page.wait_for_timeout(1_500)

            # ── Step 4: review business (step 2 of 3) ───────────────────────
            _log("Step 2: reviewing business info…")
            page.get_by_role("button", name=re.compile(r"^continue$", re.I)).first.click()
            page.wait_for_timeout(1_500)

            # ── Step 5: accept (step 3 of 3) ────────────────────────────────
            _log("Step 3: accepting invitation…")
            page.get_by_role("button", name=re.compile(r"accept invitation", re.I)).first.click()
            page.wait_for_timeout(2_000)

            # ── Steps 6+7: handle password re-auth + wait for redirect ───────
            # Poll: whenever the re-auth password dialog appears, fill it; succeed
            # as soon as the URL leaves the invitation page.
            reauth_done = False
            deadline = time.time() + 40
            while time.time() < deadline:
                # Each step is wrapped: while the page redirects to business home
                # the execution context is torn down and DOM calls raise — that's
                # actually the success signal, so we just re-check the URL.
                try:
                    url = page.url
                    if "business.facebook.com" in url and "invitation" not in url:
                        _log(f"Success — landed on {url}")
                        return {"ok": True, "url": url}

                    # Facebook rejected the accept (account-level block)
                    try:
                        body_l = page.inner_text("body").lower()
                        if "cannot be added to the business" in body_l or "user cannot be added" in body_l:
                            return {"ok": False, "error": "REJECTED — Facebook: user cannot be added to the business (account restricted)"}
                    except Exception:
                        pass

                    pwd_input = page.query_selector(
                        '[data-testid="reauth_password_field"], input[name="password"][type="password"]'
                    )
                    if pwd_input and not reauth_done and pwd_input.is_visible():
                        _log("Password re-auth required — submitting…")
                        pwd_input.fill(fb_password)
                        page.locator(
                            '[data-testid="sec_ac_button"], button:has-text("Submit"), '
                            'button:has-text("Enviar"), button:has-text("Confirm")'
                        ).first.click()
                        reauth_done = True
                        page.wait_for_timeout(3_000)
                        continue
                except Exception:
                    # navigation in progress — give it a moment, then re-check URL
                    page.wait_for_timeout(1_000)
                    try:
                        url = page.url
                        if "business.facebook.com" in url and "invitation" not in url:
                            _log(f"Success — landed on {url}")
                            return {"ok": True, "url": url}
                    except Exception:
                        pass

                page.wait_for_timeout(1_500)

            # Timed out — capture a screenshot for diagnosis
            shot = ""
            try:
                shot = str(_OUTPUT_DIR / f"fail_{profile_id}_{int(time.time())}.png")
                page.screenshot(path=shot)
            except Exception:
                pass
            return {"ok": False, "error": f"No redirect after accept (url={page.url}); shot={shot}"}

        except PWTimeout as e:
            return {"ok": False, "error": f"Playwright timeout: {e}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                browser.close()  # detach CDP connection (does not kill AdsPower)
            except Exception:
                pass
            if close_when_done:
                adspower.stop_browser(profile_id)


def _extract_code(text: str) -> str | None:
    """Pull the 6-digit OTP from an email body, preferring one near the
    'verification code' / 'código de verificação' label to avoid stray numbers."""
    m = re.search(
        r'(?:verification code|c[oó]digo de verifica[cç][aã]o)\D{0,20}(\d{6})',
        text, re.I,
    )
    if m:
        return m.group(1)
    m = re.search(r'\b(\d{6})\b', text)
    return m.group(1) if m else None


def _set_tempmail_inbox(page, prefix: str, domain: str) -> None:
    """Set the tempmail.plus inbox name and (if needed) domain."""
    name_box = page.locator(
        'input[placeholder="Name"], input#pre_button, input[name="pre_button"]'
    ).first
    name_box.click()
    page.keyboard.press("Control+a")
    name_box.type(prefix)
    name_box.press("Enter")
    page.wait_for_timeout(1_500)

    # Domain selector — the button shows the current "@domain". If it differs,
    # open it and pick the matching one.
    if domain:
        try:
            dom_btn = page.locator('button:has-text("@")').first
            cur = (dom_btn.inner_text() or "").lstrip("@").strip().lower()
            if cur and cur != domain.lower():
                dom_btn.click()
                page.wait_for_timeout(500)
                opt = page.get_by_text(f"@{domain}", exact=False).first
                if opt.count():
                    opt.click()
                    page.wait_for_timeout(1_000)
        except Exception:
            pass


def _get_otp_tempmail(context, email_addr: str, log_fn, timeout: int = 120) -> str | None:
    """
    Open tempmail.plus, switch the inbox to `email_addr`, and wait for a
    Facebook verification email. Returns the 6-digit OTP or None.
    """
    prefix, _, domain = email_addr.partition("@")
    page = context.new_page()
    try:
        page.goto("https://tempmail.plus", wait_until="domcontentloaded", timeout=15_000)
        page.wait_for_timeout(1_000)
        _set_tempmail_inbox(page, prefix, domain)

        deadline = time.time() + timeout
        while time.time() < deadline:
            # Each inbox email is <div class="mail" onclick="fex.go('mail/ID')">;
            # newest first. Pick the freshest Facebook message.
            rows = page.query_selector_all("div.mail")
            for row in rows:
                try:
                    text = row.inner_text()
                except Exception:
                    continue
                if "facebook" in text.lower():
                    log_fn("Facebook email found — opening…")
                    row.click()
                    page.wait_for_timeout(2_000)
                    code = _extract_code(page.inner_text("body"))
                    if code:
                        return code
                    # Code not rendered yet — go back and retry next loop
                    break

            page.reload(wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(1_000)
            try:
                _set_tempmail_inbox(page, prefix, domain)
            except Exception:
                pass

        return None
    finally:
        try:
            page.close()
        except Exception:
            pass
