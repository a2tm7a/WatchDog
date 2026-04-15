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
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

# pyre-ignore-all-errors[21]  -- local script modules are not installed packages;
#                                Pyre2 cannot resolve them without explicit source roots.
from playwright.sync_api import sync_playwright  # pyre-fixme[21]
from playwright_stealth import Stealth           # pyre-fixme[21]

from database import DatabaseManager            # pyre-fixme[21]
from cache import PdpCache, ProgressTracker     # pyre-fixme[21]
from handlers import (  # type: ignore[import]
    HomepageHandler,
    PLPHandler,
    StreamHandler,
    WATCHDOG_MAX_WORKERS,
    WATCHDOG_WAIT_MS,
    WATCHDOG_RETRIES,
    WATCHDOG_RETRY_BACKOFF_MS,
    WATCHDOG_FAIL_ON_EMPTY,
    WATCHDOG_ARTIFACT_DIR,
)
from validation_service import ValidationService  # type: ignore[import]
from check_config import CheckConfig  # type: ignore[import]
from report_generator import ReportGenerator      # type: ignore[import]
from email_service import EmailService            # type: ignore[import]
from auth_session import AuthSession              # type: ignore[import]

# ---------------------------------------------------------------------------
# Backward-compatible re-exports so existing callers of
# "from scraper import DatabaseManager / PdpCache / ..." still work.
# ---------------------------------------------------------------------------
from database import DatabaseManager          # noqa: F811  # type: ignore[import]
from cache import PdpCache, ProgressTracker   # noqa: F811  # type: ignore[import]
from handlers import BasePageHandler          # noqa: F811  # type: ignore[import]

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

# Stream profiles for authenticated scraping (Phase 2)
AUTH_PROFILES = ["JEE", "NEET", "Classes610"]


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

    def parse_urls(self) -> List[Tuple[str, str]]:
        """Parse urls.txt into a list of (page_type, url) tuples."""
        tasks: List[Tuple[str, str]] = []
        current_type: Optional[str] = None

        if not os.path.exists(self.urls_file):
            logging.error(f"URL file {self.urls_file} missing.")
            return []

        with open(self.urls_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                header_match = re.match(r"^\[(.*?)\]$", line)
                if header_match is not None:  # narrow Optional[re.Match] -> re.Match
                    current_type = header_match.group(1).upper()
                elif line.startswith("http"):
                    if current_type is not None:  # pyre-fixme[5]: narrow str | None -> str
                        tasks.append((current_type, line))
                    else:
                        logging.warning(f"URL found without category: {line}")
        return tasks

    def _run_viewport(
        self,
        tasks: List[Tuple[str, str]],
        label: str,
        context_kwargs: Dict[str, object],
        run_id: int,
        pdp_cache: Optional[PdpCache] = None,
    ) -> None:
        """Scrape all tasks under one browser context (one viewport pass)."""
        progress = ProgressTracker(len(tasks), label)
        logging.info(f"[{label.upper()}] ▶  Starting — {len(tasks)} URLs")

        MAX_URL_WORKERS = max(1, WATCHDOG_MAX_WORKERS)
        logging.info(f"[{label.upper()}] Using MAX_WORKERS={MAX_URL_WORKERS}")
        task_chunks: List[List[Tuple[str, str]]] = [list(tasks[i::MAX_URL_WORKERS]) for i in range(MAX_URL_WORKERS)]  # type: ignore[index]
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
            futures = [url_pool.submit(_scrape_worker, chunk) for chunk in task_chunks]  # type: ignore[arg-type]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"[{label.upper()}] Unhandled worker error: {e}")

        logging.info(f"[{label.upper()}] ✔  All {len(tasks)} URLs done")

    def recheck_failing_urls(
        self,
        failing_issues: list,
        run_id: int,
        mobile_kwargs: dict,
    ) -> None:
        """
        Re-scrape only the (base_url, viewport) pairs that had at least one
        validation issue in the first pass.

        This lets us distinguish genuine issues from transient technical
        failures (bot-detection blips, network timeouts, etc.)  Any course
        that passes on the second scrape will have its DB row updated with
        clean data, so the subsequent validation call will no longer flag it.

        Args:
            failing_issues: List[ValidationResult] from the first pass.
            run_id:         Current run identifier (scopes DB updates).
            mobile_kwargs:  Playwright device descriptor for the mobile context.
        """
        # Collect unique (base_url, viewport) pairs that need a recheck
        failing_pairs: set = set()
        for issue in failing_issues:
            base_url = getattr(issue, 'base_url', None)
            viewport = getattr(issue, 'viewport', 'desktop')
            if base_url and base_url not in ('Unknown', 'Unknown URL'):
                failing_pairs.add((base_url, viewport))

        if not failing_pairs:
            logging.info("[RECHECK] No failing URLs to re-scrape — skipping recheck pass.")
            return

        logging.info("")
        logging.info("=" * 60)
        logging.info(f"[RECHECK] Re-scraping {len(failing_pairs)} failing URL+viewport pair(s)...")
        logging.info("=" * 60)

        DESKTOP_UA = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        viewport_context_map = {
            "desktop": {
                "viewport": {"width": 1920, "height": 1080},
                "user_agent": DESKTOP_UA,
                "locale": "en-IN",
                "extra_http_headers": {"Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8"},
            },
            "mobile": mobile_kwargs,
        }

        # Group by viewport so we open one browser context per viewport type
        by_viewport: dict = {}
        for base_url, viewport in failing_pairs:
            # Infer the page type (tag) from what was originally used
            tag = None
            for t, u in self.parse_urls():
                if u == base_url:
                    tag = t
                    break
            if tag is None:
                # Best-effort fallback: guess from URL pattern
                if "/online-coaching-" in base_url or "/neet/" in base_url:
                    tag = "PLP_PAGES"
                elif "/international-olympiads" in base_url:
                    tag = "STREAM_PAGES"
                elif base_url.strip("/") == "https://allen.in":
                    tag = "HOME"
                else:
                    tag = "STREAM_PAGES"  # Fallback — handles RESULTS_PAGES too
            by_viewport.setdefault(viewport, []).append((tag, base_url))

        # Use a fresh cache so stale first-pass results don't bleed in
        recheck_cache = PdpCache()

        def _delete_old_rows(viewport_label: str, urls: list):
            """Remove the first-pass rows for these URLs so fresh data replaces them."""
            import sqlite3
            placeholders = ",".join(["?"] * len(urls))
            with sqlite3.connect(self.db.db_name, timeout=30) as conn:
                conn.execute(
                    f"DELETE FROM courses "
                    f"WHERE run_id=? AND viewport=? AND base_url IN ({placeholders})",
                    [run_id, viewport_label] + urls,
                )
                conn.commit()
            logging.info(
                f"[RECHECK][{viewport_label.upper()}] "
                f"Deleted {len(urls)} old row(s) for re-scrape."
            )

        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        with ThreadPoolExecutor(max_workers=len(by_viewport)) as pool:
            futures = {}
            for vp_label, tasks in by_viewport.items():
                context_kwargs = viewport_context_map.get(vp_label, viewport_context_map["desktop"])
                urls_only = [u for _, u in tasks]
                _delete_old_rows(vp_label, urls_only)
                f = pool.submit(  # type: ignore[arg-type]
                    self._run_viewport, tasks, vp_label, context_kwargs, run_id, recheck_cache
                )
                futures[f] = vp_label
            for future in _as_completed(futures):
                vp_label = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"[RECHECK][{vp_label.upper()}] Recheck pass failed: {e}")

        logging.info("[RECHECK] ✔  Re-scrape complete.")

    def run(self) -> None:
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
                pool.submit(  # type: ignore[arg-type]
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
        check_config = CheckConfig.load("config/url_checks.yaml")
        validator = ValidationService(self.db.db_name)
        first_pass_issues = validator.validate_all_courses(run_id=run_id, check_config=check_config)
        first_pass_count  = len(first_pass_issues)
        validator.log_results()

        # ------------------------------------------------------------------
        # Re-QC pass: re-scrape every failing (URL, viewport) pair once more.
        # Transient technical failures (bot-detect blips, timeouts) often
        # self-heal on a second attempt.  We update the DB rows in-place so
        # the final validation reflects only genuine, persistent issues.
        # ------------------------------------------------------------------
        with sync_playwright() as p:
            mobile_kwargs = dict(p.devices[MOBILE_DEVICE])

        self.recheck_failing_urls(
            failing_issues=first_pass_issues,
            run_id=run_id,
            mobile_kwargs=mobile_kwargs,
        )

        logging.info("")
        logging.info("[RECHECK] Running final validation after re-check pass...")
        final_validator = ValidationService(self.db.db_name)
        final_pass_issues = final_validator.validate_all_courses(run_id=run_id, check_config=check_config)
        final_pass_count  = len(final_pass_issues)
        final_validator.log_results()

        cleared_count = max(0, first_pass_count - final_pass_count)
        logging.info(
            f"[RECHECK] First pass: {first_pass_count} issue(s) | "
            f"After recheck: {final_pass_count} issue(s) | "
            f"Cleared on recheck: {cleared_count}"
        )

        report_file = ReportGenerator(
            validation_service=final_validator,
            db_name=self.db.db_name,
            start_time=start_time,
            urls_scraped=url_list,
            run_id=run_id,
            recheck_stats={
                "first_pass_issues":  first_pass_count,
                "final_pass_issues":  final_pass_count,
                "cleared_on_recheck": cleared_count,
            },
        ).save()

        EmailService().send_report(
            report_path=report_file,
            validation_summary=final_validator.get_summary(),
            run_id=run_id,
            start_time=start_time,
        )

        # -----------------------------------------------------------------------
        # Phase 2 — Authenticated mode: one run per stream profile
        # -----------------------------------------------------------------------
        logging.info("")
        logging.info("Starting authenticated runs (%d profiles)...", len(AUTH_PROFILES))

        with sync_playwright() as p_auth:
            mobile_kwargs_auth = dict(p_auth.devices[MOBILE_DEVICE])

            # Launch a single browser for all authenticated profiles
            auth_browser = p_auth.chromium.launch(headless=True)
            auth_context = auth_browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=DESKTOP_UA,
                locale="en-IN",
                extra_http_headers={"Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8"},
            )
            STEALTH.apply_stealth_sync(auth_context)

            session = AuthSession(auth_context)
            try:
                session.login()
            except RuntimeError as login_err:
                logging.error("[AUTH] Login failed — skipping all authenticated runs: %s", login_err)
                auth_browser.close()
            else:
                for profile in AUTH_PROFILES:
                    logging.info("")
                    logging.info("=== Authenticated run: %s ===", profile)

                    try:
                        session.switch_profile(profile)
                    except Exception as switch_err:
                        logging.error(
                            "[AUTH:%s] Profile switch failed — skipping: %s", profile, switch_err
                        )
                        continue

                    auth_run_id   = self.db.create_run(mode="authenticated", profile=profile)
                    auth_cache    = PdpCache()  # fresh cache per profile — no guest bleed
                    auth_start    = datetime.now()

                    # Build per-viewport context kwargs using storage_state from
                    # the authenticated context so both viewports share the session
                    try:
                        storage = auth_context.storage_state()
                    except Exception:
                        storage = None

                    auth_desktop_kwargs: Dict[str, object] = {
                        "viewport":            {"width": 1920, "height": 1080},
                        "user_agent":          DESKTOP_UA,
                        "locale":              "en-IN",
                        "extra_http_headers":  {"Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8"},
                    }
                    auth_mobile_kwargs: Dict[str, object] = dict(mobile_kwargs_auth)
                    if storage:
                        auth_desktop_kwargs["storage_state"] = storage
                        auth_mobile_kwargs["storage_state"]  = storage

                    auth_viewport_configs = [
                        ("desktop", auth_desktop_kwargs),
                        ("mobile",  auth_mobile_kwargs),
                    ]

                    with ThreadPoolExecutor(max_workers=2) as auth_pool:
                        auth_futures = {
                            auth_pool.submit(
                                self._run_viewport, tasks, label, kwargs,
                                auth_run_id, auth_cache
                            ): label
                            for label, kwargs in auth_viewport_configs
                        }
                        for fut in as_completed(auth_futures):
                            lbl = auth_futures[fut]
                            try:
                                fut.result()
                            except Exception as e:
                                logging.error("[AUTH:%s][%s] Viewport failed: %s", profile, lbl.upper(), e)

                    logging.info("[AUTH:%s] Validating...", profile)
                    auth_validator = ValidationService(self.db.db_name)
                    auth_issues    = auth_validator.validate_all_courses(run_id=auth_run_id)
                    auth_validator.log_results()

                    # Re-QC pass for authenticated run
                    auth_recheck_issues: list = []
                    auth_recheck_count = len(auth_issues)
                    if auth_recheck_count:
                        logging.info(
                            "[AUTH:%s] Re-QC: re-scraping %d failing pairs...",
                            profile, auth_recheck_count,
                        )
                        recheck_cache = ProgressTracker()
                        with ThreadPoolExecutor(max_workers=2) as rpool:
                            rfutures = {
                                rpool.submit(
                                    self._run_viewport, tasks, label,
                                    auth_desktop_kwargs if label == "desktop" else auth_mobile_kwargs,
                                    auth_run_id, auth_cache, recheck_cache
                                ): label
                                for label in ("desktop", "mobile")
                            }
                            for rfut in as_completed(rfutures):
                                try:
                                    rfut.result()
                                except Exception as re:
                                    logging.error("[AUTH:%s] Re-QC viewport failed: %s", profile, re)

                        final_validator = ValidationService(self.db.db_name)
                        auth_recheck_issues = final_validator.validate_all_courses(run_id=auth_run_id)
                        final_validator.log_results()

                    # Use final_validator if re-QC ran, otherwise auth_validator
                    _report_validator = final_validator if auth_recheck_count else auth_validator
                    auth_recheck_stats = {
                        "first_pass_issues":  len(auth_issues),
                        "final_pass_issues":  len(auth_recheck_issues),
                        "cleared_on_recheck": max(0, len(auth_issues) - len(auth_recheck_issues)),
                    } if auth_recheck_count else {}

                    auth_report_path = ReportGenerator(
                        validation_service=_report_validator,
                        db_name=self.db.db_name,
                        start_time=auth_start,
                        urls_scraped=[url for _, url in tasks],
                        run_id=auth_run_id,
                        recheck_stats=auth_recheck_stats,
                        mode="authenticated",
                        profile=profile,
                    ).save()
                    logging.info("[AUTH:%s] Report: %s", profile, auth_report_path)

                    auth_email = EmailService()
                    auth_email.send_report(
                        report_path=auth_report_path,
                        validation_summary=_report_validator.get_summary(),
                        run_id=auth_run_id,
                        start_time=auth_start,
                        profile=profile,
                    )

                session.close()
                auth_browser.close()


if __name__ == "__main__":
    urls_file = sys.argv[1] if len(sys.argv) > 1 else "urls.txt"
    ScraperEngine(urls_file).run()
