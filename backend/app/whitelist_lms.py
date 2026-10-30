"""
"Whitelist" (LMS) tool — Service Desk portal feature that lets a user
check whether an IP is already whitelisted in one of a fixed list of
AWS WAFv2 IPSets / EC2 Security Groups used by the LMS platform, and
(after supplying a Service Request number) add it if not.

## Target resources

A fixed, code-defined list (DEFAULT_TARGETS below) of 8 named resources
the user picks from — 7 are WAFv2 IPSets, 1 is an EC2 Security Group
(identified directly by its group ID). That Security Group was
originally "sg-021238ff0c0e522cc" (AVPC-SFTP-Client-SG-02); it was
SWAPPED on 2026-09-16 for "sg-07cb8e11e83e9efb3" (AVPC-SFTP-Client-SG-03)
because SG-02's IP allow-list was full and could not accept more entries
— per explicit user request. SG-02 is no longer referenced anywhere in
this module; any historical whitelist_store audit rows still say
"sg-021238ff0c0e522cc (AVPC-SFTP-Client-SG-02)" (audit rows record the
label AT THE TIME of the action, never rewritten retroactively — see
whitelist_store.py) but new checks/adds all go against SG-03.

Of the 7 WAFv2 IPSets, each has an explicit "waf_scope" — CONFIRMED live
against real AWS (2026-09-08), NOT auto-detected, because scope is
ambiguous by name alone (see next paragraph):
  - REGIONAL (protects an ALB/API Gateway): PRO-LMS3-BOSWAPP-Whitelist,
    PROD-WASAPP-Whitelist, ipv4-sets_pre-booking_coupon,
    ipv4-sets_bank-partner-portal — all confirmed in ap-southeast-1.
  - CLOUDFRONT ("Global" scope in the AWS WAF console — protects a
    CloudFront distribution, always queried via the us-east-1 endpoint
    regardless of where end-user traffic actually originates):
    newlms-prod-ipset-whitelist-partnerportal,
    newlms-prod-ipset-whitelist-backend,
    newlms-prod-ipset-whitelist-lmsprod — all confirmed in us-east-1,
    NOT present in REGIONAL scope in either candidate region.

IMPORTANT AMBIGUITY, discovered during the same live check: two names —
ipv4-sets_pre-booking_coupon and ipv4-sets_bank-partner-portal — exist
under BOTH scopes simultaneously (REGIONAL in ap-southeast-1 AND
CLOUDFRONT in us-east-1), with DIFFERENT IPSet Ids and DIFFERENT
Description fields under each scope — i.e. they are two genuinely
separate IP allow-lists that happen to share a name, not the same
resource visible two ways. This module is currently wired to the
REGIONAL copy for both (matching the ORIGINAL "protects an ALB/API
Gateway, not a CloudFront distribution" design assumption for these two
targets specifically) — this has NOT been re-confirmed against the user
as the intended one now that the CLOUDFRONT sibling is known to exist;
flag this to the user before assuming it's correct.

## Region — AUTO-DETECTED for REGIONAL-scope targets, FIXED for
## CLOUDFRONT-scope targets — never chosen by the user

Per explicit user request, the region is NOT a field the user picks.

For the 4 REGIONAL-scope WAFv2 IPSet targets and the 1 Security Group
target, this module automatically figures out which of the two
candidate regions (VALID_REGIONS: us-east-1, ap-southeast-1) that
specific resource actually lives in, by trying us-east-1 first and
falling back to ap-southeast-1 (_locate_ip_set() / _locate_security_group()
below). The resolved region is cached per resource name/group-id
in-process (_TARGET_REGION_CACHE) so repeat checks/adds against the
same target don't re-probe both regions every single call — only the
very first lookup for a given target (since this process started) pays
the cost of possibly checking both.

For the 3 CLOUDFRONT-scope WAFv2 IPSet targets, region is NOT
auto-detected — it's always us-east-1, per AWS's own requirement that
CLOUDFRONT-scope WAFv2 API calls go through the us-east-1 endpoint
regardless of where the protected CloudFront distribution's edge
locations or origin actually are. No probing/fallback happens for these.

Caveats of the REGIONAL-scope auto-detection, both flagged explicitly
rather than silently assumed correct:
  - If a resource with the exact same Name genuinely exists in BOTH
    regions (extremely unlikely for these internal, purpose-named
    IPSets, but not literally impossible), us-east-1 wins — this module
    has no way to disambiguate further since the user never supplies a
    region.
  - If a target is not found in EITHER region, check_ip()/add_ip() raise
    a clean WhitelistLmsError naming both regions that were tried,
    rather than a confusing single-region "not found".

WHITELIST_LMS_ACCOUNT_ID (below) is still a single fixed account across
both regions/both scopes — only the region (for REGIONAL-scope targets)
is auto-detected, not the account itself.

## Account & credentials — REQUIRES NEW, NOT-YET-PROVISIONED SETUP

Every existing cross-account credential in this codebase
(cost_explorer._core_session -> AssumeRole(PortalReadOnlyRole)) is
READ-ONLY across the whole org — confirmed by grep across every module
that uses it; no Create/Update/Put/Delete/Authorize/Revoke/Modify call
exists anywhere against those credentials. This feature needs to WRITE
(wafv2:UpdateIPSet, ec2:AuthorizeSecurityGroupIngress), so — following
screensaver.py's exact precedent (a brand-new, narrowly-scoped IAM
identity for a brand-new write capability, never reusing PortalReadOnlyRole
or any other existing credential) — this module expects its OWN
dedicated IAM user/role + Secrets Manager secret
(`job-portal/whitelist-lms-credentials`), which does NOT exist yet and
must be provisioned manually (same as screensaver.py's
`job-portal-screensaver-s3` user and CloudFront distribution were —
see that module's docstring) directly in whichever AWS account hosts
these 8 resources, scoped to ONLY:
  - wafv2:GetIPSet, wafv2:ListIPSets, wafv2:UpdateIPSet
  - ec2:DescribeSecurityGroups, ec2:AuthorizeSecurityGroupIngress,
    ec2:RevokeSecurityGroupIngress
  restricted (via resource ARN conditions where the API supports them)
  to just these 8 named resources — never a broader wafv2:*/ec2:* grant.

WHITELIST_LMS_ACCOUNT_ID defaults to "100000000004" ("was-prod") —
CONFIRMED live against real AWS (2026-09-08) via
`aws wafv2 list-ip-sets --scope REGIONAL --region ap-southeast-1` and
`aws ec2 describe-security-groups`, run with the dedicated
job-portal-whitelist-lms IAM user's own credentials (not just
inference from VPC naming convention anymore). Of the 8 TARGETS, 5 were
found directly in this account (all in ap-southeast-1, not us-east-1):
PRO-LMS3-BOSWAPP-Whitelist, PROD-WASAPP-Whitelist,
ipv4-sets_pre-booking_coupon, ipv4-sets_bank-partner-portal, and the
sg-021238ff0c0e522cc Security Group (its live ingress rule already uses
port 22/tcp, confirming DEFAULT_SG_PORT/DEFAULT_SG_PROTOCOL below). The
remaining 3 — newlms-prod-ipset-whitelist-partnerportal / -backend /
-lmsprod — were NOT found in either region of this account as of that
same check; they may live under a different account (100000000016
"prod-lms" or 100000000014 "lms-sandbox" are the next-most-likely
candidates per cidr-portal's seed_data_excel.py) or may not exist yet.
check_ip()/add_ip() against those 3 will surface a clean "not found in
either region" WhitelistLmsError until this is resolved — override
WHITELIST_LMS_ACCOUNT_ID via env var if they turn out to live elsewhere.

## Security Group whitelisting needs a port/protocol

Unlike a WAF IPSet (which has no notion of port — it's an IP allow-list
consulted by a Web ACL rule), authorizing an EC2 Security Group ingress
rule requires a protocol+port. The one SG target here
("AVPC-SFTP-Client-SG-03", see "Target resources" above for the SG-02 ->
SG-03 swap) is assumed, from its name, to gate SFTP (TCP/22) —
DEFAULT_SG_PORT/DEFAULT_SG_PROTOCOL below encode that assumption
explicitly so it's easy to find and correct. Each SG-type target can
also carry its OWN "port"/"protocol" override (used first, before the
module-level default) — see "Admin-editable target registry" below.
The frontend also lets a user override the port for an SG-type target
per-request if even that stored default is wrong for a particular add.

## Admin-editable target registry (overlay on top of the fixed defaults)

DEFAULT_TARGETS below remains the fixed, code-defined seed list (the
original 8, with SG-03 as of the swap above). On top of it, an admin can
add/edit/remove targets from the Admin-only section of the Whitelist
(LMS) page without a code deploy:
  - Adding a Security Group target: admin types the group_id + a display
    label directly (no AWS lookup needed — EC2 security groups have no
    ambiguity-by-name problem the way WAFv2 IPSets do).
  - Adding a WAF IPSet target: admin types only the IPSet's Name;
    resolve_waf_ipset_name() below probes every (scope, region)
    combination this module can reach (REGIONAL in both VALID_REGIONS,
    CLOUDFRONT in CLOUDFRONT_SCOPE_REGION) and returns every match found
    — usually exactly one, but see "IMPORTANT AMBIGUITY" above for why
    it can genuinely be more than one. The admin picks the right
    scope/region if there's more than one match; nothing is persisted by
    resolve_waf_ipset_name() itself.
  - Editing a target (default or custom): label / linked_domain / (SG
    only) port+protocol. Changing WHICH underlying AWS resource a target
    points at is deliberately NOT supported as an "edit" — remove the
    target and add it again instead, so the audit trail always shows a
    clean add/remove pair rather than a resource identity silently
    changing under an existing target_id.
  - Removing a target: a DEFAULT target is hidden (soft-delete via
    removed_ids, restorable by re-adding), a CUSTOM (admin-added) target
    is deleted outright.

Persistence reuses settings_store.py's existing generic key/value
pattern (get_setting/set_setting) — same approach as TAB_ACCESS_KEY and
GROUP_CATEGORY_ACCESS_KEY in that module — under the single key
WHITELIST_TARGETS_OVERLAY_KEY, storing
{"custom_targets": [...], "overrides": {target_id: {...}},
"removed_ids": [...]}. No new DynamoDB table/SQLite table needed: this
is a tiny, admin-curated list, the same tradeoff every other settings_
store-backed feature already makes.

### linked_domain — for display only, not enforced by AWS

Every target (default or custom) carries an optional free-text
"linked_domain" field — e.g. "lms.example.com" — purely so the
Whitelist (LMS) page can show users which WAF IPSet/Security Group
protects which actual domain/URL. AWS itself has no API that connects a
WAFv2 IPSet or an EC2 Security Group directly to a hostname (a Web ACL
associates with an ALB/CloudFront distribution, which separately has its
own listener rules/aliases — there is no single "IPSet -> domain" call).
This field is therefore ENTIRELY admin-entered/curated, never auto-
populated or auto-verified against AWS — the 8 original DEFAULT_TARGETS
ship with linked_domain=None until an admin fills it in, rather than
this module guessing a domain from the resource's name and being wrong.

### IMPORTANT — adding a target here does NOT grant it AWS permissions

Registering a target through this admin UI only teaches the PORTAL about
an AWS resource that must already exist (created by the admin/security
team directly in AWS, per the module's original design intent — see
"Account & credentials" below). It does NOT touch IAM in any way:
  - CHECK works immediately for ANY resource in the account the
    job-portal-whitelist-lms credentials can reach, because
    wafv2:ListIPSets/GetIPSet and ec2:DescribeSecurityGroups are already
    granted account-wide (see "Account & credentials" below) — there was
    no way to scope List/Describe itself to specific ARNs.
  - ADD (the actual WRITE — wafv2:UpdateIPSet /
    ec2:AuthorizeSecurityGroupIngress) will fail with AccessDenied for a
    newly registered target until whoever manages the
    job-portal-whitelist-lms IAM user's inline policy (in account
    100000000004, external to this codebase's CDK) manually adds that
    specific resource's ARN — the same one-time manual step that was
    needed for each of the original 8 targets (and for the SG-02 -> SG-03
    swap above). This module deliberately never asks for or uses a
    broader wafv2:*/ec2:* grant just to make new targets "work
    automatically" — see "Account & credentials" below for why.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import uuid
from typing import Optional

import boto3

from . import settings_store

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")  # region for Secrets Manager (portal's own account)

# See module docstring — best-available inference, not confirmed.
WHITELIST_LMS_ACCOUNT_ID = os.environ.get("WHITELIST_LMS_ACCOUNT_ID", "")

WHITELIST_LMS_CREDENTIALS_SECRET_NAME = os.environ.get(
    "WHITELIST_LMS_CREDENTIALS_SECRET_NAME", "job-portal/whitelist-lms-credentials"
)

VALID_REGIONS = ("us-east-1", "ap-southeast-1")

# See module docstring's "Security Group whitelisting needs a port/
# protocol" section.
DEFAULT_SG_PORT = 22
DEFAULT_SG_PROTOCOL = "tcp"


class WhitelistLmsError(ValueError):
    """Raised for invalid input or a total inability to reach AWS. Safe
    to show the user as-is."""


# ── Target registry ──────────────────────────────────────────────────────
# Every "waf_ipset" entry carries an explicit "waf_scope" — "regional"
# (region auto-detected between us-east-1/ap-southeast-1) or "cloudfront"
# (always us-east-1, AWS's own CLOUDFRONT-scope requirement) — CONFIRMED
# live per-target (see module docstring's "Target resources" section),
# never inferred/guessed.

DEFAULT_TARGETS: list[dict] = [
    {"id": "pro_lms3_boswapp", "label": "PRO-LMS3-BOSWAPP-Whitelist", "resource_type": "waf_ipset", "waf_scope": "regional", "linked_domain": None},
    {
        # Swapped 2026-09-16: sg-021238ff0c0e522cc (SG-02) -> sg-07cb8e11e83e9efb3
        # (SG-03) — SG-02's IP allow-list was full and could not accept
        # more entries. See module docstring's "Target resources"
        # section for the full history/rationale.
        "id": "avpc_sftp_client_sg_02", "label": "sg-07cb8e11e83e9efb3 (AVPC-SFTP-Client-SG-03)",
        "resource_type": "security_group", "group_id": "sg-07cb8e11e83e9efb3", "linked_domain": None,
    },
    {"id": "newlms_partnerportal", "label": "newlms-prod-ipset-whitelist-partnerportal", "resource_type": "waf_ipset", "waf_scope": "cloudfront", "linked_domain": None},
    {"id": "newlms_backend", "label": "newlms-prod-ipset-whitelist-backend", "resource_type": "waf_ipset", "waf_scope": "cloudfront", "linked_domain": None},
    {"id": "newlms_lmsprod", "label": "newlms-prod-ipset-whitelist-lmsprod", "resource_type": "waf_ipset", "waf_scope": "cloudfront", "linked_domain": None},
    # NOTE: both of the following also exist under CLOUDFRONT scope in
    # us-east-1 (with different IDs/descriptions — see module docstring's
    # "IMPORTANT AMBIGUITY" paragraph). Currently wired to REGIONAL, the
    # original design assumption — NOT yet re-confirmed with the user now
    # that the CLOUDFRONT sibling is known to exist.
    {"id": "ipv4_prebooking_coupon", "label": "ipv4-sets_pre-booking_coupon", "resource_type": "waf_ipset", "waf_scope": "regional", "linked_domain": None},
    {"id": "ipv4_bank_partner_portal", "label": "ipv4-sets_bank-partner-portal", "resource_type": "waf_ipset", "waf_scope": "regional", "linked_domain": None},
    {"id": "prod_wasapp", "label": "PROD-WASAPP-Whitelist", "resource_type": "waf_ipset", "waf_scope": "regional", "linked_domain": None},
]

CLOUDFRONT_SCOPE_REGION = "us-east-1"  # AWS's own hard requirement — see module docstring


# ── Admin-editable overlay (see module docstring's "Admin-editable
# target registry" section) ──────────────────────────────────────────────
# Persisted via settings_store.py's generic key/value store — same
# pattern TAB_ACCESS_KEY/GROUP_CATEGORY_ACCESS_KEY already use. Shape:
#   {"custom_targets": [target_dict, ...],
#    "overrides": {target_id: {label?, linked_domain?, port?, protocol?}},
#    "removed_ids": [target_id, ...]}
# custom_targets are full target dicts (same shape as DEFAULT_TARGETS
# entries, plus "is_custom": True); overrides only ever patches a
# DEFAULT target's label/linked_domain/port/protocol — never its
# resource_type/waf_scope/group_id/label-used-for-AWS-lookup, so an edit
# can never silently repoint a target at a different underlying AWS
# resource (see docstring — that's a remove + re-add, by design).

WHITELIST_TARGETS_OVERLAY_KEY = "whitelist_lms_targets_overlay"

_EDITABLE_OVERRIDE_FIELDS = ("label", "linked_domain", "port", "protocol")


def _get_overlay() -> dict:
    stored = settings_store.get_setting(WHITELIST_TARGETS_OVERLAY_KEY)
    value = (stored.get("value") if stored else None) or {}
    return {
        "custom_targets": list(value.get("custom_targets") or []),
        "overrides": dict(value.get("overrides") or {}),
        "removed_ids": list(value.get("removed_ids") or []),
    }


def _set_overlay(overlay: dict, *, actor_email: str, actor_name: str) -> None:
    settings_store.set_setting(
        WHITELIST_TARGETS_OVERLAY_KEY, overlay, actor_email=actor_email, actor_name=actor_name,
    )


def _effective_targets() -> list[dict]:
    """DEFAULT_TARGETS with the admin overlay applied: removed_ids
    filtered out, overrides merged onto matching default targets (custom
    targets never receive an override — they're edited by replacing their
    own dict in custom_targets instead, see update_target() below), then
    custom_targets appended. This is the ONLY place TARGETS_BY_ID-style
    lookups should read from once the overlay exists — every public
    function below goes through this rather than DEFAULT_TARGETS
    directly, so an admin-added/edited target behaves identically to a
    built-in one everywhere (check_ip/add_ip/list_targets/...)."""
    overlay = _get_overlay()
    removed = set(overlay["removed_ids"])
    overrides = overlay["overrides"]

    result = []
    for t in DEFAULT_TARGETS:
        if t["id"] in removed:
            continue
        merged = dict(t)
        override = overrides.get(t["id"])
        if override:
            for field in _EDITABLE_OVERRIDE_FIELDS:
                if field in override and override[field] is not None:
                    merged[field] = override[field]
        merged["is_custom"] = False
        result.append(merged)

    for t in overlay["custom_targets"]:
        merged = dict(t)
        merged["is_custom"] = True
        result.append(merged)

    return result


def _targets_by_id() -> dict[str, dict]:
    return {t["id"]: t for t in _effective_targets()}

# Resolved region per target, cached for the lifetime of this process —
# see module docstring's "Region — AUTO-DETECTED" section. Keyed by the
# WAF IPSet's Name (target["label"]) or the Security Group's group_id,
# NOT by target_id, since that's what the two _locate_*() helpers below
# actually probe against.
_TARGET_REGION_CACHE: dict[str, str] = {}


def list_targets() -> list[dict]:
    """Public target list for the frontend's picker — no AWS call. Every
    field here (including linked_domain and is_custom) is safe to show
    to any authenticated user; the admin-only fields (group_id, the raw
    AWS lookup label for a WAF IPSet) are also included since they're
    just AWS resource identifiers, not secrets — the actual credentials
    stay server-side (see "Credentials / clients" below)."""
    return [
        {
            "id": t["id"], "label": t["label"], "resource_type": t["resource_type"],
            "linked_domain": t.get("linked_domain"), "is_custom": t.get("is_custom", False),
        }
        for t in _effective_targets()
    ]


def list_targets_admin() -> list[dict]:
    """Full target detail (waf_scope/group_id/port/protocol included) for
    the Admin-only "Manage Whitelist Targets" panel — everything
    list_targets() returns, plus the fields needed to render/edit each
    target's underlying AWS resource reference."""
    return _effective_targets()


# ── Credentials / clients ────────────────────────────────────────────────

def _get_secret(secret_name: str) -> str:
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]


def _get_whitelist_credentials() -> dict:
    """Static IAM keys for the dedicated `whitelist-lms` write-capable
    identity — see module docstring's "REQUIRES NEW, NOT-YET-PROVISIONED
    SETUP" section. Same local-dev-env-var-override convenience as every
    other cross-account credential in this codebase."""
    env_key = os.environ.get("WHITELIST_LMS_ACCESS_KEY_ID")
    env_secret = os.environ.get("WHITELIST_LMS_SECRET_ACCESS_KEY")
    if env_key and env_secret:
        return {"aws_access_key_id": env_key, "aws_secret_access_key": env_secret}

    try:
        secret_str = _get_secret(WHITELIST_LMS_CREDENTIALS_SECRET_NAME)
        creds = json.loads(secret_str)
    except Exception as e:
        raise WhitelistLmsError(f"Could not load Whitelist (LMS) credentials: {e}") from e
    if "aws_access_key_id" not in creds or "aws_secret_access_key" not in creds:
        raise WhitelistLmsError("Whitelist (LMS) credentials secret is missing required keys")
    return creds


def _client(service: str, region: str):
    """Fresh client per call, deliberately not cached module-globally
    (unlike screensaver.py's _s3()/_cloudfront()) — this needs a
    DIFFERENT region per call depending on which region the user picked
    for this specific request, unlike screensaver.py's single fixed
    bucket region."""
    creds = _get_whitelist_credentials()
    return boto3.client(
        service, region_name=region,
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
    )


# ── Input validation ─────────────────────────────────────────────────────

def _validate_ip(ip_address: str) -> str:
    ip_address = (ip_address or "").strip()
    if not ip_address:
        raise WhitelistLmsError("Please enter an IP address.")
    try:
        ipaddress.ip_address(ip_address)
    except ValueError as e:
        raise WhitelistLmsError(f"'{ip_address}' is not a valid IP address.") from e
    return ip_address


def _get_target(target_id: str) -> dict:
    target = _targets_by_id().get(target_id)
    if not target:
        raise WhitelistLmsError(f"Unknown whitelist target '{target_id}'.")
    return target


# ── WAFv2 IPSet helpers ──────────────────────────────────────────────────

def _find_ip_set_in_region(wafv2, name: str, waf_scope: str) -> Optional[dict]:
    """Look for an IPSet named `name` (under the given WAFv2 API scope —
    "REGIONAL" or "CLOUDFRONT") in whichever region `wafv2` is already
    bound to. Returns None (does NOT raise) if not found in this
    particular region — the caller (_locate_ip_set() below) is
    responsible for trying the other candidate region before giving up
    (REGIONAL scope only — CLOUDFRONT scope has only one valid region,
    see module docstring). WAFv2 IPSets are identified by Name+Id+Scope
    together — Id isn't known/hardcoded anywhere in this codebase, so
    every call does a fresh ListIPSets to resolve Name -> Id, then
    GetIPSet for the full Addresses list + a fresh LockToken (LockToken
    changes on every update and MUST be re-fetched immediately before
    each UpdateIPSet call — never reused across requests)."""
    next_marker = None
    while True:
        kwargs = {"Scope": waf_scope}
        if next_marker:
            kwargs["NextMarker"] = next_marker
        resp = wafv2.list_ip_sets(**kwargs)
        for summary in resp.get("IPSets", []):
            if summary.get("Name") == name:
                detail = wafv2.get_ip_set(Name=name, Scope=waf_scope, Id=summary["Id"])
                ip_set = detail["IPSet"]
                return {
                    "id": ip_set["Id"], "name": ip_set["Name"], "scope": waf_scope,
                    "addresses": list(ip_set.get("Addresses") or []),
                    "ip_address_version": ip_set.get("IPAddressVersion", "IPV4"),
                    "lock_token": detail["LockToken"],
                }
        next_marker = resp.get("NextMarker")
        if not next_marker:
            break
    return None


def _locate_ip_set(name: str, waf_scope: str) -> tuple[str, dict]:
    """Resolve which region this WAFv2 IPSet lives in, and fetch it —
    see module docstring's "Region — AUTO-DETECTED for REGIONAL-scope
    targets, FIXED for CLOUDFRONT-scope targets" section.

    CLOUDFRONT scope: no auto-detection needed or performed — region is
    always CLOUDFRONT_SCOPE_REGION (us-east-1), per AWS's own API
    requirement. Raises immediately (no fallback) if not found there.

    REGIONAL scope: checks _TARGET_REGION_CACHE first; on a cache miss,
    tries each of VALID_REGIONS in order and caches + returns the first
    region where the IPSet is found."""
    if waf_scope == "cloudfront":
        ip_set = _find_ip_set_in_region(_client("wafv2", CLOUDFRONT_SCOPE_REGION), name, "CLOUDFRONT")
        if ip_set:
            return CLOUDFRONT_SCOPE_REGION, ip_set
        raise WhitelistLmsError(f"IPSet '{name}' (CloudFront scope) not found in {CLOUDFRONT_SCOPE_REGION}.")

    cached_region = _TARGET_REGION_CACHE.get(name)
    if cached_region:
        ip_set = _find_ip_set_in_region(_client("wafv2", cached_region), name, "REGIONAL")
        if ip_set:
            return cached_region, ip_set
        # Previously-resolved region no longer has it (renamed/deleted) —
        # fall through and re-probe from scratch rather than trusting a
        # stale cache entry.

    for region in VALID_REGIONS:
        ip_set = _find_ip_set_in_region(_client("wafv2", region), name, "REGIONAL")
        if ip_set:
            _TARGET_REGION_CACHE[name] = region
            return region, ip_set

    raise WhitelistLmsError(f"IPSet '{name}' not found in either region ({', '.join(VALID_REGIONS)}).")


def _ip_in_cidr_list(ip_address: str, cidrs: list[str]) -> bool:
    ip_obj = ipaddress.ip_address(ip_address)
    for cidr in cidrs:
        try:
            if ip_obj in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


# ── Security Group helpers ───────────────────────────────────────────────

def _find_security_group_in_region(ec2, group_id: str) -> Optional[dict]:
    """Look for security group `group_id` in whichever region `ec2` is
    already bound to. Returns None (does NOT raise) if not found in this
    particular region — same "try, don't raise" contract as
    _find_ip_set_in_region() above, for the same reason (auto-detection
    needs to try a second region without treating the first miss as
    fatal)."""
    try:
        resp = ec2.describe_security_groups(GroupIds=[group_id])
    except Exception:
        return None
    groups = resp.get("SecurityGroups", [])
    return groups[0] if groups else None


def _locate_security_group(group_id: str) -> tuple[str, dict]:
    """Auto-detect which region this Security Group actually lives in —
    see module docstring's "Region — AUTO-DETECTED" section. Returns
    (region, security_group_info)."""
    cached_region = _TARGET_REGION_CACHE.get(group_id)
    if cached_region:
        sg = _find_security_group_in_region(_client("ec2", cached_region), group_id)
        if sg:
            return cached_region, sg

    for region in VALID_REGIONS:
        sg = _find_security_group_in_region(_client("ec2", region), group_id)
        if sg:
            _TARGET_REGION_CACHE[group_id] = region
            return region, sg

    raise WhitelistLmsError(f"Security group '{group_id}' not found in either region ({', '.join(VALID_REGIONS)}).")


def _ip_in_sg_ingress(sg: dict, ip_address: str, port: int, protocol: str) -> bool:
    cidr = f"{ip_address}/32"
    for perm in sg.get("IpPermissions", []):
        if perm.get("IpProtocol") not in (protocol, "-1"):
            continue
        from_port, to_port = perm.get("FromPort"), perm.get("ToPort")
        # "-1" IpProtocol (all traffic) has no FromPort/ToPort at all —
        # covers every port including the one we care about.
        port_matches = perm.get("IpProtocol") == "-1" or (
            from_port is not None and to_port is not None and from_port <= port <= to_port
        )
        if not port_matches:
            continue
        for ip_range in perm.get("IpRanges", []):
            if ip_range.get("CidrIp") == cidr:
                return True
    return False


# ── Public API ────────────────────────────────────────────────────────────

def check_ip(target_id: str, ip_address: str) -> dict:
    """Check whether `ip_address` is already whitelisted in the target
    resource, WITHOUT modifying anything. Region is auto-detected (see
    module docstring's "Region — AUTO-DETECTED" section) — the caller
    never supplies one. Returns
    {"target_id", "target_label", "region", "ip_address", "already_whitelisted": bool}.
    Raises WhitelistLmsError for invalid input or an AWS-side failure."""
    target = _get_target(target_id)
    ip_address = _validate_ip(ip_address)

    if target["resource_type"] == "waf_ipset":
        # aws_lookup_name (custom targets only — see add_custom_waf_target())
        # is the real AWS WAFv2 IPSet Name; DEFAULT_TARGETS entries have
        # no separate lookup name because their display label already IS
        # the AWS Name (that's how they were originally registered).
        lookup_name = target.get("aws_lookup_name") or target["label"]
        region, ip_set = _locate_ip_set(lookup_name, target["waf_scope"])
        already = _ip_in_cidr_list(ip_address, ip_set["addresses"])
    else:
        region, sg = _locate_security_group(target["group_id"])
        # Per-target port/protocol override (settable by an admin, see
        # module docstring's "Admin-editable target registry" section)
        # falls back to the module-level default if the target has none.
        check_port = target.get("port") or DEFAULT_SG_PORT
        check_protocol = target.get("protocol") or DEFAULT_SG_PROTOCOL
        already = _ip_in_sg_ingress(sg, ip_address, check_port, check_protocol)

    return {
        "target_id": target_id, "target_label": target["label"], "region": region,
        "ip_address": ip_address, "already_whitelisted": already,
    }


def add_ip(
    target_id: str, ip_address: str, sr_number: str,
    port: Optional[int] = None, protocol: Optional[str] = None,
) -> dict:
    """Add `ip_address` to the target resource. Region is auto-detected
    (see module docstring's "Region — AUTO-DETECTED" section) — the
    caller never supplies one. Requires a non-empty `sr_number` (the the organization
    Service Request number the user must have already submitted before
    whitelisting — enforced here, not just in the frontend, so a direct
    API call can't skip it). Idempotent: if the IP is already present,
    returns immediately with already_whitelisted=True and makes NO AWS
    write call at all — the caller (the route handler) still records
    this as an audit event (see main.py) so an "add" attempt against an
    already-whitelisted IP is still traceable, it just isn't a redundant
    AWS API call.

    Returns {"target_id", "target_label", "region", "ip_address",
    "already_whitelisted": bool, "sr_number"}.
    Raises WhitelistLmsError for invalid input or an AWS-side failure."""
    target = _get_target(target_id)
    ip_address = _validate_ip(ip_address)
    sr_number = (sr_number or "").strip()
    if not sr_number:
        raise WhitelistLmsError("Please enter the Service Request (SR) number before whitelisting this IP.")

    if target["resource_type"] == "waf_ipset":
        lookup_name = target.get("aws_lookup_name") or target["label"]
        region, ip_set = _locate_ip_set(lookup_name, target["waf_scope"])
        if _ip_in_cidr_list(ip_address, ip_set["addresses"]):
            return {
                "target_id": target_id, "target_label": target["label"], "region": region,
                "ip_address": ip_address, "already_whitelisted": True, "sr_number": sr_number,
            }
        new_cidr = f"{ip_address}/32"
        wafv2 = _client("wafv2", region)
        try:
            wafv2.update_ip_set(
                Name=ip_set["name"], Scope=ip_set["scope"], Id=ip_set["id"],
                Addresses=ip_set["addresses"] + [new_cidr],
                LockToken=ip_set["lock_token"],
            )
        except Exception as e:
            raise WhitelistLmsError(f"Could not update IPSet '{ip_set['name']}': {e}") from e
    else:
        region, sg = _locate_security_group(target["group_id"])
        # Precedence: explicit per-request override (port/protocol
        # params, from the frontend's "Add" form) > the target's own
        # stored port/protocol (admin-set) > module-level default.
        use_port = port if port is not None else (target.get("port") or DEFAULT_SG_PORT)
        use_protocol = protocol or target.get("protocol") or DEFAULT_SG_PROTOCOL
        if _ip_in_sg_ingress(sg, ip_address, use_port, use_protocol):
            return {
                "target_id": target_id, "target_label": target["label"], "region": region,
                "ip_address": ip_address, "already_whitelisted": True, "sr_number": sr_number,
            }
        ec2 = _client("ec2", region)
        try:
            ec2.authorize_security_group_ingress(
                GroupId=target["group_id"],
                IpPermissions=[{
                    "IpProtocol": use_protocol, "FromPort": use_port, "ToPort": use_port,
                    "IpRanges": [{"CidrIp": f"{ip_address}/32", "Description": f"Whitelisted via Service Desk portal (SR {sr_number})"}],
                }],
            )
        except Exception as e:
            raise WhitelistLmsError(f"Could not update security group '{target['group_id']}': {e}") from e

    return {
        "target_id": target_id, "target_label": target["label"], "region": region,
        "ip_address": ip_address, "already_whitelisted": False, "sr_number": sr_number,
    }

# ── Admin: target lookup helper (WAF IPSet by Name, no region/scope
# guess needed from the admin) ────────────────────────────────────────────

def resolve_waf_ipset_name(name: str) -> list[dict]:
    """Probe every (scope, region) combination this module can reach for
    a WAFv2 IPSet named `name`, WITHOUT adding anything — used by the
    Admin "Add WAF IPSet target" form so an admin only has to type the
    Name (exactly as it appears in the AWS WAF console) and the portal
    figures out the scope/region, instead of the admin having to already
    know AWS API internals like REGIONAL vs CLOUDFRONT scope.

    Checks, in order: REGIONAL in each of VALID_REGIONS, then CLOUDFRONT
    in CLOUDFRONT_SCOPE_REGION. Returns EVERY match found (usually
    exactly one) rather than just the first — see module docstring's
    "IMPORTANT AMBIGUITY" section for why a name can genuinely resolve to
    more than one distinct IPSet (different scope AND/OR region, with
    different Ids). Each match is
    {"waf_scope": "regional"|"cloudfront", "region": ..., "ip_set_id": ...,
     "description": ..., "address_count": int}.
    Returns an empty list (does NOT raise) if the name isn't found
    anywhere reachable — the caller (the admin route handler) turns that
    into a clean 404-style message, since "not found yet" is an expected
    outcome here (e.g. the admin is registering it slightly before the
    security team finishes creating it), not necessarily user error."""
    name = (name or "").strip()
    if not name:
        raise WhitelistLmsError("Please enter the WAF IPSet name.")

    matches: list[dict] = []
    for region in VALID_REGIONS:
        found = _find_ip_set_in_region(_client("wafv2", region), name, "REGIONAL")
        if found:
            matches.append({
                "waf_scope": "regional", "region": region, "ip_set_id": found["id"],
                "address_count": len(found["addresses"]),
            })
    cf_found = _find_ip_set_in_region(_client("wafv2", CLOUDFRONT_SCOPE_REGION), name, "CLOUDFRONT")
    if cf_found:
        matches.append({
            "waf_scope": "cloudfront", "region": CLOUDFRONT_SCOPE_REGION, "ip_set_id": cf_found["id"],
            "address_count": len(cf_found["addresses"]),
        })
    return matches


def resolve_security_group_id(group_id: str) -> Optional[dict]:
    """Same idea as resolve_waf_ipset_name() but for a Security Group,
    used by the Admin "Add Security Group target" form to confirm the
    group_id the admin typed actually exists before it's saved as a
    target — EC2 security groups have no name-ambiguity problem (a
    GroupId is already globally unique), so this only needs to try the
    two candidate regions and return the first hit (or None). Returns
    {"region": ..., "group_name": ..., "vpc_id": ...} or None."""
    group_id = (group_id or "").strip()
    if not group_id:
        raise WhitelistLmsError("Please enter the Security Group ID.")
    for region in VALID_REGIONS:
        sg = _find_security_group_in_region(_client("ec2", region), group_id)
        if sg:
            return {"region": region, "group_name": sg.get("GroupName"), "vpc_id": sg.get("VpcId")}
    return None


# ── Admin: target registry CRUD (see module docstring's "Admin-editable
# target registry" section) ──────────────────────────────────────────────
# Every function below is a plain read/modify/write against the overlay
# — none of them enforce WHO may call them; that's main.py's job (same
# convention as whitelist_store.py's record()/list_records()), gated by
# require_admin (see main.py).

def add_custom_waf_target(
    *, label: str, ip_set_name: str, waf_scope: str, linked_domain: Optional[str],
    actor_email: str, actor_name: str,
) -> dict:
    """Register a new WAF IPSet target. `ip_set_name` + `waf_scope` must
    match one of the matches resolve_waf_ipset_name() just returned for
    that name (the admin picks which match if resolve_waf_ipset_name()
    found more than one) — this function itself re-resolves rather than
    trusting the caller's waf_scope blindly, so a stale/tampered request
    can't register a target against a scope the IPSet doesn't actually
    exist under."""
    label = (label or "").strip()
    ip_set_name = (ip_set_name or "").strip()
    if not label:
        raise WhitelistLmsError("Please enter a display label for this target.")
    if not ip_set_name:
        raise WhitelistLmsError("Please enter the WAF IPSet name.")
    if waf_scope not in ("regional", "cloudfront"):
        raise WhitelistLmsError("waf_scope must be 'regional' or 'cloudfront'.")

    matches = resolve_waf_ipset_name(ip_set_name)
    if not any(m["waf_scope"] == waf_scope for m in matches):
        raise WhitelistLmsError(
            f"Could not confirm IPSet '{ip_set_name}' under {waf_scope.upper()} scope — "
            "please check the name and try again."
        )

    new_target = {
        "id": f"custom_{uuid.uuid4().hex[:12]}",
        "label": label, "resource_type": "waf_ipset", "waf_scope": waf_scope,
        # This is the value _locate_ip_set()/check_ip()/add_ip() actually
        # look up against AWS by (Name) — kept distinct from `label`
        # (the display label) so an admin can give the target a friendlier
        # display name than the raw AWS resource Name.
        "aws_lookup_name": ip_set_name,
        "linked_domain": (linked_domain or "").strip() or None,
    }
    overlay = _get_overlay()
    overlay["custom_targets"].append(new_target)
    _set_overlay(overlay, actor_email=actor_email, actor_name=actor_name)
    return new_target


def add_custom_sg_target(
    *, label: str, group_id: str, linked_domain: Optional[str], port: Optional[int], protocol: Optional[str],
    actor_email: str, actor_name: str,
) -> dict:
    """Register a new Security Group target. Confirms the group_id
    actually exists (in one of VALID_REGIONS) before saving — same
    "verify before persisting" contract as add_custom_waf_target()."""
    label = (label or "").strip()
    group_id = (group_id or "").strip()
    if not label:
        raise WhitelistLmsError("Please enter a display label for this target.")
    if not group_id:
        raise WhitelistLmsError("Please enter the Security Group ID.")
    if not group_id.startswith("sg-"):
        raise WhitelistLmsError("Security Group ID should look like 'sg-xxxxxxxxxxxxxxxxx'.")

    found = resolve_security_group_id(group_id)
    if not found:
        raise WhitelistLmsError(
            f"Security group '{group_id}' was not found in either region ({', '.join(VALID_REGIONS)}) — "
            "please check the ID and try again."
        )

    new_target = {
        "id": f"custom_{uuid.uuid4().hex[:12]}",
        "label": label, "resource_type": "security_group", "group_id": group_id,
        "linked_domain": (linked_domain or "").strip() or None,
        "port": int(port) if port else None,
        "protocol": (protocol or "").strip() or None,
    }
    overlay = _get_overlay()
    overlay["custom_targets"].append(new_target)
    _set_overlay(overlay, actor_email=actor_email, actor_name=actor_name)
    return new_target


def update_target(
    target_id: str, *, label: Optional[str] = None, linked_domain: Optional[str] = None,
    port: Optional[int] = None, protocol: Optional[str] = None,
    actor_email: str, actor_name: str,
) -> dict:
    """Edit label / linked_domain / (SG only) port+protocol for ANY
    target (built-in default or admin-added custom) — see module
    docstring for why the underlying AWS resource reference itself is
    deliberately NOT editable here. Only fields explicitly passed
    (not None) are changed; omit a field to leave it as-is."""
    targets = _targets_by_id()
    target = targets.get(target_id)
    if not target:
        raise WhitelistLmsError(f"Unknown whitelist target '{target_id}'.")
    if target["resource_type"] != "security_group" and (port is not None or protocol is not None):
        raise WhitelistLmsError("port/protocol only apply to Security Group targets.")

    overlay = _get_overlay()
    patch = {}
    if label is not None:
        stripped = label.strip()
        if not stripped:
            raise WhitelistLmsError("label cannot be empty.")
        patch["label"] = stripped
    if linked_domain is not None:
        patch["linked_domain"] = linked_domain.strip() or None
    if port is not None:
        patch["port"] = int(port)
    if protocol is not None:
        patch["protocol"] = protocol.strip() or None

    if target.get("is_custom"):
        # Custom targets are edited by replacing their own dict in
        # custom_targets — there's no separate "overrides" bucket for
        # them (that bucket only ever patches DEFAULT_TARGETS entries).
        for i, t in enumerate(overlay["custom_targets"]):
            if t["id"] == target_id:
                overlay["custom_targets"][i] = {**t, **patch}
                break
    else:
        existing_override = overlay["overrides"].get(target_id, {})
        overlay["overrides"][target_id] = {**existing_override, **patch}

    _set_overlay(overlay, actor_email=actor_email, actor_name=actor_name)
    return _targets_by_id()[target_id]


def remove_target(target_id: str, *, actor_email: str, actor_name: str) -> None:
    """Remove a target. A built-in DEFAULT target is soft-deleted (added
    to removed_ids — restorable by calling restore_default_target()
    below, since the underlying AWS resource itself is untouched by this
    call either way); a CUSTOM (admin-added) target is deleted outright
    from custom_targets, since it has no "default" state to fall back to.
    Either way, this ONLY affects the portal's own target registry — it
    never calls any AWS API and never deletes/modifies the actual WAF
    IPSet or Security Group in AWS."""
    overlay = _get_overlay()
    default_ids = {t["id"] for t in DEFAULT_TARGETS}
    if target_id in default_ids:
        if target_id not in overlay["removed_ids"]:
            overlay["removed_ids"].append(target_id)
    else:
        overlay["custom_targets"] = [t for t in overlay["custom_targets"] if t["id"] != target_id]
        # Also drop any stale override for a target_id that turns out not
        # to be a default (defensive — shouldn't normally happen).
        overlay["overrides"].pop(target_id, None)
    _set_overlay(overlay, actor_email=actor_email, actor_name=actor_name)


def restore_default_target(target_id: str, *, actor_email: str, actor_name: str) -> dict:
    """Undo a soft-delete of a built-in DEFAULT target (see
    remove_target() above) — removes it from removed_ids so it reappears
    with whatever override (if any) was already stored for it."""
    default_ids = {t["id"] for t in DEFAULT_TARGETS}
    if target_id not in default_ids:
        raise WhitelistLmsError(f"'{target_id}' is not a built-in default target.")
    overlay = _get_overlay()
    overlay["removed_ids"] = [rid for rid in overlay["removed_ids"] if rid != target_id]
    _set_overlay(overlay, actor_email=actor_email, actor_name=actor_name)
    return _targets_by_id()[target_id]
