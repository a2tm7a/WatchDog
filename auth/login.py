"""
auth.login — login UI mechanics for allen.in's Form ID flow.

Selectors, timing constants, and helper functions that drive the
step-by-step login sequence. These are pure Playwright UI helpers
with no dependency on the rest of the auth package.
"""

import logging
import os
import re
import time
from typing import Any, Optional

from playwright.sync_api import Locator, Page

# ---------------------------------------------------------------------------
# Selectors — confirmed via scripts/discover_auth_selectors.py on 2026-04-15
# ---------------------------------------------------------------------------

# allen.in uses a modal login triggered from the homepage nav bar.
BASE_URL = "https://allen.in"

# Step 1: Nav "Login" button that opens the modal
NAV_LOGIN_BUTTON = "button[data-testid='loginCtaButton']"

# Headless allen.in can be slow; 8s was too tight for modal paint + hydration.
_AUTH_MODAL_OPEN_MS = int(os.environ.get("WATCHDOG_AUTH_MODAL_MS", "25000"))

# Poll for a *visible* "Continue with Form ID" (duplicate DOM nodes are often hidden).
_FORM_ID_FLOW_MS = int(os.environ.get("WATCHDOG_FORM_ID_FLOW_MS", "10000"))
_FORM_ID_FLOW_POLL_S = 0.1

# Poll for visible credential fields after the Form ID method transition.
_CRED_FIELD_MS = int(os.environ.get("WATCHDOG_CRED_FIELD_MS", "18000"))
_CRED_FIELD_POLL_S = 0.12

# Step 4–6: credential fields — use login_credentials_panel_locator() after Form ID click.
FORM_ID_FIELD_SELECTORS: tuple[str, ...] = (
    "input[name='formId']",
    "input#formId",
    "input[placeholder*='Form ID']",
    "input[placeholder*='form id']",
    "input[placeholder*='Form Id']",
)
PASSWORD_INNER = "input[type='password']"
SUBMIT_BUTTON_SELECTORS: tuple[str, ...] = (
    "button[type='submit']",
    "button:has-text('Login')",
    "button:has-text('Sign In')",
)

# Dialog must contain at least one of these to count as the credentials panel.
_CREDENTIAL_FIELD_HAS = ", ".join(FORM_ID_FIELD_SELECTORS + (PASSWORD_INNER,))

# Optional UI signals that logged-in chrome is present (see AUTH_UI_FLOW.md).
LOGGED_IN_POSITIVE_SELECTORS: tuple[str, ...] = (
    "text=Log out",
    "text=Logout",
    "[data-testid*='profile']",
    "[data-testid*='Profile']",
)

# Confirms a successful login — nav "Login" button disappears.
NAV_LOGIN_STILL_VISIBLE = "button[data-testid='loginCtaButton']"

# Indicators in URL or page text that signal session expiry / logged-out state.
SESSION_EXPIRY_INDICATORS = [
    "session expired",
    "please log in",
    "please sign in",
]

# Promo / survey layers appear after first paint; wait before opening Login.
POST_LOAD_LATE_POPUP_SEC = 12.0


def _form_id_flow_budget_ms() -> int:
    return int(os.environ.get("WATCHDOG_FORM_ID_FLOW_MS", str(_FORM_ID_FLOW_MS)))


def _cred_field_budget_ms() -> int:
    return int(os.environ.get("WATCHDOG_CRED_FIELD_MS", str(_CRED_FIELD_MS)))


def _goto_spa_no_networkidle(page: Page, url: str) -> None:
    """
    Open *url* without wait_until=networkidle.

    Marketing SPAs (allen.in) keep sockets / beacons open; networkidle can hang
    until Playwright hits the navigation timeout even when the UI is usable.
    """
    timeout_ms = int(os.environ.get("WATCHDOG_GOTO_TIMEOUT_MS", "60000"))
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("load", timeout=min(25_000, timeout_ms))
    except Exception:
        pass
    time.sleep(0.4)


def _visible_dialog_or_body(page: Page, timeout_ms: int) -> Locator:
    """First visible [role=dialog], else full page (last resort for scoped locators)."""
    any_dlg = page.locator('[role="dialog"]')
    if any_dlg.count() > 0:
        try:
            any_dlg.first.wait_for(state="visible", timeout=timeout_ms)
            return any_dlg.first
        except Exception:
            pass
    return page.locator("body")


def _dismiss_optional_overlays(page: Page) -> None:
    """Close cookie / CMP banners that sit above the login modal (best-effort)."""
    candidates = (
        "button:has-text('Accept')",
        "button:has-text('Accept all')",
        "button:has-text('I understand')",
        "button:has-text('Agree')",
        "button:has-text('Not now')",
        "button:has-text('Maybe later')",
        "[aria-label='Close']",
        "button[aria-label='Close']",
    )
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=600):
                loc.click(timeout=2_000)
                time.sleep(0.25)
        except Exception:
            continue

    # allen.in promo: full-viewport DIV[data-testid="dialog"] (bg-overlay) can sit above
    # the nav Login CTA.
    try:
        page.keyboard.press("Escape")
        time.sleep(0.2)
        page.keyboard.press("Escape")
        time.sleep(0.2)
    except Exception:
        pass

    dialog_close_selectors = (
        '[data-testid="dialog"] button[aria-label="Close"]',
        '[data-testid="dialog"] [aria-label="Close"]',
        '[data-testid="dialog"] button:has-text("Close")',
        '[data-testid="dialog"] button:has-text("Skip")',
        '[data-testid="dialog"] button:has-text("Not now")',
        '[data-testid="dialog"] button',
    )
    try:
        dlg = page.locator('[data-testid="dialog"]')
        if dlg.count() > 0 and dlg.first.is_visible(timeout=800):
            for sel in dialog_close_selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=500):
                        loc.click(timeout=2_000)
                        time.sleep(0.35)
                        break
                except Exception:
                    continue
    except Exception:
        pass


def login_drawer_locator(page: Page) -> Locator:
    """
    The login UI is a drawer/modal: a [role="dialog"] that contains the method
    picker (Form ID vs OTP vs username). Scoping step 3 here avoids clicking
    hidden duplicate buttons outside the open drawer.
    """
    method_picker = (
        "button[data-testid='FormIdLoginButtonWeb'], "
        "button[data-testid*='FormIdLogin'], "
        "button[data-testid='submitOTPButton'], "
        "button[data-testid='usernameLoginButtonWeb']"
    )
    drawer = page.locator('[role="dialog"]').filter(has=page.locator(method_picker))
    if drawer.count() > 0:
        try:
            drawer.first.wait_for(state="visible", timeout=_AUTH_MODAL_OPEN_MS)
            return drawer.first
        except Exception:
            pass
    return _visible_dialog_or_body(page, min(12_000, _AUTH_MODAL_OPEN_MS))


def click_visible_form_id_flow_button(scope: Locator) -> None:
    """
    Click the first *visible* and *enabled* Continue-with-Form-ID control inside
    *scope*. allen.in keeps duplicate ``FormIdLoginButtonWeb`` nodes (e.g. mobile
    vs desktop); ``.first`` often resolves to a hidden one, so a single long
    ``wait_for(visible)`` can time out. We poll with ``WATCHDOG_FORM_ID_FLOW_MS``
    (default 10s) and short slices instead.
    """
    budget_ms = _form_id_flow_budget_ms()
    deadline = time.time() + budget_ms / 1000.0
    primary = scope.locator('button[data-testid="FormIdLoginButtonWeb"]')
    by_label = scope.get_by_role(
        "button",
        name=re.compile(r"continue\s+with\s+form\s*id", re.I),
    )

    while time.time() < deadline:
        for loc in (primary, by_label):
            try:
                n = loc.count()
            except Exception:
                n = 0
            for i in range(min(n, 10)):
                cell = loc.nth(i)
                try:
                    if not cell.is_visible():
                        continue
                    try:
                        if not cell.is_enabled():
                            continue
                    except Exception:
                        pass
                    cell.click(timeout=5_000)
                    return
                except Exception:
                    continue
        time.sleep(_FORM_ID_FLOW_POLL_S)

    raise RuntimeError(
        f"[AUTH] No visible, enabled Continue-with-Form-ID control within {budget_ms}ms "
        "(duplicate hidden nodes are common — increase WATCHDOG_FORM_ID_FLOW_MS if needed)."
    )


def _auth_ui_snapshot(page: Page) -> dict[str, Any]:
    """Compact DOM counts for tracing which login step the UI is on."""
    try:
        return page.evaluate(
            """() => {
              const ids = ['loginCtaButton','FormIdLoginButtonWeb','submitOTPButton',
                'usernameLoginButtonWeb'];
              const testIds = {};
              for (const id of ids) {
                testIds[id] = document.querySelectorAll('[data-testid="' + id + '"]').length;
              }
              return {
                testIds,
                dialogRoleCount: document.querySelectorAll('[role="dialog"]').length,
                dataTestIdDialog: document.querySelectorAll('[data-testid="dialog"]').length,
              };
            }"""
        )
    except Exception as exc:
        return {"error": str(exc)}


def login_credentials_panel_locator(page: Page) -> Locator:
    """
    After "Continue with Form ID", the method-picker button may unmount. Resolve the
    dialog that **contains credential inputs** so we do not fall back to ``body``
    and fill the wrong field (e.g. homepage FullName).
    """
    dlg = page.locator('[role="dialog"]').filter(has=page.locator(_CREDENTIAL_FIELD_HAS))
    if dlg.count() > 0:
        try:
            dlg.first.wait_for(state="visible", timeout=min(15_000, _AUTH_MODAL_OPEN_MS))
            return dlg.first
        except Exception:
            pass
    return _visible_dialog_or_body(page, 5_000)


def fill_first_visible_in_scope(
    scope: Locator,
    selectors: tuple[str, ...],
    value: str,
    *,
    what: str = "field",
) -> None:
    """Fill the first matching *visible* control (skip hidden duplicates)."""
    budget_ms = _cred_field_budget_ms()
    deadline = time.time() + budget_ms / 1000.0
    while time.time() < deadline:
        for sel in selectors:
            loc = scope.locator(sel)
            try:
                n = loc.count()
            except Exception:
                n = 0
            for i in range(min(n, 10)):
                cell = loc.nth(i)
                try:
                    if not cell.is_visible():
                        continue
                    try:
                        if not cell.is_enabled():
                            continue
                    except Exception:
                        pass
                    cell.fill(value, timeout=5_000)
                    return
                except Exception:
                    continue
        time.sleep(_CRED_FIELD_POLL_S)
    raise RuntimeError(
        f"[AUTH] No visible, enabled {what} matched within {budget_ms}ms: {selectors!r}"
    )


def click_first_visible_submit_in_scope(scope: Locator) -> None:
    budget_ms = _cred_field_budget_ms()
    deadline = time.time() + budget_ms / 1000.0
    while time.time() < deadline:
        for sel in SUBMIT_BUTTON_SELECTORS:
            loc = scope.locator(sel)
            try:
                n = loc.count()
            except Exception:
                n = 0
            for i in range(min(n, 8)):
                cell = loc.nth(i)
                try:
                    if not cell.is_visible() or not cell.is_enabled():
                        continue
                    cell.click(timeout=5_000)
                    return
                except Exception:
                    continue
        time.sleep(_CRED_FIELD_POLL_S)
    raise RuntimeError(
        f"[AUTH] No visible, enabled submit control within {budget_ms}ms: {SUBMIT_BUTTON_SELECTORS!r}"
    )
