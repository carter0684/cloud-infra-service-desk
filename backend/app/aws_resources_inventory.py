"""
AWS Resources tab — resource inventory sourced directly from the
`itinfra-compliance-reports` S3 bucket (account 100000000001 / Core account
Server), replacing the previous "external IP/URL dashboard" landing page.

## Data source

A separate Lambda-based scanner ("Resource Inventory Scanner") already
scans every AWS account/region and writes one JSON file per
account/region/resource-type to:

    s3://itinfra-compliance-reports/resource-inventory-scanner/{account_id}/{region}/{resource_type}.json

Each file is a raw JSON array-of-arrays (no embedded column headers) —
e.g. ec2.json contains
[[account_id, region, instance_id, instance_type, state, vpc_id,
  private_ip, ebs_str, name, tags_str], ...]. Column order for every
resource type was extracted directly from the scanner Lambda's own
Python parser functions (see _HEADERS below), not guessed from sample
data, since there is no schema embedded in the files themselves.

Two resource types (s3, route53) are global rather than per-region:
their rows carry only [account_id, ...] (no leading Region column, since
a bucket/hosted zone isn't tied to one region the way an EC2 instance
is). cloudfront is also a global service. All three are still stored
under a region-named S3 key path (the scanner writes an entry per region
it's invoked for) — but the scanner only ever populates real data for
them under the us-east-1 folder; every other region's file for these
types is an empty `[]`. This module handles that transparently by simply
skipping any file whose size is <=2 bytes (a bare `[]`) before ever
issuing a GetObject for it — no special-casing per resource type needed.

## Access pattern — deliberately simpler than Cost Explorer/Security Hub

Unlike Cost Explorer / Billing by Service / Security Hub (which each
query a MEMBER account's OWN AWS API and therefore need
sts:AssumeRole(PortalReadOnlyRole) into every account), this S3 bucket lives
directly IN Core account's own account. Reading it — regardless of
which account_id subfolder the data happens to be *about* — is just a
plain S3 ListObjectsV2/GetObject call using Core account's own static
credentials (_core_session() from cost_explorer.py). No cross-account
AssumeRole fan-out is needed here at all.

## Caching

- `list_accounts()` — discovers the actual top-level account-id
  "folders" present in the bucket right now (rather than assuming
  billing_by_service.ACCOUNT_ALIAS is a perfect match — the scanner may
  cover a different account set than the Cost/Billing tabs), cached for
  _INVENTORY_CACHE_TTL_SECONDS.
- `get_inventory_summary()` — per-account/per-resource-type "has any
  non-empty data" map, built from S3 object *sizes* only (no downloads),
  cached for _INVENTORY_CACHE_TTL_SECONDS.
- `get_resource_data()` — the real per-resource-type row fetch, cached
  per (account_ids, resource_type) for _DATA_CACHE_TTL_SECONDS.

ENV=local: no real AWS calls — a small deterministic mock dataset is
returned instead, consistent with every other AWS-backed module in this
app.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import boto3

from .billing_by_service import ACCOUNT_ALIAS
from .cache_utils import TTLCache
from .cost_explorer import _core_session

logger = logging.getLogger(__name__)

ENV = os.environ.get("ENV", "local")

# S3 bucket (in the Core account) where your resource-inventory scanner writes
# its JSON output. Set RESOURCE_INVENTORY_BUCKET; the AWS Resources tab needs
# ENABLED_FEATURES=resource_inventory.
S3_BUCKET = os.environ.get("RESOURCE_INVENTORY_BUCKET", "")
S3_PREFIX = os.environ.get("RESOURCE_INVENTORY_PREFIX", "resource-inventory-scanner")

# Sub-"folders" under the scanner prefix that are NOT account-id folders
# (e.g. the scanner's own Lambda deployment package lives at
# resource-inventory-scanner/deploy/scanner.zip) — excluded from account
# discovery.
_NON_ACCOUNT_PREFIXES = {"deploy"}

# Resource types this module knows how to render, with the exact column
# order each type's JSON file uses (as written by the scanner Lambda's
# own parse_*() functions — see module docstring). Every type except s3
# and route53 carries a leading [Account ID, Region, ...]; those two
# carry [Account ID, ...] only (region is embedded inside the row itself
# for s3, and omitted entirely for route53 since a hosted zone has no
# single region).
_HEADERS: dict[str, list[str]] = {
    "ec2": ["Account ID", "Region", "Instance ID", "Instance Type", "State", "VPC ID", "Private IP", "EBS Info", "Name", "Tags"],
    "eip": ["Account ID", "Region", "Public IP", "Allocation ID", "Instance ID", "Network Interface ID", "Tags"],
    "vpc": ["Account ID", "Region", "VPC ID", "CIDR Block", "Additional CIDRs", "State", "Name", "Tags"],
    "rds": ["Account ID", "Region", "DB Instance ID", "DB Instance Class", "Engine", "Engine Version", "Status", "Storage", "Storage Type", "Multi-AZ", "AZ Info", "Cluster ID", "Role", "Tags"],
    "alb": ["Account ID", "Region", "Load Balancer Name", "Type", "DNS Name", "WAF Info", "Tags"],
    "elasticache": ["Account ID", "Region", "Cache Cluster ID", "Node Type", "Engine", "Engine Version", "Status", "Cluster Info", "Tags"],
    "lambda": ["Account ID", "Region", "Function Name", "Runtime", "Handler", "Role", "Code Size", "Memory Size", "Timeout", "Tags"],
    "cloudfront": ["Account ID", "Region", "Distribution ID", "Domain Name", "Alternate Domains", "WAF Info", "Tags"],
    "acm": ["Account ID", "Region", "Certificate ID", "Domain Name", "SAN Domains", "Status", "Type", "Key Algorithm", "Renewal Eligibility", "Exportable", "Expiry Date", "Days Until Expiry", "In Use By", "Tags"],
    "tgw": ["Account ID", "Region", "TGW ID", "Name", "State", "Route Table ID", "Route Count", "Route Info", "Tags"],
    "vpn": ["Account ID", "Region", "VPN Connection ID", "Name", "Customer Gateway ID", "CGW Name", "CGW IP", "Target Gateway", "Tunnel Status", "Tags"],
    "eks": ["Account ID", "Region", "Cluster Name", "Status", "Version", "VPC ID", "Compute Type", "Fargate Profiles", "Node Groups", "Tags"],
    "ecs": ["Account ID", "Region", "Cluster Name", "Status", "Running Tasks", "Pending Tasks", "Active Services", "Service Details", "Tags"],
    "ebs": ["Account ID", "Region", "Volume ID", "Create Time", "Size", "Volume Type", "IOPS", "Throughput", "Availability Zone", "State", "Attached To", "Encryption", "Tags"],
    "vpce": ["Account ID", "Region", "Endpoint ID", "Name", "VPC ID", "Service Name", "Type", "State", "Subnet IDs", "Route Table IDs", "Security Groups", "Tags"],
    "cwlogs": ["Account ID", "Region", "Log Group Name", "Retention (Days)", "Stored Size", "Creation Time", "KMS Key ID", "Tags"],
    "backup": ["Account ID", "Region", "Plan ID", "Plan Name", "Rule Name", "Schedule", "Retention (Days)", "Vault Name", "Vault ARN", "Vault KMS Key ARN", "Tags"],
    # Global resource types (no per-row Region column)
    "s3": ["Account ID", "Bucket Name", "Region", "Creation Date", "Size", "Versioning", "Encryption", "Lifecycle", "Tags"],
    "route53": ["Account ID", "Zone ID", "Name", "Zone Type", "Record Count", "Tags"],
}

# Friendly labels + icons for the frontend's resource-type tab bar, in
# display order (compute -> containers -> data -> network -> storage ->
# other).
RESOURCE_TYPES: list[dict] = [
    {"id": "ec2", "label": "EC2 Instances", "icon": "\U0001F5A5\uFE0F"},
    {"id": "lambda", "label": "Lambda", "icon": "\u03BB"},
    {"id": "ecs", "label": "ECS", "icon": "\U0001F4E6"},
    {"id": "eks", "label": "EKS", "icon": "\u2638\uFE0F"},
    {"id": "rds", "label": "RDS", "icon": "\U0001F5C4\uFE0F"},
    {"id": "elasticache", "label": "ElastiCache", "icon": "\u26A1"},
    {"id": "alb", "label": "ALB", "icon": "\u2696\uFE0F"},
    {"id": "vpc", "label": "VPC", "icon": "\U0001F310"},
    {"id": "eip", "label": "Elastic IPs", "icon": "\U0001F4CD"},
    {"id": "vpce", "label": "VPC Endpoints", "icon": "\U0001F517"},
    {"id": "tgw", "label": "Transit Gateway", "icon": "\U0001F500"},
    {"id": "vpn", "label": "VPN Connections", "icon": "\U0001F510"},
    {"id": "ebs", "label": "EBS Volumes", "icon": "\U0001F4BE"},
    {"id": "s3", "label": "S3 Buckets", "icon": "\U0001FAA3"},
    {"id": "cloudfront", "label": "CloudFront", "icon": "\u2601\uFE0F"},
    {"id": "route53", "label": "Route 53", "icon": "\U0001F30D"},
    {"id": "acm", "label": "ACM Certificates", "icon": "\U0001F512"},
    {"id": "cwlogs", "label": "CloudWatch Logs", "icon": "\U0001F4CB"},
    {"id": "backup", "label": "AWS Backup", "icon": "\U0001F5C2\uFE0F"},
]
_VALID_RESOURCE_TYPE_IDS = {rt["id"] for rt in RESOURCE_TYPES}


# The resource-inventory-scanner Lambda itself only re-scans every AWS
# account/region on its own (infrequent) schedule — there's no point
# re-hitting S3 every few minutes like the Cost Explorer/Security Hub
# modules do, since the underlying data in the bucket genuinely doesn't
# change that often. Both caches therefore auto-refresh once every 12
# hours; every fetch (whether it's the automatic 12-hour refresh or a
# user-triggered one) always reads whatever is CURRENTLY in S3 at that
# moment — there is no separate/older data source, so a fetch is always
# "the latest S3 has", never stale-by-design data. The "🔄 Refresh"
# button on the AWS Resources page passes force_refresh=True, which
# bypasses this 12-hour TTL entirely and re-reads S3 immediately,
# regardless of how recently the last automatic refresh happened.
_INVENTORY_CACHE_TTL_SECONDS = 12 * 60 * 60  # metadata (S3 object listings)
_DATA_CACHE_TTL_SECONDS = 12 * 60 * 60  # actual row data
_inventory_cache = TTLCache(_INVENTORY_CACHE_TTL_SECONDS)
_data_cache = TTLCache(_DATA_CACHE_TTL_SECONDS)


class AwsResourcesInventoryError(Exception):
    """Raised for inventory-fetch-level failures (e.g. cannot load the organization
    Core credentials, or cannot access the S3 bucket at all). A single
    account's or a single file's fetch failure is instead swallowed and
    logged (that account/file is simply skipped/empty) so one bad
    account/region never blanks out the rest of the page."""


def _mock_accounts() -> list[dict]:
    return [
        {"account_id": "100000000001", "alias": "core"},
        {"account_id": "100000000003", "alias": "network"},
    ]


def _mock_inventory_summary() -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "accounts": _mock_accounts(),
        "resource_types": RESOURCE_TYPES,
        "availability": {
            "100000000001": {"ec2": True, "vpc": True, "s3": True},
            "100000000003": {"ec2": True},
        },
        "as_of": now,
        "is_mock": True,
    }


def _mock_resource_data(resource_type: str) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = _HEADERS.get(resource_type, [])
    if resource_type == "ec2":
        rows = [
            ["100000000001", "ap-southeast-1", "i-0abc123mock", "t3.medium", "running", "vpc-0abc123", "10.0.1.15", "/dev/xvda: 30GB gp3", "mock-web-01", "Environment=Production"],
            ["100000000003", "ap-southeast-1", "i-0def456mock", "t3.small", "stopped", "vpc-0def456", "10.1.2.20", "/dev/xvda: 20GB gp3", "mock-api-01", ""],
        ]
    elif resource_type == "vpc":
        rows = [["100000000001", "ap-southeast-1", "vpc-0abc123", "10.0.0.0/16", "None", "available", "mock-vpc", "Environment=Production"]]
    elif resource_type == "s3":
        rows = [["100000000001", "mock-bucket-1", "ap-southeast-1", "2024-01-01", "1.20 GB", "Enabled", "AES256", "None", "Environment=Production"]]
    else:
        rows = []
    return {
        "headers": headers, "rows": rows, "resource_type": resource_type,
        "row_count_per_account": {}, "as_of": now, "is_mock": True,
    }


def _s3_client(core_session: boto3.Session):
    return core_session.client("s3", region_name="us-east-1")


def _discover_account_ids(core_session: boto3.Session) -> list[str]:
    """List the top-level account-id 'folders' actually present under
    `resource-inventory-scanner/` right now — discovered live rather
    than assumed from billing_by_service.ACCOUNT_ALIAS, since the
    scanner may cover a different set of accounts than the Cost/Billing
    tabs do."""
    s3 = _s3_client(core_session)
    account_ids: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{S3_PREFIX}/", Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            prefix = cp.get("Prefix", "")
            # e.g. "resource-inventory-scanner/100000000001/" -> "100000000001"
            parts = prefix.rstrip("/").split("/")
            if parts and parts[-1] and parts[-1] not in _NON_ACCOUNT_PREFIXES:
                account_ids.append(parts[-1])
    return account_ids


def list_accounts() -> list[dict]:
    """Account picker data for the AWS Resources page — the actual
    account-id folders present in the S3 bucket right now, with a
    friendly alias from billing_by_service.ACCOUNT_ALIAS when known
    (falling back to the raw account_id otherwise)."""
    if ENV == "local":
        return _mock_accounts()

    def _fetch() -> dict:
        core_session = _core_session()
        ids = _discover_account_ids(core_session)
        return {
            "accounts": [
                {"account_id": acct_id, "alias": ACCOUNT_ALIAS.get(acct_id, acct_id)}
                for acct_id in sorted(ids)
            ]
        }

    try:
        result = _inventory_cache.get_or_fetch("accounts", _fetch)
    except Exception as e:
        raise AwsResourcesInventoryError(f"Could not list AWS Resources accounts: {e}") from e
    return result["accounts"]


def _list_account_object_sizes(account_id: str, core_session: boto3.Session) -> dict[tuple[str, str], int]:
    """Return {(region, resource_type): size_in_bytes} for every object
    under this account's prefix — one (paginated) ListObjectsV2 call,
    metadata only (no downloads). Used both to build the availability
    map and, when actually fetching data, to skip definitely-empty
    (a bare `[]` = 2 bytes) files before ever issuing a GetObject."""
    s3 = _s3_client(core_session)
    sizes: dict[tuple[str, str], int] = {}
    prefix = f"{S3_PREFIX}/{account_id}/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            rel = key[len(prefix):]
            parts = rel.split("/")
            if len(parts) != 2 or not parts[1].endswith(".json"):
                continue
            region = parts[0]
            resource_type = parts[1][: -len(".json")]
            sizes[(region, resource_type)] = obj.get("Size", 0)
    return sizes


def get_inventory_summary(account_ids: Optional[list[str]] = None, force_refresh: bool = False) -> dict:
    """Which accounts have ANY non-empty data for which resource type —
    metadata-only (sizes from ListObjectsV2, no downloads), used to grey
    out empty account/resource-type combinations in the picker rather
    than the user discovering emptiness only after clicking into them.
    Cached for _INVENTORY_CACHE_TTL_SECONDS per exact account-id
    selection."""
    if ENV == "local":
        return _mock_inventory_summary()

    accounts = list_accounts()
    if account_ids:
        wanted = set(account_ids)
        accounts = [a for a in accounts if a["account_id"] in wanted]

    cache_key = ("summary", tuple(sorted(a["account_id"] for a in accounts)))

    def _fetch() -> dict:
        core_session = _core_session()
        availability: dict[str, dict[str, bool]] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(_list_account_object_sizes, a["account_id"], core_session): a["account_id"]
                for a in accounts
            }
            for fut in as_completed(futures):
                acct_id = futures[fut]
                try:
                    sizes = fut.result()
                except Exception as e:
                    logger.warning("aws_resources_inventory: could not list objects for account %s: %s", acct_id, e)
                    sizes = {}
                per_type: dict[str, bool] = {}
                for (_region, resource_type), size in sizes.items():
                    if resource_type not in _VALID_RESOURCE_TYPE_IDS:
                        continue
                    if size > 2:
                        per_type[resource_type] = True
                availability[acct_id] = per_type
        return {
            "accounts": accounts,
            "resource_types": RESOURCE_TYPES,
            "availability": availability,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "is_mock": False,
        }

    try:
        return _inventory_cache.get_or_fetch(cache_key, _fetch, force_refresh=force_refresh)
    except Exception as e:
        raise AwsResourcesInventoryError(f"Could not build AWS Resources inventory summary: {e}") from e


def _fetch_resource_file(account_id: str, region: str, resource_type: str, core_session: boto3.Session) -> list[list]:
    s3 = _s3_client(core_session)
    key = f"{S3_PREFIX}/{account_id}/{region}/{resource_type}.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        body = obj["Body"].read()
    except Exception as e:
        logger.warning("aws_resources_inventory: could not fetch s3://%s/%s: %s", S3_BUCKET, key, e)
        return []
    try:
        rows = json.loads(body)
    except Exception as e:
        logger.warning("aws_resources_inventory: could not parse s3://%s/%s as JSON: %s", S3_BUCKET, key, e)
        return []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, list)]


def get_resource_data(account_ids: list[str], resource_type: str, force_refresh: bool = False) -> dict:
    """Fetch every row for `resource_type` across every region and every
    account in `account_ids`, concatenated as-is — rows already carry
    their own account_id [+ region, except for the global s3/route53
    types] as their leading columns exactly as the scanner wrote them;
    nothing is re-derived or re-ordered here. Cached per exact
    (account_ids, resource_type) selection for _DATA_CACHE_TTL_SECONDS."""
    if resource_type not in _VALID_RESOURCE_TYPE_IDS:
        raise ValueError(f"Unknown resource_type: {resource_type!r}")

    if ENV == "local":
        return _mock_resource_data(resource_type)

    cache_key = ("data", tuple(sorted(account_ids)), resource_type)

    def _fetch() -> dict:
        core_session = _core_session()
        all_rows: list[list] = []
        row_count_per_account: dict[str, int] = {}

        def _account_rows(account_id: str) -> tuple[str, list[list]]:
            sizes = _list_account_object_sizes(account_id, core_session)
            rows: list[list] = []
            for (region, rtype), size in sizes.items():
                if rtype != resource_type or size <= 2:
                    continue
                rows.extend(_fetch_resource_file(account_id, region, resource_type, core_session))
            return account_id, rows

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_account_rows, acct_id) for acct_id in account_ids]
            for fut in as_completed(futures):
                try:
                    acct_id, rows = fut.result()
                except Exception as e:
                    logger.warning("aws_resources_inventory: could not fetch account rows: %s", e)
                    continue
                row_count_per_account[acct_id] = len(rows)
                all_rows.extend(rows)

        return {
            "headers": _HEADERS.get(resource_type, []),
            "rows": all_rows,
            "resource_type": resource_type,
            "row_count_per_account": row_count_per_account,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "is_mock": False,
        }

    try:
        return _data_cache.get_or_fetch(cache_key, _fetch, force_refresh=force_refresh)
    except Exception as e:
        raise AwsResourcesInventoryError(f"Could not fetch {resource_type} data: {e}") from e
