"""
AWS Security Hub integration for the Cloud Infra Job Portal.

Shows active (RecordState=ACTIVE, WorkflowStatus=NEW) Security Hub
findings across the same superset of AWS accounts Billing by Service /
Savings Plan Cost use (billing_by_service.ACCOUNT_ALIAS), plus an
on-demand AI executive summary + remediation plan (Bedrock).

## Cross-account access pattern

Identical to cost_explorer.py / billing_by_service.py / ri_sp_savings.py:
Core account (100000000001) is queried directly with its own static
IAM keys (Secrets Manager); every other account is reached via
sts:AssumeRole(PortalReadOnlyRole) using the Core account identity as the
calling principal. Fetches run in parallel via ThreadPoolExecutor, and
one account's failure (AssumeRole error, or simply not having Security
Hub enabled at all) never blanks out any other account's real findings.

## Not every account has Security Hub enabled

Security Hub is opt-in per account/region. An account that has never
enabled it returns InvalidAccessException ("Account ... is not
subscribed to AWS Security Hub") from every Security Hub API call —
this is treated as a normal, expected state (`enabled: false`), not an
error, and is reported separately from real fetch failures
(AssumeRole denial, throttling, etc. -> `error` field).

## Findings volume safety ceiling

An account can have hundreds of active findings (mostly informational/
MEDIUM severity control checks). To keep response size and latency
reasonable, GetFindings pagination per account stops at
_MAX_FINDINGS_PER_ACCOUNT — the returned `total_findings` count and
`findings_capped` flag tell the caller whether the real total is higher
than what was fetched. Findings are sorted by severity
(CRITICAL > HIGH > MEDIUM > LOW > INFORMATIONAL) before capping, so a
capped account still shows its most important findings first.

ENV=local: no real AWS calls — a small deterministic mock summary is
returned instead, consistent with the other cost/billing modules.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import boto3

from .billing_by_service import ACCOUNT_ALIAS
from .cost_explorer import (
    CORE_ACCOUNT_ID,
    READONLY_ROLE_NAME,
    CostAnalysisError,
    _client_for_account,
    _core_session,
)

logger = logging.getLogger(__name__)

ENV = os.environ.get("ENV", "local")

# Security Hub's cross-region "Finding Aggregator" feature is configured
# (per-account, RegionLinkingMode=ALL_REGIONS) to aggregate every linked
# region's findings into this single region. Confirmed live via
# `aws securityhub get-finding-aggregator` against Core account
# (100000000001) on 2026-08-21 — FindingAggregationRegion=ap-southeast-1.
# Querying us-east-1 (the default region used by cost_explorer._client_
# for_account()/_core_session(), which is correct for Cost Explorer —
# a us-east-1-only global API — but wrong for Security Hub) was the
# root cause of most accounts showing 0 findings or "not subscribed"
# even though Security Hub is in fact enabled and reporting findings.
# DO NOT reuse cost_explorer._client_for_account() here — it is
# hardcoded to us-east-1, which several modules (Cost Explorer, Billing
# by Service, RI/SP Savings) genuinely require. Security Hub needs its
# own region-aware client, mirroring cmdb._regional_client_for_account().
_SECURITY_HUB_REGION = "ap-southeast-1"


def _securityhub_client_for_account(account_id: str, core_session: boto3.Session):
    """Return a boto3 Security Hub client for `account_id`, scoped to
    _SECURITY_HUB_REGION (the account's designated Finding Aggregator
    region) instead of cost_explorer's hardcoded us-east-1.

    Mirrors cost_explorer._client_for_account() / cmdb._regional_client_
    for_account() exactly, just with a fixed non-us-east-1 region."""
    # Accounts onboarded through the Cloud Accounts page are reached with the Portal's
    # own role + the account's External_Id; every legacy account falls through to the
    # unchanged code below.
    from . import cloud_account_access, cloud_account_store

    if cloud_account_store.access_mode(account_id) == "infraops":
        return cloud_account_access.client(account_id, "securityhub", region_name=_SECURITY_HUB_REGION)
    if account_id == CORE_ACCOUNT_ID:
        return core_session.client("securityhub", region_name=_SECURITY_HUB_REGION)

    sts = core_session.client("sts", region_name="us-east-1")
    role_arn = f"arn:aws:iam::{account_id}:role/{READONLY_ROLE_NAME}"
    assumed = sts.assume_role(RoleArn=role_arn, RoleSessionName="job-portal-security-hub")
    creds = assumed["Credentials"]
    return boto3.client(
        "securityhub",
        region_name=_SECURITY_HUB_REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFORMATIONAL": 4}
_SEVERITY_LABELS = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL")

# Safety ceiling on findings fetched per account — not meant to be hit for
# most accounts, but guards against one noisy account (hundreds of
# low-severity control findings) dominating latency/payload size.
_MAX_FINDINGS_PER_ACCOUNT = 300
_FINDINGS_PAGE_SIZE = 100

# Findings actually surfaced in the API response per account (after
# sorting by severity) — keeps the page/payload readable; `total_findings`
# still reports the real (possibly capped) count separately.
_TOP_FINDINGS_PER_ACCOUNT = 25

_CACHE_TTL_SECONDS = 10 * 60  # 10 minutes — Security Hub findings don't change second-to-second
_cache_lock = threading.Lock()
_cache: dict = {}  # key (tuple of account_ids) -> {"value": dict, "fetched_at": monotonic()}
_inflight: dict = {}


def list_accounts() -> list[dict]:
    """Account picker data for the frontend — same superset of accounts
    Billing by Service / Savings Plan Cost use."""
    return [{"account_id": acct_id, "alias": alias} for acct_id, alias in ACCOUNT_ALIAS.items()]


def _severity_sort_key(finding: dict) -> tuple:
    sev = finding.get("severity") or "INFORMATIONAL"
    return (_SEVERITY_ORDER.get(sev, 5), -(finding.get("severity_normalized") or 0))


def _fetch_account_findings(account_id: str, alias: str, core_session: boto3.Session) -> dict:
    row: dict = {
        "account_id": account_id,
        "alias": alias,
        "enabled": False,
        "error": None,
        "severity_counts": {label: 0 for label in _SEVERITY_LABELS},
        "total_findings": 0,
        "findings_capped": False,
        "findings": [],
    }
    try:
        client = _securityhub_client_for_account(account_id, core_session)
    except Exception as e:
        row["error"] = f"Could not access account: {str(e)[:200]}"
        return row

    try:
        client.describe_hub()
        row["enabled"] = True
    except Exception as e:
        msg = str(e)
        if "not subscribed" in msg or "InvalidAccessException" in msg:
            row["enabled"] = False
        else:
            row["error"] = f"Could not check Security Hub status: {msg[:200]}"
        return row

    all_findings: list[dict] = []
    try:
        paginator = client.get_paginator("get_findings")
        page_iter = paginator.paginate(
            Filters={
                "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
                "WorkflowStatus": [{"Value": "NEW", "Comparison": "EQUALS"}],
            },
            MaxResults=_FINDINGS_PAGE_SIZE,
            PaginationConfig={"MaxItems": _MAX_FINDINGS_PER_ACCOUNT + 1},
        )
        for page in page_iter:
            for f in page.get("Findings", []):
                all_findings.append(f)
                if len(all_findings) >= _MAX_FINDINGS_PER_ACCOUNT + 1:
                    break
            if len(all_findings) >= _MAX_FINDINGS_PER_ACCOUNT + 1:
                break
    except Exception as e:
        row["error"] = f"Could not fetch findings: {str(e)[:200]}"
        return row

    row["findings_capped"] = len(all_findings) > _MAX_FINDINGS_PER_ACCOUNT
    if row["findings_capped"]:
        all_findings = all_findings[:_MAX_FINDINGS_PER_ACCOUNT]
    row["total_findings"] = len(all_findings)

    normalized: list[dict] = []
    for f in all_findings:
        severity = (f.get("Severity") or {}).get("Label", "INFORMATIONAL")
        row["severity_counts"][severity] = row["severity_counts"].get(severity, 0) + 1
        resources = f.get("Resources") or []
        first_resource = resources[0] if resources else {}
        remediation = ((f.get("Remediation") or {}).get("Recommendation") or {})
        normalized.append({
            "id": f.get("Id"),
            "title": f.get("Title"),
            "description": f.get("Description"),
            "severity": severity,
            "severity_normalized": (f.get("Severity") or {}).get("Normalized", 0),
            "compliance_status": (f.get("Compliance") or {}).get("Status"),
            "security_control_id": (f.get("Compliance") or {}).get("SecurityControlId"),
            "workflow_status": (f.get("Workflow") or {}).get("Status"),
            "resource_type": first_resource.get("Type"),
            "resource_id": first_resource.get("Id"),
            "region": f.get("Region"),
            "generator_id": f.get("GeneratorId"),
            "first_observed_at": f.get("FirstObservedAt"),
            "updated_at": f.get("UpdatedAt"),
            "remediation_text": remediation.get("Text"),
            "remediation_url": remediation.get("Url"),
        })

    normalized.sort(key=_severity_sort_key)
    row["findings"] = normalized[:_TOP_FINDINGS_PER_ACCOUNT]
    return row


def _mock_summary() -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    accounts = [
        {
            "account_id": "100000000001", "alias": "core", "enabled": True, "error": None,
            "severity_counts": {"CRITICAL": 1, "HIGH": 3, "MEDIUM": 12, "LOW": 5, "INFORMATIONAL": 0},
            "total_findings": 21, "findings_capped": False,
            "findings": [
                {
                    "id": "mock-finding-1", "title": "S3 bucket should have server-side encryption enabled",
                    "description": "This control checks whether an S3 bucket has default encryption enabled.",
                    "severity": "CRITICAL", "severity_normalized": 90, "compliance_status": "FAILED",
                    "security_control_id": "S3.4", "workflow_status": "NEW",
                    "resource_type": "AwsS3Bucket", "resource_id": "arn:aws:s3:::mock-bucket-1",
                    "region": "us-east-1", "generator_id": "security-control/S3.4",
                    "first_observed_at": now, "updated_at": now,
                    "remediation_text": "For information on how to correct this issue, consult the AWS Security Hub controls documentation.",
                    "remediation_url": "https://docs.aws.amazon.com/console/securityhub/S3.4/remediation",
                },
            ],
        },
        {
            "account_id": "100000000003", "alias": "network", "enabled": False, "error": None,
            "severity_counts": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFORMATIONAL": 0},
            "total_findings": 0, "findings_capped": False, "findings": [],
        },
    ]
    totals = {"CRITICAL": 1, "HIGH": 3, "MEDIUM": 12, "LOW": 5, "INFORMATIONAL": 0}
    return {
        "accounts": accounts,
        "totals": {
            "severity_counts": totals,
            "total_findings": sum(totals.values()),
            "account_count": len(accounts),
            "enabled_count": sum(1 for a in accounts if a["enabled"]),
            "not_enabled_count": sum(1 for a in accounts if not a["enabled"] and not a["error"]),
            "error_count": sum(1 for a in accounts if a["error"]),
        },
        "as_of": now,
        "is_mock": True,
    }


def _fetch_summary(account_ids: list[str]) -> dict:
    if ENV == "local":
        return _mock_summary()

    try:
        core_session = _core_session()
    except CostAnalysisError as e:
        raise
    except Exception as e:
        raise CostAnalysisError(f"Could not load Core account credentials: {e}") from e

    accounts_out: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_account_findings, acct_id, ACCOUNT_ALIAS.get(acct_id, acct_id), core_session): acct_id
            for acct_id in account_ids
        }
        for fut in as_completed(futures):
            accounts_out.append(fut.result())
    accounts_out.sort(key=lambda a: account_ids.index(a["account_id"]))

    totals = {label: 0 for label in _SEVERITY_LABELS}
    total_findings = 0
    enabled_count = 0
    not_enabled_count = 0
    error_count = 0
    for a in accounts_out:
        if a["error"]:
            error_count += 1
            continue
        if not a["enabled"]:
            not_enabled_count += 1
            continue
        enabled_count += 1
        total_findings += a["total_findings"]
        for label in _SEVERITY_LABELS:
            totals[label] += a["severity_counts"].get(label, 0)

    return {
        "accounts": accounts_out,
        "totals": {
            "severity_counts": totals,
            "total_findings": total_findings,
            "account_count": len(accounts_out),
            "enabled_count": enabled_count,
            "not_enabled_count": not_enabled_count,
            "error_count": error_count,
        },
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "is_mock": False,
    }


# Resource types this module knows how to trace back to impacted IAM
# principals (users/groups/roles). Only IAM policy findings are supported
# for now (e.g. IAM.1 "IAM policies should not allow full * administrative
# privileges") — the resource IS an IAM policy, so IAM's own
# list_entities_for_policy API directly answers "who is attached to this".
_IAM_POLICY_RESOURCE_TYPE = "AwsIamPolicy"
_IAM_ROLE_RESOURCE_TYPE = "AwsIamRole"
_IAM_USER_RESOURCE_TYPE = "AwsIamUser"


def get_impacted_principals(account_id: str, finding: dict) -> dict:
    """
    For an IAM-related finding, find which IAM users, groups, and roles
    are actually affected — e.g. for "IAM policies should not allow full
    '*' administrative privileges", this looks up every group/user/role
    the offending policy is attached to (via iam:ListEntitiesForPolicy),
    then expands each group's membership (via iam:GetGroup) so a group
    attachment still surfaces the individual users impacted, not just the
    group name.

    Returns:
      {
        "supported": bool,       # False for resource types this module
                                  # doesn't know how to trace (e.g. an S3
                                  # bucket finding has no "IAM principal"
                                  # concept the same way)
        "resource_type": str,
        "policy_arn": str | None,
        "directly_attached_users": [{"user_name": ..., "user_id": ...}],
        "directly_attached_groups": [{"group_name": ..., "group_id": ...}],
        "directly_attached_roles": [{"role_name": ..., "role_id": ...}],
        "users_via_groups": [{"user_name": ..., "via_group": ...}],
        "error": str | None,     # best-effort — a permissions/API error
                                  # here never fails the whole finding
                                  # lookup, it's reported inline instead
      }
    """
    result = {
        "supported": False,
        "resource_type": finding.get("resource_type"),
        "policy_arn": None,
        "directly_attached_users": [],
        "directly_attached_groups": [],
        "directly_attached_roles": [],
        "users_via_groups": [],
        "error": None,
    }

    resource_type = finding.get("resource_type")
    resource_id = finding.get("resource_id")

    if resource_type != _IAM_POLICY_RESOURCE_TYPE or not resource_id:
        # Only IAM-policy-attached findings are traced to principals for
        # now. Other resource types (S3 buckets, EC2 instances, etc.)
        # simply report supported=False so the frontend can skip this
        # section entirely rather than showing an empty/confusing box.
        return result

    result["supported"] = True
    result["policy_arn"] = resource_id

    if ENV == "local":
        result["directly_attached_groups"] = [{"group_name": "Infrastructure_Admins", "group_id": "mock-group-id"}]
        result["users_via_groups"] = [{"user_name": "alice_admin", "via_group": "Infrastructure_Admins"}]
        return result

    try:
        core_session = _core_session()
        iam_client = _client_for_account(account_id, "iam", core_session)
    except Exception as e:
        result["error"] = f"Could not access IAM in this account: {str(e)[:200]}"
        return result

    try:
        entities = iam_client.list_entities_for_policy(PolicyArn=resource_id)
    except Exception as e:
        result["error"] = f"Could not look up policy attachments: {str(e)[:200]}"
        return result

    result["directly_attached_users"] = [
        {"user_name": u.get("UserName"), "user_id": u.get("UserId")}
        for u in entities.get("PolicyUsers", [])
    ]
    result["directly_attached_groups"] = [
        {"group_name": g.get("GroupName"), "group_id": g.get("GroupId")}
        for g in entities.get("PolicyGroups", [])
    ]
    result["directly_attached_roles"] = [
        {"role_name": r.get("RoleName"), "role_id": r.get("RoleId")}
        for r in entities.get("PolicyRoles", [])
    ]

    # Expand each attached group's membership — a policy attached to a
    # group impacts every user in that group, not just "the group" as an
    # abstract entity. Best-effort per group: one group's lookup failing
    # (e.g. a stale/deleted group) doesn't block the others.
    for g in result["directly_attached_groups"]:
        group_name = g.get("group_name")
        if not group_name:
            continue
        try:
            group_detail = iam_client.get_group(GroupName=group_name)
            for u in group_detail.get("Users", []):
                result["users_via_groups"].append({
                    "user_name": u.get("UserName"),
                    "via_group": group_name,
                })
        except Exception as e:
            logger.warning("security_hub: could not expand group %s membership: %s", group_name, e)

    return result


def get_finding_by_id(account_id: str, finding_id: str) -> Optional[dict]:
    """
    Fetch a single finding by its AWS Security Hub finding Id, scoped to
    one account — used by the per-finding "click a row -> auto-analyse"
    AI feature so it always analyses the finding's current/live data
    rather than whatever was cached when the page first loaded. Returns
    a normalized finding dict (same shape as the `findings` list in
    get_security_hub_summary()'s per-account rows), or None if not found.
    """
    if ENV == "local":
        mock = _mock_summary()
        for acct in mock["accounts"]:
            if acct["account_id"] != account_id:
                continue
            for f in acct.get("findings", []):
                if f["id"] == finding_id:
                    return f
        return None

    core_session = _core_session()
    client = _securityhub_client_for_account(account_id, core_session)
    resp = client.get_findings(Filters={"Id": [{"Value": finding_id, "Comparison": "EQUALS"}]}, MaxResults=1)
    findings = resp.get("Findings", [])
    if not findings:
        return None
    f = findings[0]
    resources = f.get("Resources") or []
    first_resource = resources[0] if resources else {}
    remediation = ((f.get("Remediation") or {}).get("Recommendation") or {})
    return {
        "id": f.get("Id"),
        "title": f.get("Title"),
        "description": f.get("Description"),
        "severity": (f.get("Severity") or {}).get("Label", "INFORMATIONAL"),
        "severity_normalized": (f.get("Severity") or {}).get("Normalized", 0),
        "compliance_status": (f.get("Compliance") or {}).get("Status"),
        "security_control_id": (f.get("Compliance") or {}).get("SecurityControlId"),
        "workflow_status": (f.get("Workflow") or {}).get("Status"),
        "resource_type": first_resource.get("Type"),
        "resource_id": first_resource.get("Id"),
        "region": f.get("Region"),
        "generator_id": f.get("GeneratorId"),
        "first_observed_at": f.get("FirstObservedAt"),
        "updated_at": f.get("UpdatedAt"),
        "remediation_text": remediation.get("Text"),
        "remediation_url": remediation.get("Url"),
    }


def get_security_hub_summary(account_ids: list[str], force_refresh: bool = False) -> dict:
    """Fetch (or serve cached) Security Hub findings summary for every
    account_id in `account_ids`. Cached per exact account-set for
    _CACHE_TTL_SECONDS, with single-flight semantics so concurrent
    requests for the same account-set share one fetch instead of firing
    duplicate cross-account calls."""
    import copy

    key = tuple(sorted(account_ids))
    with _cache_lock:
        cached = _cache.get(key)
        if not force_refresh and cached is not None:
            age = time.monotonic() - cached["fetched_at"]
            if age < _CACHE_TTL_SECONDS:
                result = copy.deepcopy(cached["value"])
                result["cache_age_seconds"] = int(age)
                return result

        inflight = _inflight.get(key)
        if inflight is not None:
            wait_for_existing = True
        else:
            inflight = threading.Event()
            _inflight[key] = inflight
            wait_for_existing = False

    if wait_for_existing:
        inflight.wait(timeout=120)
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None:
                age = time.monotonic() - cached["fetched_at"]
                result = copy.deepcopy(cached["value"])
                result["cache_age_seconds"] = int(age)
                return result

    try:
        value = _fetch_summary(account_ids)
        with _cache_lock:
            _cache[key] = {"value": copy.deepcopy(value), "fetched_at": time.monotonic()}
        result = copy.deepcopy(value)
        result["cache_age_seconds"] = 0
        return result
    finally:
        if not wait_for_existing:
            with _cache_lock:
                _inflight.pop(key, None)
            inflight.set()
