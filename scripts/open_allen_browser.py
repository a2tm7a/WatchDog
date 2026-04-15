#!/usr/bin/env python3
"""
Open allen.in in a normal Chromium window so you can see what the site does.

Waits ~4s for the late homepage popup, dismisses common overlays, clicks Login,
then waits until you press Enter.

  python3 scripts/open_allen_browser.py

Optional URL:

  WATCHDOG_DEBUG_URL=https://allen.in/neet python3 scripts/open_allen_browser.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from auth_session import _dismiss_optional_overlays

STEALTH = Stealth()
URL = os.environ.get("WATCHDOG_DEBUG_URL", "https://allen.in")


def main() -> None:
    print(f"Opening {URL} (headed, stealth on)…")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="en-IN",
        )
        STEALTH.apply_stealth_sync(context)
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        print("Navigation finished. Waiting ~4s for delayed popup…")
        time.sleep(4.0)
        _dismiss_optional_overlays(page)
        nav = page.locator("button[data-testid='loginCtaButton']")
        nav.first.wait_for(state="visible", timeout=10_000)
        nav.first.click(timeout=10_000)
        print("Login clicked — inspect the modal. Press Enter here when done.")
        try:
            input()
        except EOFError:
            print("(no TTY — sleeping 60s then closing)")
            time.sleep(60)
        browser.close()


if __name__ == "__main__":
    main()
