"""
Tests for PurchaseCTAValidator.

Covers:
- Check 1: CTA link missing / invalid (None, "", "N/A", "Error")
- Check 2: CTA link doesn't navigate away (is_broken flag, same-URL match)
- Check 3: PDP reachable but no purchase button
- Early-exit behaviour between checks
- Metadata injection (viewport, base_url, course_name)
- Chain of Responsibility pass-through to the next validator
"""
import pytest
from validators import PurchaseCTAValidator, PriceMismatchValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def validate(overrides=None):
    """Run PurchaseCTAValidator with sensible defaults, optionally overridden."""
    data = {
        "course_name": "Test Course",
        "base_url": "https://example.com/plp",
        "cta_link": "https://example.com/course",
        "is_broken": 0,
        "cta_status": "Found (Enroll Now)",
        "viewport": "desktop",
    }
    if overrides:
        data.update(overrides)
    return PurchaseCTAValidator().validate(data)


# ---------------------------------------------------------------------------
# Check 1: CTA link missing / invalid
# ---------------------------------------------------------------------------

class TestCtaLinkMissing:
    def test_none_cta_link_raises_critical(self):
        issues = validate({"cta_link": None})
        assert len(issues) == 1
        assert issues[0].type == "CTA_BROKEN"
        assert issues[0].severity == "CRITICAL"

    def test_empty_string_cta_link_raises_critical(self):
        issues = validate({"cta_link": ""})
        assert len(issues) == 1
        assert issues[0].type == "CTA_BROKEN"
        assert issues[0].severity == "CRITICAL"

    def test_na_cta_link_raises_critical(self):
        issues = validate({"cta_link": "N/A"})
        assert len(issues) == 1
        assert issues[0].severity == "CRITICAL"

    def test_error_cta_link_raises_critical(self):
        issues = validate({"cta_link": "Error"})
        assert len(issues) == 1
        assert issues[0].severity == "CRITICAL"

    def test_missing_cta_early_exits_before_button_check(self):
        """When CTA link is absent, no further checks (incl. button check) should run."""
        issues = validate({"cta_link": None, "cta_status": "Not Found"})
        # Only one issue: the broken link — not a second CTA_MISSING issue
        assert len(issues) == 1
        assert issues[0].type == "CTA_BROKEN"

    def test_missing_cta_actual_field_shows_none(self):
        issues = validate({"cta_link": None})
        assert issues[0].actual == "None"

    def test_missing_cta_field_is_cta_link(self):
        issues = validate({"cta_link": None})
        assert issues[0].field == "cta_link"


# ---------------------------------------------------------------------------
# Check 2: Link doesn't navigate away from listing page
# ---------------------------------------------------------------------------

class TestCtaLinkSamePage:
    def test_is_broken_flag_raises_critical(self):
        issues = validate({"is_broken": 1})
        assert len(issues) == 1
        assert issues[0].type == "CTA_BROKEN"
        assert issues[0].severity == "CRITICAL"

    def test_cta_same_as_base_url_raises_critical(self):
        issues = validate({
            "cta_link": "https://example.com/plp",
            "base_url": "https://example.com/plp",
        })
        assert len(issues) == 1
        assert issues[0].type == "CTA_BROKEN"

    def test_trailing_slash_normalised_before_comparison(self):
        """Trailing slashes should not prevent same-URL detection."""
        issues = validate({
            "cta_link": "https://example.com/plp/",
            "base_url": "https://example.com/plp",
        })
        assert len(issues) == 1
        assert issues[0].type == "CTA_BROKEN"

    def test_same_page_early_exits_before_button_check(self):
        """Link staying on same page → no CTA_MISSING issue should follow."""
        issues = validate({
            "is_broken": 1,
            "cta_status": "Not Found",
        })
        assert len(issues) == 1
        assert issues[0].type == "CTA_BROKEN"

    def test_different_url_does_not_trigger_same_page(self):
        issues = validate({
            "cta_link": "https://example.com/course-detail",
            "base_url": "https://example.com/plp",
            "is_broken": 0,
        })
        assert not any(i.type == "CTA_BROKEN" for i in issues)


# ---------------------------------------------------------------------------
# Check 3: PDP reachable but no purchase button
# ---------------------------------------------------------------------------

class TestCtaButtonMissing:
    def test_not_found_status_raises_high(self):
        issues = validate({"cta_status": "Not Found"})
        assert len(issues) == 1
        assert issues[0].type == "CTA_MISSING"
        assert issues[0].severity == "HIGH"

    def test_found_cta_status_no_issues(self):
        issues = validate({"cta_status": "Found (Enroll Now)"})
        assert issues == []

    def test_found_buy_now_no_issues(self):
        issues = validate({"cta_status": "Found (Buy Now)"})
        assert issues == []

    def test_na_cta_status_no_issues(self):
        """N/A means PDP wasn't visited — should not trigger CTA_MISSING."""
        issues = validate({"cta_status": "N/A"})
        assert issues == []

    def test_cta_missing_field_is_cta_status(self):
        issues = validate({"cta_status": "Not Found"})
        assert issues[0].field == "cta_status"


# ---------------------------------------------------------------------------
# Metadata injection into results
# ---------------------------------------------------------------------------

class TestMetadataInjection:
    def test_viewport_injected_into_result(self):
        issues = validate({"viewport": "mobile", "cta_link": None})
        assert issues[0].viewport == "mobile"

    def test_base_url_injected_into_result(self):
        issues = validate({"base_url": "https://custom.com/page", "cta_link": None})
        assert issues[0].base_url == "https://custom.com/page"

    def test_course_name_preserved_in_result(self):
        issues = validate({"course_name": "My Special Course", "cta_link": None})
        assert issues[0].course_name == "My Special Course"


# ---------------------------------------------------------------------------
# Chain of Responsibility
# ---------------------------------------------------------------------------

class TestChaining:
    def test_chains_to_next_validator_when_cta_passes(self):
        """CTA passes → price validator should fire on mismatch."""
        cta = PurchaseCTAValidator()
        price = PriceMismatchValidator()
        cta.set_next(price)

        data = {
            "course_name": "Chain Test",
            "base_url": "https://example.com/plp",
            "cta_link": "https://example.com/course",
            "is_broken": 0,
            "cta_status": "Found (Enroll Now)",
            "price": "₹1,000",
            "pdp_price": "₹2,000",
            "viewport": "desktop",
        }
        issues = cta.validate(data)
        assert any(i.type == "PRICE_MISMATCH" for i in issues)

    def test_set_next_returns_the_next_validator(self):
        """set_next should return the next validator for fluent chaining."""
        cta = PurchaseCTAValidator()
        price = PriceMismatchValidator()
        result = cta.set_next(price)
        assert result is price

    def test_no_chain_returns_only_own_issues(self):
        """Without a next validator, only CTA issues are returned."""
        issues = validate({"cta_link": None})
        assert len(issues) == 1
