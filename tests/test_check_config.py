"""
Unit tests for CheckConfig — per-URL check configuration module.

Tests exercise all behaviors in isolation: YAML loading, missing file
graceful degradation, URL override matching, trailing-slash normalization,
unknown check name warnings, and empty enabled list acceptance.
"""
import logging
import textwrap
import pytest
from check_config import CheckConfig, UrlCheckSpec


def test_load_valid_config(tmp_path):
    """CheckConfig.load() parses a valid YAML file and returns a CheckConfig instance."""
    config_file = tmp_path / "url_checks.yaml"
    config_file.write_text(textwrap.dedent("""\
        version: 1
        defaults:
          enabled:
            - CTA_BROKEN
            - CTA_MISSING
            - PRICE_MISMATCH
        urls: {}
    """))

    config = CheckConfig.load(str(config_file))

    assert config.version == 1
    assert set(config.defaults.enabled) == {"CTA_BROKEN", "CTA_MISSING", "PRICE_MISMATCH"}


def test_load_missing_file():
    """CheckConfig.load() returns a permissive default when the file does not exist."""
    result = CheckConfig.load("/tmp/definitely_not_here_12345.yaml")

    assert isinstance(result, CheckConfig)
    assert set(result.defaults.enabled) == {"CTA_BROKEN", "CTA_MISSING", "PRICE_MISMATCH"}


def test_url_override():
    """enabled_checks_for() returns URL-specific check set when URL is in config."""
    config = CheckConfig(
        defaults=UrlCheckSpec(enabled=["CTA_BROKEN", "CTA_MISSING", "PRICE_MISMATCH"]),
        urls={"https://allen.in/jee/results-2025": UrlCheckSpec(enabled=["CTA_BROKEN"])},
    )

    result = config.enabled_checks_for("https://allen.in/jee/results-2025")

    assert result == {"CTA_BROKEN"}


def test_url_fallback_to_defaults():
    """enabled_checks_for() returns defaults.enabled when URL is not in config."""
    config = CheckConfig(
        defaults=UrlCheckSpec(enabled=["CTA_BROKEN", "CTA_MISSING", "PRICE_MISMATCH"]),
        urls={"https://allen.in/jee/results-2025": UrlCheckSpec(enabled=["CTA_BROKEN"])},
    )

    result = config.enabled_checks_for("https://allen.in/jee")

    assert result == {"CTA_BROKEN", "CTA_MISSING", "PRICE_MISMATCH"}


def test_trailing_slash_normalization():
    """URL trailing-slash normalization: both slash and no-slash variants resolve to same config."""
    config = CheckConfig(
        defaults=UrlCheckSpec(enabled=["CTA_BROKEN", "CTA_MISSING", "PRICE_MISMATCH"]),
        urls={"https://allen.in/jee/results-2025": UrlCheckSpec(enabled=["CTA_BROKEN"])},
    )

    # Config key has no trailing slash; lookup has trailing slash
    result_with_slash = config.enabled_checks_for("https://allen.in/jee/results-2025/")
    assert result_with_slash == {"CTA_BROKEN"}

    # Config key with trailing slash; lookup without trailing slash
    config2 = CheckConfig(
        defaults=UrlCheckSpec(enabled=["CTA_BROKEN", "CTA_MISSING", "PRICE_MISMATCH"]),
        urls={"https://allen.in/jee/results-2025/": UrlCheckSpec(enabled=["CTA_BROKEN"])},
    )
    result_without_slash = config2.enabled_checks_for("https://allen.in/jee/results-2025")
    assert result_without_slash == {"CTA_BROKEN"}


def test_unknown_check_name_warns(tmp_path, caplog):
    """CheckConfig.load() logs a WARNING for unknown check names and does not raise."""
    config_file = tmp_path / "url_checks.yaml"
    config_file.write_text(textwrap.dedent("""\
        version: 1
        defaults:
          enabled:
            - CTA_BROKEN
            - TOTALLY_UNKNOWN
        urls: {}
    """))

    with caplog.at_level(logging.WARNING):
        result = CheckConfig.load(str(config_file))

    assert result is not None
    assert any("TOTALLY_UNKNOWN" in r.message for r in caplog.records)


def test_empty_enabled_list_allowed():
    """enabled: [] is accepted; enabled_checks_for returns an empty set."""
    config = CheckConfig(
        defaults=UrlCheckSpec(enabled=["CTA_BROKEN", "CTA_MISSING", "PRICE_MISMATCH"]),
        urls={"https://allen.in/aiot-register": UrlCheckSpec(enabled=[])},
    )

    result = config.enabled_checks_for("https://allen.in/aiot-register")

    assert result == set()
