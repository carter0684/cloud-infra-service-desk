"""
RI & Savings Plan Saved Cost — per-account, per-month realized savings
from Reserved Instances and Savings Plans.

## Why this module does NOT use GetSavingsPlansUtilization/
## GetReservationUtilization as its primary data source

Those two Cost Explorer APIs only aggregate commitment data for the
*calling account plus its own member accounts*. In this AWS Organization
the SP/RI purchases are made at the **management/payer account**
(100000000026 — "payer@example.com"), which is a different
account from Core account (100000000001, the account whose static
keys this portal holds) and none of the ~27 accounts this feature
queries. Calling those APIs against any of those 27 accounts therefore
returns DataUnavailableException / all-zero results even in months where
real Savings Plan coverage and savings clearly exist — confirmed by
probing every account directly.

## The actual data source: GetCostAndUsage, CUR-style (matches Billing
## Console's Bills > Savings page)

Every AWS bill line item Cost Explorer exposes carries a RECORD_TYPE.
When usage is covered by a Savings Plan, Cost Explorer emits TWO rows for
that usage:
  - RECORD_TYPE = "SavingsPlanCoveredUsage": the On-Demand-equivalent
    cost of that usage (what you'd have paid without the commitment) —
    this is also exactly what UnblendedCost reports for that record type.
  - RECORD_TYPE = "SavingsPlanNegation": the same amount, negated (so the
    two cancel out and the *billed* total doesn't double-count SP-covered
    usage).

So for any RECORD_TYPE = "SavingsPlanCoveredUsage" line item:
  - UnblendedCost  = the On-Demand cost equivalent of that usage.
  - AmortizedCost  = what was actually paid for it (the SP's amortized
    hourly commitment rate covering that usage).
  - Amount saved   = UnblendedCost - AmortizedCost.

This naturally answers "which account, which service, how much saved"
per the Billing Console's own Bills > Savings breakdown — without needing
CUR/Athena infrastructure, and without depending on which account legally
owns the Savings Plan purchase (SP coverage is applied wherever the
eligible usage happened, which is exactly what "saved" should mean to
each account).

## IMPORTANT: this must be queried PER ACCOUNT, not as a single org-wide
## call from Core account

An earlier version of this module tried a single GetCostAndUsage call
scoped to Core account, grouped by LINKED_ACCOUNT + SERVICE, assuming
Cost Explorer would roll up every member account's usage the way it does
for the org's Consolidated Billing summary elsewhere. That assumption was
WRONG and was caught by a user manually finding Savings Plan coverage on
account 100000000009 (ibe) that this report was not showing: because
Core account is not the org's actual payer/management account (see above), a
GetCostAndUsage call scoped to its own identity only ever returns the organization
Core's own usage — confirmed directly: a GroupBy=LINKED_ACCOUNT call with
no RECORD_TYPE filter at all still only returned one account
(100000000001) even though the same call's caller is a valid IAM identity
in that account. There is no Organization-wide Cost Explorer view
available from this identity.

The correct approach — and what this module now does — mirrors
billing_by_service.py and cost_explorer.py exactly: query every account
directly through _client_for_account() (Core account with its own static
keys, every other account via sts:AssumeRole(PortalReadOnlyRole) using the
Core account identity as the calling principal), in parallel via
ThreadPoolExecutor, and isolate any single account's failure from the
rest (one account's AssumeRole/API error never blanks out every other
account's real numbers, unlike the old single-shared-call design where
a whole-month failure zeroed out every requested account at once).

Classic Reserved Instances use the analogous RECORD_TYPE =
"DiscountedUsage" (RI-covered usage priced at the RI's discounted rate
directly, with no separate on-demand-equivalent/negation pair — RI
"savings" instead has to be computed as UnblendedCost of the matching
On-Demand rate minus the RI's amortized fee, which Cost Explorer doesn't
expose per-usage-line the same tidy way SPs do). This org currently has
no active classic RI commitments (confirmed: GetReservationUtilization
returns all zeros for every account), so the RI side of this report is
intentionally left at $0 rather than adding unverified guesswork; the
per-account/per-service breakdown below is Savings-Plan-only for now.

## Reseller On-Demand Cost comparison (manual input, PER ACCOUNT)

Some accounts' AWS bill is actually invoiced through a reseller/AWS
Solution Provider rather than directly by AWS, at the reseller's own
on-demand pricing (which may differ from AWS's own on-demand list
price) — and different accounts in this org can be on different
resellers/contracts, so this figure is entered and compared **per
account, per month**, not as one org-wide number. Cost Explorer has no
visibility into any reseller's invoice at all, so there is no API to
pull this from — an admin manually types in the reseller's on-demand
cost figure for one account for a given month (see
get_reseller_ondemand_cost() / set_reseller_ondemand_cost(), persisted
via settings_store.py's generic per-key setting store, keyed
"reseller_ondemand_cost:{account_id}:{month}"). Once set, it's compared
against that same account's own "On-Demand Equivalent" figure for that
month (the account row's own sp_on_demand_equivalent — the same figure
already shown per account in the table) to produce:
  - diff = reseller_ondemand_cost - aws_ondemand_equivalent (positive =
    reseller billed more than AWS's own on-demand list price for the
    equivalent usage; negative = reseller billed less)
  - discount_pct = (aws_ondemand_equivalent - reseller_ondemand_cost)
    / aws_ondemand_equivalent * 100 (positive = the reseller is passing
    on a discount relative to AWS's on-demand list price)
Every account row's `reseller_comparison` is recomputed on every report
read (cheap — one settings-store lookup per account per month)
independently of the AWS Cost Explorer cache below, so an admin's edit
shows up immediately without waiting for the next AWS data refresh. Each
month's `totals` also carries an aggregate `reseller_comparison` — the
sum across every account that has a value entered for that month (an
account with nothing entered simply doesn't contribute to the total;
`totals.reseller_comparison` is None only when *no* account has anything
entered for that month yet).

ENV=local: no real AWS calls are made — a small deterministic mock report
is returned instead, consistent with billing_by_service.py's local mode.
The reseller-cost setting itself still goes through the real
settings_store (which has its own ENV=local SQLite fallback), so this
feature is fully exercisable in local dev too.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

from .billing_by_service import ACCOUNT_ALIAS, month_label, _month_range
from .cost_explorer import (
    CostAnalysisError,
    _client_for_account,
    _get_cost_and_usage_paginated,
    _core_session,
    _round2,
    _safe_float,
)

logger = logging.getLogger(__name__)

ENV = os.environ.get("ENV", "local")

# RECORD_TYPE values used to isolate Savings-Plan-covered usage (see
# module docstring). RI side intentionally left unimplemented — this org
# currently has no active classic Reserved Instance commitments.
_SP_COVERED_RECORD_TYPE = "SavingsPlanCoveredUsage"

_RESELLER_COST_KEY_PREFIX = "reseller_ondemand_cost"

# Hong Kong Time is UTC+8 year-round (no DST) — plain fixed offset, no
# zoneinfo/tzdata dependency needed.
_HKT_OFFSET = timedelta(hours=8)
_DAILY_REFRESH_HOUR_HKT = 8  # 08:00 HKT


def list_accounts() -> list[dict]:
    """Account picker data for the frontend — same superset of accounts
    Billing by Service uses (id + alias), in the same fixed order."""
    return [{"account_id": acct_id, "alias": alias} for acct_id, alias in ACCOUNT_ALIAS.items()]


def _fetch_account_month_sp_savings(account_id: str, month: str, core_session) -> dict:
    """One account, one month: {service_name: {on_demand_equivalent,
    amortized_cost, net_savings}}, straight from GetCostAndUsage scoped
    to that account (Core account directly, every other account via
    AssumeRole — see _client_for_account()), filtered to
    RECORD_TYPE=SavingsPlanCoveredUsage and grouped by SERVICE."""
    start, end = _month_range(month)
    ce = _client_for_account(account_id, "ce", core_session)
    buckets = _get_cost_and_usage_paginated(
        ce,
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost", "AmortizedCost"],
        Filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": [_SP_COVERED_RECORD_TYPE]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    services: dict[str, dict] = {}
    for bucket in buckets:
        for group in bucket.get("Groups", []):
            keys = group.get("Keys", [])
            service = keys[0] if len(keys) > 0 else "Unknown"
            metrics = group.get("Metrics", {})
            on_demand = _safe_float(metrics.get("UnblendedCost", {}).get("Amount"))
            amortized = _safe_float(metrics.get("AmortizedCost", {}).get("Amount"))
            if abs(on_demand) < 0.005 and abs(amortized) < 0.005:
                continue
            services[service] = {
                "on_demand_equivalent": _round2(on_demand),
                "amortized_cost": _round2(amortized),
                "net_savings": _round2(on_demand - amortized),
            }
    return services


def _fetch_month(account_ids: list[str], month: str, core_session) -> dict:
    """One month, every requested account, queried in parallel (one
    GetCostAndUsage call per account — see module docstring for why a
    single shared org-wide call does not work here). Returns the
    per-month report slice: per-account rows (each carrying a per-service
    breakdown) plus that month's aggregate totals. One account's failure
    is isolated to that account's row only."""
    rows_by_account: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_account_month_sp_savings, acct_id, month, core_session): acct_id
            for acct_id in account_ids
        }
        for future in as_completed(futures):
            acct_id = futures[future]
            try:
                services = future.result()
                row = _build_row(acct_id, services)
            except Exception:
                logger.exception("ri_sp_savings: failed to fetch Savings Plan savings for account %s, month %s", acct_id, month)
                row = _build_row(acct_id, {})
                row["error"] = "Could not fetch Savings Plan savings data for this account"
            rows_by_account[acct_id] = row

    rows = [rows_by_account[acct_id] for acct_id in account_ids]

    valid_rows = [r for r in rows if not r.get("error")]
    totals = {
        "sp_on_demand_equivalent": _round2(sum(r["sp_on_demand_equivalent"] for r in valid_rows)),
        "sp_amortized_cost": _round2(sum(r["sp_amortized_cost"] for r in valid_rows)),
        "sp_net_savings": _round2(sum(r["sp_net_savings"] for r in valid_rows)),
        "total_net_savings": _round2(sum(r["total_net_savings"] for r in valid_rows)),
        "error_count": len(rows) - len(valid_rows),
    }
    return {"month": month, "month_label": month_label(month), "accounts": rows, "totals": totals}


def _build_row(account_id: str, services: dict) -> dict:
    """Shared row-shaping logic between the real fetch path and the mock
    path so both produce byte-identical shapes. `reseller_comparison` is
    NOT set here — it's attached afterwards by _attach_reseller_comparisons()
    so it's always read fresh from settings_store, independent of whether
    this row came from the AWS-data cache or a fresh fetch."""
    sp_on_demand = _round2(sum(s["on_demand_equivalent"] for s in services.values()))
    sp_amortized = _round2(sum(s["amortized_cost"] for s in services.values()))
    sp_net_savings = _round2(sp_on_demand - sp_amortized)
    return {
        "account_id": account_id,
        "alias": ACCOUNT_ALIAS.get(account_id, account_id),
        "services": [
            {
                "service": svc_name,
                "on_demand_equivalent": svc["on_demand_equivalent"],
                "amortized_cost": svc["amortized_cost"],
                "net_savings": svc["net_savings"],
            }
            for svc_name, svc in sorted(services.items(), key=lambda kv: kv[1]["net_savings"], reverse=True)
        ],
        "sp_on_demand_equivalent": sp_on_demand,
        "sp_amortized_cost": sp_amortized,
        "sp_net_savings": sp_net_savings,
        "total_net_savings": sp_net_savings,
        "error": None,
        "reseller_comparison": None,
    }


# ── Cache (per account-set + month-set combination), refreshed once a day
# at 08:00 HKT rather than on a rolling TTL ──────────────────────────────
#
# There's no manual "Refresh" button on this page (per user request) —
# the data only needs to be as fresh as yesterday's billing snapshot, and
# Cost Explorer's own GetCostAndUsage numbers for anything but the current
# day don't change intraday anyway. Instead of a rolling TTL, the cache is
# keyed on the current "refresh epoch": the most recent 08:00 HKT
# timestamp that has already passed. Whichever request happens to be the
# first one after that boundary transparently triggers one real AWS
# fetch; every request before or after that (until the next day's 08:00
# HKT) is served from cache. No cron/EventBridge Scheduler infra needed —
# purely a change in how the existing cache is keyed/expired.

_cache_lock = threading.Lock()
_cache: dict = {}  # key -> {"report": ..., "epoch": <iso str of the 08:00 HKT boundary this was fetched under>}


def _current_refresh_epoch() -> str:
    """ISO string of the most recent 08:00 HKT timestamp at/before now —
    changes exactly once every 24 hours, at 08:00 HKT."""
    now_hkt = datetime.now(timezone.utc) + _HKT_OFFSET
    boundary = now_hkt.replace(hour=_DAILY_REFRESH_HOUR_HKT, minute=0, second=0, microsecond=0)
    if now_hkt < boundary:
        boundary -= timedelta(days=1)
    return boundary.isoformat()


def _cache_key(account_ids: list[str], months: list[str]) -> tuple:
    return (tuple(sorted(account_ids)), tuple(months))


def reset_cache_for_tests() -> None:
    """Test-only helper: clear the process-local cache so each test starts
    from a clean slate. Not used by production code paths."""
    with _cache_lock:
        _cache.clear()


_MOCK_SP_SERVICES = (
    "Amazon Elastic Compute Cloud - Compute",
    "AWS Lambda",
    "Amazon Relational Database Service",
    "Amazon ElastiCache",
)


def _mock_report(account_ids: list[str], months: list[str]) -> dict:
    """ENV=local has no real cross-account Cost Explorer access — return a
    small deterministic mock report (random.Random(42), scoped locally so
    it never touches global `random` state), consistent with
    billing_by_service._mock_report()."""
    import random as _random

    rng = _random.Random(42)
    months_out = []
    for month in months:
        rows = []
        for acct_id in account_ids:
            services: dict[str, dict] = {}
            if rng.random() < 0.35:
                num_services = rng.randint(1, 2)
                for svc_name in rng.sample(_MOCK_SP_SERVICES, num_services):
                    amortized = round(rng.uniform(50.0, 1500.0), 2)
                    on_demand = round(amortized * rng.uniform(1.3, 1.8), 2)
                    services[svc_name] = {
                        "on_demand_equivalent": on_demand,
                        "amortized_cost": amortized,
                        "net_savings": round(on_demand - amortized, 2),
                    }
            rows.append(_build_row(acct_id, services))

        totals = {
            "sp_on_demand_equivalent": _round2(sum(r["sp_on_demand_equivalent"] for r in rows)),
            "sp_amortized_cost": _round2(sum(r["sp_amortized_cost"] for r in rows)),
            "sp_net_savings": _round2(sum(r["sp_net_savings"] for r in rows)),
            "total_net_savings": _round2(sum(r["total_net_savings"] for r in rows)),
            "error_count": 0,
        }
        months_out.append({"month": month, "month_label": month_label(month), "accounts": rows, "totals": totals})

    return {
        "months": months_out,
        "is_mock": True,
        "as_of": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cache_age_seconds": 0,
    }


def get_ri_sp_savings_report(account_ids: list[str], months: list[str], force_refresh: bool = False) -> dict:
    """Fetch (or serve cached) per-account, per-month RI + Savings Plan
    net-savings figures for every account_id in `account_ids` across every
    month in `months` ('YYYY-MM' strings). Cached per exact
    (accounts, months) combination, refreshed once per day at 08:00 HKT
    (see _current_refresh_epoch()) rather than on a rolling TTL — deep-
    copies on read so callers can never mutate cache state. Each month's
    reseller on-demand cost comparison (if an admin has entered one — see
    module docstring) is layered on afterwards, independent of this cache,
    so an admin's edit is reflected immediately.
    """
    if not account_ids:
        raise CostAnalysisError("At least one account_id is required")
    if not months:
        raise CostAnalysisError("At least one month is required")

    if ENV == "local":
        report = _mock_report(account_ids, months)
        _attach_reseller_comparisons(report)
        return report

    key = _cache_key(account_ids, months)
    epoch = _current_refresh_epoch()
    with _cache_lock:
        cached = _cache.get(key)
        if not force_refresh and cached is not None and cached["epoch"] == epoch:
            report = copy.deepcopy(cached["report"])
            report["cache_age_seconds"] = int(time.monotonic() - cached["fetched_at"])
            _attach_reseller_comparisons(report)
            return report

    core_session = _core_session()
    months_out = [_fetch_month(account_ids, month, core_session) for month in months]

    report = {
        "months": months_out,
        "is_mock": False,
        "as_of": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cache_age_seconds": 0,
    }

    with _cache_lock:
        _cache[key] = {"report": copy.deepcopy(report), "epoch": epoch, "fetched_at": time.monotonic()}

    _attach_reseller_comparisons(report)
    return report


# ── Reseller On-Demand Cost comparison (manual admin input, PER ACCOUNT) ──

def _reseller_cost_key(account_id: str, month: str) -> str:
    return f"{_RESELLER_COST_KEY_PREFIX}:{account_id}:{month}"


def get_reseller_ondemand_cost(account_id: str, month: str) -> dict:
    """Public shape: {account_id, month, amount (or None if never set),
    currency, updated_by_name, updated_at}. amount is None (not 0) when
    no admin has entered a value yet for this account+month, so the
    frontend can distinguish "not entered" from "entered as zero"."""
    from . import settings_store

    stored = settings_store.get_setting(_reseller_cost_key(account_id, month))
    value = (stored or {}).get("value") or {}
    return {
        "account_id": account_id,
        "month": month,
        "amount": value.get("amount"),
        "currency": value.get("currency", "USD"),
        "updated_by_name": stored.get("updated_by_name") if stored else None,
        "updated_at": stored.get("updated_at") if stored else None,
    }


def set_reseller_ondemand_cost(account_id: str, month: str, amount: float, *, currency: str = "USD", actor_email: str, actor_name: str) -> dict:
    """Persist the reseller-invoiced on-demand cost for one AWS account,
    one calendar month. amount must be a finite, non-negative number (a
    reseller invoice can't be negative — a correction/credit would be its
    own separate concept, out of scope for this simple comparison box)."""
    from . import settings_store

    safe_amount = _safe_float(amount)
    if safe_amount < 0:
        raise ValueError("amount must be zero or a positive number")
    settings_store.set_setting(
        _reseller_cost_key(account_id, month),
        {"amount": _round2(safe_amount), "currency": (currency or "USD").strip().upper()[:3] or "USD"},
        actor_email=actor_email, actor_name=actor_name,
    )
    return get_reseller_ondemand_cost(account_id, month)


def delete_reseller_ondemand_cost(account_id: str, month: str, *, actor_email: str, actor_name: str) -> None:
    """Clear a previously entered reseller cost for one account+month
    (e.g. the admin mistyped it and wants to remove it rather than
    overwrite with another guess)."""
    from . import settings_store

    settings_store.set_setting(_reseller_cost_key(account_id, month), {}, actor_email=actor_email, actor_name=actor_name)


def _build_reseller_comparison(aws_on_demand_equivalent: float, reseller_entry: dict) -> Optional[dict]:
    """None if the admin hasn't entered a reseller cost for this
    account+month yet. Otherwise: {reseller_ondemand_cost,
    aws_ondemand_equivalent, diff, discount_pct}.
      diff = reseller_ondemand_cost - aws_ondemand_equivalent (positive =
        reseller billed MORE than AWS's own on-demand list price for the
        equivalent usage; negative = reseller billed LESS).
      discount_pct = the discount the reseller is passing on relative to
        AWS's on-demand list price: (aws - reseller) / aws * 100. Positive
        = a real discount; negative = the reseller is charging a premium
        over AWS list price. None (not 0) when aws_ondemand_equivalent is
        zero — a percentage of zero is undefined, not "0% discount"."""
    amount = reseller_entry.get("amount")
    if amount is None:
        return None
    reseller_cost = _round2(_safe_float(amount))
    aws_cost = _round2(aws_on_demand_equivalent)
    diff = _round2(reseller_cost - aws_cost)
    discount_pct = _round2((aws_cost - reseller_cost) / aws_cost * 100) if aws_cost > 0.005 else None
    return {
        "reseller_ondemand_cost": reseller_cost,
        "aws_ondemand_equivalent": aws_cost,
        "currency": reseller_entry.get("currency", "USD"),
        "diff": diff,
        "discount_pct": discount_pct,
        "updated_by_name": reseller_entry.get("updated_by_name"),
        "updated_at": reseller_entry.get("updated_at"),
    }


def _attach_reseller_comparisons(report: dict) -> None:
    """Mutates `report` in place. For every month slice, attaches a
    `reseller_comparison` to EACH account row (None if that account has
    nothing entered for this month), plus an aggregate
    `totals.reseller_comparison` — the sum across every account that does
    have a value entered for that month (None only when no account has
    anything entered for that month yet). Always re-read from
    settings_store at report build time (cheap — one lookup per account
    per month) rather than cached alongside the AWS Cost Explorer data,
    so editing an account's reseller cost box takes effect immediately
    without waiting for the next daily AWS refresh."""
    for month_slice in report.get("months", []):
        month = month_slice["month"]
        entered_comparisons = []
        for account_row in month_slice.get("accounts", []):
            entry = get_reseller_ondemand_cost(account_row["account_id"], month)
            comparison = _build_reseller_comparison(account_row.get("sp_on_demand_equivalent", 0.0), entry)
            account_row["reseller_comparison"] = comparison
            if comparison is not None:
                entered_comparisons.append(comparison)

        if not entered_comparisons:
            month_slice["totals"]["reseller_comparison"] = None
            continue

        total_reseller = _round2(sum(c["reseller_ondemand_cost"] for c in entered_comparisons))
        total_aws = _round2(sum(c["aws_ondemand_equivalent"] for c in entered_comparisons))
        total_diff = _round2(total_reseller - total_aws)
        total_discount_pct = _round2((total_aws - total_reseller) / total_aws * 100) if total_aws > 0.005 else None
        month_slice["totals"]["reseller_comparison"] = {
            "reseller_ondemand_cost": total_reseller,
            "aws_ondemand_equivalent": total_aws,
            "currency": entered_comparisons[0]["currency"],
            "diff": total_diff,
            "discount_pct": total_discount_pct,
            "accounts_with_entry": len(entered_comparisons),
        }
