"""
Billing by Service — per-account, per-month AWS spend broken down by
service, matching what the AWS Billing Console's "Bills" -> "Charges by
service" view shows. Ported from the standalone `billing_by_service.py`
CLI script (Console-matching classification rules: EC2 Compute + EC2-Other
merged into one "Amazon Elastic Compute Cloud" row, Data Transfer usage
types folded into their own "Data Transfer" row except for Shield/
CloudFront, and AWS Marketplace items detected + annotated with their
Console-only "(sold by ...)" suffix) into a portal subpage/API instead of
a local CLI + Excel file.

Unlike the original script (which authenticated via named local AWS CLI
profiles `temp-<account_id>`), this module reuses the job portal's
existing cross-account access pattern from cost_explorer.py: Core account
Server (100000000001) is queried directly with its own static keys, and
every other account is reached via sts:AssumeRole(PortalReadOnlyRole) using
the Core account identity — no local AWS CLI profiles required in the
deployed container.

ENV=local: no real AWS calls are made — a small deterministic mock report
is returned instead (see _mock_report()), consistent with how the rest of
the Cost Analysis feature behaves in local/dev mode.
"""

from __future__ import annotations

import copy
import io
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from .cloud_account_store import LiveAccountMap
from .cost_explorer import (
    _client_for_account,
    _get_cost_and_usage_paginated,
    _core_session,
    _round2,
    _safe_float,
    CostAnalysisError,
)

logger = logging.getLogger(__name__)

ENV = os.environ.get("ENV", "local")

# Shield's and CloudFront's own data-transfer usage stays on their own
# service row instead of being folded into the shared "Data Transfer"
# bucket — matches the Console's own presentation for those two services.
DT_EXCLUDE_SERVICES = ("AWS Shield", "Amazon CloudFront")

# Live view of the cloud account registry (cloud_account_store), restricted to
# the "billing" scope: Billing by Service, RI/SP Savings, Security Hub, WAF
# Monitoring and AWS Resources. It used to be a hard-coded dict; the built-in
# seed (cloud_account_seed.AWS_SEED) holds the same 30 accounts — a superset of
# cost_explorer.ACCOUNTS (hr-system and three not-yet-named accounts are
# billing-only). Read-only Mapping: `in`, `.get`, `.items()`, `len` all work.
ACCOUNT_ALIAS = LiveAccountMap("name", scope="billing")

# Marketplace detection: substring match against known Marketplace
# product-name fragments/keywords (Cost Explorer's SERVICE dimension for
# Marketplace items doesn't carry a clean flag the way BILLING_ENTITY
# does for the main Cost Analysis tab — this mirrors the original
# script's heuristic exactly).
KNOWN_MARKETPLACE_KEYWORDS = [
    "sold by", "CIS Hardened", "MongoDB Atlas", "TrendAI",
    "Datadog", "WordPress", "Cyber Security Cloud",
]

# Marketplace items' Console-only "(sold by ...)" suffix — the Cost
# Explorer API omits this, but the Console shows it, so it's re-added here
# for exact display parity when a mapping is known.
MARKETPLACE_SOLD_BY: dict[str, str] = {
    "CIS Hardened Image Level 2 on Microsoft Windows Server 2016": "(sold by Center for Internet Security)",
    "CIS Hardened Image Level 2 on Microsoft Windows Server 2022": "(sold by Center for Internet Security)",
    "Cyber Security Cloud Managed Rules for AWS WAF Classic -OWASP Set-": "(sold by Cyber Security Cloud inc.)",
    "MongoDB Atlas (pay-as-you-go)": "(sold by MongoDB Inc)",
    "TrendAI\u2122 Cloud One": "(sold by Trend Micro)",
    "Datadog Pro": "(sold by Datadog Inc.)",
    "WordPress Certified by Bitnami and Automattic": "(sold by Bitnami)",
}


def is_marketplace(service_name: str) -> bool:
    return any(kw in service_name for kw in KNOWN_MARKETPLACE_KEYWORDS)


def short_name(service_name: str) -> str:
    """Convert a Cost Explorer SERVICE name to the short display name the
    Billing Console shows (strips the 'Amazon '/'AWS ' prefix; adds the
    known Marketplace '(sold by ...)' suffix instead)."""
    if is_marketplace(service_name):
        suffix = MARKETPLACE_SOLD_BY.get(service_name, "")
        return f"{service_name} {suffix}".strip() if suffix else service_name
    for prefix in ("Amazon ", "AWS ", "Amazon"):
        if service_name.startswith(prefix):
            return service_name[len(prefix):]
    return service_name


def is_data_transfer_usage(usage_type: str) -> bool:
    return (
        "DataTransfer" in usage_type
        or "AWS-Out-Bytes" in usage_type
        or "AWS-In-Bytes" in usage_type
    )


def list_accounts() -> list[dict]:
    """Account picker data for the frontend — id + alias, in the same
    fixed order ACCOUNT_ALIAS was declared (Core account first)."""
    return [{"account_id": acct_id, "alias": alias} for acct_id, alias in ACCOUNT_ALIAS.items()]


def _month_range(month: str) -> tuple[str, str]:
    """month is 'YYYY-MM' -> (first-of-month ISO date, first-of-next-month
    ISO date), the [Start, End) window GetCostAndUsage expects."""
    start = f"{month}-01"
    dt = datetime.strptime(start, "%Y-%m-%d")
    if dt.month == 12:
        end = f"{dt.year + 1}-01-01"
    else:
        end = f"{dt.year}-{dt.month + 1:02d}-01"
    return start, end


def month_label(month_str: str) -> str:
    """'2026-03' -> 'Mar-26', matching the original script's Excel column
    headers."""
    dt = datetime.strptime(f"{month_str}-01", "%Y-%m-%d")
    return dt.strftime("%b-%y")


def _fetch_month_service_costs(account_id: str, month: str, core_session) -> dict[str, float]:
    """One account, one month: {service_name: amount}, with EC2
    Compute+Other merged and Data Transfer usage types folded into their
    own row — matches the Billing Console's per-account monthly service
    breakdown exactly."""
    start, end = _month_range(month)
    ce = _client_for_account(account_id, "ce", core_session)
    buckets = _get_cost_and_usage_paginated(
        ce,
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
        ],
    )

    service_costs: dict[str, float] = {}
    data_transfer_total = 0.0
    for bucket in buckets:
        for group in bucket.get("Groups", []):
            keys = group.get("Keys", [])
            service = keys[0] if len(keys) > 0 else "Unknown"
            usage_type = keys[1] if len(keys) > 1 else ""
            amount = _safe_float(group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount"))
            if is_data_transfer_usage(usage_type) and service not in DT_EXCLUDE_SERVICES:
                data_transfer_total += amount
            elif "VpcPeering" in usage_type:
                service_costs["Amazon Virtual Private Cloud"] = service_costs.get("Amazon Virtual Private Cloud", 0.0) + amount
            else:
                service_costs[service] = service_costs.get(service, 0.0) + amount

    ec2_compute = service_costs.pop("Amazon Elastic Compute Cloud - Compute", 0.0)
    ec2_other = service_costs.pop("EC2 - Other", 0.0)
    if ec2_compute + ec2_other > 0:
        service_costs["Amazon Elastic Compute Cloud"] = ec2_compute + ec2_other
    if data_transfer_total > 0:
        service_costs["Data Transfer"] = data_transfer_total
    return service_costs


def _build_account_rows(all_costs_by_month: dict[str, dict[str, float]], months: list[str]) -> list[dict]:
    """Turn {month: {service: amount}} into the display row shape: one row
    per service (Web Services first alphabetically, then Marketplace
    alphabetically — matching the Excel sheet layout), each carrying its
    amount for every requested month."""
    all_services: set[str] = set()
    for costs in all_costs_by_month.values():
        all_services.update(costs.keys())

    web_services = sorted(s for s in all_services if not is_marketplace(s))
    marketplace = sorted(s for s in all_services if is_marketplace(s))

    rows = []
    for svc in web_services + marketplace:
        rows.append({
            "category": "Marketplace" if is_marketplace(svc) else "Web Services",
            "service": short_name(svc),
            "amounts": {m: _round2(all_costs_by_month.get(m, {}).get(svc, 0.0)) for m in months},
        })
    return rows


def _fetch_account_report(account_id: str, months: list[str], core_session) -> dict:
    row = {
        "account_id": account_id,
        "alias": ACCOUNT_ALIAS.get(account_id, account_id),
        "rows": [],
        "totals": {m: 0.0 for m in months},
        "error": None,
    }
    all_costs_by_month: dict[str, dict[str, float]] = {}
    try:
        for month in months:
            all_costs_by_month[month] = _fetch_month_service_costs(account_id, month, core_session)
    except Exception:
        logger.exception("billing_by_service: failed for account %s", account_id)
        row["error"] = "Could not fetch billing data for this account"
        return row

    row["rows"] = _build_account_rows(all_costs_by_month, months)
    row["totals"] = {m: _round2(sum(all_costs_by_month.get(m, {}).values())) for m in months}
    return row


# ── Cache (per account-set + month-set combination) ──────────────────────

_CACHE_TTL_SECONDS = 5 * 60  # 5 minutes — standardized across Cost Analysis,
# Billing by Service, AWS Health Dashboard, and Calendars (see cache_utils.py)
_cache_lock = threading.Lock()
_cache: dict = {}  # key -> {"report": ..., "fetched_at": monotonic()}


def _cache_key(account_ids: list[str], months: list[str]) -> tuple:
    return (tuple(sorted(account_ids)), tuple(months))


def reset_cache_for_tests() -> None:
    """Test-only helper: clear the process-local cache so each test starts
    from a clean slate. Not used by production code paths."""
    with _cache_lock:
        _cache.clear()


def _mock_report(account_ids: list[str], months: list[str]) -> dict:
    """ENV=local has no real cross-account Cost Explorer access — return a
    small deterministic mock report (random.Random(42), scoped locally so
    it never touches global `random` state) so the subpage still has
    something realistic to render, including a couple of Marketplace
    entries to exercise the category grouping/short_name/sold-by logic."""
    import random as _random

    rng = _random.Random(42)
    MOCK_WEB_SERVICES = [
        "Amazon Elastic Compute Cloud - Compute", "EC2 - Other",
        "Amazon Relational Database Service", "Amazon Simple Storage Service",
        "AWS Lambda", "Amazon CloudFront", "AWS Key Management Service",
        "Amazon CloudWatch", "Amazon Route 53", "Elastic Load Balancing",
        "Amazon DynamoDB", "AWS Backup",
    ]
    MOCK_MARKETPLACE_SERVICES = ["Datadog Pro", "MongoDB Atlas (pay-as-you-go)"]

    accounts_out = []
    for acct_id in account_ids:
        all_costs_by_month: dict[str, dict[str, float]] = {}
        for month in months:
            costs: dict[str, float] = {}
            for svc in MOCK_WEB_SERVICES:
                if rng.random() < 0.85:  # most services show up most months
                    costs[svc] = round(rng.uniform(1.0, 800.0), 2)
            for svc in MOCK_MARKETPLACE_SERVICES:
                if rng.random() < 0.4:
                    costs[svc] = round(rng.uniform(5.0, 150.0), 2)
            all_costs_by_month[month] = costs
        accounts_out.append({
            "account_id": acct_id,
            "alias": ACCOUNT_ALIAS.get(acct_id, acct_id),
            "rows": _build_account_rows(all_costs_by_month, months),
            "totals": {m: _round2(sum(all_costs_by_month.get(m, {}).values())) for m in months},
            "error": None,
        })

    return {
        "months": months,
        "month_labels": [month_label(m) for m in months],
        "accounts": accounts_out,
        "is_mock": True,
        "as_of": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cache_age_seconds": 0,
    }


def get_billing_report(account_ids: list[str], months: list[str], force_refresh: bool = False) -> dict:
    """Fetch (or serve cached) per-account, per-month service billing for
    every account_id in `account_ids` across every month in `months`
    ('YYYY-MM' strings). Cached per exact (accounts, months) combination
    for _CACHE_TTL_SECONDS, mirroring cost_explorer.get_organization_cost_summary()'s
    caching approach (deep-copies on read so callers can never mutate
    cache state).
    """
    if not account_ids:
        raise CostAnalysisError("At least one account_id is required")
    if not months:
        raise CostAnalysisError("At least one month is required")

    if ENV == "local":
        return _mock_report(account_ids, months)

    key = _cache_key(account_ids, months)
    with _cache_lock:
        cached = _cache.get(key)
        if not force_refresh and cached is not None:
            age = time.monotonic() - cached["fetched_at"]
            if age < _CACHE_TTL_SECONDS:
                report = copy.deepcopy(cached["report"])
                report["cache_age_seconds"] = int(age)
                return report

    core_session = _core_session()
    accounts_out: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_account_report, acct_id, months, core_session): acct_id
            for acct_id in account_ids
        }
        for fut in as_completed(futures):
            accounts_out.append(fut.result())
    accounts_out.sort(key=lambda a: account_ids.index(a["account_id"]))

    report = {
        "months": months,
        "month_labels": [month_label(m) for m in months],
        "accounts": accounts_out,
        "is_mock": False,
        "as_of": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cache_age_seconds": 0,
    }

    with _cache_lock:
        _cache[key] = {"report": copy.deepcopy(report), "fetched_at": time.monotonic()}

    return report


def build_workbook(report: dict):
    """Build an openpyxl Workbook from a get_billing_report() result, one
    sheet per account, in exactly the original script's layout:
    header row [alias, 'item', month1, month2, ...], one row per service
    (category in col A, short service name in col B, amount per month),
    then a 'Grand total' row. Sheet name is the account alias (truncated
    to Excel's 31-character sheet-name limit)."""
    from openpyxl import Workbook

    months = report["months"]
    month_labels = report["month_labels"]

    wb = Workbook()
    wb.remove(wb.active)
    for account in report["accounts"]:
        alias = account["alias"]
        ws = wb.create_sheet(title=alias[:31])
        ws.append([alias, "item"] + month_labels)
        if account.get("error"):
            ws.append(["", f"ERROR: {account['error']}"])
            continue
        for row in account["rows"]:
            ws.append([row["category"], row["service"]] + [row["amounts"].get(m, 0.0) for m in months])
        last_row = ws.max_row
        ws.append(["", "Grand total"] + [0.0 for _ in months])
        grand_row = ws.max_row
        # Grand Total is always a live =SUM() formula (not a pre-computed
        # static number) so the sheet keeps auto-summing if a value above
        # it is edited by hand after export — matches the master
        # template's own "auto sum" rows.
        for col_idx in range(3, 3 + len(months)):
            col_letter = ws.cell(row=1, column=col_idx).column_letter
            ws.cell(row=grand_row, column=col_idx).value = f"=SUM({col_letter}2:{col_letter}{last_row})"
    return wb


def workbook_to_bytes(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Merge-into-template export ────────────────────────────────────────────
# Auto-fills a get_billing_report() result into the user's own multi-tab
# "AWS Cost Raw data" master workbook (one tab per AWS account, each with
# a header row of real date cells + per-service rows + a Grand Total row,
# plus a set of manually-authored "helper" columns to the right of the
# month columns — a blank spacer, two month-over-month comparison columns,
# and a small VLOOKUP staging area the user pastes new numbers into by
# hand today). Unlike build_workbook() above (which always creates a
# brand-new plain workbook from scratch), this loads the user's existing
# template bytes, updates only the account/month/service cells that need
# updating, appends new rows for services not already on a sheet and a new
# month column if the report includes a month the template doesn't have
# yet, and leaves every other cell (including the manual helper columns
# and any notes typed into them) completely untouched.
#
# Row insertion is deliberately restricted to "append new service rows
# directly above the Grand Total row" rather than inserting them in
# strict alphabetical position: openpyxl does not rewrite formula TEXT
# when rows shift (only cell coordinates), so inserting a row ABOVE
# existing rows would silently break every affected row's own
# self-referencing helper formulas (e.g. "Mar vs Jan" = H<row>-F<row>).
# Only ever inserting at the very bottom of the data block means no
# existing row's row-number changes, so no existing formula can go stale
# — only the Grand Total row's own formulas (SUM ranges + its own two
# comparison-column cells) need fixing up afterward, which this code does
# explicitly.
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "billing_by_service_template.xlsx")

_ACCOUNT_ID_SHEET_RE = re.compile(r"^(\d{9,12})-")
_STAGING_RANGE_RE = re.compile(r"\$[A-Za-z]{1,3}:\$[A-Za-z]{1,3}")


def _sheet_account_id(sheet_title: str) -> Optional[str]:
    """'100000000001-core' -> '100000000001'. Matching is done by this
    account-id prefix, never by the alias text after the dash — several
    sheets in the real template have alias spellings/truncations that
    don't exactly match ACCOUNT_ALIAS (manually retyped by the user at
    some point), so the account id is the only reliable join key."""
    m = _ACCOUNT_ID_SHEET_RE.match(sheet_title or "")
    return m.group(1) if m else None


def _normalize_key(category: str, service: str) -> tuple:
    return (
        " ".join((category or "").split()).strip().lower(),
        " ".join((service or "").split()).strip().lower(),
    )


def _find_month_columns(ws) -> dict:
    """Row 1, scanning from column C: every real datetime cell is a month
    column. Stops at the first non-datetime cell — the blank spacer column
    that separates the month columns from the user's helper/staging area —
    so it never mistakes anything past that boundary for a month."""
    months: dict = {}
    col = 3
    while True:
        val = ws.cell(row=1, column=col).value
        if not isinstance(val, datetime):
            break
        months[val.strftime("%Y-%m")] = col
        col += 1
    return months


def _find_grand_total_row(ws) -> Optional[int]:
    for r in range(2, ws.max_row + 2):
        val = ws.cell(row=r, column=2).value
        if val and str(val).strip().lower() == "grand total":
            return r
    return None


def _copy_cell_style(src_cell, dst_cell) -> None:
    if src_cell.has_style:
        dst_cell.font = copy.copy(src_cell.font)
        dst_cell.border = copy.copy(src_cell.border)
        dst_cell.fill = copy.copy(src_cell.fill)
        dst_cell.number_format = src_cell.number_format
        dst_cell.protection = copy.copy(src_cell.protection)
        dst_cell.alignment = copy.copy(src_cell.alignment)


def _find_header_col(ws, text: str, row: int = 1) -> Optional[int]:
    for c in range(1, ws.max_column + 1):
        if ws.cell(row=row, column=c).value == text:
            return c
    return None


def _fixup_staging_column_refs(ws) -> None:
    """After a month column is inserted, the sheet's small VLOOKUP
    staging area (S/T/U/V columns the user manually pastes new numbers
    into) shifts right, but openpyxl doesn't rewrite whole-column
    absolute-range formula text like '$T:$V' when a column is inserted —
    only the cell positions move, not the string inside them. Locate the
    staging block by its header text (there are exactly two "item"
    headers in row 1: one for the main table at column B, one for the
    staging block) and the two helper-formula columns by their own header
    text ('Function↓' / 'Paste New Data'), then rewrite any stale
    '$<col>:$<col>' range in those two columns' formulas to point at the
    staging block's current position. No-op if this sheet doesn't have
    this helper area at all."""
    func_col = _find_header_col(ws, "Function\u2193")
    paste_col = _find_header_col(ws, "Paste New Data")
    item_cols = [c for c in range(1, ws.max_column + 1) if ws.cell(row=1, column=c).value == "item"]
    if func_col is None or paste_col is None or len(item_cols) < 2:
        return
    staging_t_col = item_cols[-1]
    staging_v_col = staging_t_col + 2
    from openpyxl.utils import get_column_letter

    new_range = f"${get_column_letter(staging_t_col)}:${get_column_letter(staging_v_col)}"
    for col in (func_col, paste_col):
        for r in range(1, ws.max_row + 1):
            cell = ws.cell(row=r, column=col)
            if isinstance(cell.value, str) and cell.value.startswith("=") and _STAGING_RANGE_RE.search(cell.value):
                cell.value = _STAGING_RANGE_RE.sub(new_range, cell.value)


def _insert_month_column(ws, month_cols: dict, month: str) -> int:
    """Insert one new month column immediately after the last existing
    month column (i.e. still before the blank spacer / helper-columns
    area, so nothing about the helper area's own internal layout needs to
    move) and mutate `month_cols` in place. Returns the new column index."""
    from openpyxl.utils import get_column_letter

    last_col = max(month_cols.values()) if month_cols else 2
    insert_at = last_col + 1
    ws.insert_cols(insert_at, amount=1)

    header_cell = ws.cell(row=1, column=insert_at)
    header_cell.value = datetime.strptime(f"{month}-01", "%Y-%m-%d")
    sample_col = next(iter(month_cols.values())) if month_cols else None
    header_cell.number_format = (
        ws.cell(row=1, column=sample_col).number_format if sample_col else "[$-409]mmm\\-yy;@"
    )
    if sample_col:
        _copy_cell_style(ws.cell(row=1, column=sample_col), header_cell)
        header_cell.value = datetime.strptime(f"{month}-01", "%Y-%m-%d")  # style copy may reset value's cached type

    new_letter = get_column_letter(insert_at)
    if sample_col:
        prev_letter = get_column_letter(sample_col)
        prev_dim = ws.column_dimensions.get(prev_letter)
        if prev_dim is not None and prev_dim.width:
            ws.column_dimensions[new_letter].width = prev_dim.width
        for r in range(2, ws.max_row + 1):
            src = ws.cell(row=r, column=sample_col)
            dst = ws.cell(row=r, column=insert_at)
            _copy_cell_style(src, dst)
            dst.value = 0

    _fixup_staging_column_refs(ws)
    month_cols[month] = insert_at
    return insert_at


def _fixup_grand_total(ws, grand_total_row: int, month_cols: dict) -> None:
    """Ensure the Grand Total row always auto-sums: every month column's
    Grand Total cell is (re)written as a live =SUM() formula covering
    every data row up to (new) grand_total_row - 1, regardless of
    whether that cell previously held a formula or a hand-typed static
    number — every export must "auto sum" the Grand Total, per the
    template's intended behaviour, not just the sheets that already
    happened to have a formula there. Also re-points the Grand Total
    row's own two comparison-column formulas (the per-row "Mar vs Jan" /
    "Mar vs Oct" style cells, which also exist on the Grand Total row
    itself in the real template) at the Grand Total row's current row
    number."""
    last_data_row = grand_total_row - 1
    for col in month_cols.values():
        cell = ws.cell(row=grand_total_row, column=col)
        col_letter = cell.column_letter
        cell.value = f"=SUM({col_letter}2:{col_letter}{last_data_row})"

    for col in range(1, ws.max_column + 1):
        if col in month_cols.values():
            continue
        cell = ws.cell(row=grand_total_row, column=col)
        if isinstance(cell.value, str) and cell.value.startswith("=") and re.fullmatch(r"=[A-Za-z]+\d+-[A-Za-z]+\d+", cell.value):
            cell.value = re.sub(r"(\d+)", str(grand_total_row), cell.value)


def _build_new_sheet(wb, account_id: str, alias: str, months: list[str], month_labels: list[str], rows: list[dict], totals: dict):
    """No existing sheet matched this account_id at all (a brand new AWS
    account not yet in the user's template) — create one from scratch with
    the same plain header/Grand-total layout build_workbook() uses (no
    helper columns to preserve, since there's nothing to copy from)."""
    ws = wb.create_sheet(title=f"{account_id}-{alias}"[:31])
    ws.append([alias, "item"] + month_labels)
    for row in rows:
        ws.append([row["category"], row["service"]] + [row["amounts"].get(m, 0.0) for m in months])
    last_row = ws.max_row
    ws.append(["", "Grand total"] + [totals.get(m, 0.0) for m in months])
    grand_row = ws.max_row
    for col_idx, m in enumerate(months, start=3):
        col_letter = ws.cell(row=1, column=col_idx).column_letter
        ws.cell(row=grand_row, column=col_idx).value = f"=SUM({col_letter}2:{col_letter}{last_row})"
    return ws


def merge_report_into_template(report: dict, template_bytes: bytes) -> bytes:
    """Load the user's existing multi-tab template workbook and write this
    report's numbers into it in place, following the template's own
    existing format exactly:
      - each account maps to a sheet by account_id (parsed from the sheet
        name), not by alias text (aliases in the live template don't all
        match ACCOUNT_ALIAS exactly — see _sheet_account_id());
      - each report month maps to an existing date-typed header cell, or
        gets a new month column appended right after the last existing one
        if the template doesn't have that month yet;
      - each report row maps to an existing service row (category+service,
        case/whitespace-insensitive) and just has its amount cell(s)
        updated, or — if no existing row matches — gets a brand new row
        appended directly above the Grand Total row (see the module
        docstring above for why rows are always appended there rather than
        inserted in strict alphabetical order);
      - the Grand Total row's SUM() formulas are extended to cover any
        newly appended rows;
      - accounts with no matching sheet at all get a new sheet created;
      - every other cell — the blank spacer column, the two manual
        month-comparison columns, any typed notes, and the VLOOKUP
        staging area — is left completely untouched (aside from shifting
        along with column/row inserts, which openpyxl handles for cell
        positions automatically).
    Returns the updated workbook as .xlsx bytes.
    """
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    wb = load_workbook(io.BytesIO(template_bytes), data_only=False)

    months = report["months"]
    month_labels = report["month_labels"]
    month_label_by_month = dict(zip(months, month_labels))

    sheet_by_account: dict = {}
    for title in wb.sheetnames:
        acct = _sheet_account_id(title)
        if acct:
            sheet_by_account[acct] = wb[title]

    for account in report["accounts"]:
        account_id = account["account_id"]
        if account.get("error"):
            continue  # never overwrite a sheet with a failed fetch's absence of data

        alias = account.get("alias", account_id)
        ws = sheet_by_account.get(account_id)

        if ws is None:
            _build_new_sheet(wb, account_id, alias, months, month_labels, account["rows"], account.get("totals", {}))
            continue

        month_cols = _find_month_columns(ws)
        for m in months:
            if m not in month_cols:
                _insert_month_column(ws, month_cols, m)
                ws.cell(row=1, column=month_cols[m]).value = datetime.strptime(f"{m}-01", "%Y-%m-%d")

        grand_total_row = _find_grand_total_row(ws)
        if grand_total_row is None:
            # Sheet has no Grand Total row at all (shouldn't happen in the
            # real template, but keep this safe for a hand-edited/blank
            # sheet) — append one right after the current last row.
            grand_total_row = ws.max_row + 1
            ws.cell(row=grand_total_row, column=2).value = "Grand total"

        existing_rows: dict = {}
        for r in range(2, grand_total_row):
            cat = ws.cell(row=r, column=1).value
            svc = ws.cell(row=r, column=2).value
            if svc is None:
                continue
            existing_rows[_normalize_key(cat, svc)] = r

        new_items = []
        for row in account["rows"]:
            key = _normalize_key(row["category"], row["service"])
            target_row = existing_rows.get(key)
            if target_row is not None:
                for m in months:
                    col = month_cols.get(m)
                    if col is None:
                        continue
                    ws.cell(row=target_row, column=col).value = row["amounts"].get(m, 0.0)
            else:
                new_items.append(row)

        if new_items:
            style_source_row = grand_total_row - 1 if grand_total_row > 2 else 2
            n_new = len(new_items)
            ws.insert_rows(grand_total_row, amount=n_new)
            for i, row in enumerate(new_items):
                r = grand_total_row + i
                for col in range(1, ws.max_column + 1):
                    _copy_cell_style(ws.cell(row=style_source_row, column=col), ws.cell(row=r, column=col))
                ws.cell(row=r, column=1).value = row["category"]
                ws.cell(row=r, column=2).value = row["service"]
                for m, col in month_cols.items():
                    ws.cell(row=r, column=col).value = row["amounts"].get(m, 0.0)
            grand_total_row += n_new

        _fixup_grand_total(ws, grand_total_row, month_cols)

    return workbook_to_bytes(wb)


def load_template_bytes() -> bytes:
    """Read the checked-in master template file
    (app/templates/billing_by_service_template.xlsx). Raises
    FileNotFoundError if it hasn't been added to the repo yet."""
    with open(TEMPLATE_PATH, "rb") as f:
        return f.read()
