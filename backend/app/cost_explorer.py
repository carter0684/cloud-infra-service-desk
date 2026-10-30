"""
AWS Cost Analysis integration for the Cloud Infra Job Portal.

Fetches AWS Cost Explorer (month-to-date and previous-month) spend across
the whole AWS Organization:

  - Core account (100000000001): queried directly using a raw IAM
    user's static access keys (portal-core-admin), stored in Secrets
    Manager — same "long-lived cross-account keys" pattern already used
    by escalation.py for the AMS support account.
  - All other member accounts: queried by using those same Core account
    credentials to sts:AssumeRole into each account's "PortalReadOnlyRole",
    then calling Cost Explorer with the resulting temporary credentials.

Results are cached in-process for a short TTL (Cost Explorer's
GetCostAndUsage API is billed per call beyond a small free tier, and 24
accounts x every page load would add up quickly).

Which accounts are covered is no longer hard-coded here: ACCOUNTS and ACCOUNT_BU are
live views of the cloud account registry (cloud_account_store.py), managed by Admins on
the Cloud Accounts page. Legacy accounts are still reached through Core account keys and
PortalReadOnlyRole (below); accounts onboarded from that page use their own InfraOps role
(cloud_account_access.py).
"""

from __future__ import annotations

import copy
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

import boto3

from . import org_config
from .cloud_account_store import LiveAccountMap

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

CORE_ACCOUNT_ID = org_config.CORE_ACCOUNT_ID
CORE_CREDENTIALS_SECRET_NAME = os.environ.get(
    "CORE_CREDENTIALS_SECRET_NAME", "job-portal/core-admin-credentials"
)
READONLY_ROLE_NAME = os.environ.get("READONLY_ROLE_NAME", "PortalReadOnlyRole")
# Core account itself is queried directly (no assume-role needed);
# every other legacy account is reached via READONLY_ROLE_NAME.
#
# Live viewer account is reached via READONLY_ROLE_NAME.
# Live view of the cloud account registry (cloud_account_store), restricted to
# the "core" scope: the accounts Cost Analysis, CMDB, AWS Health member
# accounts, VPN Lookup names and cost-adjustment validation cover. It used to
# be a hard-coded dict; the built-in seed (cloud_account_seed.AWS_SEED) holds
# the same 26 accounts, and Admins now manage the list on the Cloud Accounts
# page. Read-only Mapping: `in`, `.get`, `.items()`, `len` all work, and every
# lookup sees the current registry, so no importer keeps a stale copy.
ACCOUNTS = LiveAccountMap("name", scope="core")

# AWS account ID -> Business Unit, sourced from the finance team's
# "AWS Actual Expenses" tracking workbook ("0A - AWS Accounts" sheet).
# Used to group/filter the Cost Analysis table the same way finance does
# (BU-A / BU-D / BU-B / BU-C / BU-E), and to produce BU subtotal
# rows. Unmapped/unknown accounts fall back to "Other" (see
# ACCOUNT_BU.get() call sites) rather than raising.
# Live view of each account's Business Unit from the registry (same source as
# above; unmapped accounts are "Other", which is also what .get(id, "Other")
# call sites fall back to).
ACCOUNT_BU = LiveAccountMap("bu", scope=None)

# Cost Explorer SERVICE names that should be bucketed as "Marketplace" /
# "Shield" / "Support" spend (mirroring the finance workbook's per-account
# monthly breakdown columns: AWS Usage except MP / AWS Marketplace / AWS
# Shield / AWS Enterprise Support / Credits). Matched by prefix/substring
# against the exact SERVICE dimension value Cost Explorer returns.
# BILLING_ENTITY == "AWS Marketplace" (when available) takes priority
# over the SERVICE-name heuristic since it's the authoritative signal
# AWS itself uses to route Marketplace charges.
_MARKETPLACE_SERVICE_PREFIX = "AWS Marketplace"
_MARKETPLACE_BILLING_ENTITY = "AWS Marketplace"
_SHIELD_SERVICE_NAME = "AWS Shield"
_SUPPORT_SERVICE_SUBSTRINGS = ("Support",)  # matches "AWS Support (Business)", "AWS Premium Support", etc.

# RECORD_TYPE values Cost Explorer can return. "normal" = actual usage
# charges (kept as-is, including DiscountedUsage/SavingsPlanNegation so
# discounted/SP-covered usage isn't dropped from the usage total).
# "adjustment" = credits/refunds/discounts that should be isolated into
# the `credits` bucket. The two sets are disjoint by construction so a
# single UnblendedCost amount is never counted in both buckets.
_ADJUSTMENT_RECORD_TYPES = ("Credit", "Refund", "Discount", "RIFee", "SavingsPlanRecurringFee", "SavingsPlanUpfrontFee")
_NORMAL_RECORD_TYPE_EXCLUDE_FILTER = {
    "Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": list(_ADJUSTMENT_RECORD_TYPES)}}
}

_CACHE_TTL_SECONDS = 5 * 60  # 5 minutes — standardized across Cost Analysis,
# Billing by Service, AWS Health Dashboard, and Calendars (see cache_utils.py)
_cache_lock = threading.Lock()
# "summary": last good summary (deep-copied on write/read so callers can
# never mutate cache state). "fetched_at": monotonic() timestamp of that
# summary. "last_good": most recent summary that had zero account errors,
# kept as a fallback so a transient AWS blip doesn't blank the dashboard.
# "fetch_in_progress": single-flight guard — while a refresh is already
# running, concurrent callers wait on the same result instead of firing
# duplicate Cost Explorer calls.
_cache: dict = {"summary": None, "fetched_at": 0.0, "last_good": None, "last_good_at": 0.0}
_fetch_in_progress: "threading.Event | None" = None


class CostAnalysisError(Exception):
    """Raised for org-wide-summary-level failures (e.g. cannot obtain the
    Core account credentials at all). Per-account failures are instead
    reported inline in that account's result row so one bad account
    doesn't take down the whole dashboard."""


# ── Numeric safety helpers ────────────────────────────────────────────────

def _safe_float(value, default: float = 0.0) -> float:
    """Coerce `value` to a finite float, falling back to `default` for
    None, NaN, +/-Infinity, or anything that can't be parsed as a number.
    AWS Cost Explorer amounts are decimal strings, but this guards against
    malformed responses and mock/test data alike."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _cents(value) -> Decimal:
    """Quantize a monetary amount to cent precision using Decimal, to
    avoid binary-float rounding drift when summing many small per-service
    amounts across pages/accounts."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        d = Decimal("0")
    if not d.is_finite():
        d = Decimal("0")
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _round2(value) -> float:
    return float(_cents(value))


def _get_secret(secret_name: str) -> str:
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]


def _get_core_credentials() -> dict:
    """Static IAM keys for account 100000000001 (portal-core-admin).

    Local-dev convenience: if CORE_ACCESS_KEY_ID / CORE_SECRET_ACCESS_KEY
    env vars are set, use those directly (no Secrets Manager round trip
    needed on a developer laptop). Otherwise fetch the same two values
    from Secrets Manager (the path used in the deployed container).
    """
    env_key = os.environ.get("CORE_ACCESS_KEY_ID")
    env_secret = os.environ.get("CORE_SECRET_ACCESS_KEY")
    if env_key and env_secret:
        return {"aws_access_key_id": env_key, "aws_secret_access_key": env_secret}

    import json

    try:
        secret_str = _get_secret(CORE_CREDENTIALS_SECRET_NAME)
        creds = json.loads(secret_str)
    except Exception as e:
        raise CostAnalysisError(f"Could not load Core account credentials: {e}") from e
    if "aws_access_key_id" not in creds or "aws_secret_access_key" not in creds:
        raise CostAnalysisError("Core account credentials secret is missing required keys")
    return creds


def _core_session() -> boto3.Session:
    creds = _get_core_credentials()
    return boto3.Session(
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        region_name="us-east-1",
    )


def _client_for_account(account_id: str, service: str, core_session: boto3.Session):
    """Return a boto3 client for `service` scoped to `account_id`.

    Core account (100000000001) is queried directly with its own raw
    keys. Every other account is reached via AssumeRole(PortalReadOnlyRole)
    using the Core account identity as the calling principal.
    """
    # Accounts onboarded through the Cloud Accounts page are reached with the Portal's
    # own role + the account's External_Id; every legacy account falls through to the
    # unchanged code below.
    from . import cloud_account_access, cloud_account_store

    if cloud_account_store.access_mode(account_id) == "infraops":
        return cloud_account_access.client(account_id, service, region_name="us-east-1")
    if account_id == CORE_ACCOUNT_ID:
        return core_session.client(service, region_name="us-east-1")

    sts = core_session.client("sts", region_name="us-east-1")
    role_arn = f"arn:aws:iam::{account_id}:role/{READONLY_ROLE_NAME}"
    assumed = sts.assume_role(RoleArn=role_arn, RoleSessionName="job-portal-cost-analysis")
    creds = assumed["Credentials"]
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _add_months(d: date, delta: int) -> date:
    """Return the 1st of the month `delta` months away from `d` (delta may
    be negative). Used purely for month arithmetic on the 1st-of-month
    boundaries this module works with, so no calendar edge cases (day
    overflow) ever arise."""
    month_index = d.month - 1 + delta
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _month_bounds() -> tuple[str, str, str, str]:
    """Return (two_months_ago_start, prev_month_start, mtd_start, tomorrow)
    as ISO date strings. A single GetCostAndUsage call spanning
    two_months_ago_start..tomorrow with MONTHLY granularity returns
    exactly three buckets: the month before last (used as the trend
    baseline), last full month, and the current month's month-to-date
    total — one API call per account instead of three."""
    today = date.today()
    mtd_start = today.replace(day=1)
    tomorrow = today + timedelta(days=1)
    prev_start = _add_months(mtd_start, -1)
    two_months_ago_start = _add_months(mtd_start, -2)
    return two_months_ago_start.isoformat(), prev_start.isoformat(), mtd_start.isoformat(), tomorrow.isoformat()


def _pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    """% change of `new` vs `old`, matching how finance/management usually
    read a cost trend (positive = spend went up, negative = went down).

    Returns None (i.e. "not comparable") whenever there is no meaningful
    baseline to compare against: `old`/`new` missing or non-finite, or
    `old` is zero or negative (a negative baseline — e.g. a month that net
    reconciled into a credit — makes a percentage change directionally
    misleading, so we deliberately don't compute one)."""
    if old is None or new is None:
        return None
    old_f = _safe_float(old, default=math.nan)
    new_f = _safe_float(new, default=math.nan)
    if math.isnan(old_f) or math.isnan(new_f) or old_f <= 0:
        return None
    return round((new_f - old_f) / old_f * 100, 1)


# Number of individual AWS services kept per account/org before the
# remainder is folded into a single "Other services" bucket — keeps the
# drill-down modal and org-wide chart readable instead of listing 50+ tiny
# line items.
_TOP_SERVICES_PER_ACCOUNT = 10
_TOP_SERVICES_ORG_WIDE = 12


def _empty_breakdown() -> dict:
    """One period's cost split into the same buckets the finance team's
    'AWS Actual Expenses' workbook tracks per account per month: usage,
    marketplace, shield, support, credits (negative), a manual
    `adjustments` bucket for admin-entered extra fees/corrections (see
    apply_manual_adjustments() below — 0.0 until adjustments are merged
    in), and the resulting subtotal (= usage + marketplace + shield +
    support + credits + adjustments)."""
    return {
        "usage": 0.0, "marketplace": 0.0, "shield": 0.0, "support": 0.0,
        "credits": 0.0, "adjustments": 0.0, "subtotal": 0.0,
    }


def period_key_for(period_name: str) -> str:
    """Map a summary period name ("mtd" / "last_month" / "two_months_ago")
    to the "YYYY-MM" calendar-month string manual adjustments are keyed
    by, based on *today's* date — matching exactly which calendar months
    _month_bounds() currently considers MTD/last-month/two-months-ago.
    Kept as its own function (not inlined) so main.py's adjustment CRUD
    endpoints can validate/default a `period` against the same mapping
    the merge step uses, instead of duplicating month arithmetic."""
    two_ago_start, prev_start, mtd_start, _tomorrow = _month_bounds()
    mapping = {"mtd": mtd_start[:7], "last_month": prev_start[:7], "two_months_ago": two_ago_start[:7]}
    return mapping.get(period_name, mtd_start[:7])


def apply_manual_adjustments(summary: dict, adjustments: list[dict]) -> dict:
    """Layer admin-entered manual cost adjustments onto an already-built
    organization cost summary, purely additively:

      - Each adjustment (account_id, period="YYYY-MM", amount) is matched
        against whichever of that account's mtd/last_month/two_months_ago
        buckets currently maps to that calendar month (via
        period_key_for()). Adjustments for a month outside that 3-month
        window are ignored for display (still safely stored, just not
        currently shown — the window rolls forward every month).
      - The matched bucket's `adjustments` field and `subtotal` are
        updated; `usage`/`marketplace`/`shield`/`support`/`credits` (the
        real AWS-reported numbers) are NEVER modified.
      - Account-level and org-level roll-up fields (mtd_cost,
        last_month_cost, total_mtd_cost, business_units, pct-change
        fields, etc.) are recomputed from the adjusted subtotals so the
        whole summary stays internally consistent, exactly as if the
        adjustment had been part of the original AWS data.
      - Returns a new dict; the input `summary` is never mutated in
        place, so callers holding a reference to the pre-adjustment
        summary (e.g. the process-local cache) are unaffected.
    """
    import copy as _copy

    result = _copy.deepcopy(summary)
    by_account: dict[str, dict] = {a["account_id"]: a for a in result.get("accounts") or []}

    period_names = ("mtd", "last_month", "two_months_ago")
    period_month_map = {p: period_key_for(p) for p in period_names}

    for adj in adjustments or []:
        account = by_account.get(adj.get("account_id"))
        if account is None:
            continue
        adj_period = adj.get("period")
        matched_period_name = next((p for p, month in period_month_map.items() if month == adj_period), None)
        if matched_period_name is None:
            continue
        bucket = account.get(matched_period_name)
        if not isinstance(bucket, dict):
            continue
        amount = _safe_float(adj.get("amount"))
        bucket["adjustments"] = _round2(bucket.get("adjustments", 0.0) + amount)
        bucket["subtotal"] = _round2(
            bucket.get("usage", 0.0) + bucket.get("marketplace", 0.0) + bucket.get("shield", 0.0)
            + bucket.get("support", 0.0) + bucket.get("credits", 0.0) + bucket["adjustments"]
        )

    # Recompute every roll-up field from the (possibly adjusted) per-account
    # subtotals, reusing the exact same aggregation logic _build_summary()
    # uses so behaviour never silently diverges between "no adjustments"
    # and "some adjustments" responses.
    accounts_list = list(by_account.values())
    for a in accounts_list:
        a["mtd_cost"] = a["mtd"]["subtotal"]
        a["last_month_cost"] = a["last_month"]["subtotal"]
        a["mtd_vs_last_month_pct"] = _pct_change(a["mtd_cost"], a["last_month_cost"])
        a["last_month_vs_prior_pct"] = _pct_change(a["last_month_cost"], a["two_months_ago"]["subtotal"])

    rebuilt = _build_summary(accounts_list)
    # _build_summary() strips '_full_*' keys and recomputes top_services_*
    # from them, which no longer exist on already-stripped account rows —
    # preserve the original top_services_*/business_units-derived service
    # lists untouched (manual adjustments never change per-service
    # breakdowns, only the account/BU/org subtotal totals).
    rebuilt["top_services_mtd"] = result.get("top_services_mtd", rebuilt["top_services_mtd"])
    rebuilt["top_services_last_month"] = result.get("top_services_last_month", rebuilt["top_services_last_month"])
    rebuilt["as_of"] = result.get("as_of", rebuilt["as_of"])
    rebuilt["cache_age_seconds"] = result.get("cache_age_seconds", 0)
    for passthrough_key in ("is_mock", "stale_fallback"):
        if passthrough_key in result:
            rebuilt[passthrough_key] = result[passthrough_key]
    return rebuilt


def _classify_service(service_name: str, billing_entity: Optional[str] = None) -> str:
    """Map a Cost Explorer SERVICE (+ optional BILLING_ENTITY) dimension
    value to one of the finance workbook's breakdown buckets (usage /
    marketplace / shield / support).

    BILLING_ENTITY == "AWS Marketplace" is authoritative when present —
    it's the field AWS itself uses to route Marketplace billing, and is
    more reliable than pattern-matching the SERVICE string (some
    Marketplace line items don't start with "AWS Marketplace"). We only
    fall back to the SERVICE-name heuristic when BILLING_ENTITY wasn't
    requested/returned for this group, never overriding it with a guess
    when it *was* returned and says otherwise."""
    service_name = service_name or ""
    if billing_entity == _MARKETPLACE_BILLING_ENTITY:
        return "marketplace"
    if billing_entity is None and service_name.startswith(_MARKETPLACE_SERVICE_PREFIX):
        return "marketplace"
    if service_name == _SHIELD_SERVICE_NAME:
        return "shield"
    if any(s in service_name for s in _SUPPORT_SERVICE_SUBSTRINGS):
        return "support"
    return "usage"


# ── Paginated Cost Explorer retrieval ────────────────────────────────────

def _get_cost_and_usage_paginated(ce, **kwargs) -> list[dict]:
    """Call ce.get_cost_and_usage(**kwargs) and follow NextPageToken until
    exhausted, returning the concatenated ResultsByTime across all pages.

    Guards against a malformed/looping NextPageToken (seen occasionally in
    mocked/misbehaving clients) by tracking tokens already consumed and
    stopping if one repeats, plus a hard page-count ceiling — rather than
    looping forever."""
    all_results: list[dict] = []
    seen_tokens: set[str] = set()
    token = None
    max_pages = 50
    for _ in range(max_pages):
        call_kwargs = dict(kwargs)
        if token:
            call_kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**call_kwargs)
        all_results.extend(resp.get("ResultsByTime", []))
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
        if next_token in seen_tokens:
            logger.warning("cost_explorer: repeated NextPageToken detected, stopping pagination")
            break
        seen_tokens.add(next_token)
        token = next_token
    return all_results


def _merge_results_by_time(results: list[dict]) -> dict[str, dict]:
    """Merge possibly-repeated ResultsByTime buckets (same TimePeriod.Start
    appearing across multiple pages) into one dict keyed by period start,
    with Groups merged by their dimension key tuple and Total merged by
    metric. Cost Explorer normally returns each period once per page, but
    this makes merging correct even if that ever changes."""
    merged: dict[str, dict] = {}
    for bucket in results:
        start = bucket.get("TimePeriod", {}).get("Start")
        if start is None:
            continue
        target = merged.setdefault(start, {"groups": {}, "total": {}})
        for group in bucket.get("Groups", []):
            key = tuple(group.get("Keys", []))
            gtarget = target["groups"].setdefault(key, {})
            for metric_name, metric in group.get("Metrics", {}).items():
                amount = _safe_float(metric.get("Amount"))
                unit = metric.get("Unit", "USD")
                existing = gtarget.get(metric_name, {"Amount": 0.0, "Unit": unit})
                existing["Amount"] = existing["Amount"] + amount
                existing["Unit"] = unit
                gtarget[metric_name] = existing
        for metric_name, metric in bucket.get("Total", {}).items():
            amount = _safe_float(metric.get("Amount"))
            unit = metric.get("Unit", "USD")
            existing = target["total"].get(metric_name, {"Amount": 0.0, "Unit": unit})
            existing["Amount"] = existing["Amount"] + amount
            existing["Unit"] = unit
            target["total"][metric_name] = existing
    return merged


def _fetch_account_cost(account_id: str, account_name: str, core_session: boto3.Session) -> dict:
    row = {
        "account_id": account_id,
        "account_name": account_name,
        "bu": ACCOUNT_BU.get(account_id, "Other"),
        "mtd": _empty_breakdown(),
        "last_month": _empty_breakdown(),
        "two_months_ago": _empty_breakdown(),
        # Per-service spend for this account, used by the frontend's
        # click-to-drill-down modal. Sorted descending, capped at
        # _TOP_SERVICES_PER_ACCOUNT with the remainder folded into
        # "Other services" so the popup stays readable.
        "services_mtd": [],
        "services_last_month": [],
        # Month-over-month % change vs. last month, and last month vs.
        # the month before that. None when not comparable (see
        # _pct_change()'s docstring).
        "mtd_vs_last_month_pct": None,
        "last_month_vs_prior_pct": None,
        # Kept for backward compatibility with any existing callers that
        # only care about the top-line subtotal per period.
        "mtd_cost": None,
        "last_month_cost": None,
        "currency": "USD",
        "error": None,
    }
    try:
        ce = _client_for_account(account_id, "ce", core_session)
        two_ago_start, prev_start, mtd_start, tomorrow = _month_bounds()
        time_period = {"Start": two_ago_start, "End": tomorrow}

        # Query 1 ("normal"): UnblendedCost grouped by SERVICE + BILLING_ENTITY,
        # explicitly excluding adjustment RECORD_TYPEs (Credit/Refund/Discount/
        # RIFee/SavingsPlan*Fee). DiscountedUsage and SavingsPlanNegation stay
        # in this bucket (they're not in the exclusion list), matching how the
        # finance workbook treats SP/RI-covered usage as still "usage".
        normal_pages = _get_cost_and_usage_paginated(
            ce,
            TimePeriod=time_period,
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter=_NORMAL_RECORD_TYPE_EXCLUDE_FILTER,
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "BILLING_ENTITY"},
            ],
        )
        # Query 2 ("adjustment"): the disjoint complement — only the
        # adjustment RECORD_TYPEs, summed per period (ungrouped total is
        # sufficient; the finance workbook only needs one "Credits" number).
        adjustment_pages = _get_cost_and_usage_paginated(
            ce,
            TimePeriod=time_period,
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": list(_ADJUSTMENT_RECORD_TYPES)}},
        )
        # Query 3 (authoritative totals): ungrouped UnblendedCost per period,
        # with no RECORD_TYPE filter at all. This is the number Cost Explorer
        # itself would show as "total cost" for the period, used only to
        # reconcile rounding residue between (normal + adjustment) and the
        # true total — never to override the classified usage/marketplace/
        # shield/support split itself.
        totals_pages = _get_cost_and_usage_paginated(
            ce,
            TimePeriod=time_period,
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )

        normal_by_period = _merge_results_by_time(normal_pages)
        adjustment_by_period = _merge_results_by_time(adjustment_pages)
        totals_by_period = _merge_results_by_time(totals_pages)

        periods = {mtd_start: row["mtd"], prev_start: row["last_month"], two_ago_start: row["two_months_ago"]}
        # Full (untruncated) per-service maps, kept private to this
        # account until organization-level aggregation folds the long
        # tail — truncating here would silently drop spend from the
        # org-wide "top services" rollup.
        services_by_period: dict[str, dict[str, float]] = {mtd_start: {}, prev_start: {}}
        authoritative_totals: dict[str, float] = {}

        for start, merged in normal_by_period.items():
            target = periods.get(start)
            svc_target = services_by_period.get(start)
            if target is None:
                continue
            for keys, metrics in merged["groups"].items():
                service_name = keys[0] if len(keys) > 0 else "Unknown"
                billing_entity = keys[1] if len(keys) > 1 else None
                amount = _safe_float(metrics.get("UnblendedCost", {}).get("Amount"))
                unit = metrics.get("UnblendedCost", {}).get("Unit") or row["currency"]
                row["currency"] = unit
                bucket_key = _classify_service(service_name, billing_entity)
                target[bucket_key] += amount
                if svc_target is not None and amount != 0:
                    svc_target[service_name] = svc_target.get(service_name, 0.0) + amount

        for start, merged in adjustment_by_period.items():
            target = periods.get(start)
            if target is None:
                continue
            amount = _safe_float(merged["total"].get("UnblendedCost", {}).get("Amount"))
            target["credits"] += amount

        for start, merged in totals_by_period.items():
            authoritative_totals[start] = _safe_float(merged["total"].get("UnblendedCost", {}).get("Amount"))

        # Reconcile: (usage+marketplace+shield+support+credits) should
        # equal the authoritative ungrouped total for that period. Any
        # difference (sub-cent float drift, or a RECORD_TYPE Cost Explorer
        # added that isn't in either query) is folded into `usage` so the
        # subtotal always ties to AWS's own total-cost number, without
        # ever touching the `credits` bucket (credits must stay exactly
        # what query 2 reported).
        for start, period in periods.items():
            classified_sum = period["usage"] + period["marketplace"] + period["shield"] + period["support"] + period["credits"]
            authoritative = authoritative_totals.get(start)
            if authoritative is not None:
                residual = authoritative - classified_sum
                if abs(residual) > 0.0001:
                    period["usage"] += residual

        for period in (row["mtd"], row["last_month"], row["two_months_ago"]):
            for k in ("usage", "marketplace", "shield", "support", "credits"):
                period[k] = _round2(period[k])
            period["subtotal"] = _round2(
                period["usage"] + period["marketplace"] + period["shield"] + period["support"] + period["credits"]
            )

        row["mtd_cost"] = row["mtd"]["subtotal"]
        row["last_month_cost"] = row["last_month"]["subtotal"]
        row["mtd_vs_last_month_pct"] = _pct_change(row["mtd_cost"], row["last_month_cost"])
        row["last_month_vs_prior_pct"] = _pct_change(row["last_month_cost"], row["two_months_ago"]["subtotal"])

        row["services_mtd"] = _top_services(services_by_period[mtd_start])
        row["services_last_month"] = _top_services(services_by_period[prev_start])
        # Full (untruncated) copies stashed for org-level aggregation —
        # not part of the public account row shape, stripped before the
        # row is returned to callers/cached.
        row["_full_services_mtd"] = dict(services_by_period[mtd_start])
        row["_full_services_last_month"] = dict(services_by_period[prev_start])
    except Exception as e:
        logger.exception("cost_explorer: failed for account %s (%s)", account_id, account_name)
        row["error"] = "Could not fetch cost data for this account"
        row["mtd_cost"] = 0.0
        row["last_month_cost"] = 0.0
    return row


def _top_services(service_amounts: dict[str, float], limit: int = _TOP_SERVICES_PER_ACCOUNT) -> list[dict]:
    """Sort a {service_name: amount} map descending, keep the top `limit`
    entries individually, and fold everything else into a single "Other
    services" row so drill-down popups / charts don't get flooded with
    long tails of $0.01 line items. Non-finite/negative amounts are
    treated as 0 rather than corrupting the sort or totals."""
    cleaned = {name: _safe_float(amount) for name, amount in (service_amounts or {}).items()}
    items = sorted(cleaned.items(), key=lambda kv: kv[1], reverse=True)
    top = [{"service": name, "amount": _round2(amount)} for name, amount in items[:limit] if amount > 0]
    rest_total = sum(amount for _, amount in items[limit:] if amount > 0)
    if rest_total > 0.005:
        top.append({"service": "Other services", "amount": _round2(rest_total)})
    return top


def _aggregate_org_services(accounts_result: list[dict], full_key: str, limit: int) -> list[dict]:
    """Aggregate every account's full (untruncated) per-service map into
    one org-wide {service: amount} total, then apply the same top-N +
    'Other services' folding _top_services() uses per-account. Using the
    full maps (not each account's already-truncated services_mtd/
    services_last_month) means long-tail spend from every account is
    still represented in the org-wide total instead of being dropped."""
    org_totals: dict[str, float] = {}
    for r in accounts_result:
        for name, amount in (r.get(full_key) or {}).items():
            org_totals[name] = org_totals.get(name, 0.0) + _safe_float(amount)
    return _top_services(org_totals, limit=limit)


def _strip_internal_fields(row: dict) -> dict:
    """Return a shallow copy of an account row without the '_full_*'
    private per-service maps used only for org-level aggregation — those
    must never leak into the public API response or cache."""
    return {k: v for k, v in row.items() if not k.startswith("_full_")}


def _build_summary(accounts_result: list[dict]) -> dict:
    """Pure aggregation step: takes a list of already-fetched account rows
    (each possibly still carrying '_full_services_*' internal keys) and
    produces the public organization summary dict. Kept separate from the
    fetch/cache orchestration so it's independently unit-testable."""
    # Stable sort by account_id first so ties resolve deterministically,
    # then by mtd_cost descending (highest spenders first).
    accounts_sorted = sorted(accounts_result, key=lambda r: r["account_id"])
    accounts_sorted.sort(key=lambda r: (r["mtd_cost"] or 0), reverse=True)

    total_mtd = sum(r["mtd_cost"] or 0 for r in accounts_sorted)
    total_last_month = sum(r["last_month_cost"] or 0 for r in accounts_sorted)
    total_two_months_ago = sum((r.get("two_months_ago") or {}).get("subtotal") or 0 for r in accounts_sorted)
    error_count = sum(1 for r in accounts_sorted if r.get("error"))

    # Business-Unit subtotals (BU-A / BU-D / BU-B / BU-C / BU-E /
    # Other), same grouping the finance workbook's "0A - AWS Accounts"
    # mapping sheet uses. Sorted by mtd subtotal descending.
    bu_totals: dict[str, dict] = {}
    for r in accounts_sorted:
        bu = r["bu"]
        bt = bu_totals.setdefault(bu, {"bu": bu, "mtd_cost": 0.0, "last_month_cost": 0.0, "account_count": 0})
        bt["mtd_cost"] += r["mtd_cost"] or 0
        bt["last_month_cost"] += r["last_month_cost"] or 0
        bt["account_count"] += 1
    business_units = sorted(
        [{**bt, "mtd_cost": _round2(bt["mtd_cost"]), "last_month_cost": _round2(bt["last_month_cost"])} for bt in bu_totals.values()],
        key=lambda b: b["mtd_cost"], reverse=True,
    )

    # Org-wide top services — aggregated from each account's full
    # (untruncated) per-service maps, not the already-truncated
    # services_mtd/services_last_month, so long-tail spend isn't lost.
    top_services_mtd = _aggregate_org_services(accounts_sorted, "_full_services_mtd", _TOP_SERVICES_ORG_WIDE)
    top_services_last_month = _aggregate_org_services(accounts_sorted, "_full_services_last_month", _TOP_SERVICES_ORG_WIDE)

    public_accounts = [_strip_internal_fields(r) for r in accounts_sorted]

    return {
        "accounts": public_accounts,
        "business_units": business_units,
        "top_services_mtd": top_services_mtd,
        "top_services_last_month": top_services_last_month,
        "total_mtd_cost": _round2(total_mtd),
        "total_last_month_cost": _round2(total_last_month),
        "total_two_months_ago_cost": _round2(total_two_months_ago),
        "mtd_vs_last_month_pct": _pct_change(total_mtd, total_last_month),
        "last_month_vs_prior_pct": _pct_change(total_last_month, total_two_months_ago),
        "currency": "USD",
        "account_count": len(public_accounts),
        "error_count": error_count,
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cache_age_seconds": 0,
    }


def _fetch_all_accounts(core_session: boto3.Session) -> list[dict]:
    accounts_result: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_account_cost, acct_id, name, core_session): acct_id
            for acct_id, name in ACCOUNTS.items()
        }
        for fut in as_completed(futures):
            accounts_result.append(fut.result())
    return accounts_result


def reset_cache_for_tests() -> None:
    """Test-only helper: clear the process-local cache and single-flight
    state so each test starts from a clean slate. Not used by production
    code paths."""
    global _fetch_in_progress
    with _cache_lock:
        _cache["summary"] = None
        _cache["fetched_at"] = 0.0
        _cache["last_good"] = None
        _cache["last_good_at"] = 0.0
        _fetch_in_progress = None


def get_organization_cost_summary(force_refresh: bool = False) -> dict:
    """Org-wide MTD + previous-month cost, one row per account, sorted by
    MTD cost descending. Cached for _CACHE_TTL_SECONDS unless force_refresh.

    Concurrency-safe: uses time.monotonic() (immune to wall-clock jumps),
    returns deep copies so callers can never mutate cached state, and
    single-flights concurrent refreshes so N simultaneous requests trigger
    at most one Cost Explorer fetch instead of N. If every account in a
    fresh fetch failed, falls back to the last known-good summary (if any)
    rather than caching/returning an all-error result — a transient AWS
    blip shouldn't blank out a dashboard that had good data 10 minutes ago.
    """
    global _fetch_in_progress

    with _cache_lock:
        if not force_refresh and _cache["summary"] is not None:
            age = time.monotonic() - _cache["fetched_at"]
            if age < _CACHE_TTL_SECONDS:
                cached = copy.deepcopy(_cache["summary"])
                cached["cache_age_seconds"] = int(age)
                return cached

        # Single-flight: if another thread is already refreshing (whether
        # it was triggered by a normal cache-miss or an explicit
        # force_refresh), wait for it and reuse its result instead of
        # issuing duplicate Cost Explorer calls. force_refresh only means
        # "don't serve a possibly-stale cached value" — it does not mean
        # "always fire my own redundant fetch" when a concurrent caller's
        # fetch is already underway.
        if _fetch_in_progress is not None:
            in_progress_event = _fetch_in_progress
            wait_for_existing = True
        else:
            in_progress_event = threading.Event()
            _fetch_in_progress = in_progress_event
            wait_for_existing = False

    if wait_for_existing:
        in_progress_event.wait(timeout=120)
        with _cache_lock:
            if _cache["summary"] is not None:
                age = time.monotonic() - _cache["fetched_at"]
                cached = copy.deepcopy(_cache["summary"])
                cached["cache_age_seconds"] = int(age)
                return cached
        # Fall through to fetch ourselves if the other fetch didn't
        # produce a usable cached summary (e.g. it raised before caching).

    try:
        core_session = _core_session()
        accounts_result = _fetch_all_accounts(core_session)
        summary = _build_summary(accounts_result)

        with _cache_lock:
            error_count = summary["error_count"]
            account_count = summary["account_count"]
            all_failed = account_count > 0 and error_count == account_count

            if all_failed and _cache["last_good"] is not None:
                logger.warning(
                    "cost_explorer: all %d accounts failed on refresh, serving last known-good summary from %ds ago",
                    account_count, int(time.monotonic() - _cache["last_good_at"]),
                )
                fallback = copy.deepcopy(_cache["last_good"])
                fallback["cache_age_seconds"] = int(time.monotonic() - _cache["last_good_at"])
                fallback["stale_fallback"] = True
                _cache["summary"] = copy.deepcopy(fallback)
                _cache["fetched_at"] = _cache["last_good_at"]
                result = fallback
            else:
                _cache["summary"] = copy.deepcopy(summary)
                _cache["fetched_at"] = time.monotonic()
                if error_count == 0:
                    _cache["last_good"] = copy.deepcopy(summary)
                    _cache["last_good_at"] = _cache["fetched_at"]
                result = summary

        return result
    finally:
        # Only the thread that actually owns this fetch (i.e. set
        # _fetch_in_progress itself, not one that was waiting on it)
        # clears the single-flight guard and wakes up waiters.
        if not wait_for_existing:
            with _cache_lock:
                _fetch_in_progress = None
            in_progress_event.set()
