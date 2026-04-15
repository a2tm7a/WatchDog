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

Navigation uses domcontentloaded+load (not networkidle). Override ms if needed:
    WATCHDOG_GOTO_TIMEOUT_MS=90000 python3 scripts/discover_auth_selectors.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from playwright.sync_api import sync_playwright, Page
from playwright_stealth import Stealth

from auth_session import _dismiss_optional_overlays

STEALTH   = Stealth()
HEADLESS  = os.environ.get("HEADLESS", "1") != "0"
BASE_URL  = "https://allen.in"
# allen.in keeps long-lived connections; networkidle often never fires.
DEFAULT_GOTO_TIMEOUT_MS = 60_000


def _goto_allen_home(page: Page) -> None:
    """
    Load the homepage without wait_until=networkidle (SPAs + analytics hang it).

    Uses domcontentloaded + load, optional short settle, then waits for the
    nav Login CTA when present so downstream steps do not race a blank shell.
    """
    timeout_ms = int(os.environ.get("WATCHDOG_GOTO_TIMEOUT_MS", str(DEFAULT_GOTO_TIMEOUT_MS)))
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("load", timeout=min(25_000, timeout_ms))
    except Exception:
        pass
    # Late promo / survey popups (~3–5s after shell load)
    time.sleep(4.0)
    _dismiss_optional_overlays(page)
    try:
        page.locator("button[data-testid='loginCtaButton']").first.wait_for(
            state="visible",
            timeout=25_000,
        )
    except Exception:
        print(
            "  ⚠ Nav Login CTA not visible within 25s — page may be blocked or "
            "selectors changed; continuing with dump anyway."
        )


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
    """
    Try every likely sign-in trigger until one produces a visible input form.

    allen.in has a user/login icon on the top-right corner of the nav bar —
    not a text "Sign In" link. We target icon buttons via aria-label, title,
    class patterns, and position (rightmost button in the header).
    """

    # ── Pass 1: icon / aria / data-attribute selectors ──────────────────────
    icon_candidates = [
        # aria-label matches (most reliable)
        "[aria-label*='login' i]",
        "[aria-label*='sign in' i]",
        "[aria-label*='signin' i]",
        "[aria-label*='account' i]",
        "[aria-label*='user' i]",
        "[aria-label*='profile' i]",
        # title attribute
        "[title*='login' i]",
        "[title*='sign in' i]",
        "[title*='account' i]",
        # data-testid
        "[data-testid*='login']",
        "[data-testid*='signin']",
        "[data-testid*='user']",
        "[data-testid*='account']",
        # class-name heuristics
        "button[class*='login']",
        "button[class*='signin']",
        "button[class*='sign-in']",
        "button[class*='user']",
        "button[class*='account']",
        "a[class*='login']",
        "a[class*='signin']",
        # svg icon wrapper buttons in header/nav
        "header button svg",
        "nav button svg",
        # text-based fallbacks
        "a:has-text('Sign In')",
        "a:has-text('Login')",
        "button:has-text('Sign In')",
        "button:has-text('Login')",
        "header a[href*='sign']",
        "header a[href*='login']",
    ]

    for sel in icon_candidates:
        try:
            els = page.query_selector_all(sel)
            if els:
                # For SVG hits, walk up to the clickable parent button/a
                el = els[0]
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "svg":
                    el = el.evaluate_handle(
                        "el => el.closest('button, a') || el.parentElement"
                    ).as_element()
                    if not el:
                        continue

                print(f"  → Clicking: {sel!r}  (found {len(els)} match(es))")
                el.click()
                page.wait_for_selector(
                    "input[type='password'], input[type='tel'], input[type='email'], input[type='text']",
                    timeout=5_000,
                )
                print("  → Login form appeared!")
                return True
        except Exception:
            pass

    # ── Pass 2: rightmost button/link in the header ──────────────────────────
    # allen.in puts the login icon at the far right of the nav
    print("  → Trying rightmost header buttons...")
    try:
        header_btns = page.query_selector_all("header button, header a[href]")
        if header_btns:
            # Sort by bounding box x-position, pick the rightmost
            with_pos = []
            for btn in header_btns:
                try:
                    box = btn.bounding_box()
                    if box:
                        with_pos.append((box["x"] + box["width"], btn))
                except Exception:
                    pass
            with_pos.sort(key=lambda t: t[0], reverse=True)
            for _, btn in with_pos[:5]:
                text = btn.inner_text()[:40].strip()
                aria = btn.get_attribute("aria-label") or ""
                print(f"    Trying rightmost element: text={text!r}  aria={aria!r}")
                try:
                    btn.click()
                    page.wait_for_selector(
                        "input[type='password'], input[type='tel'], input[type='email'], input[type='text']",
                        timeout=4_000,
                    )
                    print("  → Login form appeared (rightmost header button)!")
                    return True
                except Exception:
                    pass
    except Exception as e:
        print(f"  → Rightmost-button pass failed: {e}")

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
        _goto_allen_home(page)
        _dump_page_state(page, "Homepage (before sign-in click)")
        _dump_all_links(page)

        # ── Step 2: Click nav "Login" button to open modal ──────────────────
        # allen.in pre-renders modal buttons in DOM (hidden). Buttons like
        # FormIdLoginButtonWeb exist BEFORE the click — two copies each
        # (desktop + mobile). We must click nav Login first, WAIT for the
        # visible instance, then click only the visible copy.
        print("\nStep 2: Opening login modal...")
        try:
            # The nav button itself has two copies too — wait for visible one
            nav_btn = page.locator("button[data-testid='loginCtaButton']")
            nav_btn.first.wait_for(state="visible", timeout=10_000)
            nav_btn.first.click(timeout=10_000)
            print("  ✓ Clicked nav Login button — waiting for modal animation...")
        except Exception as e:
            print(f"  ✗ Could not click nav Login button: {e}")

        # Wait for FormIdLoginButtonWeb to become VISIBLE (not just in DOM)
        # before reporting — this is the reliable signal the modal is open.
        try:
            form_id_btn = page.locator("button[data-testid='FormIdLoginButtonWeb']")
            form_id_btn.first.wait_for(state="visible", timeout=25_000)
            print("  ✓ Modal open confirmed — FormIdLoginButtonWeb is visible")
        except Exception as e:
            print(f"  ✗ Modal did not open (FormIdLoginButtonWeb never became visible): {e}")

        # Report which modal buttons are now visible
        for testid in ["submitOTPButton", "FormIdLoginButtonWeb", "usernameLoginButtonWeb"]:
            loc = page.locator(f"button[data-testid='{testid}']")
            count = loc.count()
            visible = sum(1 for i in range(count) if loc.nth(i).is_visible())
            print(f"  {testid}: {count} in DOM, {visible} visible")

        # ── Step 3: Click "Continue with Form ID" (first VISIBLE instance) ──
        # The locator already points to the visible one after wait_for above;
        # click it directly.
        print("\nStep 3: Switching to Form ID flow...")
        try:
            form_id_btn = page.locator("button[data-testid='FormIdLoginButtonWeb']")
            form_id_btn.first.wait_for(state="visible", timeout=25_000)
            form_id_btn.first.click(timeout=15_000)
            time.sleep(0.6)  # form transition animation
            print("  ✓ Clicked 'Continue with Form ID' (first visible)")
        except Exception as e:
            print(f"  ✗ Could not click Form ID button: {e}")

        _dump_page_state(page, ">>> After 'Continue with Form ID' — COPY THESE INPUT SELECTORS <<<")

        # ── Step 4: Fill credentials using .first to avoid homepage fields ───
        print("\nStep 4: Filling Form ID credentials (using .first locator)...")

        # Try known form_id selectors — check which one is visible now
        form_id_candidates = [
            "input[name='formId']",
            "input[id='formId']",
            "input[placeholder*='Form ID']",
            "input[placeholder*='form id']",
            "input[placeholder*='Form Id']",
            "input[type='text']",  # last resort, use .first
        ]
        form_id_sel_used = None
        for sel in form_id_candidates:
            loc = page.locator(sel)
            count = loc.count()
            if count > 0:
                vis = sum(1 for i in range(count) if loc.nth(i).is_visible())
                print(f"  Candidate {sel!r}: {count} in DOM, {vis} visible")
                if vis > 0 and form_id_sel_used is None:
                    form_id_sel_used = sel

        if form_id_sel_used:
            try:
                page.locator(form_id_sel_used).first.fill(creds["form_id"], timeout=5_000)
                print(f"  ✓ Filled form_id using .first of {form_id_sel_used!r}")
            except Exception as e:
                print(f"  ✗ Could not fill form_id: {e}")
        else:
            print("  ✗ No visible form_id input found")

        # Check for password field
        pass_loc = page.locator("input[type='password']")
        pass_count = pass_loc.count()
        pass_visible = sum(1 for i in range(pass_count) if pass_loc.nth(i).is_visible())
        print(f"  Password inputs: {pass_count} in DOM, {pass_visible} visible")
        if pass_visible > 0:
            try:
                pass_loc.first.fill(creds["password"], timeout=5_000)
                print("  ✓ Filled password")
            except Exception as e:
                print(f"  ✗ Could not fill password: {e}")
        else:
            print("  (password field not yet visible — may appear after form_id step)")

        # Submit
        submit_loc = page.locator("button[type='submit'], button:has-text('Login'), button:has-text('Sign In')")
        try:
            submit_loc.first.click(timeout=5_000)
            try:
                page.wait_for_load_state("load", timeout=25_000)
            except Exception:
                pass
            time.sleep(1.0)
            print("  ✓ Submitted")
        except Exception as e:
            print(f"  ✗ Could not submit: {e}")

        _dump_page_state(page, "After login attempt")
        print(f"\n  Final URL: {page.url}")

        # Login confirmed when the nav "Login" button is gone
        nav_btn = page.query_selector("button[data-testid='loginCtaButton']")
        logged_in = not (nav_btn and nav_btn.is_visible())
        print(f"  Login success (nav Login btn gone): {logged_in}")

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
