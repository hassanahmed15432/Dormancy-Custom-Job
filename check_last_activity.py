#!/usr/bin/env python3
"""
Step 2 — Determine Last Activity Date per User
================================================
For each active user (from Step 1 output), queries two DataRobot API
endpoints to find the most recent *real* activity timestamp:

  1. App Usage           — /api/v2/eventLogs/ (excluding admin events like
     Activate/Deactivate/Update account)
  2. Prediction Usage    — /api/v2/eventLogs/predictionUsage/

The later of the two timestamps is kept as the user's last activity date.
The specific event type that triggered the timestamp is also recorded.

Usage
-----
  1. Ensure Step 1 has been run (active_users.json exists in output/).
  2. Run:  python check_last_activity.py

Output
------
  • Console summary with activity breakdown.
  • `output/user_activity.json`  — enriched user list with last_activity fields.
  • `output/user_activity.csv`   — CSV for quick review / further processing.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
import urllib3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = lambda: None  # noqa: E731

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

load_dotenv()

DATAROBOT_API_URL: str = os.getenv(
    "DATAROBOT_API_URL", os.getenv("DATAROBOT_ENDPOINT", "")
).rstrip("/")
DATAROBOT_API_TOKEN: str = os.getenv("DATAROBOT_API_TOKEN", "")

REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "30"))
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF: float = float(os.getenv("RETRY_BACKOFF", "2.0"))
SSL_VERIFY: bool = os.getenv("SSL_VERIFY", "false").lower() in ("true", "1", "yes")

DORMANCY_THRESHOLD_DAYS: int = int(os.getenv("DORMANCY_THRESHOLD_DAYS", "40"))

# Throttle delay between per-user API calls (seconds) to avoid rate-limiting
REQUEST_DELAY: float = float(os.getenv("REQUEST_DELAY", "0.2"))

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OUTPUT_DIR: Path = Path(__file__).resolve().parent / "output"
INPUT_FILE: Path = OUTPUT_DIR / "active_users.json"

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("check_last_activity")


# ──────────────────────────────────────────────
# API helpers (shared with Step 1)
# ──────────────────────────────────────────────

def _validate_config() -> None:
    """Ensure required env vars are set."""
    errors: list[str] = []
    if not DATAROBOT_API_URL:
        errors.append("DATAROBOT_API_URL is not set.")
    if not DATAROBOT_API_TOKEN:
        errors.append("DATAROBOT_API_TOKEN is not set.")
    if not INPUT_FILE.exists():
        errors.append(
            f"Step 1 output not found at {INPUT_FILE}. "
            "Run fetch_active_users.py first."
        )
    if errors:
        for err in errors:
            logger.error(err)
        sys.exit(1)


def _build_session() -> requests.Session:
    """Return a requests Session pre-configured with auth headers and SSL."""
    session = requests.Session()
    session.verify = SSL_VERIFY
    session.headers.update({
        "Authorization": f"Bearer {DATAROBOT_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    if not SSL_VERIFY:
        logger.info("SSL verification disabled (SSL_VERIFY=false)")
    return session


def _request_with_retry(
    session: requests.Session,
    url: str,
    *,
    max_retries: int = MAX_RETRIES,
    backoff: float = RETRY_BACKOFF,
    timeout: int = REQUEST_TIMEOUT,
) -> dict[str, Any] | None:
    """GET *url* with retry. Returns parsed JSON or None on 404/403."""
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=timeout)

            # Rate-limit handling (429)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff * attempt))
                logger.warning(
                    "Rate-limited (429). Retrying in %d s (attempt %d/%d)",
                    retry_after, attempt, max_retries,
                )
                time.sleep(retry_after)
                continue

            # 404 = endpoint may not exist on this deployment
            # 403 = no permission for this user's logs
            if resp.status_code in (403, 404):
                return None

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.HTTPError as exc:
            if resp.status_code < 500 and resp.status_code != 429:
                logger.warning("HTTP %d for %s: %s", resp.status_code, url, resp.text[:300])
                return None
            last_exc = exc

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc

        wait = backoff ** attempt
        logger.warning(
            "Request failed (attempt %d/%d). Retrying in %.1f s … %s",
            attempt, max_retries, wait, last_exc,
        )
        time.sleep(wait)

    logger.error("All %d attempts failed for %s", max_retries, url)
    return None


# ──────────────────────────────────────────────
# Timestamp helpers
# ──────────────────────────────────────────────

def _parse_timestamp(ts: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string into a timezone-aware datetime."""
    if not ts:
        return None
    try:
        # Handle various ISO formats: with/without Z, with/without microseconds
        ts_clean = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_clean)
    except (ValueError, TypeError):
        logger.debug("Could not parse timestamp: %s", ts)
        return None


def _later_of(a: datetime | None, b: datetime | None) -> datetime | None:
    """Return the later of two optional datetimes."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _format_iso(dt: datetime | None) -> str | None:
    """Format a datetime as ISO 8601 string, or None."""
    if dt is None:
        return None
    return dt.isoformat()


# ──────────────────────────────────────────────
# Core logic — query event logs
# ──────────────────────────────────────────────

EXCLUDED_EVENT_TYPES: frozenset[str] = frozenset(
    e.strip().lower()
    for e in os.getenv(
        "EXCLUDED_EVENT_TYPES",
        "Activate account,Deactivate Account,Update account",
    ).split(",")
    if e.strip()
)


def _get_last_app_usage(
    session: requests.Session,
    user_id: str,
    cutoff_iso: str,
    now_iso: str,
) -> tuple[datetime | None, str | None]:
    """Query GET /api/v2/eventLogs/ for the user's most recent *app usage*
    event, skipping admin-only events (activate/deactivate/update account).

    Returns ``(timestamp, event_type)`` or ``(None, None)``.
    """
    url = (
        f"{DATAROBOT_API_URL}/api/v2/eventLogs/"
        f"?userId={quote(user_id)}"
        f"&minTimestamp={quote(cutoff_iso)}"
        f"&maxTimestamp={quote(now_iso)}"
        f"&order=desc"
    )
    data = _request_with_retry(session, url)
    if not data:
        return None, None

    events = data.get("data") or data.get("results") or []

    for evt in events:
        event_name = evt.get("event") or ""
        if event_name.strip().lower() in EXCLUDED_EVENT_TYPES:
            continue

        raw_ts = (
            evt.get("timestamp")
            or evt.get("createdAt")
            or evt.get("eventTimestamp")
        )
        parsed = _parse_timestamp(raw_ts)
        if parsed:
            return parsed, event_name

    return None, None


def _get_last_prediction_usage(
    session: requests.Session,
    user_id: str,
    cutoff: datetime,
    now: datetime,
) -> tuple[datetime | None, str | None]:
    """Query GET /api/v2/eventLogs/predictionUsage/ for the user's most
    recent prediction API call.

    The predictionUsage endpoint enforces a max 24-hour window between
    minTimestamp and maxTimestamp.  We scan backwards in 24-hour chunks
    from *now* to *cutoff*, returning as soon as we find an event.

    Returns ``(timestamp, event_type)`` or ``(None, None)``.
    """
    window_end = now
    window_size = timedelta(hours=24)

    while window_end > cutoff:
        window_start = max(window_end - window_size, cutoff)
        min_ts = window_start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        max_ts = window_end.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        url = (
            f"{DATAROBOT_API_URL}/api/v2/eventLogs/predictionUsage/"
            f"?userId={quote(user_id)}"
            f"&minTimestamp={quote(min_ts)}"
            f"&maxTimestamp={quote(max_ts)}"
            f"&order=desc"
        )
        data = _request_with_retry(session, url)

        if data:
            events = data.get("data") or data.get("results") or []
            if events:
                raw_ts = (
                    events[0].get("timestamp")
                    or events[0].get("createdAt")
                    or events[0].get("eventTimestamp")
                )
                parsed = _parse_timestamp(raw_ts)
                if parsed:
                    event_name = events[0].get("event") or "Prediction Usage"
                    return parsed, event_name

        window_end = window_start

    return None, None


def check_activity_for_users(
    users: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """For each user, query both event sources and enrich with last_activity.

    Returns a new list of user dicts with additional fields:
      - last_event_log:        ISO timestamp or null
      - last_prediction_usage: ISO timestamp or null
      - last_activity:         max of the two, ISO timestamp or null
    """
    _validate_config()
    session = _build_session()

    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    cutoff = now - timedelta(days=DORMANCY_THRESHOLD_DAYS)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    logger.info(
        "Activity lookback window: %d days  (cutoff: %s)",
        DORMANCY_THRESHOLD_DAYS, cutoff_iso,
    )

    enriched: list[dict[str, Any]] = []
    total = len(users)

    # Track whether each endpoint is available
    event_log_available = True
    pred_usage_available = True

    for idx, user in enumerate(users, start=1):
        user_id = user["user_id"]
        email = user["email"]

        logger.info(
            "[%d/%d]  Checking activity for %s …",
            idx, total, email,
        )

        # Source 1: App usage (event log minus admin events)
        last_app: datetime | None = None
        app_event_type: str | None = None
        if event_log_available:
            last_app, app_event_type = _get_last_app_usage(session, user_id, cutoff_iso, now_iso)
            if last_app is None and idx == 1:
                logger.info("  appUsage: no events found (or endpoint unavailable)")

        # Source 2: Prediction usage
        last_pred: datetime | None = None
        pred_event_type: str | None = None
        if pred_usage_available:
            last_pred, pred_event_type = _get_last_prediction_usage(session, user_id, cutoff, now)
            if last_pred is None and idx == 1:
                logger.info("  predictionUsage: no events found (or endpoint unavailable)")

        # Take the later of the two
        last_activity = _later_of(last_app, last_pred)

        # Determine which source provided the last activity
        if last_activity is None:
            activity_type = None
        elif last_app and last_pred and last_app == last_pred:
            activity_type = f"App Usage ({app_event_type}) + Prediction ({pred_event_type})"
        elif last_activity == last_app:
            activity_type = f"App Usage ({app_event_type})"
        else:
            activity_type = f"Prediction Usage ({pred_event_type})"

        enriched_user = {
            **user,
            "last_app_usage": _format_iso(last_app),
            "last_app_event_type": app_event_type,
            "last_prediction_usage": _format_iso(last_pred),
            "last_pred_event_type": pred_event_type,
            "last_activity": _format_iso(last_activity),
            "last_activity_type": activity_type,
        }
        enriched.append(enriched_user)

        activity_str = (
            last_activity.strftime("%Y-%m-%d %H:%M UTC") if last_activity else "NO ACTIVITY"
        )
        logger.info(
            "  → last_activity: %s  (app=%s, pred=%s)",
            activity_str,
            "✓" if last_app else "—",
            "✓" if last_pred else "—",
        )

        # Throttle to avoid rate-limiting
        if idx < total:
            time.sleep(REQUEST_DELAY)

    # Summary counts
    with_activity = sum(1 for u in enriched if u["last_activity"])
    without_activity = total - with_activity

    logger.info(
        "✅  Activity check complete: %d users processed  "
        "(%d with activity, %d without)",
        total, with_activity, without_activity,
    )

    return enriched


# ──────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────

def _print_summary(users: list[dict[str, Any]], cutoff_days: int) -> None:
    """Print a human-readable activity summary."""
    with_activity = [u for u in users if u["last_activity"]]
    without_activity = [u for u in users if not u["last_activity"]]

    print("\n" + "═" * 60)
    print("  USER ACTIVITY SUMMARY")
    print("═" * 60)
    print(f"  Lookback window:     {cutoff_days} days")
    print(f"  Total users checked: {len(users)}")
    print(f"  With activity:       {len(with_activity)}")
    print(f"  No activity:         {len(without_activity)}")
    print("  ─" * 20)

    if with_activity:
        print("\n  📊 Users WITH recent activity:")
        for u in sorted(with_activity, key=lambda x: x["last_activity"], reverse=True):
            dt = u["last_activity"][:16].replace("T", " ")
            src = []
            if u["last_app_usage"]:
                src.append("app")
            if u["last_prediction_usage"]:
                src.append("pred")
            print(f"    ✓ {u['email']:<45} {dt}  ({', '.join(src)})")

    if without_activity:
        print(f"\n  ⚠️  Users with NO activity in last {cutoff_days} days:")
        for u in without_activity:
            print(f"    ✗ {u['email']}")

    print("═" * 60 + "\n")


def save_json(data: list[dict[str, Any]], path: Path) -> None:
    """Write data to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("JSON saved → %s  (%d records)", path, len(data))


def save_csv(data: list[dict[str, Any]], path: Path) -> None:
    """Write data to CSV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "user_id", "email", "seat_type", "is_active",
        "last_app_usage", "last_app_event_type",
        "last_prediction_usage", "last_pred_event_type",
        "last_activity", "last_activity_type",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    logger.info("CSV  saved → %s  (%d records)", path, len(data))


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 60)
    logger.info("  Step 2 — Determine Last Activity Date per User")
    logger.info("=" * 60)

    # Load Step 1 output
    logger.info("Loading active users from %s …", INPUT_FILE)
    if not INPUT_FILE.exists():
        logger.warning("Step 1 output not found — writing empty output files.")
        users = []
    else:
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            users = json.load(f)
    logger.info("Loaded %d active users.", len(users))

    # Enrich each user with last activity info
    enriched = check_activity_for_users(users) if users else []

    if not enriched:
        logger.warning("No users to process.")

    if enriched:
        _print_summary(enriched, DORMANCY_THRESHOLD_DAYS)

    save_json(enriched, OUTPUT_DIR / "user_activity.json")
    save_csv(enriched, OUTPUT_DIR / "user_activity.csv")

    logger.info("Done.  Files written to %s  (%d users)", OUTPUT_DIR, len(enriched))


if __name__ == "__main__":
    main()
