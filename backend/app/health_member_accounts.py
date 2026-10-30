"""
Member-account AWS Health events — fills a gap in the organization view.

Why this exists
---------------
The Health Dashboard is built from `describe_events_for_organization()`
(see escalation.list_ams_organization_health_events). Measured against
the live Organization (2026-10): that API does NOT return every event.
Some ACCOUNT_SPECIFIC events that a member account's OWN
`describe_events()` returns are simply absent from the organization
view — even when the organization call is filtered to that very account.
Example: the Gen-BI account (100000000005) had 32 events of its own, of
which the organization API returned 13; the missing ones included an
upcoming AWS_KIRO_PLANNED_LIFECYCLE_EVENT and its own
AWS_BILLING_PLANNED_LIFECYCLE_EVENT_1786512164 (both effective 2026-11-09).

This is not a de-duplication issue in the Portal (which keeps every ARN
the organization API returns); AWS never returns those events there. So
the only way to see them is to ask each member account directly, which
is what this module does — using the same read-only cross-account path as
the rest of the Portal (Core account identity -> sts:AssumeRole(Q-readonly-
role)), and the AMS account's own Health client for the AMS account.

What it does
------------
- collect_member_events(): per-account describe_events() for every
  configured account, in parallel. One account's failure (no role, or a
  missing Business/Enterprise Support plan -> SubscriptionRequiredException)
  never affects the others. Never raises.
- events_missing_from_org(): the events found in member accounts whose ARN
  the organization view did not return, each tagged with the account(s)
  that see it. The organization list stays authoritative — nothing it
  returned is replaced or duplicated.
- get_member_event_detail(): description + affected resources for such an
  event, read from a member account that sees it (the organization detail
  APIs cannot look these ARNs up).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import boto3
from botocore.config import Config

from . import cost_explorer, escalation, org_config
from .cache_utils import TTLCache

logger = logging.getLogger(__name__)

# AMS account == Core account: queried with the AMS Health client
# directly (no AssumeRole into itself).
CORE_ACCOUNT_ID = org_config.CORE_ACCOUNT_ID

_MAX_WORKERS = 10
_MAX_EVENTS_PER_ACCOUNT = 10000  # safety ceiling, same spirit as _MAX_ORG_HEALTH_EVENTS
_BOTO_CONFIG = Config(connect_timeout=5, read_timeout=25, retries={"max_attempts": 2})

# Errors that just mean "this account can't use the Health API" (e.g. no
# Business/Enterprise Support plan) — expected, logged quietly, not a fault.
_EXPECTED_SKIP_CODES = {"SubscriptionRequiredException"}

# arn -> {"event": raw event, "accounts": [account ids]} for EVERY event any
# member account returned. Also powers the detail lookup, so it is shared
# between the dashboard refresh and detail clicks. 10 min: longer than the
# dashboard's own 5 min cache so a detail click right after a refresh never
# has to repeat the whole fan-out.
_INDEX_CACHE = TTLCache(ttl_seconds=10 * 60)
_INDEX_KEY = "member_events"


def _health_client_for(account_id: str):
    # Accounts onboarded through the Cloud Accounts page are reached with the Portal's
    # own role + the account's External_Id; every legacy account falls through to the
    # unchanged code below.
    from . import cloud_account_access, cloud_account_store

    if cloud_account_store.access_mode(account_id) == "infraops":
        return cloud_account_access.client(account_id, "health", region_name="us-east-1", config=_BOTO_CONFIG)
    if account_id == CORE_ACCOUNT_ID:
        return escalation._get_ams_health_client()
    core = cost_explorer._core_session()
    creds = core.client("sts", region_name="us-east-1").assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{cost_explorer.READONLY_ROLE_NAME}",
        RoleSessionName="job-portal-health-member",
    )["Credentials"]
    return boto3.client(
        "health",
        region_name="us-east-1",
        config=_BOTO_CONFIG,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _error_code(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = resp.get("Error", {}).get("Code")
        if code:
            return code
    return type(exc).__name__


def _fetch_account_events(account_id: str) -> tuple[str, list[dict], Optional[str]]:
    """(account_id, events, error_code). error_code is None on success."""
    try:
        client = _health_client_for(account_id)
        events: list[dict] = []
        token: Optional[str] = None
        while True:
            kwargs = {"maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            resp = client.describe_events(**kwargs)
            events.extend(resp.get("events", []))
            token = resp.get("nextToken")
            if not token or len(events) >= _MAX_EVENTS_PER_ACCOUNT:
                break
        return account_id, events, None
    except Exception as e:
        code = _error_code(e)
        if code in _EXPECTED_SKIP_CODES:
            logger.info("health_member_accounts: skipping %s (%s)", account_id, code)
        else:
            logger.warning("health_member_accounts: %s failed: %s", account_id, code)
        return account_id, [], code


def _collect() -> dict:
    """Pure fan-out: query every configured account's own Health API in
    parallel. No cache side effects (safe to use as a TTLCache fetch_fn).

    Returns {"by_arn": {arn: {"event": raw, "accounts": [ids]}},
             "failures": {account_id: error_code}}. Never raises.
    """
    try:
        account_ids = list(cost_explorer.ACCOUNTS)
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            results = list(pool.map(_fetch_account_events, account_ids))
    except Exception as e:  # pragma: no cover - defensive; per-account errors are already contained
        logger.error("health_member_accounts: collect failed: %s", e)
        return {"by_arn": {}, "failures": {"*": type(e).__name__}}

    by_arn: dict[str, dict] = {}
    failures: dict[str, str] = {}
    for account_id, events, err in results:
        if err:
            failures[account_id] = err
            continue
        for ev in events:
            arn = ev.get("arn")
            if not arn:
                continue
            slot = by_arn.setdefault(arn, {"event": ev, "accounts": []})
            if account_id not in slot["accounts"]:
                slot["accounts"].append(account_id)

    unexpected = {a: c for a, c in failures.items() if c not in _EXPECTED_SKIP_CODES}
    if unexpected:
        logger.warning("health_member_accounts: %d account(s) could not be queried: %s", len(unexpected), unexpected)
    return {"by_arn": by_arn, "failures": failures}


def collect_member_events() -> dict:
    """Fresh fan-out for the dashboard refresh; also stores the result in
    the detail-lookup index so a detail click right after doesn't repeat
    the whole fan-out. Never raises. Returns the same shape as _collect()
    (plus a cache_age_seconds key, which callers ignore)."""
    return _INDEX_CACHE.get_or_fetch(_INDEX_KEY, _collect, force_refresh=True)


def events_missing_from_org(member: dict, org_arns: set[str]) -> list[dict]:
    """Member-account events the organization view did not return.

    Each is a copy of the raw boto3 event with `_member_account_ids` set to
    the account(s) that see it, so the rest of the app can treat it like
    any other raw event (same keys) and still know where it came from.
    """
    extra: list[dict] = []
    for arn, slot in member.get("by_arn", {}).items():
        if arn in org_arns:
            continue
        ev = dict(slot["event"])
        ev["_member_account_ids"] = sorted(slot["accounts"])
        extra.append(ev)
    return extra


def _index() -> dict:
    """The arn -> {event, accounts} index, re-collected if it has expired
    (e.g. a detail click served by a different ECS task than the one that
    built the dashboard)."""
    return _INDEX_CACHE.get_or_fetch(_INDEX_KEY, _collect)


def accounts_for_event(event_arn: str) -> list[str]:
    slot = _index().get("by_arn", {}).get(event_arn)
    return list(slot["accounts"]) if slot else []


def get_member_event_detail(event_arn: str) -> Optional[dict]:
    """Detail for an event that only member accounts can see, in the same
    shape as escalation.get_ams_organization_health_event_detail(). Returns
    None if no member account knows this ARN."""
    accounts = accounts_for_event(event_arn)
    if not accounts:
        return None

    description: Optional[str] = None
    status_code: Optional[str] = None
    entities: list[dict] = []
    for i, account_id in enumerate(accounts):
        try:
            client = _health_client_for(account_id)
            if i == 0:
                resp = client.describe_event_details(eventArns=[event_arn])
                ok = resp.get("successfulSet") or []
                if ok:
                    description = (ok[0].get("eventDescription") or {}).get("latestDescription")
                    status_code = (ok[0].get("event") or {}).get("statusCode")
            token: Optional[str] = None
            while True:
                kwargs = {"filter": {"eventArns": [event_arn]}}
                if token:
                    kwargs["nextToken"] = token
                resp = client.describe_affected_entities(**kwargs)
                for ent in resp.get("entities", []):
                    entities.append({
                        "entity_value": ent.get("entityValue"),
                        "aws_account_id": ent.get("awsAccountId") or account_id,
                        "status_code": ent.get("statusCode"),
                        "last_updated_time": ent.get("lastUpdatedTime"),
                    })
                token = resp.get("nextToken")
                if not token:
                    break
        except Exception as e:
            # One account failing must not hide what the others returned.
            logger.warning("health_member_accounts: detail for %s via %s failed: %s", event_arn, account_id, _error_code(e))

    return {
        "event_arn": event_arn,
        "event_description": description,
        "event_status_code": status_code,
        "affected_accounts": accounts,
        "affected_entities": entities,
    }
