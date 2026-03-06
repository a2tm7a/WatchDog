"""
DatabaseManager
SQLite persistence layer for WatchDog.

WAL journal mode allows concurrent reads while a write is in progress,
which is important when desktop and mobile viewport threads save batches
at the same time.
"""

import sqlite3
import logging


class DatabaseManager:
    def __init__(self, db_name="scraped_data.db"):
        self.db_name = db_name
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute('''
                CREATE TABLE IF NOT EXISTS runs (
                    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS courses (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id         INTEGER REFERENCES runs(run_id),
                    base_url       TEXT,
                    course_name    TEXT,
                    cta_link       TEXT,
                    price          TEXT,
                    pdp_price      TEXT,
                    cta_status     TEXT,
                    is_broken      INTEGER DEFAULT 0,
                    price_mismatch INTEGER DEFAULT 0,
                    viewport       TEXT DEFAULT 'desktop',
                    timestamp      DATETIME DEFAULT CURRENT_TIMESTAMP
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
        """Persist a batch of scraped courses tagged with *run_id*."""
        # timeout=30 ensures threads wait for the write lock instead of crashing
        with sqlite3.connect(self.db_name, timeout=30) as conn:
            cursor = conn.cursor()
            new_items = 0
            for item in courses:
                viewport = item.get("viewport", "desktop")
                cursor.execute(
                    """
                    INSERT INTO courses
                        (run_id, base_url, course_name, cta_link, price,
                         pdp_price, cta_status, is_broken, price_mismatch, viewport)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        item["base_url"],
                        item["course_name"],
                        item["cta_link"],
                        item["price"],
                        item.get("pdp_price", "N/A"),
                        item.get("cta_status", "N/A"),
                        item.get("is_broken", 0),
                        item.get("price_mismatch", 0),
                        viewport,
                    ),
                )
                new_items += 1
            conn.commit()
            if new_items > 0:
                logging.debug(
                    f"[{viewport}] Saved {new_items} courses (run #{run_id})."
                )

    def get_url_stats(self, base_url: str, run_id: int, viewport: str) -> dict:
        """Return total card count + issue count for a URL in this run/viewport.

        Issues = broken links + price mismatches + missing CTA buttons.
        All three flags must agree with the validation report.
        """
        with sqlite3.connect(self.db_name, timeout=30) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(is_broken),
                    SUM(price_mismatch),
                    SUM(CASE WHEN cta_status = 'Not Found' THEN 1 ELSE 0 END)
                FROM courses
                WHERE base_url=? AND run_id=? AND viewport=?
                """,
                (base_url, run_id, viewport),
            )
            row = cursor.fetchone()
            total       = row[0] or 0
            broken      = row[1] or 0
            mismatch    = row[2] or 0
            cta_missing = row[3] or 0
            return {"cards": total, "issues": broken + mismatch + cta_missing}
