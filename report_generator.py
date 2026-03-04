"""
Report Generator
Produces a human-readable Markdown report for every WatchDog scraper run.
Reports are saved to the reports/ directory with a timestamp in the filename.
"""

import os
import sqlite3
import logging
from datetime import datetime
from typing import List, Optional
from validation_service import ValidationService


REPORTS_DIR = "reports"


class ReportGenerator:
    """
    Generates a structured Markdown report from a completed scraper run.

    Report layout:
        1. Header — run timestamp, duration, URLs scraped
        2. Summary table — courses and issues per viewport
        3. Validation issues — counts by type and severity
        4. Details — per-issue breakdown (broken links, price mismatches, etc.)
    """

    def __init__(
        self,
        validation_service: ValidationService,
        db_name: str,
        start_time: datetime,
        urls_scraped: List[str],
        run_id: Optional[int] = None,
    ):
        self.vs = validation_service
        self.db_name = db_name
        self.start_time = start_time
        self.end_time = datetime.now()
        self.urls_scraped = urls_scraped
        self.run_id = run_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(self) -> str:
        """Generate the report and save it to reports/. Returns the file path."""
        os.makedirs(REPORTS_DIR, exist_ok=True)
        filename = self.start_time.strftime("report_%Y-%m-%d_%H-%M-%S.md")
        filepath = os.path.join(REPORTS_DIR, filename)

        content = self._build_report()
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        logging.info(f"Report saved → {filepath}")
        return filepath

    # ------------------------------------------------------------------
    # Report building
    # ------------------------------------------------------------------

    def _build_report(self) -> str:
        duration = self.end_time - self.start_time
        total_seconds = int(duration.total_seconds())
        duration_str = f"{total_seconds // 60}m {total_seconds % 60}s"

        summary = self.vs.get_summary()
        db_stats = self._query_db_stats()
        issues = self.vs.validation_results

        sections = [
            self._section_header(duration_str),
            self._section_summary(summary, db_stats),
            self._section_url_summary(issues),
            self._section_issue_breakdown(summary),
            self._section_details(issues),
        ]
        return "\n\n".join(sections) + "\n"

    def _section_header(self, duration_str: str) -> str:
        lines = [
            f"# WatchDog Run Report",
            f"",
            f"| | |",
            f"|---|---|",
            f"| **Date** | {self.start_time.strftime('%Y-%m-%d %H:%M:%S')} |",
            f"| **Duration** | {duration_str} |",
            f"| **URLs Scraped** | {len(self.urls_scraped)} |",
            f"| **Viewports** | Desktop (1920×1080), Mobile — iPhone XR (390×844) |",
        ]
        if self.urls_scraped:
            lines += ["", "**URLs:**"]
            lines += [f"- `{u}`" for u in self.urls_scraped]
        return "\n".join(lines)

    def _section_summary(self, summary: dict, db_stats: dict) -> str:
        desktop = db_stats.get("desktop", {})
        mobile = db_stats.get("mobile", {})

        def stat(key: str) -> tuple:
            d = desktop.get(key, 0)
            m = mobile.get(key, 0)
            return d, m, d + m

        d_courses, m_courses, t_courses = stat("courses")
        d_broken, m_broken, t_broken = stat("broken")
        d_p_missing, m_p_missing, t_p_missing = stat("price_missing")
        d_p_correct, m_p_correct, t_p_correct = stat("price_correct")
        d_mismatch, m_mismatch, t_mismatch = stat("price_mismatch")
        d_cta_found, m_cta_found, t_cta_found = stat("cta_found")
        d_cta_missing, m_cta_missing, t_cta_missing = stat("cta_missing")

        lines = [
            "## Summary",
            "",
            "| Metric | Desktop | Mobile | Total |",
            "|--------|--------:|-------:|------:|",
            f"| Courses scraped | {d_courses} | {m_courses} | {t_courses} |",
            f"| Broken links | {d_broken} | {m_broken} | **{t_broken}** |",
            f"| Price missing | {d_p_missing} | {m_p_missing} | {t_p_missing} |",
            f"| Price correct ✅ | {d_p_correct} | {m_p_correct} | {t_p_correct} |",
            f"| Price mismatches | {d_mismatch} | {m_mismatch} | **{t_mismatch}** |",
            f"| CTA found on PDP | {d_cta_found} | {m_cta_found} | {t_cta_found} |",
            f"| CTA missing on PDP | {d_cta_missing} | {m_cta_missing} | **{t_cta_missing}** |",
            f"| **Validation issues** | | | **{summary.get('total_issues', 0)}** |",
        ]
        return "\n".join(lines)

    def _section_url_summary(self, issues: list) -> str:
        if not issues:
            return "## Errors by URL\n\n✅ No errors found."
            
        # Group issues by base_url
        url_counts = {}
        for issue in issues:
            url = getattr(issue, 'base_url', 'Unknown URL')
            url_counts[url] = url_counts.get(url, 0) + 1
            
        lines = [
            "## Errors by URL",
            "",
            "| URL | Issue Count |",
            "|-----|-------------|"
        ]
        
        # Sort by issue count descending
        for url, count in sorted(url_counts.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"| {url} | **{count}** |")
            
        return "\n".join(lines)

    def _section_issue_breakdown(self, summary: dict) -> str:
        if not summary.get("total_issues"):
            return "## Validation Issues\n\n✅ No issues found."

        by_type = summary.get("by_type", {})
        by_severity = summary.get("by_severity", {})

        lines = [
            "## Validation Issues",
            "",
            f"**Total: {summary['total_issues']}**",
            "",
            "### By Type",
            "",
            "| Type | Count |",
            "|------|------:|",
        ]
        for t, count in sorted(by_type.items()):
            lines.append(f"| {t} | {count} |")

        lines += [
            "",
            "### By Severity",
            "",
            "| Severity | Count |",
            "|----------|------:|",
        ]
        for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
            count = by_severity.get(sev, 0)
            icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}.get(sev, "")
            lines.append(f"| {icon} {sev} | {count} |")

        return "\n".join(lines)

    def _section_details(self, issues: list) -> str:
        if not issues:
            return "## Issue Details\n\n✅ No issues to report."

        # Group by type
        by_type: dict = {}
        for issue in issues:
            by_type.setdefault(issue.type, []).append(issue)

        lines = ["## Issue Details"]

        for issue_type, type_issues in sorted(by_type.items()):
            lines += [
                "",
                f"### {issue_type.replace('_', ' ').title()} ({len(type_issues)})",
                "",
                "| Course | URL | Viewport | Field | Expected | Actual |",
                "|--------|-----|----------|-------|----------|--------|",
            ]
            for issue in type_issues:
                # Viewport is embedded in course_data when the validator runs;
                # fall back gracefully if not present
                viewport = getattr(issue, "viewport", "—")
                url = getattr(issue, "base_url", "—")
                expected = str(issue.expected or "—").replace("|", "\\|")
                actual = str(issue.actual or "—").replace("|", "\\|")
                field = issue.field or "—"
                lines.append(
                    f"| {issue.course_name} | {url} | {viewport} | {field} | {expected} | {actual} |"
                )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _query_db_stats(self) -> dict:
        """Return per-viewport counts from the DB, scoped to the current run_id."""
        stats: dict = {}
        try:
            # Only count rows from this specific run so the summary never
            # accumulates across previous runs.
            where = "WHERE run_id = ?" if self.run_id is not None else ""
            params = (self.run_id,) if self.run_id is not None else ()

            with sqlite3.connect(self.db_name, timeout=10) as conn:
                for row in conn.execute(
                    f"""
                    SELECT
                        viewport,
                        COUNT(*)                                                        AS courses,
                        SUM(is_broken)                                                  AS broken,
                        SUM(price_mismatch)                                             AS price_mismatch,
                        SUM(CASE WHEN pdp_price IN ('Not Found','N/A','Error','')
                                  OR pdp_price IS NULL                  THEN 1 ELSE 0 END) AS price_missing,
                        SUM(CASE WHEN (pdp_price NOT IN ('Not Found','N/A','Error','')
                                  AND pdp_price IS NOT NULL)
                                  AND price_mismatch = 0               THEN 1 ELSE 0 END) AS price_correct,
                        SUM(CASE WHEN cta_status LIKE 'Found%' THEN 1 ELSE 0 END)      AS cta_found,
                        SUM(CASE WHEN cta_status = 'Not Found' THEN 1 ELSE 0 END)      AS cta_missing
                    FROM courses
                    {where}
                    GROUP BY viewport
                    """,
                    params,
                ):
                    viewport = row[0] or "unknown"
                    stats[viewport] = {
                        "courses":        row[1] or 0,
                        "broken":         row[2] or 0,
                        "price_mismatch": row[3] or 0,
                        "price_missing":  row[4] or 0,
                        "price_correct":  row[5] or 0,
                        "cta_found":      row[6] or 0,
                        "cta_missing":    row[7] or 0,
                    }
        except Exception as e:
            logging.warning(f"Could not query DB stats for report: {e}")
        return stats
