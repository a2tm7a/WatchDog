"""
Shared price-cleaning utilities used by both the scraper handlers and
the price-mismatch validator.

Keeping this logic in one place prevents the two implementations from
drifting apart.
"""

import re
from typing import Optional

_MISSING_SENTINELS = {"n/a", "not found", "error", ""}


def is_price_missing(price_str: Optional[str]) -> bool:
    """Return True if *price_str* represents an absent or unknown price."""
    if not price_str:
        return True
    return price_str.strip().lower() in _MISSING_SENTINELS


def clean_price(price_str: Optional[str]) -> Optional[str]:
    """
    Extract the numeric digits from a price string for comparison.

    Examples::

        clean_price("₹ 93,500")  -> "93500"
        clean_price("₹1,299")    -> "1299"
        clean_price("N/A")       -> None
        clean_price(None)        -> None
    """
    if is_price_missing(price_str):
        return None
    nums = "".join(re.findall(r"\d+", str(price_str).replace(",", "")))
    return nums if nums else None
