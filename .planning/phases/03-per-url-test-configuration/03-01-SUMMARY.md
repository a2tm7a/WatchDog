---
phase: 03-per-url-test-configuration
plan: 01
subsystem: config
tags: [pydantic, yaml, check-config, tdd, constants]
dependency_graph:
  requires: []
  provides: [CheckConfig, UrlCheckSpec, KNOWN_CHECK_TYPES, config/url_checks.yaml]
  affects: [validation_service.py, scraper.py]
tech_stack:
  added: []
  patterns: [Pydantic BaseModel, yaml.safe_load, TDD RED-GREEN]
key_files:
  created:
    - tests/test_check_config.py
    - check_config.py
    - config/url_checks.yaml
  modified:
    - constants.py
decisions:
  - "Use frozenset (not set) for KNOWN_CHECK_TYPES — immutable, communicates registry semantics"
  - "Trailing-slash normalization via rstrip('/') on both config key and lookup URL"
  - "FileNotFoundError returns permissive defaults — zero-downside on new deployments"
  - "@field_validator logs WARNING for unknown check names (not raise) — future-proofs new validators"
metrics:
  duration: ~10 min
  completed: 2026-04-15
  tasks_completed: 3
  files_created: 3
  files_modified: 1
---

# Phase 3 Plan 1: CheckConfig Module + YAML Check Matrix Summary

**One-liner:** Pydantic CheckConfig with yaml.safe_load(), KNOWN_CHECK_TYPES registry, and 11-entry production url_checks.yaml for false-positive suppression on results pages.

---

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Write test scaffold for CheckConfig (TDD RED) | 7a26a09 | tests/test_check_config.py |
| 2 | Implement CheckConfig module (TDD GREEN) | 287093a | constants.py, check_config.py |
| 3 | Create config/url_checks.yaml production check matrix | 9d9eeb0 | config/url_checks.yaml |

---

## What Was Built

The standalone config layer for WatchDog's per-URL check filtering:

1. **`constants.py`** — Extended with `KNOWN_CHECK_TYPES: frozenset` (single source of truth for valid `ValidationResult.type` strings: `CTA_BROKEN`, `CTA_MISSING`, `PRICE_MISMATCH`).

2. **`check_config.py`** — `UrlCheckSpec` and `CheckConfig` Pydantic models:
   - `CheckConfig.load(path)` — loads YAML via `yaml.safe_load()`, catches `FileNotFoundError` and returns permissive defaults (all checks enabled)
   - `CheckConfig.enabled_checks_for(url)` — returns `frozenset` of enabled check type strings; normalizes trailing slashes on both lookup and config keys
   - `@field_validator` on `UrlCheckSpec.enabled` — logs WARNING for names not in `KNOWN_CHECK_TYPES` (typo detection without crashing)

3. **`config/url_checks.yaml`** — Production check matrix:
   - `defaults`: all 3 checks enabled for any unconfigured URL
   - 11 per-URL overrides: 10 results pages + 1 registration page (`aiot-register`) — `CTA_BROKEN` only (prevents `CTA_MISSING` and `PRICE_MISMATCH` false positives on non-purchase pages)

4. **`tests/test_check_config.py`** — 7 pytest tests covering all behaviors (TDD RED→GREEN):
   - `test_load_valid_config`, `test_load_missing_file`
   - `test_url_override`, `test_url_fallback_to_defaults`
   - `test_trailing_slash_normalization` (both slash/no-slash directions)
   - `test_unknown_check_name_warns` (caplog assertion)
   - `test_empty_enabled_list_allowed`

---

## Verification Results

```
python3 -m pytest tests/test_check_config.py -x -q
# → 7 passed in 0.07s

python3 -m pytest tests/ -q
# → 237 passed in 0.68s (zero regressions)

python3 -c "from check_config import CheckConfig; c = CheckConfig.load('config/url_checks.yaml'); assert c.version == 1; assert len(c.urls) == 11; print('OK')"
# → OK
```

---

## Deviations from Plan

None — plan executed exactly as written.

---

## Threat Mitigations Applied

| Threat ID | Mitigation |
|-----------|-----------|
| T-03-01 | `yaml.safe_load()` used exclusively in `CheckConfig.load()` — `yaml.load()` never used |
| T-03-03 | `@field_validator("enabled")` in `UrlCheckSpec` logs WARNING for unknown check names at load time |

---

## Known Stubs

None — all data is wired. `CheckConfig` returns real config from YAML; no placeholder values.

## Threat Flags

None — no new network endpoints, auth paths, or trust-boundary changes introduced.

---

## Self-Check: PASSED

```bash
[ -f "tests/test_check_config.py" ] && echo "FOUND" || echo "MISSING"   # FOUND
[ -f "check_config.py" ] && echo "FOUND" || echo "MISSING"               # FOUND
[ -f "config/url_checks.yaml" ] && echo "FOUND" || echo "MISSING"        # FOUND
git log --oneline | grep "7a26a09"   # FOUND
git log --oneline | grep "287093a"   # FOUND
git log --oneline | grep "9d9eeb0"   # FOUND
```
