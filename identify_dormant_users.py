#!/usr/bin/env python3
"""
Step 3 — Identify Dormant Accounts
====================================
Reads the enriched user-activity data from Step 2 and classifies each user
as dormant or active based on a configurable inactivity threshold (default
40 days).  A user is dormant if:

  • Their last_activity is more than DORMANCY_THRESHOLD_DAYS ago, **or**
  • They have no recorded activity at all (last_activity is null).

Usage
-----
  1. Ensure Step 2 has been run (user_activity.json exists in output/).
  2. Run:  python identify_dormant_users.py

Output
------
  • Console report with dormant-vs-active breakdown.
  • ``output/dormant_users.json``  — dormant users sorted by days_dormant desc.
  • ``output/dormant_users.csv``   — CSV for quick review / further processing.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
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

DORMANCY_THRESHOLD_DAYS: int = int(os.getenv("DORMANCY_THRESHOLD_DAYS", "40"))

OUTPUT_DIR: Path = Path(__file__).resolve().parent / "output"
INPUT_FILE: Path = OUTPUT_DIR / "user_activity.json"

NO_ACTIVITY_SENTINEL: int = 9999

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("identify_dormant_users")


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _parse_timestamp(ts: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string into a timezone-aware datetime."""
    if not ts:
        return None
    try:
        ts_clean = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_clean)
    except (ValueError, TypeError):
        logger.debug("Could not parse timestamp: %s", ts)
        return None


def _load_user_activity() -> list[dict[str, Any]]:
    """Load the Step 2 output file."""
    if not INPUT_FILE.exists():
        logger.warning("Step 2 output not found at %s — treating as empty.", INPUT_FILE)
        return []

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        users = json.load(f)

    logger.info("Loaded %d users from %s", len(users), INPUT_FILE)
    return users


# ──────────────────────────────────────────────
# Core logic — dormancy classification
# ──────────────────────────────────────────────

def classify_dormancy(
    users: list[dict[str, Any]],
    threshold_days: int = DORMANCY_THRESHOLD_DAYS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Classify each user as dormant or active.

    Returns (dormant_users, active_users), both sorted by days_dormant
    descending.  Each user dict is enriched with:
      - days_dormant (int)
      - is_dormant   (bool)
    """
    now = datetime.now(timezone.utc)
    dormant: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []

    for user in users:
        last_activity_dt = _parse_timestamp(user.get("last_activity"))

        if last_activity_dt is None:
            days_dormant = NO_ACTIVITY_SENTINEL
            is_dormant = True
        else:
            delta = now - last_activity_dt
            days_dormant = delta.days
            is_dormant = days_dormant >= threshold_days

        enriched = {
            **user,
            "days_dormant": days_dormant,
            "is_dormant": is_dormant,
        }

        if is_dormant:
            dormant.append(enriched)
        else:
            active.append(enriched)

    dormant.sort(key=lambda u: u["days_dormant"], reverse=True)
    active.sort(key=lambda u: u["days_dormant"], reverse=True)

    return dormant, active


# ──────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────

def _days_label(days: int) -> str:
    """Human-readable label for days_dormant."""
    if days >= NO_ACTIVITY_SENTINEL:
        return "no activity ever"
    return f"{days}d ago"


def _print_report(
    dormant: list[dict[str, Any]],
    active: list[dict[str, Any]],
    threshold: int,
) -> None:
    """Print a human-readable dormancy report to the console."""
    total = len(dormant) + len(active)

    print("\n" + "=" * 64)
    print("  DORMANT ACCOUNT REPORT")
    print("=" * 64)
    print(f"  Dormancy threshold:     {threshold} days")
    print(f"  Total users evaluated:  {total}")
    print(f"  Dormant:                {len(dormant)}")
    print(f"  Active:                 {len(active)}")
    print("-" * 64)

    if dormant:
        no_activity = [u for u in dormant if u["days_dormant"] >= NO_ACTIVITY_SENTINEL]
        stale = [u for u in dormant if u["days_dormant"] < NO_ACTIVITY_SENTINEL]

        if no_activity:
            print(f"\n  NEVER ACTIVE  ({len(no_activity)} users — no recorded activity):")
            for u in no_activity:
                print(f"    x {u['email']}")

        if stale:
            print(f"\n  STALE  ({len(stale)} users — last activity >= {threshold} days ago):")
            for u in stale:
                last = u["last_activity"][:10] if u.get("last_activity") else "n/a"
                print(f"    x {u['email']:<50} {last}  ({u['days_dormant']}d)")

    if active:
        print(f"\n  ACTIVE  ({len(active)} users — last activity < {threshold} days ago):")
        for u in active:
            last = u["last_activity"][:10] if u.get("last_activity") else "n/a"
            print(f"    . {u['email']:<50} {last}  ({u['days_dormant']}d)")

    print("=" * 64 + "\n")


def save_json(data: list[dict[str, Any]], path: Path) -> None:
    """Write data to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    logger.info("JSON saved -> %s  (%d records)", path, len(data))


def save_csv(data: list[dict[str, Any]], path: Path) -> None:
    """Write data to CSV file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "user_id", "email", "seat_type", "is_active",
        "last_app_usage", "last_app_event_type",
        "last_prediction_usage", "last_pred_event_type",
        "last_activity", "last_activity_type",
        "days_dormant", "is_dormant",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in data:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    logger.info("CSV  saved -> %s  (%d records)", path, len(data))


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 64)
    logger.info("  Step 3 - Identify Dormant Accounts")
    logger.info("=" * 64)

    users = _load_user_activity()

    if not users:
        logger.warning("No users to classify.")
        dormant, active = [], []
    else:
        dormant, active = classify_dormancy(users, DORMANCY_THRESHOLD_DAYS)

        logger.info(
            "Classification complete: %d dormant, %d active (threshold=%d days)",
            len(dormant), len(active), DORMANCY_THRESHOLD_DAYS,
        )

        _print_report(dormant, active, DORMANCY_THRESHOLD_DAYS)

    save_json(dormant, OUTPUT_DIR / "dormant_users.json")
    save_csv(dormant, OUTPUT_DIR / "dormant_users.csv")

    logger.info("Done.  Files written to %s  (%d dormant)", OUTPUT_DIR, len(dormant))


if __name__ == "__main__":
    main()
