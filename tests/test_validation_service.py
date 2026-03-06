"""
Tests for ValidationService.

Covers:
- validate_course: clean course, broken CTA, price mismatch
- validate_all_courses: empty DB, populates results, run_id scoping,
  viewport stamping from DB row, stores results on instance
- get_summary: empty, counts by type, counts by severity
- get_issues_by_severity / get_issues_by_type: filtering and empty cases
- log_results: deduplication by (course_name, type, viewport)
"""
import logging
import pytest
from database import DatabaseManager
from validation_service import ValidationService


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_vs_no_db():
    """ValidationService instance that does not need a real DB for unit tests."""
    vs = ValidationService.__new__(ValidationService)
    vs.db_name = ":memory:"
    vs.validator_chain = vs._build_default_validator_chain()
    vs.validation_results = []
    return vs


@pytest.fixture
def populated_db(tmp_path):
    """DB pre-seeded with one run containing 4 diverse courses."""
    db_path = str(tmp_path / "test.db")
    dm = DatabaseManager(db_path)
    run_id = dm.create_run()
    courses = [
        {   # clean — no issues
            "base_url": "https://example.com",
            "course_name": "Clean Course",
            "cta_link": "https://example.com/clean",
            "price": "₹1,000",
            "pdp_price": "₹1,000",
            "cta_status": "Found (Enroll Now)",
            "is_broken": 0,
            "price_mismatch": 0,
            "viewport": "desktop",
        },
        {   # broken CTA → CTA_BROKEN CRITICAL
            "base_url": "https://example.com",
            "course_name": "Broken CTA Course",
            "cta_link": "N/A",
            "price": "₹2,000",
            "pdp_price": "N/A",
            "cta_status": "N/A",
            "is_broken": 1,
            "price_mismatch": 0,
            "viewport": "desktop",
        },
        {   # price mismatch → PRICE_MISMATCH MEDIUM
            "base_url": "https://example.com",
            "course_name": "Mismatch Course",
            "cta_link": "https://example.com/mismatch",
            "price": "₹3,000",
            "pdp_price": "₹5,000",
            "cta_status": "Found (Enroll Now)",
            "is_broken": 0,
            "price_mismatch": 1,
            "viewport": "mobile",
        },
        {   # missing button → CTA_MISSING HIGH
            "base_url": "https://example.com",
            "course_name": "No Button Course",
            "cta_link": "https://example.com/no-button",
            "price": "₹4,000",
            "pdp_price": "₹4,000",
            "cta_status": "Not Found",
            "is_broken": 0,
            "price_mismatch": 0,
            "viewport": "mobile",
        },
    ]
    dm.save_batch(courses, run_id)
    return db_path, run_id


# ---------------------------------------------------------------------------
# validate_course
# ---------------------------------------------------------------------------

class TestValidateCourse:
    def test_clean_course_returns_no_issues(self):
        vs = _make_vs_no_db()
        issues = vs.validate_course({
            "course_name": "Clean",
            "base_url": "https://example.com",
            "cta_link": "https://example.com/course",
            "is_broken": 0,
            "cta_status": "Found (Enroll Now)",
            "price": "₹1,000",
            "pdp_price": "₹1,000",
            "viewport": "desktop",
        })
        assert issues == []

    def test_broken_cta_returns_critical_issue(self):
        vs = _make_vs_no_db()
        issues = vs.validate_course({
            "course_name": "Broken",
            "cta_link": "N/A",
            "is_broken": 1,
            "viewport": "desktop",
            "base_url": "https://example.com",
        })
        assert any(i.severity == "CRITICAL" for i in issues)

    def test_price_mismatch_returns_medium_issue(self):
        vs = _make_vs_no_db()
        issues = vs.validate_course({
            "course_name": "Mismatch",
            "cta_link": "https://example.com/course",
            "is_broken": 0,
            "cta_status": "Found (Enroll Now)",
            "price": "₹1,000",
            "pdp_price": "₹2,000",
            "viewport": "desktop",
            "base_url": "https://example.com",
        })
        assert any(i.severity == "MEDIUM" and i.type == "PRICE_MISMATCH" for i in issues)

    def test_missing_cta_button_returns_high_issue(self):
        vs = _make_vs_no_db()
        issues = vs.validate_course({
            "course_name": "NoButton",
            "cta_link": "https://example.com/course",
            "is_broken": 0,
            "cta_status": "Not Found",
            "price": "₹1,000",
            "pdp_price": "₹1,000",
            "viewport": "mobile",
            "base_url": "https://example.com",
        })
        assert any(i.severity == "HIGH" and i.type == "CTA_MISSING" for i in issues)


# ---------------------------------------------------------------------------
# validate_all_courses
# ---------------------------------------------------------------------------

class TestValidateAllCourses:
    def test_empty_db_returns_empty_list(self, tmp_path):
        db_path = str(tmp_path / "empty.db")
        DatabaseManager(db_path)  # create schema only
        vs = ValidationService(db_path)
        assert vs.validate_all_courses() == []

    def test_returns_issues_for_bad_courses(self, populated_db):
        db_path, run_id = populated_db
        vs = ValidationService(db_path)
        results = vs.validate_all_courses(run_id=run_id)
        assert len(results) > 0

    def test_run_id_filter_excludes_other_runs(self, tmp_path):
        """Courses from a different run must not appear in results."""
        db_path = str(tmp_path / "multi.db")
        dm = DatabaseManager(db_path)
        run1 = dm.create_run()
        run2 = dm.create_run()

        dm.save_batch([{
            "base_url": "https://example.com",
            "course_name": "Run1 Bad",
            "cta_link": "N/A",
            "price": "N/A",
            "pdp_price": "N/A",
            "cta_status": "N/A",
            "is_broken": 1,
            "price_mismatch": 0,
            "viewport": "desktop",
        }], run1)

        dm.save_batch([{
            "base_url": "https://example.com",
            "course_name": "Run2 Clean",
            "cta_link": "https://example.com/course",
            "price": "₹1,000",
            "pdp_price": "₹1,000",
            "cta_status": "Found (Enroll Now)",
            "is_broken": 0,
            "price_mismatch": 0,
            "viewport": "desktop",
        }], run2)

        vs = ValidationService(db_path)
        results = vs.validate_all_courses(run_id=run2)
        # run2 only has a clean course → no issues
        assert results == []

    def test_viewport_stamped_from_db_row(self, populated_db):
        db_path, run_id = populated_db
        vs = ValidationService(db_path)
        results = vs.validate_all_courses(run_id=run_id)
        for r in results:
            assert r.viewport in ("desktop", "mobile")

    def test_stores_results_on_instance(self, populated_db):
        db_path, run_id = populated_db
        vs = ValidationService(db_path)
        results = vs.validate_all_courses(run_id=run_id)
        assert vs.validation_results is results

    def test_no_run_id_validates_all_rows(self, tmp_path):
        """Calling without run_id should validate every course in the DB."""
        db_path = str(tmp_path / "all.db")
        dm = DatabaseManager(db_path)
        run1 = dm.create_run()
        run2 = dm.create_run()
        bad = {
            "base_url": "https://example.com",
            "course_name": "Bad",
            "cta_link": "N/A",
            "price": "N/A",
            "pdp_price": "N/A",
            "cta_status": "N/A",
            "is_broken": 1,
            "price_mismatch": 0,
            "viewport": "desktop",
        }
        dm.save_batch([bad], run1)
        dm.save_batch([{**bad, "course_name": "Bad2"}], run2)
        vs = ValidationService(db_path)
        results = vs.validate_all_courses()
        course_names = {r.course_name for r in results}
        assert "Bad" in course_names
        assert "Bad2" in course_names


# ---------------------------------------------------------------------------
# get_summary
# ---------------------------------------------------------------------------

class TestGetSummary:
    def test_empty_results_returns_zero_summary(self, tmp_path):
        vs = ValidationService(str(tmp_path / "x.db"))
        summary = vs.get_summary()
        assert summary["total_issues"] == 0
        assert summary["by_type"] == {}
        assert summary["by_severity"] == {}

    def test_total_issues_equals_result_count(self, populated_db):
        db_path, run_id = populated_db
        vs = ValidationService(db_path)
        vs.validate_all_courses(run_id=run_id)
        assert vs.get_summary()["total_issues"] == len(vs.validation_results)

    def test_by_type_contains_cta_broken(self, populated_db):
        db_path, run_id = populated_db
        vs = ValidationService(db_path)
        vs.validate_all_courses(run_id=run_id)
        assert "CTA_BROKEN" in vs.get_summary()["by_type"]

    def test_by_type_contains_price_mismatch(self, populated_db):
        db_path, run_id = populated_db
        vs = ValidationService(db_path)
        vs.validate_all_courses(run_id=run_id)
        assert "PRICE_MISMATCH" in vs.get_summary()["by_type"]

    def test_by_severity_contains_critical(self, populated_db):
        db_path, run_id = populated_db
        vs = ValidationService(db_path)
        vs.validate_all_courses(run_id=run_id)
        assert "CRITICAL" in vs.get_summary()["by_severity"]

    def test_by_severity_counts_are_positive(self, populated_db):
        db_path, run_id = populated_db
        vs = ValidationService(db_path)
        vs.validate_all_courses(run_id=run_id)
        for count in vs.get_summary()["by_severity"].values():
            assert count > 0


# ---------------------------------------------------------------------------
# get_issues_by_severity / get_issues_by_type
# ---------------------------------------------------------------------------

class TestFilterMethods:
    def test_get_issues_by_severity_returns_only_matching(self, populated_db):
        db_path, run_id = populated_db
        vs = ValidationService(db_path)
        vs.validate_all_courses(run_id=run_id)
        criticals = vs.get_issues_by_severity("CRITICAL")
        assert all(r.severity == "CRITICAL" for r in criticals)
        assert len(criticals) > 0

    def test_get_issues_by_severity_empty_when_none(self, tmp_path):
        vs = ValidationService(str(tmp_path / "x.db"))
        assert vs.get_issues_by_severity("CRITICAL") == []

    def test_get_issues_by_type_returns_only_matching(self, populated_db):
        db_path, run_id = populated_db
        vs = ValidationService(db_path)
        vs.validate_all_courses(run_id=run_id)
        mismatches = vs.get_issues_by_type("PRICE_MISMATCH")
        assert all(r.type == "PRICE_MISMATCH" for r in mismatches)
        assert len(mismatches) > 0

    def test_get_issues_by_type_empty_when_none(self, tmp_path):
        vs = ValidationService(str(tmp_path / "x.db"))
        assert vs.get_issues_by_type("PRICE_MISMATCH") == []

    def test_nonexistent_severity_returns_empty(self, populated_db):
        db_path, run_id = populated_db
        vs = ValidationService(db_path)
        vs.validate_all_courses(run_id=run_id)
        assert vs.get_issues_by_severity("NONEXISTENT") == []


# ---------------------------------------------------------------------------
# log_results / deduplication
# ---------------------------------------------------------------------------

class TestLogResults:
    def test_logs_no_issues_message_when_empty(self, tmp_path, caplog):
        vs = ValidationService(str(tmp_path / "x.db"))
        with caplog.at_level(logging.INFO):
            vs.log_results()
        assert "No validation issues" in caplog.text

    def test_deduplicates_same_course_type_viewport(self, tmp_path, caplog):
        """Two rows with the same (course_name, type, viewport) → 1 unique in log."""
        db_path = str(tmp_path / "dedup.db")
        dm = DatabaseManager(db_path)
        run_id = dm.create_run()
        bad_course = {
            "base_url": "https://a.com",
            "course_name": "Dup Course",
            "cta_link": "N/A",
            "price": "N/A",
            "pdp_price": "N/A",
            "cta_status": "N/A",
            "is_broken": 1,
            "price_mismatch": 0,
            "viewport": "desktop",
        }
        # Same course scraped from two different listing pages
        dm.save_batch([bad_course], run_id)
        dm.save_batch([{**bad_course, "base_url": "https://b.com"}], run_id)

        vs = ValidationService(db_path)
        vs.validate_all_courses(run_id=run_id)

        with caplog.at_level(logging.INFO):
            vs.log_results()

        # 2 raw issues, 1 unique after dedup
        assert "2 unique" in caplog.text or "1 unique" in caplog.text
        # Specifically it should say "1 unique"
        assert "1 unique" in caplog.text

    def test_different_viewports_not_deduplicated(self, tmp_path, caplog):
        """Same course on desktop and mobile are different entries — both kept."""
        db_path = str(tmp_path / "vp.db")
        dm = DatabaseManager(db_path)
        run_id = dm.create_run()
        base = {
            "base_url": "https://example.com",
            "course_name": "VP Course",
            "cta_link": "N/A",
            "price": "N/A",
            "pdp_price": "N/A",
            "cta_status": "N/A",
            "is_broken": 1,
            "price_mismatch": 0,
        }
        dm.save_batch([{**base, "viewport": "desktop"}], run_id)
        dm.save_batch([{**base, "viewport": "mobile"}], run_id)

        vs = ValidationService(db_path)
        vs.validate_all_courses(run_id=run_id)

        with caplog.at_level(logging.INFO):
            vs.log_results()

        # 2 raw issues, 2 unique (desktop + mobile are separate)
        assert "2 unique" in caplog.text
