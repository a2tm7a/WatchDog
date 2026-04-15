# Phase 2 — Authenticated Validation · CONTEXT.md

**Project:** WatchDog (allen.in)
**Phase:** 2 — Authenticated Validation
**Status:** Ready for planning
**Created:** 2026-04-15

**Login UI contract:** See [AUTH_UI_FLOW.md](AUTH_UI_FLOW.md) for the allen.in modal flow, unsupported branches (OTP/CAPTCHA), and debug env vars.

---

## Phase Scope (Locked)

Phase 2 extends core validation (R-01, R-02, R-03) across all 11 stream × class profiles using a single shared test account. Sticky banner and tab checks are explicitly deferred.

**In scope:**
| Req | Requirement | Ships order |
|-----|-------------|-------------|
| R-23 | Secrets Management | First — test credentials need secure storage before anything else |
| R-20 | Test Account Management | Login + sequential profile switching via UI dropdown |
| R-21 | Session & Cookie Handling | Re-login on session expiry; no silent guest fallback |
| R-09 | Authenticated Mode Scraping | R-01, R-02, R-03 across all 11 stream × class profiles |

**Explicitly deferred to Phase 3:**
- R-04 — Wrong Sticky Banner (Page Context) — OQ-11 unresolved (sticky stream selector unknown)
- R-05 — Wrong Sticky Banner (User Context) — same blocker
- R-06 — Default Tab Validation — OQ-12 unresolved (tab pre-selection mechanism unknown)

---

## Decisions

### Run Architecture

**Decision:** One `run_id` per profile. A full nightly job produces 4 run_ids: 1 for guest + 1 for each of the 3 stream profiles (JEE, NEET, Classes 6-10).

**Rationale:** Minimises schema changes. The existing `runs` table only needs two new columns: `mode` (TEXT: `"guest"` or `"authenticated"`) and `profile` (TEXT: e.g. `"JEE_Class11"`, NULL for guest). All existing queries on `run_id` continue to work unchanged.

**Profile list (3 profiles — stream-level only for Phase 2):**
| Stream | profile key |
|--------|-------------|
| JEE | `JEE` |
| NEET | `NEET` |
| Classes 6-10 | `Classes610` |

> **Scoping decision:** Phase 2 validates at stream level only (3 profiles). Per-class profiles (the full 11-profile matrix) are deferred to a later phase once stream-level validation is stable.

### Profile Execution Order

**Decision:** Sequential. Login once → switch profile (UI dropdown) → scrape → switch profile → scrape → … → logout.

**Rationale:** Reuses a single authenticated browser session across all 11 profiles. Safer for bot detection. Simpler session expiry handling (one re-login point, not 11).

**Viewport behaviour:** Each profile still runs on both desktop and mobile (existing `ThreadPoolExecutor(max_workers=2)` parallelism is preserved per profile).

### Profile Switching Mechanism

**Decision:** UI interaction — clicking the stream × class dropdown on allen.in.

**Login URL:** Unknown. The plan must include a discovery/investigation step: navigate allen.in, find the login form URL, capture the form selectors (username/password fields, submit button), and verify a successful login.

**Profile switching:** After login, navigate to the profile switcher UI, select the target stream, then select the target class. The page reloads after each selection — use `page.wait_for_load_state("networkidle")` to confirm the switch is complete before scraping begins.

**Session expiry handling (R-21):** If any page navigation returns a 401, redirects to login, or a known "session expired" indicator is detected, trigger a full re-login and retry the current profile. Do NOT silently fall back to guest-mode data — raise `SCRAPER_ERROR` if re-login fails.

**Credentials source:** `test_credentials.json` exists with `form_id` and `password`. After R-23 (secrets management) ships, credentials move to environment variables (`WATCHDOG_TEST_FORM_ID`, `WATCHDOG_TEST_PASSWORD`). The scraper reads from env vars, falling back to `test_credentials.json` during local development only.

### Product Rules (OQ resolutions)

**OQ-7 — Does login change which cards are visible?**
Unresolved. Proceed with the assumption that the same card catalogue is visible to all profiles. Add a card-count guard: if an authenticated profile returns significantly fewer cards than the guest baseline for the same URL (threshold TBD during dev, suggest < 50% of guest card count), raise `SCRAPER_ERROR` and alert.

**OQ-8 — Do prices differ between guest and authenticated?**
Resolved: Prices are expected to be the same across all modes and profiles. Price mismatch checks (R-03) apply unchanged in authenticated mode.

**OQ-9 — Is "Continue Learning" a valid purchase CTA?**
Resolved: **No.** A "Continue Learning" button must be treated as `CTA_MISSING`. Every course card must show a purchasable CTA (Enroll Now / Buy Now / Select Batch) regardless of whether the test account is already enrolled.

*Implication:* The test account must NOT be enrolled in any courses at the start of a run. The plan should include a note to keep the test account unenrolled, or add a pre-run check that raises a warning if enrolled courses are detected.

### Secrets Management (R-23)

**Decision:** All secrets (SMTP credentials, test account credentials, Slack webhook — when added in Phase 3) move to environment variables. Config files (`email_config.json`, `test_credentials.json`) are kept as gitignored local fallbacks for development only. `.example` files are the committed templates.

**New env vars introduced in Phase 2:**
- `WATCHDOG_TEST_FORM_ID` — test account form_id
- `WATCHDOG_TEST_PASSWORD` — test account password

**Existing env vars to formalise (from email_config.json):**
- `WATCHDOG_SMTP_SERVER`, `WATCHDOG_SMTP_PORT`, `WATCHDOG_SMTP_USER`, `WATCHDOG_SMTP_PASSWORD`, `WATCHDOG_EMAIL_FROM`, `WATCHDOG_EMAIL_TO`, `WATCHDOG_SEND_ON`

The scheduled task (Claude Code) must be updated to pass these env vars into the scraper process.

---

## Schema Changes Required

```sql
-- Add to runs table
ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'guest';
ALTER TABLE runs ADD COLUMN profile TEXT;  -- NULL for guest runs
```

No other table changes are required for Phase 2 scope. The `courses` and `issues` tables inherit context via `run_id`.

---

## Out of Scope (Do Not Plan)

- R-04, R-05 (sticky banner checks) — deferred to Phase 3
- R-06 (tab pre-selection check) — deferred to Phase 3
- R-10 (per-profile differential reporting) — Phase 3
- R-13, R-14 (Slack alerts, YAML config) — Phase 3
- R-22 (card-count guard as a formal requirement) — Phase 3 (informal guard added in Phase 2 as a safety net only)

---

## Open Questions Still Blocking (must resolve before go-live)

| # | Question | Impact |
|---|----------|--------|
| OQ-5 | Who owns CRITICAL/HIGH alerts and what is the response SLA? | Must be agreed before Phase 2 authenticated alerts go live |
| OQ-7 | Does login actually change visible card counts? | Verify during dev; card-count guard is the safety net |

---

## Next Step

Run `/gsd-plan-phase 2` — the planner should produce a PLAN.md that:
1. Starts with a live-site investigation task (login URL, form selectors, profile switcher selectors)
2. Implements R-23 (secrets management) first
3. Adds `mode` + `profile` columns to the `runs` table
4. Builds `AuthSession` (login, switch-profile, re-login on expiry) as a new module
5. Wires authenticated mode into `ScraperEngine` as sequential profile passes after the guest pass
6. Updates reports to show per-profile breakdowns
