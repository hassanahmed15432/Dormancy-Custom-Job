#!/usr/bin/env python3
"""
DataRobot Custom Job Entrypoint
=================================
Wraps the existing inactive-users pipeline so it runs inside a DataRobot
Custom Job execution environment.

DataRobot automatically injects ``DATAROBOT_ENDPOINT`` and
``DATAROBOT_API_TOKEN`` as environment variables.  This entrypoint
normalises those into the names the pipeline scripts expect, then
delegates to ``run_all.main()``.

All tuning knobs (DRY_RUN, BUILDER_MODE, etc.) are exposed as
Runtime Parameters in ``metadata.yaml`` and arrive as environment
variables — no ``.env`` file needed.
"""

from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("custom_job")


def _normalise_env() -> None:
    """Map DataRobot-injected env vars to the names the pipeline expects."""
    if "DATAROBOT_ENDPOINT" in os.environ and "DATAROBOT_API_URL" not in os.environ:
        url = os.environ["DATAROBOT_ENDPOINT"].rstrip("/")
        if url.endswith("/api/v2"):
            url = url[: -len("/api/v2")]
        os.environ["DATAROBOT_API_URL"] = url
        logger.info(
            "Mapped DATAROBOT_ENDPOINT -> DATAROBOT_API_URL (%s)",
            os.environ["DATAROBOT_API_URL"],
        )


def main() -> None:
    _normalise_env()

    logger.info("=" * 72)
    logger.info("  DataRobot Custom Job — Inactive Users Cleanup")
    logger.info("=" * 72)
    logger.info("  DRY_RUN         = %s", os.getenv("DRY_RUN", "true"))
    logger.info("  BUILDER_MODE    = %s", os.getenv("BUILDER_MODE", "resource_inventory"))
    logger.info("  DORMANCY_DAYS   = %s", os.getenv("DORMANCY_THRESHOLD_DAYS", "40"))
    logger.info("=" * 72)

    sys.argv = ["run_all.py"]
    from run_all import main as run_pipeline
    run_pipeline()


if __name__ == "__main__":
    main()
