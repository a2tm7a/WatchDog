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
    ) -> bool:
        """
        Send the report email if the configuration requires it.

        Returns True if an email was sent, False otherwise (no-op or error).
        """
        if not self._should_send(validation_summary):
            return False

        try:
            msg = self._build_message(report_path, validation_summary, run_id, start_time)
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
        # 1. Try JSON file first
        cfg: dict = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception as e:
                logging.warning(f"Could not load email config: {e}")

        # 2. Override / supplement with environment variables.
        #    Environment variables can override/supplement the JSON config.
        #    Any env var present takes precedence over the JSON value.
        #
        #    Required env vars:
        #      EMAIL_USERNAME   — SMTP login (e.g. you@gmail.com)
        #      EMAIL_PASSWORD   — SMTP App Password
        #      EMAIL_TO         — comma-separated recipient list
        #    Optional:
        #      EMAIL_ENABLED    — "true" / "false"  (default: true if creds present)
        #      EMAIL_SEND_ON    — "always" / "errors" / "never"  (default: errors)
        #      EMAIL_FROM       — display name + address
        #      EMAIL_HOST       — SMTP host  (default: smtp.gmail.com)
        #      EMAIL_PORT       — SMTP port  (default: 587)

        env_username = os.environ.get("EMAIL_USERNAME", "")
        env_password = os.environ.get("EMAIL_PASSWORD", "")
        env_to       = os.environ.get("EMAIL_TO", "")

        if env_username or env_password or env_to:
            # Merge env vars on top of whatever the JSON said
            smtp = cfg.setdefault("smtp", {})
            if env_username:
                smtp["username"] = env_username
            if env_password:
                smtp["password"] = env_password
            smtp.setdefault("host", os.environ.get("EMAIL_HOST", "smtp.gmail.com"))
            smtp.setdefault("port", int(os.environ.get("EMAIL_PORT", "587")))
            smtp.setdefault("use_tls", True)

            if env_to:
                cfg["to"] = [a.strip() for a in env_to.split(",") if a.strip()]
            cfg.setdefault("from", os.environ.get(
                "EMAIL_FROM",
                f"WatchDog <{env_username}>" if env_username else "WatchDog"
            ))
            cfg.setdefault("send_on", os.environ.get("EMAIL_SEND_ON", "errors"))

            # Auto-enable if creds are supplied via env
            enabled_env = os.environ.get("EMAIL_ENABLED", "").lower()
            cfg["enabled"] = (enabled_env != "false")

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
    ) -> MIMEMultipart:
        total = summary.get("total_issues", 0)
        by_type = summary.get("by_type", {})
        by_severity = summary.get("by_severity", {})
        recipients = self.config.get("to", [])
        sender = self.config.get("from", "WatchDog")

        timestamp = (start_time or datetime.now()).strftime("%Y-%m-%d %H:%M")
        subject = (
            f"⚠️ [{timestamp}] WatchDog — {total} issue{'s' if total != 1 else ''} found"
            if total > 0
            else f"✅ [{timestamp}] WatchDog — All checks passed"
        )

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
