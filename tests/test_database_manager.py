"""
Tests for DatabaseManager.

Covers:
- Schema creation: runs and courses tables exist, WAL mode enabled
- create_run: returns int, successive runs get different/increasing IDs
- save_batch: all fields persisted, viewport stored, defaults applied,
  multiple courses in one batch
- get_url_stats: card counts, issue counts (broken / price_mismatch / cta_missing),
  viewport filtering, run_id filtering, unknown URL returns zeros
"""
import sqlite3
import pytest
from database import DatabaseManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def dm(tmp_path):
    return DatabaseManager(str(tmp_path / "test.db"))


def _clean_course(**overrides):
    """Return a minimal valid course dict, with optional field overrides."""
    course = {
        "base_url": "https://example.com/plp",
        "course_name": "Test Course",
        "cta_link": "https://example.com/course",
        "price": "₹1,000",
        "pdp_price": "₹1,000",
        "cta_status": "Found (Enroll Now)",
        "is_broken": 0,
        "price_mismatch": 0,
        "viewport": "desktop",
    }
    course.update(overrides)
    return course


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

class TestSchemaCreation:
    def test_runs_table_exists(self, dm):
        with sqlite3.connect(dm.db_name) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert "runs" in tables

    def test_courses_table_exists(self, dm):
        with sqlite3.connect(dm.db_name) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert "courses" in tables

    def test_wal_mode_enabled(self, dm):
        with sqlite3.connect(dm.db_name) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_reinitialising_does_not_destroy_data(self, dm):
        """Calling _init_db again (CREATE TABLE IF NOT EXISTS) must not lose data."""
        run_id = dm.create_run()
        dm._init_db()  # re-run schema creation
        with sqlite3.connect(dm.db_name) as conn:
            count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# create_run
# ---------------------------------------------------------------------------

class TestCreateRun:
    def test_returns_positive_integer(self, dm):
        run_id = dm.create_run()
        assert isinstance(run_id, int)
        assert run_id > 0

    def test_create_run_stores_mode_and_profile(self, dm):
        rid = dm.create_run(mode="authenticated", profile="JEE")
        with sqlite3.connect(dm.db_name) as conn:
            row = conn.execute(
                "SELECT mode, profile FROM runs WHERE run_id=?", (rid,)
            ).fetchone()
        assert row == ("authenticated", "JEE")

    def test_create_run_defaults_guest(self, dm):
        rid = dm.create_run()
        with sqlite3.connect(dm.db_name) as conn:
            row = conn.execute(
                "SELECT mode, profile FROM runs WHERE run_id=?", (rid,)
            ).fetchone()
        assert row[0] == "guest"
        assert row[1] is None

    def test_successive_runs_have_different_ids(self, dm):
        run1 = dm.create_run()
        run2 = dm.create_run()
        assert run1 != run2

    def test_run_id_is_strictly_increasing(self, dm):
        run1 = dm.create_run()
        run2 = dm.create_run()
        assert run2 > run1

    def test_run_row_inserted_in_runs_table(self, dm):
        run_id = dm.create_run()
        with sqlite3.connect(dm.db_name) as conn:
            row = conn.execute(
                "SELECT run_id FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        assert row is not None


# ---------------------------------------------------------------------------
# save_batch
# ---------------------------------------------------------------------------

class TestSaveBatch:
    def test_course_is_persisted(self, dm):
        run_id = dm.create_run()
        dm.save_batch([_clean_course()], run_id)
        with sqlite3.connect(dm.db_name) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM courses WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        assert count == 1

    def test_all_fields_saved_correctly(self, dm):
        run_id = dm.create_run()
        course = _clean_course(
            course_name="Special Course",
            price="₹2,500",
            pdp_price="₹2,500",
            cta_status="Found (Buy Now)",
            is_broken=0,
            price_mismatch=0,
            viewport="mobile",
        )
        dm.save_batch([course], run_id)
        with sqlite3.connect(dm.db_name) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM courses WHERE run_id=?", (run_id,)
            ).fetchone()
        assert row["base_url"] == "https://example.com/plp"
        assert row["course_name"] == "Special Course"
        assert row["cta_link"] == "https://example.com/course"
        assert row["price"] == "₹2,500"
        assert row["pdp_price"] == "₹2,500"
        assert row["cta_status"] == "Found (Buy Now)"
        assert row["is_broken"] == 0
        assert row["price_mismatch"] == 0
        assert row["viewport"] == "mobile"

    def test_viewport_desktop_stored(self, dm):
        run_id = dm.create_run()
        dm.save_batch([_clean_course(viewport="desktop")], run_id)
        with sqlite3.connect(dm.db_name) as conn:
            vp = conn.execute(
                "SELECT viewport FROM courses WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        assert vp == "desktop"

    def test_viewport_mobile_stored(self, dm):
        run_id = dm.create_run()
        dm.save_batch([_clean_course(viewport="mobile")], run_id)
        with sqlite3.connect(dm.db_name) as conn:
            vp = conn.execute(
                "SELECT viewport FROM courses WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        assert vp == "mobile"

    def test_default_pdp_price_is_na_when_omitted(self, dm):
        run_id = dm.create_run()
        minimal = {
            "base_url": "https://example.com/plp",
            "course_name": "Minimal",
            "cta_link": "https://example.com/course",
            "price": "₹1,000",
            "viewport": "desktop",
        }
        dm.save_batch([minimal], run_id)
        with sqlite3.connect(dm.db_name) as conn:
            pdp_price = conn.execute(
                "SELECT pdp_price FROM courses WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        assert pdp_price == "N/A"

    def test_default_cta_status_is_na_when_omitted(self, dm):
        run_id = dm.create_run()
        minimal = {
            "base_url": "https://example.com/plp",
            "course_name": "Minimal",
            "cta_link": "https://example.com/course",
            "price": "₹1,000",
            "viewport": "desktop",
        }
        dm.save_batch([minimal], run_id)
        with sqlite3.connect(dm.db_name) as conn:
            status = conn.execute(
                "SELECT cta_status FROM courses WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        assert status == "N/A"

    def test_multiple_courses_in_single_batch(self, dm):
        run_id = dm.create_run()
        courses = [_clean_course(course_name=f"Course {i}") for i in range(5)]
        dm.save_batch(courses, run_id)
        with sqlite3.connect(dm.db_name) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM courses WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        assert count == 5

    def test_empty_batch_saves_nothing(self, dm):
        run_id = dm.create_run()
        dm.save_batch([], run_id)
        with sqlite3.connect(dm.db_name) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM courses WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        assert count == 0

    def test_run_id_is_tagged_on_course(self, dm):
        run_id = dm.create_run()
        dm.save_batch([_clean_course()], run_id)
        with sqlite3.connect(dm.db_name) as conn:
            stored_run_id = conn.execute(
                "SELECT run_id FROM courses"
            ).fetchone()[0]
        assert stored_run_id == run_id


# ---------------------------------------------------------------------------
# get_url_stats
# ---------------------------------------------------------------------------

class TestGetUrlStats:
    BASE_URL = "https://example.com/plp"

    def _seed(self, dm, run_id, **overrides):
        dm.save_batch([_clean_course(**overrides)], run_id)

    def test_returns_correct_card_count(self, dm):
        run_id = dm.create_run()
        for i in range(3):
            self._seed(dm, run_id, course_name=f"Course {i}")
        stats = dm.get_url_stats(self.BASE_URL, run_id, "desktop")
        assert stats["cards"] == 3

    def test_clean_course_has_zero_issues(self, dm):
        run_id = dm.create_run()
        self._seed(dm, run_id)
        stats = dm.get_url_stats(self.BASE_URL, run_id, "desktop")
        assert stats["issues"] == 0

    def test_broken_link_counted_as_issue(self, dm):
        run_id = dm.create_run()
        self._seed(dm, run_id, is_broken=1)
        stats = dm.get_url_stats(self.BASE_URL, run_id, "desktop")
        assert stats["issues"] == 1

    def test_price_mismatch_counted_as_issue(self, dm):
        run_id = dm.create_run()
        self._seed(dm, run_id, price_mismatch=1)
        stats = dm.get_url_stats(self.BASE_URL, run_id, "desktop")
        assert stats["issues"] == 1

    def test_cta_not_found_counted_as_issue(self, dm):
        run_id = dm.create_run()
        self._seed(dm, run_id, cta_status="Not Found")
        stats = dm.get_url_stats(self.BASE_URL, run_id, "desktop")
        assert stats["issues"] == 1

    def test_multiple_issue_types_summed(self, dm):
        run_id = dm.create_run()
        self._seed(dm, run_id, is_broken=1, course_name="Broken")
        self._seed(dm, run_id, price_mismatch=1, course_name="Mismatch")
        self._seed(dm, run_id, cta_status="Not Found", course_name="NoBtn")
        stats = dm.get_url_stats(self.BASE_URL, run_id, "desktop")
        assert stats["issues"] == 3

    def test_filters_by_viewport_desktop(self, dm):
        run_id = dm.create_run()
        self._seed(dm, run_id, viewport="desktop", is_broken=1, course_name="D")
        self._seed(dm, run_id, viewport="mobile", course_name="M")
        stats = dm.get_url_stats(self.BASE_URL, run_id, "desktop")
        assert stats["cards"] == 1
        assert stats["issues"] == 1

    def test_filters_by_viewport_mobile(self, dm):
        run_id = dm.create_run()
        self._seed(dm, run_id, viewport="desktop", is_broken=1, course_name="D")
        self._seed(dm, run_id, viewport="mobile", course_name="M")
        stats = dm.get_url_stats(self.BASE_URL, run_id, "mobile")
        assert stats["cards"] == 1
        assert stats["issues"] == 0

    def test_filters_by_run_id(self, dm):
        run1 = dm.create_run()
        run2 = dm.create_run()
        self._seed(dm, run1, is_broken=1, course_name="Run1 Bad")
        self._seed(dm, run2, course_name="Run2 Clean")
        stats_run2 = dm.get_url_stats(self.BASE_URL, run2, "desktop")
        assert stats_run2["issues"] == 0

    def test_unknown_url_returns_zeros(self, dm):
        run_id = dm.create_run()
        stats = dm.get_url_stats("https://nonexistent.com", run_id, "desktop")
        assert stats["cards"] == 0
        assert stats["issues"] == 0
