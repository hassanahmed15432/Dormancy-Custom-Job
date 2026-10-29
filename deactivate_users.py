#!/usr/bin/env python3
"""
Step 5 — Deactivate Dormant Users
====================================
Processes the two lanes produced by Step 4:

  **5A — Non-Builder auto-deactivation**
    PATCH ``/api/v2/users/{id}/`` with ``{ "isActive": false }`` for every
    Non-Builder dormant user.

  **5B — Builder handling** (four modes, controlled by ``BUILDER_MODE``):
    • ``resource_inventory`` (default) — reads the pre-scanned resource
      inventory from ``list_user_resources.py``.  Builders with 0
      resources are auto-deactivated; those with resources are flagged
      for manual review.
    • ``manual_review`` — no API calls; flags all Builders for manual review.
    • ``resource_check`` — queries resources per-user via API (may not
      work on all deployments).
    • ``force_deactivate`` — deactivates all Builders regardless of
      owned resources.  Use with ``run_all.py --force-deactivate``.

Safety
------
  ``DRY_RUN=true`` (the default) logs what *would* happen without making
  any PATCH calls.  Set ``DRY_RUN=false`` only when you are ready to
  deactivate for real.

Usage
-----
  1. Ensure Step 4 has been run (dormant_non_builders.json and
     dormant_builders.json exist in output/).
  2. Run:  python deactivate_users.py            # dry-run by default
  3. When satisfied:  DRY_RUN=false python deactivate_users.py

Output
------
  • Console audit trail for every user processed.
  • ``output/deactivation_results.json / .csv`` — full results with
    per-user status.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
import urllib3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
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

DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
BUILDER_MODE: str = os.getenv("BUILDER_MODE", "resource_inventory")
REQUEST_DELAY: float = float(os.getenv("REQUEST_DELAY", "0.3"))
DORMANCY_THRESHOLD_DAYS: int = int(os.getenv("DORMANCY_THRESHOLD_DAYS", "40"))

VALID_BUILDER_MODES = ("manual_review", "resource_check", "resource_inventory", "force_deactivate")

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OUTPUT_DIR: Path = Path(__file__).resolve().parent / "output"
NON_BUILDERS_FILE: Path = OUTPUT_DIR / "dormant_non_builders.json"
BUILDERS_FILE: Path = OUTPUT_DIR / "dormant_builders.json"
RESOURCES_FILE: Path = OUTPUT_DIR / "dormant_user_resources.json"

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("deactivate_users")

# ──────────────────────────────────────────────
# Status constants
# ──────────────────────────────────────────────

STATUS_DEACTIVATED = "DEACTIVATED"
STATUS_DRY_RUN = "DRY_RUN"
STATUS_FAILED = "FAILED"
STATUS_PENDING_REVIEW = "PENDING_MANUAL_REVIEW"
STATUS_HAS_RESOURCES = "MANUAL_REVIEW_HAS_RESOURCES"


# ──────────────────────────────────────────────
# Validation & session
# ──────────────────────────────────────────────

def _validate_config() -> None:
    errors: list[str] = []
    if not DATAROBOT_API_URL:
        errors.append("DATAROBOT_API_URL is not set.")
    if not DATAROBOT_API_TOKEN:
        errors.append("DATAROBOT_API_TOKEN is not set.")
    if BUILDER_MODE not in VALID_BUILDER_MODES:
        errors.append(
            f"BUILDER_MODE must be one of {VALID_BUILDER_MODES}, "
            f"got '{BUILDER_MODE}'."
        )
    if BUILDER_MODE == "resource_inventory" and not RESOURCES_FILE.exists():
        errors.append(
            f"BUILDER_MODE=resource_inventory requires {RESOURCES_FILE}. "
            "Run list_user_resources.py first."
        )
    if errors:
        for err in errors:
            logger.error(err)
        sys.exit(1)


def _build_session() -> requests.Session:
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


# ──────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────

def _get_with_retry(
    session: requests.Session,
    url: str,
) -> dict[str, Any] | None:
    """GET with retry.  Returns parsed JSON or None on 403/404."""
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 429:
                retry_after = int(
                    resp.headers.get("Retry-After", RETRY_BACKOFF * attempt)
                )
                logger.warning(
                    "Rate-limited (429). Waiting %d s (attempt %d/%d)",
                    retry_after, attempt, MAX_RETRIES,
                )
                time.sleep(retry_after)
                continue

            if resp.status_code in (403, 404):
                return None

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.HTTPError as exc:
            if resp.status_code < 500 and resp.status_code != 429:
                logger.warning("HTTP %d: %s", resp.status_code, resp.text[:300])
                return None
            last_exc = exc

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc

        wait = RETRY_BACKOFF ** attempt
        logger.warning(
            "GET failed (attempt %d/%d). Retrying in %.1f s … %s",
            attempt, MAX_RETRIES, wait, last_exc,
        )
        time.sleep(wait)

    logger.error("All %d GET attempts failed for %s", MAX_RETRIES, url)
    return None


def _patch_with_retry(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
) -> tuple[bool, int, str]:
    """PATCH with retry.  Returns (success, status_code, response_text)."""
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.patch(url, json=payload, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 429:
                retry_after = int(
                    resp.headers.get("Retry-After", RETRY_BACKOFF * attempt)
                )
                logger.warning(
                    "Rate-limited (429). Waiting %d s (attempt %d/%d)",
                    retry_after, attempt, MAX_RETRIES,
                )
                time.sleep(retry_after)
                continue

            if resp.status_code in (200, 204):
                return True, resp.status_code, "OK"

            resp.raise_for_status()
            return True, resp.status_code, "OK"

        except requests.exceptions.HTTPError as exc:
            if resp.status_code < 500 and resp.status_code != 429:
                body = resp.text[:500]
                logger.error(
                    "PATCH %s -> HTTP %d: %s", url, resp.status_code, body,
                )
                return False, resp.status_code, body
            last_exc = exc

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc

        wait = RETRY_BACKOFF ** attempt
        logger.warning(
            "PATCH failed (attempt %d/%d). Retrying in %.1f s … %s",
            attempt, MAX_RETRIES, wait, last_exc,
        )
        time.sleep(wait)

    msg = f"All {MAX_RETRIES} PATCH attempts failed"
    logger.error("%s for %s", msg, url)
    return False, 0, msg


def _patch_deactivate(
    session: requests.Session,
    url: str,
) -> tuple[bool, int, str]:
    """Deactivate a user.  Tries ``activated: false`` first (v2 API on most
    deployments), then falls back to ``isActive: false`` for older versions.
    """
    success, code, body = _patch_with_retry(session, url, {"activated": False})
    if success:
        return success, code, body
    if code == 422:
        return _patch_with_retry(session, url, {"isActive": False})
    return success, code, body


# ──────────────────────────────────────────────
# File loaders
# ──────────────────────────────────────────────

def _load_json(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        logger.warning("%s not found at %s — treating as empty.", label, path)
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    logger.info("Loaded %d users from %s", len(data), path)
    return data


# ──────────────────────────────────────────────
# 5A — Non-Builder deactivation
# ──────────────────────────────────────────────

def deactivate_non_builders(
    users: list[dict[str, Any]],
    session: requests.Session,
) -> list[dict[str, Any]]:
    """Deactivate each Non-Builder dormant user (or log dry-run)."""
    results: list[dict[str, Any]] = []
    total = len(users)

    if not users:
        logger.info("5A: No Non-Builder dormant users to process.")
        return results

    logger.info(
        "5A: Processing %d Non-Builder dormant user(s)%s …",
        total, "  [DRY RUN]" if DRY_RUN else "",
    )

    for idx, user in enumerate(users, start=1):
        uid = user["user_id"]
        email = user["email"]
        url = f"{DATAROBOT_API_URL}/api/v2/users/{quote(uid)}/"

        result = {**user, "step": "5A"}

        if DRY_RUN:
            logger.info(
                "  [%d/%d] DRY RUN — WOULD deactivate %s", idx, total, email,
            )
            result["deactivation_status"] = STATUS_DRY_RUN
            result["http_status"] = None
            result["error"] = None
        else:
            success, status_code, body = _patch_deactivate(session, url)
            if success:
                logger.info(
                    "  [%d/%d] SUCCESS — %s deactivated (HTTP %d)",
                    idx, total, email, status_code,
                )
                result["deactivation_status"] = STATUS_DEACTIVATED
                result["http_status"] = status_code
                result["error"] = None
            else:
                logger.error(
                    "  [%d/%d] ERROR — %s — HTTP %d — %s",
                    idx, total, email, status_code, body,
                )
                result["deactivation_status"] = STATUS_FAILED
                result["http_status"] = status_code
                result["error"] = body

        results.append(result)

        if idx < total:
            time.sleep(REQUEST_DELAY)

    return results


# ──────────────────────────────────────────────
# 5B — Builder handling
# ──────────────────────────────────────────────

def _count_resources(
    session: requests.Session,
    user_id: str,
) -> dict[str, int | None]:
    """Query owned deployments, running jobs, and custom apps for a user.

    Returns a dict with counts (int) or None if the endpoint was
    unreachable / returned an error.
    """
    counts: dict[str, int | None] = {
        "deployments": None,
        "active_jobs": None,
        "custom_apps": None,
    }

    dep_url = (
        f"{DATAROBOT_API_URL}/api/v2/deployments/"
        f"?ownerUserId={quote(user_id)}&limit=1"
    )
    dep_data = _get_with_retry(session, dep_url)
    if dep_data is not None:
        counts["deployments"] = dep_data.get("count", len(
            dep_data.get("data") or dep_data.get("results") or []
        ))

    jobs_url = (
        f"{DATAROBOT_API_URL}/api/v2/jobs/"
        f"?ownerUserId={quote(user_id)}&status=RUNNING&limit=1"
    )
    jobs_data = _get_with_retry(session, jobs_url)
    if jobs_data is not None:
        counts["active_jobs"] = jobs_data.get("count", len(
            jobs_data.get("data") or jobs_data.get("results") or []
        ))

    apps_url = (
        f"{DATAROBOT_API_URL}/api/v2/customApplications/"
        f"?ownerUserId={quote(user_id)}&limit=1"
    )
    apps_data = _get_with_retry(session, apps_url)
    if apps_data is not None:
        counts["custom_apps"] = apps_data.get("count", len(
            apps_data.get("data") or apps_data.get("results") or []
        ))

    return counts


def handle_builders_manual_review(
    users: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Option 1 — flag every Builder for manual review (no API calls)."""
    results: list[dict[str, Any]] = []

    if not users:
        logger.info("5B: No Builder dormant users to process.")
        return results

    logger.info(
        "5B (manual_review): Flagging %d Builder user(s) for manual review …",
        len(users),
    )

    for user in users:
        result = {
            **user,
            "step": "5B",
            "deactivation_status": STATUS_PENDING_REVIEW,
            "deployments": None,
            "active_jobs": None,
            "custom_apps": None,
            "http_status": None,
            "error": None,
        }
        results.append(result)
        logger.info(
            "  FLAGGED — %s — %s",
            user["email"], STATUS_PENDING_REVIEW,
        )

    return results


def handle_builders_resource_check(
    users: list[dict[str, Any]],
    session: requests.Session,
) -> list[dict[str, Any]]:
    """Option 2 — check resources, auto-deactivate if clean."""
    results: list[dict[str, Any]] = []
    total = len(users)

    if not users:
        logger.info("5B: No Builder dormant users to process.")
        return results

    logger.info(
        "5B (resource_check): Checking resources for %d Builder user(s)%s …",
        total, "  [DRY RUN]" if DRY_RUN else "",
    )

    for idx, user in enumerate(users, start=1):
        uid = user["user_id"]
        email = user["email"]

        logger.info("  [%d/%d] Checking resources for %s …", idx, total, email)

        counts = _count_resources(session, uid)
        dep = counts["deployments"]
        jobs = counts["active_jobs"]
        apps = counts["custom_apps"]

        any_unknown = dep is None or jobs is None or apps is None
        has_resources = (dep or 0) > 0 or (jobs or 0) > 0 or (apps or 0) > 0

        result = {
            **user,
            "step": "5B",
            "deployments": dep,
            "active_jobs": jobs,
            "custom_apps": apps,
        }

        if any_unknown and not has_resources:
            logger.info(
                "    FLAGGED — %s — could not query all resource endpoints "
                "(deployments=%s, jobs=%s, apps=%s); defaulting to manual review",
                email, dep, jobs, apps,
            )
            result["deactivation_status"] = STATUS_HAS_RESOURCES
            result["http_status"] = None
            result["error"] = "one or more resource endpoints unavailable"
        elif has_resources:
            logger.info(
                "    FLAGGED — %s owns resources "
                "(deployments=%s, jobs=%s, apps=%s)",
                email, dep, jobs, apps,
            )
            result["deactivation_status"] = STATUS_HAS_RESOURCES
            result["http_status"] = None
            result["error"] = None
        else:
            url = f"{DATAROBOT_API_URL}/api/v2/users/{quote(uid)}/"

            if DRY_RUN:
                logger.info(
                    "    DRY RUN — WOULD deactivate %s (no resources)", email,
                )
                result["deactivation_status"] = STATUS_DRY_RUN
                result["http_status"] = None
                result["error"] = None
            else:
                success, status_code, body = _patch_deactivate(session, url)
                if success:
                    logger.info(
                        "    SUCCESS — %s deactivated (no resources, HTTP %d)",
                        email, status_code,
                    )
                    result["deactivation_status"] = STATUS_DEACTIVATED
                    result["http_status"] = status_code
                    result["error"] = None
                else:
                    logger.error(
                        "    ERROR — %s — HTTP %d — %s",
                        email, status_code, body,
                    )
                    result["deactivation_status"] = STATUS_FAILED
                    result["http_status"] = status_code
                    result["error"] = body

        results.append(result)

        if idx < total:
            time.sleep(REQUEST_DELAY)

    return results


def handle_builders_resource_inventory(
    users: list[dict[str, Any]],
    session: requests.Session,
) -> list[dict[str, Any]]:
    """Option 3 — use the pre-computed resource inventory from
    list_user_resources.py.  Deactivate builders with 0 resources;
    flag the rest for manual review with full resource counts.
    """
    results: list[dict[str, Any]] = []
    total = len(users)

    if not users:
        logger.info("5B: No Builder dormant users to process.")
        return results

    with open(RESOURCES_FILE, "r", encoding="utf-8") as f:
        inventory: list[dict[str, Any]] = json.load(f)

    res_map: dict[str, dict[str, Any]] = {r["user_id"]: r for r in inventory}

    logger.info(
        "5B (resource_inventory): Processing %d Builder user(s) "
        "against pre-scanned inventory%s …",
        total, "  [DRY RUN]" if DRY_RUN else "",
    )

    for idx, user in enumerate(users, start=1):
        uid = user["user_id"]
        email = user["email"]
        res_summary = res_map.get(uid, {})
        total_res = res_summary.get("total_resources", 0)
        res_counts = res_summary.get("resource_counts", {})

        result = {
            **user,
            "step": "5B",
            "deployments": res_counts.get("deployment", 0),
            "active_jobs": res_counts.get("custom_job", 0),
            "custom_apps": res_counts.get("custom_application", 0),
            "total_resources": total_res,
        }

        if total_res > 0:
            logger.info(
                "  [%d/%d] FLAGGED — %s owns %d resource(s) (%s)",
                idx, total, email, total_res,
                ", ".join(f"{v} {k}" for k, v in sorted(res_counts.items())),
            )
            result["deactivation_status"] = STATUS_HAS_RESOURCES
            result["http_status"] = None
            result["error"] = None
        else:
            url = f"{DATAROBOT_API_URL}/api/v2/users/{quote(uid)}/"

            if DRY_RUN:
                logger.info(
                    "  [%d/%d] DRY RUN — WOULD deactivate %s (0 resources)",
                    idx, total, email,
                )
                result["deactivation_status"] = STATUS_DRY_RUN
                result["http_status"] = None
                result["error"] = None
            else:
                success, status_code, body = _patch_deactivate(session, url)
                if success:
                    logger.info(
                        "  [%d/%d] SUCCESS — %s deactivated (0 resources, HTTP %d)",
                        idx, total, email, status_code,
                    )
                    result["deactivation_status"] = STATUS_DEACTIVATED
                    result["http_status"] = status_code
                    result["error"] = None
                else:
                    logger.error(
                        "  [%d/%d] ERROR — %s — HTTP %d — %s",
                        idx, total, email, status_code, body,
                    )
                    result["deactivation_status"] = STATUS_FAILED
                    result["http_status"] = status_code
                    result["error"] = body

            if idx < total:
                time.sleep(REQUEST_DELAY)

        results.append(result)

    return results


def handle_builders_force_deactivate(
    users: list[dict[str, Any]],
    session: requests.Session,
) -> list[dict[str, Any]]:
    """Force-deactivate all Builder users regardless of owned resources."""
    results: list[dict[str, Any]] = []
    total = len(users)

    if not users:
        logger.info("5B: No Builder dormant users to process.")
        return results

    logger.info(
        "5B (force_deactivate): Deactivating %d Builder user(s) "
        "regardless of resources%s …",
        total, "  [DRY RUN]" if DRY_RUN else "",
    )

    for idx, user in enumerate(users, start=1):
        uid = user["user_id"]
        email = user["email"]
        url = f"{DATAROBOT_API_URL}/api/v2/users/{quote(uid)}/"

        result = {**user, "step": "5B"}

        if DRY_RUN:
            logger.info(
                "  [%d/%d] DRY RUN — WOULD force-deactivate %s",
                idx, total, email,
            )
            result["deactivation_status"] = STATUS_DRY_RUN
            result["http_status"] = None
            result["error"] = None
        else:
            success, status_code, body = _patch_deactivate(session, url)
            if success:
                logger.info(
                    "  [%d/%d] SUCCESS — %s force-deactivated (HTTP %d)",
                    idx, total, email, status_code,
                )
                result["deactivation_status"] = STATUS_DEACTIVATED
                result["http_status"] = status_code
                result["error"] = None
            else:
                logger.error(
                    "  [%d/%d] ERROR — %s — HTTP %d — %s",
                    idx, total, email, status_code, body,
                )
                result["deactivation_status"] = STATUS_FAILED
                result["http_status"] = status_code
                result["error"] = body

        results.append(result)

        if idx < total:
            time.sleep(REQUEST_DELAY)

    return results


# ──────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────

def _print_report(results: list[dict[str, Any]]) -> None:
    total = len(results)
    by_status: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_status.setdefault(r["deactivation_status"], []).append(r)

    print("\n" + "=" * 76)
    print("  DEACTIVATION RESULTS")
    print("=" * 76)
    print(f"  DRY RUN:       {'YES' if DRY_RUN else 'NO (LIVE)'}")
    print(f"  Builder mode:  {BUILDER_MODE}")
    print(f"  Total processed: {total}")
    print("-" * 76)

    status_order = [
        STATUS_DEACTIVATED, STATUS_DRY_RUN, STATUS_FAILED,
        STATUS_PENDING_REVIEW, STATUS_HAS_RESOURCES,
    ]
    for status in status_order:
        group = by_status.get(status, [])
        if not group:
            continue
        label = {
            STATUS_DEACTIVATED: "DEACTIVATED",
            STATUS_DRY_RUN: "DRY RUN (would deactivate)",
            STATUS_FAILED: "FAILED",
            STATUS_PENDING_REVIEW: "PENDING MANUAL REVIEW",
            STATUS_HAS_RESOURCES: "HAS RESOURCES (manual review)",
        }.get(status, status)

        print(f"\n  {label}  ({len(group)}):")
        for u in group:
            step = u.get("step", "?")
            seat = u.get("seat_type_display", u.get("seat_type") or "Unknown")
            dormancy = (
                f"{u['days_dormant']}d"
                if u.get("days_dormant", 9999) < 9999
                else f"Not Active for the last {DORMANCY_THRESHOLD_DAYS} days"
            )
            line = f"    [{step}] {u['email']:<50} seat={seat:<15} dormant={dormancy}"

            if status == STATUS_HAS_RESOURCES:
                dep = u.get("deployments")
                jobs = u.get("active_jobs")
                apps = u.get("custom_apps")
                fmt = lambda v: "?" if v is None else str(v)
                line += f"  res=({fmt(dep)}d/{fmt(jobs)}j/{fmt(apps)}a)"

            if status == STATUS_FAILED:
                line += f"  err=HTTP {u.get('http_status', '?')}"

            print(line)

    print("=" * 76 + "\n")


def save_json(data: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    logger.info("JSON saved -> %s  (%d records)", path, len(data))


def save_csv(data: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "user_id", "email", "seat_type", "seat_type_display", "route", "step",
        "is_active", "last_activity", "days_dormant", "is_dormant",
        "deactivation_status", "http_status", "error",
        "deployments", "active_jobs", "custom_apps",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in data:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    logger.info("CSV  saved -> %s  (%d records)", path, len(data))


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 76)
    logger.info("  Step 5 - Deactivate Dormant Users")
    logger.info("=" * 76)

    if DRY_RUN:
        logger.info("*** DRY RUN MODE — no accounts will be deactivated ***")
    else:
        logger.warning("*** LIVE MODE — accounts WILL be deactivated ***")

    _validate_config()
    session = _build_session()

    non_builders = _load_json(NON_BUILDERS_FILE, "Step 4 non-builders output")
    builders = _load_json(BUILDERS_FILE, "Step 4 builders output")

    all_results: list[dict[str, Any]] = []

    # ── 5A: Non-Builders ──
    nb_results = deactivate_non_builders(non_builders, session)
    all_results.extend(nb_results)

    # ── 5B: Builders ──
    if BUILDER_MODE == "force_deactivate":
        b_results = handle_builders_force_deactivate(builders, session)
    elif BUILDER_MODE == "resource_inventory":
        b_results = handle_builders_resource_inventory(builders, session)
    elif BUILDER_MODE == "resource_check":
        b_results = handle_builders_resource_check(builders, session)
    else:
        b_results = handle_builders_manual_review(builders)

    all_results.extend(b_results)

    # ── Summary ──
    deactivated = sum(
        1 for r in all_results if r["deactivation_status"] == STATUS_DEACTIVATED
    )
    dry_run_ct = sum(
        1 for r in all_results if r["deactivation_status"] == STATUS_DRY_RUN
    )
    failed = sum(
        1 for r in all_results if r["deactivation_status"] == STATUS_FAILED
    )
    pending = sum(
        1 for r in all_results
        if r["deactivation_status"] in (STATUS_PENDING_REVIEW, STATUS_HAS_RESOURCES)
    )

    logger.info(
        "Step 5 complete: %d deactivated, %d dry-run, %d failed, %d pending review",
        deactivated, dry_run_ct, failed, pending,
    )

    _print_report(all_results)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_json(all_results, OUTPUT_DIR / "deactivation_results.json")
    save_csv(all_results, OUTPUT_DIR / "deactivation_results.csv")

    logger.info("Done.  Files written to %s", OUTPUT_DIR)


if __name__ == "__main__":
    main()
