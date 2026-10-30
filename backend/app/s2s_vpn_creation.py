"""
"S2S VPN Profile Creation" — Security section tool that WRITES real AWS
network infrastructure in the network account (100000000003): Customer
Gateways, Site-to-Site VPN connections (attached to one of four fixed
Transit Gateways), and static routes on those Transit Gateways' route
tables. See frontend/src/pages/S2sVpnProfileCreationPage.jsx for the
wizard UI this backs.

THIS MODULE CREATES REAL, BILLABLE, HARD-TO-FULLY-UNDO AWS RESOURCES —
unlike app/vpn_lookup.py (a read-only Describe*-only sibling this module
deliberately reuses the cross-account session plumbing from), every
public function below issues an EC2 Create*/Delete* mutation. Three
guards mirror app/dns_records.py's precedent for this exact reason:

  1. AUTHORIZATION lives in the route handler (main.py's
     require_vpn_writer()), not here — restricted to admins plus the
     Cloud Infra / Security Team User Groups (a narrower set than DNS's
     three groups, since this is infra/security-specific, not something
     Management Team would ever need to do). Reads (listing existing
     Customer Gateways for the "reuse an existing one?" popup) use plain
     get_actor, gated only by the 's2s_vpn_profile_creation' tab flag.
  2. A CHANGE-TICKET REFERENCE (sr_number — same Service Request
     convention as DNS) is mandatory on every write, checked in
     _require_sr_number() below as well as by the route's Pydantic model.
  3. Every write attempt — success AND failure — is recorded to
     s2s_vpn_audit_store.py, mirroring dns_audit_store.py exactly, so a
     rejected/failed change is as visible in the trail as a successful one.

CROSS-ACCOUNT ACCESS: reuses app/vpn_lookup.py's
_regional_client_for_account()/_cross_account_session() helpers as-is —
same target account (network, 100000000003), same role
(JobPortal-CrossAccount-InfraOps-1000), same ECS-task-identity AssumeRole
chain, same cached session. Deliberately NOT re-implemented here: the
STS caching/threading logic in vpn_lookup.py is exactly what this module
also needs, and importing it keeps exactly one cross-account session
cache per account_id shared across both read (lookup) and write
(creation) tools, rather than each maintaining its own separate cache of
the same credentials.

FIXED, NON-EDITABLE INFRASTRUCTURE MAPPINGS: the Transit Gateways and
their route tables, one per supported region, come from the
S2S_VPN_TGW_CONFIG environment variable (see _load_tgw_config) — this tool
intentionally does NOT call ec2:DescribeTransitGateways to discover them,
because the whole point is to constrain every VPN this wizard creates to the
organisation's known-good TGWs rather than let a user attach to an
arbitrary/wrong one found by a scan.
Same reasoning for TUNNEL_OPTIONS_STANDARD: these IKE/IPsec parameters
are the organization's fixed standard for every S2S VPN tunnel this tool creates, not
something the wizard exposes as editable fields.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import time
from typing import Optional

from .vpn_lookup import _regional_client_for_account

logger = logging.getLogger(__name__)

# Same target account as VPN Profile Lookup (app/vpn_lookup.py) — network,
# per the user's explicit request. Kept as its own env-overridable
# constant (not imported from vpn_lookup.DEFAULT_ACCOUNT_ID) so this
# module's account can be repointed independently if VPN creation and VPN
# lookup are ever split across accounts.
DEFAULT_ACCOUNT_ID = os.environ.get("S2S_VPN_ACCOUNT_ID", "")


def _load_tgw_config() -> tuple[dict, dict]:
    """Read the organisation's fixed Transit Gateway layout from the
    S2S_VPN_TGW_CONFIG env var (a JSON object, region -> gateway):

        {"us-east-1": {"name": "TGW-US", "id": "tgw-0123...",
                       "route_table_name": "TGW-US-RT", "route_table_id": "tgw-rtb-0123..."}}

    Not discovered via ec2:DescribeTransitGateways — see module docstring.
    Returns (TRANSIT_GATEWAYS, TGW_ROUTE_TABLES). Empty when not configured,
    in which case the wizard has no allowed regions and stays unusable.
    """
    raw = os.environ.get("S2S_VPN_TGW_CONFIG", "").strip()
    gateways: dict[str, dict[str, str]] = {}
    route_tables: dict[str, dict[str, str]] = {}
    if not raw:
        return gateways, route_tables
    try:
        for region, item in json.loads(raw).items():
            gateways[region] = {"name": item["name"], "id": item["id"]}
            route_tables[region] = {
                "name": item.get("route_table_name", f"{item['name']}-RT"),
                "id": item["route_table_id"],
            }
    except Exception:
        logger.exception("s2s_vpn_creation: S2S_VPN_TGW_CONFIG is not valid; wizard disabled")
        return {}, {}
    return gateways, route_tables


# Fixed per-region Transit Gateway and Transit Gateway ROUTE TABLE mappings
# (the latter is the target of add_static_route() below).
TRANSIT_GATEWAYS, TGW_ROUTE_TABLES = _load_tgw_config()

# The ONLY regions this tool will ever operate in — every function below
# rejects any other region outright, rather than silently working in a region
# the organisation hasn't provisioned a Transit Gateway/route table for.
ALLOWED_REGIONS = list(TRANSIT_GATEWAYS)

# Target gateway types offered by the wizard's Step 2 (mirrors the AWS
# console's own "Target Gateway Type" choices for a VPN connection).
# Only "Transit Gateway" is fully wired to a fixed, known-good resource
# (TRANSIT_GATEWAYS above); the other three are accepted for
# completeness per the user's spec but create a VPN connection with no
# gateway attachment resolved by this tool (Virtual Private Gateway
# requires a caller-supplied VGW id; the latter two create an
# unassociated connection) — see create_vpn_connection()'s docstring.
TARGET_GATEWAY_TYPES = [
    "Virtual private gateway",
    "Transit gateway",
    "Site-to-Site VPN Concentrator",
    "Not associated",
]
DEFAULT_TARGET_GATEWAY_TYPE = "Transit gateway"

# the organization's fixed Tunnel Configuration Standard, applied identically to BOTH
# tunnels of every VPN connection this tool creates (given verbatim by
# the user) — NOT an editable wizard field. Rendered read-only in the
# frontend (see get_config() below, which exposes this same dict for
# display) and translated into boto3's TunnelOptionsSpecification shape
# in _tunnel_options_specifications().
TUNNEL_CONFIG_STANDARD: dict[str, object] = {
    "phase1_encryption": "AES256",
    "phase2_encryption": "AES256",
    "ike_version": "IKEv2",
    "phase1_integrity": "SHA2-256",
    "phase2_integrity": "SHA2-256",
    "phase1_dh_group": 14,
    "phase2_dh_group": 14,
    "dpd_timeout_seconds": 60,
    "dpd_action": "none",
    "phase1_lifetime_seconds": 28800,
    "phase2_lifetime_seconds": 3600,
    "cloudwatch_logging": "Disabled",
}


class S2sVpnCreationError(ValueError):
    """Raised for invalid input, a region outside ALLOWED_REGIONS, or a
    boto3 AWS error while creating/modifying a resource. Safe to show the
    user as-is (never wraps a raw stack trace, only the AWS error
    message or a validation message written by this module)."""


# ── Region / input validation ───────────────────────────────────────────

def _require_allowed_region(region: str) -> str:
    region = (region or "").strip()
    if region not in ALLOWED_REGIONS:
        raise S2sVpnCreationError(
            f"'{region}' is not a supported region for S2S VPN Profile Creation. "
            f"Choose one of: {', '.join(ALLOWED_REGIONS)}."
        )
    return region


def _require_sr_number(sr_number: Optional[str]) -> str:
    """Every VPN infrastructure change must cite an approved approved change
    ticket (Service Request number) — same convention app/dns_records.py
    enforces for DNS writes, for the same reason: the audit trail must
    always tie a real-infra change back to an approved request. Duplicated
    here (also checked by the route's Pydantic model) so no future caller
    of this module can create/modify VPN infrastructure without leaving
    an SR number in the audit trail."""
    value = (sr_number or "").strip()
    if not value:
        raise S2sVpnCreationError("A Service Request (SR) number is required for this change.")
    return value


def _client(region: str, account_id: str):
    return _regional_client_for_account(account_id, "ec2", region)


def _tags_from_name(name_tag: str) -> list[dict]:
    name_tag = (name_tag or "").strip()
    if not name_tag:
        raise S2sVpnCreationError("Name tag is required.")
    return [{"Key": "Name", "Value": name_tag}]


def _name_from_tags(tags: Optional[list[dict]]) -> Optional[str]:
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value")
    return None


# ── Config (fixed mappings + tunnel standard) for the frontend ─────────

def get_config() -> dict:
    """Everything the wizard's frontend needs to render Step 1-3 without
    a separate call per fixed mapping: allowed regions, the Transit
    Gateway per region, the Transit Gateway route table per region, the
    target-gateway-type choices (+ default), and the read-only Tunnel
    Configuration Standard."""
    return {
        "allowed_regions": ALLOWED_REGIONS,
        "transit_gateways": TRANSIT_GATEWAYS,
        "tgw_route_tables": TGW_ROUTE_TABLES,
        "target_gateway_types": TARGET_GATEWAY_TYPES,
        "default_target_gateway_type": DEFAULT_TARGET_GATEWAY_TYPE,
        "tunnel_config_standard": TUNNEL_CONFIG_STANDARD,
        "account_id": DEFAULT_ACCOUNT_ID,
    }


# ── Step 1: Customer Gateway ────────────────────────────────────────────

def list_customer_gateways(region: str, account_id: str = DEFAULT_ACCOUNT_ID) -> list[dict]:
    """Every non-deleted Customer Gateway in `region` — backs the
    wizard's "reuse an existing CGW?" popup so a user renewing a VPN
    profile doesn't have to create a duplicate CGW for a device that
    already has one registered."""
    region = _require_allowed_region(region)
    ec2 = _client(region, account_id)
    try:
        resp = ec2.describe_customer_gateways()
    except Exception as e:
        raise S2sVpnCreationError(f"Could not list Customer Gateways in {region}: {e}") from e

    gateways = []
    for cgw in resp.get("CustomerGateways", []):
        if cgw.get("State") == "deleted":
            continue
        gateways.append({
            "customer_gateway_id": cgw.get("CustomerGatewayId"),
            "name": _name_from_tags(cgw.get("Tags")) or cgw.get("CustomerGatewayId"),
            "ip_address": cgw.get("IpAddress"),
            "bgp_asn": cgw.get("BgpAsn"),
            "state": cgw.get("State"),
            "type": cgw.get("Type"),
        })
    gateways.sort(key=lambda g: (g["name"] or "").lower())
    return gateways


def create_customer_gateway(
    region: str, *, name_tag: str, ip_address: str, bgp_asn: int,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict:
    """Create a new Customer Gateway (ec2:CreateCustomerGateway). Always
    Type="ipsec.1" (the only type AWS Site-to-Site VPN supports)."""
    region = _require_allowed_region(region)
    tags = _tags_from_name(name_tag)

    ip_address = (ip_address or "").strip()
    try:
        ipaddress.ip_address(ip_address)
    except ValueError as e:
        raise S2sVpnCreationError(f"'{ip_address}' is not a valid IP address.") from e

    try:
        bgp_asn_int = int(bgp_asn)
    except (TypeError, ValueError) as e:
        raise S2sVpnCreationError("BGP ASN must be a whole number.") from e
    if not (1 <= bgp_asn_int <= 4294967294):
        raise S2sVpnCreationError("BGP ASN must be between 1 and 4294967294.")

    ec2 = _client(region, account_id)
    try:
        resp = ec2.create_customer_gateway(
            Type="ipsec.1",
            IpAddress=ip_address,
            BgpAsn=bgp_asn_int,
            TagSpecifications=[{"ResourceType": "customer-gateway", "Tags": tags}],
        )
    except Exception as e:
        raise S2sVpnCreationError(f"Could not create the Customer Gateway: {e}") from e

    cgw = resp["CustomerGateway"]
    return {
        "customer_gateway_id": cgw.get("CustomerGatewayId"),
        "name": name_tag.strip(),
        "ip_address": cgw.get("IpAddress"),
        "bgp_asn": cgw.get("BgpAsn"),
        "state": cgw.get("State"),
        "type": cgw.get("Type"),
        "region": region,
    }


# ── Step 2: Site-to-Site VPN connection ─────────────────────────────────

def _tunnel_options_specifications() -> list[dict]:
    """Translate TUNNEL_CONFIG_STANDARD into the two identical
    TunnelOptionsSpecification dicts boto3's create_vpn_connection Options
    parameter expects — one per tunnel, both configured identically per
    the organization's fixed standard (the user's spec makes no distinction between
    "tunnel 1" and "tunnel 2" settings)."""
    spec = {
        "Phase1LifetimeSeconds": TUNNEL_CONFIG_STANDARD["phase1_lifetime_seconds"],
        "Phase2LifetimeSeconds": TUNNEL_CONFIG_STANDARD["phase2_lifetime_seconds"],
        "Phase1EncryptionAlgorithms": [{"Value": TUNNEL_CONFIG_STANDARD["phase1_encryption"]}],
        "Phase2EncryptionAlgorithms": [{"Value": TUNNEL_CONFIG_STANDARD["phase2_encryption"]}],
        "Phase1IntegrityAlgorithms": [{"Value": TUNNEL_CONFIG_STANDARD["phase1_integrity"]}],
        "Phase2IntegrityAlgorithms": [{"Value": TUNNEL_CONFIG_STANDARD["phase2_integrity"]}],
        "Phase1DHGroupNumbers": [{"Value": TUNNEL_CONFIG_STANDARD["phase1_dh_group"]}],
        "Phase2DHGroupNumbers": [{"Value": TUNNEL_CONFIG_STANDARD["phase2_dh_group"]}],
        "IKEVersions": [{"Value": "ikev2"}],
        "DPDTimeoutSeconds": TUNNEL_CONFIG_STANDARD["dpd_timeout_seconds"],
        "DPDTimeoutAction": TUNNEL_CONFIG_STANDARD["dpd_action"],
    }
    # CloudWatch Logging: Disabled -> simply omit LogOptions entirely
    # (AWS's own default for a new tunnel is disabled logging; no
    # EnableTunnelLifecycleControl / LogOptions block is sent).
    return [dict(spec), dict(spec)]


def create_vpn_connection(
    region: str, *, name_tag: str, customer_gateway_id: str,
    target_gateway_type: str = DEFAULT_TARGET_GATEWAY_TYPE,
    transit_gateway_id: Optional[str] = None,
    virtual_private_gateway_id: Optional[str] = None,
    static_routes_only: bool = True,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict:
    """Create a Site-to-Site VPN connection (ec2:CreateVpnConnection),
    attached per `target_gateway_type`:

      - "Transit gateway" (default): attaches to THIS region's fixed
        Transit Gateway (TRANSIT_GATEWAYS[region]) — transit_gateway_id
        is ignored/overridden with that fixed id, so a caller can never
        attach to a TGW outside the organization's known-good four.
      - "Virtual private gateway": attaches to the caller-supplied
        virtual_private_gateway_id (this tool has no fixed VGW mapping;
        the user must already know which VGW to use).
      - "Site-to-Site VPN Concentrator" / "Not associated": creates the
        VPN connection with NEITHER a Transit Gateway NOR a Virtual
        Private Gateway attachment — AWS will accept this as an
        unassociated connection; associating it to a concentrator is not
        something this tool's fixed mappings cover, so it's left for the
        AWS console/a follow-up action.

    Every tunnel gets the organization's fixed Tunnel Configuration Standard (see
    TUNNEL_CONFIG_STANDARD / _tunnel_options_specifications()) — there is
    no per-call override for IKE/IPsec parameters.
    """
    region = _require_allowed_region(region)
    tags = _tags_from_name(name_tag)

    customer_gateway_id = (customer_gateway_id or "").strip()
    if not customer_gateway_id:
        raise S2sVpnCreationError("A Customer Gateway ID is required.")

    if target_gateway_type not in TARGET_GATEWAY_TYPES:
        raise S2sVpnCreationError(
            f"'{target_gateway_type}' is not a valid target gateway type. "
            f"Choose one of: {', '.join(TARGET_GATEWAY_TYPES)}."
        )

    kwargs: dict = {
        "CustomerGatewayId": customer_gateway_id,
        "Type": "ipsec.1",
        "TagSpecifications": [{"ResourceType": "vpn-connection", "Tags": tags}],
        "Options": {
            "StaticRoutesOnly": bool(static_routes_only),
            "TunnelOptions": _tunnel_options_specifications(),
        },
    }

    resolved_transit_gateway_id = None
    resolved_vgw_id = None
    if target_gateway_type == "Transit gateway":
        resolved_transit_gateway_id = TRANSIT_GATEWAYS[region]["id"]
        kwargs["TransitGatewayId"] = resolved_transit_gateway_id
    elif target_gateway_type == "Virtual private gateway":
        resolved_vgw_id = (virtual_private_gateway_id or "").strip()
        if not resolved_vgw_id:
            raise S2sVpnCreationError(
                "A Virtual Private Gateway ID is required when Target gateway type is "
                "'Virtual private gateway'."
            )
        kwargs["VpnGatewayId"] = resolved_vgw_id
    # "Site-to-Site VPN Concentrator" / "Not associated": no gateway id
    # is sent at all — see docstring above.

    ec2 = _client(region, account_id)
    try:
        resp = ec2.create_vpn_connection(**kwargs)
    except Exception as e:
        raise S2sVpnCreationError(f"Could not create the Site-to-Site VPN connection: {e}") from e

    vpn = resp["VpnConnection"]
    options = vpn.get("Options") or {}
    return {
        "vpn_connection_id": vpn.get("VpnConnectionId"),
        "name": name_tag.strip(),
        "region": region,
        "state": vpn.get("State"),
        "customer_gateway_id": vpn.get("CustomerGatewayId"),
        "transit_gateway_id": vpn.get("TransitGatewayId") or resolved_transit_gateway_id,
        "vpn_gateway_id": vpn.get("VpnGatewayId") or resolved_vgw_id,
        "target_gateway_type": target_gateway_type,
        "routing_type": "Static (no BGP)" if options.get("StaticRoutesOnly", True) else "Dynamic (BGP)",
        "tunnel_config_standard": TUNNEL_CONFIG_STANDARD,
    }


# ── Step 3: static route on the Transit Gateway route table ────────────

# A new VPN connection's Transit Gateway attachment shows up a few seconds
# after CreateVpnConnection and stays "pending" for a while before it is
# "available". The wizard moves straight from Step 2 to Step 3, so the lookup
# below waits briefly instead of failing on the first try. Module-level so
# tests can shrink them.
_ATTACHMENT_WAIT_SECONDS = 25
_ATTACHMENT_POLL_SECONDS = 4
_ATTACHMENT_DEAD_STATES = {"deleting", "deleted", "failed", "failing", "rejected", "rejecting", "rollingBack"}


def _find_vpn_tgw_attachment(ec2, vpn_connection_id: str) -> str:
    """Looks up the Transit Gateway attachment that belongs to a VPN connection.

    NOTE: ec2:DescribeVpnConnections does NOT return the attachment id — the
    VpnConnection shape has TransitGatewayId but no TransitGatewayAttachmentId
    (checked against botocore's EC2 model). The attachment has to be found with
    ec2:DescribeTransitGatewayAttachments, filtered on the VPN connection as
    the attached resource."""
    deadline = time.monotonic() + _ATTACHMENT_WAIT_SECONDS
    last_state: Optional[str] = None
    while True:
        try:
            resp = ec2.describe_transit_gateway_attachments(Filters=[
                {"Name": "resource-id", "Values": [vpn_connection_id]},
                {"Name": "resource-type", "Values": ["vpn"]},
            ])
        except Exception as e:
            raise S2sVpnCreationError(
                f"Could not look up the Transit Gateway attachment for {vpn_connection_id}: {e}"
            ) from e
        live = [a for a in resp.get("TransitGatewayAttachments", [])
                if a.get("State") not in _ATTACHMENT_DEAD_STATES]
        if live:
            attachment = live[0]
            last_state = attachment.get("State")
            if last_state == "available":
                return attachment["TransitGatewayAttachmentId"]
        if time.monotonic() >= deadline:
            break
        time.sleep(_ATTACHMENT_POLL_SECONDS)

    if last_state is None:
        raise S2sVpnCreationError(
            f"No Transit Gateway attachment was found for {vpn_connection_id}. If the VPN connection was "
            "created a moment ago, wait about a minute and click Add route again."
        )
    raise S2sVpnCreationError(
        f"The Transit Gateway attachment for {vpn_connection_id} is still '{last_state}'. "
        "Wait about a minute until it is 'available', then click Add route again."
    )


def _resolve_tgw_attachment_id(region: str, account_id: str, attachment_ref: str) -> str:
    """`attachment_ref` from the wizard's Step 3 is either:
      - a Transit Gateway VPN Attachment ID already known (tgw-attach-...)
        -> used as-is;
      - a VPN connection ID (vpn-...) -> resolved to the Transit Gateway
        attachment of that connection (see _find_vpn_tgw_attachment), since
        the CreateTransitGatewayRoute API needs the ATTACHMENT id, not
        the VPN connection id itself.
    Raises S2sVpnCreationError if a vpn-... id is not attached to a Transit
    Gateway (e.g. it was created against a Virtual Private Gateway
    instead), since a static route can't be added for it here."""
    ref = (attachment_ref or "").strip()
    if not ref:
        raise S2sVpnCreationError("A Transit Gateway attachment (VPN connection ID or attachment ID) is required.")
    if ref.startswith("tgw-attach-"):
        return ref
    if ref.startswith("vpn-"):
        ec2 = _client(region, account_id)
        try:
            resp = ec2.describe_vpn_connections(VpnConnectionIds=[ref])
        except Exception as e:
            raise S2sVpnCreationError(f"Could not look up VPN connection {ref}: {e}") from e
        connections = resp.get("VpnConnections", [])
        if not connections:
            raise S2sVpnCreationError(f"VPN connection {ref} was not found in {region}.")
        if not connections[0].get("TransitGatewayId"):
            raise S2sVpnCreationError(
                f"VPN connection {ref} is not attached to a Transit Gateway "
                "(it may be attached to a Virtual Private Gateway instead)."
            )
        return _find_vpn_tgw_attachment(ec2, ref)
    # A bare Transit Gateway ID (tgw-...) is not itself an attachment —
    # tell the caller explicitly rather than passing it through and
    # getting an opaque AWS 400.
    raise S2sVpnCreationError(
        f"'{ref}' is not a VPN connection ID or Transit Gateway attachment ID. "
        "Provide the VPN connection ID (vpn-...) created in Step 2, or a "
        "Transit Gateway attachment ID (tgw-attach-...)."
    )


def add_static_route(
    region: str, *, cidr: str, attachment_ref: str,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict:
    """Add a static route (ec2:CreateTransitGatewayRoute) to THIS region's
    fixed Transit Gateway route table (TGW_ROUTE_TABLES[region]) —
    destined for `cidr`, via the attachment resolved from `attachment_ref`
    (a VPN connection ID or a Transit Gateway attachment ID directly —
    see _resolve_tgw_attachment_id)."""
    region = _require_allowed_region(region)

    cidr = (cidr or "").strip()
    try:
        ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        raise S2sVpnCreationError(f"'{cidr}' is not a valid CIDR block.") from e

    route_table_id = TGW_ROUTE_TABLES[region]["id"]
    attachment_id = _resolve_tgw_attachment_id(region, account_id, attachment_ref)

    ec2 = _client(region, account_id)
    try:
        resp = ec2.create_transit_gateway_route(
            DestinationCidrBlock=cidr,
            TransitGatewayRouteTableId=route_table_id,
            TransitGatewayAttachmentId=attachment_id,
        )
    except Exception as e:
        raise S2sVpnCreationError(f"Could not add this route to {TGW_ROUTE_TABLES[region]['name']}: {e}") from e

    route = resp.get("Route", {})
    return {
        "region": region,
        "route_table_id": route_table_id,
        "route_table_name": TGW_ROUTE_TABLES[region]["name"],
        "destination_cidr_block": route.get("DestinationCidrBlock", cidr),
        "attachment_id": attachment_id,
        "state": route.get("State"),
        "type": route.get("Type"),
    }
