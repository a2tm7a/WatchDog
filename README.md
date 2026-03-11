# 🐕 WatchDog

> Automated scrape-and-validate agent for [allen.in](https://allen.in) — catches broken CTAs, price mismatches, and missing purchase buttons before users do.

WatchDog crawls every course card across the Homepage, PLP (Product Listing Pages), and Olympiad Stream pages, verifies each CTA link leads to a reachable PDP (Product Detail Page), checks that prices match, and confirms a purchase button is present — on both **desktop** and **mobile** viewports, in parallel.

---

## Table of Contents

1. [Features](#features)
2. [Quick Start](#quick-start)
3. [Scheduled Runs (Claude Code)](#scheduled-runs-claude-code)
4. [Configuration](#configuration)
5. [Architecture](#architecture)
6. [Design Patterns](#design-patterns)
7. [Data Model](#data-model)
8. [Validation Rules](#validation-rules)
9. [Reports & Alerts](#reports--alerts)
10. [Adding a New Validator](#adding-a-new-validator)
11. [Adding a New Page Handler](#adding-a-new-page-handler)
12. [Backlog & Roadmap](#backlog--roadmap)

---

## Features

| Feature | Details |
|---------|---------|
| 🔗 **Broken-link detection** | Confirms every CTA card link navigates to a distinct PDP |
| 💰 **Price-mismatch detection** | Compares card price vs PDP price numerically (strips ₹, commas) |
| 🛒 **Purchase-button detection** | Scans the PDP for Enroll Now / Buy Now / Select Batch CTAs |
| 📱 **Dual-viewport** | Runs desktop (1920×1080) + mobile (iPhone XR 390×844) in parallel |
| ⚡ **PDP result cache** | Thread-safe in-memory cache avoids re-visiting the same PDP URL |
| 🏃 **Parallel scraping** | Up to 4 URL workers per viewport thread (ThreadPoolExecutor) |
| 🗃️ **SQLite persistence** | Every run is stored in `scraped_data.db` with a unique `run_id` |
| 📄 **Markdown reports** | Human-readable per-run reports saved to `reports/` |
| 📧 **Email alerts** | Configurable SMTP email with the report attached (optional) |
| 🔌 **Modular validators** | Chain-of-Responsibility pattern — add rules without touching the scraper |

---

## Quick Start

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install the Chromium browser used by Playwright
playwright install chromium

# 3. Run a full scrape + validate cycle
python3 scraper.py

# 4. (Optional) Run the unit tests
python3 -m pytest
```

> **Tip:** Pass a custom URL file as an argument:
> ```bash
> python3 scraper.py my_urls.txt
> ```

---

## Scheduled Runs (Claude Code)

WatchDog runs automatically via a **Claude Code scheduled task** — no CI/CD pipeline needed. The task is configured to execute daily at **2:00 AM** local time.

To run manually at any time:

```bash
python3 scraper.py
```

The scheduled task (`watchdog-daily-scrape`) will:
1. Verify Playwright browsers are installed
2. Run the scraper
3. Summarize results (courses scraped, issues found, duration)
4. Highlight any CRITICAL or HIGH severity issues

---

## Configuration

### `urls.txt` — Pages to scrape

Group URLs by page type using `[SECTION]` headers:

```
# Homepage (one per file)
[HOME]
https://allen.in/

# Product Listing Pages
[PLP_PAGES]
https://allen.in/online-coaching-jee
https://allen.in/online-coaching-neet

# Olympiad / Stream pages
[STREAM_PAGES]
https://allen.in/international-olympiads

# Results pages (same handler as STREAM_PAGES)
[RESULTS_PAGES]
https://allen.in/jee-result
```

Lines starting with `#` and blank lines are ignored.

### `email_config.json` — Email alerts (optional)

Copy `email_config.example.json` and fill in your credentials:

```json
{
  "enabled": true,
  "send_on": "errors",
  "smtp": {
    "host": "smtp.gmail.com",
    "port": 587,
    "use_tls": true,
    "username": "you@gmail.com",
    "password": "<gmail-app-password>"
  },
  "from": "WatchDog <you@gmail.com>",
  "to": ["team@example.com"]
}
```

| `send_on` value | Behaviour |
|----------------|-----------|
| `"always"` | Send after every run |
| `"errors"` | Send only when issues are found *(default)* |
| `"never"` | Disable email entirely |

> **Note:** Environment variables (`EMAIL_USERNAME`, `EMAIL_PASSWORD`, `EMAIL_TO`,
> `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_SEND_ON`, `EMAIL_ENABLED`) can override
> `email_config.json` values if set in your shell profile. This is useful if you
> prefer not to store credentials in a JSON file.

### Runtime Tuning (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCHDOG_MAX_WORKERS` | `4` | URL-level concurrency per viewport |
| `WATCHDOG_WAIT_MS` | `10000` | Timeout for card selector (ms) |
| `WATCHDOG_RETRIES` | `1` | Retry count if cards don't appear |
| `WATCHDOG_RETRY_BACKOFF_MS` | `2000` | Sleep between retries (ms) |
| `WATCHDOG_NAV_JITTER_MS` | `0` | Random pre-request delay ceiling (ms) |
| `WATCHDOG_FAIL_ON_EMPTY` | `false` | Raise on empty card lists |
| `WATCHDOG_ARTIFACT_DIR` | `artifacts/watchdog` | Debug artifact path |

Desktop and mobile viewports always run in parallel (2 viewport threads, each with up to `WATCHDOG_MAX_WORKERS` browser instances).

---

## Architecture

```
scraper.py
├── ScraperEngine          — entry point; orchestrates runs & threads
├── DatabaseManager        — SQLite reads/writes (WAL mode, thread-safe)
├── PdpCache               — thread-safe in-memory PDP result cache
├── ProgressTracker        — [N/total] log prefix per viewport
│
├── BasePageHandler (ABC)  — shared helpers: clean_price, verify_pdp, extract_cta_link
│   ├── HomepageHandler    — tab-based cards (JEE / NEET / Classes 6-10)
│   ├── PLPHandler         — filter-pill based course listings
│   └── StreamHandler      — class-tab based Olympiad / Results listings
│
validation_service.py
└── ValidationService      — builds validator chain; queries DB; logs results
    │
    validators/
    ├── __init__.py
    ├── base_validator.py          — BaseValidator (ABC) + ValidationResult dataclass
    ├── purchase_cta_validator.py  — CTA_BROKEN / CTA_MISSING checks
    └── price_mismatch_validator.py — PRICE_MISMATCH check
│
report_generator.py
└── ReportGenerator        — produces a Markdown report in reports/
│
email_service.py
└── EmailService           — SMTP sender; attaches the Markdown report
```

### Execution flow

```
ScraperEngine.run()
  │
  ├── DatabaseManager.create_run()        → run_id
  │
  ├── [Thread: desktop]  _run_viewport()
  │   └── [Workers × 4]  _scrape_one_url()  →  Handler.scrape()  →  verify_pdp()
  │                                                                  └─ PdpCache
  │
  ├── [Thread: mobile]   _run_viewport()   (same structure, iPhone XR UA)
  │
  ├── ValidationService.validate_all_courses(run_id)
  │   └── validator chain: PurchaseCTAValidator → PriceMismatchValidator
  │
  ├── ReportGenerator.save()              → reports/report_YYYY-MM-DD_HH-MM-SS.md
  │
  └── EmailService.send_report()          → SMTP (if configured)
```

---

## Design Patterns

| Pattern | Where used | Purpose |
|---------|-----------|---------|
| **Strategy** | `HomepageHandler`, `PLPHandler`, `StreamHandler` | Each page type has its own scraping algorithm; `ScraperEngine` selects the right one via `handler_map` |
| **Chain of Responsibility** | `BaseValidator.set_next()` | Validators are linked; each passes results to the next without coupling |
| **Template Method** | `BaseValidator._validate()` | The public `validate()` handles chaining and enrichment; subclasses only implement `_validate()` |

---

## Data Model

### `runs` table

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | INTEGER PK | Auto-incremented per `ScraperEngine.run()` call |
| `started_at` | DATETIME | UTC timestamp of run start |

### `courses` table

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Row ID |
| `run_id` | INTEGER FK | Links to `runs.run_id` |
| `base_url` | TEXT | Listing page URL where the card was found |
| `course_name` | TEXT | Course title extracted from the card |
| `cta_link` | TEXT | URL the card CTA navigates to |
| `price` | TEXT | Raw price string from the card (e.g. `₹ 93,500`) |
| `pdp_price` | TEXT | Raw price string from the PDP |
| `cta_status` | TEXT | `Found (Enroll Now)` ∣ `Not Found` ∣ `N/A` ∣ `Error` |
| `is_broken` | INTEGER | `1` if CTA link didn't lead to a different page |
| `price_mismatch` | INTEGER | `1` if card price ≠ PDP price numerically |
| `viewport` | TEXT | `desktop` ∣ `mobile` |
| `timestamp` | DATETIME | UTC insert time |

---

## Validation Rules

Validators run **after** scraping, against the `courses` table for the current `run_id`.

### `PurchaseCTAValidator`

Checks that a user can actually purchase a course. Issues are raised in order — once a CRITICAL issue is found, the validator stops for that record (no point checking an unreachable PDP).

| Check | Type | Severity | Condition |
|-------|------|----------|-----------|
| No CTA link | `CTA_BROKEN` | 🔴 CRITICAL | `cta_link` is empty/`N/A`/`Error` |
| Link stays on listing page | `CTA_BROKEN` | 🔴 CRITICAL | `is_broken == 1` or link == `base_url` |
| PDP has no purchase button | `CTA_MISSING` | 🟠 HIGH | `cta_status == 'Not Found'` |

### `PriceMismatchValidator`

| Check | Type | Severity | Condition |
|-------|------|----------|-----------|
| Card vs PDP price differ | `PRICE_MISMATCH` | 🟡 MEDIUM | Both prices present but numeric values differ |

> **Note:** If either price is missing (`N/A`, `Not Found`, `Error`), no mismatch is raised — the check is inconclusive.

---

## Reports & Alerts

### Markdown report (`reports/`)

Each run saves a report at `reports/report_YYYY-MM-DD_HH-MM-SS.md` containing:

1. **Header** — date, duration, URLs scraped
2. **Summary table** — courses and issue counts per viewport (desktop / mobile)
3. **Errors by URL** — issue count per listing page
4. **Validation Issues** — counts by type and severity
5. **Issue Details** — full table per issue type (course, URL, viewport, expected vs actual)

### Email alert

If `email_config.json` is present and `enabled: true`, WatchDog sends an HTML email summary with the Markdown report attached. See [Configuration](#configuration) for setup.

---

## Adding a New Validator

Three steps, no changes to `scraper.py` required:

**Step 1** — Create `validators/my_validator.py`:

```python
from .base_validator import BaseValidator, ValidationResult

class MyValidator(BaseValidator):
    def _validate(self, course_data: dict) -> list[ValidationResult]:
        issues = []
        # your logic here — inspect any key in course_data
        return issues
```

**Step 2** — Export from `validators/__init__.py`:

```python
from .my_validator import MyValidator
```

**Step 3** — Append to the chain in `validation_service.py`:

```python
cta = PurchaseCTAValidator()
price = PriceMismatchValidator()
my_val = MyValidator()

cta.set_next(price).set_next(my_val)
return cta
```

#### Available `course_data` keys

| Key | Example value |
|-----|--------------|
| `course_name` | `"JEE 2025 Dropper"` |
| `base_url` | `"https://allen.in/online-coaching-jee"` |
| `cta_link` | `"https://allen.in/buy/jee-dropper"` |
| `price` | `"₹ 93,500"` |
| `pdp_price` | `"₹ 93,500"` |
| `cta_status` | `"Found (Enroll Now)"` |
| `is_broken` | `0` |
| `price_mismatch` | `0` |
| `viewport` | `"desktop"` |

---

## Adding a New Page Handler

**Step 1** — Create a class in `scraper.py` extending `BasePageHandler`:

```python
class MyPageHandler(BasePageHandler):
    @staticmethod
    def can_handle(url: str) -> bool:
        return "/my-section/" in url

    def scrape(self, url: str):
        self.page.goto(url, wait_until="domcontentloaded")
        # locate cards, call self.verify_pdp(), self.db.save_batch()
```

**Step 2** — Register a URL section tag in `ScraperEngine.handler_map`:

```python
self.handler_map = {
    "HOME":         HomepageHandler,
    "PLP_PAGES":    PLPHandler,
    "STREAM_PAGES": StreamHandler,
    "MY_PAGES":     MyPageHandler,   # ← new
}
```

**Step 3** — Add URLs under `[MY_PAGES]` in `urls.txt`.

---

## Backlog & Roadmap

### Completed ✅

- [x] Homepage course card CTA validation
- [x] PLP course card CTA validation
- [x] Stream / Olympiad page CTA validation (carousel + tabbed layouts)
- [x] Price mismatch detection (card vs PDP)
- [x] Dual-viewport scraping (desktop + mobile)
- [x] Modular Chain-of-Responsibility validator system
- [x] Per-run SQLite tracking (`run_id`)
- [x] Email alert notifications
- [x] Markdown report generation with per-URL error summary
- [x] Local Claude Code scheduled task for daily execution (replaced GitHub Actions)

### Pending 🔲

- [ ] Sticky banner clickability check
- [ ] Alert system — Slack webhook notifications (Phase 2)
- [ ] YAML-based rule configuration (severity thresholds per alert channel)
- [ ] DLP course support (currently skipped upstream)

### Phase 2 — Alert Channels

Goal: route validation reports to Email / Slack based on a severity threshold.

```
alerters/
├── base_alerter.py     — Abstract base
├── console_alerter.py  — Current log output (default)
├── email_alerter.py    — SMTP email notifications
└── slack_alerter.py    — Slack webhook alerts
```

`AlertService` will sit between `ValidationService` and the alerters, filtering by severity before dispatching. Planned config:

```yaml
# config/watchdog.yaml
alerters:
  - type: email
    severity_threshold: HIGH
    smtp_host: smtp.gmail.com
    recipients: [team@example.com]
  - type: slack
    severity_threshold: MEDIUM
    webhook_url: https://hooks.slack.com/...
    channel: "#watchdog-alerts"
```
