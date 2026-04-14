"""
Email Notification Service
Sends a WatchDog validation report email at the end of each scraper run.

Configuration lives in email_config.json (gitignored):
  {
    "enabled": true,
    "send_on": "errors",          // "always" | "errors" | "never"
    "smtp": {
      "host": "smtp.gmail.com",
      "port": 587,
      "use_tls": true,
      "username": "you@gmail.com",
      "password": "app-password"
    },
    "from": "WatchDog <you@gmail.com>",
    "to": ["one@example.com", "two@example.com"]
  }

For Gmail, generate an App Password at:
  https://myaccount.google.com/apppasswords
  (Requires 2-Step Verification to be enabled.)
"""

import json
import logging
import os
import smtplib
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from constants import SEVERITY_ICONS, SEVERITY_ORDER

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "email_config.json")


class EmailService:
    """
    Reads email_config.json and sends the WatchDog validation report.

    Gracefully no-ops when:
    - Config file is missing
    - `enabled` is false
    - `send_on` is "never"
    - `send_on` is "errors" and there are no issues
    """

    def __init__(self, config_path: str = CONFIG_FILE):
        self.config = self._load_config(config_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_report(
        self,
        report_path: str,
        validation_summary: dict,
        run_id: Optional[int] = None,
        start_time: Optional[datetime] = None,
        profile: Optional[str] = None,
    ) -> bool:
        """
        Send the report email if the configuration requires it.

        Returns True if an email was sent, False otherwise (no-op or error).
        """
        if not self._should_send(validation_summary):
            return False

        try:
            msg = self._build_message(report_path, validation_summary, run_id, start_time, profile)
            self._smtp_send(msg)
            recipients = self.config.get("to", [])
            logging.info(f"📧 Report email sent to: {', '.join(recipients)}")
            return True
        except Exception as e:
            logging.error(f"📧 Email delivery failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_config(self, path: str) -> dict:
        # 1. Try JSON file first (fallback)
        cfg: dict = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception as e:
                logging.warning(f"Could not load email config: {e}")

        # 2. Environment variables take precedence over JSON config.
        #    Priority order: WATCHDOG_* env vars > old EMAIL_* env vars > JSON file
        #
        #    New WATCHDOG_* env vars (preferred):
        #      WATCHDOG_SMTP_HOST      → smtp.host     (fallback: smtp.gmail.com)
        #      WATCHDOG_SMTP_PORT      → smtp.port     (fallback: 587)
        #      WATCHDOG_SMTP_USER      → smtp.username
        #      WATCHDOG_SMTP_PASSWORD  → smtp.password
        #      WATCHDOG_EMAIL_FROM     → from
        #      WATCHDOG_EMAIL_TO       → to (comma-separated)
        #      WATCHDOG_SEND_ON        → send_on       (fallback: errors)
        #
        #    Legacy EMAIL_* env vars (still supported for backward compatibility):
        #      EMAIL_USERNAME, EMAIL_PASSWORD, EMAIL_TO, EMAIL_HOST, EMAIL_PORT, EMAIL_SEND_ON

        # Try WATCHDOG_* vars first
        env_host = os.environ.get("WATCHDOG_SMTP_HOST", "")
        env_port = os.environ.get("WATCHDOG_SMTP_PORT", "")
        env_user = os.environ.get("WATCHDOG_SMTP_USER", "")
        env_password = os.environ.get("WATCHDOG_SMTP_PASSWORD", "")
        env_from = os.environ.get("WATCHDOG_EMAIL_FROM", "")
        env_to = os.environ.get("WATCHDOG_EMAIL_TO", "")
        env_send_on = os.environ.get("WATCHDOG_SEND_ON", "")

        # Fallback to legacy EMAIL_* vars if WATCHDOG_* not set
        if not env_user:
            env_user = os.environ.get("EMAIL_USERNAME", "")
        if not env_password:
            env_password = os.environ.get("EMAIL_PASSWORD", "")
        if not env_to:
            env_to = os.environ.get("EMAIL_TO", "")
        if not env_host:
            env_host = os.environ.get("EMAIL_HOST", "")
        if not env_port:
            env_port = os.environ.get("EMAIL_PORT", "")
        if not env_send_on:
            env_send_on = os.environ.get("EMAIL_SEND_ON", "")

        # If any env var is set, merge on top of JSON config
        if env_host or env_port or env_user or env_password or env_from or env_to or env_send_on:
            smtp = cfg.setdefault("smtp", {})

            # SMTP host (highest priority: WATCHDOG_SMTP_HOST > EMAIL_HOST > JSON > default)
            if env_host:
                smtp["host"] = env_host
            smtp.setdefault("host", "smtp.gmail.com")

            # SMTP port (parse as int if provided)
            if env_port:
                try:
                    smtp["port"] = int(env_port)
                except (ValueError, TypeError):
                    pass
            smtp.setdefault("port", 587)

            # SMTP username
            if env_user:
                smtp["username"] = env_user

            # SMTP password
            if env_password:
                smtp["password"] = env_password

            # TLS always enabled (safe default)
            smtp.setdefault("use_tls", True)

            # From address
            if env_from:
                cfg["from"] = env_from
            cfg.setdefault("from", f"WatchDog <{env_user}>" if env_user else "WatchDog")

            # Recipients (comma-separated string → list)
            if env_to:
                cfg["to"] = [a.strip() for a in env_to.split(",") if a.strip()]

            # Send trigger policy
            if env_send_on:
                cfg["send_on"] = env_send_on
            cfg.setdefault("send_on", "errors")

            # Auto-enable if creds are supplied via env
            enabled_env = os.environ.get("WATCHDOG_ENABLED", "").lower() or os.environ.get("EMAIL_ENABLED", "").lower()
            if enabled_env:
                cfg["enabled"] = (enabled_env != "false")
            else:
                cfg.setdefault("enabled", bool(env_user or env_password or env_to))

        if not cfg:
            logging.debug("No email config found — email notifications disabled.")
        return cfg

    def _should_send(self, summary: dict) -> bool:
        if not self.config:
            return False
        if not self.config.get("enabled", False):
            return False
        send_on = self.config.get("send_on", "errors")
        if send_on == "never":
            return False
        if send_on == "errors" and summary.get("total_issues", 0) == 0:
            logging.info("📧 No validation issues — skipping email.")
            return False
        return True

    def _build_message(
        self,
        report_path: str,
        summary: dict,
        run_id: Optional[int],
        start_time: Optional[datetime],
        profile: Optional[str] = None,
    ) -> MIMEMultipart:
        total = summary.get("total_issues", 0)
        by_type = summary.get("by_type", {})
        by_severity = summary.get("by_severity", {})
        recipients = self.config.get("to", [])
        sender = self.config.get("from", "WatchDog")

        timestamp = (start_time or datetime.now()).strftime("%Y-%m-%d")

        # Build subject with profile if authenticated
        profile_suffix = f" [Auth: {profile}]" if profile else ""
        subject = f"WatchDog Report — {timestamp}{profile_suffix}"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)

        html = self._html_body(total, by_type, by_severity, run_id, start_time)
        msg.attach(MIMEText(html, "html"))

        # Attach the markdown report
        if report_path and os.path.exists(report_path):
            with open(report_path, "rb") as f:
                part = MIMEBase("text", "markdown")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=os.path.basename(report_path),
                )
                msg.attach(part)

        return msg

    def _html_body(
        self,
        total: int,
        by_type: dict,
        by_severity: dict,
        run_id: Optional[int],
        start_time: Optional[datetime],
    ) -> str:
        severity_icons = SEVERITY_ICONS
        timestamp = (start_time or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        banner_color = "#d9534f" if total > 0 else "#5cb85c"
        banner_text = f"⚠️ {total} issue{'s' if total != 1 else ''} found" if total > 0 else "✅ All checks passed"

        by_type_rows = "".join(
            f"<tr><td>{t}</td><td style='text-align:right'><b>{c}</b></td></tr>"
            for t, c in sorted(by_type.items())
        ) or "<tr><td colspan='2'>None</td></tr>"

        by_sev_rows = "".join(
            f"<tr><td>{severity_icons.get(s, '')} {s}</td><td style='text-align:right'><b>{by_severity.get(s, 0)}</b></td></tr>"
            for s in SEVERITY_ORDER
            if by_severity.get(s, 0) > 0
        ) or "<tr><td colspan='2'>None</td></tr>"

        return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; font-size: 14px; color: #333; margin: 0; padding: 0; }}
    .container {{ max-width: 640px; margin: 24px auto; border: 1px solid #ddd; border-radius: 6px; overflow: hidden; }}
    .banner {{ background: {banner_color}; color: #fff; padding: 18px 24px; font-size: 18px; font-weight: bold; }}
    .body {{ padding: 24px; }}
    .meta {{ color: #666; font-size: 13px; margin-bottom: 20px; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
    th {{ background: #f5f5f5; text-align: left; padding: 8px 10px; font-size: 13px; border-bottom: 2px solid #ddd; }}
    td {{ padding: 7px 10px; border-bottom: 1px solid #eee; font-size: 13px; }}
    .footer {{ background: #f9f9f9; padding: 12px 24px; color: #999; font-size: 12px; border-top: 1px solid #eee; }}
  </style>
</head>
<body>
<div class="container">
  <div class="banner">{banner_text}</div>
  <div class="body">
    <p class="meta">
      Run #{run_id or "—"} &nbsp;|&nbsp; {timestamp}
    </p>

    <table>
      <tr><th colspan="2">Issues by Type</th></tr>
      {by_type_rows}
    </table>

    <table>
      <tr><th colspan="2">Issues by Severity</th></tr>
      {by_sev_rows}
    </table>

    <p style="color:#666; font-size:13px;">
      The full Markdown report is attached to this email.
    </p>
  </div>
  <div class="footer">
    WatchDog &mdash; automated scrape &amp; validation report
  </div>
</div>
</body>
</html>"""

    def _smtp_send(self, msg: MIMEMultipart):
        smtp_cfg = self.config.get("smtp", {})
        host = smtp_cfg.get("host", "smtp.gmail.com")
        port = smtp_cfg.get("port", 587)
        use_tls = smtp_cfg.get("use_tls", True)
        username = smtp_cfg.get("username", "")
        password = smtp_cfg.get("password", "")
        recipients = self.config.get("to", [])

        with smtplib.SMTP(host, port, timeout=30) as server:
            if use_tls:
                server.ehlo()
                server.starttls()
                server.ehlo()
            if username:
                server.login(username, password)
            server.sendmail(msg["From"], recipients, msg.as_string())
