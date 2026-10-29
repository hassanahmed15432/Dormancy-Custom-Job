#!/usr/bin/env python3
"""
End-to-End Pipeline Runner
============================
Executes every step of the inactive-users workflow in sequence:

  Step 1  — Fetch all active users
  Step 2  — Determine last activity date per user
  Step 3  — Identify dormant accounts
  Step 4  — Split by seat type and route
  Step R  — List resources owned by dormant users
  Step 5  — Deactivate dormant users (DRY_RUN=true by default)
  Step 6  — Generate the Excel report

Usage
-----
  python run_all.py                      # full pipeline, dry-run (safe)
  DRY_RUN=false python run_all.py           # full pipeline, live deactivation
  python run_all.py --from 3             # resume from Step 3 onward
  python run_all.py --only 6             # re-run only Step 6
  python run_all.py --force-deactivate   # deactivate all users (incl. builders with resources)

All environment variables from .env are respected as usual.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_all")

STEPS: list[tuple[str, str, str]] = [
    ("1", "fetch_active_users",    "Fetch all active users"),
    ("2", "check_last_activity",   "Determine last activity per user"),
    ("3", "identify_dormant_users","Identify dormant accounts"),
    ("4", "split_by_seat_type",    "Split by seat type and route"),
    ("R", "list_user_resources",   "List resources owned by dormant users"),
    ("5", "deactivate_users",      "Deactivate dormant users"),
    ("6", "generate_report",       "Generate Excel report"),
    ("7", "send_report_email",    "Email deactivation report"),
]


def _run_step(label: str, module_name: str, description: str) -> None:
    logger.info("")
    logger.info("=" * 72)
    logger.info("  [Step %s]  %s", label, description)
    logger.info("=" * 72)

    start = time.monotonic()
    mod = importlib.import_module(module_name)
    mod.main()
    elapsed = time.monotonic() - start

    logger.info("  [Step %s]  Completed in %.1f s", label, elapsed)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the inactive-users pipeline end-to-end.",
    )
    parser.add_argument(
        "--from", dest="from_step", default=None,
        help="Resume from this step onward (e.g. --from 3)",
    )
    parser.add_argument(
        "--only", default=None,
        help="Run only this single step (e.g. --only 6)",
    )
    parser.add_argument(
        "--force-deactivate", action="store_true", default=False,
        help="Deactivate all dormant users including builders with resources",
    )
    args = parser.parse_args()

    step_labels = [s[0] for s in STEPS]

    if args.only:
        targets = [s for s in STEPS if s[0] == args.only]
        if not targets:
            logger.error("Unknown step: %s  (valid: %s)", args.only, ", ".join(step_labels))
            sys.exit(1)
        steps_to_run = targets
    elif args.from_step:
        try:
            idx = step_labels.index(args.from_step)
        except ValueError:
            logger.error("Unknown step: %s  (valid: %s)", args.from_step, ", ".join(step_labels))
            sys.exit(1)
        steps_to_run = STEPS[idx:]
    else:
        steps_to_run = STEPS

    if args.force_deactivate:
        os.environ["BUILDER_MODE"] = "force_deactivate"
        logger.warning(
            "*** --force-deactivate enabled: ALL dormant users will be "
            "deactivated regardless of owned resources ***"
        )

    logger.info("")
    logger.info("*" * 72)
    logger.info("  INACTIVE USERS PIPELINE")
    logger.info("  Steps to run: %s", " -> ".join(s[0] for s in steps_to_run))
    logger.info("*" * 72)

    pipeline_start = time.monotonic()

    for label, module_name, description in steps_to_run:
        try:
            _run_step(label, module_name, description)
        except SystemExit as exc:
            if exc.code and exc.code != 0:
                logger.error("  [Step %s]  Exited with code %s — aborting pipeline.", label, exc.code)
                sys.exit(exc.code)
        except Exception:
            logger.exception("  [Step %s]  Failed — aborting pipeline.", label)
            sys.exit(1)

    total = time.monotonic() - pipeline_start
    logger.info("")
    logger.info("*" * 72)
    logger.info("  PIPELINE COMPLETE  (%.1f s total)", total)
    logger.info("*" * 72)


if __name__ == "__main__":
    main()
