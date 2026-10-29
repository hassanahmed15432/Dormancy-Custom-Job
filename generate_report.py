#!/usr/bin/env python3
"""
Step 6 — Generate the Output Report
======================================
Reads all prior-step outputs and produces a structured Excel workbook
to be handed to the customer admin as the final deliverable.

Tabs
----
  1. **Summary**                  — Run metadata, counts, and high-level stats.
  2. **Non-Builder — Deactivated** — One row per Non-Builder dormant user.
  3. **Builder — Manual Review**   — One row per Builder user requiring action,
     with optional resource counts (deployments, jobs, apps).
  4. **Resource Inventory**        — Per-user resource breakdown (if
     ``dormant_user_resources.json`` exists from ``list_user_resources.py``).
  5. **Audit Log**                 — Every API write action recorded during
     the deactivation run.

Usage
-----
  1. Ensure Steps 1–5 have been run.
  2. Run:  python generate_report.py

Output
------
  • ``output/inactive_users_report_<YYYYMMDD>.xlsx``
"""

from __future__ import annotations

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
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

load_dotenv()

DATAROBOT_API_URL: str = os.getenv(
    "DATAROBOT_API_URL", os.getenv("DATAROBOT_ENDPOINT", "")
).rstrip("/")
DORMANCY_THRESHOLD: int = int(os.getenv("DORMANCY_THRESHOLD_DAYS", "40"))
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
BUILDER_MODE: str = os.getenv("BUILDER_MODE", "manual_review")

OUTPUT_DIR: Path = Path(__file__).resolve().parent / "output"

DEACTIVATION_FILE: Path = OUTPUT_DIR / "deactivation_results.json"
RESOURCES_FILE: Path = OUTPUT_DIR / "dormant_user_resources.json"
NON_BUILDERS_FILE: Path = OUTPUT_DIR / "dormant_non_builders.json"
BUILDERS_FILE: Path = OUTPUT_DIR / "dormant_builders.json"
USER_ACTIVITY_FILE: Path = OUTPUT_DIR / "user_activity.json"

NO_ACTIVITY_SENTINEL = 9999

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("generate_report")

# ──────────────────────────────────────────────
# Styles
# ──────────────────────────────────────────────

HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)

LABEL_FONT = Font(bold=True, size=11)
VALUE_FONT = Font(size=11)
TITLE_FONT = Font(bold=True, size=14, color="2F5496")

THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)

STATUS_FILLS = {
    "DEACTIVATED": PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
    "DRY_RUN": PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid"),
    "FAILED": PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
    "PENDING_MANUAL_REVIEW": PatternFill(start_color="FFE699", end_color="FFE699", fill_type="solid"),
    "MANUAL_REVIEW_HAS_RESOURCES": PatternFill(start_color="F4B084", end_color="F4B084", fill_type="solid"),
}


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _dormancy_display(days: int | None) -> str:
    if days is None or days >= NO_ACTIVITY_SENTINEL:
        return "No activity ever"
    return str(days)


def _activity_display(iso_ts: str | None) -> str:
    if not iso_ts:
        return "None"
    return iso_ts[:19].replace("T", " ")


def _activity_type_display(row: dict[str, Any]) -> str:
    activity_type = row.get("last_activity_type")
    if activity_type:
        return activity_type
    has_app = bool(row.get("last_app_usage") or row.get("last_event_log"))
    has_pred = bool(row.get("last_prediction_usage"))
    if has_app and has_pred:
        return "App Usage + Prediction"
    if has_app:
        return "App Usage"
    if has_pred:
        return "Prediction Usage"
    return "None"


def _style_header_row(ws, col_count: int) -> None:
    for col in range(1, col_count + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT
        cell.border = THIN_BORDER


def _auto_width(ws, min_width: int = 10, max_width: int = 55) -> None:
    for col_cells in ws.columns:
        length = min_width
        col_letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            if cell.value:
                length = max(length, min(len(str(cell.value)) + 2, max_width))
        ws.column_dimensions[col_letter].width = length


def _apply_border(ws) -> None:
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=ws.max_column):
        for cell in row:
            cell.border = THIN_BORDER


# ──────────────────────────────────────────────
# Tab builders
# ──────────────────────────────────────────────

def _build_summary_tab(
    wb: Workbook,
    deactivation_results: list[dict[str, Any]],
    non_builders: list[dict[str, Any]],
    builders: list[dict[str, Any]],
    resource_summaries: list[dict[str, Any]],
) -> None:
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_properties.tabColor = "2F5496"

    now = datetime.now(timezone.utc)

    nb_results = [r for r in deactivation_results if r.get("step") == "5A"]
    b_results = [r for r in deactivation_results if r.get("step") == "5B"]

    nb_deactivated = sum(1 for r in nb_results if r.get("deactivation_status") == "DEACTIVATED")
    nb_dryrun = sum(1 for r in nb_results if r.get("deactivation_status") == "DRY_RUN")
    nb_errors = sum(1 for r in nb_results if r.get("deactivation_status") == "FAILED")

    b_deactivated = sum(1 for r in b_results if r.get("deactivation_status") == "DEACTIVATED")
    b_dryrun = sum(1 for r in b_results if r.get("deactivation_status") == "DRY_RUN")
    b_review = sum(
        1 for r in b_results
        if r.get("deactivation_status") in ("PENDING_MANUAL_REVIEW", "MANUAL_REVIEW_HAS_RESOURCES")
    )
    b_errors = sum(1 for r in b_results if r.get("deactivation_status") == "FAILED")

    total_dormant = len(non_builders) + len(builders)

    users_with_resources = sum(1 for s in resource_summaries if s.get("total_resources", 0) > 0)
    total_resources = sum(s.get("total_resources", 0) for s in resource_summaries)

    rows: list[tuple[str, Any]] = [
        ("Report Generated", now.strftime("%Y-%m-%d %H:%M:%S UTC")),
        ("DataRobot Instance", DATAROBOT_API_URL),
        ("Dormancy Threshold (days)", DORMANCY_THRESHOLD),
        ("Execution Mode", "DRY RUN" if DRY_RUN else "LIVE"),
        ("Builder Mode", BUILDER_MODE),
        ("", ""),
        ("DORMANT USER COUNTS", ""),
        ("Total dormant users", total_dormant),
        ("Non-Builders", len(non_builders)),
        ("Builders", len(builders)),
        ("", ""),
        ("NON-BUILDER RESULTS", ""),
        ("Deactivated", nb_deactivated),
        ("Dry-run (would deactivate)", nb_dryrun),
        ("Errors", nb_errors),
        ("", ""),
        ("BUILDER RESULTS", ""),
        ("Deactivated (no resources)", b_deactivated),
        ("Dry-run (would deactivate)", b_dryrun),
        ("Pending manual review", b_review),
        ("Errors", b_errors),
        ("", ""),
        ("RESOURCE INVENTORY", ""),
        ("Users owning resources", users_with_resources),
        ("Users with no resources", len(resource_summaries) - users_with_resources),
        ("Total resources owned", total_resources),
    ]

    ws.cell(row=1, column=1, value="Inactive Users Report").font = TITLE_FONT
    ws.merge_cells("A1:B1")

    for idx, (label, value) in enumerate(rows, start=3):
        label_cell = ws.cell(row=idx, column=1, value=label)
        value_cell = ws.cell(row=idx, column=2, value=value)

        if label and not value and value != 0:
            label_cell.font = Font(bold=True, size=12, color="2F5496")
        else:
            label_cell.font = LABEL_FONT
            value_cell.font = VALUE_FONT

    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 60


def _build_non_builder_tab(
    wb: Workbook,
    deactivation_results: list[dict[str, Any]],
) -> None:
    ws = wb.create_sheet("Non-Builder — Deactivated")
    ws.sheet_properties.tabColor = "548235"

    headers = [
        "Email", "User ID", "Seat Type", "Last Activity",
        "Activity Type", "Days Dormant", "Deactivation Status", "Detail",
    ]
    ws.append(headers)
    _style_header_row(ws, len(headers))

    nb_results = [r for r in deactivation_results if r.get("step") == "5A"]

    if not nb_results:
        ws.append(["(No Non-Builder dormant users in this run)"] + [""] * (len(headers) - 1))
    else:
        for r in sorted(nb_results, key=lambda x: x.get("days_dormant", 0), reverse=True):
            status = r.get("deactivation_status", "")
            error = r.get("error") or ""
            http = r.get("http_status")
            detail = f"HTTP {http}" if http else ""
            if error:
                detail = f"HTTP {http} — {error}" if http else error

            row_idx = ws.max_row + 1
            ws.append([
                r.get("email", ""),
                r.get("user_id", ""),
                r.get("seat_type_display") or r.get("seat_type") or "Unknown",
                _activity_display(r.get("last_activity")),
                _activity_type_display(r),
                _dormancy_display(r.get("days_dormant")),
                status,
                detail,
            ])
            fill = STATUS_FILLS.get(status)
            if fill:
                ws.cell(row=row_idx, column=7).fill = fill

    _apply_border(ws)
    _auto_width(ws)


def _build_builder_tab(
    wb: Workbook,
    deactivation_results: list[dict[str, Any]],
    resource_summaries: list[dict[str, Any]],
) -> None:
    ws = wb.create_sheet("Builder — Manual Review")
    ws.sheet_properties.tabColor = "BF8F00"

    headers = [
        "Email", "User ID", "Seat Type", "Last Activity",
        "Activity Type", "Days Dormant", "Deactivation Status",
        "Deployments", "Active Jobs", "Custom Apps",
        "Total Resources", "Detail",
    ]
    ws.append(headers)
    _style_header_row(ws, len(headers))

    b_results = [r for r in deactivation_results if r.get("step") == "5B"]

    resource_map: dict[str, dict[str, Any]] = {
        s["user_id"]: s for s in resource_summaries
    }

    if not b_results:
        ws.append(["(No Builder dormant users in this run)"] + [""] * (len(headers) - 1))
    else:
        for r in sorted(b_results, key=lambda x: x.get("days_dormant", 0), reverse=True):
            uid = r.get("user_id", "")
            status = r.get("deactivation_status", "")
            error = r.get("error") or ""
            http = r.get("http_status")
            detail = f"HTTP {http}" if http else ""
            if error:
                detail = f"HTTP {http} — {error}" if http else error

            res = resource_map.get(uid, {})
            res_counts = res.get("resource_counts", {})
            dep_count = r.get("deployments")
            job_count = r.get("active_jobs")
            app_count = r.get("custom_apps")

            if dep_count is None:
                dep_count = res_counts.get("deployment", "—")
            if job_count is None:
                job_count = res_counts.get("custom_job", "—")
            if app_count is None:
                app_count = res_counts.get("custom_application", "—")

            total_res = res.get("total_resources", "—")

            row_idx = ws.max_row + 1
            ws.append([
                r.get("email", ""),
                uid,
                r.get("seat_type_display") or r.get("seat_type") or "Unknown",
                _activity_display(r.get("last_activity")),
                _activity_type_display(r),
                _dormancy_display(r.get("days_dormant")),
                status,
                dep_count,
                job_count,
                app_count,
                total_res,
                detail,
            ])
            fill = STATUS_FILLS.get(status)
            if fill:
                ws.cell(row=row_idx, column=7).fill = fill

    _apply_border(ws)
    _auto_width(ws)


def _build_resource_tab(
    wb: Workbook,
    resource_summaries: list[dict[str, Any]],
    activity_map: dict[str, dict[str, Any]],
) -> None:
    ws = wb.create_sheet("Resource Inventory")
    ws.sheet_properties.tabColor = "7030A0"

    headers = [
        "Owner Email", "Owner User ID", "Days Dormant",
        "Last Activity", "Activity Type",
        "Resource Type", "Resource Name", "Resource ID",
        "Status", "Created At",
    ]
    ws.append(headers)
    _style_header_row(ws, len(headers))

    has_rows = False
    for summary in resource_summaries:
        uid = summary.get("user_id", "")
        act = activity_map.get(uid, {})
        for res in summary.get("resources", []):
            has_rows = True
            ws.append([
                summary.get("email", ""),
                uid,
                _dormancy_display(summary.get("days_dormant")),
                _activity_display(act.get("last_activity")),
                _activity_type_display(act),
                res.get("resource_type", ""),
                res.get("resource_name", ""),
                res.get("resource_id", ""),
                res.get("status", ""),
                _activity_display(res.get("created_at")),
            ])

    if not has_rows:
        ws.append(["(No resources found for dormant users)"] + [""] * (len(headers) - 1))

    _apply_border(ws)
    _auto_width(ws)


def _build_audit_tab(
    wb: Workbook,
    deactivation_results: list[dict[str, Any]],
) -> None:
    ws = wb.create_sheet("Audit Log")
    ws.sheet_properties.tabColor = "C00000"

    headers = [
        "Timestamp", "Email", "User ID", "Step",
        "Action", "Deactivation Status",
        "HTTP Status", "Response Detail",
    ]
    ws.append(headers)
    _style_header_row(ws, len(headers))

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    has_rows = False

    for r in deactivation_results:
        status = r.get("deactivation_status", "")

        if status == "DEACTIVATED":
            action = "PATCH /api/v2/users/{id}/ — isActive=false"
        elif status == "DRY_RUN":
            action = "DRY RUN — would PATCH /api/v2/users/{id}/ — isActive=false"
        elif status == "FAILED":
            action = "PATCH /api/v2/users/{id}/ — isActive=false (FAILED)"
        elif status == "PENDING_MANUAL_REVIEW":
            action = "Flagged for manual review (no API call)"
        elif status == "MANUAL_REVIEW_HAS_RESOURCES":
            action = "Flagged — owns resources (no deactivation)"
        else:
            action = status

        error = r.get("error") or ""
        http = r.get("http_status")
        http_str = str(http) if http is not None else "—"

        has_rows = True
        row_idx = ws.max_row + 1
        ws.append([
            now_str,
            r.get("email", ""),
            r.get("user_id", ""),
            r.get("step", ""),
            action,
            status,
            http_str,
            error,
        ])
        fill = STATUS_FILLS.get(status)
        if fill:
            ws.cell(row=row_idx, column=6).fill = fill

    if not has_rows:
        ws.append(["(No actions recorded)"] + [""] * (len(headers) - 1))

    _apply_border(ws)
    _auto_width(ws)


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 72)
    logger.info("  Step 6 - Generate Output Report")
    logger.info("=" * 72)

    deactivation_results = _load_json(DEACTIVATION_FILE)
    resource_summaries = _load_json(RESOURCES_FILE)
    non_builders = _load_json(NON_BUILDERS_FILE)
    builders = _load_json(BUILDERS_FILE)
    user_activity = _load_json(USER_ACTIVITY_FILE)

    activity_map: dict[str, dict[str, Any]] = {
        u["user_id"]: u for u in user_activity if "user_id" in u
    }

    if not deactivation_results:
        logger.warning("No deactivation results — generating empty report.")

    logger.info(
        "Loaded: %d deactivation results, %d resource summaries, "
        "%d non-builders, %d builders, %d activity records",
        len(deactivation_results), len(resource_summaries),
        len(non_builders), len(builders), len(user_activity),
    )

    wb = Workbook()

    _build_summary_tab(wb, deactivation_results, non_builders, builders, resource_summaries)
    _build_non_builder_tab(wb, deactivation_results)
    _build_builder_tab(wb, deactivation_results, resource_summaries)
    _build_resource_tab(wb, resource_summaries, activity_map)
    _build_audit_tab(wb, deactivation_results)

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    output_path = OUTPUT_DIR / f"inactive_users_report_{date_str}.xlsx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))

    logger.info("Report saved -> %s", output_path)
    logger.info(
        "Tabs: Summary | Non-Builder — Deactivated | "
        "Builder — Manual Review | Resource Inventory | Audit Log"
    )
    logger.info("Done.")


if __name__ == "__main__":
    main()
