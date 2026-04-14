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
from typing import Optional

from playwright.sync_api import BrowserContext, Page

# ---------------------------------------------------------------------------
# Selectors
# TODO: Run scripts/discover_auth_selectors.py against live allen.in and
#       replace these placeholders with the actual selectors found.
# ---------------------------------------------------------------------------

# Login page URL — try /sign-in first; update if allen.in uses a different path
LOGIN_URL = "https://allen.in/sign-in"

# Input field for the username / phone / form_id
FORM_ID_SELECTOR = "input[name='username'], input[name='phone'], input[type='tel'], input[placeholder*='Phone'], input[placeholder*='phone'], input[placeholder*='Mobile']"

# Password input
PASSWORD_SELECTOR = "input[type='password']"

# Submit / login button
SUBMIT_SELECTOR = "button[type='submit']"

# URL fragment that confirms a successful login (update after discovery)
LOGIN_SUCCESS_INDICATORS = ["/dashboard", "/home", "/profile"]

# Indicators in URL or page text that signal session expiry
SESSION_EXPIRY_INDICATORS = [
    "sign-in",
    "/login",
    "session expired",
    "please log in",
    "please sign in",
]

# Stream profile → selector mapping for the stream-switcher UI
# TODO: Run discover_auth_selectors.py post-login to find the actual selectors.
# These are best-guess placeholders based on common allen.in UI patterns.
STREAM_SELECTORS: dict[str, str] = {
    "JEE":        "[data-stream='JEE'], [data-value='JEE'], a:has-text('JEE'), button:has-text('JEE')",
    "NEET":       "[data-stream='NEET'], [data-value='NEET'], a:has-text('NEET'), button:has-text('NEET')",
    "Classes610": "[data-stream='Classes 6-10'], a:has-text('Classes 6-10'), a:has-text('Class 6'), button:has-text('Classes 6-10')",
}

# URL to navigate to before switching profile (homepage, where switcher is visible)
PROFILE_SWITCH_BASE_URL = "https://allen.in"


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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def login(self) -> None:
        """
        Navigate to the login page, fill credentials, submit, and verify.
        Raises RuntimeError if login cannot be confirmed after 3 attempts.
        """
        logging.info("[AUTH] Starting login...")
        if self.page is None or self.page.is_closed():
            self.page = self.context.new_page()

        for attempt in range(1, 4):
            try:
                self.page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

                # Fill form_id / phone
                self.page.fill(FORM_ID_SELECTOR, self._creds["form_id"])
                # Fill password
                self.page.fill(PASSWORD_SELECTOR, self._creds["password"])
                # Submit
                self.page.click(SUBMIT_SELECTOR)
                self.page.wait_for_load_state("networkidle", timeout=30_000)

                if self._is_logged_in():
                    self._logged_in = True
                    logging.info("[AUTH] Login confirmed. URL: %s", self.page.url)
                    return

                logging.warning(
                    "[AUTH] Login attempt %d/3 not confirmed. URL: %s",
                    attempt, self.page.url,
                )
                time.sleep(3)

            except Exception as exc:
                logging.warning("[AUTH] Login attempt %d/3 raised: %s", attempt, exc)
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

        self.page.goto(PROFILE_SWITCH_BASE_URL, wait_until="networkidle", timeout=30_000)
        self.page.click(selector, timeout=10_000)
        self.page.wait_for_load_state("networkidle", timeout=30_000)
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
        """Return True if the current page looks like a post-login page."""
        if self.page is None:
            return False
        url = self.page.url.lower()
        # Negative check: we're NOT on the login page
        on_login_page = any(ind in url for ind in SESSION_EXPIRY_INDICATORS)
        if on_login_page:
            return False
        # Positive check: we ARE on a known post-login path
        on_success = any(path in url for path in LOGIN_SUCCESS_INDICATORS)
        # If neither clearly positive nor negative, assume success if URL changed
        return on_success or url != LOGIN_URL.lower()

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
