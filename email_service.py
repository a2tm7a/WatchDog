"""
Email Notification Service
Sends a validation report email at the end of a scraper run.

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
    "from": "ALLEN Verifier <you@gmail.com>",
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

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "email_config.json")


class EmailService:
    """
    Reads email_config.json and sends the scraper validation report.

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
        if not os.path.exists(path):
            logging.debug(f"Email config not found at {path} — email notifications disabled.")
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Could not load email config: {e}")
            return {}

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
        sender = self.config.get("from", "ALLEN Verifier")

        timestamp = (start_time or datetime.now()).strftime("%Y-%m-%d %H:%M")
        subject = (
            f"⚠️ [{timestamp}] ALLEN Verifier — {total} issue{'s' if total != 1 else ''} found"
            if total > 0
            else f"✅ [{timestamp}] ALLEN Verifier — All checks passed"
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
        severity_icons = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}
        timestamp = (start_time or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        banner_color = "#d9534f" if total > 0 else "#5cb85c"
        banner_text = f"⚠️ {total} issue{'s' if total != 1 else ''} found" if total > 0 else "✅ All checks passed"

        by_type_rows = "".join(
            f"<tr><td>{t}</td><td style='text-align:right'><b>{c}</b></td></tr>"
            for t, c in sorted(by_type.items())
        ) or "<tr><td colspan='2'>None</td></tr>"

        by_sev_rows = "".join(
            f"<tr><td>{severity_icons.get(s, '')} {s}</td><td style='text-align:right'><b>{by_severity.get(s, 0)}</b></td></tr>"
            for s in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
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
    ALLEN Verifier &mdash; automated scrape &amp; validation report
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
