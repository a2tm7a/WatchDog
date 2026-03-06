"""
Tests for ReportGenerator.

Covers:
- save(): file created, filename uses start_time, file has content
- _section_header: title, duration, URL count, URL list
- _section_url_summary: no-issues message, issue table, counts
- _section_issue_breakdown: no-issues message, type names, severity names
- _section_details: no-issues message, course names, pipe escaping, grouping
- _query_db_stats: viewport grouping, field accuracy, empty DB
"""
import os
import pytest
from datetime import datetime, timedelta
from validators import ValidationResult
from scraper import DatabaseManager
from validation_service import ValidationService
from report_generator import ReportGenerator
import report_generator as rg_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def report_env(tmp_path, monkeypatch):
    """DB + ValidationService + ReportGenerator, pointing reports at tmp_path."""
    monkeypatch.setattr(rg_module, "REPORTS_DIR", str(tmp_path / "reports"))

    db_path = str(tmp_path / "test.db")
    dm = DatabaseManager(db_path)
    run_id = dm.create_run()

    courses = [
        {   # clean — desktop
            "base_url": "https://example.com/plp",
            "course_name": "Clean Course",
            "cta_link": "https://example.com/clean",
            "price": "₹1,000",
            "pdp_price": "₹1,000",
            "cta_status": "Found (Enroll Now)",
            "is_broken": 0,
            "price_mismatch": 0,
            "viewport": "desktop",
        },
        {   # broken CTA — desktop
            "base_url": "https://example.com/plp",
            "course_name": "Broken Course",
            "cta_link": "N/A",
            "price": "₹2,000",
            "pdp_price": "N/A",
            "cta_status": "N/A",
            "is_broken": 1,
            "price_mismatch": 0,
            "viewport": "desktop",
        },
        {   # price mismatch — mobile
            "base_url": "https://example.com/plp",
            "course_name": "Mismatch Course",
            "cta_link": "https://example.com/mismatch",
            "price": "₹3,000",
            "pdp_price": "₹5,000",
            "cta_status": "Found (Enroll Now)",
            "is_broken": 0,
            "price_mismatch": 1,
            "viewport": "mobile",
        },
    ]
    dm.save_batch(courses, run_id)

    vs = ValidationService(db_path)
    vs.validate_all_courses(run_id=run_id)

    start_time = datetime(2024, 1, 15, 10, 0, 0)
    generator = ReportGenerator(
        validation_service=vs,
        db_name=db_path,
        start_time=start_time,
        urls_scraped=["https://example.com/plp"],
        run_id=run_id,
    )
    return generator, tmp_path


@pytest.fixture
def empty_report_env(tmp_path, monkeypatch):
    """ReportGenerator backed by an empty DB (no courses, no issues)."""
    monkeypatch.setattr(rg_module, "REPORTS_DIR", str(tmp_path / "reports"))
    db_path = str(tmp_path / "empty.db")
    dm = DatabaseManager(db_path)
    run_id = dm.create_run()
    vs = ValidationService(db_path)
    vs.validate_all_courses(run_id=run_id)
    generator = ReportGenerator(
        validation_service=vs,
        db_name=db_path,
        start_time=datetime(2024, 6, 1, 8, 0, 0),
        urls_scraped=["https://example.com"],
        run_id=run_id,
    )
    return generator, tmp_path


# ---------------------------------------------------------------------------
# save()
# ---------------------------------------------------------------------------

class TestSave:
    def test_creates_file(self, report_env):
        gen, _ = report_env
        path = gen.save()
        assert os.path.exists(path)

    def test_filename_uses_start_time(self, report_env):
        gen, _ = report_env
        path = gen.save()
        assert "2024-01-15_10-00-00" in os.path.basename(path)

    def test_file_has_content(self, report_env):
        gen, _ = report_env
        path = gen.save()
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert len(content) > 50

    def test_file_ends_with_newline(self, report_env):
        gen, _ = report_env
        path = gen.save()
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert content.endswith("\n")


# ---------------------------------------------------------------------------
# _section_header
# ---------------------------------------------------------------------------

class TestSectionHeader:
    def test_contains_title(self, report_env):
        gen, _ = report_env
        assert "WatchDog Run Report" in gen._section_header("5m 30s")

    def test_contains_duration_string(self, report_env):
        gen, _ = report_env
        assert "5m 30s" in gen._section_header("5m 30s")

    def test_contains_url_count(self, report_env):
        gen, _ = report_env
        section = gen._section_header("1m 0s")
        assert "1" in section  # 1 URL scraped

    def test_lists_scraped_url(self, report_env):
        gen, _ = report_env
        assert "https://example.com/plp" in gen._section_header("1m 0s")

    def test_contains_date(self, report_env):
        gen, _ = report_env
        assert "2024-01-15" in gen._section_header("0m 0s")


# ---------------------------------------------------------------------------
# _section_url_summary
# ---------------------------------------------------------------------------

class TestSectionUrlSummary:
    def test_no_issues_shows_clean_message(self, report_env):
        gen, _ = report_env
        assert "No errors found" in gen._section_url_summary([])

    def test_issues_shows_table_header(self, report_env):
        gen, _ = report_env
        section = gen._section_url_summary(gen.vs.validation_results)
        assert "URL" in section
        assert "Issue Count" in section

    def test_issues_shows_base_url(self, report_env):
        gen, _ = report_env
        section = gen._section_url_summary(gen.vs.validation_results)
        assert "https://example.com/plp" in section

    def test_issues_shows_bold_count(self, report_env):
        gen, _ = report_env
        section = gen._section_url_summary(gen.vs.validation_results)
        assert "**" in section

    def test_sorted_descending_by_count(self, report_env):
        """URL with more issues should appear before one with fewer."""
        gen, _ = report_env
        from validators import ValidationResult
        issues = [
            ValidationResult("CTA_BROKEN", "CRITICAL", "msg", "C", base_url="https://a.com"),
            ValidationResult("CTA_BROKEN", "CRITICAL", "msg", "C", base_url="https://a.com"),
            ValidationResult("PRICE_MISMATCH", "MEDIUM", "msg", "C", base_url="https://b.com"),
        ]
        section = gen._section_url_summary(issues)
        pos_a = section.find("https://a.com")
        pos_b = section.find("https://b.com")
        assert pos_a < pos_b  # a.com has 2 issues → appears first


# ---------------------------------------------------------------------------
# _section_issue_breakdown
# ---------------------------------------------------------------------------

class TestSectionIssueBreakdown:
    def test_no_issues_shows_clean_message(self, report_env):
        gen, _ = report_env
        section = gen._section_issue_breakdown({"total_issues": 0, "by_type": {}, "by_severity": {}})
        assert "No issues found" in section

    def test_shows_cta_broken_type(self, report_env):
        gen, _ = report_env
        summary = gen.vs.get_summary()
        assert "CTA_BROKEN" in gen._section_issue_breakdown(summary)

    def test_shows_price_mismatch_type(self, report_env):
        gen, _ = report_env
        summary = gen.vs.get_summary()
        assert "PRICE_MISMATCH" in gen._section_issue_breakdown(summary)

    def test_shows_critical_severity(self, report_env):
        gen, _ = report_env
        summary = gen.vs.get_summary()
        assert "CRITICAL" in gen._section_issue_breakdown(summary)

    def test_shows_total_count(self, report_env):
        gen, _ = report_env
        summary = gen.vs.get_summary()
        section = gen._section_issue_breakdown(summary)
        total = str(summary["total_issues"])
        assert total in section


# ---------------------------------------------------------------------------
# _section_details
# ---------------------------------------------------------------------------

class TestSectionDetails:
    def test_no_issues_shows_clean_message(self, report_env):
        gen, _ = report_env
        assert "No issues to report" in gen._section_details([])

    def test_shows_course_name(self, report_env):
        gen, _ = report_env
        section = gen._section_details(gen.vs.validation_results)
        assert "Broken Course" in section

    def test_pipe_chars_escaped_in_expected_field(self, report_env):
        gen, _ = report_env
        issue = ValidationResult(
            type="CTA_BROKEN",
            severity="CRITICAL",
            message="test",
            course_name="Pipe Course",
            field="cta_link",
            expected="https://example.com?a=1|b=2",
            actual="N/A",
        )
        section = gen._section_details([issue])
        assert "\\|" in section

    def test_pipe_chars_escaped_in_actual_field(self, report_env):
        gen, _ = report_env
        issue = ValidationResult(
            type="CTA_BROKEN",
            severity="CRITICAL",
            message="test",
            course_name="Pipe Course",
            field="cta_link",
            expected="Valid URL",
            actual="https://example.com?a=1|b=2",
        )
        section = gen._section_details([issue])
        assert "\\|" in section

    def test_groups_by_issue_type(self, report_env):
        gen, _ = report_env
        section = gen._section_details(gen.vs.validation_results)
        # Different issue types should have their own ### subsections
        assert section.count("###") >= 1

    def test_includes_viewport_column(self, report_env):
        gen, _ = report_env
        section = gen._section_details(gen.vs.validation_results)
        assert "Viewport" in section

    def test_includes_url_column(self, report_env):
        gen, _ = report_env
        section = gen._section_details(gen.vs.validation_results)
        assert "URL" in section


# ---------------------------------------------------------------------------
# _query_db_stats
# ---------------------------------------------------------------------------

class TestQueryDbStats:
    def test_returns_desktop_viewport(self, report_env):
        gen, _ = report_env
        assert "desktop" in gen._query_db_stats()

    def test_returns_mobile_viewport(self, report_env):
        gen, _ = report_env
        assert "mobile" in gen._query_db_stats()

    def test_desktop_course_count(self, report_env):
        gen, _ = report_env
        stats = gen._query_db_stats()
        assert stats["desktop"]["courses"] == 2  # clean + broken seeded for desktop

    def test_mobile_course_count(self, report_env):
        gen, _ = report_env
        stats = gen._query_db_stats()
        assert stats["mobile"]["courses"] == 1  # mismatch seeded for mobile

    def test_broken_count_for_desktop(self, report_env):
        gen, _ = report_env
        stats = gen._query_db_stats()
        assert stats["desktop"]["broken"] == 1

    def test_price_mismatch_count_for_mobile(self, report_env):
        gen, _ = report_env
        stats = gen._query_db_stats()
        assert stats["mobile"]["price_mismatch"] == 1

    def test_empty_db_returns_empty_dict(self, empty_report_env):
        gen, _ = empty_report_env
        assert gen._query_db_stats() == {}

    def test_scoped_to_run_id(self, tmp_path, monkeypatch):
        """Stats should only reflect the current run_id."""
        monkeypatch.setattr(rg_module, "REPORTS_DIR", str(tmp_path / "reports"))
        db_path = str(tmp_path / "scoped.db")
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
        dm.save_batch([bad], run1)   # run 1: 1 bad course
        # run 2: nothing

        vs = ValidationService(db_path)
        gen = ReportGenerator(
            validation_service=vs,
            db_name=db_path,
            start_time=datetime.now(),
            urls_scraped=[],
            run_id=run2,  # <-- scoped to run2
        )
        # run2 has no courses, so stats should be empty
        assert gen._query_db_stats() == {}
