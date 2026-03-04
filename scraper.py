import sqlite3
import os
import re
import logging
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from playwright.sync_api import sync_playwright
from validation_service import ValidationService
from report_generator import ReportGenerator
from email_service import EmailService

# iPhone XR — logical resolution 390×844, touch, mobile Safari user-agent
MOBILE_DEVICE = "iPhone XR"

# --- LOGGING & DATABASE SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    handlers=[logging.FileHandler("scraper.log"), logging.StreamHandler()]
)

class DatabaseManager:
    def __init__(self, db_name="scraped_data.db"):
        self.db_name = db_name
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_name) as conn:
            # WAL mode allows concurrent reads while a write is in progress
            conn.execute("PRAGMA journal_mode=WAL")
            # Runs table — one row per scraper invocation
            conn.execute('''
                CREATE TABLE IF NOT EXISTS runs (
                    run_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS courses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id  INTEGER REFERENCES runs(run_id),
                    base_url TEXT,
                    course_name TEXT,
                    cta_link TEXT,
                    price TEXT,
                    pdp_price TEXT,
                    cta_status TEXT,
                    is_broken INTEGER DEFAULT 0,
                    price_mismatch INTEGER DEFAULT 0,
                    viewport TEXT DEFAULT 'desktop',
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

    def create_run(self) -> int:
        """Insert a new row into the runs table and return its run_id."""
        with sqlite3.connect(self.db_name, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO runs DEFAULT VALUES")
            conn.commit()
            run_id = cursor.lastrowid
            logging.info(f"Run #{run_id} started.")
            return run_id

    def save_batch(self, courses, run_id: int):
        """Persist a batch of scraped courses, tagged with the current run_id."""
        # timeout=30 ensures threads wait for the write lock instead of crashing
        with sqlite3.connect(self.db_name, timeout=30) as conn:
            cursor = conn.cursor()
            new_items = 0
            for item in courses:
                viewport = item.get('viewport', 'desktop')
                cursor.execute('''
                    INSERT INTO courses
                        (run_id, base_url, course_name, cta_link, price, pdp_price, cta_status, is_broken, price_mismatch, viewport)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    run_id,
                    item['base_url'],
                    item['course_name'],
                    item['cta_link'],
                    item['price'],
                    item.get('pdp_price', 'N/A'),
                    item.get('cta_status', 'N/A'),
                    item.get('is_broken', 0),
                    item.get('price_mismatch', 0),
                    viewport
                ))
                new_items += 1
            conn.commit()
            if new_items > 0:
                logging.debug(f"[{viewport}] Saved {new_items} courses (run #{run_id}).")

    def get_url_stats(self, base_url: str, run_id: int, viewport: str) -> dict:
        """Return total card count + issue count for a URL in this run/viewport.

        Issues = broken links + price mismatches + missing CTA buttons.
        All three flags must agree with the validation report.
        """
        with sqlite3.connect(self.db_name, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*), SUM(is_broken), SUM(price_mismatch), "
                "SUM(CASE WHEN cta_status = 'Not Found' THEN 1 ELSE 0 END) "
                "FROM courses WHERE base_url=? AND run_id=? AND viewport=?",
                (base_url, run_id, viewport)
            )
            row = cursor.fetchone()
            total       = row[0] or 0
            broken      = row[1] or 0
            mismatch    = row[2] or 0
            cta_missing = row[3] or 0
            return {"cards": total, "issues": broken + mismatch + cta_missing}

# --- PDP RESULT CACHE ---
class PdpCache:
    """
    Thread-safe in-memory cache for PDP verification results.
    Key: (pdp_url, viewport)  →  Value: (pdp_price, cta_status, is_broken, price_mismatch)

    Multiple entry-point URLs (HOME, STREAM_PAGES, PLP_PAGES) frequently surface the same
    course card pointing to the same PDP.  Caching avoids re-navigating pages that have
    already been checked in the current run, which can save dozens of 8-12 s round-trips.
    """
    def __init__(self):
        self._cache: dict = {}
        self._lock = threading.Lock()

    def get(self, pdp_url: str, viewport: str):
        """Return cached result tuple or None if not cached."""
        with self._lock:
            return self._cache.get((pdp_url, viewport))

    def set(self, pdp_url: str, viewport: str, result: tuple):
        """Store a result tuple for the given (url, viewport) pair."""
        with self._lock:
            self._cache[(pdp_url, viewport)] = result

    def size(self) -> int:
        with self._lock:
            return len(self._cache)


# --- PROGRESS TRACKER ---
class ProgressTracker:
    """
    Thread-safe counter that shows [N/total] progress for a scrape pass.
    Each viewport thread owns its own tracker so the counts stay independent.
    """
    def __init__(self, total: int, label: str):
        self.total = total
        self.label = label.upper()
        self._done = 0
        self._lock = threading.Lock()
        # Width of the total number for zero-padded formatting, e.g. " 3/31"
        self._w = len(str(total))

    def advance(self) -> str:
        """Increment counter and return a formatted '[N/total]' prefix string."""
        with self._lock:
            self._done += 1
            return f"[{self.label} {self._done:{self._w}}/{self.total}]"


# --- BASE HANDLER STRATEGY ---
class BasePageHandler(ABC):
    """Abstract base class for all page-specific scraping logic."""

    def __init__(self, page, db_manager, viewport: str = 'desktop', run_id: int = None, pdp_cache: PdpCache = None):
        self.page = page
        self.db = db_manager
        self.viewport = viewport  # 'desktop' | 'mobile'
        self.run_id = run_id
        self.pdp_cache = pdp_cache  # shared, thread-safe PDP result cache
        self.processed_keys = set()

    def clean_price(self, price_str):
        """Extracts numeric value from price strings (e.g., '₹ 93,500' -> '93500')."""
        if not price_str or "N/A" in price_str or "Not Found" in price_str:
            return None
        # Extract only digits
        nums = "".join(re.findall(r'\d+', price_str.replace(',', '')))
        return nums if nums else None

    @abstractmethod
    def can_handle(url: str) -> bool:
        """Determines if this handler is suitable for the given URL."""
        pass

    @abstractmethod
    def scrape(self, url: str):
        """High-level scraping workflow for the page."""
        pass

    def safe_get_text(self, container, selectors):
        """Utility to try multiple selectors and return the first found text."""
        for sel in selectors:
            loc = container.locator(sel)
            if loc.count() > 0:
                text = loc.first.inner_text().strip().replace('\n', ' ')
                if text: return text
        return "N/A"

    def extract_cta_link(self, card, tab_el=None, tab_text="Default"):
        """Standard logic to find a link: Href first, then Click-and-Back."""
        # 1. Look for direct links
        links = card.locator('xpath=self::a | .//a')
        for i in range(links.count()):
            href = links.nth(i).get_attribute('href')
            if href and not href.startswith('#') and 'javascript' not in href:
                return f"https://allen.in{href}" if href.startswith('/') else href

        # 2. Click and Capture logic
        cta = card.locator('button')
        if cta.count() > 0:
            current_url = self.page.url
            try:
                cta.first.scroll_into_view_if_needed()
                cta.first.evaluate("el => el.click()")
                
                # Wait for URL to change
                start = time.time()
                while time.time() - start < 8:
                    if self.page.evaluate("window.location.href") != current_url:
                        break
                    time.sleep(0.5)
                
                final_link = self.page.evaluate("window.location.href")
                
                # Restoration
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
        """Navigates to the PDP and returns found price, CTA status, and verification flags.
        
        Results are cached per (pdp_url, viewport) so that the same PDP is never
        visited more than once per run, regardless of how many listing pages link to it.
        """
        if not pdp_url or pdp_url == original_url:
            return "N/A", "N/A", 1, 0  # Broken if it didn't lead to a new page

        # --- Cache look-up ---
        if self.pdp_cache is not None:
            cached = self.pdp_cache.get(pdp_url, self.viewport)
            if cached is not None:
                logging.debug(f"  [CACHE HIT] {pdp_url} ({self.viewport})")
                return cached
            
        try:
            logging.debug(f"  → PDP: {pdp_url}")
            self.page.goto(pdp_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            
            # 1. Check if broken (did we actually navigate away from the list?)
            # If current URL is still the list URL (with maybe just a # anchor), it's potentially broken
            is_broken = 1 if self.page.url.strip('/') == original_url.strip('/') else 0

            # 2. Look for Price (₹ symbol)
            pdp_price = "Not Found"
            price_locators = ['h2:has-text("₹")', 'span:has-text("₹")', 'p:has-text("₹")', 'div:has-text("₹")']
            for sel in price_locators:
                loc = self.page.locator(sel)
                for i in range(loc.count()):
                    text = loc.nth(i).inner_text().strip()
                    if '₹' in text and len(text) < 25:
                        pdp_price = text
                        break
                if pdp_price != "Not Found": break
            
            # 3. Price Mismatch Check
            price_mismatch = 0
            if card_price and pdp_price != "Not Found":
                c_price = self.clean_price(card_price)
                p_price = self.clean_price(pdp_price)
                if c_price and p_price and c_price != p_price:
                    price_mismatch = 1
                    logging.warning(f"     [FLAG] Price mismatch: Card={card_price} vs PDP={pdp_price}")
                
            # 4. Look for CTA
            # Desktop PDPs: "Enroll Now" / "Buy Now" buttons.
            # Mobile PDPs:  sticky bottom bar ("Select batch and enroll").
            # Mobile requires a reload to properly render layout/sticky elements after
            # viewport change; desktop renders correctly on first load so skip the reload.
            if self.viewport == 'mobile':
                self.page.reload(wait_until="domcontentloaded")
                time.sleep(1) # short buffer after reload

            # Scroll to bottom to trigger sticky element in case it relies on scroll
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self.page.wait_for_timeout(1000)

            cta_status = "Not Found"
            cta_keywords = ["enroll now", "enrol now", "buy now", "select batch", "select phase"]
            buttons = self.page.locator('button, a, input[type="button"], input[type="submit"]').all()
            for btn in buttons:
                try:
                    # inner_text() can return "" for sticky/fixed elements in headless mobile
                    # rendering — DOM node exists but layout hasn't been fully computed.
                    # Fall back through text_content() → aria-label → value attribute.
                    text = btn.inner_text().strip().lower()
                    if not text:
                        text = (btn.text_content() or "").strip().lower()
                    if not text:
                        text = (btn.get_attribute("aria-label") or "").strip().lower()
                    if not text:
                        text = (btn.get_attribute("value") or "").strip().lower()

                    if text and any(kw == text or (kw in text and len(text) < 40) for kw in cta_keywords):
                        display = btn.inner_text().strip() or btn.text_content().strip() or text
                        cta_status = f"Found ({display})"
                        break
                except Exception:
                    continue
            
            # Navigate back to original context
            self.page.goto(original_url, wait_until="domcontentloaded")
            result = (pdp_price, cta_status, is_broken, price_mismatch)

            # --- Cache store ---
            if self.pdp_cache is not None:
                self.pdp_cache.set(pdp_url, self.viewport, result)

            return result
        except Exception as e:
            logging.warning(f"     PDP verification failed: {e}")
            try: self.page.goto(original_url, wait_until="domcontentloaded")
            except: pass
            return "Error", "Error", 1, 0

# --- SPECIALIZED HANDLER: Homepage ---
class HomepageHandler(BasePageHandler):
    @staticmethod
    def can_handle(url):
        return url.strip('/') == "https://allen.in"

    def scrape(self, url):
        logging.debug(f"HomepageHandler: {url}")
        # networkidle waits for all API calls (course data) to complete before
        # we start scanning — critical in CI where domcontentloaded fires too early.
        self.page.goto(url, wait_until="networkidle", timeout=60000)

        # Explicitly wait for the tab elements that signal the React app is mounted.
        # If they don't appear within 15s, the page may have been blocked (Cloudflare).
        try:
            self.page.wait_for_selector('div[data-testid*="TAB_ITEM"]', timeout=15000)
        except Exception:
            logging.warning(f"HomepageHandler: Tab elements not found after 15s on {url}. "
                            "Page may not have rendered fully (possible bot-detection).")

        time.sleep(2)  # allow SPA animations / delayed content to settle

        # Identify Tabs (JEE, NEET, etc.)
        tab_loc = self.page.locator('div[data-testid*="TAB_ITEM"]')
        tabs = []
        for t in tab_loc.all():
            txt = t.inner_text().strip()
            if txt in ['JEE', 'NEET', 'Classes 6-10']:
                tabs.append((t, txt))

        for tab_el, tab_name in (tabs if tabs else [(None, "Main")]):
            logging.debug(f"  Tab: {tab_name}")
            if tab_el:
                tab_el.evaluate("el => el.click()")
                time.sleep(2)

            # Homepage cards are div-based
            cards = self.page.locator('div.rounded-normal.flex.flex-col')
            scraped_batch = []
            
            for i in range(cards.count()):
                # Re-verify filter state before each card to handle SPA resets
                if tab_el:
                    tab_el.evaluate("el => el.click()")
                    time.sleep(1)

                card = cards.nth(i)
                name = self.safe_get_text(card, ['h2', 'p.font-semibold'])
                
                if name == "N/A" or f"{tab_name}_{name}" in self.processed_keys: continue
                self.processed_keys.add(f"{tab_name}_{name}")

                # TODO(hack): skip DLP courses until they are fixed upstream
                if "DLP" in name: logging.debug(f"  [SKIP-DLP] {name}"); continue

                logging.debug(f"    Card: {name}")
                card_price = self.safe_get_text(card, ['[class*="price"]', '[class*="fee"]', 'h3'])
                link = self.extract_cta_link(card, tab_el, tab_name)
                
                # Verify PDP
                pdp_price, cta_status, is_broken, mismatch = self.verify_pdp(link, url, card_price)
                
                if is_broken: logging.warning(f"  ⚠️  Broken link for '{name}': {link}")

                scraped_batch.append({
                    "base_url": url,
                    "course_name": name,
                    "cta_link": link,
                    "price": card_price,
                    "pdp_price": pdp_price,
                    "cta_status": cta_status,
                    "is_broken": is_broken,
                    "price_mismatch": mismatch,
                    "viewport": self.viewport
                })

            self.db.save_batch(scraped_batch, self.run_id)

# --- SPECIALIZED HANDLER: PLP Page (Product Listing Page) ---
class PLPHandler(BasePageHandler):
    @staticmethod
    def can_handle(url):
        return "/online-coaching-" in url or ("/neet/" in url and url.strip('/') != "https://allen.in")

    def scrape(self, url):
        logging.debug(f"PLPHandler: {url}")
        self.page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)

        # Filters/Pills (Live, Recorded)
        pills_loc = self.page.locator('button').filter(
            has_text=re.compile(r'^(Live|Recorded|Online Test Series|Offline Test Series)$')
        )
        pill_count = pills_loc.count()
        pills_info = []
        for i in range(pill_count):
            pills_info.append(pills_loc.nth(i).inner_text().strip())

        for p_idx in range(max(1, pill_count)):
            pill_name = pills_info[p_idx] if pills_info else "Default"
            logging.debug(f"  Filter: {pill_name}")
            
            # Re-select the pill by index to avoid stale element issues
            active_pill = pills_loc.nth(p_idx) if pills_info else None
            if active_pill:
                active_pill.evaluate("el => el.click()")
                time.sleep(2)

            # Details page cards are li-based
            cards = self.page.locator('li[data-testid^="card-"]')
            scraped_batch = []

            for i in range(cards.count()):
                # Crucial: Re-apply filter before each card processing to handle SPA page resets
                if active_pill:
                    active_pill.evaluate("el => el.click()")
                    time.sleep(1)

                card = cards.nth(i)
                name = self.safe_get_text(card, ['p.font-semibold', 'h2', 'p'])
                
                if name == "N/A" or f"{pill_name}_{name}" in self.processed_keys: continue
                self.processed_keys.add(f"{pill_name}_{name}")

                # TODO(hack): skip DLP courses until they are fixed upstream
                if "DLP" in name: logging.debug(f"  [SKIP-DLP] {name}"); continue

                logging.debug(f"    Card: {name}")
                card_price = self.safe_get_text(card, ['[class*="price"]', '[class*="fee"]', 'h3'])
                link = self.extract_cta_link(card, active_pill, pill_name)

                # Verify PDP
                pdp_price, cta_status, is_broken, mismatch = self.verify_pdp(link, url, card_price)
                
                if is_broken: logging.warning(f"  ⚠️  Broken link for '{name}': {link}")

                scraped_batch.append({
                    "base_url": url,
                    "course_name": name,
                    "cta_link": link,
                    "price": card_price,
                    "pdp_price": pdp_price,
                    "cta_status": cta_status,
                    "is_broken": is_broken,
                    "price_mismatch": mismatch,
                    "viewport": self.viewport
                })

            self.db.save_batch(scraped_batch, self.run_id)

# --- SPECIALIZED HANDLER: Stream Page (e.g., International Olympiads) ---
class StreamHandler(BasePageHandler):
    @staticmethod
    def can_handle(url):
        return "/international-olympiads" in url

    def scrape(self, url):
        logging.debug(f"StreamHandler: {url}")
        self.page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)

        # Identify Class Tabs (Class 8, Class 9, etc.)
        # Important: restrict to <button> only — bare <div> matches decorative class-grid
        # elements on pages like aiot-register, creating phantom tab loops.
        tab_loc = self.page.locator('button').filter(has_text=re.compile(r'^Class \d+$'))
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
                # Click the matching button tab
                active_tab = self.page.locator('button').filter(has_text=tab_name).first
                active_tab.evaluate("el => el.click()")
                time.sleep(2)

            # Stream page cards: searching for li with p (title) and h3 (price)
            cards = self.page.locator('li').filter(has=self.page.locator('p')).filter(has=self.page.locator('h3'))
            scraped_batch = []

            for i in range(cards.count()):
                if active_tab:
                    active_tab.evaluate("el => el.click()")
                    time.sleep(1)

                card = cards.nth(i)
                card.scroll_into_view_if_needed()
                
                name = self.safe_get_text(card, ['p', 'h2'])
                
                if name == "N/A" or f"{tab_name}_{name}" in self.processed_keys: continue
                self.processed_keys.add(f"{tab_name}_{name}")

                # TODO(hack): skip DLP courses until they are fixed upstream
                if "DLP" in name: logging.debug(f"  [SKIP-DLP] {name}"); continue

                logging.debug(f"    Card: {name}")
                card_price = self.safe_get_text(card, ['h3', '[class*="price"]'])
                link = self.extract_cta_link(card, active_tab, tab_name)

                pdp_price, cta_status, is_broken, mismatch = self.verify_pdp(link, url, card_price)
                if is_broken: logging.warning(f"  ⚠️  Broken link for '{name}': {link}")

                scraped_batch.append({
                    "base_url": url,
                    "course_name": name,
                    "cta_link": link,
                    "price": card_price,
                    "pdp_price": pdp_price,
                    "cta_status": cta_status,
                    "is_broken": is_broken,
                    "price_mismatch": mismatch,
                    "viewport": self.viewport
                })

            self.db.save_batch(scraped_batch, self.run_id)

# --- CORE ENGINE ---
class ScraperEngine:
    def __init__(self, urls_file="urls.txt"):
        self.urls_file = urls_file
        self.db = DatabaseManager()
        # Mapping tags in urls.txt to Handler classes
        self.handler_map = {
            "HOME": HomepageHandler,
            "PLP_PAGES": PLPHandler,
            "STREAM_PAGES": StreamHandler,
            "RESULTS_PAGES": StreamHandler
        }

    def parse_urls(self):
        """Parses urls.txt into a list of (type, url) tuples."""
        tasks = []
        current_type = None
        
        if not os.path.exists(self.urls_file):
            logging.error(f"URL file {self.urls_file} missing.")
            return []

        with open(self.urls_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                
                # Check for section header [TYPE]
                header_match = re.match(r'^\[(.*?)\]$', line)
                if header_match:
                    current_type = header_match.group(1).upper()
                elif line.startswith('http'):
                    if current_type:
                        tasks.append((current_type, line))
                    else:
                        logging.warning(f"URL found without category: {line}")
        return tasks

    def _run_viewport(self, tasks: list, label: str, context_kwargs: dict, run_id: int, pdp_cache: PdpCache = None):
        """
        Scrape all tasks under one browser context.

        IMPORTANT — Playwright sync_api is NOT thread-safe.
        Each URL worker creates its own independent sync_playwright() session.
        The shared PdpCache is thread-safe via its own Lock.
        """
        progress = ProgressTracker(len(tasks), label)
        logging.info(f"[{label.upper()}] ▶  Starting — {len(tasks)} URLs")

        # Number of concurrent URL workers per viewport.
        MAX_URL_WORKERS = 4

        def _scrape_one_url(tag: str, url: str):
            """Worker: own playwright session → own browser → own page."""
            prefix = progress.advance()
            handler_class = self.handler_map.get(tag)
            if not handler_class:
                logging.warning(f"{prefix} ⚠️  No handler for tag [{tag}] — skipping {url}")
                return

            logging.info(f"{prefix} 🔄 {url}")
            t0 = time.time()
            success = True

            with sync_playwright() as pw:
                # --no-sandbox is required on Linux (GitHub Actions / Docker).
                # --disable-dev-shm-usage prevents crashes from /dev/shm being too small.
                # --disable-blink-features=AutomationControlled reduces bot fingerprint.
                # These flags are harmless on macOS.
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                try:
                    context = browser.new_context(**context_kwargs)
                    page = context.new_page()
                    handler = handler_class(
                        page, self.db,
                        viewport=label,
                        run_id=run_id,
                        pdp_cache=pdp_cache,
                    )
                    handler.scrape(url)
                except Exception as e:
                    success = False
                    logging.error(f"{prefix} 💥 Error scraping {url}: {e}")
                finally:
                    browser.close()

            elapsed = time.time() - t0
            if success:
                stats = self.db.get_url_stats(url, run_id, label)
                if stats["issues"] == 0:
                    logging.info(
                        f"{prefix} ✅ {url}  "
                        f"({stats['cards']} cards, all OK, {elapsed:.0f}s)"
                    )
                else:
                    logging.info(
                        f"{prefix} ❌ {url}  "
                        f"({stats['cards']} cards, {stats['issues']} issue(s), {elapsed:.0f}s)"
                    )

        with ThreadPoolExecutor(max_workers=MAX_URL_WORKERS) as url_pool:
            futures = {
                url_pool.submit(_scrape_one_url, tag, url): (tag, url)
                for tag, url in tasks
            }
            for future in as_completed(futures):
                tag, url = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"[{label.upper()}] Unhandled error for {url}: {e}")

        logging.info(f"[{label.upper()}] ✔  All {len(tasks)} URLs done")

    def run(self):
        tasks = self.parse_urls()
        if not tasks:
            logging.warning("No scraping tasks found.")
            return

        # Create a new run record before anything else
        run_id = self.db.create_run()
        start_time = datetime.now()
        url_list = [url for _, url in tasks]

        # One shared PDP cache for the entire run.
        # Both viewport threads see the same cache so a PDP verified by desktop
        # is NOT automatically reused for mobile (different viewport = different result),
        # but PDPs appearing on multiple listing pages within the same viewport ARE cached.
        pdp_cache = PdpCache()

        # Resolve the iPhone XR device descriptor before entering threads
        # (p.devices must be read inside a sync_playwright() context)
        with sync_playwright() as p:
            mobile_kwargs = dict(p.devices[MOBILE_DEVICE])

        viewport_configs = [
            ("desktop", {"viewport": {"width": 1920, "height": 1080}}),
            ("mobile",  mobile_kwargs),
        ]

        # Run desktop and mobile passes in parallel — ~2x faster.
        # Within each viewport, individual URLs are also scraped in parallel (see _run_viewport).
        logging.info("Starting parallel scrape (desktop + mobile, URLs in parallel per viewport)...")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(self._run_viewport, tasks, label, kwargs, run_id, pdp_cache): label
                for label, kwargs in viewport_configs
            }
            for future in as_completed(futures):
                label = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"[{label.upper()}] Pass failed with unhandled error: {e}")

        logging.info(f"PDP cache size at end of run: {pdp_cache.size()} unique PDP-viewport pairs")

        # Validation runs after both passes are done
        logging.info("")
        logging.info("Running validation checks across all viewports...")
        validator = ValidationService(self.db.db_name)
        validator.validate_all_courses(run_id=run_id)
        validator.log_results()

        # Save human-readable report
        report_file = ReportGenerator(
            validation_service=validator,
            db_name=self.db.db_name,
            start_time=start_time,
            urls_scraped=url_list,
            run_id=run_id,
        ).save()

        # Email notification (reads email_config.json; gracefully no-ops if not configured)
        EmailService().send_report(
            report_path=report_file,
            validation_summary=validator.get_summary(),
            run_id=run_id,
            start_time=start_time,
        )

if __name__ == "__main__":
    import sys
    urls_file = sys.argv[1] if len(sys.argv) > 1 else "urls.txt"
    engine = ScraperEngine(urls_file)
    engine.run()
