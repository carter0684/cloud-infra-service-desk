"""
"VPN Profile Lookup" — Helpdesk/Infra Team tool under the Resources
sidebar section. Given a site's WAN IP (the public IP of its Customer
Gateway device), finds any matching AWS Site-to-Site VPN connection(s)
in the network account (100000000003) and returns the full connection
profile: both tunnels' outside/inside IPs, pre-shared keys, IKE (phase 1)
and IPsec (phase 2) parameters, Dead Peer Detection settings, and the
firewall rules a site network admin needs to open — exactly the shape a
network engineer needs to (re)configure a customer-gateway device,
without having to open the AWS console.

CROSS-ACCOUNT ACCESS — DELIBERATE EXCEPTION TO THE REST OF THIS CODEBASE:
every other cross-account module here (cost_explorer.py, cmdb.py,
security_hub.py, waf_monitoring.py, ri_sp_savings.py, billing_by_service.py)
uses Core account's long-lived static IAM user keys (Secrets Manager)
to sts:AssumeRole(PortalReadOnlyRole) into the target account. This module
was explicitly changed (per user request, 2026-08) to NOT use that
pattern at all. Instead it uses the ECS task's OWN identity —
JobPortalTaskRole, whose credentials come automatically from the
container credential provider, no static keys anywhere — to
sts:AssumeRole a dedicated, purpose-built role
(JobPortal-CrossAccount-InfraOps-<first 4 digits of the target account
id>) that must be separately provisioned in each target account, with a
trust policy that trusts only JobPortalTaskRole's ARN and requires the
exact ExternalId below. See _cross_account_session() for the full
implementation. This exception exists ONLY in this module — every other
cross-account tool in this portal is untouched and keeps using
PortalReadOnlyRole via Core account credentials.

EC2's ec2:DescribeCustomerGateways / ec2:DescribeVpnConnections (plain
Describe* calls) are called across every enabled region of the target
account — a WAN IP could be associated with a VPN terminating in ANY
region, and there's no cheaper way to find out which one than to check
the CustomerGateway resource, which is region-scoped.

Unlike cmdb.py / cost_explorer.py, this is a single ON-DEMAND lookup
triggered by a user typing in one IP address (not a periodically-
refreshed dashboard) — so there's no result cache, no DynamoDB history,
no background thread (the assumed cross-account *session* is cached —
see _cross_account_session() — but every search still does a live
fan-out across regions).

SECURITY NOTE: DescribeVpnConnections' TunnelOptions includes each
tunnel's raw IPsec Pre-Shared Key in plaintext (same value the AWS
Console's "Tunnel Details" tab shows an authorized user) — treat this
module's output as sensitive. The frontend masks each PSK behind a
"reveal" click rather than rendering it in the clear by default. Access
to the underlying page itself should be restricted via the Admin page's
Tab Access Control if PSKs shouldn't be visible to every portal user
(see settings_store.TAB_REGISTRY's "vpn_lookup" entry).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3

# ACCOUNTS is a live, read-only view of the cloud account registry (names only here);
# this module keeps its own InfraOps role handling and shared External_Id below.
from .cost_explorer import ACCOUNTS
from .cmdb import _should_use_mock_inventory

logger = logging.getLogger(__name__)

# network (100000000003) per the user's explicit request — the only
# account this tool ever scans. Kept as a module constant (not something
# the frontend picks) since this is a single-purpose lookup tool, unlike
# CMDB/Cost Analysis which are org-wide across every ACCOUNTS entry.
DEFAULT_ACCOUNT_ID = os.environ.get("VPN_LOOKUP_ACCOUNT_ID", "")

# Modest parallelism across regions — DescribeCustomerGateways/
# DescribeVpnConnections are cheap calls, and (unlike the rest of this
# codebase's cross-account pattern) don't cost a fresh sts:AssumeRole per
# region here, since the assumed session is cached per-account (see
# _cross_account_session() below) and reused across every region. Lower
# than cmdb.py's own CMDB_FETCH_CONCURRENCY (which fans out across
# accounts, not just regions of one account) since this is a single
# foreground user-triggered request that should return in a few seconds,
# not a background job.
_FETCH_CONCURRENCY = int(os.environ.get("VPN_LOOKUP_FETCH_CONCURRENCY", "6"))

# Fallback region list used only if ec2:DescribeRegions itself fails for
# the target account (extremely unlikely for a read-only call, but a
# network blip shouldn't take the whole lookup to zero) — see
# cmdb._FALLBACK_REGIONS for the analogous list used by that module's own
# (unrelated) cross-account pattern.
_FALLBACK_REGIONS = ["us-east-1", "ap-southeast-1"]

# ── Cross-account access: JobPortal-CrossAccount-InfraOps-<acct> ────────
# DELIBERATE EXCEPTION to every other cross-account module in this
# codebase — see this module's docstring above for the full rationale.
# The ECS task's OWN identity (JobPortalTaskRole; credentials come
# automatically from the container credential provider — no static keys
# anywhere in this module) is used to sts:AssumeRole into a dedicated
# role provisioned per target account. `ExternalId` must match that
# role's trust policy EXACTLY — env-overridable in case a future rollout
# needs a different value, but the default is what's been agreed with
# the target-account owner.
_ASSUME_ROLE_EXTERNAL_ID = os.environ.get("VPN_LOOKUP_INFRA_OPS_EXTERNAL_ID", "jobportal-infra-ops")
# Fixed (not per-user) session name — shows up in the target account's
# CloudTrail sessionContext for every call made with the resulting
# temporary credentials, identifying this portal/feature as the caller.
_ASSUME_ROLE_SESSION_NAME = "jobportal-infra-ops"
# Must not exceed the target role's own MaxSessionDuration (default 3600,
# max 43200) — see the target role's IAM configuration if this ever needs
# to be raised.
_ASSUME_ROLE_DURATION_SECONDS = int(os.environ.get("VPN_LOOKUP_INFRA_OPS_DURATION_SECONDS", "3600"))

# account_id -> (Session, Expiration). Cached across requests so a
# multi-region scan (or a burst of lookups) doesn't re-hit STS on every
# single call — assumed credentials are refreshed 5 minutes before they
# actually expire. Not keyed by region: STS credentials from AssumeRole
# are account-scoped, not region-scoped (a single Session here can open
# clients in any region via region_name=... at client-creation time), so
# one cache entry per account covers every region that account's VPN
# might be in.
_cross_account_session_cache: dict[str, tuple[boto3.Session, datetime]] = {}
_cross_account_session_cache_lock = threading.Lock()


def _cross_account_role_arn(account_id: str) -> str:
    role_name = f"JobPortal-CrossAccount-InfraOps-{account_id[:4]}"
    return f"arn:aws:iam::{account_id}:role/{role_name}"


def _cross_account_session(account_id: str) -> boto3.Session:
    """Assume JobPortal-CrossAccount-InfraOps-<acct> using the ECS task's
    own identity (JobPortalTaskRole — no static keys read or used
    anywhere in this function; boto3.client("sts") here picks up the
    task's container credentials automatically) and return a Session
    backed by the resulting temporary credentials. Cached per-account
    (see _cross_account_session_cache above); never written to disk or
    logged — kept in memory only, discarded once the process exits or
    the cache entry naturally expires and is replaced.

    Raises VpnLookupError (wrapping the underlying boto3 exception) if
    the role can't be assumed at all — e.g. the target account doesn't
    have this role provisioned yet, or its trust policy doesn't trust
    JobPortalTaskRole/doesn't match the ExternalId. Never leaks the raw
    boto3 exception text beyond that wrapping (it's shown to the user
    as-is by search_vpn_by_wan_ip's caller, so keep it AWS-error-shaped
    but not stack-trace-shaped)."""
    now = datetime.now(timezone.utc)
    with _cross_account_session_cache_lock:
        cached = _cross_account_session_cache.get(account_id)
        if cached and cached[1] > now + timedelta(minutes=5):
            return cached[0]

    role_arn = _cross_account_role_arn(account_id)
    try:
        resp = boto3.client("sts").assume_role(
            RoleArn=role_arn,
            RoleSessionName=_ASSUME_ROLE_SESSION_NAME,
            ExternalId=_ASSUME_ROLE_EXTERNAL_ID,
            DurationSeconds=_ASSUME_ROLE_DURATION_SECONDS,
        )
    except Exception as e:
        raise VpnLookupError(f"Could not assume {role_arn}: {e}") from e

    creds = resp["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    with _cross_account_session_cache_lock:
        _cross_account_session_cache[account_id] = (session, creds["Expiration"])
    return session


def _regions_for_account(account_id: str) -> list[str]:
    try:
        ec2 = _regional_client_for_account(account_id, "ec2", "us-east-1")
        resp = ec2.describe_regions(AllRegions=False)
        regions = [r["RegionName"] for r in resp.get("Regions", [])]
        if regions:
            return regions
    except Exception:
        logger.warning("vpn_lookup: could not list regions for account %s, using fallback list", account_id)
    return list(_FALLBACK_REGIONS)


def _regional_client_for_account(account_id: str, service: str, region: str):
    """boto3 client for `service` in `account_id`, scoped to `region`,
    backed by the cross-account InfraOps session above (cached, so this
    never costs an extra STS round trip beyond the first call for a
    given account within the cache's validity window)."""
    session = _cross_account_session(account_id)
    return session.client(service, region_name=region)


class VpnLookupError(ValueError):
    """Raised for invalid input (bad IP address) or a total inability to
    reach AWS at all (e.g. cannot obtain Core account credentials). Safe to
    show the user as-is. Per-region scan failures are instead collected
    into the result's `errors` list so one bad region doesn't fail the
    whole lookup."""


# ── IKE/IPsec field formatting helpers ──────────────────────────────────

def _values(items: Optional[list]) -> Optional[str]:
    """TunnelOption sub-fields like Phase1EncryptionAlgorithms come back
    as a list of {"Value": "AES256"} dicts — join into one display
    string ("AES256, AES128"), or None if the list is empty/absent."""
    if not items:
        return None
    vals = [str(i.get("Value")) for i in items if i and i.get("Value") is not None]
    return ", ".join(vals) if vals else None


def _ike_version_label(value: str) -> str:
    return {"ikev1": "IKEv1", "ikev2": "IKEv2"}.get((value or "").lower(), value)


def _ike_versions(items: Optional[list]) -> Optional[str]:
    if not items:
        return None
    vals = [_ike_version_label(i.get("Value")) for i in items if i and i.get("Value")]
    return ", ".join(vals) if vals else None


def _tunnel_inside_ips(cidr: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """AWS assigns a /30 CIDR (from 169.254.0.0/16) per tunnel for the
    "inside" tunnel interface addresses. Convention (confirmed by AWS's
    own IPv6 tunnel documentation, which states the analogous rule
    explicitly: "the AWS side of the connection is the first usable
    address, and your side of the connection is the second usable
    address"): AWS's side = first usable host in the /30, the customer
    gateway device's side = second usable host. Returns
    (aws_inside_ip, local_inside_ip), each as "x.x.x.x/30", or (None,
    None) if the CIDR is missing/unparseable."""
    if not cidr:
        return None, None
    try:
        network = ipaddress.ip_network(cidr, strict=False)
        hosts = list(network.hosts())
    except ValueError:
        return None, None
    if len(hosts) < 2:
        return None, None
    prefix = network.prefixlen
    return f"{hosts[0]}/{prefix}", f"{hosts[1]}/{prefix}"


def _firewall_rules_summary(tunnels: list[dict], wan_ip: str) -> list[str]:
    """The exact 3-rule set AWS's own Site-to-Site VPN documentation
    specifies a customer gateway device's firewall must permit
    (bidirectionally) toward each AWS tunnel outside IP: UDP 500 (IKE),
    UDP 4500 (NAT-T), and IP protocol 50 (ESP) — computed from the
    live/mocked tunnel data rather than hardcoded, so it always reflects
    this specific VPN connection's actual AWS-side IPs."""
    aws_ips = [t["aws_outside_ip"] for t in tunnels if t.get("aws_outside_ip")]
    if not aws_ips:
        return []
    ip_list = " and ".join(aws_ips)
    return [
        f"UDP 500 to/from {ip_list} (IKE)",
        f"UDP 4500 to/from {ip_list} (NAT-T)",
        f"IP Protocol 50 to/from {ip_list} (ESP)",
    ]


def _format_tunnel(index: int, tunnel_option: dict, vgw_entry: dict, wan_ip: str) -> dict:
    """One tunnel's full profile. `vgw_entry` (VgwTelemetry[index]) is
    matched to `tunnel_option` (Options.TunnelOptions[index]) by list
    position — AWS returns both lists in the same tunnel-1/tunnel-2
    order for every VPN connection created through the normal console/
    API flow, and there is no other reliable correlation key exposed by
    DescribeVpnConnections (TunnelOptions' own OutsideIpAddress field is
    usually blank unless the customer explicitly set it at creation
    time, so VgwTelemetry's OutsideIpAddress — always populated — is
    preferred and TunnelOptions' copy is only a fallback)."""
    aws_outside_ip = vgw_entry.get("OutsideIpAddress") or tunnel_option.get("OutsideIpAddress")
    aws_inside_ip, local_inside_ip = _tunnel_inside_ips(tunnel_option.get("TunnelInsideCidr"))
    last_status_change = vgw_entry.get("LastStatusChange")
    return {
        "tunnel_number": index + 1,
        "aws_outside_ip": aws_outside_ip,
        "aws_inside_ip": aws_inside_ip,
        "local_outside_ip": wan_ip,
        "local_inside_ip": local_inside_ip,
        "pre_shared_key": tunnel_option.get("PreSharedKey"),
        "status": vgw_entry.get("Status"),
        "status_message": vgw_entry.get("StatusMessage") or None,
        "last_status_change": last_status_change.isoformat() if hasattr(last_status_change, "isoformat") else last_status_change,
        "accepted_route_count": vgw_entry.get("AcceptedRouteCount"),
        "ike": {
            "version": _ike_versions(tunnel_option.get("IkeVersions")),
            "encryption": _values(tunnel_option.get("Phase1EncryptionAlgorithms")),
            "integrity": _values(tunnel_option.get("Phase1IntegrityAlgorithms")),
            "dh_group": _values(tunnel_option.get("Phase1DHGroupNumbers")),
            "lifetime_seconds": tunnel_option.get("Phase1LifetimeSeconds"),
            "authentication": "Pre-Shared Key",
        },
        "ipsec": {
            "encryption": _values(tunnel_option.get("Phase2EncryptionAlgorithms")),
            "integrity": _values(tunnel_option.get("Phase2IntegrityAlgorithms")),
            "dh_group": _values(tunnel_option.get("Phase2DHGroupNumbers")),
            "lifetime_seconds": tunnel_option.get("Phase2LifetimeSeconds"),
            # AWS Site-to-Site VPN is always tunnel mode (never
            # transport mode) — not itself a field DescribeVpnConnections
            # returns, so this is a constant, not live API data.
            "mode": "Tunnel",
        },
        "dpd": {
            "timeout_seconds": tunnel_option.get("DpdTimeoutSeconds"),
            "action": tunnel_option.get("DpdTimeoutAction"),
        },
    }


def _format_vpn_connection(vpn_connection: dict, customer_gateway: dict, region: str, wan_ip: str) -> dict:
    tags = {t.get("Key"): t.get("Value") for t in (vpn_connection.get("Tags") or []) if t.get("Key")}
    name = tags.get("Name") or vpn_connection.get("VpnConnectionId")

    options = vpn_connection.get("Options") or {}
    tunnel_options = options.get("TunnelOptions") or []
    vgw_telemetry = vpn_connection.get("VgwTelemetry") or []

    tunnels = [
        _format_tunnel(i, topt, vgw_telemetry[i] if i < len(vgw_telemetry) else {}, wan_ip)
        for i, topt in enumerate(tunnel_options)
    ]

    return {
        "vpn_connection_id": vpn_connection.get("VpnConnectionId"),
        "name": name,
        "region": region,
        "state": vpn_connection.get("State"),
        "customer_gateway_id": vpn_connection.get("CustomerGatewayId"),
        "customer_gateway_ip": customer_gateway.get("IpAddress") or wan_ip,
        "vpn_gateway_id": vpn_connection.get("VpnGatewayId"),
        "transit_gateway_id": vpn_connection.get("TransitGatewayId"),
        # StaticRoutesOnly defaults True (matches AWS's own default) if
        # absent, since a static-only VPN is the far more common config
        # for a fixed-site customer gateway device (per the user's own
        # example: "Type: Static (no BGP)").
        "routing_type": "Static (no BGP)" if options.get("StaticRoutesOnly", True) else "Dynamic (BGP)",
        "tunnels": tunnels,
        "firewall_rules": _firewall_rules_summary(tunnels, wan_ip),
        "tags": tags,
    }


def _scan_region_for_wan_ip(account_id: str, region: str, wan_ip: str) -> list[dict]:
    """Look for a Customer Gateway resource with this exact public IP in
    one region, then any VPN Connection(s) attached to it. Returns an
    empty list (not an error) when nothing matches this region — that's
    the expected/common case for every region except the one the site's
    VPN actually terminates in."""
    ec2 = _regional_client_for_account(account_id, "ec2", region)

    cgw_resp = ec2.describe_customer_gateways(Filters=[{"Name": "ip-address", "Values": [wan_ip]}])
    customer_gateways = [c for c in cgw_resp.get("CustomerGateways", []) if c.get("State") != "deleted"]
    if not customer_gateways:
        return []

    cgw_by_id = {c["CustomerGatewayId"]: c for c in customer_gateways if c.get("CustomerGatewayId")}
    vpn_resp = ec2.describe_vpn_connections(
        Filters=[{"Name": "customer-gateway-id", "Values": list(cgw_by_id.keys())}]
    )
    connections = []
    for vc in vpn_resp.get("VpnConnections", []):
        if vc.get("State") == "deleted":
            continue
        cgw = cgw_by_id.get(vc.get("CustomerGatewayId"), {})
        connections.append(_format_vpn_connection(vc, cgw, region, wan_ip))
    return connections


def _mock_search(wan_ip: str, account_id: str) -> dict:
    """ENV=local with no Core account credentials configured — return an
    empty-but-valid result for any IP except the exact WAN IP from this
    feature's original example request (203.0.113.18), which returns a
    deterministic mock VPN connection matching that example exactly, so
    local development/demoing this page doesn't require real AWS access
    at all (same convention as cmdb._mock_inventory())."""
    if wan_ip != "203.0.113.18":
        return {
            "wan_ip": wan_ip, "account_id": account_id,
            "account_name": ACCOUNTS.get(account_id, account_id),
            "regions_scanned": ["us-east-1", "ap-southeast-1"],
            "connections": [], "errors": [], "is_mock": True,
        }

    tunnels = [
        {
            "tunnel_number": 1, "aws_outside_ip": "198.51.100.168", "aws_inside_ip": "169.254.240.85/30",
            "local_outside_ip": wan_ip, "local_inside_ip": "169.254.240.86/30",
            "pre_shared_key": "pOlNN9P2PdLMsOY2JqLIWj_wIJAlygo5",
            "status": "UP", "status_message": None, "last_status_change": None, "accepted_route_count": 0,
            "ike": {"version": "IKEv2", "encryption": "AES256", "integrity": "SHA2-256", "dh_group": "14",
                    "lifetime_seconds": 28800, "authentication": "Pre-Shared Key"},
            "ipsec": {"encryption": "AES256", "integrity": "SHA2-256", "dh_group": "14",
                      "lifetime_seconds": 3600, "mode": "Tunnel"},
            "dpd": {"timeout_seconds": 30, "action": "clear"},
        },
        {
            "tunnel_number": 2, "aws_outside_ip": "198.51.100.4", "aws_inside_ip": "169.254.136.45/30",
            "local_outside_ip": wan_ip, "local_inside_ip": "169.254.136.46/30",
            "pre_shared_key": "YkPjJii5AcZ6WaMZP27RkJtc1vk6X83h",
            "status": "UP", "status_message": None, "last_status_change": None, "accepted_route_count": 0,
            "ike": {"version": "IKEv2", "encryption": "AES256", "integrity": "SHA2-256", "dh_group": "14",
                    "lifetime_seconds": 28800, "authentication": "Pre-Shared Key"},
            "ipsec": {"encryption": "AES256", "integrity": "SHA2-256", "dh_group": "14",
                      "lifetime_seconds": 3600, "mode": "Tunnel"},
            "dpd": {"timeout_seconds": 30, "action": "clear"},
        },
    ]
    connection = {
        "vpn_connection_id": "vpn-0ab9ab4ce4f1e66ed",
        "name": "YUL-US Permanent",
        "region": "us-east-1",
        "state": "available",
        "customer_gateway_id": "cgw-mock0ab9ab4ce",
        "customer_gateway_ip": wan_ip,
        "vpn_gateway_id": "vgw-mock0ab9ab4ce",
        "transit_gateway_id": None,
        "routing_type": "Static (no BGP)",
        "tunnels": tunnels,
        "firewall_rules": _firewall_rules_summary(tunnels, wan_ip),
        "tags": {"Name": "YUL-US Permanent"},
    }
    return {
        "wan_ip": wan_ip, "account_id": account_id, "account_name": ACCOUNTS.get(account_id, account_id),
        "regions_scanned": ["us-east-1"], "connections": [connection], "errors": [], "is_mock": True,
    }


def search_vpn_by_wan_ip(wan_ip: str, account_id: str = DEFAULT_ACCOUNT_ID) -> dict:
    """Scan every enabled region of `account_id` for a Customer Gateway
    resource matching `wan_ip`, and return the full VPN connection
    profile(s) attached to it (empty `connections` list if none found —
    not an error; a WAN IP simply not having a VPN configured yet is a
    normal, expected outcome for this tool).

    Returns:
      {
        "wan_ip": str, "account_id": str, "account_name": str,
        "regions_scanned": [str, ...],
        "connections": [ {...one VPN connection profile, see
                           _format_vpn_connection()...}, ... ],
        "errors": [ {"region": str, "error": str}, ... ],  # per-region scan failures, non-fatal
        "is_mock": bool,
      }
    """
    wan_ip = (wan_ip or "").strip()
    if not wan_ip:
        raise VpnLookupError("Please enter a WAN IP address.")
    try:
        ipaddress.ip_address(wan_ip)
    except ValueError as e:
        raise VpnLookupError(f"'{wan_ip}' is not a valid IP address.") from e

    if _should_use_mock_inventory():
        return _mock_search(wan_ip, account_id)

    # Cross-account access for THIS module only: ECS task's own identity
    # (JobPortalTaskRole) assuming JobPortal-CrossAccount-InfraOps-<acct>
    # — no Core account static credentials involved at all, unlike every
    # other cross-account tool in this portal (see module docstring).
    # Establish the session up front (rather than only discovering the
    # failure later inside a per-region scan) so a total inability to
    # assume the InfraOps role — missing role, bad trust policy, wrong
    # ExternalId — surfaces immediately as one clear VpnLookupError
    # instead of a confusing region-by-region errors list.
    try:
        _cross_account_session(account_id)
    except VpnLookupError:
        raise
    except Exception as e:
        raise VpnLookupError(f"Could not obtain AWS credentials for this lookup: {e}") from e

    regions = _regions_for_account(account_id)

    connections: list[dict] = []
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=_FETCH_CONCURRENCY) as pool:
        future_to_region = {
            pool.submit(_scan_region_for_wan_ip, account_id, region, wan_ip): region
            for region in regions
        }
        for future in as_completed(future_to_region):
            region = future_to_region[future]
            try:
                connections.extend(future.result())
            except Exception:
                logger.warning("vpn_lookup: scan failed for account %s region %s", account_id, region, exc_info=True)
                errors.append({"region": region, "error": "Could not scan this region"})

    connections.sort(key=lambda c: (c.get("region") or "", c.get("vpn_connection_id") or ""))

    return {
        "wan_ip": wan_ip,
        "account_id": account_id,
        "account_name": ACCOUNTS.get(account_id, account_id),
        "regions_scanned": sorted(regions),
        "connections": connections,
        "errors": errors,
        "is_mock": False,
    }
