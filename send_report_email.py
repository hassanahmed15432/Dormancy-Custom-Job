#!/usr/bin/env python3
"""
Step 7 — Email Deactivation Report
=====================================
Sends an HTML email summarising the deactivation results to the security
team via Office 365 SMTP.

The email is **only sent** when both conditions are met:
  • ``SEND_EMAIL=true``
  • ``DRY_RUN=false``

Otherwise the step is silently skipped.

Usage
-----
  Normally called automatically by ``run_all.py`` as the final pipeline
  step.  Can also be run standalone after Step 5 has produced
  ``output/deactivation_results.json``.

Runtime Parameters
------------------
  ``SMTP_HOST``       — SMTP server  (default: smtp.office365.com)
  ``SMTP_PORT``       — SMTP port    (default: 587)
  ``SMTP_USER``       — login / From address (optional for open relays)
  ``SMTP_PASSWORD``   — DataRobot credential ID (resolved via API at runtime;
                        optional for open relays)
  ``SMTP_USE_CRED``   — "true"/"false" to force authentication on/off. Leave
                        empty for auto (auth is used only when SMTP_USER and
                        SMTP_PASSWORD are both provided). STARTTLS follows auth.
  ``EMAIL_FROM``      — From address (falls back to SMTP_USER)
  ``EMAIL_TO``        — comma-separated recipient addresses
  ``SEND_EMAIL``      — "true" to enable  (default: "false")

Authentication is optional: when ``SMTP_USE_CRED`` is false (or auto with no
credentials) the message is relayed without STARTTLS or login, which is how
internal port-25 relays typically accept mail from trusted networks.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any


try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = lambda: None  # noqa: E731

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

load_dotenv()

SEND_EMAIL: bool = os.getenv("SEND_EMAIL", "false").lower() in ("true", "1", "yes")
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.office365.com")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER: str = os.getenv("SMTP_USER", "")
SMTP_PASSWORD_RAW: str = os.getenv("SMTP_PASSWORD", "")
# Empty = auto (authenticate only when SMTP_USER and SMTP_PASSWORD are set).
SMTP_USE_CRED: str = os.getenv("SMTP_USE_CRED", "").strip().lower()
EMAIL_FROM: str = os.getenv("EMAIL_FROM", "") or SMTP_USER
EMAIL_TO: list[str] = [
    e.strip() for e in os.getenv("EMAIL_TO", "").split(",") if e.strip()
]

DATAROBOT_API_URL: str = os.getenv(
    "DATAROBOT_API_URL", os.getenv("DATAROBOT_ENDPOINT", "")
).rstrip("/")
DORMANCY_THRESHOLD_DAYS: int = int(os.getenv("DORMANCY_THRESHOLD_DAYS", "40"))

OUTPUT_DIR: Path = Path(__file__).resolve().parent / "output"
RESULTS_FILE: Path = OUTPUT_DIR / "deactivation_results.json"

NO_ACTIVITY_SENTINEL = 9999

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("send_report_email")

# ──────────────────────────────────────────────
# DataRobot credential resolver
# ──────────────────────────────────────────────

def _resolve_credential(raw_value: str) -> str:
    """Extract the password from a DataRobot credential runtime parameter.

    DataRobot injects ``type: credential`` values as a JSON string like
    ``{"credential_type": "basic", "username": "...", "password": "..."}``.
    This function parses that JSON and returns the ``password`` field.

    If *raw_value* is plain text (e.g. from a local ``.env`` file),
    it is returned as-is.
    """
    if not raw_value:
        return ""

    if raw_value.startswith("{"):
        try:
            data = json.loads(raw_value)
            password = data.get("password", "")
            if password:
                logger.info("SMTP credential extracted from DataRobot credential JSON.")
                return password
            logger.warning(
                "Credential JSON parsed but 'password' field is empty. "
                "Available keys: %s",
                ", ".join(data.keys()),
            )
            return ""
        except json.JSONDecodeError:
            pass

    logger.info("Using SMTP_PASSWORD as plain text.")
    return raw_value


# ──────────────────────────────────────────────
# HTML builder
# ──────────────────────────────────────────────

_CSS = """\
body { font-family: Segoe UI, Arial, sans-serif; color: #333; margin: 0; padding: 20px; }
.container { max-width: 800px; margin: 0 auto; }
h1 { color: #2F5496; font-size: 22px; }
h2 { color: #2F5496; font-size: 16px; margin-top: 28px; border-bottom: 2px solid #2F5496; padding-bottom: 4px; }
.meta { background: #f4f6f9; padding: 12px 16px; border-radius: 6px; margin-bottom: 20px; font-size: 14px; }
.meta td { padding: 2px 12px 2px 0; }
.meta .label { font-weight: 600; }
table.data { border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 8px; }
table.data th { background: #2F5496; color: #fff; padding: 8px 10px; text-align: left; }
table.data td { padding: 6px 10px; border-bottom: 1px solid #e0e0e0; }
table.data tr:nth-child(even) { background: #f9f9f9; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 12px; font-weight: 600; }
.badge-green { background: #C6EFCE; color: #006100; }
.badge-red { background: #FFC7CE; color: #9C0006; }
.badge-orange { background: #F4B084; color: #7C3A00; }
.badge-yellow { background: #FFE699; color: #7C6600; }
.counts { font-size: 15px; margin-bottom: 16px; }
.counts td { padding: 3px 16px 3px 0; }
.counts .num { font-weight: 700; font-size: 18px; }
.footer { margin-top: 30px; font-size: 12px; color: #888; }
"""


def _dormancy_display(days: int | None) -> str:
    if days is None or days >= NO_ACTIVITY_SENTINEL:
        return "No activity"
    return f"{days} days"


def _build_user_rows(users: list[dict[str, Any]], include_resources: bool = False) -> str:
    rows = ""
    for u in users:
        email = u.get("email", "")
        seat = u.get("seat_type_display") or u.get("seat_type") or "Unknown"
        dormancy = _dormancy_display(u.get("days_dormant"))
        step = u.get("step", "")

        cols = f"<td>{email}</td><td>{seat}</td><td>{step}</td><td>{dormancy}</td>"

        if include_resources:
            dep = u.get("deployments", "—")
            jobs = u.get("active_jobs", "—")
            apps = u.get("custom_apps", "—")
            cols += f"<td>{dep}</td><td>{jobs}</td><td>{apps}</td>"

        rows += f"<tr>{cols}</tr>\n"
    return rows


def _build_html(results: list[dict[str, Any]]) -> str:
    now = datetime.now(timezone.utc)

    by_status: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_status.setdefault(r["deactivation_status"], []).append(r)

    deactivated = by_status.get("DEACTIVATED", [])

    html = f"""\
<html>
<head><style>{_CSS}</style></head>
<body>
<div class="container">
<h1>Inactive Users — Deactivation Report</h1>

<table class="meta"><tr>
<td class="label">Date</td><td>{now.strftime('%Y-%m-%d %H:%M UTC')}</td>
</tr><tr>
<td class="label">Dormancy days</td><td>{DORMANCY_THRESHOLD_DAYS} days</td>
</tr></table>

<table class="counts"><tr>
<td class="num badge-green">{len(deactivated)}</td><td>Deactivated</td>
<td class="num">{len(results)}</td><td>Total processed</td>
</tr></table>
"""

    if deactivated:
        deactivated_sorted = sorted(deactivated, key=lambda u: u.get("email", ""))
        html += """\
<h2>Deactivated Accounts</h2>
<table class="data">
<tr><th>Email</th></tr>
"""
        for u in deactivated_sorted:
            html += f"<tr><td>{u.get('email', '')}</td></tr>\n"
        html += "</table>\n"
    else:
        html += '<h2>Deactivated Accounts</h2>\n<p>No accounts were deactivated.</p>\n'

    html += f"""\
<div class="footer">
Generated by Inactive Users Cleanup pipeline &middot; {now.strftime('%Y-%m-%d %H:%M:%S UTC')}
</div>
</div>
</body>
</html>"""

    return html


# ──────────────────────────────────────────────
# SMTP sender
# ──────────────────────────────────────────────

def _send_email(subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(html_body, "html"))

    smtp_password = _resolve_credential(SMTP_PASSWORD_RAW)

    # Decide whether to authenticate: explicit override via SMTP_USE_CRED, else
    # auto (authenticate only when credentials are provided). STARTTLS follows
    # authentication.
    if SMTP_USE_CRED in ("true", "1", "yes"):
        use_auth = True
    elif SMTP_USE_CRED in ("false", "0", "no"):
        use_auth = False
    else:
        use_auth = bool(SMTP_USER and smtp_password)

    if use_auth and not (SMTP_USER and smtp_password):
        raise RuntimeError(
            "SMTP_USE_CRED is enabled but SMTP_USER/SMTP_PASSWORD are missing."
        )

    use_tls = use_auth

    logger.info(
        "Connecting to %s:%d … (auth=%s, tls=%s)",
        SMTP_HOST, SMTP_PORT, use_auth, use_tls,
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        if use_tls:
            server.starttls()
            server.ehlo()
        if use_auth:
            server.login(SMTP_USER, smtp_password)
        else:
            logger.info("Relaying without authentication (SMTP_USE_CRED disabled).")
        server.send_message(msg)

    logger.info("Email sent to %s", ", ".join(EMAIL_TO))


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 72)
    logger.info("  Step 7 — Email Deactivation Report")
    logger.info("=" * 72)

    if DRY_RUN:
        logger.info("Skipping email — DRY_RUN is enabled.")
        return

    if not SEND_EMAIL:
        logger.info("Skipping email — SEND_EMAIL is not enabled.")
        return

    errors: list[str] = []
    if not EMAIL_FROM:
        errors.append("EMAIL_FROM (or SMTP_USER) is not set.")
    if not EMAIL_TO:
        errors.append("EMAIL_TO is not set.")
    if not RESULTS_FILE.exists():
        errors.append(f"Deactivation results not found at {RESULTS_FILE}.")
    # Credentials are optional: SMTP_USER/SMTP_PASSWORD may both be empty for
    # open relays. But if one is provided, the other must be too.
    if bool(SMTP_USER) != bool(SMTP_PASSWORD_RAW):
        errors.append(
            "SMTP_USER and SMTP_PASSWORD must both be set, or both be empty "
            "(for an unauthenticated relay)."
        )
    if errors:
        for err in errors:
            logger.error(err)
        logger.error("Cannot send email — fix the errors above.")
        return

    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        results: list[dict[str, Any]] = json.load(f)

    if not results:
        logger.info("No deactivation results to report. Skipping email.")
        return

    deactivated_count = sum(
        1 for r in results if r.get("deactivation_status") == "DEACTIVATED"
    )

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = (
        f"DataRobot Inactive Users Report — {now_str} — "
        f"{deactivated_count} user(s) deactivated"
    )

    html = _build_html(results)
    _send_email(subject, html)

    logger.info("Done.")


if __name__ == "__main__":
    main()
