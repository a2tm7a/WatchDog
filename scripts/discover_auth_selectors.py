"""
WatchDog — Auth Selector Discovery
===================================
Run this script to inspect the allen.in login flow and the **profile** Change UI
(``/profile`` → Change → stream / class / board).

It navigates the homepage, tries to trigger the login modal by clicking
sign-in buttons, waits for the form to appear, then prints everything needed
to fill in auth_session.py.

Usage:
    python3 scripts/discover_auth_selectors.py

Set HEADLESS=0 to watch the browser live:
    HEADLESS=0 python3 scripts/discover_auth_selectors.py

Credentials: same as AuthSession — `WATCHDOG_TEST_FORM_ID` + `WATCHDOG_TEST_PASSWORD`
or `test_credentials.json` (see `test_credentials.example.json`).

Navigation uses domcontentloaded+load (not networkidle). Override ms if needed:
    WATCHDOG_GOTO_TIMEOUT_MS=90000 python3 scripts/discover_auth_selectors.py

Headed window size (~13" MacBook Air content area, default 1440×900):
    WATCHDOG_HEADED_VIEWPORT_WIDTH=1280 WATCHDOG_HEADED_VIEWPORT_HEIGHT=800 HEADLESS=0 ...

**Live profile switch (optional, mutates the signed-in account):**

    WATCHDOG_DISCOVER_PROFILE_STREAM=JEE HEADLESS=0 python3 scripts/discover_auth_selectors.py

Same as ``AuthSession.switch_profile``: ``/profile`` → Change → stream → optional
class / board → Save. Reuses ``WATCHDOG_PROFILE_CLASS`` and ``WATCHDOG_PROFILE_BOARD``
(``WATCHDOG_PROFILE_BOARD`` matters when stream is ``Classes610``). Set
``WATCHDOG_AUTH_DEBUG=1`` to capture screenshots on login failure inside
``AuthSession`` only; discover prints errors on switch failure.

For **profile / JEE pill** issues, set ``WATCHDOG_PROFILE_DEBUG=1`` to emit
``[AUTH][profile]`` logs and write ``reports/profile-debug-*.txt`` + ``.png``.
"""

import logging
import os
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from playwright.sync_api import sync_playwright, Page
from playwright_stealth import Stealth

from auth import (
    FORM_ID_FIELD_SELECTORS,
    PASSWORD_INNER,
    POST_LOAD_LATE_POPUP_SEC,
    PROFILE_CHANGE_BUTTON,
    PROFILE_PAGE_URL,
    PROFILE_STREAM_LABELS,
    _dismiss_optional_overlays,
    _load_credentials,
    click_first_visible_submit_in_scope,
    click_visible_form_id_flow_button,
    fill_first_visible_in_scope,
    login_credentials_panel_locator,
    login_drawer_locator,
    run_profile_change_flow,
)

STEALTH   = Stealth()
HEADLESS  = os.environ.get("HEADLESS", "1") != "0"
BASE_URL  = "https://allen.in"
# Headed browser: MacBook Air–class viewport (not full 1080p — avoids tiny UI on large monitors).
_HEADED_VIEWPORT_W = int(os.environ.get("WATCHDOG_HEADED_VIEWPORT_WIDTH", "1440"))
_HEADED_VIEWPORT_H = int(os.environ.get("WATCHDOG_HEADED_VIEWPORT_HEIGHT", "900"))
# allen.in keeps long-lived connections; networkidle often never fires.
DEFAULT_GOTO_TIMEOUT_MS = 60_000


def _goto_allen_home(page: Page) -> None:
    """
    Load the homepage without wait_until=networkidle (SPAs + analytics hang it).

    Uses domcontentloaded + load, then POST_LOAD_LATE_POPUP_SEC sleep, overlay
    dismiss, then waits for the nav Login CTA when present.
    """
    timeout_ms = int(os.environ.get("WATCHDOG_GOTO_TIMEOUT_MS", str(DEFAULT_GOTO_TIMEOUT_MS)))
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("load", timeout=min(25_000, timeout_ms))
    except Exception:
        pass
    time.sleep(POST_LOAD_LATE_POPUP_SEC)
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


def _goto_profile(page: Page) -> None:
    timeout_ms = int(os.environ.get("WATCHDOG_GOTO_TIMEOUT_MS", "60000"))
    page.goto(PROFILE_PAGE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("load", timeout=min(25_000, timeout_ms))
    except Exception:
        pass
    time.sleep(0.4)


def _dump_post_login_profile_change(page: Page) -> None:
    """After login, open ``/profile`` and print Change + stream-related controls."""
    print("\n" + "=" * 70)
    print("  STAGE: Post-login — profile page (Change stream / class / board)")
    print("=" * 70)

    _goto_profile(page)
    _dismiss_optional_overlays(page)
    print(f"\n  URL  : {page.url}")

    loc = page.locator(PROFILE_CHANGE_BUTTON)
    try:
        n = loc.count()
    except Exception:
        n = 0
    print(f"\n  PROFILE_CHANGE_BUTTON matches: {n}")
    for i in range(min(n, 12)):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
        except Exception:
            continue
        try:
            tag = el.evaluate("el => el.tagName.toLowerCase()")
            text = el.inner_text()[:80].strip().replace("\n", " ")
            attrs = {
                a: el.get_attribute(a)
                for a in ("class", "id", "data-testid", "aria-label", "role", "href")
            }
            print(f"    [{i}] <{tag}> text={text!r}  attrs={attrs}")
        except Exception as exc:
            print(f"    [{i}] (could not describe element: {exc})")

    # Stream / class keywords on profile (popup opens only after Change — keyword scan still helps)
    stream_keywords = ["JEE", "NEET", "Class 6", "Classes 6", "6-10", "Stream", "Board", "Change"]
    print("\n  Elements containing stream / profile keywords (visible slice):")
    for kw in stream_keywords:
        found = page.query_selector_all(
            f"button:has-text('{kw}'), a:has-text('{kw}'), "
            f"[data-value*='{kw}'], [data-stream*='{kw}'], "
            f"li:has-text('{kw}'), span:has-text('{kw}')"
        )
        if found:
            print(f"  Keyword {kw!r}: {len(found)} match(es)")
            for el in found[:3]:
                try:
                    if not el.is_visible():
                        continue
                except Exception:
                    pass
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                text = el.inner_text()[:60].strip().replace("\n", " ")
                attrs = {
                    a: el.get_attribute(a)
                    for a in ["class", "id", "data-value", "data-stream", "href"]
                }
                print(f"      <{tag}> text={text!r}  attrs={attrs}")


def _normalize_discover_profile_stream(raw: str) -> Optional[str]:
    t = raw.strip()
    if not t:
        return None
    for key in PROFILE_STREAM_LABELS:
        if t.upper() == key.upper():
            return key
    compact = t.replace(" ", "").replace("–", "-").lower()
    if compact in ("classes6-10", "class6-10", "6-10"):
        return "Classes610"
    return None


def _run_discover_profile_switch_if_configured(page: Page) -> None:
    """
    If ``WATCHDOG_DISCOVER_PROFILE_STREAM`` is set, run the same Change flow as
    ``AuthSession.switch_profile`` to validate selectors end-to-end.
    """
    raw = os.environ.get("WATCHDOG_DISCOVER_PROFILE_STREAM", "").strip()
    if not raw:
        return
    stream = _normalize_discover_profile_stream(raw)
    if stream is None:
        print(
            "\n  ✗ WATCHDOG_DISCOVER_PROFILE_STREAM="
            f"{raw!r} — use JEE, NEET, or Classes610 (aliases: 6-10, class 6-10)."
        )
        return

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cls = os.environ.get("WATCHDOG_PROFILE_CLASS", "").strip()
    brd = os.environ.get("WATCHDOG_PROFILE_BOARD", "").strip()
    print("\n" + "=" * 70)
    print(f"  STAGE: Live profile switch — stream={stream!r}")
    if cls:
        print(f"  WATCHDOG_PROFILE_CLASS={cls!r}")
    if brd:
        print(f"  WATCHDOG_PROFILE_BOARD={brd!r}")
    print("=" * 70)

    try:
        run_profile_change_flow(page, stream)
    except Exception as exc:
        print(f"\n  ✗ Profile switch failed: {exc}")
        return

    print(f"\n  ✓ Profile change flow finished (no exception). URL: {page.url}")
    try:
        snippet = page.locator("body").inner_text(timeout=8_000)[:500].replace("\n", " ")
        print(f"  Body text preview (500 chars): {snippet!r}")
    except Exception as exc:
        print(f"  (Could not read body preview: {exc})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        creds = _load_credentials()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        print(
            "Set WATCHDOG_TEST_FORM_ID and WATCHDOG_TEST_PASSWORD, or create "
            "test_credentials.json from test_credentials.example.json."
        )
        sys.exit(1)

    print(f"Headless: {HEADLESS}")
    if not HEADLESS:
        print(f"Viewport: {_HEADED_VIEWPORT_W}×{_HEADED_VIEWPORT_H} (set WATCHDOG_HEADED_VIEWPORT_* to override)")
    print(f"Credentials: form_id={creds['form_id'][:4]}***")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            viewport={"width": _HEADED_VIEWPORT_W, "height": _HEADED_VIEWPORT_H},
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

        # Login drawer = dialog that contains the method picker (scoped like AuthSession.login).
        login_drawer = login_drawer_locator(page)

        # Report which modal buttons are in the drawer (visible vs total — duplicates are common)
        for testid in ["submitOTPButton", "FormIdLoginButtonWeb", "usernameLoginButtonWeb"]:
            loc = login_drawer.locator(f"button[data-testid='{testid}']")
            count = loc.count()
            visible = sum(1 for i in range(count) if loc.nth(i).is_visible())
            print(f"  {testid}: {count} in DOM, {visible} visible (in drawer)")

        # ── Step 3: Click first *visible* "Continue with Form ID" (poll; see WATCHDOG_FORM_ID_FLOW_MS) ──
        print("\nStep 3: Clicking visible Continue with Form ID in login drawer...")
        try:
            click_visible_form_id_flow_button(login_drawer)
            time.sleep(0.6)  # form transition animation
            print("  ✓ Clicked visible Continue with Form ID (skipped hidden duplicates)")
        except Exception as e:
            print(f"  ✗ Could not click Form ID button in drawer: {e}")

        _dump_page_state(page, ">>> After 'Continue with Form ID' — COPY THESE INPUT SELECTORS <<<")

        # ── Step 4: Fill credentials (credentials-panel scope — post picker DOM) ───
        print("\nStep 4: Filling Form ID credentials (credentials panel + visible-first)...")
        time.sleep(0.35)
        scope = login_credentials_panel_locator(page)
        try:
            fill_first_visible_in_scope(
                scope,
                FORM_ID_FIELD_SELECTORS,
                creds["form_id"],
                what="form id field",
            )
            print(f"  ✓ Filled form_id (visible-first among {FORM_ID_FIELD_SELECTORS!r})")
        except Exception as e:
            print(f"  ✗ Could not fill form_id: {e}")

        try:
            fill_first_visible_in_scope(
                scope,
                (PASSWORD_INNER,),
                creds["password"],
                what="password field",
            )
            print("  ✓ Filled password (first visible)")
        except Exception as e:
            print(f"  ✗ Could not fill password: {e}")

        try:
            click_first_visible_submit_in_scope(scope)
            try:
                page.wait_for_load_state("load", timeout=25_000)
            except Exception:
                pass
            time.sleep(1.0)
            print("  ✓ Submitted (first visible enabled)")
        except Exception as e:
            print(f"  ✗ Could not submit: {e}")

        _dump_page_state(page, "After login attempt")
        print(f"\n  Final URL: {page.url}")

        # Login confirmed when the nav "Login" button is gone
        nav_btn = page.query_selector("button[data-testid='loginCtaButton']")
        logged_in = not (nav_btn and nav_btn.is_visible())
        print(f"  Login success (nav Login btn gone): {logged_in}")

        # ── Step 4: Post-login — /profile Change UI ─────────────────────────
        if logged_in:
            _dump_post_login_profile_change(page)
            _run_discover_profile_switch_if_configured(page)

        print("\n" + "=" * 70)
        print("  DISCOVERY COMPLETE")
        print("  Copy the selectors above into auth_session.py constants:")
        print("    NAV_LOGIN_BUTTON, FORM_ID_FIELD_SELECTORS, PASSWORD_INNER,")
        print("    SUBMIT_BUTTON_SELECTORS, PROFILE_PAGE_URL, PROFILE_CHANGE_BUTTON,")
        print("    PROFILE_STREAM_LABELS")
        print("  Optional live switch: WATCHDOG_DISCOVER_PROFILE_STREAM=JEE|NEET|Classes610")
        print("    (+ WATCHDOG_PROFILE_CLASS / WATCHDOG_PROFILE_BOARD as needed)")
        print("=" * 70)

        browser.close()


if __name__ == "__main__":
    main()
