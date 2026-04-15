"""
Tests for EmailService.

Covers:
- _should_send: all combinations of enabled/send_on/total_issues
- _load_config: missing file, JSON loading, env-var overrides, auto-enable,
  EMAIL_ENABLED=false, EMAIL_SEND_ON override, malformed JSON
- send_report: no-send path, SMTP exception → False, success → True,
  _smtp_send called with message
- _build_message: subject with/without issues, From/To headers,
  attachment present/absent
- _smtp_send: SMTP constructor args, starttls called, login called
"""
import json
import os
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from email_service import EmailService


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

NO_ISSUES = {"total_issues": 0, "by_type": {}, "by_severity": {}}
WITH_ISSUES = {
    "total_issues": 3,
    "by_type": {"CTA_BROKEN": 2, "PRICE_MISMATCH": 1},
    "by_severity": {"CRITICAL": 2, "MEDIUM": 1},
}

BASE_CONFIG = {
    "enabled": True,
    "send_on": "errors",
    "smtp": {
        "host": "smtp.example.com",
        "port": 587,
        "use_tls": True,
        "username": "user@example.com",
        "password": "secret",
    },
    "from": "WatchDog <user@example.com>",
    "to": ["recipient@example.com"],
}


def make_service(config=None):
    """Build an EmailService instance with an explicit config dict (bypasses file I/O)."""
    svc = EmailService.__new__(EmailService)
    svc.config = config if config is not None else {}
    return svc


# ---------------------------------------------------------------------------
# _should_send
# ---------------------------------------------------------------------------

class TestShouldSend:
    def test_empty_config_returns_false(self):
        assert make_service({})._should_send(WITH_ISSUES) is False

    def test_enabled_false_returns_false(self):
        cfg = {**BASE_CONFIG, "enabled": False}
        assert make_service(cfg)._should_send(WITH_ISSUES) is False

    def test_send_on_never_returns_false(self):
        cfg = {**BASE_CONFIG, "send_on": "never"}
        assert make_service(cfg)._should_send(WITH_ISSUES) is False

    def test_send_on_errors_no_issues_returns_false(self):
        assert make_service(BASE_CONFIG)._should_send(NO_ISSUES) is False

    def test_send_on_errors_with_issues_returns_true(self):
        assert make_service(BASE_CONFIG)._should_send(WITH_ISSUES) is True

    def test_send_on_always_no_issues_returns_true(self):
        cfg = {**BASE_CONFIG, "send_on": "always"}
        assert make_service(cfg)._should_send(NO_ISSUES) is True

    def test_send_on_always_with_issues_returns_true(self):
        cfg = {**BASE_CONFIG, "send_on": "always"}
        assert make_service(cfg)._should_send(WITH_ISSUES) is True

    def test_enabled_missing_defaults_to_false(self):
        cfg = {k: v for k, v in BASE_CONFIG.items() if k != "enabled"}
        assert make_service(cfg)._should_send(WITH_ISSUES) is False


# ---------------------------------------------------------------------------
# _load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_missing_file_returns_empty_config(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        svc = EmailService(config_path=path)
        assert svc.config == {}

    def test_loads_json_correctly(self, tmp_path):
        path = str(tmp_path / "config.json")
        with open(path, "w") as f:
            json.dump(BASE_CONFIG, f)
        svc = EmailService(config_path=path)
        assert svc.config["enabled"] is True
        assert svc.config["send_on"] == "errors"
        assert svc.config["to"] == ["recipient@example.com"]

    def test_env_username_overrides_json(self, tmp_path, monkeypatch):
        path = str(tmp_path / "config.json")
        with open(path, "w") as f:
            json.dump(BASE_CONFIG, f)
        monkeypatch.setenv("EMAIL_USERNAME", "env@example.com")
        monkeypatch.setenv("EMAIL_PASSWORD", "envpass")
        monkeypatch.setenv("EMAIL_TO", "dest@example.com")
        svc = EmailService(config_path=path)
        assert svc.config["smtp"]["username"] == "env@example.com"

    def test_env_password_overrides_json(self, tmp_path, monkeypatch):
        path = str(tmp_path / "config.json")
        with open(path, "w") as f:
            json.dump(BASE_CONFIG, f)
        monkeypatch.setenv("EMAIL_USERNAME", "u@e.com")
        monkeypatch.setenv("EMAIL_PASSWORD", "newpass")
        monkeypatch.setenv("EMAIL_TO", "d@e.com")
        svc = EmailService(config_path=path)
        assert svc.config["smtp"]["password"] == "newpass"

    def test_env_to_parsed_as_list(self, tmp_path, monkeypatch):
        path = str(tmp_path / "config.json")
        monkeypatch.setenv("EMAIL_USERNAME", "u@e.com")
        monkeypatch.setenv("EMAIL_PASSWORD", "p")
        monkeypatch.setenv("EMAIL_TO", "a@e.com, b@e.com, c@e.com")
        svc = EmailService(config_path=path)
        assert svc.config["to"] == ["a@e.com", "b@e.com", "c@e.com"]

    def test_env_to_strips_whitespace(self, tmp_path, monkeypatch):
        path = str(tmp_path / "config.json")
        monkeypatch.setenv("EMAIL_USERNAME", "u@e.com")
        monkeypatch.setenv("EMAIL_PASSWORD", "p")
        monkeypatch.setenv("EMAIL_TO", "  a@e.com  ,  b@e.com  ")
        svc = EmailService(config_path=path)
        assert "a@e.com" in svc.config["to"]
        assert "b@e.com" in svc.config["to"]

    def test_env_auto_enables_when_credentials_present(self, tmp_path, monkeypatch):
        path = str(tmp_path / "config.json")
        monkeypatch.setenv("EMAIL_USERNAME", "u@e.com")
        monkeypatch.setenv("EMAIL_PASSWORD", "p")
        monkeypatch.setenv("EMAIL_TO", "dest@e.com")
        svc = EmailService(config_path=path)
        assert svc.config.get("enabled") is True

    def test_env_enabled_false_disables_email(self, tmp_path, monkeypatch):
        path = str(tmp_path / "config.json")
        monkeypatch.setenv("EMAIL_USERNAME", "u@e.com")
        monkeypatch.setenv("EMAIL_PASSWORD", "p")
        monkeypatch.setenv("EMAIL_TO", "dest@e.com")
        monkeypatch.setenv("EMAIL_ENABLED", "false")
        svc = EmailService(config_path=path)
        assert svc.config.get("enabled") is False

    def test_env_send_on_override(self, tmp_path, monkeypatch):
        path = str(tmp_path / "config.json")
        monkeypatch.setenv("EMAIL_USERNAME", "u@e.com")
        monkeypatch.setenv("EMAIL_PASSWORD", "p")
        monkeypatch.setenv("EMAIL_TO", "dest@e.com")
        monkeypatch.setenv("EMAIL_SEND_ON", "always")
        svc = EmailService(config_path=path)
        assert svc.config.get("send_on") == "always"

    def test_malformed_json_does_not_raise(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            f.write("NOT { VALID JSON }")
        # Should not propagate exception
        svc = EmailService(config_path=path)
        assert isinstance(svc.config, dict)

    def test_env_host_default_is_gmail(self, tmp_path, monkeypatch):
        path = str(tmp_path / "config.json")
        monkeypatch.setenv("EMAIL_USERNAME", "u@e.com")
        monkeypatch.setenv("EMAIL_PASSWORD", "p")
        monkeypatch.setenv("EMAIL_TO", "d@e.com")
        svc = EmailService(config_path=path)
        assert svc.config["smtp"]["host"] == "smtp.gmail.com"

    def test_env_host_override(self, tmp_path, monkeypatch):
        path = str(tmp_path / "config.json")
        monkeypatch.setenv("EMAIL_USERNAME", "u@e.com")
        monkeypatch.setenv("EMAIL_PASSWORD", "p")
        monkeypatch.setenv("EMAIL_TO", "d@e.com")
        monkeypatch.setenv("EMAIL_HOST", "smtp.custom.com")
        svc = EmailService(config_path=path)
        assert svc.config["smtp"]["host"] == "smtp.custom.com"


# ---------------------------------------------------------------------------
# send_report
# ---------------------------------------------------------------------------

class TestSendReport:
    def test_returns_false_when_should_not_send(self):
        svc = make_service({})
        assert svc.send_report("/path/report.md", NO_ISSUES) is False

    def test_returns_false_on_smtp_exception(self):
        svc = make_service(BASE_CONFIG)
        with patch.object(svc, "_smtp_send", side_effect=Exception("Connection refused")):
            result = svc.send_report("/path/report.md", WITH_ISSUES)
        assert result is False

    def test_returns_true_on_successful_send(self, tmp_path):
        report_path = str(tmp_path / "report.md")
        with open(report_path, "w") as f:
            f.write("# Report")
        svc = make_service(BASE_CONFIG)
        with patch.object(svc, "_smtp_send"):
            result = svc.send_report(report_path, WITH_ISSUES)
        assert result is True

    def test_smtp_send_called_once(self, tmp_path):
        report_path = str(tmp_path / "report.md")
        with open(report_path, "w") as f:
            f.write("# Report")
        svc = make_service(BASE_CONFIG)
        with patch.object(svc, "_smtp_send") as mock_send:
            svc.send_report(report_path, WITH_ISSUES)
        mock_send.assert_called_once()

    def test_does_not_call_smtp_when_no_send(self):
        svc = make_service({})
        with patch.object(svc, "_smtp_send") as mock_send:
            svc.send_report("/path/report.md", NO_ISSUES)
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# _build_message
# ---------------------------------------------------------------------------

class TestBuildMessage:
    def _build(self, summary=WITH_ISSUES, report_path=""):
        svc = make_service(BASE_CONFIG)
        return svc._build_message(
            report_path,
            summary,
            run_id=42,
            start_time=datetime(2024, 1, 15, 10, 0, 0),
        )

    def test_subject_contains_date_stamp(self):
        """Subject is WatchDog Report — YYYY-MM-DD (issue counts live in HTML body)."""
        msg = self._build(WITH_ISSUES)
        assert "WatchDog Report — 2024-01-15" == msg["Subject"]

    def test_subject_guest_has_no_profile_suffix(self):
        msg = self._build(NO_ISSUES)
        assert msg["Subject"] == "WatchDog Report — 2024-01-15"
        assert "[Auth:" not in msg["Subject"]

    def test_subject_includes_auth_profile_when_set(self):
        svc = make_service(BASE_CONFIG)
        msg = svc._build_message(
            "",
            {"total_issues": 1, "by_type": {}, "by_severity": {}},
            run_id=1,
            start_time=datetime(2024, 1, 1),
            profile="JEE",
        )
        assert msg["Subject"] == "WatchDog Report — 2024-01-01 [Auth: JEE]"

    def test_from_header_set_correctly(self):
        assert self._build()["From"] == "WatchDog <user@example.com>"

    def test_to_header_set_correctly(self):
        assert "recipient@example.com" in self._build()["To"]

    def test_attaches_report_when_file_exists(self, tmp_path):
        report_path = str(tmp_path / "report.md")
        with open(report_path, "w") as f:
            f.write("# Report content")
        svc = make_service(BASE_CONFIG)
        msg = svc._build_message(report_path, WITH_ISSUES, run_id=1, start_time=None)
        payloads = msg.get_payload()
        assert len(payloads) == 2  # HTML body + file attachment

    def test_no_attachment_when_file_missing(self):
        msg = self._build(report_path="/nonexistent/path/report.md")
        payloads = msg.get_payload()
        assert len(payloads) == 1  # HTML body only

    def test_no_attachment_when_path_is_empty_string(self):
        msg = self._build(report_path="")
        payloads = msg.get_payload()
        assert len(payloads) == 1


# ---------------------------------------------------------------------------
# _smtp_send (mocked smtplib.SMTP)
# ---------------------------------------------------------------------------

class TestSmtpSend:
    def _call_smtp_send(self, cfg=None):
        svc = make_service(cfg or BASE_CONFIG)
        mock_msg = MagicMock()
        mock_msg.__getitem__ = MagicMock(return_value="test@example.com")
        mock_msg.as_string.return_value = "raw email content"

        with patch("smtplib.SMTP") as mock_cls:
            mock_server = MagicMock()
            mock_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_cls.return_value.__exit__ = MagicMock(return_value=False)
            svc._smtp_send(mock_msg)

        return mock_cls, mock_server

    def test_smtp_constructor_called_with_correct_host_port(self):
        mock_cls, _ = self._call_smtp_send()
        mock_cls.assert_called_once_with("smtp.example.com", 587, timeout=30)

    def test_starttls_called_when_use_tls_true(self):
        _, mock_server = self._call_smtp_send()
        mock_server.starttls.assert_called_once()

    def test_starttls_not_called_when_use_tls_false(self):
        cfg = {
            **BASE_CONFIG,
            "smtp": {**BASE_CONFIG["smtp"], "use_tls": False},
        }
        _, mock_server = self._call_smtp_send(cfg)
        mock_server.starttls.assert_not_called()

    def test_login_called_with_credentials(self):
        _, mock_server = self._call_smtp_send()
        mock_server.login.assert_called_once_with("user@example.com", "secret")

    def test_login_not_called_when_no_username(self):
        cfg = {
            **BASE_CONFIG,
            "smtp": {**BASE_CONFIG["smtp"], "username": ""},
        }
        _, mock_server = self._call_smtp_send(cfg)
        mock_server.login.assert_not_called()

    def test_sendmail_called(self):
        _, mock_server = self._call_smtp_send()
        mock_server.sendmail.assert_called_once()
