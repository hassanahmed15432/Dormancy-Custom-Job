#!/usr/bin/env python3
"""
Step 4 — Split by Seat Type and Route
=======================================
Reads the dormant-user list from Step 3 and splits it into two routing
lanes based on ``seatType``:

  **Non-Builder** — seatType is explicitly one of the recognised
  Non-Builder values (case-insensitive).  These go to the
  auto-deactivation path (Step 5A).

  **Builder** — seatType is null, missing, or any other value.
  This is the safe default: uncertain classification lands here so a
  human reviews before anything is deactivated (Step 5B).

Usage
-----
  1. Ensure Step 3 has been run (dormant_users.json exists in output/).
  2. Run:  python split_by_seat_type.py

Output
------
  • Console routing report.
  • ``output/dormant_non_builders.json / .csv`` — auto-deactivation lane.
  • ``output/dormant_builders.json    / .csv`` — manual-review lane.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
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

NON_BUILDER_VALUES: frozenset[str] = frozenset(
    v.strip().lower()
    for v in os.getenv(
        "NON_BUILDER_SEAT_TYPES",
        "Non-Builder,viewer,consumer,read-only",
    ).split(",")
    if v.strip()
)

OUTPUT_DIR: Path = Path(__file__).resolve().parent / "output"
INPUT_FILE: Path = OUTPUT_DIR / "dormant_users.json"

UNKNOWN_SEAT_LABEL: str = "Unknown"

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("split_by_seat_type")


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _load_dormant_users() -> list[dict[str, Any]]:
    """Load the Step 3 output file."""
    if not INPUT_FILE.exists():
        logger.warning("Step 3 output not found at %s — treating as empty.", INPUT_FILE)
        return []

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        users = json.load(f)

    logger.info("Loaded %d dormant users from %s", len(users), INPUT_FILE)
    return users


def _is_non_builder(seat_type: str | None) -> bool:
    """Return True only if seat_type explicitly matches a Non-Builder value."""
    if seat_type is None:
        return False
    return seat_type.strip().lower() in NON_BUILDER_VALUES


# ──────────────────────────────────────────────
# Core logic — seat-type routing
# ──────────────────────────────────────────────

def route_by_seat_type(
    dormant_users: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split dormant users into (non_builders, builders).

    Each user dict is enriched with:
      - route             ("non_builder" | "builder")
      - seat_type_display (original value or "Unknown" when absent)
    """
    non_builders: list[dict[str, Any]] = []
    builders: list[dict[str, Any]] = []

    for user in dormant_users:
        seat = user.get("seat_type")
        display_seat = seat if seat else UNKNOWN_SEAT_LABEL

        enriched = {
            **user,
            "seat_type_display": display_seat,
        }

        if _is_non_builder(seat):
            enriched["route"] = "non_builder"
            non_builders.append(enriched)
        else:
            enriched["route"] = "builder"
            builders.append(enriched)

    non_builders.sort(key=lambda u: u["days_dormant"], reverse=True)
    builders.sort(key=lambda u: u["days_dormant"], reverse=True)

    return non_builders, builders


# ──────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────

def _print_report(
    non_builders: list[dict[str, Any]],
    builders: list[dict[str, Any]],
) -> None:
    """Print a human-readable routing report."""
    total = len(non_builders) + len(builders)

    print("\n" + "=" * 72)
    print("  SEAT-TYPE ROUTING REPORT")
    print("=" * 72)
    print(f"  Total dormant users:  {total}")
    print(f"  Non-Builders:         {len(non_builders):<6} -> auto-deactivation  (Step 5A)")
    print(f"  Builders:             {len(builders):<6} -> manual review       (Step 5B)")
    print("-" * 72)

    if non_builders:
        print(f"\n  NON-BUILDERS  ({len(non_builders)} users — auto-deactivation path):")
        for u in non_builders:
            dormancy = f"{u['days_dormant']}d" if u["days_dormant"] < 9999 else "never"
            print(
                f"    -> {u['email']:<50} "
                f"seat={u['seat_type_display']:<15} dormant={dormancy}"
            )

    if builders:
        print(f"\n  BUILDERS  ({len(builders)} users — manual review path):")
        for u in builders:
            dormancy = f"{u['days_dormant']}d" if u["days_dormant"] < 9999 else "never"
            print(
                f"    ** {u['email']:<50} "
                f"seat={u['seat_type_display']:<15} dormant={dormancy}"
            )

    print("=" * 72)

    if builders:
        unknown_count = sum(
            1 for u in builders if u["seat_type_display"] == UNKNOWN_SEAT_LABEL
        )
        if unknown_count:
            print(
                f"  NOTE: {unknown_count} builder(s) have no seat type on record. "
                "They default to the manual-review lane for safety."
            )
    print()


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
        "user_id", "email", "seat_type", "seat_type_display", "route",
        "is_active", "last_app_usage", "last_app_event_type",
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
    logger.info("=" * 72)
    logger.info("  Step 4 - Split by Seat Type and Route")
    logger.info("=" * 72)

    dormant_users = _load_dormant_users()

    if not dormant_users:
        logger.warning("No dormant users to route.")
        non_builders, builders = [], []
    else:
        non_builders, builders = route_by_seat_type(dormant_users)

        logger.info(
            "Routing complete: %d non-builders, %d builders",
            len(non_builders), len(builders),
        )

        _print_report(non_builders, builders)

    save_json(non_builders, OUTPUT_DIR / "dormant_non_builders.json")
    save_csv(non_builders, OUTPUT_DIR / "dormant_non_builders.csv")
    save_json(builders, OUTPUT_DIR / "dormant_builders.json")
    save_csv(builders, OUTPUT_DIR / "dormant_builders.csv")

    logger.info("Done.  Files written to %s", OUTPUT_DIR)


if __name__ == "__main__":
    main()
