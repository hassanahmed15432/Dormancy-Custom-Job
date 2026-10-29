#!/usr/bin/env python3
"""
List Resources Owned by Dormant Users
=======================================
Fetches all resources across the DataRobot instance and cross-references
ownership against the dormant user list from Step 3.  Produces a per-user
resource inventory so admins can decide what to transfer or retire before
deactivating accounts.

Supported resource types and their ownership detection:

  • Deployments        — creator.id / owners.preview[].id
  • Projects           — /accessControl/ sub-endpoint (role=OWNER)
  • Custom Models      — createdBy (email match)
  • Custom Applications— userId
  • Custom Jobs        — userId (if present)
  • Use Cases          — owners[].id
  • Registered Models  — createdBy.id
  • Batch Predictions  — userId
  • Notebooks          — best-effort (limited owner info in API)

Usage
-----
  1. Ensure Step 3 has been run (dormant_users.json exists in output/).
  2. Run:  python list_user_resources.py

Output
------
  • Console inventory grouped by user.
  • ``output/dormant_user_resources.json`` — structured per-user resource list.
  • ``output/dormant_user_resources.csv``  — flattened (one row per resource).
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
import urllib3
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
REQUEST_DELAY: float = float(os.getenv("REQUEST_DELAY", "0.2"))
PAGE_SIZE: int = int(os.getenv("PAGE_SIZE", "100"))

if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OUTPUT_DIR: Path = Path(__file__).resolve().parent / "output"
INPUT_FILE: Path = OUTPUT_DIR / "dormant_users.json"

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("list_user_resources")


# ──────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────

def _build_session() -> requests.Session:
    session = requests.Session()
    session.verify = SSL_VERIFY
    session.headers.update({
        "Authorization": f"Bearer {DATAROBOT_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    return session


def _get(session: requests.Session, url: str) -> dict[str, Any] | list | None:
    """GET with retry. Returns parsed JSON or None on error."""
    last_exc: Exception | None = None
    resp = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", RETRY_BACKOFF * attempt))
                logger.warning("Rate-limited. Waiting %d s …", wait)
                time.sleep(wait)
                continue

            if resp.status_code in (400, 403, 404):
                return None

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.HTTPError as exc:
            if resp is not None and resp.status_code < 500:
                return None
            last_exc = exc
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc

        wait = RETRY_BACKOFF ** attempt
        logger.warning("GET failed (%d/%d). Retry in %.1f s …", attempt, MAX_RETRIES, wait)
        time.sleep(wait)

    logger.error("All GET attempts failed for %s: %s", url, last_exc)
    return None


def _paginate_all(
    session: requests.Session,
    base_url: str,
    page_size: int = PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Paginate through a DataRobot list endpoint and collect all items."""
    all_items: list[dict[str, Any]] = []
    offset = 0

    while True:
        sep = "&" if "?" in base_url else "?"
        url = f"{base_url}{sep}limit={page_size}&offset={offset}"
        data = _get(session, url)

        if data is None:
            break

        if isinstance(data, list):
            all_items.extend(data)
            if len(data) < page_size:
                break
            offset += page_size
        else:
            items = data.get("data") or data.get("results") or []
            all_items.extend(items)
            if not items or len(items) < page_size:
                break
            if data.get("next"):
                offset += page_size
            else:
                break

        time.sleep(REQUEST_DELAY)

    return all_items


# ──────────────────────────────────────────────
# Resource fetchers
# ──────────────────────────────────────────────

ResourceRecord = dict[str, Any]


def _fetch_deployments(
    session: requests.Session,
    dormant_ids: set[str],
) -> list[ResourceRecord]:
    """Deployments — ownership via creator.id and owners.preview[].id."""
    logger.info("  Fetching deployments …")
    items = _paginate_all(session, f"{DATAROBOT_API_URL}/api/v2/deployments/")
    logger.info("    %d deployments total", len(items))

    results: list[ResourceRecord] = []
    for item in items:
        owner_ids: set[str] = set()
        creator = item.get("creator") or {}
        if creator.get("id"):
            owner_ids.add(creator["id"])
        for o in (item.get("owners") or {}).get("preview") or []:
            if o.get("id"):
                owner_ids.add(o["id"])

        for uid in owner_ids & dormant_ids:
            results.append({
                "resource_type": "deployment",
                "resource_id": item.get("id", ""),
                "resource_name": item.get("label", ""),
                "owner_user_id": uid,
                "status": item.get("status", ""),
                "created_at": item.get("createdAt", ""),
            })
    return results


def _fetch_projects(
    session: requests.Session,
    dormant_ids: set[str],
) -> list[ResourceRecord]:
    """Projects — ownership via /accessControl/ (role=OWNER)."""
    logger.info("  Fetching projects …")
    items = _paginate_all(session, f"{DATAROBOT_API_URL}/api/v2/projects/")
    logger.info("    %d projects total", len(items))

    results: list[ResourceRecord] = []
    for item in items:
        pid = item.get("id", "")
        ac_data = _get(session, f"{DATAROBOT_API_URL}/api/v2/projects/{pid}/accessControl/")
        if not ac_data:
            continue

        entries = []
        if isinstance(ac_data, dict):
            entries = ac_data.get("data") or ac_data.get("results") or []
        elif isinstance(ac_data, list):
            entries = ac_data

        for entry in entries:
            uid = entry.get("userId", "")
            role = entry.get("role", "")
            if uid in dormant_ids and role == "OWNER":
                results.append({
                    "resource_type": "project",
                    "resource_id": pid,
                    "resource_name": item.get("projectName", ""),
                    "owner_user_id": uid,
                    "status": item.get("stage", ""),
                    "created_at": item.get("created", ""),
                })
        time.sleep(REQUEST_DELAY)

    return results


def _fetch_custom_models(
    session: requests.Session,
    dormant_ids: set[str],
    email_to_uid: dict[str, str],
) -> list[ResourceRecord]:
    """Custom Models — createdBy is an email string; match via lookup."""
    logger.info("  Fetching custom models …")
    items = _paginate_all(session, f"{DATAROBOT_API_URL}/api/v2/customModels/")
    logger.info("    %d custom models total", len(items))

    results: list[ResourceRecord] = []
    for item in items:
        created_by = item.get("createdBy", "")
        uid = ""
        if isinstance(created_by, dict):
            uid = created_by.get("id", "")
        elif isinstance(created_by, str):
            uid = email_to_uid.get(created_by.lower(), "")

        if uid and uid in dormant_ids:
            results.append({
                "resource_type": "custom_model",
                "resource_id": item.get("id", ""),
                "resource_name": item.get("name", ""),
                "owner_user_id": uid,
                "status": "",
                "created_at": item.get("created", ""),
            })
    return results


def _fetch_custom_applications(
    session: requests.Session,
    dormant_ids: set[str],
) -> list[ResourceRecord]:
    """Custom Applications — userId field."""
    logger.info("  Fetching custom applications …")
    items = _paginate_all(session, f"{DATAROBOT_API_URL}/api/v2/customApplications/")
    logger.info("    %d custom applications total", len(items))

    results: list[ResourceRecord] = []
    for item in items:
        uid = item.get("userId", "")
        if uid in dormant_ids:
            results.append({
                "resource_type": "custom_application",
                "resource_id": item.get("id", ""),
                "resource_name": item.get("name", ""),
                "owner_user_id": uid,
                "status": item.get("status", ""),
                "created_at": item.get("createdAt", ""),
            })
    return results


def _fetch_custom_jobs(
    session: requests.Session,
    dormant_ids: set[str],
) -> list[ResourceRecord]:
    """Custom Jobs — userId or createdBy field."""
    logger.info("  Fetching custom jobs …")
    items = _paginate_all(session, f"{DATAROBOT_API_URL}/api/v2/customJobs/")
    logger.info("    %d custom jobs total", len(items))

    results: list[ResourceRecord] = []
    for item in items:
        uid = item.get("userId") or ""
        if not uid:
            cb = item.get("createdBy")
            if isinstance(cb, dict):
                uid = cb.get("id", "")
        if uid in dormant_ids:
            results.append({
                "resource_type": "custom_job",
                "resource_id": item.get("id", ""),
                "resource_name": item.get("name", ""),
                "owner_user_id": uid,
                "status": "",
                "created_at": item.get("created", ""),
            })
    return results


def _fetch_use_cases(
    session: requests.Session,
    dormant_ids: set[str],
) -> list[ResourceRecord]:
    """Use Cases — owners[].id list."""
    logger.info("  Fetching use cases …")
    items = _paginate_all(session, f"{DATAROBOT_API_URL}/api/v2/useCases/")
    logger.info("    %d use cases total", len(items))

    results: list[ResourceRecord] = []
    for item in items:
        owners = item.get("owners") or []
        for owner in owners:
            uid = owner.get("id", "")
            if uid in dormant_ids:
                results.append({
                    "resource_type": "use_case",
                    "resource_id": item.get("id", ""),
                    "resource_name": item.get("name", ""),
                    "owner_user_id": uid,
                    "status": "",
                    "created_at": item.get("createdAt", ""),
                })
    return results


def _fetch_registered_models(
    session: requests.Session,
    dormant_ids: set[str],
    email_to_uid: dict[str, str],
) -> list[ResourceRecord]:
    """Registered Models — createdBy.id (dict) or email (string)."""
    logger.info("  Fetching registered models …")
    items = _paginate_all(session, f"{DATAROBOT_API_URL}/api/v2/registeredModels/")
    logger.info("    %d registered models total", len(items))

    results: list[ResourceRecord] = []
    for item in items:
        created_by = item.get("createdBy")
        uid = ""
        if isinstance(created_by, dict):
            uid = created_by.get("id", "")
        elif isinstance(created_by, str):
            uid = email_to_uid.get(created_by.lower(), "")

        if uid and uid in dormant_ids:
            results.append({
                "resource_type": "registered_model",
                "resource_id": item.get("id", ""),
                "resource_name": item.get("name", ""),
                "owner_user_id": uid,
                "status": "",
                "created_at": item.get("createdAt", item.get("created", "")),
            })
    return results


def _fetch_batch_predictions(
    session: requests.Session,
    dormant_ids: set[str],
) -> list[ResourceRecord]:
    """Batch Predictions — userId field."""
    logger.info("  Fetching batch predictions …")
    items = _paginate_all(session, f"{DATAROBOT_API_URL}/api/v2/batchPredictions/")
    logger.info("    %d batch predictions total", len(items))

    results: list[ResourceRecord] = []
    for item in items:
        uid = item.get("userId") or ""
        if uid in dormant_ids:
            results.append({
                "resource_type": "batch_prediction",
                "resource_id": item.get("id", ""),
                "resource_name": item.get("jobName", ""),
                "owner_user_id": uid,
                "status": item.get("status", ""),
                "created_at": item.get("createdAt", item.get("created", "")),
            })
    return results


def _fetch_notebooks(
    session: requests.Session,
    dormant_ids: set[str],
) -> list[ResourceRecord]:
    """Notebooks — limited ownership info; best-effort via permissions."""
    logger.info("  Fetching notebooks …")
    items = _paginate_all(session, f"{DATAROBOT_API_URL}/api/v2/notebooks/")
    logger.info("    %d notebooks total", len(items))

    results: list[ResourceRecord] = []
    for item in items:
        uid = item.get("userId") or item.get("ownerId") or ""
        if isinstance(item.get("createdBy"), dict):
            uid = uid or item["createdBy"].get("id", "")
        if uid in dormant_ids:
            results.append({
                "resource_type": "notebook",
                "resource_id": item.get("id", ""),
                "resource_name": item.get("name", ""),
                "owner_user_id": uid,
                "status": "",
                "created_at": item.get("created", ""),
            })
    return results


# ──────────────────────────────────────────────
# Aggregation
# ──────────────────────────────────────────────

def _aggregate_by_user(
    resources: list[ResourceRecord],
    dormant_users: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a per-user summary with resource lists and counts."""
    user_resources: dict[str, list[ResourceRecord]] = {}
    for rec in resources:
        uid = rec["owner_user_id"]
        user_resources.setdefault(uid, []).append(rec)

    user_map = {u["user_id"]: u for u in dormant_users}
    summaries: list[dict[str, Any]] = []

    for user in dormant_users:
        uid = user["user_id"]
        owned = user_resources.get(uid, [])
        type_counts: dict[str, int] = {}
        for r in owned:
            rt = r["resource_type"]
            type_counts[rt] = type_counts.get(rt, 0) + 1

        summaries.append({
            "user_id": uid,
            "email": user["email"],
            "seat_type": user.get("seat_type"),
            "days_dormant": user.get("days_dormant", 0),
            "total_resources": len(owned),
            "resource_counts": type_counts,
            "resources": owned,
        })

    summaries.sort(key=lambda u: u["total_resources"], reverse=True)
    return summaries


# ──────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────

def _print_report(summaries: list[dict[str, Any]], total_resources: int) -> None:
    users_with = [s for s in summaries if s["total_resources"] > 0]
    users_without = [s for s in summaries if s["total_resources"] == 0]

    print("\n" + "=" * 80)
    print("  DORMANT USER RESOURCE INVENTORY")
    print("=" * 80)
    print(f"  Dormant users scanned:    {len(summaries)}")
    print(f"  Users owning resources:   {len(users_with)}")
    print(f"  Users with no resources:  {len(users_without)}")
    print(f"  Total resources found:    {total_resources}")
    print("-" * 80)

    if users_with:
        print("\n  USERS WITH RESOURCES:")
        for s in users_with:
            dormancy = f"{s['days_dormant']}d" if s["days_dormant"] < 9999 else "never active"
            print(f"\n    {s['email']}  (dormant: {dormancy})")
            for rtype, count in sorted(s["resource_counts"].items()):
                print(f"      {rtype:<25} {count:>4}")
            for r in s["resources"]:
                name = r["resource_name"] or "(unnamed)"
                print(f"        - [{r['resource_type']}] {name}  (id={r['resource_id'][:20]}…)")

    if users_without:
        print(f"\n  USERS WITH NO RESOURCES  ({len(users_without)}):")
        for s in users_without:
            dormancy = f"{s['days_dormant']}d" if s["days_dormant"] < 9999 else "never active"
            print(f"    . {s['email']:<55} ({dormancy})")

    print("=" * 80 + "\n")


def save_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    logger.info("JSON saved -> %s", path)


def save_csv(resources: list[ResourceRecord], path: Path) -> None:
    """Flat CSV — one row per resource."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "owner_user_id", "owner_email", "resource_type", "resource_id",
        "resource_name", "status", "created_at",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in resources:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    logger.info("CSV  saved -> %s  (%d rows)", path, len(resources))


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 80)
    logger.info("  List Resources Owned by Dormant Users")
    logger.info("=" * 80)

    if not DATAROBOT_API_URL or not DATAROBOT_API_TOKEN:
        logger.error("DATAROBOT_API_URL / DATAROBOT_API_TOKEN not set.")
        sys.exit(1)

    if not INPUT_FILE.exists():
        logger.warning("Dormant users file not found at %s — treating as empty.", INPUT_FILE)
        dormant_users: list[dict[str, Any]] = []
    else:
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            dormant_users = json.load(f)
    logger.info("Loaded %d dormant users.", len(dormant_users))

    summaries: list[dict[str, Any]] = []
    all_resources: list[ResourceRecord] = []

    if dormant_users:
        dormant_ids: set[str] = {u["user_id"] for u in dormant_users}
        email_to_uid: dict[str, str] = {
            u["email"].lower(): u["user_id"] for u in dormant_users
        }

        session = _build_session()
        if not SSL_VERIFY:
            logger.info("SSL verification disabled.")

        logger.info("Scanning all resource types …")

        all_resources.extend(_fetch_deployments(session, dormant_ids))
        all_resources.extend(_fetch_projects(session, dormant_ids))
        all_resources.extend(_fetch_custom_models(session, dormant_ids, email_to_uid))
        all_resources.extend(_fetch_custom_applications(session, dormant_ids))
        all_resources.extend(_fetch_custom_jobs(session, dormant_ids))
        all_resources.extend(_fetch_use_cases(session, dormant_ids))
        all_resources.extend(_fetch_registered_models(session, dormant_ids, email_to_uid))
        all_resources.extend(_fetch_batch_predictions(session, dormant_ids))
        all_resources.extend(_fetch_notebooks(session, dormant_ids))

        uid_to_email = {u["user_id"]: u["email"] for u in dormant_users}
        for r in all_resources:
            r["owner_email"] = uid_to_email.get(r["owner_user_id"], "")

        logger.info(
            "Scan complete: %d resources owned by dormant users.", len(all_resources),
        )

        summaries = _aggregate_by_user(all_resources, dormant_users)
        _print_report(summaries, len(all_resources))
    else:
        logger.warning("No dormant users — skipping resource scan.")

    save_json(summaries, OUTPUT_DIR / "dormant_user_resources.json")
    save_csv(all_resources, OUTPUT_DIR / "dormant_user_resources.csv")

    logger.info("Done.  Files written to %s", OUTPUT_DIR)


if __name__ == "__main__":
    main()
