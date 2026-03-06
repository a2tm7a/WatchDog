"""
Tests for PriceMismatchValidator.

Covers:
- _is_price_missing: all sentinel values and valid prices
- _clean_price: currency symbols, commas, spaces, edge cases
- Mismatch detection: matching prices, mismatched prices, field/message content
- Skip logic: when either price is missing, no issue is raised
"""
import pytest
from validators import PriceMismatchValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_validator():
    return PriceMismatchValidator()


def validate(overrides=None):
    data = {
        "course_name": "Test Course",
        "price": "₹1,299",
        "pdp_price": "₹1,299",
        "viewport": "desktop",
        "base_url": "https://example.com",
    }
    if overrides:
        data.update(overrides)
    return PriceMismatchValidator().validate(data)


# ---------------------------------------------------------------------------
# _is_price_missing
# ---------------------------------------------------------------------------

class TestIsPriceMissing:
    def setup_method(self):
        self.v = make_validator()

    def test_none_is_missing(self):
        assert self.v._is_price_missing(None) is True

    def test_empty_string_is_missing(self):
        assert self.v._is_price_missing("") is True

    def test_na_uppercase_is_missing(self):
        assert self.v._is_price_missing("N/A") is True

    def test_na_lowercase_is_missing(self):
        assert self.v._is_price_missing("n/a") is True

    def test_not_found_is_missing(self):
        assert self.v._is_price_missing("Not Found") is True

    def test_not_found_lowercase_is_missing(self):
        assert self.v._is_price_missing("not found") is True

    def test_error_is_missing(self):
        assert self.v._is_price_missing("Error") is True

    def test_error_lowercase_is_missing(self):
        assert self.v._is_price_missing("error") is True

    def test_valid_rupee_price_is_not_missing(self):
        assert self.v._is_price_missing("₹1,299") is False

    def test_plain_numeric_price_is_not_missing(self):
        assert self.v._is_price_missing("1299") is False

    def test_price_with_spaces_is_not_missing(self):
        assert self.v._is_price_missing("₹ 1,299") is False


# ---------------------------------------------------------------------------
# _clean_price
# ---------------------------------------------------------------------------

class TestCleanPrice:
    def setup_method(self):
        self.v = make_validator()

    def test_rupee_and_comma(self):
        assert self.v._clean_price("₹1,299") == "1299"

    def test_rupee_with_space(self):
        assert self.v._clean_price("₹ 93,500") == "93500"

    def test_plain_number(self):
        assert self.v._clean_price("1299") == "1299"

    def test_large_indian_format(self):
        assert self.v._clean_price("₹1,00,000") == "100000"

    def test_returns_none_for_none(self):
        assert self.v._clean_price(None) is None

    def test_returns_none_for_na(self):
        assert self.v._clean_price("N/A") is None

    def test_returns_none_for_empty_string(self):
        assert self.v._clean_price("") is None

    def test_returns_none_for_not_found(self):
        assert self.v._clean_price("Not Found") is None

    def test_returns_none_for_error(self):
        assert self.v._clean_price("Error") is None

    def test_returns_none_for_nonnumeric(self):
        assert self.v._clean_price("Free") is None

    def test_strips_leading_currency_symbol(self):
        # Both "₹1,299" and "1,299" should yield the same numeric string
        assert self.v._clean_price("₹1,299") == self.v._clean_price("1,299")


# ---------------------------------------------------------------------------
# Mismatch detection
# ---------------------------------------------------------------------------

class TestMismatchDetection:
    def test_matching_prices_no_issues(self):
        assert validate({"price": "₹1,299", "pdp_price": "₹1,299"}) == []

    def test_matching_prices_with_spacing_no_issues(self):
        assert validate({"price": "₹ 1,299", "pdp_price": "₹1,299"}) == []

    def test_different_formats_same_value_no_issues(self):
        # "₹1,299" vs "1299" should be equal after cleaning
        assert validate({"price": "₹1,299", "pdp_price": "1299"}) == []

    def test_mismatched_prices_raises_issue(self):
        issues = validate({"price": "₹1,000", "pdp_price": "₹2,000"})
        assert len(issues) == 1
        assert issues[0].type == "PRICE_MISMATCH"
        assert issues[0].severity == "MEDIUM"

    def test_mismatch_message_contains_both_prices(self):
        issues = validate({"price": "₹1,000", "pdp_price": "₹2,000"})
        assert "₹1,000" in issues[0].message
        assert "₹2,000" in issues[0].message

    def test_mismatch_expected_is_card_price(self):
        issues = validate({"price": "₹1,000", "pdp_price": "₹2,000"})
        assert issues[0].expected == "₹1,000"

    def test_mismatch_actual_is_pdp_price(self):
        issues = validate({"price": "₹1,000", "pdp_price": "₹2,000"})
        assert issues[0].actual == "₹2,000"

    def test_mismatch_field_is_price(self):
        issues = validate({"price": "₹1,000", "pdp_price": "₹2,000"})
        assert issues[0].field == "price"

    def test_course_name_in_result(self):
        issues = validate({"price": "₹1,000", "pdp_price": "₹2,000",
                           "course_name": "Special Course"})
        assert issues[0].course_name == "Special Course"


# ---------------------------------------------------------------------------
# Skip when either price is missing
# ---------------------------------------------------------------------------

class TestSkipWhenPriceMissing:
    def test_skip_when_card_price_is_none(self):
        assert validate({"price": None}) == []

    def test_skip_when_card_price_is_empty(self):
        assert validate({"price": ""}) == []

    def test_skip_when_card_price_is_na(self):
        assert validate({"price": "N/A"}) == []

    def test_skip_when_card_price_is_not_found(self):
        assert validate({"price": "Not Found"}) == []

    def test_skip_when_pdp_price_is_none(self):
        assert validate({"pdp_price": None}) == []

    def test_skip_when_pdp_price_is_empty(self):
        assert validate({"pdp_price": ""}) == []

    def test_skip_when_pdp_price_is_not_found(self):
        assert validate({"pdp_price": "Not Found"}) == []

    def test_skip_when_pdp_price_is_error(self):
        assert validate({"pdp_price": "Error"}) == []

    def test_skip_when_both_prices_missing(self):
        assert validate({"price": None, "pdp_price": None}) == []

    def test_skip_when_card_has_price_but_pdp_na(self):
        # PDP wasn't visited — mismatch check should be skipped
        assert validate({"price": "₹1,000", "pdp_price": "N/A"}) == []
