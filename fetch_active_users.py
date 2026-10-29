#!/usr/bin/env python3
"""
Step 1 — Fetch All Active Users from DataRobot
================================================
Paginates through the DataRobot /api/v2/users/ endpoint, collecting every
currently-active user's internal ID, email, seat type, and active status.

Usage
-----
  1. Copy `.env.example` to `.env` and fill in your credentials.
  2. Install dependencies:  pip install -r requirements.txt
  3. Run:  python fetch_active_users.py

Output
------
  • Console summary with counts per seat type.
  • `active_users.json`  — full JSON dump of the collected user list.
  • `active_users.csv`   — CSV for quick review / further processing.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
import urllib3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = lambda: None  # noqa: E731

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

load_dotenv()  # reads .env in working directory

DATAROBOT_API_URL: str = os.getenv(
    "DATAROBOT_API_URL", os.getenv("DATAROBOT_ENDPOINT", "")
).rstrip("/")
DATAROBOT_API_TOKEN: str = os.getenv("DATAROBOT_API_TOKEN", "")

PAGE_SIZE: int = int(os.getenv("PAGE_SIZE", "100"))
REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "30"))
MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF: float = float(os.getenv("RETRY_BACKOFF", "2.0"))
SSL_VERIFY: bool = os.getenv("SSL_VERIFY", "false").lower() in ("true", "1", "yes")

EXCLUDE_ACCOUNTS: frozenset[str] = frozenset(
    e.strip().lower()
    for e in os.getenv("EXCLUDE_ACCOUNTS", "").split(",")
    if e.strip()
)

INCLUDE_ONLY_ACCOUNTS: frozenset[str] = frozenset(
    e.strip().lower()
    for e in os.getenv("INCLUDE_ONLY_ACCOUNTS", "").split(",")
    if e.strip()
)

# Suppress InsecureRequestWarning when SSL verification is disabled
if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OUTPUT_DIR: Path = Path(__file__).resolve().parent / "output"

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fetch_active_users")


# ──────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────

@dataclass
class ActiveUser:
    """Lightweight representation of a DataRobot user."""

    user_id: str
    email: str
    seat_type: Optional[str] = None   # may be absent on some deployments
    is_active: bool = True

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "ActiveUser":
        """Build an ActiveUser from a single user dict returned by the API.

        The DataRobot API returns user objects with varying key names depending
        on deployment version.  We normalise the most common variations here.
        """
        user_id = payload.get("userId") or payload.get("id") or payload.get("uid", "")
        email = payload.get("email") or payload.get("username", "")
        seat_type = payload.get("seatType") or payload.get("seat_type")  # handle both cases
        is_active = payload.get("isActive",
                               payload.get("activated",
                               payload.get("is_active", True)))

        return cls(
            user_id=str(user_id),
            email=str(email),
            seat_type=seat_type,
            is_active=bool(is_active),
        )


# ──────────────────────────────────────────────
# API helpers
# ──────────────────────────────────────────────

def _validate_config() -> None:
    """Ensure required env vars are set before making any requests."""
    errors: list[str] = []
    if not DATAROBOT_API_URL:
        errors.append("DATAROBOT_API_URL is not set.")
    if not DATAROBOT_API_TOKEN:
        errors.append("DATAROBOT_API_TOKEN is not set.")
    if errors:
        for err in errors:
            logger.error(err)
        logger.error(
            "Set these via .env (local) or Runtime Parameters (Custom Job)."
        )
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
) -> dict[str, Any]:
    """GET *url* with exponential-backoff retry on transient failures."""
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

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.HTTPError as exc:
            # 4xx (except 429) → don't retry, fail immediately
            if resp.status_code < 500 and resp.status_code != 429:
                logger.error("HTTP %d: %s", resp.status_code, resp.text[:500])
                raise
            last_exc = exc

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc

        wait = backoff ** attempt
        logger.warning(
            "Request failed (attempt %d/%d). Retrying in %.1f s … %s",
            attempt, max_retries, wait, last_exc,
        )
        time.sleep(wait)

    raise RuntimeError(
        f"All {max_retries} attempts failed for {url}"
    ) from last_exc


# ──────────────────────────────────────────────
# Core logic — fetch & paginate
# ──────────────────────────────────────────────

def fetch_active_users() -> list[ActiveUser]:
    """Paginate through GET /api/v2/users/?isActive=true and collect all
    active users.

    Returns a list of ActiveUser dataclass instances.
    """
    _validate_config()
    session = _build_session()

    if INCLUDE_ONLY_ACCOUNTS:
        logger.info(
            "INCLUDE_ONLY_ACCOUNTS is set — only processing: %s",
            ", ".join(sorted(INCLUDE_ONLY_ACCOUNTS)),
        )

    users: list[ActiveUser] = []
    skipped_inactive = 0
    skipped_excluded = 0
    offset = 0
    page = 1

    base_url = f"{DATAROBOT_API_URL}/api/v2/users/"

    while True:
        # Note: some DR deployments don't support `isActive` as a query param,
        # so we fetch ALL users and filter client-side.
        url = f"{base_url}?limit={PAGE_SIZE}&offset={offset}"
        logger.info("Fetching page %d  (offset=%d, limit=%d) …", page, offset, PAGE_SIZE)

        data = _request_with_retry(session, url)

        # The response body uses either "data" or "results" depending on version
        user_list: list[dict[str, Any]] = data.get("data") or data.get("results") or []

        if not user_list:
            logger.info("No more users returned — pagination complete.")
            break

        for raw_user in user_list:
            user = ActiveUser.from_api(raw_user)

            # Filter: skip users already marked inactive
            if not user.is_active:
                skipped_inactive += 1
                logger.debug("Skipping inactive user %s", user.email)
                continue

            # Filter: skip explicitly excluded emails
            if user.email and user.email.lower() in EXCLUDE_ACCOUNTS:
                skipped_excluded += 1
                logger.info("Excluding user %s (in EXCLUDE_ACCOUNTS)", user.email)
                continue

            # Filter: if include-only list is set, skip everyone not on it
            if INCLUDE_ONLY_ACCOUNTS and user.email.lower() not in INCLUDE_ONLY_ACCOUNTS:
                continue

            users.append(user)

        logger.info(
            "  → collected %d active users from page %d (%d total so far, %d inactive skipped)",
            len(user_list), page, len(users), skipped_inactive,
        )

        # Determine next page
        next_url = data.get("next")
        if next_url:
            # "next" may be a full URL or a relative path
            if next_url.startswith("http"):
                url = next_url
                # Extract offset for logging, but use the full next URL
                offset += PAGE_SIZE
            else:
                offset += PAGE_SIZE
        else:
            # No "next" key — check if we received a full page; if not, we're done
            if len(user_list) < PAGE_SIZE:
                logger.info("Last page reached (received %d < %d).", len(user_list), PAGE_SIZE)
                break
            else:
                offset += PAGE_SIZE

        page += 1

    logger.info(
        "✅  Total active users collected: %d  (skipped %d inactive, %d excluded)",
        len(users), skipped_inactive, skipped_excluded,
    )
    return users


# ──────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────

def _print_summary(users: list[ActiveUser]) -> None:
    """Print a human-readable summary to the console."""
    seat_counts: dict[str, int] = {}
    for u in users:
        key = u.seat_type or "(no seat type)"
        seat_counts[key] = seat_counts.get(key, 0) + 1

    print("\n" + "═" * 52)
    print("  ACTIVE USERS SUMMARY")
    print("═" * 52)
    print(f"  Total active users:  {len(users)}")
    print("  ─────────────────────────────────────")
    print("  Seat-type breakdown:")
    for seat, count in sorted(seat_counts.items(), key=lambda x: -x[1]):
        print(f"    • {seat:<25} {count:>5}")
    print("═" * 52 + "\n")


def save_json(users: list[ActiveUser], path: Path) -> None:
    """Serialise the user list to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [asdict(u) for u in users]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    logger.info("JSON saved → %s  (%d records)", path, len(records))


def save_csv(users: list[ActiveUser], path: Path) -> None:
    """Serialise the user list to a CSV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["user_id", "email", "seat_type", "is_active"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for u in users:
            writer.writerow(asdict(u))
    logger.info("CSV  saved → %s  (%d records)", path, len(records := users))


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 52)
    logger.info("  Step 1 — Fetch All Active DataRobot Users")
    logger.info("=" * 52)

    users = fetch_active_users()

    if not users:
        logger.warning("No active users found.")

    if users:
        _print_summary(users)

    save_json(users, OUTPUT_DIR / "active_users.json")
    save_csv(users, OUTPUT_DIR / "active_users.csv")

    logger.info("Done.  Files written to %s  (%d users)", OUTPUT_DIR, len(users))


if __name__ == "__main__":
    main()
