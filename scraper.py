"""
WatchDog — Scraper Engine
=========================
Entry point and orchestrator for the WatchDog scraping pipeline.

Run with::

    python3 scraper.py [urls_file]

The heavy lifting lives in the focused sub-modules:

    database.py   — DatabaseManager (SQLite persistence)
    cache.py      — PdpCache, ProgressTracker
    handlers.py   — BasePageHandler and concrete page handlers,
                    plus all WATCHDOG_* runtime config constants
    constants.py  — Shared CTA keywords, severity icons/order
    utils.py      — Shared price-cleaning helpers
"""

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from database import DatabaseManager
from cache import PdpCache, ProgressTracker
from handlers import (
    HomepageHandler,
    PLPHandler,
    StreamHandler,
    WATCHDOG_MAX_WORKERS,
)
from validation_service import ValidationService
from report_generator import ReportGenerator
from email_service import EmailService

# ---------------------------------------------------------------------------
# Backward-compatible re-exports so existing callers of
# "from scraper import DatabaseManager / PdpCache / ..." still work.
# ---------------------------------------------------------------------------
from database import DatabaseManager          # noqa: F811
from cache import PdpCache, ProgressTracker   # noqa: F811
from handlers import BasePageHandler          # noqa: F811

# ---------------------------------------------------------------------------
# Logging (configured here once, at the application entry point)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[logging.FileHandler("scraper.log"), logging.StreamHandler()],
)

MOBILE_DEVICE = "iPhone XR"  # logical resolution 390x844, touch, mobile Safari UA
STEALTH = Stealth()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ScraperEngine:
    def __init__(self, urls_file="urls.txt"):
        self.urls_file = urls_file
        self.db = DatabaseManager()
        self.handler_map = {
            "HOME":          HomepageHandler,
            "PLP_PAGES":     PLPHandler,
            "STREAM_PAGES":  StreamHandler,
            "RESULTS_PAGES": StreamHandler,
        }

    def parse_urls(self):
        """Parse urls.txt into a list of (type, url) tuples."""
        import os, re
        tasks = []
        current_type = None

        if not os.path.exists(self.urls_file):
            logging.error(f"URL file {self.urls_file} missing.")
            return []

        with open(self.urls_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                header_match = re.match(r"^\[(.*?)\]$", line)
                if header_match:
                    current_type = header_match.group(1).upper()
                elif line.startswith("http"):
                    if current_type:
                        tasks.append((current_type, line))
                    else:
                        logging.warning(f"URL found without category: {line}")
        return tasks

    def _run_viewport(
        self,
        tasks: list,
        label: str,
        context_kwargs: dict,
        run_id: int,
        pdp_cache: PdpCache = None,
    ):
        """Scrape all tasks under one browser context (one viewport pass)."""
        progress = ProgressTracker(len(tasks), label)
        logging.info(f"[{label.upper()}] ▶  Starting — {len(tasks)} URLs")

        MAX_URL_WORKERS = max(1, WATCHDOG_MAX_WORKERS)
        logging.info(f"[{label.upper()}] Using MAX_WORKERS={MAX_URL_WORKERS}")
        task_chunks = [tasks[i::MAX_URL_WORKERS] for i in range(MAX_URL_WORKERS)]
        task_chunks = [c for c in task_chunks if c]
        if not task_chunks:
            logging.info(f"[{label.upper()}] ✔  No URLs to process")
            return

        def _scrape_worker(worker_tasks: list):
            with sync_playwright() as pw:
                launch_args = [
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ]
                if sys.platform.startswith("linux"):
                    launch_args.insert(0, "--no-sandbox")

                def _run_with_browser(browser_type):
                    try:
                        browser = browser_type.launch(headless=True, args=launch_args)
                    except Exception as exc:
                        logging.warning(
                            f"[{label.upper()}] Unable to launch {browser_type.name}: {exc}"
                        )
                        return False

                    fatal_error = False
                    try:
                        logging.info(
                            f"[{label.upper()}] Using {browser_type.name} "
                            f"for {len(worker_tasks)} URLs"
                        )
                        for tag, url in worker_tasks:
                            prefix = progress.advance()
                            handler_class = self.handler_map.get(tag)
                            if not handler_class:
                                logging.warning(
                                    f"{prefix} ⚠️  No handler for tag [{tag}] "
                                    f"— skipping {url}"
                                )
                                continue

                            logging.info(f"{prefix} 🔄 {url}")
                            t0 = time.time()
                            success = True
                            context = None
                            handler = None

                            try:
                                context = browser.new_context(**context_kwargs)
                                STEALTH.apply_stealth_sync(context)
                                page = context.new_page()
                                handler = handler_class(
                                    page,
                                    self.db,
                                    viewport=label,
                                    run_id=run_id,
                                    pdp_cache=pdp_cache,
                                )
                                handler.scrape(url)
                            except Exception as e:
                                err_msg = str(e)
                                if "Target page, context or browser has been closed" in err_msg:
                                    fatal_error = True
                                    logging.warning(
                                        f"[{label.upper()}] {browser_type.name} crashed "
                                        f"while scraping {url}: {err_msg}"
                                    )
                                    break
                                success = False
                                logging.error(f"{prefix} 💥 Error scraping {url}: {e}")
                                if handler is not None:
                                    handler._capture_artifacts(
                                        handler_class.__name__, url, "exception"
                                    )
                            finally:
                                if context:
                                    context.close()

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
                                        f"({stats['cards']} cards, "
                                        f"{stats['issues']} issue(s), {elapsed:.0f}s)"
                                    )
                        return not fatal_error
                    finally:
                        browser.close()

                for browser_type in (pw.chromium, pw.webkit):
                    if _run_with_browser(browser_type):
                        break
                else:
                    logging.error(
                        f"[{label.upper()}] All supported browsers failed for this worker."
                    )

        with ThreadPoolExecutor(max_workers=len(task_chunks)) as url_pool:
            futures = [url_pool.submit(_scrape_worker, chunk) for chunk in task_chunks]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"[{label.upper()}] Unhandled worker error: {e}")

        logging.info(f"[{label.upper()}] ✔  All {len(tasks)} URLs done")

    def run(self):
        from handlers import (
            WATCHDOG_WAIT_MS, WATCHDOG_RETRIES, WATCHDOG_RETRY_BACKOFF_MS,
            WATCHDOG_FAIL_ON_EMPTY, WATCHDOG_ARTIFACT_DIR,
        )

        tasks = self.parse_urls()
        if not tasks:
            logging.warning("No scraping tasks found.")
            return

        logging.info(
            "Config: WAIT_MS=%s RETRIES=%s BACKOFF_MS=%s MAX_WORKERS=%s "
            "FAIL_ON_EMPTY=%s ARTIFACT_DIR=%s",
            WATCHDOG_WAIT_MS, WATCHDOG_RETRIES, WATCHDOG_RETRY_BACKOFF_MS,
            WATCHDOG_MAX_WORKERS,
            WATCHDOG_FAIL_ON_EMPTY, WATCHDOG_ARTIFACT_DIR,
        )

        run_id     = self.db.create_run()
        start_time = datetime.now()
        url_list   = [url for _, url in tasks]
        pdp_cache  = PdpCache()

        with sync_playwright() as p:
            mobile_kwargs = dict(p.devices[MOBILE_DEVICE])

        DESKTOP_UA = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        viewport_configs = [
            ("desktop", {
                "viewport": {"width": 1920, "height": 1080},
                "user_agent": DESKTOP_UA,
                "locale": "en-IN",
                "extra_http_headers": {"Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8"},
            }),
            ("mobile", mobile_kwargs),
        ]

        logging.info(
            "Starting parallel scrape "
            "(desktop + mobile, URLs in parallel per viewport)..."
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(
                    self._run_viewport, tasks, label, kwargs, run_id, pdp_cache
                ): label
                for label, kwargs in viewport_configs
            }
            for future in as_completed(futures):
                label = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"[{label.upper()}] Pass failed: {e}")

        logging.info(
            f"PDP cache size at end of run: "
            f"{pdp_cache.size()} unique PDP-viewport pairs"
        )

        logging.info("")
        logging.info("Running validation checks across all viewports...")
        validator = ValidationService(self.db.db_name)
        validator.validate_all_courses(run_id=run_id)
        validator.log_results()

        report_file = ReportGenerator(
            validation_service=validator,
            db_name=self.db.db_name,
            start_time=start_time,
            urls_scraped=url_list,
            run_id=run_id,
        ).save()

        EmailService().send_report(
            report_path=report_file,
            validation_summary=validator.get_summary(),
            run_id=run_id,
            start_time=start_time,
        )


if __name__ == "__main__":
    urls_file = sys.argv[1] if len(sys.argv) > 1 else "urls.txt"
    ScraperEngine(urls_file).run()
