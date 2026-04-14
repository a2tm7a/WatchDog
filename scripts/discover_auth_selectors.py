"""
WatchDog — Auth Selector Discovery
===================================
Run this script once with headless=False to inspect the allen.in login flow
and profile-switching UI. It navigates the site and prints all input/button
elements found, so you can copy the correct selectors into auth_session.py.

Usage:
    python3 scripts/discover_auth_selectors.py

Requirements:
    pip install playwright playwright-stealth
    playwright install chromium
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

STEALTH = Stealth()

CANDIDATE_LOGIN_URLS = [
    "https://allen.in/sign-in",
    "https://allen.in/login",
    "https://allen.in/",
]


def _print_form_elements(page, label: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  URL: {page.url}")
    print(f"{'='*60}")

    inputs = page.query_selector_all("input")
    print(f"\nInputs ({len(inputs)} found):")
    for el in inputs:
        attrs = {a: el.get_attribute(a) for a in ["name", "id", "type", "placeholder", "class"]}
        print(f"  {attrs}")

    buttons = page.query_selector_all("button")
    print(f"\nButtons ({len(buttons)} found):")
    for el in buttons:
        attrs = {a: el.get_attribute(a) for a in ["type", "id", "class"]}
        attrs["text"] = el.inner_text()[:60].strip()
        print(f"  {attrs}")

    links = page.query_selector_all("a[href*='sign'], a[href*='login'], a[href*='auth']")
    print(f"\nAuth-related links ({len(links)} found):")
    for el in links:
        print(f"  href={el.get_attribute('href')}  text={el.inner_text()[:40].strip()}")


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        STEALTH.apply_stealth_sync(context)
        page = context.new_page()

        for url in CANDIDATE_LOGIN_URLS:
            try:
                print(f"\nNavigating to {url} ...")
                page.goto(url, wait_until="networkidle", timeout=30_000)
                _print_form_elements(page, f"Page: {url}")
            except Exception as exc:
                print(f"  ERROR: {exc}")

        browser.close()


if __name__ == "__main__":
    main()
