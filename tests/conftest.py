"""
pytest configuration: stub out Playwright and playwright-stealth so that
scraper.py can be imported in unit tests without a browser installed.

All tests in this suite that touch scraper.py only exercise pure-Python logic
(DatabaseManager, PdpCache, ProgressTracker, BasePageHandler.clean_price).
None of them launch a real browser.
"""
import sys
from unittest.mock import MagicMock

# Stub playwright before scraper.py is imported during test collection.
for _mod in [
    "playwright",
    "playwright.sync_api",
    "playwright_stealth",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Ensure Stealth() can be called at module level (STEALTH = Stealth())
sys.modules["playwright_stealth"].Stealth = MagicMock(return_value=MagicMock())
