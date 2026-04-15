"""
WatchDog — AuthSession
=======================
Manages login and stream-profile switching for WatchDog's authenticated
scraping mode (Phase 2 / R-20, R-21).

Credentials are read from environment variables (preferred) or from
test_credentials.json as a local-dev fallback:
    WATCHDOG_TEST_FORM_ID      — test account phone / email / form_id
    WATCHDOG_TEST_PASSWORD     — test account password

Stream profiles supported: JEE, NEET, Classes610

Usage::

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(**desktop_kwargs)
        stealth.apply_stealth_sync(context)

        session = AuthSession(context)
        session.login()
        session.switch_profile("JEE")
        # ... hand context to ScraperEngine for scraping ...
        session.switch_profile("NEET")
        # ...
        session.close()
"""

import json
import logging
import os
import time
from typing import Any, Optional

from playwright.sync_api import BrowserContext, Locator, Page

# ---------------------------------------------------------------------------
# Selectors — confirmed via scripts/discover_auth_selectors.py on 2026-04-15
# ---------------------------------------------------------------------------

# allen.in uses a modal login triggered from the homepage nav bar.
# The modal offers three flows; we use the Form ID flow.
BASE_URL = "https://allen.in"

# Step 1: Nav "Login" button that opens the modal
NAV_LOGIN_BUTTON = "button[data-testid='loginCtaButton']"

# Step 2: "Continue with Form ID" inside the modal (site may rename testids).
FORM_ID_FLOW_BUTTON = (
    "button[data-testid='FormIdLoginButtonWeb'], "
    "button[data-testid*='FormIdLogin'], "
    "button:has-text('Continue with Form ID'), "
    "button:has-text('Form ID')"
)

# Headless allen.in can be slow; 8s was too tight for modal paint + hydration.
_AUTH_MODAL_OPEN_MS = int(os.environ.get("WATCHDOG_AUTH_MODAL_MS", "25000"))

# Step 3–5: Fields inside the login modal (scoped via login_modal_locator in login()).
# Avoid bare `input[type='text']:visible` — it matches marketing fields outside the modal.
FORM_ID_INPUT_INNER = (
    "input[name='formId'], input#formId, "
    "input[placeholder*='Form ID'], input[placeholder*='form id'], input[placeholder*='Form Id']"
)
PASSWORD_INNER = "input[type='password']"
SUBMIT_INNER = "button[type='submit'], button:has-text('Login'), button:has-text('Sign In')"

# Legacy export name (same as inner; login always prefers modal scope first).
FORM_ID_INPUT = FORM_ID_INPUT_INNER
PASSWORD_SELECTOR = PASSWORD_INNER
SUBMIT_SELECTOR = SUBMIT_INNER

# Optional UI signals that logged-in chrome is present (see AUTH_UI_FLOW.md).
LOGGED_IN_POSITIVE_SELECTORS: tuple[str, ...] = (
    "text=Log out",
    "text=Logout",
    "[data-testid*='profile']",
    "[data-testid*='Profile']",
)

# Confirms a successful login — nav "Login" button disappears and a user
# avatar / profile icon appears. We detect login by absence of the nav button.
NAV_LOGIN_STILL_VISIBLE = "button[data-testid='loginCtaButton']"

# Indicators in URL or page text that signal session expiry / logged-out state
SESSION_EXPIRY_INDICATORS = [
    "session expired",
    "please log in",
    "please sign in",
]

# Stream profile → selector mapping for the stream-switcher UI
# TODO: confirm post-login by running discover_auth_selectors.py while logged in.
# The nav links for JEE / NEET / Class 6-10 are present on the homepage;
# for authenticated profile switching there may be a separate user-profile
# dropdown. Update once discovered.
STREAM_SELECTORS: dict[str, str] = {
    "JEE":        "a[href='/jee']:visible, a[href*='/jee']:visible",
    "NEET":       "a[href='/neet']:visible, a[href*='/neet']:visible",
    "Classes610": "a[href='/classes-6-10']:visible, a[href*='/class-6-10']:visible",
}

# URL to land on before switching profile
PROFILE_SWITCH_BASE_URL = "https://allen.in"

# Promo / survey layers appear after first paint; wait before opening Login.
POST_LOAD_LATE_POPUP_SEC = 12.0


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


def login_modal_locator(page: Page) -> Locator:
    """
    Prefer a visible [role="dialog"] that contains auth controls.
    Falls back to first visible dialog, then body (legacy — narrow field selectors only).
    """
    filtered = page.locator('[role="dialog"]').filter(
        has=page.locator(
            "button[data-testid='FormIdLoginButtonWeb'], button[data-testid*='FormIdLogin'], "
            "input[name='formId'], input[type='password']"
        )
    )
    if filtered.count() > 0:
        try:
            filtered.first.wait_for(state="visible", timeout=10_000)
            return filtered.first
        except Exception:
            pass
    any_dlg = page.locator('[role="dialog"]')
    if any_dlg.count() > 0:
        try:
            any_dlg.first.wait_for(state="visible", timeout=5_000)
            return any_dlg.first
        except Exception:
            pass
    return page.locator("body")


def _auth_debug_screenshot(page: Page, tag: str) -> None:
    if os.environ.get("WATCHDOG_AUTH_DEBUG", "").lower() not in ("1", "true", "yes"):
        return
    reports = os.path.join(os.path.dirname(__file__), "reports")
    os.makedirs(reports, exist_ok=True)
    path = os.path.join(reports, f"auth-debug-{tag}-{int(time.time())}.png")
    try:
        page.screenshot(path=path, full_page=True)
        logging.info("[AUTH] Debug screenshot written: %s", path)
    except Exception as exc:
        logging.warning("[AUTH] Debug screenshot failed: %s", exc)


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

def _load_credentials() -> dict:
    """
    Load test account credentials.
    Priority: WATCHDOG_TEST_FORM_ID / WATCHDOG_TEST_PASSWORD env vars,
    then test_credentials.json (gitignored, local dev only).
    """
    form_id  = os.environ.get("WATCHDOG_TEST_FORM_ID", "").strip()
    password = os.environ.get("WATCHDOG_TEST_PASSWORD", "").strip()
    if form_id and password:
        logging.debug("[AUTH] Using credentials from env vars.")
        return {"form_id": form_id, "password": password}

    creds_path = os.path.join(os.path.dirname(__file__), "test_credentials.json")
    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            "No test credentials found. Set WATCHDOG_TEST_FORM_ID and "
            "WATCHDOG_TEST_PASSWORD env vars, or create test_credentials.json "
            "(see test_credentials.example.json)."
        )
    with open(creds_path) as f:
        creds = json.load(f)
    logging.debug("[AUTH] Using credentials from test_credentials.json.")
    return creds


# ---------------------------------------------------------------------------
# AuthSession
# ---------------------------------------------------------------------------

class AuthSession:
    """
    Manages a single authenticated browser context for WatchDog.

    Typical usage — one session shared across all stream profiles::

        session = AuthSession(context)
        session.login()

        for stream in ["JEE", "NEET", "Classes610"]:
            session.switch_profile(stream)
            # scraper runs here against context ...

        session.close()
    """

    def __init__(self, context: BrowserContext) -> None:
        self.context   = context
        self.page: Optional[Page] = None
        self._creds    = _load_credentials()
        self._logged_in = False

    def _auth_trace(self, attempt: int, step: str) -> None:
        if self.page is None or self.page.is_closed():
            logging.info("[AUTH][trace] attempt=%s step=%s page=closed", attempt, step)
            return
        snap = _auth_ui_snapshot(self.page)
        logging.info(
            "[AUTH][trace] attempt=%s step=%s url=%s snapshot=%s",
            attempt,
            step,
            self.page.url,
            snap,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def login(self) -> None:
        """
        Log in via allen.in's modal Form ID flow:
          1. Navigate to homepage
          2. Click the nav "Login" button  → modal opens
          3. Click "Continue with Form ID" → form_id + password inputs appear
          4. Fill credentials and submit
          5. Confirm login by checking nav "Login" button is gone

        Raises RuntimeError if login cannot be confirmed after 3 attempts.
        """
        logging.info("[AUTH] Starting login...")
        if self.page is None or self.page.is_closed():
            self.page = self.context.new_page()

        for attempt in range(1, 4):
            last_step = "init"
            try:
                # Step 1 — land on homepage (avoid networkidle — see _goto_spa_no_networkidle)
                last_step = "goto_home"
                _goto_spa_no_networkidle(self.page, BASE_URL)
                logging.info(
                    "[AUTH] Waiting %.1fs for late homepage popup…",
                    POST_LOAD_LATE_POPUP_SEC,
                )
                time.sleep(POST_LOAD_LATE_POPUP_SEC)
                last_step = "dismiss_overlays"
                _dismiss_optional_overlays(self.page)
                self._auth_trace(attempt, last_step)

                if attempt > 1:
                    # Clear a stuck modal / overlay from a previous failed attempt
                    for _ in range(2):
                        self.page.keyboard.press("Escape")
                        time.sleep(0.2)

                # Step 2 — open the login modal.
                # allen.in pre-renders modal buttons in the DOM (hidden) — there are
                # TWO instances of each modal button (desktop + mobile). We must:
                #   a) wait for the nav Login button to be visible before clicking it
                #   b) wait for Form ID entry in the modal to become VISIBLE before
                #      clicking — allow extra time for hydration in headless mode.
                last_step = "nav_login_click"
                nav_btn_loc = self.page.locator(NAV_LOGIN_BUTTON)
                nav_btn_loc.first.wait_for(state="visible", timeout=15_000)
                nav_btn_loc.first.click(timeout=15_000)
                logging.info("[AUTH] Nav Login button clicked.")
                self._auth_trace(attempt, last_step)

                # Optional: dialog shell (not all builds use role=dialog).
                last_step = "wait_dialog_shell"
                try:
                    self.page.locator('[role="dialog"]').first.wait_for(
                        state="visible", timeout=min(12_000, _AUTH_MODAL_OPEN_MS)
                    )
                except Exception:
                    pass
                self._auth_trace(attempt, last_step)

                last_step = "wait_form_id_flow"
                form_id_flow_loc = self.page.locator(FORM_ID_FLOW_BUTTON)
                try:
                    form_id_flow_loc.first.wait_for(
                        state="visible", timeout=_AUTH_MODAL_OPEN_MS
                    )
                except Exception as wait_exc:
                    n = form_id_flow_loc.count()
                    logging.warning(
                        "[AUTH] Form ID flow control not visible (matches=%s): %s",
                        n,
                        wait_exc,
                    )
                    raise
                logging.info("[AUTH] Login modal opened.")

                # Step 3 — click "Continue with Form ID" (now confirmed visible)
                last_step = "click_form_id_flow"
                form_id_flow_loc.first.click(timeout=15_000)
                time.sleep(0.5)  # allow form transition animation
                logging.info("[AUTH] Form ID flow selected.")
                self._auth_trace(attempt, last_step)

                # Step 4–6 — fill inside modal scope (falls back to body + narrow selectors).
                last_step = "resolve_modal_scope"
                scope = login_modal_locator(self.page)
                self._auth_trace(attempt, last_step)

                last_step = "fill_form_id"
                scope.locator(FORM_ID_INPUT_INNER).first.fill(
                    self._creds["form_id"], timeout=15_000
                )

                last_step = "fill_password"
                time.sleep(0.3)
                scope.locator(PASSWORD_INNER).first.fill(
                    self._creds["password"], timeout=15_000
                )

                last_step = "submit"
                scope.locator(SUBMIT_INNER).first.click(timeout=15_000)
                try:
                    self.page.wait_for_load_state("load", timeout=30_000)
                except Exception:
                    pass
                time.sleep(0.5)
                self._auth_trace(attempt, last_step)

                # Step 7 — confirm login
                last_step = "confirm_logged_in"
                if self._is_logged_in():
                    self._logged_in = True
                    logging.info("[AUTH] Login confirmed. URL: %s", self.page.url)
                    return

                logging.warning(
                    "[AUTH] Login attempt %d/3 not confirmed. URL: %s",
                    attempt, self.page.url,
                )
                self._auth_trace(attempt, last_step)
                time.sleep(3)

            except Exception as exc:
                logging.warning("[AUTH] Login attempt %d/3 raised at %s: %s", attempt, last_step, exc)
                self._auth_trace(attempt, f"error_after_{last_step}")
                _auth_debug_screenshot(self.page, f"a{attempt}-{last_step}")
                time.sleep(3)

        raise RuntimeError(
            "[AUTH] Login failed after 3 attempts. "
            "Check selectors in auth_session.py and run "
            "scripts/discover_auth_selectors.py to inspect the live page."
        )

    def switch_profile(self, stream: str) -> None:
        """
        Switch the logged-in user's stream profile via the UI dropdown.
        Waits for the full page reload before returning.

        Args:
            stream: One of "JEE", "NEET", "Classes610"
        """
        if stream not in STREAM_SELECTORS:
            raise ValueError(
                f"Unknown stream '{stream}'. Valid options: {list(STREAM_SELECTORS)}"
            )
        if not self._logged_in:
            raise RuntimeError(
                "[AUTH] Cannot switch profile — not logged in. Call login() first."
            )

        # Re-check session before switching
        self._ensure_session()

        logging.info("[AUTH] Switching to stream profile: %s", stream)
        selector = STREAM_SELECTORS[stream]

        _goto_spa_no_networkidle(self.page, PROFILE_SWITCH_BASE_URL)
        self.page.locator(selector).first.click(timeout=10_000)
        try:
            self.page.wait_for_load_state("load", timeout=30_000)
        except Exception:
            pass
        time.sleep(0.4)
        logging.info("[AUTH] Profile switched to %s. URL: %s", stream, self.page.url)

    def close(self) -> None:
        """Close the auth page (not the context — that's the caller's responsibility)."""
        if self.page and not self.page.is_closed():
            try:
                self.page.close()
            except Exception:
                pass
        self._logged_in = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_logged_in(self) -> bool:
        """
        True when the nav Login CTA is not visible and either a positive
        logged-in chrome signal matches, or (by default) we infer success from
        the nav CTA alone. Set WATCHDOG_AUTH_STRICT_SUCCESS=1 to require a
        positive selector match.
        """
        if self.page is None:
            return False
        try:
            nav_btn = self.page.query_selector(NAV_LOGIN_STILL_VISIBLE)
            if nav_btn and nav_btn.is_visible():
                return False
        except Exception:
            return False

        strict = os.environ.get("WATCHDOG_AUTH_STRICT_SUCCESS", "").lower() in (
            "1",
            "true",
            "yes",
        )
        for sel in LOGGED_IN_POSITIVE_SELECTORS:
            try:
                loc = self.page.locator(sel).first
                if loc.is_visible(timeout=600):
                    logging.debug("[AUTH] Logged-in check: positive match %r", sel)
                    return True
            except Exception:
                continue

        if strict:
            logging.warning(
                "[AUTH] Strict logged-in check failed: nav login hidden but no positive selector."
            )
            return False

        logging.debug(
            "[AUTH] Logged-in inferred (nav login hidden; no positive selector). "
            "Set WATCHDOG_AUTH_STRICT_SUCCESS=1 to require profile/logout UI."
        )
        return True

    def _ensure_session(self) -> None:
        """
        Detect session expiry and transparently re-login if needed.
        Called before each profile switch.
        """
        if self.page is None or self.page.is_closed():
            self._logged_in = False
            self.login()
            return

        try:
            current_url = self.page.url.lower()
        except Exception:
            self._logged_in = False
            self.login()
            return

        # Check URL for expiry signals
        url_expired = any(ind in current_url for ind in SESSION_EXPIRY_INDICATORS)

        # Lightweight check: try fetching a short text snippet from the body
        body_expired = False
        try:
            snippet = self.page.inner_text("body", timeout=5_000)[:500].lower()
            body_expired = any(ind in snippet for ind in SESSION_EXPIRY_INDICATORS)
        except Exception:
            pass

        if url_expired or body_expired:
            logging.warning("[AUTH] Session expiry detected — re-logging in.")
            self._logged_in = False
            self.login()
