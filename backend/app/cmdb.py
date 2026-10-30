"""
CMDB (Configuration Management Database) — a live inventory of AWS
hardware/software assets (EC2 instances, RDS databases, Lambda functions,
S3 buckets, DynamoDB tables, Elastic Load Balancers) across the whole the organization
AWS Organization.

Reuses the job portal's existing cross-account access pattern from
cost_explorer.py: Core account (100000000001) is queried directly with
its own static keys, and every other member account is reached via
sts:AssumeRole(PortalReadOnlyRole) using the Core account identity — no new IAM
setup required (PortalReadOnlyRole already grants ec2:Describe*,
rds:Describe*, lambda:List*, s3:List*, dynamodb:List*/Describe*, and
elasticloadbalancing:Describe* in every member account, confirmed by a
live read-only probe before this module was written).

Resource types covered ("All" scope, per user request):
  - EC2 instances
  - RDS database instances
  - Lambda functions
  - S3 buckets
  - DynamoDB tables
  - Elastic Load Balancers (ALB/NLB/CLB, via elbv2 + classic elb)

Regions: each account is scanned across every region returned by
ec2:DescribeRegions for that account (falls back to a short static list
if that call itself fails), so resources aren't missed just because
they're outside ap-southeast-1/us-east-1.

Caching: identical 10-minute in-process TTL cache pattern used by
cost_explorer.py / billing_by_service.py — cross-account Describe/List
calls fan out across every region of every account, so re-fetching on
every page load would be slow and (for a few services) rate-limited.

History / change tracking (Task 9 requirement #4): every successful
fetch is diffed against the last known snapshot for that account (stored
in DynamoDB in production / SQLite locally, see cmdb_store.py) and any
added/removed/status-changed resource is appended to an audit trail,
without ever blocking or slowing down the read path itself.

AI impact analysis (Task 9 requirement #5): analyse_resource_impact()
asks Bedrock what would break if a given resource were to go down/be
deleted, based purely on the resource's own metadata (type, name, tags,
region, dependents inferred from tags) — never touches or modifies any
AWS resource.

ENV=local: no real AWS calls — a small deterministic mock inventory is
returned instead (see _mock_inventory()), same approach the rest of this
feature area uses in local/dev mode.
"""

from __future__ import annotations

import copy
import ipaddress
import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3

from .cost_explorer import (
    ACCOUNTS,
    CORE_ACCOUNT_ID,
    READONLY_ROLE_NAME,
    _client_for_account,
    _core_session,
    CostAnalysisError,
)

# _client_for_account is re-exported (used by _regions_for_account, which
# only ever needs the default us-east-1-scoped client for the global
# ec2:DescribeRegions call — no per-region client needed there).

logger = logging.getLogger(__name__)

ENV = os.environ.get("ENV", "local")

# Fallback region list used only if ec2:DescribeRegions itself fails for
# an account (extremely unlikely for a read-only call, but a network blip
# shouldn't take the whole account's inventory to zero) — a generic default;
# adjust to the regions your organisation actually runs workloads in.
_FALLBACK_REGIONS = ["ap-southeast-1", "ap-southeast-2", "us-east-1"]

# Resource type labels, used consistently across backend rows, DynamoDB
# history events, and the frontend table's "Type" column.
RESOURCE_TYPE_EC2 = "EC2 Instance"
RESOURCE_TYPE_RDS = "RDS Instance"
RESOURCE_TYPE_LAMBDA = "Lambda Function"
RESOURCE_TYPE_S3 = "S3 Bucket"
RESOURCE_TYPE_DYNAMODB = "DynamoDB Table"
RESOURCE_TYPE_ELB = "Load Balancer"


class CmdbTopologyError(Exception):
    """Raised when an EC2 topology lookup can't proceed at all (e.g. the
    instance ID doesn't exist in the given account/region). This is
    distinct from the many individual AWS-call failures inside
    get_ec2_topology (flow-log lookup, Logs Insights query, CloudWatch
    metrics) that are already caught internally and simply degrade the
    diagram (fewer peers / no animated traffic) instead of raising —
    those must never take down the whole topology view just because one
    optional data source (e.g. Flow Logs) isn't available for this
    particular VPC."""


_CACHE_TTL_SECONDS = 10 * 60  # 10 minutes
_cache_lock = threading.Lock()
_cache: dict = {"inventory": None, "fetched_at": 0.0, "last_good": None, "last_good_at": 0.0}
# True while a background org-wide fetch is running. Unlike a
# threading.Event, nothing ever blocks/waits on this — it's purely a
# single-flight guard so concurrent requests don't kick off duplicate
# multi-minute fetches. See get_cmdb_inventory()'s docstring for why this
# module no longer lets any HTTP request thread block on the fetch itself.
_fetch_in_progress: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tags_to_dict(tag_list) -> dict:
    """Normalize the handful of different {Key,Value} tag list shapes AWS
    SDKs return (EC2/RDS/ELB use 'Key'/'Value', Lambda/DynamoDB/S3 use a
    plain dict already or a ListTagsForResource response) into one flat
    {key: value} dict. None/empty input -> {}."""
    if not tag_list:
        return {}
    if isinstance(tag_list, dict):
        return dict(tag_list)
    out = {}
    for t in tag_list:
        k = t.get("Key")
        if k is not None:
            out[k] = t.get("Value")
    return out


def _norm(resource_type: str, resource_id: str, name: Optional[str], region: str, status: Optional[str], tags: dict, extra: Optional[dict] = None) -> dict:
    """One normalized resource row, the shape every _list_* function below
    returns and the shape stored in CmdbSnapshot / returned to the API."""
    row = {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "name": name or resource_id,
        "region": region,
        "status": status,
        "tags": tags or {},
    }
    if extra:
        row["extra"] = extra
    return row


# ── Per-service resource listers ─────────────────────────────────────────
# Each function takes an already-constructed boto3 client scoped to one
# account+region (via _client_for_account) and returns a list of
# normalized rows for that one service/region. Any AWS error (including
# a service not being enabled/available in that region, or a region this
# role can't see) is left to the caller to catch — these functions assume
# a working client.

def _list_ec2_instances(ec2, region: str) -> list[dict]:
    rows = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate():
        for reservation in page.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                tags = _tags_to_dict(inst.get("Tags"))
                rows.append(_norm(
                    RESOURCE_TYPE_EC2,
                    inst["InstanceId"],
                    tags.get("Name"),
                    region,
                    (inst.get("State") or {}).get("Name"),
                    tags,
                    extra={
                        "instance_type": inst.get("InstanceType"),
                        "private_ip": inst.get("PrivateIpAddress"),
                        "platform": inst.get("PlatformDetails"),
                    },
                ))
    return rows


def _list_rds_instances(rds, region: str) -> list[dict]:
    rows = []
    paginator = rds.get_paginator("describe_db_instances")
    for page in paginator.paginate():
        for db in page.get("DBInstances", []):
            tags = _tags_to_dict(db.get("TagList"))
            rows.append(_norm(
                RESOURCE_TYPE_RDS,
                db["DBInstanceIdentifier"],
                db["DBInstanceIdentifier"],
                region,
                db.get("DBInstanceStatus"),
                tags,
                extra={
                    "engine": db.get("Engine"),
                    "instance_class": db.get("DBInstanceClass"),
                    "multi_az": db.get("MultiAZ"),
                },
            ))
    return rows


def _list_lambda_functions(lam, region: str) -> list[dict]:
    rows = []
    paginator = lam.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page.get("Functions", []):
            rows.append(_norm(
                RESOURCE_TYPE_LAMBDA,
                fn["FunctionName"],
                fn["FunctionName"],
                region,
                fn.get("State", "Active"),
                {},  # Lambda tags need a separate ListTags call per function — skipped for list-scale performance
                extra={
                    "runtime": fn.get("Runtime"),
                    "memory_mb": fn.get("MemorySize"),
                    "last_modified": fn.get("LastModified"),
                },
            ))
    return rows


def _list_s3_buckets(s3, region: str) -> list[dict]:
    """S3 is a global service (one bucket list per account, not per
    region) — callers should only invoke this once per account, using
    whichever region's client was already constructed; `region` here is
    just the label attached to each row (S3 buckets don't have a
    meaningful "home region" in the list_buckets response itself)."""
    rows = []
    resp = s3.list_buckets()
    for b in resp.get("Buckets", []):
        rows.append(_norm(
            RESOURCE_TYPE_S3,
            b["Name"],
            b["Name"],
            "global",
            "active",
            {},
            extra={"created": b.get("CreationDate").isoformat() if b.get("CreationDate") else None},
        ))
    return rows


def _list_dynamodb_tables(ddb, region: str) -> list[dict]:
    rows = []
    paginator = ddb.get_paginator("list_tables")
    table_names = []
    for page in paginator.paginate():
        table_names.extend(page.get("TableNames", []))
    for name in table_names:
        try:
            desc = ddb.describe_table(TableName=name)["Table"]
            status = desc.get("TableStatus")
            item_count = desc.get("ItemCount")
        except Exception:
            status, item_count = None, None
        rows.append(_norm(
            RESOURCE_TYPE_DYNAMODB, name, name, region, status, {},
            extra={"item_count": item_count},
        ))
    return rows


def _list_load_balancers(elbv2, region: str) -> list[dict]:
    rows = []
    paginator = elbv2.get_paginator("describe_load_balancers")
    for page in paginator.paginate():
        for lb in page.get("LoadBalancers", []):
            rows.append(_norm(
                RESOURCE_TYPE_ELB,
                lb["LoadBalancerName"],
                lb["LoadBalancerName"],
                region,
                lb.get("State", {}).get("Code"),
                {},
                extra={"type": lb.get("Type"), "scheme": lb.get("Scheme")},
            ))
    return rows


# Region-scoped services (need one client per region). S3/DynamoDB's
# LIST is global-ish (S3 truly global; DynamoDB is region-scoped but
# small accounts rarely have many regions with tables) — S3 is fetched
# once per account (see _fetch_account_inventory), the rest per region.
_REGIONAL_LISTERS = [
    ("ec2", _list_ec2_instances),
    ("rds", _list_rds_instances),
    ("lambda", _list_lambda_functions),
    ("dynamodb", _list_dynamodb_tables),
    ("elbv2", _list_load_balancers),
]


def _regions_for_account(account_id: str, core_session: boto3.Session) -> list[str]:
    try:
        ec2 = _client_for_account(account_id, "ec2", core_session)
        resp = ec2.describe_regions(AllRegions=False)
        regions = [r["RegionName"] for r in resp.get("Regions", [])]
        if regions:
            return regions
    except Exception:
        logger.warning("cmdb: could not list regions for account %s, using fallback list", account_id)
    return list(_FALLBACK_REGIONS)


def _regional_client_for_account(account_id: str, service: str, region: str, core_session: boto3.Session):
    """Like cost_explorer._client_for_account(), but returns a client
    explicitly scoped to `region` instead of always us-east-1 — needed
    here because CMDB resources can live in any region, unlike Cost
    Explorer (which is a global/us-east-1-only API). Core account is
    queried directly with its own keys; every other account goes through
    a fresh AssumeRole(PortalReadOnlyRole) call (STS credentials aren't
    region-scoped, so this is just as cheap as reusing a cached session)."""
    # Accounts onboarded through the Cloud Accounts page are reached with the Portal's
    # own role + the account's External_Id; every legacy account falls through to the
    # unchanged code below.
    from . import cloud_account_access, cloud_account_store

    if cloud_account_store.access_mode(account_id) == "infraops":
        return cloud_account_access.client(account_id, service, region_name=region)
    if account_id == CORE_ACCOUNT_ID:
        return core_session.client(service, region_name=region)

    sts = core_session.client("sts", region_name="us-east-1")
    role_arn = f"arn:aws:iam::{account_id}:role/{READONLY_ROLE_NAME}"
    assumed = sts.assume_role(RoleArn=role_arn, RoleSessionName="job-portal-cmdb")
    creds = assumed["Credentials"]
    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _fetch_account_inventory(account_id: str, account_name: str, core_session: boto3.Session, regions: Optional[list[str]] = None) -> dict:
    """Fetch every covered resource type across every region for one
    account. Never raises — a failure fetching one service/region is
    recorded as a warning and simply yields fewer rows for that
    combination, so one broken service in one region doesn't blank the
    whole account's inventory (mirrors cost_explorer.py's per-account
    error isolation)."""
    row = {
        "account_id": account_id,
        "account_name": account_name,
        "resources": [],
        "resource_counts": {},
        "error": None,
    }
    try:
        target_regions = regions if regions is not None else _regions_for_account(account_id, core_session)
        resources: list[dict] = []

        # S3 is a true global service — one ListBuckets call per account
        # is sufficient (any regional client works for this API).
        try:
            s3 = _regional_client_for_account(account_id, "s3", "us-east-1", core_session)
            resources.extend(_list_s3_buckets(s3, "global"))
        except Exception:
            logger.warning("cmdb: S3 list failed for account %s", account_id, exc_info=True)

        for region in target_regions:
            for service_name, lister in _REGIONAL_LISTERS:
                try:
                    client = _regional_client_for_account(account_id, service_name, region, core_session)
                    resources.extend(lister(client, region))
                except Exception:
                    logger.warning(
                        "cmdb: %s list failed for account %s region %s", service_name, account_id, region, exc_info=True,
                    )

        row["resources"] = resources
        counts: dict = {}
        for r in resources:
            counts[r["resource_type"]] = counts.get(r["resource_type"], 0) + 1
        row["resource_counts"] = counts
    except Exception:
        logger.exception("cmdb: failed for account %s (%s)", account_id, account_name)
        row["error"] = "Could not fetch CMDB inventory for this account"
    return row


# Cross-account fan-out concurrency. This used to be 8, which (combined
# with each account's own per-region/per-service fanout and the JSON
# parsing work boto3 does for every Describe/List response) pegged the
# ECS task's CPU at 100% for the whole ~15-minute scan on the 0.5 vCPU
# Fargate task this service runs on — starving every other concurrent
# request on the same container (Cost Analysis, Jobs, etc. all got slow
# or timed out while a CMDB fetch was in flight). Lowered to 3 so the
# scan takes longer in wall-clock time but leaves enough CPU headroom for
# the rest of the portal to keep responding normally. Overridable via
# CMDB_FETCH_CONCURRENCY for future tuning without a code change.
_FETCH_CONCURRENCY = int(os.environ.get("CMDB_FETCH_CONCURRENCY", "3"))


def _fetch_all_accounts(core_session: boto3.Session) -> list[dict]:
    accounts_result: list[dict] = []
    with ThreadPoolExecutor(max_workers=_FETCH_CONCURRENCY) as pool:
        futures = {
            pool.submit(_fetch_account_inventory, acct_id, name, core_session): acct_id
            for acct_id, name in ACCOUNTS.items()
        }
        for fut in as_completed(futures):
            accounts_result.append(fut.result())
    accounts_result.sort(key=lambda r: r["account_id"])
    return accounts_result


def _build_inventory(accounts_result: list[dict]) -> dict:
    total_resources = sum(len(a.get("resources") or []) for a in accounts_result)
    error_count = sum(1 for a in accounts_result if a.get("error"))
    type_totals: dict = {}
    for a in accounts_result:
        for t, c in (a.get("resource_counts") or {}).items():
            type_totals[t] = type_totals.get(t, 0) + c
    return {
        "accounts": accounts_result,
        "account_count": len(accounts_result),
        "error_count": error_count,
        "total_resources": total_resources,
        "resource_type_totals": type_totals,
        "as_of": _now_iso(),
        "cache_age_seconds": 0,
        "is_mock": False,
    }


def reset_cache_for_tests() -> None:
    """Test-only helper: clear the process-local cache/single-flight state
    so each test starts from a clean slate. Not used by production code."""
    global _fetch_in_progress
    with _cache_lock:
        _cache["inventory"] = None
        _cache["fetched_at"] = 0.0
        _cache["last_good"] = None
        _cache["last_good_at"] = 0.0
        _fetch_in_progress = False


def _mock_inventory() -> dict:
    """ENV=local has no real cross-account access configured by default —
    return a small deterministic mock inventory (random.Random(7)) so the
    CMDB page still has something realistic to render without any AWS
    setup."""
    import random as _random

    rng = _random.Random(7)
    mock_account_ids = list(ACCOUNTS.items())[:5]
    statuses = {
        RESOURCE_TYPE_EC2: ["running", "running", "stopped"],
        RESOURCE_TYPE_RDS: ["available", "available"],
        RESOURCE_TYPE_LAMBDA: ["Active"],
        RESOURCE_TYPE_S3: ["active"],
        RESOURCE_TYPE_DYNAMODB: ["ACTIVE"],
        RESOURCE_TYPE_ELB: ["active"],
    }
    regions = ["ap-southeast-1", "us-east-1"]

    accounts_out = []
    for acct_id, acct_name in mock_account_ids:
        resources = []
        for rtype, count in [
            (RESOURCE_TYPE_EC2, rng.randint(1, 6)),
            (RESOURCE_TYPE_RDS, rng.randint(0, 2)),
            (RESOURCE_TYPE_LAMBDA, rng.randint(1, 8)),
            (RESOURCE_TYPE_S3, rng.randint(1, 4)),
            (RESOURCE_TYPE_DYNAMODB, rng.randint(0, 3)),
            (RESOURCE_TYPE_ELB, rng.randint(0, 2)),
        ]:
            for i in range(count):
                rid = f"{rtype[:3].lower()}-mock-{acct_id[-4:]}-{i}"
                resources.append(_norm(
                    rtype, rid, f"{acct_name}-{rtype.split()[0].lower()}-{i}",
                    rng.choice(regions), rng.choice(statuses[rtype]),
                    {"Environment": rng.choice(["prod", "sandbox", "dev"])},
                ))
        counts: dict = {}
        for r in resources:
            counts[r["resource_type"]] = counts.get(r["resource_type"], 0) + 1
        accounts_out.append({
            "account_id": acct_id, "account_name": acct_name,
            "resources": resources, "resource_counts": counts, "error": None,
        })

    inventory = _build_inventory(accounts_out)
    inventory["is_mock"] = True
    return inventory


# The single shared "User" node every unresolved-private or public-IP
# inbound peer gets grouped under (Illumio-style "external/human-
# originated traffic" framing) — see _resolve_peer_ip()/
# _append_traffic_edges_from_peers() below for how a peer IP is
# classified as "User" vs a real resolved CMDB resource.
_USER_NODE_ID = "user-external"


def _user_node() -> dict:
    """Fresh 'User' node for one topology response. Deliberately NOT a
    module-level singleton dict — every call to get_*_topology() needs
    its own copy (callers append the actual peer IPs they saw into
    external_ips), so this is a small factory instead of a shared
    constant."""
    return {"id": _USER_NODE_ID, "kind": "user", "label": "User", "external_ips": []}


def _mock_topology(account_id: str, resource_id: str, resource_type: str, root_kind_label: str, root_extra: dict) -> dict:
    """Shared ENV=local deterministic mock topology builder (mirrors
    _mock_inventory's approach) used by all three mock topology functions
    below — one fake instance/db/lb, one security group, one VPC/subnet,
    a "will access" (yellow) context chain, plus a mix of traffic edges
    demonstrating every trigger classification the real implementation
    produces: a resolved internal "resource" peer (private IP), an
    unresolved internal peer, and a public-IP peer that gets grouped
    under the shared "User" node — so the demo shows every visual style
    without any real AWS access."""
    account_name = ACCOUNTS.get(account_id, account_id)
    root_id = f"{root_kind_label}-{resource_id}"
    nodes = [
        {"id": root_id, "kind": "instance", "label": f"mock-{resource_id[-6:]}", "resource_type": resource_type, "region": "ap-southeast-1", **root_extra},
        {"id": "vpc-vpc-mock1", "kind": "vpc", "label": "vpc-mock1", "vpc_id": "vpc-mock1"},
        {"id": "subnet-subnet-mock1", "kind": "subnet", "label": "subnet-mock1", "subnet_id": "subnet-mock1"},
        {"id": "sg-sg-mock1", "kind": "security_group", "label": "web-sg", "sg_id": "sg-mock1",
         "inbound_rules": ["TCP 443 ← 0.0.0.0/0", "TCP 22 ← 10.0.0.0/16"], "outbound_rules": ["All All ← 0.0.0.0/0"]},
        {"id": "res-10.0.2.5", "kind": "resource", "label": "app-server-2", "ip": "10.0.2.5", "resource_type": RESOURCE_TYPE_EC2, "resolved": True},
        {"id": "res-10.0.9.9", "kind": "resource", "label": "10.0.9.9", "ip": "10.0.9.9", "resource_type": None, "resolved": False},
        _user_node(),
    ]
    nodes[-1]["external_ips"] = ["8.8.8.8"]
    edges = [
        {"id": "e-subnet-vpc", "source": "subnet-subnet-mock1", "target": "vpc-vpc-mock1", "kind": "context", "animated": True},
        {"id": f"e-{root_id}-sg-mock1", "source": root_id, "target": "sg-sg-mock1", "kind": "context", "animated": True},
        {"id": "e-sg-mock1-subnet", "source": "sg-sg-mock1", "target": "subnet-subnet-mock1", "kind": "context", "animated": True},
        {"id": "e-traffic-in-resource", "source": "res-10.0.2.5", "target": root_id, "kind": "traffic",
         "direction": "inbound", "action": "ACCEPT", "bytes": 184320, "flow_count": 42, "dst_port": "443", "animated": True},
        {"id": "e-traffic-in-user", "source": _USER_NODE_ID, "target": root_id, "kind": "traffic",
         "direction": "inbound", "action": "ACCEPT", "bytes": 51200, "flow_count": 30, "dst_port": "443", "animated": True},
        {"id": "e-traffic-out-resource", "source": root_id, "target": "res-10.0.9.9", "kind": "traffic",
         "direction": "outbound", "action": "ACCEPT", "bytes": 92160, "flow_count": 18, "dst_port": "5432", "animated": True},
        {"id": "e-traffic-in-rejected", "source": "res-10.0.9.9", "target": root_id, "kind": "traffic",
         "direction": "inbound", "action": "REJECT", "bytes": 0, "flow_count": 6, "dst_port": "22", "animated": False},
    ]
    return {
        "account_id": account_id, "account_name": account_name, "instance_id": resource_id,
        "resource_type": resource_type, "nodes": nodes, "edges": edges, "has_flow_logs": True, "flow_log_truncated": False,
        "lookback_minutes": _FLOW_LOG_LOOKBACK_MINUTES, "as_of": _now_iso(), "is_mock": True,
    }


def _mock_ec2_topology(account_id: str, instance_id: str) -> dict:
    return _mock_topology(account_id, instance_id, RESOURCE_TYPE_EC2, "ec2", {
        "instance_id": instance_id, "instance_type": "t3.medium", "state": "running",
        "private_ip": "10.0.1.42", "public_ip": "203.0.113.10",
    })


def _mock_rds_topology(account_id: str, db_instance_id: str) -> dict:
    return _mock_topology(account_id, db_instance_id, RESOURCE_TYPE_RDS, "rds", {
        "engine": "postgres", "state": "available",
        "endpoint": f"{db_instance_id}.mock.ap-southeast-1.rds.amazonaws.com", "private_ip": "10.0.1.50",
    })


def _mock_lb_topology(account_id: str, lb_name: str) -> dict:
    result = _mock_topology(account_id, lb_name, RESOURCE_TYPE_ELB, "lb", {
        "lb_type": "application", "state": "active", "scheme": "internet-facing",
        "dns_name": f"{lb_name}-mock.ap-southeast-1.elb.amazonaws.com",
    })
    result["nodes"].extend([
        {"id": "tg-mock-tg", "kind": "target_group", "label": "mock-tg", "protocol": "HTTP", "port": 8080},
        {"id": "target-i-mock1", "kind": "target", "label": "i-mock1", "health": "healthy"},
        {"id": "target-i-mock2", "kind": "target", "label": "i-mock2", "health": "healthy"},
    ])
    root_id = f"lb-{lb_name}"
    result["edges"].extend([
        {"id": f"e-{root_id}-tg-mock-tg", "source": root_id, "target": "tg-mock-tg", "kind": "context", "animated": True},
        {"id": "e-tg-mock-tg-target-i-mock1", "source": "tg-mock-tg", "target": "target-i-mock1", "kind": "context", "animated": True},
        {"id": "e-tg-mock-tg-target-i-mock2", "source": "tg-mock-tg", "target": "target-i-mock2", "kind": "context", "animated": True},
    ])
    return result


def _should_use_mock_inventory() -> bool:
    """ENV=="local" normally means "use mock data" for this feature, but
    a developer can opt a *local* run into real cross-account data by
    setting CORE_ACCESS_KEY_ID / CORE_SECRET_ACCESS_KEY in the
    shell before starting the backend — the same env-var-override
    convenience cost_explorer._get_core_credentials() already offers.
    This is deliberately scoped to CMDB alone: it does NOT touch the
    global ENV flag, so every other local-mode behaviour (SQLite-backed
    Jobs, mock Freshservice/AWS Support ticket IDs, etc.) is completely
    unaffected — only this one feature's data source flips to real AWS."""
    if ENV != "local":
        return False
    return not (os.environ.get("CORE_ACCESS_KEY_ID") and os.environ.get("CORE_SECRET_ACCESS_KEY"))


def _start_background_thread(target) -> None:
    """Thin wrapper around threading.Thread(...).start() so tests can
    monkeypatch this one seam (e.g. to run `target` synchronously inline)
    without touching the real global threading module."""
    threading.Thread(target=target, daemon=True, name="cmdb-fetch").start()


def _run_background_fetch() -> None:
    """Do the actual org-wide fetch (can take up to ~15 minutes across 24
    accounts/6 services/multiple regions) on a plain background thread,
    never on an HTTP request's thread. Updates the shared cache when
    done. Any exception here is logged and swallowed — a failed
    background refresh just means the next poll keeps serving whatever
    was cached before (or the mock/empty state), it must never crash the
    daemon thread or the process."""
    global _fetch_in_progress
    try:
        core_session = _core_session()
        accounts_result = _fetch_all_accounts(core_session)
        inventory = _build_inventory(accounts_result)

        # Diff against the last snapshot and record history — best-effort,
        # never lets a history-store problem break the read path itself.
        try:
            from . import cmdb_store
            for account in accounts_result:
                if account.get("error"):
                    continue
                cmdb_store.diff_and_record(account["account_id"], account["resources"])
        except Exception:
            logger.exception("cmdb: history diff/record failed (inventory itself still cached)")

        with _cache_lock:
            error_count = inventory["error_count"]
            account_count = inventory["account_count"]
            all_failed = account_count > 0 and error_count == account_count

            if all_failed and _cache["last_good"] is not None:
                logger.warning(
                    "cmdb: all %d accounts failed on refresh, serving last known-good inventory from %ds ago",
                    account_count, int(time.monotonic() - _cache["last_good_at"]),
                )
                fallback = copy.deepcopy(_cache["last_good"])
                fallback["cache_age_seconds"] = int(time.monotonic() - _cache["last_good_at"])
                fallback["stale_fallback"] = True
                _cache["inventory"] = fallback
                _cache["fetched_at"] = _cache["last_good_at"]
            else:
                _cache["inventory"] = inventory
                _cache["fetched_at"] = time.monotonic()
                if error_count == 0:
                    _cache["last_good"] = copy.deepcopy(inventory)
                    _cache["last_good_at"] = _cache["fetched_at"]
    except Exception:
        logger.exception("cmdb: background org-wide fetch failed entirely")
    finally:
        with _cache_lock:
            _fetch_in_progress = False


def _hydrate_cache_from_store() -> bool:
    """Seed the in-process cache from the shared DynamoDB/SQLite snapshot
    store (cmdb_store.py) when this process has never fetched anything
    itself. Returns True if the cache now has data (either already had
    some, or was just hydrated), False if the store itself is empty too.

    Why this exists: the ALB has no session stickiness across the 2+ ECS
    tasks running this service, so each task's in-process _cache starts
    out empty and is completely invisible to every other task. Without
    this, a request/poll that happens to land on a task that has never
    run its own background fetch would see "fetch_status": "pending"
    forever, even while ANY other task (or a previous deployment) has
    already successfully written a full inventory to the shared
    DynamoDB store — this is exactly what caused the CMDB page to show
    "0 accounts / 0 resources" while the History drawer (which reads
    that same DynamoDB table directly) correctly showed data. Called
    with _cache_lock already held by the caller."""
    if _cache["inventory"] is not None:
        return True
    try:
        from . import cmdb_store
        snapshots = cmdb_store.get_all_snapshots()
    except Exception:
        logger.exception("cmdb: could not hydrate cache from snapshot store")
        return False
    if not snapshots:
        return False

    accounts_result = [
        {
            "account_id": acct_id,
            "account_name": ACCOUNTS.get(acct_id, acct_id),
            "resources": resources,
            "resource_counts": {
                t: sum(1 for r in resources if r.get("resource_type") == t)
                for t in {r.get("resource_type") for r in resources}
            },
            "error": None,
        }
        for acct_id, resources in snapshots.items()
    ]
    accounts_result.sort(key=lambda r: r["account_id"])
    inventory = _build_inventory(accounts_result)
    inventory["hydrated_from_store"] = True
    _cache["inventory"] = inventory
    _cache["fetched_at"] = time.monotonic()
    _cache["last_good"] = copy.deepcopy(inventory)
    _cache["last_good_at"] = _cache["fetched_at"]
    return True


def get_cmdb_inventory(force_refresh: bool = False) -> dict:
    """Org-wide CMDB resource inventory, one row per account, cached for
    _CACHE_TTL_SECONDS unless force_refresh.

    IMPORTANT: this function NEVER blocks the calling (HTTP request)
    thread on the actual multi-minute cross-account/cross-region AWS
    fetch. The real fetch (_run_background_fetch) always runs on a
    separate background thread; this function only ever reads whatever
    is currently cached and, if the cache is stale/empty/a refresh was
    requested, kicks off (or single-flights into) that background fetch
    and returns immediately with a "fetch_status" telling the frontend
    whether to keep polling.

    This is a deliberate change from the original design, which awaited
    the fetch inline and could tie up a request thread — and the shared
    CPU on this service's small Fargate task — for up to ~15 minutes,
    which was found to be starving every other concurrent request on the
    portal (Cost Analysis, Jobs, etc. all slowed down while a CMDB fetch
    was running). See _FETCH_CONCURRENCY's comment for the other half of
    that fix (lower per-account fan-out concurrency).

    ALSO IMPORTANT: this process's cache may be empty simply because THIS
    particular ECS task has never run a background fetch yet, even
    though other tasks behind the same ALB (no session stickiness) have.
    Before deciding there's genuinely no data anywhere, an empty cache is
    first hydrated from the shared DynamoDB/SQLite snapshot store (see
    _hydrate_cache_from_store) — this is what makes the CMDB page usable
    immediately after any deployment/task replacement instead of every
    task independently re-running its own ~15-minute scan before it has
    anything to show.

    Return shape when data is available (either freshly fetched, cached,
    hydrated from the shared store, mock, or last-known-good fallback):
    the full inventory dict, plus "fetch_status": "ready".

    Return shape when no data is available yet ANYWHERE — not this
    process's cache, not the shared store either (e.g. the very first
    fetch ever, org-wide): a minimal dict with "fetch_status": "pending",
    "accounts": [], etc. — the frontend should keep polling (e.g. every
    5-10s) until fetch_status flips to "ready"."""
    if _should_use_mock_inventory():
        result = _mock_inventory()
        result["fetch_status"] = "ready"
        return result

    global _fetch_in_progress

    with _cache_lock:
        _hydrate_cache_from_store()

        have_fresh_cache = False
        if _cache["inventory"] is not None:
            age = time.monotonic() - _cache["fetched_at"]
            have_fresh_cache = age < _CACHE_TTL_SECONDS

        need_new_fetch = force_refresh or not have_fresh_cache
        should_start_fetch = need_new_fetch and not _fetch_in_progress

        if should_start_fetch:
            _fetch_in_progress = True

    if should_start_fetch:
        # Deliberately started AFTER releasing the lock above (fetching
        # must never happen while holding _cache_lock — it can take up
        # to ~15 minutes) and BEFORE reading the cache below, so that if
        # this ever completes essentially instantly (e.g. tests
        # monkeypatch this to run inline/synchronously), the freshest
        # possible data is served on this very call instead of an
        # unnecessarily stale "pending" response.
        _start_background_thread(_run_background_fetch)

    with _cache_lock:
        # Read whatever is cached NOW (possibly just updated by a fetch
        # that finished synchronously above, in tests, or by the
        # store-hydration above) — always serve this even if stale, since
        # a stale-but-present inventory is far more useful to render than
        # a blank page while a real background refresh runs.
        if _cache["inventory"] is not None:
            age = time.monotonic() - _cache["fetched_at"]
            result = copy.deepcopy(_cache["inventory"])
            result["cache_age_seconds"] = int(age)
        else:
            result = None
        currently_fetching = _fetch_in_progress

    if result is not None:
        result["fetch_status"] = "refreshing" if currently_fetching else "ready"
        return result

    # No cached data at all yet (process just started, nobody has ever
    # fetched successfully) — tell the frontend to keep polling instead
    # of showing an error, since a fetch has just been kicked off above.
    return {
        "accounts": [],
        "account_count": 0,
        "error_count": 0,
        "total_resources": 0,
        "resource_type_totals": {},
        "as_of": _now_iso(),
        "cache_age_seconds": 0,
        "is_mock": False,
        "fetch_status": "pending",
    }


# ── AI impact analysis (Task 9 requirement #5) ────────────────────────────

def analyse_resource_impact(resource: dict, account_id: str, account_name: str) -> dict:
    """
    Ask Bedrock what would likely break / who would be impacted if the
    given resource (a row from get_cmdb_inventory(), or the equivalent
    shape) were to fail, be stopped, or be deleted. Purely advisory and
    read-only — this never touches any AWS resource, it only reasons
    about the resource's own metadata (type, name, region, tags).

    Uses the same Claude Sonnet 5.5 (structured JSON) + retry model
    pattern ai_engine.py already uses for job/health-event analysis, so
    this shares the exact same reliability characteristics operators are
    already used to.
    """
    from .ai_engine import _get_client, _invoke, _extract_json, MODEL_ANALYSIS, MODEL_ANALYSIS_FALLBACK, BEDROCK_REGION

    tags = resource.get("tags") or {}
    tags_str = ", ".join(f"{k}={v}" for k, v in tags.items()) if tags else "(no tags set)"
    extra = resource.get("extra") or {}
    extra_str = ", ".join(f"{k}={v}" for k, v in extra.items()) if extra else "(none)"

    prompt = f"""You are a senior cloud infrastructure engineer performing an impact/blast-radius assessment for a CMDB (Configuration Management Database) tool.

A resource may go down, be stopped, or be deleted. Assess the impact based on the metadata below (you have no other information — infer sensibly from naming conventions, tags, and resource type; do not invent specific unrelated systems).

Resource:
- Type: {resource.get('resource_type')}
- ID/Name: {resource.get('resource_id')} ({resource.get('name')})
- AWS Account: {account_name} ({account_id})
- Region: {resource.get('region')}
- Current status: {resource.get('status')}
- Tags: {tags_str}
- Additional metadata: {extra_str}

Tasks:
1. In plain language, summarise what would likely happen if this resource stopped working right now (1-3 sentences).
2. Estimate a severity level for the business/operational impact.
3. List up to 5 likely dependent systems/services/teams that could be affected (infer from tags like 'environment', 'application_id', 'application_owner', naming patterns, and resource type — e.g. an RDS instance tagged environment=prod is likely backing a production application; a Lambda function named '*-report' likely feeds a reporting workflow). Say "Unknown — insufficient tagging" if there isn't enough information to infer anything.
4. Give up to 4 concrete recommendations to reduce the risk/impact (e.g. add monitoring, confirm backup/DR plan, verify no hardcoded IPs, check for a standby/replica, improve tagging so future impact assessments are more accurate).
5. Give a one-line "bottom line" statement for an on-call engineer.

IMPORTANT: Reply with valid JSON ONLY, no extra text:
{{
  "impact_summary": "...",
  "severity": "critical" | "high" | "medium" | "low",
  "likely_affected": ["affected system/team 1", "affected system/team 2"],
  "recommendations": ["recommendation 1", "recommendation 2"],
  "bottom_line": "..."
}}"""

    client = _get_client()
    used_model = MODEL_ANALYSIS
    data = None
    last_error: Optional[Exception] = None

    for model_id in (MODEL_ANALYSIS, MODEL_ANALYSIS_FALLBACK):
        try:
            raw_text = _invoke(client, model_id, prompt, max_tokens=800)
        except Exception as e:
            logger.warning(f"Bedrock invocation failed for CMDB impact analysis with {model_id}: {e}")
            last_error = e
            continue
        try:
            data = _extract_json(raw_text)
        except Exception as e:
            logger.warning(f"Could not parse CMDB impact-analysis response from {model_id}: {e}")
            last_error = e
            continue
        if not isinstance(data.get("impact_summary"), str) or not data.get("impact_summary", "").strip():
            data = None
            continue
        used_model = model_id
        break

    if data is None:
        logger.error(f"All models failed to produce a usable CMDB impact analysis: {last_error}")
        data = {
            "impact_summary": "Automated AI impact analysis is temporarily unavailable. Review this resource's tags and dependents manually before taking action.",
            "severity": "medium",
            "likely_affected": [],
            "recommendations": ["Retry the AI analysis in a few minutes.", "Manually check CloudWatch alarms and any downstream consumers before proceeding."],
            "bottom_line": "AI analysis failed — treat this resource as unassessed and proceed with caution.",
        }
        used_model = "fallback-static"

    severity = data.get("severity") if data.get("severity") in ("critical", "high", "medium", "low") else "medium"
    return {
        "impact_summary": str(data.get("impact_summary", ""))[:1500],
        "severity": severity,
        "likely_affected": [str(x)[:200] for x in (data.get("likely_affected") or [])][:5],
        "recommendations": [str(x)[:300] for x in (data.get("recommendations") or [])][:4],
        "bottom_line": str(data.get("bottom_line", ""))[:300],
        "generated_at": _now_iso(),
        "model_used": used_model,
        "bedrock_region": BEDROCK_REGION,
    }


# ── EC2 Topology / Illumio-style traffic diagram ──────────────────────────
#
# Per-instance topology view: given one EC2 instance (account_id +
# instance_id), build a small graph (nodes + edges) suitable for
# rendering with @xyflow/react on the frontend — the EC2 instance itself,
# its VPC/subnet, its attached security groups, and (traffic peers).
#
# Traffic data source, in priority order (decided with the user):
#   1. If the instance's VPC has an ACTIVE Flow Log delivering to
#      CloudWatch Logs (S3-only destinations are skipped — Athena-based
#      querying is too slow for an interactive UI) -> run a bounded
#      CloudWatch Logs Insights query scoped to this instance's private
#      IP over the last _FLOW_LOG_LOOKBACK_MINUTES minutes, and turn each
#      distinct peer IP + ACCEPT/REJECT direction into a real edge with a
#      genuine "ACCEPT"/"REJECT" + byte count. This gives real
#      peer-to-peer arrows (green = inbound accepted, red = outbound
#      accepted; rejected traffic is flagged separately).
#   2. If there's no usable Flow Log, still render the same
#      SG/VPC/subnet topology (so the diagram is never empty), but the
#      green/red "has traffic" animation is instead driven by whether
#      CloudWatch's NetworkIn/NetworkOut metrics for this instance show
#      non-zero activity in the same lookback window — a generic
#      activity indicator, not real peer-to-peer, animated toward/away
#      from a single "Network" node instead of named peers.
#
# Every external AWS call in here is wrapped so a failure of ANY single
# piece (flow logs, Logs Insights, CloudWatch metrics) never blanks the
# whole diagram — the instance/SG/VPC nodes are always returned as long
# as describe_instances/describe_security_groups themselves succeed.

_FLOW_LOG_LOOKBACK_MINUTES = 15
_FLOW_LOG_QUERY_TIMEOUT_SECONDS = 20
_FLOW_LOG_MAX_PEERS = 25


def _find_target_instance(ec2, instance_id: str) -> Optional[dict]:
    try:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
    except Exception:
        logger.warning("cmdb topology: describe_instances failed for %s", instance_id, exc_info=True)
        return None
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            if inst.get("InstanceId") == instance_id:
                return inst
    return None


def _security_group_nodes(ec2, sg_ids: list[str]) -> list[dict]:
    """One node per attached security group, each carrying its inbound/
    outbound rule summaries (protocol/port/source) so the frontend can
    show them on hover/click — this is what actually explains WHY a given
    peer's traffic was allowed or blocked, matching Illumio's "policy"
    framing rather than just showing raw traffic."""
    if not sg_ids:
        return []
    try:
        resp = ec2.describe_security_groups(GroupIds=sg_ids)
    except Exception:
        logger.warning("cmdb topology: describe_security_groups failed", exc_info=True)
        return []

    def _rule_summary(perms: list[dict]) -> list[str]:
        out = []
        for p in perms or []:
            proto = p.get("IpProtocol", "-1")
            proto_label = "All" if proto == "-1" else proto.upper()
            port_from, port_to = p.get("FromPort"), p.get("ToPort")
            port_label = "All ports" if port_from is None else (f"{port_from}" if port_from == port_to else f"{port_from}-{port_to}")
            sources = [r.get("CidrIp") for r in p.get("IpRanges", []) if r.get("CidrIp")]
            sources += [r.get("CidrIpv6") for r in p.get("Ipv6Ranges", []) if r.get("CidrIpv6")]
            sources += [f"sg:{r.get('GroupId')}" for r in p.get("UserIdGroupPairs", []) if r.get("GroupId")]
            src_label = ", ".join(sources) if sources else "—"
            out.append(f"{proto_label} {port_label} ← {src_label}")
        return out[:10]

    nodes = []
    for sg in resp.get("SecurityGroups", []):
        nodes.append({
            "id": f"sg-{sg['GroupId']}",
            "kind": "security_group",
            "label": sg.get("GroupName") or sg["GroupId"],
            "sg_id": sg["GroupId"],
            "inbound_rules": _rule_summary(sg.get("IpPermissions")),
            "outbound_rules": _rule_summary(sg.get("IpPermissionsEgress")),
        })
    return nodes


def _vpc_flow_log_destination(ec2, vpc_id: str) -> Optional[str]:
    """Return the CloudWatch Logs log-group name for the first ACTIVE,
    CloudWatch-Logs-destined flow log attached to this VPC, or None if
    there isn't one (either no flow log at all, or it's S3-only —
    deliberately not supported here per the earlier decision that
    Athena-based S3 flow log querying is too slow for this interactive
    view)."""
    try:
        resp = ec2.describe_flow_logs(Filters=[{"Name": "resource-id", "Values": [vpc_id]}])
    except Exception:
        logger.warning("cmdb topology: describe_flow_logs failed for vpc %s", vpc_id, exc_info=True)
        return None
    for fl in resp.get("FlowLogs", []):
        if fl.get("FlowLogStatus") != "ACTIVE":
            continue
        if fl.get("LogDestinationType") != "cloud-watch-logs":
            continue
        log_group = fl.get("LogGroupName")
        if log_group:
            return log_group
    return None


def _query_flow_log_peers(logs_client, log_group: str, ips: list[str]) -> dict:
    """Run one bounded CloudWatch Logs Insights query against the given
    flow-log group, scoped to traffic where ANY of the given IPs (the
    target resource's own addresses — one private+public IP for an EC2
    instance, or every ENI private IP for a multi-AZ Load Balancer) is
    either the source or destination. Returns
    {"peers": [...], "truncated": bool} — never raises; any failure
    (query timeout, malformed fields, permission issue) yields an empty
    peer list so the caller falls back to the metrics-only view."""
    ips = [ip for ip in ips if ip]
    if not ips:
        return {"peers": [], "truncated": False}

    ip_filter = " or ".join(f'srcaddr = "{ip}" or dstaddr = "{ip}"' for ip in ips)
    query = f"""
fields srcaddr, dstaddr, srcport, dstport, protocol, action, bytes
| filter {ip_filter}
| stats sum(bytes) as total_bytes, count(*) as flow_count by srcaddr, dstaddr, dstport, action
| sort total_bytes desc
| limit {_FLOW_LOG_MAX_PEERS}
"""
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=_FLOW_LOG_LOOKBACK_MINUTES)

    try:
        started = logs_client.start_query(
            logGroupName=log_group,
            startTime=int(start_time.timestamp()),
            endTime=int(end_time.timestamp()),
            queryString=query,
        )
        query_id = started["queryId"]
    except Exception:
        logger.warning("cmdb topology: start_query failed for log group %s", log_group, exc_info=True)
        return {"peers": [], "truncated": False}

    deadline = time.monotonic() + _FLOW_LOG_QUERY_TIMEOUT_SECONDS
    result_rows: list[dict] = []
    status = "Running"
    while time.monotonic() < deadline:
        try:
            resp = logs_client.get_query_results(queryId=query_id)
        except Exception:
            logger.warning("cmdb topology: get_query_results failed for query %s", query_id, exc_info=True)
            return {"peers": [], "truncated": False}
        status = resp.get("status")
        if status in ("Complete", "Failed", "Cancelled", "Timeout"):
            result_rows = resp.get("results", [])
            break
        time.sleep(1)

    if status != "Complete":
        logger.warning("cmdb topology: flow log query %s did not complete in time (status=%s)", query_id, status)
        try:
            logs_client.stop_query(queryId=query_id)
        except Exception:
            pass
        return {"peers": [], "truncated": True}

    peers = []
    for row in result_rows:
        fields = {f["field"]: f["value"] for f in row}
        src, dst = fields.get("srcaddr"), fields.get("dstaddr")
        if not src or not dst:
            continue
        direction = "inbound" if dst in ips else "outbound"
        peer_ip = src if direction == "inbound" else dst
        peers.append({
            "peer_ip": peer_ip,
            "direction": direction,
            "action": (fields.get("action") or "ACCEPT").upper(),
            "dst_port": fields.get("dstport"),
            "bytes": int(fields.get("total_bytes") or 0),
            "flow_count": int(fields.get("flow_count") or 0),
        })
    return {"peers": peers, "truncated": len(result_rows) >= _FLOW_LOG_MAX_PEERS}


def _resolve_peer_ip(ec2, ip: str) -> dict:
    """Mandatory "trigger" classification for one Flow-Log peer IP (per
    the user's explicit requirement: every inbound edge must say WHO
    triggered it):
      - a public (non-private) IP -> always "user" (grouped under the
        shared User node — Illumio's convention for external/human-
        originated traffic).
      - a private IP that matches an existing ENI's private IP in this
        account/region (a live ec2:DescribeNetworkInterfaces reverse
        lookup, not a possibly-stale inventory cache) -> "resource": it's
        another real AWS resource (an EC2 instance, an RDS instance's own
        ENI, a Load Balancer node's ENI, or some other internal ENI) and
        gets its own labeled node.
      - a private IP that does NOT match any ENI (e.g. on-prem traffic
        over VPN/Direct Connect, a stale/decommissioned resource, or
        simply outside what this role can see) -> still "user", per the
        decision that only a POSITIVELY resolved resource earns its own
        node; everything else falls back to the generic User bucket
        rather than cluttering the diagram with unlabeled private IPs.

    Never raises — an ENI lookup failure degrades to "user" (always the
    safe default), it must never block the rest of the diagram."""
    try:
        is_private = ipaddress.ip_address(ip).is_private
    except ValueError:
        is_private = False

    if not is_private:
        return {"classification": "user"}

    try:
        resp = ec2.describe_network_interfaces(Filters=[{"Name": "addresses.private-ip-address", "Values": [ip]}])
    except Exception:
        logger.warning("cmdb topology: describe_network_interfaces failed for peer %s", ip, exc_info=True)
        return {"classification": "user"}

    enis = resp.get("NetworkInterfaces") or []
    if not enis:
        return {"classification": "user"}

    eni = enis[0]
    attachment = eni.get("Attachment") or {}
    description = eni.get("Description") or ""

    if attachment.get("InstanceId"):
        return {"classification": "resource", "resource_type": RESOURCE_TYPE_EC2, "resource_id": attachment["InstanceId"], "label": attachment["InstanceId"]}

    # ELB ENI descriptions from AWS look like "ELB app/my-lb-name/abcdef"
    # (ALB) or "ELB net/my-lb-name/abcdef" (NLB) or "ELB my-clb-name" (CLB).
    if description.startswith("ELB "):
        parts = description[len("ELB "):].split("/")
        lb_name = parts[1] if len(parts) >= 2 else parts[0]
        return {"classification": "resource", "resource_type": RESOURCE_TYPE_ELB, "resource_id": lb_name, "label": lb_name}

    if "RDS" in description:
        return {"classification": "resource", "resource_type": RESOURCE_TYPE_RDS, "resource_id": ip, "label": description or ip}

    # Some other internal ENI this diagram doesn't have a dedicated icon
    # for yet (Lambda-in-VPC, VPC endpoint, NAT gateway, etc.) — it IS a
    # real internal resource (not external "User" traffic), so it still
    # gets its own resolved node, just without a specific resource_type.
    return {"classification": "resource", "resource_type": None, "resource_id": ip, "label": description or ip}


def _peer_node_and_edge(ec2, root_id: str, peer: dict, user_node: dict) -> tuple[Optional[dict], dict]:
    """Turn one Flow-Log peer record ({peer_ip, direction, action,
    dst_port, bytes, flow_count}) into (node_or_None, edge). `node` is
    None when the peer resolves to the shared User node (the caller
    already has exactly one User node in its node list; this just
    records the IP onto it) — otherwise a fresh `kind: "resource"` node
    is returned (the caller is responsible for de-duplicating by peer IP,
    since the same IP can appear across multiple peer rows for different
    ports/directions).

    Every inbound-and-accepted edge is animated regardless of whether the
    source is the User node or a resolved resource — the frontend colors
    inbound edges green and outbound edges red; a resolved-resource
    source is simply a differently-labeled/iconed green dot, per the
    user's explicit request ("make that resource with green dot inbound
    move point")."""
    resolved = _resolve_peer_ip(ec2, peer["peer_ip"])
    is_inbound = peer["direction"] == "inbound"
    is_accept = peer["action"] == "ACCEPT"

    if resolved["classification"] == "user":
        if peer["peer_ip"] not in user_node["external_ips"]:
            user_node["external_ips"].append(peer["peer_ip"])
        node = None
        peer_node_id = _USER_NODE_ID
    else:
        peer_node_id = f"res-{peer['peer_ip']}"
        node = {
            "id": peer_node_id,
            "kind": "resource",
            "label": resolved.get("label") or peer["peer_ip"],
            "ip": peer["peer_ip"],
            "resource_type": resolved.get("resource_type"),
            "resolved": True,
        }

    edge = {
        "id": f"e-traffic-{peer['peer_ip']}-{peer['direction']}-{peer.get('dst_port') or 'na'}",
        "source": peer_node_id if is_inbound else root_id,
        "target": root_id if is_inbound else peer_node_id,
        "kind": "traffic",
        "direction": peer["direction"],
        "action": peer["action"],
        "bytes": peer["bytes"],
        "flow_count": peer["flow_count"],
        "dst_port": peer.get("dst_port"),
        "animated": is_accept,
    }
    return node, edge


def _append_peers_to_graph(ec2, root_id: str, peers: list[dict], nodes: list[dict], edges: list[dict]) -> None:
    """Shared driver used by all three get_*_topology() functions: turns
    a list of Flow-Log peer records into nodes+edges, appending onto the
    given `nodes`/`edges` lists in place. Guarantees exactly one User
    node is added (only if at least one peer actually resolves to it),
    and de-duplicates resolved-resource nodes by peer IP."""
    user_node = _user_node()
    added_user = False
    seen_resource_ids: set[str] = set()

    for p in peers:
        node, edge = _peer_node_and_edge(ec2, root_id, p, user_node)
        if node is None:
            if not added_user:
                nodes.append(user_node)
                added_user = True
        elif node["id"] not in seen_resource_ids:
            seen_resource_ids.add(node["id"])
            nodes.append(node)
        edges.append(edge)


def _query_network_activity_metrics(cw_client, instance_id: str) -> dict:
    """Fallback traffic indicator when no CloudWatch-Logs-backed flow log
    is available: pull recent NetworkIn/NetworkOut Sum datapoints for
    this one instance. Returns booleans (any inbound/outbound bytes seen
    in the lookback window) plus the raw totals, purely to drive a
    generic animated pulse — never raises."""
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=_FLOW_LOG_LOOKBACK_MINUTES)
    try:
        resp = cw_client.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "netin",
                    "MetricStat": {
                        "Metric": {"Namespace": "AWS/EC2", "MetricName": "NetworkIn", "Dimensions": [{"Name": "InstanceId", "Value": instance_id}]},
                        "Period": 300, "Stat": "Sum",
                    },
                    "ReturnData": True,
                },
                {
                    "Id": "netout",
                    "MetricStat": {
                        "Metric": {"Namespace": "AWS/EC2", "MetricName": "NetworkOut", "Dimensions": [{"Name": "InstanceId", "Value": instance_id}]},
                        "Period": 300, "Stat": "Sum",
                    },
                    "ReturnData": True,
                },
            ],
            StartTime=start_time,
            EndTime=end_time,
        )
    except Exception:
        logger.warning("cmdb topology: get_metric_data failed for instance %s", instance_id, exc_info=True)
        return {"has_inbound": False, "has_outbound": False, "bytes_in": 0, "bytes_out": 0}

    totals = {"netin": 0, "netout": 0}
    for result in resp.get("MetricDataResults", []):
        totals[result["Id"]] = int(sum(result.get("Values") or []))

    return {
        "has_inbound": totals["netin"] > 0,
        "has_outbound": totals["netout"] > 0,
        "bytes_in": totals["netin"],
        "bytes_out": totals["netout"],
    }


def _query_rds_activity_metrics(cw_client, db_instance_id: str) -> dict:
    """RDS equivalent of _query_network_activity_metrics(): RDS has no
    direct NetworkIn/NetworkOut metric, but NetworkReceiveThroughput /
    NetworkTransmitThroughput (bytes/sec, AWS/RDS namespace,
    DBInstanceIdentifier dimension) serve the same "is there any traffic
    at all right now" purpose for the CloudWatch-metrics fallback path."""
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=_FLOW_LOG_LOOKBACK_MINUTES)
    try:
        resp = cw_client.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "netin",
                    "MetricStat": {
                        "Metric": {"Namespace": "AWS/RDS", "MetricName": "NetworkReceiveThroughput", "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]},
                        "Period": 300, "Stat": "Sum",
                    },
                    "ReturnData": True,
                },
                {
                    "Id": "netout",
                    "MetricStat": {
                        "Metric": {"Namespace": "AWS/RDS", "MetricName": "NetworkTransmitThroughput", "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": db_instance_id}]},
                        "Period": 300, "Stat": "Sum",
                    },
                    "ReturnData": True,
                },
            ],
            StartTime=start_time,
            EndTime=end_time,
        )
    except Exception:
        logger.warning("cmdb topology: get_metric_data failed for db instance %s", db_instance_id, exc_info=True)
        return {"has_inbound": False, "has_outbound": False, "bytes_in": 0, "bytes_out": 0}

    totals = {"netin": 0, "netout": 0}
    for result in resp.get("MetricDataResults", []):
        totals[result["Id"]] = int(sum(result.get("Values") or []))
    return {
        "has_inbound": totals["netin"] > 0,
        "has_outbound": totals["netout"] > 0,
        "bytes_in": totals["netin"],
        "bytes_out": totals["netout"],
    }


def _query_lb_activity_metrics(cw_client, lb_name: str, lb_type: str) -> dict:
    """Load Balancer equivalent: ALB/NLB expose ProcessedBytes (AWS/
    ApplicationELB or AWS/NetworkELB namespace, LoadBalancer dimension —
    note the dimension VALUE must be the LB's "label" form,
    e.g. "app/my-lb/1234567890abcdef", not just its name). Used purely as
    a generic activity indicator when no VPC Flow Log is available;
    ProcessedBytes doesn't distinguish direction, so both has_inbound and
    has_outbound are driven by the same non-zero check."""
    namespace = "AWS/NetworkELB" if lb_type == "network" else "AWS/ApplicationELB"
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=_FLOW_LOG_LOOKBACK_MINUTES)
    try:
        resp = cw_client.get_metric_data(
            MetricDataQueries=[{
                "Id": "processed",
                "MetricStat": {
                    "Metric": {"Namespace": namespace, "MetricName": "ProcessedBytes", "Dimensions": [{"Name": "LoadBalancer", "Value": lb_name}]},
                    "Period": 300, "Stat": "Sum",
                },
                "ReturnData": True,
            }],
            StartTime=start_time,
            EndTime=end_time,
        )
    except Exception:
        logger.warning("cmdb topology: get_metric_data failed for load balancer %s", lb_name, exc_info=True)
        return {"has_inbound": False, "has_outbound": False, "bytes_in": 0, "bytes_out": 0}

    total = 0
    for result in resp.get("MetricDataResults", []):
        total += int(sum(result.get("Values") or []))
    return {"has_inbound": total > 0, "has_outbound": total > 0, "bytes_in": total, "bytes_out": total}


def get_ec2_topology(account_id: str, instance_id: str) -> dict:
    """Build the Illumio-style topology graph for one EC2 instance:
    nodes = [this instance, its VPC, its subnet, each attached security
    group, and (if real flow-log data is available) one node per
    distinct traffic peer IP] and edges = [instance<->SG, SG<->VPC/
    subnet context, and instance<->peer for each observed flow].

    Raises CmdbTopologyError if the instance itself can't be found (bad
    instance_id, wrong account, or the account's AssumeRole itself
    fails) — every other individual data source failure degrades the
    diagram instead of raising (see module-level docstring above)."""
    if _should_use_mock_inventory():
        return _mock_ec2_topology(account_id, instance_id)

    account_name = ACCOUNTS.get(account_id, account_id)
    core_session = _core_session()

    # The instance's region isn't known up front, so probe every region
    # this account has, stopping at the first hit — mirrors how the org-
    # wide inventory already has to scan per-region, but scoped to just
    # one DescribeInstances call per region instead of 6 services.
    regions = _regions_for_account(account_id, core_session)
    instance, found_region, ec2 = None, None, None
    for region in regions:
        try:
            candidate_ec2 = _regional_client_for_account(account_id, "ec2", region, core_session)
        except Exception:
            continue
        found = _find_target_instance(candidate_ec2, instance_id)
        if found:
            instance, found_region, ec2 = found, region, candidate_ec2
            break

    if instance is None:
        raise CmdbTopologyError(f"EC2 instance {instance_id} was not found in account {account_id} ({account_name}) in any region")

    tags = _tags_to_dict(instance.get("Tags"))
    name = tags.get("Name") or instance_id
    vpc_id = instance.get("VpcId")
    subnet_id = instance.get("SubnetId")
    private_ip = instance.get("PrivateIpAddress")
    public_ip = instance.get("PublicIpAddress")
    sg_ids = [sg["GroupId"] for sg in instance.get("SecurityGroups", []) if sg.get("GroupId")]
    state = (instance.get("State") or {}).get("Name")

    root_id = f"ec2-{instance_id}"
    nodes: list[dict] = [{
        "id": root_id,
        "kind": "instance",
        "label": name,
        "instance_id": instance_id,
        "instance_type": instance.get("InstanceType"),
        "state": state,
        "private_ip": private_ip,
        "public_ip": public_ip,
        "region": found_region,
        "resource_type": RESOURCE_TYPE_EC2,
    }]
    edges: list[dict] = []

    if vpc_id:
        nodes.append({"id": f"vpc-{vpc_id}", "kind": "vpc", "label": vpc_id, "vpc_id": vpc_id})
    if subnet_id:
        nodes.append({"id": f"subnet-{subnet_id}", "kind": "subnet", "label": subnet_id, "subnet_id": subnet_id})
        if vpc_id:
            edges.append({"id": "e-subnet-vpc", "source": f"subnet-{subnet_id}", "target": f"vpc-{vpc_id}", "kind": "context", "animated": True})

    sg_nodes = _security_group_nodes(ec2, sg_ids)
    nodes.extend(sg_nodes)
    for sg_node in sg_nodes:
        edges.append({"id": f"e-{root_id}-{sg_node['sg_id']}", "source": root_id, "target": sg_node["id"], "kind": "context", "animated": True})
        if subnet_id:
            edges.append({"id": f"e-{sg_node['sg_id']}-subnet", "source": sg_node["id"], "target": f"subnet-{subnet_id}", "kind": "context", "animated": True})

    has_flow_logs = False
    flow_log_truncated = False
    activity: Optional[dict] = None

    if vpc_id:
        log_group = _vpc_flow_log_destination(ec2, vpc_id)
        if log_group:
            try:
                logs_client = _regional_client_for_account(account_id, "logs", found_region, core_session)
                result = _query_flow_log_peers(logs_client, log_group, [private_ip, public_ip])
                peers, flow_log_truncated = result["peers"], result["truncated"]
                if peers:
                    has_flow_logs = True
                    _append_peers_to_graph(ec2, root_id, peers, nodes, edges)
            except Exception:
                logger.warning("cmdb topology: flow-log-based traffic lookup failed for %s", instance_id, exc_info=True)

    if not has_flow_logs:
        # Fallback: generic activity-based animation via CloudWatch
        # metrics, per the user's explicit instruction to still show
        # green/red movement even without real flow-log peer data.
        try:
            cw_client = _regional_client_for_account(account_id, "cloudwatch", found_region, core_session)
            activity = _query_network_activity_metrics(cw_client, instance_id)
        except Exception:
            logger.warning("cmdb topology: metrics fallback failed for %s", instance_id, exc_info=True)
            activity = {"has_inbound": False, "has_outbound": False, "bytes_in": 0, "bytes_out": 0}

        nodes.append({"id": "net-generic", "kind": "network", "label": "Network"})
        edges.append({
            "id": "e-traffic-generic-in", "source": "net-generic", "target": root_id,
            "kind": "traffic", "direction": "inbound", "action": "ACCEPT",
            "bytes": activity["bytes_in"], "animated": activity["has_inbound"],
        })
        edges.append({
            "id": "e-traffic-generic-out", "source": root_id, "target": "net-generic",
            "kind": "traffic", "direction": "outbound", "action": "ACCEPT",
            "bytes": activity["bytes_out"], "animated": activity["has_outbound"],
        })

    return {
        "account_id": account_id,
        "account_name": account_name,
        "instance_id": instance_id,
        "resource_type": RESOURCE_TYPE_EC2,
        "nodes": nodes,
        "edges": edges,
        "has_flow_logs": has_flow_logs,
        "flow_log_truncated": flow_log_truncated,
        "lookback_minutes": _FLOW_LOG_LOOKBACK_MINUTES,
        "as_of": _now_iso(),
    }


# ── RDS Topology ───────────────────────────────────────────────────────────

def _find_target_db_instance(rds, db_instance_id: str) -> Optional[dict]:
    try:
        resp = rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)
    except Exception:
        logger.warning("cmdb topology: describe_db_instances failed for %s", db_instance_id, exc_info=True)
        return None
    for db in resp.get("DBInstances", []):
        if db.get("DBInstanceIdentifier") == db_instance_id:
            return db
    return None


def get_rds_topology(account_id: str, db_instance_id: str) -> dict:
    """RDS equivalent of get_ec2_topology(): the target DB instance, its
    VPC/subnets (via DBSubnetGroup) and security groups (VpcSecurityGroups),
    then real Flow-Log-based traffic peers (scoped to the endpoint's
    resolved private IP, since RDS doesn't expose PrivateIpAddress
    directly the way EC2 does) or a CloudWatch NetworkReceiveThroughput/
    NetworkTransmitThroughput-based fallback indicator — same trigger
    classification (User vs resolved resource) as EC2."""
    if _should_use_mock_inventory():
        return _mock_rds_topology(account_id, db_instance_id)

    account_name = ACCOUNTS.get(account_id, account_id)
    core_session = _core_session()

    regions = _regions_for_account(account_id, core_session)
    db, found_region, rds, ec2 = None, None, None, None
    for region in regions:
        try:
            candidate_rds = _regional_client_for_account(account_id, "rds", region, core_session)
        except Exception:
            continue
        found = _find_target_db_instance(candidate_rds, db_instance_id)
        if found:
            db, found_region, rds = found, region, candidate_rds
            try:
                ec2 = _regional_client_for_account(account_id, "ec2", region, core_session)
            except Exception:
                ec2 = None
            break

    if db is None:
        raise CmdbTopologyError(f"RDS instance {db_instance_id} was not found in account {account_id} ({account_name}) in any region")

    vpc_id = (db.get("DBSubnetGroup") or {}).get("VpcId")
    subnet_ids = [s.get("SubnetIdentifier") for s in (db.get("DBSubnetGroup") or {}).get("Subnets", []) if s.get("SubnetIdentifier")]
    sg_ids = [sg["VpcSecurityGroupId"] for sg in db.get("VpcSecurityGroups", []) if sg.get("VpcSecurityGroupId")]
    endpoint = (db.get("Endpoint") or {}).get("Address")
    state = db.get("DBInstanceStatus")

    # RDS doesn't expose a resolved private IP on the instance itself —
    # look it up via the same ENI reverse-lookup used to resolve peer IPs
    # (RDS instances always have an associated ENI carrying the actual
    # database traffic), scoped by the RDS-specific ENI description
    # AWS attaches ("RDSNetworkInterface").
    private_ip = None
    if ec2 and vpc_id:
        try:
            eni_resp = ec2.describe_network_interfaces(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "description", "Values": [f"RDSNetworkInterface"]},
            ])
            for eni in eni_resp.get("NetworkInterfaces", []):
                for addr in eni.get("PrivateIpAddresses", []):
                    if addr.get("Primary"):
                        private_ip = addr.get("PrivateIpAddress")
                        break
                if private_ip:
                    break
        except Exception:
            logger.warning("cmdb topology: RDS ENI lookup failed for %s", db_instance_id, exc_info=True)

    root_id = f"rds-{db_instance_id}"
    nodes: list[dict] = [{
        "id": root_id,
        "kind": "instance",
        "label": db_instance_id,
        "instance_id": db_instance_id,
        "engine": db.get("Engine"),
        "instance_class": db.get("DBInstanceClass"),
        "state": state,
        "endpoint": endpoint,
        "private_ip": private_ip,
        "region": found_region,
        "resource_type": RESOURCE_TYPE_RDS,
    }]
    edges: list[dict] = []

    if vpc_id:
        nodes.append({"id": f"vpc-{vpc_id}", "kind": "vpc", "label": vpc_id, "vpc_id": vpc_id})
    for subnet_id in subnet_ids:
        nodes.append({"id": f"subnet-{subnet_id}", "kind": "subnet", "label": subnet_id, "subnet_id": subnet_id})
        if vpc_id:
            edges.append({"id": f"e-subnet-{subnet_id}-vpc", "source": f"subnet-{subnet_id}", "target": f"vpc-{vpc_id}", "kind": "context", "animated": True})

    sg_nodes = _security_group_nodes(ec2, sg_ids) if ec2 else []
    nodes.extend(sg_nodes)
    for sg_node in sg_nodes:
        edges.append({"id": f"e-{root_id}-{sg_node['sg_id']}", "source": root_id, "target": sg_node["id"], "kind": "context", "animated": True})
        for subnet_id in subnet_ids:
            edges.append({"id": f"e-{sg_node['sg_id']}-{subnet_id}", "source": sg_node["id"], "target": f"subnet-{subnet_id}", "kind": "context", "animated": True})

    has_flow_logs = False
    flow_log_truncated = False

    if vpc_id and ec2 and private_ip:
        log_group = _vpc_flow_log_destination(ec2, vpc_id)
        if log_group:
            try:
                logs_client = _regional_client_for_account(account_id, "logs", found_region, core_session)
                result = _query_flow_log_peers(logs_client, log_group, [private_ip])
                peers, flow_log_truncated = result["peers"], result["truncated"]
                if peers:
                    has_flow_logs = True
                    _append_peers_to_graph(ec2, root_id, peers, nodes, edges)
            except Exception:
                logger.warning("cmdb topology: flow-log-based traffic lookup failed for %s", db_instance_id, exc_info=True)

    if not has_flow_logs:
        try:
            cw_client = _regional_client_for_account(account_id, "cloudwatch", found_region, core_session)
            activity = _query_rds_activity_metrics(cw_client, db_instance_id)
        except Exception:
            logger.warning("cmdb topology: metrics fallback failed for %s", db_instance_id, exc_info=True)
            activity = {"has_inbound": False, "has_outbound": False, "bytes_in": 0, "bytes_out": 0}

        nodes.append({"id": "net-generic", "kind": "network", "label": "Network"})
        edges.append({
            "id": "e-traffic-generic-in", "source": "net-generic", "target": root_id,
            "kind": "traffic", "direction": "inbound", "action": "ACCEPT",
            "bytes": activity["bytes_in"], "animated": activity["has_inbound"],
        })
        edges.append({
            "id": "e-traffic-generic-out", "source": root_id, "target": "net-generic",
            "kind": "traffic", "direction": "outbound", "action": "ACCEPT",
            "bytes": activity["bytes_out"], "animated": activity["has_outbound"],
        })

    return {
        "account_id": account_id,
        "account_name": account_name,
        "instance_id": db_instance_id,
        "resource_type": RESOURCE_TYPE_RDS,
        "nodes": nodes,
        "edges": edges,
        "has_flow_logs": has_flow_logs,
        "flow_log_truncated": flow_log_truncated,
        "lookback_minutes": _FLOW_LOG_LOOKBACK_MINUTES,
        "as_of": _now_iso(),
    }


# ── Load Balancer Topology ─────────────────────────────────────────────────

def _find_target_load_balancer(elbv2, lb_name: str) -> Optional[dict]:
    try:
        resp = elbv2.describe_load_balancers(Names=[lb_name])
    except Exception:
        logger.warning("cmdb topology: describe_load_balancers failed for %s", lb_name, exc_info=True)
        return None
    for lb in resp.get("LoadBalancers", []):
        if lb.get("LoadBalancerName") == lb_name:
            return lb
    return None


def _lb_target_chain_nodes_and_edges(elbv2, lb_arn: str, root_id: str) -> tuple[list[dict], list[dict]]:
    """LB → TargetGroup → Target chain (the "service chain" extension
    that goes further than EC2/RDS's flat instance→SG topology, per the
    earlier decision that this is a more accurate view of what an LB
    actually routes traffic to). Every edge here is a yellow "will
    access" context edge, not measured traffic — the LB's real inbound/
    outbound traffic is still handled separately via Flow Logs/metrics,
    same as EC2/RDS. Never raises — a describe_target_groups/
    describe_target_health failure just means no target-chain nodes are
    added, the rest of the diagram is unaffected."""
    nodes: list[dict] = []
    edges: list[dict] = []
    try:
        tg_resp = elbv2.describe_target_groups(LoadBalancerArn=lb_arn)
    except Exception:
        logger.warning("cmdb topology: describe_target_groups failed for %s", lb_arn, exc_info=True)
        return nodes, edges

    for tg in tg_resp.get("TargetGroups", []):
        tg_arn = tg.get("TargetGroupArn")
        tg_name = tg.get("TargetGroupName") or tg_arn
        tg_node_id = f"tg-{tg_name}"
        nodes.append({
            "id": tg_node_id, "kind": "target_group", "label": tg_name,
            "protocol": tg.get("Protocol"), "port": tg.get("Port"),
        })
        edges.append({"id": f"e-{root_id}-{tg_node_id}", "source": root_id, "target": tg_node_id, "kind": "context", "animated": True})

        try:
            health_resp = elbv2.describe_target_health(TargetGroupArn=tg_arn)
        except Exception:
            logger.warning("cmdb topology: describe_target_health failed for %s", tg_arn, exc_info=True)
            continue

        for th in health_resp.get("TargetHealthDescriptions", []):
            target_id = (th.get("Target") or {}).get("Id")
            if not target_id:
                continue
            target_node_id = f"target-{target_id}"
            nodes.append({
                "id": target_node_id, "kind": "target", "label": target_id,
                "health": (th.get("TargetHealth") or {}).get("State"),
            })
            edges.append({"id": f"e-{tg_node_id}-{target_node_id}", "source": tg_node_id, "target": target_node_id, "kind": "context", "animated": True})

    return nodes, edges


def get_lb_topology(account_id: str, lb_name: str) -> dict:
    """Load Balancer equivalent of get_ec2_topology(): the LB itself, its
    VPC/subnets/security groups, its TargetGroup→Target service chain
    (yellow context edges), and real Flow-Log-based traffic peers scoped
    to all of the LB's ENI private IPs (an LB has one ENI per AZ/subnet,
    unlike a single-homed EC2 instance) or a CloudWatch ProcessedBytes-
    based fallback indicator when no usable flow log exists."""
    if _should_use_mock_inventory():
        return _mock_lb_topology(account_id, lb_name)

    account_name = ACCOUNTS.get(account_id, account_id)
    core_session = _core_session()

    regions = _regions_for_account(account_id, core_session)
    lb, found_region, elbv2, ec2 = None, None, None, None
    for region in regions:
        try:
            candidate_elbv2 = _regional_client_for_account(account_id, "elbv2", region, core_session)
        except Exception:
            continue
        found = _find_target_load_balancer(candidate_elbv2, lb_name)
        if found:
            lb, found_region, elbv2 = found, region, candidate_elbv2
            try:
                ec2 = _regional_client_for_account(account_id, "ec2", region, core_session)
            except Exception:
                ec2 = None
            break

    if lb is None:
        raise CmdbTopologyError(f"Load Balancer {lb_name} was not found in account {account_id} ({account_name}) in any region")

    lb_arn = lb.get("LoadBalancerArn")
    vpc_id = lb.get("VpcId")
    lb_type = lb.get("Type")  # "application" | "network" | "gateway"
    sg_ids = lb.get("SecurityGroups") or []
    az_info = lb.get("AvailabilityZones") or []
    subnet_ids = [az.get("SubnetId") for az in az_info if az.get("SubnetId")]
    state = (lb.get("State") or {}).get("Code")
    dns_name = lb.get("DNSName")

    root_id = f"lb-{lb_name}"
    nodes: list[dict] = [{
        "id": root_id,
        "kind": "instance",
        "label": lb_name,
        "instance_id": lb_name,
        "lb_type": lb_type,
        "state": state,
        "scheme": lb.get("Scheme"),
        "dns_name": dns_name,
        "region": found_region,
        "resource_type": RESOURCE_TYPE_ELB,
    }]
    edges: list[dict] = []

    if vpc_id:
        nodes.append({"id": f"vpc-{vpc_id}", "kind": "vpc", "label": vpc_id, "vpc_id": vpc_id})
    for subnet_id in subnet_ids:
        nodes.append({"id": f"subnet-{subnet_id}", "kind": "subnet", "label": subnet_id, "subnet_id": subnet_id})
        if vpc_id:
            edges.append({"id": f"e-subnet-{subnet_id}-vpc", "source": f"subnet-{subnet_id}", "target": f"vpc-{vpc_id}", "kind": "context", "animated": True})

    # NLBs don't have SecurityGroups (they inherit target-instance SGs) —
    # sg_ids will simply be empty and _security_group_nodes returns [].
    sg_nodes = _security_group_nodes(ec2, sg_ids) if (ec2 and sg_ids) else []
    nodes.extend(sg_nodes)
    for sg_node in sg_nodes:
        edges.append({"id": f"e-{root_id}-{sg_node['sg_id']}", "source": root_id, "target": sg_node["id"], "kind": "context", "animated": True})
        for subnet_id in subnet_ids:
            edges.append({"id": f"e-{sg_node['sg_id']}-{subnet_id}", "source": sg_node["id"], "target": f"subnet-{subnet_id}", "kind": "context", "animated": True})

    if elbv2 and lb_arn:
        chain_nodes, chain_edges = _lb_target_chain_nodes_and_edges(elbv2, lb_arn, root_id)
        nodes.extend(chain_nodes)
        edges.extend(chain_edges)

    has_flow_logs = False
    flow_log_truncated = False

    lb_ips: list[str] = []
    if ec2 and vpc_id:
        try:
            eni_resp = ec2.describe_network_interfaces(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "description", "Values": [f"ELB {lb_name}", f"ELB app/{lb_name}/*", f"ELB net/{lb_name}/*"]},
            ])
            for eni in eni_resp.get("NetworkInterfaces", []):
                for addr in eni.get("PrivateIpAddresses", []):
                    ip = addr.get("PrivateIpAddress")
                    if ip:
                        lb_ips.append(ip)
        except Exception:
            logger.warning("cmdb topology: LB ENI lookup failed for %s", lb_name, exc_info=True)

    if vpc_id and ec2 and lb_ips:
        log_group = _vpc_flow_log_destination(ec2, vpc_id)
        if log_group:
            try:
                logs_client = _regional_client_for_account(account_id, "logs", found_region, core_session)
                result = _query_flow_log_peers(logs_client, log_group, lb_ips)
                peers, flow_log_truncated = result["peers"], result["truncated"]
                if peers:
                    has_flow_logs = True
                    _append_peers_to_graph(ec2, root_id, peers, nodes, edges)
            except Exception:
                logger.warning("cmdb topology: flow-log-based traffic lookup failed for %s", lb_name, exc_info=True)

    if not has_flow_logs:
        try:
            cw_client = _regional_client_for_account(account_id, "cloudwatch", found_region, core_session)
            activity = _query_lb_activity_metrics(cw_client, lb_name, lb_type)
        except Exception:
            logger.warning("cmdb topology: metrics fallback failed for %s", lb_name, exc_info=True)
            activity = {"has_inbound": False, "has_outbound": False, "bytes_in": 0, "bytes_out": 0}

        nodes.append({"id": "net-generic", "kind": "network", "label": "Network"})
        edges.append({
            "id": "e-traffic-generic-in", "source": "net-generic", "target": root_id,
            "kind": "traffic", "direction": "inbound", "action": "ACCEPT",
            "bytes": activity["bytes_in"], "animated": activity["has_inbound"],
        })
        edges.append({
            "id": "e-traffic-generic-out", "source": root_id, "target": "net-generic",
            "kind": "traffic", "direction": "outbound", "action": "ACCEPT",
            "bytes": activity["bytes_out"], "animated": activity["has_outbound"],
        })

    return {
        "account_id": account_id,
        "account_name": account_name,
        "instance_id": lb_name,
        "resource_type": RESOURCE_TYPE_ELB,
        "nodes": nodes,
        "edges": edges,
        "has_flow_logs": has_flow_logs,
        "flow_log_truncated": flow_log_truncated,
        "lookback_minutes": _FLOW_LOG_LOOKBACK_MINUTES,
        "as_of": _now_iso(),
    }
