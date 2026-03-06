"""
Thread-safe in-process caches for a single WatchDog run.

PdpCache    — stores PDP verification results keyed by (url, viewport).
ProgressTracker — formats [N/total] prefixes for log lines.
"""

import threading


class PdpCache:
    """
    Thread-safe in-memory cache for PDP verification results.
    Key: (pdp_url, viewport)  →  Value: (pdp_price, cta_status, is_broken, price_mismatch)

    Multiple entry-point URLs (HOME, STREAM_PAGES, PLP_PAGES) frequently surface
    the same course card pointing to the same PDP.  Caching avoids re-navigating
    pages that have already been checked, saving dozens of 8-12 s round-trips per run.
    """

    def __init__(self):
        self._cache: dict = {}
        self._lock = threading.Lock()

    def get(self, pdp_url: str, viewport: str):
        """Return cached result tuple or None if not cached."""
        with self._lock:
            return self._cache.get((pdp_url, viewport))

    def set(self, pdp_url: str, viewport: str, result: tuple):
        """Store a result tuple for the given (url, viewport) pair."""
        with self._lock:
            self._cache[(pdp_url, viewport)] = result

    def size(self) -> int:
        with self._lock:
            return len(self._cache)


class ProgressTracker:
    """
    Thread-safe counter that emits [N/total] progress prefixes for log lines.
    Each viewport thread owns its own tracker so counts stay independent.
    """

    def __init__(self, total: int, label: str):
        self.total = total
        self.label = label.upper()
        self._done = 0
        self._lock = threading.Lock()
        # Width of the total number for zero-padded formatting, e.g. "  3/31"
        self._w = len(str(total))

    def advance(self) -> str:
        """Increment counter and return a formatted '[LABEL N/total]' string."""
        with self._lock:
            self._done += 1
            return f"[{self.label} {self._done:{self._w}}/{self.total}]"
