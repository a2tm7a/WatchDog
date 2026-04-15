"""
Shared constants for WatchDog.

Centralises magic strings so validators, reports, and the email service
all stay in sync when new severity levels or CTA variants are added.
"""

# Severity levels in priority order (highest → lowest)
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]

# Emoji icons used in email HTML and Markdown reports
SEVERITY_ICONS = {
    "CRITICAL": "🔴",
    "HIGH":     "🟠",
    "MEDIUM":   "🟡",
    "LOW":      "🟢",
}

# Button text fragments that indicate a working purchase CTA on a PDP.
# Checked case-insensitively and as a substring (up to 40 chars).
CTA_KEYWORDS = [
    "enroll now",
    "enrol now",
    "buy now",
    "select batch",
    "select phase",
]

# Valid check type strings produced by validators.
# Must stay in sync with ValidationResult.type values across all validators.
KNOWN_CHECK_TYPES: frozenset = frozenset({"CTA_BROKEN", "CTA_MISSING", "PRICE_MISMATCH"})
