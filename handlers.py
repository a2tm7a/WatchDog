"""
Page Handlers
=============
Playwright-based page scrapers using the Strategy pattern.

Classes
-------
BasePageHandler   — Abstract base with shared helpers (clean_price, verify_pdp,
                    extract_cta_link, safe_get_text, wait_for_cards).
HomepageHandler   — Scrapes tab-based course cards (JEE / NEET / Classes 6-10).
PLPHandler        — Scrapes filter-pill based Product Listing Pages.
StreamHandler     — Scrapes class-tab based Olympiad / Results pages.

Environment variables
---------------------
WATCHDOG_WAIT_MS              Timeout for waiting on card selectors (default 10000 ms).
WATCHDOG_RETRIES              Retry count if cards don't appear (default 1).
WATCHDOG_RETRY_BACKOFF_MS     Sleep between retries (default 2000 ms).
WATCHDOG_MAX_WORKERS          URL-level concurrency per viewport (default 4).
WATCHDOG_FAIL_ON_EMPTY        Raise on empty card lists instead of warning (default false).
WATCHDOG_ARTIFACT_DIR         Where to save HTML/PNG/log artifacts (default artifacts/watchdog).
WATCHDOG_HOME_API_RE          Regex to await a network response before home-page scrape.
WATCHDOG_PLP_API_RE           Regex to await a network response before PLP scrape.
WATCHDOG_STREAM_API_RE        Regex to await a network response before stream-page scrape.
WATCHDOG_NAV_JITTER_MS        Random pre-request delay ceiling in ms (default 0 = disabled).
"""

import os
import re
import random
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime

from database import DatabaseManager
from cache import PdpCache
from constants import CTA_KEYWORDS
from utils import clean_price as _shared_clean_price


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    try:
        parsed = int(val)
        return parsed if parsed > 0 else default
    except ValueError:
        logging.warning(f"Invalid int for {name}={val!r}; using default {default}.")
        return default


def _env_str(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name)
    return val if val not in (None, "") else default


# ---------------------------------------------------------------------------
# Runtime configuration (read once at import time from environment)
# ---------------------------------------------------------------------------

WATCHDOG_WAIT_MS          = _env_int("WATCHDOG_WAIT_MS", 10000)
WATCHDOG_RETRIES          = _env_int("WATCHDOG_RETRIES", 1)
WATCHDOG_RETRY_BACKOFF_MS = _env_int("WATCHDOG_RETRY_BACKOFF_MS", 2000)
WATCHDOG_MAX_WORKERS      = _env_int("WATCHDOG_MAX_WORKERS", 4)
WATCHDOG_FAIL_ON_EMPTY    = _env_bool("WATCHDOG_FAIL_ON_EMPTY", False)
WATCHDOG_ARTIFACT_DIR     = _env_str("WATCHDOG_ARTIFACT_DIR", "artifacts/watchdog")
WATCHDOG_NAV_JITTER_MS    = _env_int("WATCHDOG_NAV_JITTER_MS", 0)
WATCHDOG_HOME_API_RE      = _env_str("WATCHDOG_HOME_API_RE")
WATCHDOG_PLP_API_RE       = _env_str("WATCHDOG_PLP_API_RE")
WATCHDOG_STREAM_API_RE    = _env_str("WATCHDOG_STREAM_API_RE")


# ---------------------------------------------------------------------------
# Abstract base handler
# ---------------------------------------------------------------------------

class BasePageHandler(ABC):
    """Abstract base class for all page-specific scraping logic."""

    def __init__(
        self,
        page,
        db_manager: DatabaseManager,
        viewport: str = "desktop",
        run_id: int = None,
        pdp_cache: PdpCache = None,
    ):
        self.page = page
        self.db = db_manager
        self.viewport = viewport       # 'desktop' | 'mobile'
        self.run_id = run_id
        self.pdp_cache = pdp_cache     # shared, thread-safe PDP result cache
        self.processed_keys = set()
        self._console_logs: list[str] = []
        try:
            self.page.on("console", self._on_console)
        except Exception as e:
            logging.debug(f"Could not attach console listener: {e}")

    def _on_console(self, msg):
        try:
            self._console_logs.append(f"{msg.type}: {msg.text}")
        except Exception as e:
            logging.debug(f"Console log capture failed: {e}")

    def _capture_artifacts(self, handler_name: str, url: str, reason: str):
        try:
            os.makedirs(WATCHDOG_ARTIFACT_DIR, exist_ok=True)
        except Exception as e:
            logging.debug(f"Could not create artifact directory: {e}")
            return

        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        base = f"{self.viewport}_{handler_name}_{ts}"
        html_path = os.path.join(WATCHDOG_ARTIFACT_DIR, f"{base}.html")
        png_path  = os.path.join(WATCHDOG_ARTIFACT_DIR, f"{base}.png")
        log_path  = os.path.join(WATCHDOG_ARTIFACT_DIR, f"{base}.log")

        try:
            content = self.page.content()
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            logging.debug(f"Could not capture HTML artifact: {e}")

        try:
            self.page.screenshot(path=png_path, full_page=True)
        except Exception as e:
            logging.debug(f"Could not capture screenshot artifact: {e}")

        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"reason={reason}\n")
                f.write(f"url={url}\n")
                for line in self._console_logs:
                    f.write(line + "\n")
        except Exception as e:
            logging.debug(f"Could not write log artifact: {e}")

    def _wait_for_api(self, api_re: str | None, timeout_ms: int) -> bool:
        if not api_re:
            return False
        try:
            pattern = re.compile(api_re)
        except re.error:
            logging.warning(f"Invalid WATCHDOG API regex: {api_re!r}")
            return False
        try:
            self.page.wait_for_response(
                lambda resp: pattern.search(resp.url), timeout=timeout_ms
            )
            return True
        except Exception:
            return False

    def _is_cloudfront_403(self) -> bool:
        """Returns True if the current page is a CloudFront 403 block page."""
        try:
            content = self.page.content()
            return (
                "The request could not be satisfied" in content
                and "cloudfront" in content.lower()
            )
        except Exception:
            return False

    def _navigate(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 30000) -> bool:
        """Navigate to url with 403-aware exponential backoff retry.

        Detects both HTTP-level 403 responses and CloudFront HTML error pages.
        Returns True on success, False if all retries are exhausted.
        """
        base_backoff = WATCHDOG_RETRY_BACKOFF_MS / 1000.0
        for attempt in range(WATCHDOG_RETRIES + 1):
            response = None
            try:
                response = self.page.goto(url, wait_until=wait_until, timeout=timeout)
            except Exception as e:
                logging.warning(f"  _navigate: goto exception on {url}: {e}")

            is_403 = (response is not None and response.status == 403) or self._is_cloudfront_403()

            if not is_403:
                return True

            wait_s = base_backoff * (2 ** attempt) + random.uniform(0, 3)
            if attempt < WATCHDOG_RETRIES:
                logging.warning(
                    f"  _navigate: CloudFront 403 on {url} "
                    f"(attempt {attempt + 1}/{WATCHDOG_RETRIES + 1}). "
                    f"Waiting {wait_s:.1f}s before retry."
                )
                time.sleep(wait_s)
            else:
                logging.warning(
                    f"  _navigate: Persistent 403 on {url} after {WATCHDOG_RETRIES + 1} attempts."
                )
                self._capture_artifacts("navigate_403", url, "cloudfront_403")
                return False
        return False

    def wait_for_cards(
        self, selector: str, url: str, handler_name: str, api_re: str | None = None
    ) -> bool:
        for attempt in range(WATCHDOG_RETRIES + 1):
            if api_re:
                self._wait_for_api(api_re, WATCHDOG_WAIT_MS)
            try:
                self.page.wait_for_selector(selector, timeout=WATCHDOG_WAIT_MS)
                return True
            except Exception:
                if attempt < WATCHDOG_RETRIES:
                    logging.warning(
                        f"{handler_name}: Cards not found after {WATCHDOG_WAIT_MS}ms on {url}. "
                        f"Retry {attempt + 1}/{WATCHDOG_RETRIES}."
                    )
                    try:
                        self.page.reload(wait_until="domcontentloaded")
                    except Exception as e:
                        logging.debug(f"Page reload during retry failed: {e}")
                    time.sleep(WATCHDOG_RETRY_BACKOFF_MS / 1000.0)
                else:
                    logging.warning(
                        f"{handler_name}: Cards not found after {WATCHDOG_WAIT_MS}ms on {url}. "
                        "Page may not have rendered fully (possible bot-detection)."
                    )
                    self._capture_artifacts(handler_name, url, "cards_not_found")
                    return False
        self.processed_keys = set()

    def clean_price(self, price_str):
        """Extracts numeric value from price strings (e.g., '₹ 93,500' -> '93500')."""
        return _shared_clean_price(price_str)

    @abstractmethod
    def can_handle(url: str) -> bool:
        """Determines if this handler is suitable for the given URL."""
        pass

    @abstractmethod
    def scrape(self, url: str):
        """High-level scraping workflow for the page."""
        pass

    def safe_get_text(self, container, selectors):
        """Try multiple selectors and return the first non-empty text found."""
        for sel in selectors:
            loc = container.locator(sel)
            if loc.count() > 0:
                text = loc.first.inner_text().strip().replace("\n", " ")
                if text:
                    return text
        return "N/A"

    def extract_cta_link(self, card, tab_el=None, tab_text="Default"):
        """Return a CTA URL: checks hrefs first, then click-and-capture."""
        # 1. Look for direct links
        links = card.locator("xpath=self::a | .//a")
        for i in range(links.count()):
            href = links.nth(i).get_attribute("href")
            if href and not href.startswith("#") and "javascript" not in href:
                return f"https://allen.in{href}" if href.startswith("/") else href

        # 2. Click and Capture
        cta = card.locator("button")
        if cta.count() > 0:
            current_url = self.page.url
            try:
                cta.first.scroll_into_view_if_needed()
                cta.first.evaluate("el => el.click()")

                start = time.time()
                while time.time() - start < 8:
                    if self.page.evaluate("window.location.href") != current_url:
                        break
                    time.sleep(0.5)

                final_link = self.page.evaluate("window.location.href")

                if final_link != current_url:
                    self.page.go_back(wait_until="domcontentloaded")
                    if tab_el:
                        tab_el.evaluate("el => el.click()")
                        time.sleep(2)
                return final_link
            except Exception as e:
                logging.warning(f"Failed to capture link via click: {e}")
        return self.page.url

    def verify_pdp(self, pdp_url, original_url, card_price=None):
        """Navigate to the PDP and return (pdp_price, cta_status, is_broken, price_mismatch).

        Results are cached per (pdp_url, viewport) so the same PDP is never
        visited more than once per run.
        """
        if not pdp_url or pdp_url == original_url:
            return "N/A", "N/A", 1, 0

        if self.pdp_cache is not None:
            cached = self.pdp_cache.get(pdp_url, self.viewport)
            if cached is not None:
                logging.debug(f"  [CACHE HIT] {pdp_url} ({self.viewport})")
                return cached

        try:
            logging.debug(f"  → PDP: {pdp_url}")
            if not self._navigate(pdp_url, timeout=30000):
                return "Blocked", "Blocked", 1, 0
            time.sleep(2)

            is_broken = 1 if self.page.url.strip("/") == original_url.strip("/") else 0

            # 2. Look for Price (₹ symbol)
            pdp_price = "Not Found"
            price_locators = [
                'h2:has-text("₹")',
                'span:has-text("₹")',
                'p:has-text("₹")',
                'div:has-text("₹")',
            ]
            for sel in price_locators:
                loc = self.page.locator(sel)
                for i in range(loc.count()):
                    text = loc.nth(i).inner_text().strip()
                    if "₹" in text and len(text) < 25:
                        pdp_price = text
                        break
                if pdp_price != "Not Found":
                    break

            # 3. Price mismatch check
            price_mismatch = 0
            if card_price and pdp_price != "Not Found":
                c_price = self.clean_price(card_price)
                p_price = self.clean_price(pdp_price)
                if c_price and p_price and c_price != p_price:
                    price_mismatch = 1
                    logging.warning(
                        f"     [FLAG] Price mismatch: Card={card_price} vs PDP={pdp_price}"
                    )

            # 4. Look for CTA
            # Mobile PDPs require a reload to render sticky bottom bars correctly.
            if self.viewport == "mobile":
                self.page.reload(wait_until="domcontentloaded")
                time.sleep(1)

            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self.page.wait_for_timeout(1000)

            cta_status = "Not Found"
            buttons = self.page.locator(
                'button, a, input[type="button"], input[type="submit"]'
            ).all()
            for btn in buttons:
                try:
                    text = btn.inner_text().strip().lower()
                    if not text:
                        text = (btn.text_content() or "").strip().lower()
                    if not text:
                        text = (btn.get_attribute("aria-label") or "").strip().lower()
                    if not text:
                        text = (btn.get_attribute("value") or "").strip().lower()

                    if text and any(
                        kw == text or (kw in text and len(text) < 40)
                        for kw in CTA_KEYWORDS
                    ):
                        display = (
                            btn.inner_text().strip()
                            or btn.text_content().strip()
                            or text
                        )
                        cta_status = f"Found ({display})"
                        break
                except Exception:
                    continue

            self._navigate(original_url)
            result = (pdp_price, cta_status, is_broken, price_mismatch)

            if self.pdp_cache is not None:
                self.pdp_cache.set(pdp_url, self.viewport, result)

            return result

        except Exception as e:
            logging.warning(f"     PDP verification failed: {e}")
            try:
                self._navigate(original_url)
            except Exception as nav_err:
                logging.debug(f"Could not navigate back to {original_url}: {nav_err}")
            return "Error", "Error", 1, 0


# ---------------------------------------------------------------------------
# Concrete handlers
# ---------------------------------------------------------------------------

class HomepageHandler(BasePageHandler):
    @staticmethod
    def can_handle(url):
        return url.strip("/") == "https://allen.in"

    def scrape(self, url):
        logging.debug(f"HomepageHandler: {url}")
        if WATCHDOG_NAV_JITTER_MS > 0:
            time.sleep(random.uniform(0, WATCHDOG_NAV_JITTER_MS / 1000.0))
        if not self._navigate(url):
            return
        self.wait_for_cards(
            "div.rounded-normal.flex.flex-col",
            url,
            "HomepageHandler",
            api_re=WATCHDOG_HOME_API_RE,
        )
        time.sleep(2)

        tab_loc = self.page.locator('div[data-testid*="TAB_ITEM"]')
        tabs = []
        for t in tab_loc.all():
            txt = t.inner_text().strip()
            if txt in ["JEE", "NEET", "Classes 6-10"]:
                tabs.append((t, txt))

        for tab_el, tab_name in (tabs if tabs else [(None, "Main")]):
            logging.debug(f"  Tab: {tab_name}")
            if tab_el:
                tab_el.evaluate("el => el.click()")
                time.sleep(2)

            cards = self.page.locator("div.rounded-normal.flex.flex-col")
            if cards.count() == 0:
                logging.warning(
                    f"HomepageHandler: Zero cards on tab '{tab_name}' at {url}"
                )
                self._capture_artifacts("HomepageHandler", url, f"empty_cards_{tab_name}")
                if WATCHDOG_FAIL_ON_EMPTY:
                    raise RuntimeError(f"HomepageHandler: No cards found on {url}")
                continue
            scraped_batch = []

            for i in range(cards.count()):
                if tab_el:
                    tab_el.evaluate("el => el.click()")
                    time.sleep(1)

                card = cards.nth(i)
                name = self.safe_get_text(card, ["h2", "p.font-semibold"])

                if name == "N/A" or f"{tab_name}_{name}" in self.processed_keys:
                    continue
                self.processed_keys.add(f"{tab_name}_{name}")

                if "DLP" in name:
                    logging.debug(f"  [SKIP-DLP] {name}")
                    continue

                logging.debug(f"    Card: {name}")
                card_price = self.safe_get_text(
                    card, ['[class*="price"]', '[class*="fee"]', "h3"]
                )
                link = self.extract_cta_link(card, tab_el, tab_name)
                pdp_price, cta_status, is_broken, mismatch = self.verify_pdp(
                    link, url, card_price
                )

                if is_broken:
                    logging.warning(f"  ⚠️  Broken link for '{name}': {link}")

                scraped_batch.append({
                    "base_url":       url,
                    "course_name":    name,
                    "cta_link":       link,
                    "price":          card_price,
                    "pdp_price":      pdp_price,
                    "cta_status":     cta_status,
                    "is_broken":      is_broken,
                    "price_mismatch": mismatch,
                    "viewport":       self.viewport,
                })

            self.db.save_batch(scraped_batch, self.run_id)


class PLPHandler(BasePageHandler):
    @staticmethod
    def can_handle(url):
        return "/online-coaching-" in url or (
            "/neet/" in url and url.strip("/") != "https://allen.in"
        )

    def scrape(self, url):
        logging.debug(f"PLPHandler: {url}")
        if WATCHDOG_NAV_JITTER_MS > 0:
            time.sleep(random.uniform(0, WATCHDOG_NAV_JITTER_MS / 1000.0))
        if not self._navigate(url):
            return
        self.wait_for_cards(
            'li[data-testid^="card-"]',
            url,
            "PLPHandler",
            api_re=WATCHDOG_PLP_API_RE,
        )
        time.sleep(1)

        pills_loc = self.page.locator("button").filter(
            has_text=re.compile(
                r"^(Live|Recorded|Online Test Series|Offline Test Series)$"
            )
        )
        pill_count = pills_loc.count()
        pills_info = [pills_loc.nth(i).inner_text().strip() for i in range(pill_count)]

        for p_idx in range(max(1, pill_count)):
            pill_name = pills_info[p_idx] if pills_info else "Default"
            logging.debug(f"  Filter: {pill_name}")

            active_pill = pills_loc.nth(p_idx) if pills_info else None
            if active_pill:
                active_pill.evaluate("el => el.click()")
                time.sleep(2)

            cards = self.page.locator('li[data-testid^="card-"]')
            if cards.count() == 0:
                logging.warning(
                    f"PLPHandler: Zero cards on pill '{pill_name}' at {url}"
                )
                self._capture_artifacts("PLPHandler", url, f"empty_cards_{pill_name}")
                if WATCHDOG_FAIL_ON_EMPTY:
                    raise RuntimeError(f"PLPHandler: No cards found on {url}")
                continue
            scraped_batch = []

            for i in range(cards.count()):
                if active_pill:
                    active_pill.evaluate("el => el.click()")
                    time.sleep(1)

                card = cards.nth(i)
                name = self.safe_get_text(card, ["p.font-semibold", "h2", "p"])

                if name == "N/A" or f"{pill_name}_{name}" in self.processed_keys:
                    continue
                self.processed_keys.add(f"{pill_name}_{name}")

                if "DLP" in name:
                    logging.debug(f"  [SKIP-DLP] {name}")
                    continue

                logging.debug(f"    Card: {name}")
                card_price = self.safe_get_text(
                    card, ['[class*="price"]', '[class*="fee"]', "h3"]
                )
                link = self.extract_cta_link(card, active_pill, pill_name)
                pdp_price, cta_status, is_broken, mismatch = self.verify_pdp(
                    link, url, card_price
                )

                if is_broken:
                    logging.warning(f"  ⚠️  Broken link for '{name}': {link}")

                scraped_batch.append({
                    "base_url":       url,
                    "course_name":    name,
                    "cta_link":       link,
                    "price":          card_price,
                    "pdp_price":      pdp_price,
                    "cta_status":     cta_status,
                    "is_broken":      is_broken,
                    "price_mismatch": mismatch,
                    "viewport":       self.viewport,
                })

            self.db.save_batch(scraped_batch, self.run_id)


class StreamHandler(BasePageHandler):
    @staticmethod
    def can_handle(url):
        return "/international-olympiads" in url

    def scrape(self, url):
        logging.debug(f"StreamHandler: {url}")
        if WATCHDOG_NAV_JITTER_MS > 0:
            time.sleep(random.uniform(0, WATCHDOG_NAV_JITTER_MS / 1000.0))
        if not self._navigate(url):
            return
        self.wait_for_cards(
            'li[data-testid^="card-"]',
            url,
            "StreamHandler",
            api_re=WATCHDOG_STREAM_API_RE,
        )
        time.sleep(1)

        tab_loc = self.page.locator("button").filter(
            has_text=re.compile(r"^Class \d+\+?$")
        )
        tab_count = tab_loc.count()
        tabs_info = []
        for i in range(tab_count):
            txt = tab_loc.nth(i).inner_text().strip()
            if txt and txt not in tabs_info:
                tabs_info.append(txt)

        for t_idx in range(max(1, len(tabs_info))):
            tab_name = tabs_info[t_idx] if tabs_info else "Default"
            logging.debug(f"  Tab: {tab_name}")

            active_tab = None
            if tabs_info:
                active_tab = (
                    self.page.locator("button").filter(has_text=tab_name).first
                )
                active_tab.evaluate("el => el.click()")
                time.sleep(2)

            cards = (
                self.page.locator("li")
                .filter(has=self.page.locator("p"))
                .filter(has=self.page.locator("h3"))
            )
            if cards.count() == 0:
                logging.warning(
                    f"StreamHandler: Zero cards on tab '{tab_name}' at {url}"
                )
                self._capture_artifacts("StreamHandler", url, f"empty_cards_{tab_name}")
                if WATCHDOG_FAIL_ON_EMPTY:
                    raise RuntimeError(f"StreamHandler: No cards found on {url}")
                continue
            scraped_batch = []

            for i in range(cards.count()):
                if active_tab:
                    active_tab.evaluate("el => el.click()")
                    time.sleep(1)

                card = cards.nth(i)
                card.scroll_into_view_if_needed()
                name = self.safe_get_text(card, ["p", "h2"])

                if name == "N/A" or f"{tab_name}_{name}" in self.processed_keys:
                    continue
                self.processed_keys.add(f"{tab_name}_{name}")

                if "DLP" in name:
                    logging.debug(f"  [SKIP-DLP] {name}")
                    continue

                logging.debug(f"    Card: {name}")
                card_price = self.safe_get_text(
                    card, ["h3", '[class*="price"]']
                )
                link = self.extract_cta_link(card, active_tab, tab_name)
                pdp_price, cta_status, is_broken, mismatch = self.verify_pdp(
                    link, url, card_price
                )

                if is_broken:
                    logging.warning(f"  ⚠️  Broken link for '{name}': {link}")

                scraped_batch.append({
                    "base_url":       url,
                    "course_name":    name,
                    "cta_link":       link,
                    "price":          card_price,
                    "pdp_price":      pdp_price,
                    "cta_status":     cta_status,
                    "is_broken":      is_broken,
                    "price_mismatch": mismatch,
                    "viewport":       self.viewport,
                })

            self.db.save_batch(scraped_batch, self.run_id)
