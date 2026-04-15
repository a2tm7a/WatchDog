"""
Per-URL check configuration for WatchDog.

Loads config/url_checks.yaml and provides CheckConfig.enabled_checks_for(url)
to filter ValidationResult objects by check type for a given URL.
"""
import logging
import yaml
from typing import Dict, List
from pydantic import BaseModel, field_validator
from constants import KNOWN_CHECK_TYPES


class UrlCheckSpec(BaseModel):
    enabled: List[str]

    @field_validator("enabled")
    @classmethod
    def _warn_unknown_check_names(cls, v: List[str]) -> List[str]:
        unknown = set(v) - KNOWN_CHECK_TYPES
        if unknown:
            logging.warning(
                "Unknown check names in config (will never match any result): %s", unknown
            )
        return v


class CheckConfig(BaseModel):
    version: int = 1
    defaults: UrlCheckSpec
    urls: Dict[str, UrlCheckSpec] = {}

    def enabled_checks_for(self, url: str) -> frozenset:
        """Return the set of enabled check type strings for a given URL.

        Normalizes trailing slashes on both the lookup URL and config keys
        so that 'https://allen.in/' and 'https://allen.in' resolve identically.
        Falls back to defaults.enabled when the URL is not explicitly configured.
        """
        normalized = url.rstrip("/")
        for config_url, spec in self.urls.items():
            if config_url.rstrip("/") == normalized:
                return frozenset(spec.enabled)
        return frozenset(self.defaults.enabled)

    @classmethod
    def load(cls, path: str = "config/url_checks.yaml") -> "CheckConfig":
        """Load CheckConfig from a YAML file.

        Returns a permissive default config (all known checks enabled for all URLs)
        if the file does not exist, so existing deployments without a config file
        continue to behave exactly as before.
        """
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return cls.model_validate(data)
        except FileNotFoundError:
            logging.warning(
                "Check config %s not found — running all checks for all URLs", path
            )
            return cls(defaults=UrlCheckSpec(enabled=list(KNOWN_CHECK_TYPES)))
