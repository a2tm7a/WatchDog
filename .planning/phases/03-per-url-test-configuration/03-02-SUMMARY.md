---
phase: 03-per-url-test-configuration
plan: 02
subsystem: validation
tags: [check-config, validation-service, scraper, filtering, tdd]
dependency_graph:
  requires: [CheckConfig, UrlCheckSpec, config/url_checks.yaml]
  provides: [validate_course(check_config=), validate_all_courses(check_config=), CheckConfig wired in ScraperEngine.run()]
  affects: [validation_service.py, scraper.py, tests/test_validation_service.py]
tech_stack:
  added: []
  patterns: [Optional parameter with TYPE_CHECKING guard, frozenset membership filter, TDD RED-GREEN]
key_files:
  created: []
  modified:
    - validation_service.py
    - scraper.py
    - tests/test_validation_service.py
decisions:
  - "Use TYPE_CHECKING guard for CheckConfig import in validation_service.py — avoids circular import risk, keeps dependency optional at module load time"
  - "check_config loaded once in ScraperEngine.run() before first pass and reused for recheck pass — same config object, read-only, correct for nightly job"
  - "Optional[Any] annotation for check_config parameter — avoids requiring check_config.py to be importable in test environments that mock other deps"
  - "Only unauthenticated validate_all_courses() call sites updated (lines ~418, ~440) per plan scope; auth path call sites unchanged"
metrics:
  duration: ~5 min
  completed: 2026-04-15
  tasks_completed: 2
  files_created: 0
  files_modified: 3
---

# Phase 3 Plan 2: Wire CheckConfig into ValidationService and ScraperEngine Summary

**One-liner:** CheckConfig filtering wired into ValidationService.validate_course() via frozenset membership test, propagated through validate_all_courses(), and loaded once in ScraperEngine.run() for both first-pass and recheck-pass validation calls.

---

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Update ValidationService to accept and apply CheckConfig (TDD) | e81ff12 | validation_service.py, tests/test_validation_service.py |
| 2 | Wire CheckConfig into ScraperEngine.run() | 8fa2a6f | scraper.py |

---

## What Was Built

The runtime filtering layer that connects the CheckConfig config module (built in Plan 01) to the actual validation pipeline:

1. **`validation_service.py`** — Two method signature updates:
   - `validate_course(course_data, check_config=None)` — when `check_config` is provided, calls `check_config.enabled_checks_for(base_url)` to get a frozenset of enabled check types, then filters raw results to only those whose `result.type` is in the frozenset. When `check_config=None`, returns raw results unchanged (backward compat).
   - `validate_all_courses(run_id=None, check_config=None)` — propagates `check_config` to each `validate_course()` call in the inner loop. All other logic (viewport stamping, result accumulation) unchanged.
   - `TYPE_CHECKING` guard used for `CheckConfig` import — avoids circular import risk, keeps dep optional at runtime.

2. **`scraper.py`** — Three changes in `ScraperEngine.run()`:
   - Added `from check_config import CheckConfig` import
   - `check_config = CheckConfig.load("config/url_checks.yaml")` loaded once before the first validation pass
   - Both `validate_all_courses()` call sites (first pass line ~419, recheck pass line ~441) updated to pass `check_config=check_config`

3. **`tests/test_validation_service.py`** — New `test_check_config_filtering()` test (TDD RED→GREEN):
   - Builds a `CheckConfig` with `enabled=["CTA_BROKEN"]` for `https://example.com/course`
   - Calls `validate_course()` on a course that would produce both `CTA_BROKEN` and `PRICE_MISMATCH`
   - Asserts only `CTA_BROKEN` in filtered results; `PRICE_MISMATCH` absent
   - Also asserts backward compat: `check_config=None` returns both result types

---

## Verification Results

```
python3 -m pytest tests/test_validation_service.py::test_check_config_filtering -x -q
# → 1 passed in 0.15s

python3 -m pytest tests/test_validation_service.py -x -q
# → 25 passed in 0.18s

python3 -m pytest tests/test_check_config.py -x -q
# → 7 passed in 0.07s

python3 -m pytest tests/ -q
# → 238 passed in 0.64s (zero regressions)

python3 -c "import scraper; print('scraper import OK')"
# → scraper import OK

# Integration filter check:
# → All imports OK
# → Filter integration check PASSED
# → Filtered types: {'CTA_BROKEN'}
```

---

## Deviations from Plan

None — plan executed exactly as written.

---

## Threat Mitigations Applied

| Threat ID | Mitigation |
|-----------|-----------|
| T-03-05 | Filter uses `frozenset` membership test (O(1), no code execution). Check type strings from config compared against `ValidationResult.type` strings — neither path executes arbitrary code. |
| T-03-06 | `check_config=None` backward-compat default intentional — not a security bypass. ValidationService runs in trusted local process. |
| T-03-07 | `base_url` from local SQLite DB (operator-controlled); not internet-user-supplied input. |
| T-03-08 | Same `CheckConfig` object (Pydantic model, immutable in practice) reused across both validation passes within a single `run()` call. |

---

## Known Stubs

None — all data wired. `validate_course()` calls `enabled_checks_for()` against the real `CheckConfig` loaded from disk; no placeholder values.

## Threat Flags

None — no new network endpoints, auth paths, or trust-boundary changes introduced.

---

## Self-Check: PASSED

```bash
[ -f "validation_service.py" ] && echo "FOUND" || echo "MISSING"          # FOUND
[ -f "scraper.py" ] && echo "FOUND" || echo "MISSING"                      # FOUND
[ -f "tests/test_validation_service.py" ] && echo "FOUND" || echo "MISSING" # FOUND
git log --oneline | grep "e81ff12"   # FOUND
git log --oneline | grep "8fa2a6f"   # FOUND
```
