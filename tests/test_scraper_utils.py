"""
Tests for scraper utility classes that don't require a live browser.

Covers:
- BasePageHandler.clean_price: currency symbols, commas, N/A sentinels, edge cases
- PdpCache: get/set, viewport isolation, size, overwrite, thread safety
- ProgressTracker: counter increments, label formatting, padding, thread safety
"""
import threading
import pytest
from unittest.mock import MagicMock
from cache import PdpCache, ProgressTracker
from handlers import BasePageHandler


# ---------------------------------------------------------------------------
# Concrete handler for testing BasePageHandler methods (no scraping needed)
# ---------------------------------------------------------------------------

class ConcreteHandler(BasePageHandler):
    @staticmethod
    def can_handle(url):
        return True

    def scrape(self, url):
        pass


def make_handler(viewport="desktop"):
    """Return a ConcreteHandler with a mocked page and db."""
    page = MagicMock()
    db = MagicMock()
    return ConcreteHandler(page, db, viewport=viewport)


# ---------------------------------------------------------------------------
# clean_price
# ---------------------------------------------------------------------------

class TestCleanPrice:
    def setup_method(self):
        self.h = make_handler()

    def test_rupee_and_comma(self):
        assert self.h.clean_price("₹1,299") == "1299"

    def test_rupee_with_space(self):
        assert self.h.clean_price("₹ 93,500") == "93500"

    def test_plain_number(self):
        assert self.h.clean_price("1299") == "1299"

    def test_large_indian_price_format(self):
        assert self.h.clean_price("₹1,00,000") == "100000"

    def test_none_returns_none(self):
        assert self.h.clean_price(None) is None

    def test_empty_string_returns_none(self):
        assert self.h.clean_price("") is None

    def test_na_substring_returns_none(self):
        assert self.h.clean_price("N/A") is None

    def test_na_as_part_of_string_returns_none(self):
        assert self.h.clean_price("Price N/A") is None

    def test_not_found_returns_none(self):
        assert self.h.clean_price("Not Found") is None

    def test_not_found_as_part_of_string_returns_none(self):
        assert self.h.clean_price("Price Not Found") is None

    def test_purely_non_numeric_string_returns_none(self):
        assert self.h.clean_price("Free") is None

    def test_same_result_regardless_of_currency_symbol(self):
        # "₹1,299" and "1,299" should clean to the same value
        assert self.h.clean_price("₹1,299") == self.h.clean_price("1,299")

    def test_desktop_and_mobile_handlers_same_result(self):
        desktop_h = make_handler(viewport="desktop")
        mobile_h = make_handler(viewport="mobile")
        assert desktop_h.clean_price("₹5,000") == mobile_h.clean_price("₹5,000")


# ---------------------------------------------------------------------------
# PdpCache
# ---------------------------------------------------------------------------

class TestPdpCache:
    def test_get_on_empty_cache_returns_none(self):
        cache = PdpCache()
        assert cache.get("https://example.com/pdp", "desktop") is None

    def test_set_then_get_returns_stored_value(self):
        cache = PdpCache()
        result = ("₹1,000", "Found (Enroll Now)", 0, 0)
        cache.set("https://example.com/pdp", "desktop", result)
        assert cache.get("https://example.com/pdp", "desktop") == result

    def test_different_viewports_are_independent_entries(self):
        cache = PdpCache()
        d_result = ("₹1,000", "Found", 0, 0)
        m_result = ("₹1,200", "Not Found", 0, 0)
        cache.set("https://example.com/pdp", "desktop", d_result)
        cache.set("https://example.com/pdp", "mobile", m_result)
        assert cache.get("https://example.com/pdp", "desktop") == d_result
        assert cache.get("https://example.com/pdp", "mobile") == m_result

    def test_different_urls_are_independent_entries(self):
        cache = PdpCache()
        r1 = ("₹1,000", "Found", 0, 0)
        r2 = ("₹2,000", "Found", 0, 0)
        cache.set("https://example.com/pdp1", "desktop", r1)
        cache.set("https://example.com/pdp2", "desktop", r2)
        assert cache.get("https://example.com/pdp1", "desktop") == r1
        assert cache.get("https://example.com/pdp2", "desktop") == r2

    def test_size_starts_at_zero(self):
        assert PdpCache().size() == 0

    def test_size_increments_per_unique_key(self):
        cache = PdpCache()
        cache.set("https://example.com/1", "desktop", ("x", "y", 0, 0))
        cache.set("https://example.com/2", "desktop", ("x", "y", 0, 0))
        assert cache.size() == 2

    def test_same_key_does_not_increment_size(self):
        cache = PdpCache()
        cache.set("https://example.com/pdp", "desktop", ("v1", "s", 0, 0))
        cache.set("https://example.com/pdp", "desktop", ("v2", "s", 0, 0))
        assert cache.size() == 1

    def test_overwrite_replaces_value(self):
        cache = PdpCache()
        cache.set("https://example.com/pdp", "desktop", ("old", "s", 0, 0))
        cache.set("https://example.com/pdp", "desktop", ("new", "s", 0, 0))
        assert cache.get("https://example.com/pdp", "desktop")[0] == "new"

    def test_desktop_miss_does_not_affect_mobile(self):
        cache = PdpCache()
        cache.set("https://example.com/pdp", "desktop", ("price", "status", 0, 0))
        assert cache.get("https://example.com/pdp", "mobile") is None

    def test_thread_safe_concurrent_writes(self):
        """Multiple threads writing different keys should not corrupt the cache."""
        cache = PdpCache()
        errors = []

        def writer(thread_id):
            try:
                for i in range(20):
                    url = f"https://example.com/pdp-{thread_id}-{i}"
                    cache.set(url, "desktop", ("price", "status", 0, 0))
                    assert cache.get(url, "desktop") is not None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        assert cache.size() == 5 * 20


# ---------------------------------------------------------------------------
# ProgressTracker
# ---------------------------------------------------------------------------

class TestProgressTracker:
    def test_first_advance_shows_one(self):
        tracker = ProgressTracker(10, "desktop")
        result = tracker.advance()
        assert "1" in result
        assert "10" in result

    def test_label_is_uppercased(self):
        tracker = ProgressTracker(5, "mobile")
        result = tracker.advance()
        assert "MOBILE" in result

    def test_advance_increments_counter(self):
        tracker = ProgressTracker(10, "desktop")
        tracker.advance()
        tracker.advance()
        result = tracker.advance()
        assert " 3/10" in result

    def test_total_always_shown(self):
        tracker = ProgressTracker(42, "desktop")
        result = tracker.advance()
        assert "42" in result

    def test_counter_pads_to_width_of_total(self):
        """For total=100, the first count should be right-padded as '  1'."""
        tracker = ProgressTracker(100, "desktop")
        result = tracker.advance()
        assert "  1/100" in result

    def test_format_contains_brackets(self):
        tracker = ProgressTracker(5, "t")
        result = tracker.advance()
        assert result.startswith("[")
        assert result.endswith("]")

    def test_thread_safe_all_advances_unique(self):
        """100 concurrent advances must all produce unique results."""
        tracker = ProgressTracker(100, "t")
        results = []
        lock = threading.Lock()

        def advance():
            r = tracker.advance()
            with lock:
                results.append(r)

        threads = [threading.Thread(target=advance) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 100
        assert len(set(results)) == 100  # every result must be unique
