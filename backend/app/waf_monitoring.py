"""
AWS WAF Rule Monitoring for the Cloud Infra Job Portal.

Monitors AWS WAFv2 Web ACLs across the same superset of AWS accounts
Billing by Service / Savings Plan Cost / Security Hub use
(billing_by_service.ACCOUNT_ALIAS) — for each account, lists every Web ACL
(both REGIONAL and CLOUDFRONT scope; CLOUDFRONT-scope ACLs only exist in
us-east-1 and are queried there regardless of the account's other
resources' region) and shows which resources (ALB/API Gateway/AppSync/
CloudFront distribution/Cognito user pool/Verified Access) each ACL is
associated with.

## Cost-conscious, click-to-load design

CloudWatch GetMetricData/GetMetricStatistics calls are NOT free at scale
(charged per metric per API call above the free tier), and
GetSampledRequests is comparatively "heavy" (it returns actual per-request
sample data, not an aggregate). To avoid firing a large fan-out of
CloudWatch calls every time the page loads or refreshes, this module
splits data into three cost tiers, each fetched only when the frontend
actually asks for it:

  1. list_accounts() / get_waf_summary() — STRUCTURAL ONLY. Lists every
     Web ACL, its associated resources, and its rule names/priorities/
     types via wafv2:ListWebACLs + GetWebACL + ListResourcesForWebACL.
     No CloudWatch calls at all. This is what renders on initial page
     load and every account/Web-ACL-list refresh.
  2. get_web_acl_metrics() — one Web ACL's aggregate ("Rule=ALL")
     Blocked/Counted/Allowed time series over a selected time range.
     Called only when a user expands a specific Web ACL card to view
     its "Metrics" chart (mirrors the CloudWatch console's own
     WebACL-level Browse/Graphed-metrics view). 3 CloudWatch calls.
  3. get_rule_metrics() — the same time series, but scoped to one
     specific rule inside a Web ACL. Called only when a user clicks
     into a rule's own detail view. 3 more CloudWatch calls.
  3b. get_account_action_breakdown() — for the "By Action" tab: the
     user picks ONE account and ONE action (Block/Allow/Count/CAPTCHA/
     Challenge) — no Web ACL picker. Every Web ACL already known to
     exist in that account (from the cached structural summary) is
     queried for just that single action, merged into one account-wide
     total + a combined "impacted rules" list (each entry tagged with
     which Web ACL it came from). One GetMetricData call per Web ACL in
     the account (not per rule, and not per action) minimizes
     round-trips. Called only when a user opens the "By Action" tab,
     picks an account, AND picks an action — never eagerly, never for
     all five actions at once.
  4. get_sampled_blocked_requests() — a live sample of ALL actions
     (BLOCK/ALLOW/COUNT/CAPTCHA/CHALLENGE) recorded for one specific
     rule (client IP, country, URI, matched sub-rule, action) via
     GetSampledRequests. This is the heaviest/most "log-like" call.
     Automatically loaded as soon as a user expands a rule's own
     metrics chart (see get_rule_metrics()) — no extra click required.
     IMPORTANT AWS LIMITATION: GetSampledRequests only ever covers
     roughly the last 3 hours (_SAMPLED_REQUESTS_WINDOW_HOURS) and
     returns at most ~500 samples per call, regardless of the chart's
     own selected time range — there is no way to get "any time"/full
     history from this specific API; that requires a separate WAF
     logging (Kinesis Firehose -> S3/CloudWatch Logs) pipeline instead.

## Cross-account access pattern

Identical to cost_explorer.py / billing_by_service.py / security_hub.py:
Core account (100000000001) is queried directly with its own static
IAM keys (Secrets Manager); every other account is reached via
sts:AssumeRole(PortalReadOnlyRole) using the Core account identity as the
calling principal. Structural fetches run in parallel via
ThreadPoolExecutor, and one account's failure (AssumeRole error, WAFv2
not used in that account, etc.) never blanks out any other account's
real data.

## Not every account/scope has a Web ACL

Most member accounts don't run their own ALB/CloudFront and simply have
no Web ACLs at all — this is a normal, expected state (`web_acls: []`),
not an error. A real fetch failure (AssumeRole denial, throttling, no
PortalReadOnlyRole in that account, etc.) is reported separately via the
`error` field so it's never confused with "this account just has none".

ENV=local: no real AWS calls are made — deterministic mock data is
returned instead, consistent with the other cost/security modules.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3

from .billing_by_service import ACCOUNT_ALIAS
from .cost_explorer import CostAnalysisError, _client_for_account, _core_session

logger = logging.getLogger(__name__)

ENV = os.environ.get("ENV", "local")

# WAFv2 is a regional API — REGIONAL-scope Web ACLs can live in any
# region, but this portal only has visibility into the regions its own
# resources actually run in. All known infra for this org lives in
# us-east-1, so that's the only region queried for REGIONAL scope too
# (mirrors every other cross-account module in this app, which is
# us-east-1-only throughout). CLOUDFRONT-scope Web ACLs are a global
# resource that AWS requires you to create/query from us-east-1
# specifically, regardless of where anything else runs.
_WAF_REGION = "us-east-1"

# Time-range presets for the on-demand metrics charts, mirroring the
# CloudWatch console's own WAF metrics view (screenshot reference:
# 1h/3h/12h/1d/3d/1w buttons). Period (seconds) is chosen per range so
# each chart renders a reasonable number of data points (well under
# CloudWatch's 1440-datapoint-per-call limit) without being so coarse
# the line looks flat.
_TIME_RANGE_PRESETS = {
    "1h": {"hours": 1, "period": 60},
    "3h": {"hours": 3, "period": 300},
    "12h": {"hours": 12, "period": 900},
    "1d": {"hours": 24, "period": 1800},
    "3d": {"hours": 72, "period": 3600},
    "1w": {"hours": 168, "period": 21600},
}
_DEFAULT_TIME_RANGE = "3h"
TIME_RANGE_CHOICES = tuple(_TIME_RANGE_PRESETS.keys())

# GetSampledRequests only returns matches from roughly the last 3 hours
# (AWS-enforced window, not configurable) and caps MaxItems at 500.
_SAMPLED_REQUESTS_WINDOW_HOURS = 3
_SAMPLED_REQUESTS_MAX_ITEMS = 100

_CACHE_TTL_SECONDS = 10 * 60  # 10 minutes — same convention as security_hub.py
_cache_lock = threading.Lock()
_cache: dict = {}
_inflight: dict = {}

# Resource-type label shown in the UI for each ARN pattern a Web ACL can
# be associated with — GetWebACLForResource doesn't exist for wafv2 (that
# call is one-directional: resource -> ACL, not ACL -> resources), so
# associated resources are discovered by listing every resource type a
# Web ACL CAN attach to and checking get_web_acl_for_resource() on each
# candidate found via that service's own list API.
_RESOURCE_TYPE_LABELS = {
    "elasticloadbalancing": "Application Load Balancer",
    "apigateway": "API Gateway",
    "appsync": "AppSync GraphQL API",
    "cognito-idp": "Cognito User Pool",
    "ec2": "Verified Access Instance",
    "cloudfront": "CloudFront Distribution",
}


def list_accounts() -> list[dict]:
    """Account picker data for the frontend — same superset of accounts
    Billing by Service / Savings Plan Cost / Security Hub use."""
    return [{"account_id": acct_id, "alias": alias} for acct_id, alias in ACCOUNT_ALIAS.items()]


def _resource_type_label(resource_arn: str) -> str:
    for key, label in _RESOURCE_TYPE_LABELS.items():
        if f":{key}:" in resource_arn:
            return label
    return "Other resource"


def _web_acl_arn(account_id: str, scope: str, web_acl_name: str, web_acl_id: str) -> str:
    """Reconstruct a Web ACL's ARN from its name/id/scope — needed by
    get_sampled_blocked_requests(), which the ListWebACLs/GetWebACL
    structural fetch doesn't carry all the way through to that on-demand
    call. Matches the real AWS ARN format for each scope:
      REGIONAL:   arn:aws:wafv2:<region>:<account>:regional/webacl/<name>/<id>
      CLOUDFRONT: arn:aws:wafv2:<region>:<account>:global/webacl/<name>/<id>
    (CLOUDFRONT-scope ACLs use the "global" scope segment, not
    "cloudfront" — confirmed against this app's own CloudFront WebACL
    ARN, e.g. .../global/webacl/job-portal-cloudfront-waf/<id>.)"""
    scope_segment = "global" if scope == "CLOUDFRONT" else "regional"
    return f"arn:aws:wafv2:{_WAF_REGION}:{account_id}:{scope_segment}/webacl/{web_acl_name}/{web_acl_id}"


def _list_web_acls_for_scope(wafv2_client, scope: str) -> list[dict]:
    acls: list[dict] = []
    next_marker = None
    while True:
        kwargs = {"Scope": scope}
        if next_marker:
            kwargs["NextMarker"] = next_marker
        resp = wafv2_client.list_web_acls(**kwargs)
        acls.extend(resp.get("WebACLs", []))
        next_marker = resp.get("NextMarker")
        if not next_marker:
            break
    return acls


def _find_associated_resources(wafv2_client, web_acl_arn: str, scope: str, cloudfront_client=None) -> list[dict]:
    """Best-effort discovery of which resources a Web ACL is attached to.

    REGIONAL scope: wafv2.list_resources_for_web_acl() directly returns
    every associated ALB/API Gateway/AppSync/Cognito/Verified-Access ARN.

    CLOUDFRONT scope: that same call does NOT support CLOUDFRONT scope
    (AWS API limitation) — a CloudFront distribution's Web ACL
    association is only discoverable by reading each distribution's own
    config (cloudfront.get_distribution / list_distributions), so this
    lists distributions and matches by WebACLId when a CloudFront client
    is available."""
    resources: list[dict] = []
    if scope == "REGIONAL":
        try:
            resp = wafv2_client.list_resources_for_web_acl(WebACLArn=web_acl_arn)
            for arn in resp.get("ResourceArns", []):
                resources.append({"arn": arn, "type": _resource_type_label(arn)})
        except Exception as e:
            logger.warning("waf_monitoring: list_resources_for_web_acl failed for %s: %s", web_acl_arn, e)
        return resources

    # CLOUDFRONT scope
    if cloudfront_client is None:
        return resources
    try:
        paginator = cloudfront_client.get_paginator("list_distributions")
        for page in paginator.paginate():
            items = ((page.get("DistributionList") or {}).get("Items")) or []
            for dist in items:
                if dist.get("WebACLId") == web_acl_arn:
                    resources.append({
                        "arn": dist.get("ARN"),
                        "type": "CloudFront Distribution",
                        "extra": dist.get("DomainName"),
                    })
    except Exception as e:
        logger.warning("waf_monitoring: could not list CloudFront distributions for %s: %s", web_acl_arn, e)
    return resources


def _fetch_web_acl_detail(wafv2_client, web_acl_summary: dict, scope: str, cloudfront_client) -> dict:
    """Structural-only Web ACL detail: which resources it's attached to
    and its rule list (name/priority/type/action). Deliberately makes NO
    CloudWatch calls — see module docstring's cost-tier design. Metrics
    are fetched separately, on demand, via get_web_acl_metrics() /
    get_rule_metrics()."""
    name = web_acl_summary.get("Name")
    arn = web_acl_summary.get("ARN")
    acl_id = web_acl_summary.get("Id")

    detail: dict = {
        "id": acl_id,
        "name": name,
        "arn": arn,
        "scope": scope,
        "description": web_acl_summary.get("Description"),
        "associated_resources": [],
        "rules": [],
        "error": None,
    }

    try:
        get_resp = wafv2_client.get_web_acl(Name=name, Scope=scope, Id=acl_id)
        full_acl = get_resp.get("WebACL", {})
    except Exception as e:
        detail["error"] = f"Could not fetch Web ACL details: {str(e)[:200]}"
        return detail

    detail["associated_resources"] = _find_associated_resources(wafv2_client, arn, scope, cloudfront_client)
    detail["default_action"] = "ALLOW" if "Allow" in (full_acl.get("DefaultAction") or {}) else "BLOCK"

    rules = full_acl.get("Rules") or []
    for rule in rules:
        rule_name = rule.get("Name")
        statement = rule.get("Statement") or {}
        if "ManagedRuleGroupStatement" in statement:
            rule_type = "AWS Managed Rule Group"
            rule_action = "Managed (per sub-rule)"
        elif "RateBasedStatement" in statement:
            rule_type = "Rate-based rule"
            action = rule.get("Action") or {}
            rule_action = next(iter(action.keys()), "—").upper() if action else "COUNT (override)"
        else:
            rule_type = "Custom rule"
            action = rule.get("Action") or {}
            rule_action = next(iter(action.keys()), "—").upper() if action else "COUNT (override)"

        detail["rules"].append({
            "name": rule_name,
            "priority": rule.get("Priority"),
            "type": rule_type,
            "action": rule_action,
        })

    # Sort rules by priority so the frontend shows evaluation order.
    detail["rules"].sort(key=lambda r: (r["priority"] if r["priority"] is not None else 9999))
    return detail


def _fetch_account_waf(account_id: str, alias: str, core_session: boto3.Session) -> dict:
    row: dict = {
        "account_id": account_id,
        "alias": alias,
        "error": None,
        "web_acls": [],
    }
    try:
        wafv2_client = _client_for_account(account_id, "wafv2", core_session)
        cloudfront_client = _client_for_account(account_id, "cloudfront", core_session)
    except Exception as e:
        row["error"] = f"Could not access account: {str(e)[:200]}"
        return row

    web_acl_summaries: list[tuple[dict, str]] = []
    for scope in ("REGIONAL", "CLOUDFRONT"):
        try:
            for summary in _list_web_acls_for_scope(wafv2_client, scope):
                web_acl_summaries.append((summary, scope))
        except Exception as e:
            # A single scope failing (e.g. this account/role can't list
            # CLOUDFRONT-scope ACLs for some reason) shouldn't blank out
            # the other scope's real results.
            logger.warning("waf_monitoring: list_web_acls(%s) failed for %s: %s", scope, account_id, e)

    if not web_acl_summaries:
        return row

    details: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(_fetch_web_acl_detail, wafv2_client, summary, scope, cloudfront_client): summary
            for summary, scope in web_acl_summaries
        }
        for fut in as_completed(futures):
            details.append(fut.result())

    details.sort(key=lambda d: (d.get("name") or ""))
    row["web_acls"] = details
    return row


def _region_dimension(scope: str) -> list[dict]:
    """CloudWatch AWS/WAFV2 metrics for CLOUDFRONT-scope Web ACLs do NOT
    carry a Region dimension at all — only REGIONAL-scope metrics do
    (confirmed via a live ListMetrics check against this app's own
    CloudFront-scope Web ACL). Including a Region dimension on a
    CLOUDFRONT-scope GetMetricStatistics call doesn't error — it just
    silently returns zero datapoints, which would otherwise look
    indistinguishable from "this rule never matched anything"."""
    if scope == "REGIONAL":
        return [{"Name": "Region", "Value": _WAF_REGION}]
    return []


def _time_range_bounds(time_range: str) -> tuple[datetime, datetime, int]:
    preset = _TIME_RANGE_PRESETS.get(time_range, _TIME_RANGE_PRESETS[_DEFAULT_TIME_RANGE])
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=preset["hours"])
    return start, end, preset["period"]


def _fetch_metric_series(cw_client, web_acl_name: str, rule_name: str, scope: str, time_range: str) -> dict:
    """Core CloudWatch fetcher shared by get_web_acl_metrics() (rule_name
    = 'ALL', WAF's own reserved name for the Web-ACL-level aggregate) and
    get_rule_metrics() (rule_name = an actual rule's metric name).
    Returns a zero-filled, period-aligned time series so the chart draws
    a continuous line instead of only plotting buckets that had traffic."""
    start, end, period = _time_range_bounds(time_range)
    dimensions = [
        {"Name": "WebACL", "Value": web_acl_name},
        {"Name": "Rule", "Value": rule_name},
        *_region_dimension(scope),
    ]

    raw_by_metric: dict[str, dict[str, int]] = {"BlockedRequests": {}, "CountedRequests": {}, "AllowedRequests": {}}
    for metric_name in raw_by_metric:
        try:
            resp = cw_client.get_metric_statistics(
                Namespace="AWS/WAFV2",
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=start,
                EndTime=end,
                Period=period,
                Statistics=["Sum"],
            )
            for dp in resp.get("Datapoints", []):
                bucket = dp["Timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ")
                raw_by_metric[metric_name][bucket] = raw_by_metric[metric_name].get(bucket, 0) + int(dp.get("Sum") or 0)
        except Exception as e:
            logger.warning(
                "waf_monitoring: get_metric_statistics failed for %s/rule=%s (%s): %s",
                web_acl_name, rule_name, metric_name, e,
            )

    buckets: list[datetime] = []
    cursor = start
    while cursor < end:
        buckets.append(cursor)
        cursor += timedelta(seconds=period)

    series: list[dict] = []
    for b in buckets:
        key = b.strftime("%Y-%m-%dT%H:%M:%SZ")
        series.append({
            "timestamp": key,
            "blocked": raw_by_metric["BlockedRequests"].get(key, 0),
            "counted": raw_by_metric["CountedRequests"].get(key, 0),
            "allowed": raw_by_metric["AllowedRequests"].get(key, 0),
        })

    return {
        "time_range": time_range,
        "period_seconds": period,
        "series": series,
        "blocked_total": sum(p["blocked"] for p in series),
        "counted_total": sum(p["counted"] for p in series),
        "allowed_total": sum(p["allowed"] for p in series),
        "is_mock": False,
    }


def get_web_acl_metrics(account_id: str, web_acl_name: str, web_acl_id: str, scope: str,
                         time_range: str = _DEFAULT_TIME_RANGE) -> dict:
    """Web-ACL-level aggregate ("Rule=ALL") Blocked/Counted/Allowed time
    series — the "Metrics" chart shown when a user expands a Web ACL
    card. Fetched on demand only; never part of the structural summary.
    `web_acl_id` isn't needed by CloudWatch (metrics key on the Web ACL's
    NAME, not its ID) but is accepted for call-signature symmetry with
    the other per-ACL endpoints."""
    if ENV == "local":
        return _mock_metric_series("ALL", time_range)

    if account_id not in ACCOUNT_ALIAS:
        raise CostAnalysisError(f"Unknown AWS account_id: {account_id}")

    core_session = _core_session()
    cw_client = _client_for_account(account_id, "cloudwatch", core_session)
    return _fetch_metric_series(cw_client, web_acl_name, "ALL", scope, time_range)


def get_rule_metrics(account_id: str, web_acl_name: str, web_acl_id: str, scope: str, rule_name: str,
                      time_range: str = _DEFAULT_TIME_RANGE) -> dict:
    """Same time series as get_web_acl_metrics(), scoped to ONE specific
    rule — fetched on demand only when a user clicks into that rule's
    detail view."""
    if ENV == "local":
        return _mock_metric_series(rule_name, time_range)

    if account_id not in ACCOUNT_ALIAS:
        raise CostAnalysisError(f"Unknown AWS account_id: {account_id}")

    core_session = _core_session()
    cw_client = _client_for_account(account_id, "cloudwatch", core_session)
    return _fetch_metric_series(cw_client, web_acl_name, rule_name, scope, time_range)


# Maps this module's own snake_case action keys to the actual AWS/WAFV2
# CloudWatch metric name for that action. CaptchaRequests/ChallengeRequests
# weren't fetched by get_web_acl_metrics()/get_rule_metrics() (those only
# ever needed Blocked/Counted/Allowed for their line chart) — the "By
# Action" tab is the first place in this module that needs all five.
_ACTION_METRICS = {
    "blocked": "BlockedRequests",
    "allowed": "AllowedRequests",
    "counted": "CountedRequests",
    "captcha": "CaptchaRequests",
    "challenge": "ChallengeRequests",
}
# Valid `action` query-param values for get_action_breakdown()/the
# /action-breakdown endpoint — the frontend's "By Action" tab requires
# the user to pick exactly one of these before anything is fetched.
ACTION_CHOICES = tuple(_ACTION_METRICS.keys())
# GetMetricData accepts at most 500 MetricDataQueries per call.
_GET_METRIC_DATA_BATCH_SIZE = 500


def _fetch_action_breakdown(cw_client, web_acl_name: str, rule_names: list[str], scope: str, time_range: str,
                             action: Optional[str] = None) -> dict:
    """For the "By Action" tab: totals for ONE action (or, if `action` is
    omitted, all five actions — kept for flexibility/back-compat) for one
    Web ACL over a selected time range, broken down by which rule(s)
    contributed ("impacted rules"). Every rule x action combination is
    batched into as few GetMetricData calls as possible (chunks of up to
    500 queries each) instead of one GetMetricStatistics call per rule
    per action. Scoping to a single `action` (the normal case now that
    the frontend always picks one action before fetching) cuts the
    number of metrics requested — and therefore the CloudWatch cost —
    down to 1/5th of what querying all five actions would cost for the
    same Web ACL."""
    start, end, period = _time_range_bounds(time_range)
    region_dims = _region_dimension(scope)
    metrics_to_query = {action: _ACTION_METRICS[action]} if action else _ACTION_METRICS

    queries: list[dict] = []
    query_id_map: dict[str, tuple[str, str]] = {}
    counter = 0
    for rule_name in rule_names:
        for action_key, metric_name in metrics_to_query.items():
            qid = f"q{counter}"
            counter += 1
            query_id_map[qid] = (rule_name, action_key)
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/WAFV2",
                        "MetricName": metric_name,
                        "Dimensions": [
                            {"Name": "WebACL", "Value": web_acl_name},
                            {"Name": "Rule", "Value": rule_name},
                            *region_dims,
                        ],
                    },
                    "Period": period,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            })

    totals_by_qid: dict[str, int] = {}
    for batch_start in range(0, len(queries), _GET_METRIC_DATA_BATCH_SIZE):
        batch = queries[batch_start:batch_start + _GET_METRIC_DATA_BATCH_SIZE]
        if not batch:
            continue
        try:
            resp = cw_client.get_metric_data(MetricDataQueries=batch, StartTime=start, EndTime=end)
            for result in resp.get("MetricDataResults", []):
                totals_by_qid[result["Id"]] = int(sum(result.get("Values") or []))
        except Exception as e:
            logger.warning("waf_monitoring: get_metric_data failed for %s (batch of %d): %s", web_acl_name, len(batch), e)

    actions: dict[str, dict] = {key: {"total": 0, "rules": []} for key in metrics_to_query}
    for qid, (rule_name, action_key) in query_id_map.items():
        count = totals_by_qid.get(qid, 0)
        if count > 0:
            actions[action_key]["rules"].append({"rule_name": rule_name, "count": count})
            actions[action_key]["total"] += count

    for action_key in actions:
        actions[action_key]["rules"].sort(key=lambda r: -r["count"])

    return {
        "web_acl_name": web_acl_name,
        "time_range": time_range,
        "period_seconds": period,
        "actions": actions,
        "is_mock": False,
    }


def get_account_action_breakdown(account_id: str, action: str, time_range: str = _DEFAULT_TIME_RANGE) -> dict:
    """Public entry point for the "By Action" tab, ACCOUNT-LEVEL: the
    user picks one AWS account and one action (Block/Allow/Count/CAPTCHA/
    Challenge) — NO Web ACL picker. Every Web ACL that already exists in
    that account (per the structural summary — reuses
    get_waf_summary()'s own 10-minute cache + single-flight semantics
    rather than re-fetching the Web-ACL/rule list itself) is queried for
    the chosen action, and results are merged into one account-wide
    total plus a combined "impacted rules" list (each entry tagged with
    which Web ACL it came from, since two different Web ACLs commonly
    reuse the same AWS-managed rule group name — e.g.
    AWSManagedRulesCommonRuleSet — and merging them without that tag
    would make it look like one rule silently accounted for both ACLs'
    hits).

    One GetMetricData call per Web ACL in the account (not per rule) is
    made, each scoped to the single selected action, so the cost of this
    endpoint scales with "how many Web ACLs does this account have" —
    typically 1-3 — not with total rule count. Fetched on demand only
    when a user opens the "By Action" tab, picks an account, and picks
    an action — never as part of the structural summary, never for all
    five actions at once, and never eagerly."""
    if action not in _ACTION_METRICS:
        raise CostAnalysisError(f"action must be one of {ACTION_CHOICES}")

    if ENV == "local":
        return _mock_account_action_breakdown(account_id, action, time_range)

    if account_id not in ACCOUNT_ALIAS:
        raise CostAnalysisError(f"Unknown AWS account_id: {account_id}")

    summary = get_waf_summary([account_id])
    accounts = summary.get("accounts") or []
    account_row = accounts[0] if accounts else None
    if account_row is None:
        raise CostAnalysisError(f"Could not fetch Web ACL structural data for account {account_id}")
    if account_row.get("error"):
        raise CostAnalysisError(f"Could not fetch Web ACL structural data for account {account_id}: {account_row['error']}")

    web_acls = account_row.get("web_acls") or []
    default_period = _TIME_RANGE_PRESETS.get(time_range, _TIME_RANGE_PRESETS[_DEFAULT_TIME_RANGE])["period"]
    if not web_acls:
        return {
            "account_id": account_id, "action": action, "time_range": time_range,
            "period_seconds": default_period, "total": 0, "rules": [], "web_acl_count": 0, "is_mock": False,
        }

    core_session = _core_session()
    cw_client = _client_for_account(account_id, "cloudwatch", core_session)

    merged_rules: list[dict] = []
    total = 0
    period_seconds = default_period
    for acl in web_acls:
        rule_names = [r["name"] for r in (acl.get("rules") or [])]
        if not rule_names:
            continue
        acl_result = _fetch_action_breakdown(cw_client, acl["name"], rule_names, acl["scope"], time_range, action)
        period_seconds = acl_result["period_seconds"]
        action_data = acl_result["actions"].get(action, {"total": 0, "rules": []})
        total += action_data["total"]
        for r in action_data["rules"]:
            # web_acl_id/scope are carried through (not just web_acl_name)
            # so the frontend can click a rule name here and immediately
            # call get_sampled_blocked_requests() for that exact
            # Web ACL/rule pair — GetSampledRequests needs the Web ACL's
            # ARN (built from id+scope, see _web_acl_arn()), not just its
            # name.
            merged_rules.append({
                "web_acl_name": acl["name"], "web_acl_id": acl["id"], "scope": acl["scope"],
                "rule_name": r["rule_name"], "count": r["count"],
            })

    merged_rules.sort(key=lambda r: -r["count"])

    return {
        "account_id": account_id,
        "action": action,
        "time_range": time_range,
        "period_seconds": period_seconds,
        "total": total,
        "rules": merged_rules,
        "web_acl_count": len(web_acls),
        "is_mock": False,
    }


def get_sampled_blocked_requests(account_id: str, web_acl_name: str, web_acl_id: str, scope: str, rule_name: str) -> dict:
    """Live sample of requests that recently matched one specific rule
    inside one Web ACL, ACROSS EVERY ACTION (BLOCK, ALLOW, COUNT,
    CAPTCHA, CHALLENGE) — AWS's GetSampledRequests doesn't accept an
    action filter, so this always returns every action WAF recorded for
    the rule and lets the caller decide what to show/filter (the
    frontend used to filter this down to BLOCK-only client-side; it no
    longer does, so all actions are visible together with an `action`
    column per sample).

    HARD AWS LIMITATION — cannot be worked around from this API: AWS
    only lets GetSampledRequests look back roughly the last 3 hours
    (_SAMPLED_REQUESTS_WINDOW_HOURS), and returns at most ~500 raw
    samples per call (this module asks for at most
    _SAMPLED_REQUESTS_MAX_ITEMS). There is no parameter to extend this
    to "any time"/full history — AWS does not retain a queryable
    superset anywhere else for this specific API. Older-than-3h or
    fully-exhaustive (non-sampled) history requires a completely
    separate feature: enabling WAF logging (Kinesis Data Firehose -> S3
    or CloudWatch Logs) and querying that log store instead — a
    different data path with its own ongoing storage cost, not
    something GetSampledRequests can ever provide."""
    if ENV == "local":
        return _mock_sampled_requests(rule_name)

    if account_id not in ACCOUNT_ALIAS:
        raise CostAnalysisError(f"Unknown AWS account_id: {account_id}")

    core_session = _core_session()
    wafv2_client = _client_for_account(account_id, "wafv2", core_session)

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=_SAMPLED_REQUESTS_WINDOW_HOURS)

    kwargs = {
        "WebAclArn": _web_acl_arn(account_id, scope, web_acl_name, web_acl_id),
        "RuleMetricName": rule_name,
        "Scope": scope,
        "TimeWindow": {"StartTime": start, "EndTime": end},
        "MaxItems": _SAMPLED_REQUESTS_MAX_ITEMS,
    }
    try:
        resp = wafv2_client.get_sampled_requests(**kwargs)
    except Exception as e:
        raise CostAnalysisError(f"Could not fetch sampled requests: {str(e)[:300]}") from e

    samples: list[dict] = []
    for s in resp.get("SampledRequests", []):
        req = s.get("Request") or {}
        headers = {h.get("Name", "").lower(): h.get("Value") for h in (req.get("Headers") or [])}
        samples.append({
            "timestamp": s.get("Timestamp").strftime("%Y-%m-%dT%H:%M:%SZ") if s.get("Timestamp") else None,
            "client_ip": req.get("ClientIP"),
            "country": req.get("Country"),
            "uri": req.get("URI"),
            "method": req.get("Method"),
            "action": s.get("Action"),
            "rule_name_within_rule_group": s.get("RuleNameWithinRuleGroup"),
            "host": headers.get("host"),
            "user_agent": headers.get("user-agent"),
        })

    return {
        "web_acl_name": web_acl_name,
        "rule_name": rule_name,
        "window_hours": _SAMPLED_REQUESTS_WINDOW_HOURS,
        "population_size": resp.get("PopulationSize"),
        "samples": samples,
        "is_mock": False,
    }


def _mock_metric_series(rule_name: str, time_range: str) -> dict:
    start, end, period = _time_range_bounds(time_range)
    buckets: list[datetime] = []
    cursor = start
    while cursor < end:
        buckets.append(cursor)
        cursor += timedelta(seconds=period)

    series: list[dict] = []
    for i, b in enumerate(buckets):
        spike = 15 if i == max(1, len(buckets) // 2) else 0
        series.append({
            "timestamp": b.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "blocked": 3 + (i % 6) * 2 + spike,
            "counted": 1 + (i % 4),
            "allowed": 18 + (i % 9) * 3,
        })

    return {
        "time_range": time_range,
        "period_seconds": period,
        "series": series,
        "blocked_total": sum(p["blocked"] for p in series),
        "counted_total": sum(p["counted"] for p in series),
        "allowed_total": sum(p["allowed"] for p in series),
        "is_mock": True,
    }


def _mock_account_action_breakdown(account_id: str, action: str, time_range: str) -> dict:
    """Mock counterpart of get_account_action_breakdown() — deterministic
    (no randomness) so the same account/action/time_range always renders
    the same numbers in local dev mode. Simulates an account with 2 Web
    ACLs, each contributing rules to the merged breakdown, tagged with
    which Web ACL they came from (matching the real function's output
    shape)."""
    _, _, period = _time_range_bounds(time_range)
    action_index = ACTION_CHOICES.index(action) if action in ACTION_CHOICES else 0
    mock_web_acls = [
        {"name": "job-portal-alb-waf", "id": "mock-acl-1", "scope": "REGIONAL", "rules": ["AWSManagedRulesCommonRuleSet", "AWSManagedRulesKnownBadInputsRuleSet", "RateLimitRule"]},
        {"name": "job-portal-cloudfront-waf", "id": "mock-acl-2", "scope": "CLOUDFRONT", "rules": ["AWSManagedRulesCommonRuleSet", "GeoBlockRule"]},
    ]
    rules: list[dict] = []
    for i, acl in enumerate(mock_web_acls):
        for j, rule_name in enumerate(acl["rules"]):
            # Deterministic-but-varied counts so every action/rule combo
            # in mock mode looks plausible without any randomness.
            count = (action_index + 1) * (i + 1) * (j + 1) * 7
            if count > 0:
                rules.append({
                    "web_acl_name": acl["name"], "web_acl_id": acl["id"], "scope": acl["scope"],
                    "rule_name": rule_name, "count": count,
                })
    rules.sort(key=lambda r: -r["count"])

    return {
        "account_id": account_id,
        "action": action,
        "time_range": time_range,
        "period_seconds": period,
        "total": sum(r["count"] for r in rules),
        "rules": rules,
        "web_acl_count": len(mock_web_acls),
        "is_mock": True,
    }


def _mock_sampled_requests(rule_name: str) -> dict:
    now = datetime.now(timezone.utc)
    # Mixed actions (BLOCK/ALLOW/COUNT/CAPTCHA/CHALLENGE) so mock mode
    # exercises the same multi-action rendering as real GetSampledRequests
    # data, instead of only ever showing BLOCK.
    actions = ["BLOCK", "ALLOW", "COUNT", "CAPTCHA", "CHALLENGE", "BLOCK", "ALLOW", "BLOCK"]
    return {
        "web_acl_name": "job-portal-alb-waf",
        "rule_name": rule_name,
        "window_hours": _SAMPLED_REQUESTS_WINDOW_HOURS,
        "population_size": 5000,
        "samples": [
            {
                "timestamp": (now - timedelta(minutes=i * 7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "client_ip": f"203.0.113.{10 + i}",
                "country": ["US", "GB", "DE", "HK", "IT"][i % 5],
                "uri": ["/wp-login.php", "/.env", "/admin/config.php", "/api/health", "/xmlrpc.php"][i % 5],
                "method": "GET",
                "action": actions[i % len(actions)],
                "rule_name_within_rule_group": "AnonymousIpList#HostingProviderIPList",
                "host": "portal.example.com",
                "user_agent": "Mozilla/5.0 (mock)",
            }
            for i in range(8)
        ],
        "is_mock": True,
    }


def _mock_summary() -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    accounts = [
        {
            "account_id": "100000000002", "alias": "it-core-sandbox", "error": None,
            "web_acls": [
                {
                    "id": "mock-acl-1", "name": "job-portal-alb-waf", "arn": "arn:aws:wafv2:us-east-1:100000000002:regional/webacl/job-portal-alb-waf/mock",
                    "scope": "REGIONAL", "description": None, "default_action": "ALLOW", "error": None,
                    "associated_resources": [
                        {"arn": "arn:aws:elasticloadbalancing:us-east-1:100000000002:loadbalancer/app/JobPor-JobPo-mock/abc123", "type": "Application Load Balancer"},
                    ],
                    "rules": [
                        {"name": "AWSManagedRulesCommonRuleSet", "priority": 1, "type": "AWS Managed Rule Group", "action": "Managed (per sub-rule)"},
                        {"name": "AWSManagedRulesKnownBadInputsRuleSet", "priority": 2, "type": "AWS Managed Rule Group", "action": "Managed (per sub-rule)"},
                    ],
                },
            ],
        },
        {
            "account_id": "100000000001", "alias": "core", "error": None,
            "web_acls": [],
        },
    ]
    return {
        "accounts": accounts,
        "totals": {
            "account_count": len(accounts),
            "accounts_with_waf_count": sum(1 for a in accounts if a["web_acls"]),
            "web_acl_count": sum(len(a["web_acls"]) for a in accounts),
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
    except CostAnalysisError:
        raise
    except Exception as e:
        raise CostAnalysisError(f"Could not load Core account credentials: {e}") from e

    accounts_out: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_account_waf, acct_id, ACCOUNT_ALIAS.get(acct_id, acct_id), core_session): acct_id
            for acct_id in account_ids
        }
        for fut in as_completed(futures):
            accounts_out.append(fut.result())
    accounts_out.sort(key=lambda a: account_ids.index(a["account_id"]))

    accounts_with_waf = 0
    web_acl_count = 0
    error_count = 0
    for a in accounts_out:
        if a["error"]:
            error_count += 1
            continue
        if a["web_acls"]:
            accounts_with_waf += 1
        web_acl_count += len(a["web_acls"])

    return {
        "accounts": accounts_out,
        "totals": {
            "account_count": len(accounts_out),
            "accounts_with_waf_count": accounts_with_waf,
            "web_acl_count": web_acl_count,
            "error_count": error_count,
        },
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "is_mock": False,
    }


def get_waf_summary(account_ids: list[str], force_refresh: bool = False) -> dict:
    """Fetch (or serve cached) WAF Rule Monitoring STRUCTURAL summary
    (Web ACLs + linked resources + rule names — no CloudWatch calls) for
    every account_id in `account_ids`. Cached per exact account-set for
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
        inflight.wait(timeout=180)
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
