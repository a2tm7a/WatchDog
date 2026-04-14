"""
WatchDog — Auth Selector Discovery
===================================
Run this script to inspect the allen.in login flow and profile-switching UI.

It navigates the homepage, tries to trigger the login modal by clicking
sign-in buttons, waits for the form to appear, then prints everything needed
to fill in auth_session.py.

Usage:
    python3 scripts/discover_auth_selectors.py

Set HEADLESS=0 to watch the browser live:
    HEADLESS=0 python3 scripts/discover_auth_selectors.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from playwright.sync_api import sync_playwright, Page
from playwright_stealth import Stealth

STEALTH   = Stealth()
HEADLESS  = os.environ.get("HEADLESS", "1") != "0"
BASE_URL  = "https://allen.in"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dump_inputs(page: Page, label: str) -> None:
    inputs = page.query_selector_all("input:visible, input")
    print(f"\n  Inputs ({len(inputs)} found):")
    for el in inputs:
        attrs = {a: el.get_attribute(a)
                 for a in ["name", "id", "type", "placeholder", "autocomplete", "class"]}
        # skip hidden empties
        if not any(v for v in attrs.values() if v):
            continue
        print(f"    {attrs}")


def _dump_buttons(page: Page) -> None:
    buttons = page.query_selector_all("button, [role='button'], [type='submit']")
    print(f"\n  Buttons / submit elements ({len(buttons)} found):")
    for el in buttons:
        text = el.inner_text()[:60].strip().replace("\n", " ")
        attrs = {a: el.get_attribute(a) for a in ["type", "id", "class", "data-testid"]}
        if text or any(v for v in attrs.values() if v):
            print(f"    text={text!r:40s}  {attrs}")


def _dump_all_links(page: Page) -> None:
    links = page.query_selector_all("a[href]")
    print(f"\n  All <a> links ({len(links)} found — first 40):")
    for el in links[:40]:
        href = el.get_attribute("href") or ""
        text = el.inner_text()[:40].strip().replace("\n", " ")
        print(f"    href={href:50s}  text={text!r}")


def _dump_page_state(page: Page, label: str) -> None:
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"  STAGE: {label}")
    print(f"  URL  : {page.url}")
    print(sep)
    _dump_inputs(page, label)
    _dump_buttons(page)


def _try_click_signin(page: Page) -> bool:
    """Try every likely sign-in trigger until one produces a visible form."""

    # Broad list of candidate selectors for a "Sign in" nav button/link
    candidates = [
        "a:has-text('Sign In')",
        "a:has-text('Sign in')",
        "a:has-text('Login')",
        "a:has-text('Log In')",
        "button:has-text('Sign In')",
        "button:has-text('Sign in')",
        "button:has-text('Login')",
        "button:has-text('Log In')",
        "[data-testid*='login']",
        "[data-testid*='signin']",
        "[class*='login']",
        "[class*='signin']",
        "[class*='sign-in']",
        "header a[href*='sign']",
        "nav a[href*='login']",
    ]

    for sel in candidates:
        try:
            els = page.query_selector_all(sel)
            if els:
                print(f"  → Clicking: {sel!r}  (found {len(els)} match(es))")
                els[0].click()
                # Wait for a visible input to appear (modal opening)
                page.wait_for_selector("input[type='password'], input[type='tel'], input[type='email']",
                                       timeout=5_000)
                print("  → Login form appeared!")
                return True
        except Exception:
            pass

    return False


def _dump_post_login_stream_switcher(page: Page) -> None:
    """After login, look for the stream / class switcher UI."""
    print("\n" + "=" * 70)
    print("  STAGE: Post-login — looking for stream switcher")
    print(f"  URL  : {page.url}")
    print("=" * 70)

    # Stream keywords
    stream_keywords = ["JEE", "NEET", "Class 6", "Classes 6", "6-10", "Stream", "stream"]
    print("\n  Elements containing stream keywords:")
    for kw in stream_keywords:
        found = page.query_selector_all(
            f"button:has-text('{kw}'), a:has-text('{kw}'), "
            f"[data-value*='{kw}'], [data-stream*='{kw}'], "
            f"li:has-text('{kw}'), span:has-text('{kw}')"
        )
        if found:
            print(f"  Keyword {kw!r}: {len(found)} match(es)")
            for el in found[:3]:
                tag  = el.evaluate("el => el.tagName.toLowerCase()")
                text = el.inner_text()[:60].strip().replace("\n", " ")
                attrs = {a: el.get_attribute(a) for a in ["class", "id", "data-value", "data-stream", "href"]}
                print(f"      <{tag}> text={text!r}  attrs={attrs}")

    # Also dump nav / header buttons that might be the switcher
    print("\n  Nav / header buttons (possible switcher triggers):")
    nav_btns = page.query_selector_all("header button, nav button, [role='navigation'] button")
    for el in nav_btns[:10]:
        text = el.inner_text()[:60].strip().replace("\n", " ")
        attrs = {a: el.get_attribute(a) for a in ["class", "id", "aria-label"]}
        print(f"    text={text!r:40s}  {attrs}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    creds_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "test_credentials.json")
    if not os.path.exists(creds_path):
        print(f"ERROR: {creds_path} not found — cannot attempt login.")
        print("Create it from test_credentials.example.json first.")
        sys.exit(1)

    import json
    with open(creds_path) as f:
        creds = json.load(f)

    print(f"Headless: {HEADLESS}")
    print(f"Credentials: form_id={creds['form_id'][:4]}***")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
        )
        STEALTH.apply_stealth_sync(context)
        page = context.new_page()

        # ── Step 1: Homepage ────────────────────────────────────────────────
        print(f"\nNavigating to {BASE_URL} ...")
        page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)
        _dump_page_state(page, "Homepage (before sign-in click)")
        _dump_all_links(page)

        # ── Step 2: Trigger sign-in modal / page ────────────────────────────
        print("\nLooking for sign-in trigger...")
        found_form = _try_click_signin(page)

        if not found_form:
            # Fallback: try direct URL
            print("  → No modal trigger found — trying /sign-in directly...")
            page.goto(f"{BASE_URL}/sign-in", wait_until="networkidle", timeout=30_000)

        _dump_page_state(page, "Login form (after trigger / direct URL)")

        # ── Step 3: Fill form and submit ────────────────────────────────────
        print("\nAttempting to fill login form...")
        phone_sel  = "input[type='tel'], input[type='email'], input[name*='phone'], input[name*='mobile'], input[placeholder*='Phone'], input[placeholder*='phone'], input[placeholder*='Mobile'], input[placeholder*='email']"
        pass_sel   = "input[type='password']"
        submit_sel = "button[type='submit'], button:has-text('Sign In'), button:has-text('Login'), button:has-text('Continue'), button:has-text('Verify')"

        try:
            page.fill(phone_sel, creds["form_id"], timeout=5_000)
            print(f"  ✓ Filled form_id using: {phone_sel!r[:60]}")
        except Exception as e:
            print(f"  ✗ Could not fill form_id: {e}")

        # Some sites send OTP first — check for a Continue / Send OTP button
        try:
            cont = page.query_selector("button:has-text('Continue'), button:has-text('Send OTP'), button:has-text('Get OTP')")
            if cont:
                btn_text = cont.inner_text().strip()
                print(f"  → Intermediate button found: {btn_text!r} — clicking it")
                cont.click()
                time.sleep(2)
                _dump_page_state(page, f"After clicking '{btn_text}'")
        except Exception:
            pass

        try:
            page.fill(pass_sel, creds["password"], timeout=5_000)
            print(f"  ✓ Filled password")
        except Exception as e:
            print(f"  ✗ Could not fill password: {e}")

        try:
            page.click(submit_sel, timeout=5_000)
            page.wait_for_load_state("networkidle", timeout=15_000)
            print(f"  ✓ Submitted login form")
        except Exception as e:
            print(f"  ✗ Could not submit: {e}")

        _dump_page_state(page, "After login attempt")
        print(f"\n  Final URL: {page.url}")

        logged_in = "/sign-in" not in page.url and "/login" not in page.url
        print(f"  Login success (heuristic): {logged_in}")

        # ── Step 4: Post-login — stream switcher ────────────────────────────
        if logged_in:
            _dump_post_login_stream_switcher(page)

        print("\n" + "=" * 70)
        print("  DISCOVERY COMPLETE")
        print("  Copy the selectors above into auth_session.py constants:")
        print("    LOGIN_URL, FORM_ID_SELECTOR, PASSWORD_SELECTOR,")
        print("    SUBMIT_SELECTOR, LOGIN_SUCCESS_INDICATORS, STREAM_SELECTORS")
        print("=" * 70)

        browser.close()


if __name__ == "__main__":
    main()
