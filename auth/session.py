"""
auth.session — AuthSession class and credential loading.

AuthSession manages a single authenticated browser context:
  - login()          : Form ID flow against allen.in
  - switch_profile() : Change stream/class/board via /profile modal
  - close()          : Release the browser page

Credentials are read from environment variables (preferred) or from
test_credentials.json as a local-dev fallback:
    WATCHDOG_TEST_FORM_ID      — test account phone / email / form_id
    WATCHDOG_TEST_PASSWORD     — test account password

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

from auth.debug import _auth_debug_screenshot
from auth.login import (
    BASE_URL,
    FORM_ID_FIELD_SELECTORS,
    LOGGED_IN_POSITIVE_SELECTORS,
    NAV_LOGIN_BUTTON,
    NAV_LOGIN_STILL_VISIBLE,
    PASSWORD_INNER,
    POST_LOAD_LATE_POPUP_SEC,
    SESSION_EXPIRY_INDICATORS,
    _auth_ui_snapshot,
    _cred_field_budget_ms,
    _dismiss_optional_overlays,
    _form_id_flow_budget_ms,
    _goto_spa_no_networkidle,
    click_first_visible_submit_in_scope,
    click_visible_form_id_flow_button,
    fill_first_visible_in_scope,
    login_credentials_panel_locator,
    login_drawer_locator,
)
from auth.profile import PROFILE_STREAM_LABELS, run_profile_change_flow

# Root of the project (one level above this package directory).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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

    creds_path = os.path.join(_PROJECT_ROOT, "test_credentials.json")
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
            logging.debug("[AUTH][trace] attempt=%s step=%s page=closed", attempt, step)
            return
        snap = _auth_ui_snapshot(self.page)
        logging.debug(
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
                logging.debug(
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
                logging.debug("[AUTH] Nav Login button clicked.")
                self._auth_trace(attempt, last_step)

                last_step = "wait_login_drawer"
                login_drawer = login_drawer_locator(self.page)
                self._auth_trace(attempt, last_step)

                # Step 3 — first *visible* + *enabled* Continue-with-Form-ID (skip hidden duplicates).
                last_step = "click_form_id_flow"
                logging.debug(
                    "[AUTH] Polling for visible Continue-with-Form-ID (budget=%sms)…",
                    _form_id_flow_budget_ms(),
                )
                click_visible_form_id_flow_button(login_drawer)
                time.sleep(0.5)  # allow form transition animation
                logging.debug("[AUTH] Form ID flow selected (visible Continue-with-Form-ID clicked).")
                self._auth_trace(attempt, last_step)

                # Step 4–6 — credential panel (picker controls may have unmounted).
                last_step = "resolve_credentials_panel"
                time.sleep(0.35)
                scope = login_credentials_panel_locator(self.page)
                self._auth_trace(attempt, last_step)

                last_step = "fill_form_id"
                fill_first_visible_in_scope(
                    scope,
                    FORM_ID_FIELD_SELECTORS,
                    self._creds["form_id"],
                    what="form id field",
                )

                last_step = "fill_password"
                time.sleep(0.2)
                fill_first_visible_in_scope(
                    scope,
                    (PASSWORD_INNER,),
                    self._creds["password"],
                    what="password field",
                )

                last_step = "submit"
                click_first_visible_submit_in_scope(scope)
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
            "Check selectors in auth/login.py and run "
            "scripts/discover_auth_selectors.py to inspect the live page."
        )

    def switch_profile(self, stream: str) -> None:
        """
        Switch stream (and optionally class / board) via ``/profile`` → Change.

        Strict order: **stream → (wait) → class → (wait) → board** (board only
        for ``Classes610``, default **CBSE**) → **Save**. JEE / NEET skip board.

        Args:
            stream: One of "JEE", "NEET", "Classes610"
        """
        if stream not in PROFILE_STREAM_LABELS:
            raise ValueError(
                f"Unknown stream '{stream}'. Valid options: {list(PROFILE_STREAM_LABELS)}"
            )
        if not self._logged_in:
            raise RuntimeError(
                "[AUTH] Cannot switch profile — not logged in. Call login() first."
            )

        # Re-check session before switching
        self._ensure_session()
        page = self.page
        assert page is not None

        logging.info("[AUTH] Switching profile via /profile Change flow: stream=%s", stream)
        run_profile_change_flow(page, stream)
        logging.info("[AUTH] Profile switched to %s. URL: %s", stream, page.url)

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

        url_expired = any(ind in current_url for ind in SESSION_EXPIRY_INDICATORS)

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
