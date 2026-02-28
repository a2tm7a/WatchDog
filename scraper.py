import sqlite3
import os
import re
import logging
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from playwright.sync_api import sync_playwright
from validation_service import ValidationService
from report_generator import ReportGenerator

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
            conn.execute('''
                CREATE TABLE IF NOT EXISTS courses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            # Migration guard for existing DBs that pre-date this column
            try:
                conn.execute("ALTER TABLE courses ADD COLUMN viewport TEXT DEFAULT 'desktop'")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def save_batch(self, courses):
        # timeout=30 ensures threads wait for the write lock instead of crashing
        with sqlite3.connect(self.db_name, timeout=30) as conn:
            cursor = conn.cursor()
            new_items = 0
            for item in courses:
                viewport = item.get('viewport', 'desktop')
                # Dedup is scoped per viewport — same course on desktop vs mobile = 2 rows
                cursor.execute(
                    'SELECT id FROM courses WHERE course_name = ? AND cta_link = ? AND viewport = ?',
                    (item['course_name'], item['cta_link'], viewport)
                )
                if not cursor.fetchone():
                    cursor.execute('''
                        INSERT INTO courses
                            (base_url, course_name, cta_link, price, pdp_price, cta_status, is_broken, price_mismatch, viewport)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
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
                logging.info(f"[{viewport}] Successfully saved {new_items} new courses.")

# --- BASE HANDLER STRATEGY ---
class BasePageHandler(ABC):
    """Abstract base class for all page-specific scraping logic."""

    def __init__(self, page, db_manager, viewport: str = 'desktop'):
        self.page = page
        self.db = db_manager
        self.viewport = viewport  # 'desktop' | 'mobile'
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
        """Navigates to the PDP and returns found price, CTA status, and verification flags."""
        if not pdp_url or pdp_url == original_url:
            return "N/A", "N/A", 1, 0  # Broken if it didn't lead to a new page
            
        try:
            logging.info(f"     Verifying PDP: {pdp_url}")
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
            cta_keywords = ["enroll now", "enrol now", "buy now", "select batch"]
            buttons = self.page.locator('button, a').all()
            for btn in buttons:
                try:
                    text = btn.inner_text().strip().lower()
                    if any(kw == text or (kw in text and len(text) < 40) for kw in cta_keywords):
                        cta_status = f"Found ({btn.inner_text().strip()})"
                        break
                except Exception:
                    continue
            
            # Navigate back to original context
            self.page.goto(original_url, wait_until="domcontentloaded")
            return pdp_price, cta_status, is_broken, price_mismatch
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
        logging.info(f"Using HomepageHandler for {url}")
        self.page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)
        
        # Identify Tabs (JEE, NEET, etc.)
        tab_loc = self.page.locator('div[data-testid*="TAB_ITEM"]')
        tabs = []
        for t in tab_loc.all():
            txt = t.inner_text().strip()
            if txt in ['JEE', 'NEET', 'Classes 6-10']:
                tabs.append((t, txt))

        for tab_el, tab_name in (tabs if tabs else [(None, "Main")]):
            logging.info(f"--- Category: {tab_name} ---")
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

                logging.info(f"  -> {name}")
                card_price = self.safe_get_text(card, ['[class*="price"]', '[class*="fee"]', 'h3'])
                link = self.extract_cta_link(card, tab_el, tab_name)
                logging.info(f"     Listing URL: {link}")
                
                # Verify PDP
                pdp_price, cta_status, is_broken, mismatch = self.verify_pdp(link, url, card_price)
                
                logging.info(f"     PDP Price: {pdp_price} | CTA: {cta_status}")
                if is_broken: logging.warning(f"     [FLAG] Broken Link: {link}")

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

            self.db.save_batch(scraped_batch)

# --- SPECIALIZED HANDLER: PLP Page (Product Listing Page) ---
class PLPHandler(BasePageHandler):
    @staticmethod
    def can_handle(url):
        return "/online-coaching-" in url or ("/neet/" in url and url.strip('/') != "https://allen.in")

    def scrape(self, url):
        logging.info(f"Using PLPHandler for {url}")
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
            logging.info(f"--- Filter: {pill_name} ---")
            
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

                logging.info(f"  -> {name}")
                card_price = self.safe_get_text(card, ['[class*="price"]', '[class*="fee"]', 'h3'])
                link = self.extract_cta_link(card, active_pill, pill_name)
                logging.info(f"     Listing URL: {link}")

                # Verify PDP
                pdp_price, cta_status, is_broken, mismatch = self.verify_pdp(link, url, card_price)
                
                logging.info(f"     PDP Price: {pdp_price} | CTA: {cta_status}")
                if is_broken: logging.warning(f"     [FLAG] Broken Link: {link}")

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

            self.db.save_batch(scraped_batch)

# --- SPECIALIZED HANDLER: Stream Page (e.g., International Olympiads) ---
class StreamHandler(BasePageHandler):
    @staticmethod
    def can_handle(url):
        return "/international-olympiads" in url

    def scrape(self, url):
        logging.info(f"Using StreamHandler for {url}")
        self.page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)

        # Identify Class Tabs (Class 8, Class 9, etc.)
        tab_loc = self.page.locator('button, div').filter(has_text=re.compile(r'^Class \d+$'))
        tab_count = tab_loc.count()
        tabs_info = []
        for i in range(tab_count):
            txt = tab_loc.nth(i).inner_text().strip()
            if txt and txt not in tabs_info:
                tabs_info.append(txt)

        for t_idx in range(max(1, len(tabs_info))):
            tab_name = tabs_info[t_idx] if tabs_info else "Default"
            logging.info(f"--- Stream Category: {tab_name} ---")
            
            active_tab = None
            if tabs_info:
                # Find all potential tabs and select the one matching the name
                active_tab = self.page.locator('button, div').filter(has_text=tab_name).first
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

                logging.info(f"  -> {name}")
                card_price = self.safe_get_text(card, ['h3', '[class*="price"]'])
                link = self.extract_cta_link(card, active_tab, tab_name)
                logging.info(f"     Listing URL: {link}")

                pdp_price, cta_status, is_broken, mismatch = self.verify_pdp(link, url, card_price)
                logging.info(f"     PDP Price: {pdp_price} | CTA: {cta_status}")
                if is_broken: logging.warning(f"     [FLAG] Broken Link: {link}")

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

            self.db.save_batch(scraped_batch)

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
                if not line: continue
                
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

    def _run_viewport(self, tasks: list, label: str, context_kwargs: dict):
        """
        Scrape all tasks under one browser context.
        Designed to run in its own thread — each call creates an independent
        sync_playwright() session so there is no cross-thread state sharing.
        """
        logging.info(f"[{label.upper()}] Starting scrape pass")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            for tag, url in tasks:
                handler_class = self.handler_map.get(tag)
                if handler_class:
                    logging.info(f"[{label.upper()}] {tag} -> {url}")
                    handler = handler_class(page, self.db, viewport=label)
                    try:
                        handler.scrape(url)
                    except Exception as e:
                        logging.error(f"[{label.upper()}] Error on {url}: {e}")
                else:
                    logging.warning(f"No handler for tag: [{tag}]")

            browser.close()
        logging.info(f"[{label.upper()}] Scrape pass complete")

    def run(self):
        tasks = self.parse_urls()
        if not tasks:
            logging.warning("No scraping tasks found.")
            return

        start_time = datetime.now()
        url_list = [url for _, url in tasks]

        # Resolve the iPhone XR device descriptor before entering threads
        # (p.devices must be read inside a sync_playwright() context)
        with sync_playwright() as p:
            mobile_kwargs = dict(p.devices[MOBILE_DEVICE])

        viewport_configs = [
            ("desktop", {"viewport": {"width": 1920, "height": 1080}}),
            ("mobile",  mobile_kwargs),
        ]

        # Run desktop and mobile passes in parallel — ~2x faster
        logging.info("Starting parallel scrape (desktop + mobile)...")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(self._run_viewport, tasks, label, kwargs): label
                for label, kwargs in viewport_configs
            }
            for future in as_completed(futures):
                label = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"[{label.upper()}] Pass failed with unhandled error: {e}")

        # Validation runs after both passes are done
        logging.info("")
        logging.info("Running validation checks across all viewports...")
        validator = ValidationService(self.db.db_name)
        validator.validate_all_courses()
        validator.log_results()

        # Save human-readable report
        ReportGenerator(
            validation_service=validator,
            db_name=self.db.db_name,
            start_time=start_time,
            urls_scraped=url_list,
        ).save()

if __name__ == "__main__":
    import sys
    urls_file = sys.argv[1] if len(sys.argv) > 1 else "urls.txt"
    engine = ScraperEngine(urls_file)
    engine.run()
