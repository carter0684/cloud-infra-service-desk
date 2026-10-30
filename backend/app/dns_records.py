"""
Route 53 DNS record browse/create/modify/delete for the "DNS Record" tab
(Security section) — see frontend/src/pages/DnsRecordsPage.jsx.

ACCOUNT / CREDENTIALS
--------------------
Every hosted zone the organization owns lives in the Core account
(100000000001, "CORE"), which is the SAME account whose static IAM
keys cost_explorer._core_session() already loads (from the
CORE_ACCESS_KEY_ID/CORE_SECRET_ACCESS_KEY env vars locally, or
Secrets Manager "job-portal/core-admin-credentials" in the
container). So unlike CMDB / Cost Analysis / WAF Monitoring — which all
AssumeRole(PortalReadOnlyRole) into a *different* member account — this
module needs NO AssumeRole at all and no new AWS credential: it just
opens a route53 client on the core session directly, the same short-cut
cost_explorer._client_for_account() takes when account_id ==
CORE_ACCOUNT_ID.

That identity (arn:aws:iam::100000000001:user/portal-core-admin) is in
the Admins group with AdministratorAccess, so it already holds
route53:ListHostedZones, ListResourceRecordSets,
ChangeResourceRecordSets and GetChange — verified with
iam:simulate_principal_policy before this module was written. Nothing
in infra-cdk grants or needs to grant Route 53 access, because these are
static user keys, not the ECS task role.

Route 53 is a global service; the region passed to the client is
irrelevant to which zones are visible, but boto3 still requires one, so
us-east-1 is used (same as cost_explorer).

THIS MODULE CAN MUTATE PRODUCTION DNS
-------------------------------------
Every other AWS-facing module in this app is read-only. This one is not:
change_batch() calls reach real public hosted zones for
your organisation's domains, and a bad change can take a whole domain's mail or web traffic
down. Three guards exist because of that:

  1. AUTHORIZATION lives in the route handler, not here — main.py's
     require_dns_writer() restricts every write to admins plus the
     Cloud Infra / Security / Management Team User Groups. Reads are
     gated only by the ordinary 'dns_records' tab flag. Same split as
     Whitelist (LMS), where visibility of the audit log is likewise
     enforced in main.py and not in the store.
  2. PROTECTED_RECORD_TYPES (NS/SOA) are refused outright, for create,
     modify AND delete. These are the two types whose corruption breaks
     a domain wholesale rather than one hostname, and they essentially
     never need editing through a portal.
  3. A Service Request number is mandatory on every write (checked
     in _require_sr_number below as well as by the route), so the audit
     trail in dns_audit_store.py always ties a DNS change back to an
     approved request.

CREATE vs UPSERT — deliberate asymmetry
---------------------------------------
"Add" issues a Route 53 CREATE, which FAILS if a record set with that
name+type+set-identifier already exists. "Modify" issues UPSERT, which
replaces it. If add used UPSERT (the obvious single-code-path choice) a
user who typed an existing hostname would silently overwrite live
values instead of being told the record already exists — so the two
actions intentionally do not share an action verb.

DELETE reads before it writes
-----------------------------
Route 53 rejects a DELETE whose supplied TTL/values do not match the
record set's current contents exactly. delete_record() therefore always
re-reads the live record set from Route 53 and deletes THAT, ignoring
any values the caller passed. This both avoids spurious
InvalidChangeBatch failures and prevents a stale browser tab from
deleting something other than what the user is looking at.
"""

from __future__ import annotations

import base64
import csv
import logging
import re
import uuid
from io import BytesIO
from typing import Optional

import openpyxl

_logger = logging.getLogger(__name__)

# The one account that hosts every zone. Kept as a module constant
# purely for documentation/error messages — no code branches on it,
# since _r53_client() is hardwired to the core session anyway.
HOSTED_ZONE_ACCOUNT_ID = "100000000001"

# Record types the portal refuses to create, modify or delete — see the
# module docstring. Enforced in _validate_record() for writes and
# surfaced as `editable: false` on reads so the UI can grey them out.
PROTECTED_RECORD_TYPES = frozenset({"NS", "SOA"})

# Types offerable in the "add record" form. Route 53 supports a few more
# exotic ones, but these are the ones the organization's zones actually contain plus
# the common remainder; anything outside this list is rejected rather
# than passed through blindly, so a typo in `type` fails fast with a
# clear message instead of an opaque Route 53 InvalidInput.
SUPPORTED_RECORD_TYPES = (
    "A", "AAAA", "CAA", "CNAME", "DS", "MX", "NAPTR", "PTR", "SPF", "SRV", "TXT",
)

# Types whose values Route 53 requires to be quoted character strings.
# The UI lets people paste a raw SPF/DKIM/verification string, so
# _normalise_values() adds the quotes for them — see its docstring.
_QUOTED_VALUE_TYPES = frozenset({"TXT", "SPF"})

# Route 53's own limit on a single quoted character-string inside a
# TXT/SPF record. Longer values are legal only when split into several
# adjacent quoted strings, which the caller has to do explicitly.
_MAX_CHARACTER_STRING = 255

TTL_MIN = 0
TTL_MAX = 2147483647

# Route 53 returns characters outside the printable-ASCII DNS set as
# backslash-escaped 3-digit OCTAL, e.g. "\\052" for "*" in a wildcard
# record and "\\040" for a space. example-site-a.com and
# onetecosolutions.com both contain such records (the latter has a
# "\\040_0743b0f7...".onetecosolutions.com. leading-space twin of an
# otherwise identical name), so decoding is not hypothetical.
_OCTAL_ESCAPE_RE = re.compile(r"\\(\d{3})")


class DnsRecordError(Exception):
    """An expected, user-correctable failure (bad input, protected type,
    record already exists, ...). main.py turns this into an HTTP 400 with
    the message shown verbatim to the user, so messages here must be
    safe to display and free of AWS internals."""


# ── Client ────────────────────────────────────────────────────────────────

def _r53_client():
    """route53 client on the Core account's own static keys.

    Imported lazily (inside the function, not at module import time) so
    that merely importing this module never touches Secrets Manager —
    main.py imports its route handlers at startup, and the pattern
    elsewhere in this app is that AWS credentials are resolved on first
    real use, not on import.
    """
    from .cost_explorer import _core_session

    return _core_session().client("route53", region_name="us-east-1")


# ── Name / value normalisation ────────────────────────────────────────────

def unescape_name(name: str) -> str:
    """Decode Route 53's backslash-octal escapes for DISPLAY only.

    Writes always keep the raw form Route 53 itself returned, because
    the escaped string is part of what identifies the record set — see
    _same_record().
    """
    return _OCTAL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), name)


def _canonical_name(name: str) -> str:
    """Fully-qualified, lower-cased, trailing-dot form.

    Route 53 stores and returns every name with a trailing dot and
    lower-cased, and treats DNS names case-insensitively, so both the
    zone-suffix check and the record-identity comparison have to
    normalise before comparing or "WWW.Example.com" and
    "www.example.com." would look like different records.
    """
    trimmed = (name or "").strip().rstrip(".")
    return f"{trimmed.lower()}." if trimmed else "."


def _same_record(rs: dict, name: str, rtype: str, set_identifier: Optional[str]) -> bool:
    """Does this raw Route 53 record set refer to the same record?

    A record set's identity is the triple (name, type, set-identifier) —
    the third element is what distinguishes the several weighted /
    latency / failover / geolocation siblings that can share one name
    and type. Comparing only name+type would make a delete ambiguous
    whenever such siblings exist.

    Both sides are unescaped before comparison so a caller can pass the
    human form ("*.example.com") and still match a record Route 53
    reports as "\\052.example.com.".
    """
    return (
        _canonical_name(unescape_name(rs.get("Name", ""))) == _canonical_name(unescape_name(name))
        and rs.get("Type") == rtype
        and (rs.get("SetIdentifier") or None) == (set_identifier or None)
    )


def _normalise_values(rtype: str, values: list[str]) -> list[str]:
    """Clean up user-entered record values.

    TXT and SPF values must reach Route 53 as quoted character strings.
    People paste them unquoted (`v=spf1 include:...`, a DKIM key, a
    domain-verification token), and an unquoted value is rejected by
    Route 53 with an unhelpful error, so quotes are added here when the
    value isn't already quoted. A value that IS already quoted is left
    exactly as typed, which is what makes long multi-string TXT records
    (`"chunk1" "chunk2"`) still possible.
    """
    cleaned: list[str] = []
    for raw in values:
        value = (raw or "").strip()
        if not value:
            continue
        if rtype in _QUOTED_VALUE_TYPES and not value.startswith('"'):
            if len(value) > _MAX_CHARACTER_STRING:
                raise DnsRecordError(
                    f"{rtype} value is {len(value)} characters; Route 53 allows at most "
                    f"{_MAX_CHARACTER_STRING} per quoted string. Split it into several "
                    'quoted strings yourself, e.g. "first part" "second part".'
                )
            value = '"{}"'.format(value.replace('\\', '\\\\').replace('"', '\\"'))
        cleaned.append(value)
    return cleaned


# ── Reads ─────────────────────────────────────────────────────────────────

def list_zones() -> list[dict]:
    """Every hosted zone in the Core account, sorted by domain name.

    Paginated: the organization has 26 zones today, comfortably under one page, but
    ListHostedZones caps MaxItems at 100 and the account will keep
    growing, so the Marker loop is not optional.
    """
    client = _r53_client()
    zones: list[dict] = []
    marker: Optional[str] = None
    while True:
        kwargs: dict = {"MaxItems": "100"}
        if marker:
            kwargs["Marker"] = marker
        resp = client.list_hosted_zones(**kwargs)
        for z in resp.get("HostedZones", []):
            config = z.get("Config", {}) or {}
            zones.append({
                "id": _zone_id(z["Id"]),
                "name": z["Name"],
                "display_name": unescape_name(z["Name"]).rstrip("."),
                "record_count": z.get("ResourceRecordSetCount"),
                "private": bool(config.get("PrivateZone")),
                "comment": config.get("Comment"),
            })
        if resp.get("IsTruncated"):
            marker = resp.get("NextMarker")
        else:
            break
    zones.sort(key=lambda z: z["display_name"])
    return zones


def _zone_id(raw: str) -> str:
    """Strip Route 53's "/hostedzone/" prefix.

    ListHostedZones returns Id as "/hostedzone/Z08611652W2TA05EOPP20"
    but every *request* parameter wants the bare id, and the API happily
    accepts either form — normalising here means the frontend only ever
    sees and sends one shape.
    """
    return (raw or "").rsplit("/", 1)[-1]


def get_zone(zone_id: str) -> dict:
    """One zone's metadata. Raises DnsRecordError if the id is unknown,
    so a stale/hand-edited zone id in a URL becomes a 400 with a clear
    message rather than a boto3 NoSuchHostedZone traceback."""
    client = _r53_client()
    try:
        resp = client.get_hosted_zone(Id=_zone_id(zone_id))
    except client.exceptions.NoSuchHostedZone:
        raise DnsRecordError(f"No such hosted zone: {zone_id}") from None
    z = resp["HostedZone"]
    config = z.get("Config", {}) or {}
    return {
        "id": _zone_id(z["Id"]),
        "name": z["Name"],
        "display_name": unescape_name(z["Name"]).rstrip("."),
        "record_count": z.get("ResourceRecordSetCount"),
        "private": bool(config.get("PrivateZone")),
        "comment": config.get("Comment"),
    }


def _list_raw_records(client, zone_id: str) -> list[dict]:
    """Every ResourceRecordSet in the zone, exactly as Route 53 returns it.

    ListResourceRecordSets is always paginated — it reports
    IsTruncated=True well before a large zone (hundreds of
    records) is exhausted, and continuation needs all three of
    NextRecordName / NextRecordType / NextRecordIdentifier, the last of
    which is present only when the page boundary falls between weighted
    siblings. Dropping StartRecordIdentifier when absent matters: Route
    53 rejects the call if it's sent without a matching name+type.
    """
    records: list[dict] = []
    kwargs: dict = {"HostedZoneId": _zone_id(zone_id), "MaxItems": "300"}
    while True:
        resp = client.list_resource_record_sets(**kwargs)
        records.extend(resp.get("ResourceRecordSets", []))
        if not resp.get("IsTruncated"):
            break
        kwargs["StartRecordName"] = resp["NextRecordName"]
        kwargs["StartRecordType"] = resp["NextRecordType"]
        if resp.get("NextRecordIdentifier"):
            kwargs["StartRecordIdentifier"] = resp["NextRecordIdentifier"]
        else:
            kwargs.pop("StartRecordIdentifier", None)
    return records


def _normalise_record(rs: dict) -> dict:
    """Flatten one raw ResourceRecordSet into the shape the UI renders.

    ALIAS record sets carry an AliasTarget and have NO TTL and NO
    ResourceRecords, while ordinary ones have TTL + ResourceRecords and
    no AliasTarget — the two are mutually exclusive in Route 53. Both
    are surfaced here (is_alias tells them apart) rather than hiding
    aliases, because the aliases in these zones point at the
    CloudFront/ELB endpoints people most often need to look up.
    """
    alias = rs.get("AliasTarget")
    rtype = rs.get("Type", "")
    is_protected = rtype in PROTECTED_RECORD_TYPES
    return {
        "name": rs.get("Name", ""),
        "display_name": unescape_name(rs.get("Name", "")),
        "type": rtype,
        "ttl": rs.get("TTL"),
        "values": [r.get("Value", "") for r in rs.get("ResourceRecords", []) or []],
        "is_alias": alias is not None,
        "alias_target": {
            "dns_name": alias.get("DNSName"),
            "hosted_zone_id": alias.get("HostedZoneId"),
            "evaluate_target_health": bool(alias.get("EvaluateTargetHealth", False)),
        } if alias else None,
        # Routing-policy fields — present only on records that actually
        # use a routing policy. Read-only in this tab: they're echoed so
        # the table can show why two records share a name, and passed
        # back through unchanged on modify (see _build_record_set), but
        # the portal offers no UI to create new routing policies.
        "set_identifier": rs.get("SetIdentifier"),
        "weight": rs.get("Weight"),
        "aws_region": rs.get("Region"),
        "failover": rs.get("Failover"),
        "geo_location": rs.get("GeoLocation"),
        "multi_value_answer": rs.get("MultiValueAnswer"),
        "health_check_id": rs.get("HealthCheckId"),
        "traffic_policy_instance_id": rs.get("TrafficPolicyInstanceId"),
        "editable": not is_protected,
        "protected_reason": (
            f"{rtype} records cannot be changed from the portal — a mistake here takes the "
            "whole domain offline, not one hostname. Change it in the Route 53 console."
            if is_protected else None
        ),
    }


def list_records(zone_id: str) -> dict:
    """Zone metadata + every record in it, normalised for the UI.

    Returns the zone alongside the records (rather than making the
    frontend hold zone state and issue two calls) because the zone's
    name is needed to validate/prefill any subsequent add, and
    record_count from ListHostedZones is a cached approximation while
    len(records) here is exact.
    """
    client = _r53_client()
    zone = get_zone(zone_id)
    raw = _list_raw_records(client, zone["id"])
    records = [_normalise_record(rs) for rs in raw]
    records.sort(key=lambda r: (r["display_name"], r["type"], r["set_identifier"] or ""))
    return {"zone": zone, "records": records, "count": len(records)}


# ── Write validation ──────────────────────────────────────────────────────

def _require_sr_number(sr_number: Optional[str]) -> str:
    """Every DNS change must cite an approved Service Request.

    Also enforced by the route's request model; duplicated here so that
    no future caller of this module can write to a production zone
    without leaving an SR number in the audit trail.
    """
    value = (sr_number or "").strip()
    if not value:
        raise DnsRecordError("A Service Request (SR) number is required for any DNS change.")
    return value


def _reject_protected_type(rtype: str) -> None:
    if rtype in PROTECTED_RECORD_TYPES:
        raise DnsRecordError(
            f"{rtype} records cannot be added, modified or deleted from the portal — "
            "a mistake in an NS or SOA record takes the whole domain offline. "
            "Use the Route 53 console for these."
        )


def _validate_record(
    *,
    zone: dict,
    name: str,
    rtype: str,
    ttl: Optional[int],
    values: Optional[list[str]],
    alias_target: Optional[dict],
) -> dict:
    """Validate an incoming create/modify payload and return the cleaned
    parts (canonical name, normalised values). Raises DnsRecordError with
    a display-safe message on any problem."""
    rtype = (rtype or "").strip().upper()
    if not rtype:
        raise DnsRecordError("Record type is required.")
    _reject_protected_type(rtype)

    canonical = _canonical_name(name)
    if canonical == ".":
        raise DnsRecordError("Record name is required.")

    # A record must live inside the zone it's being written to. Route 53
    # would reject a mismatch anyway, but catching it here produces a
    # message that names both domains instead of "InvalidChangeBatch".
    zone_suffix = _canonical_name(unescape_name(zone["name"]))
    if not (canonical == zone_suffix or canonical.endswith(f".{zone_suffix}")):
        raise DnsRecordError(
            f"'{unescape_name(canonical).rstrip('.')}' is not inside the zone "
            f"'{zone['display_name']}'."
        )

    if alias_target:
        # ALIAS is structurally a different record: the target replaces
        # both TTL and values (Route 53 derives the TTL from whatever the
        # alias points at), so accepting a TTL here would silently
        # discard it.
        if ttl is not None:
            raise DnsRecordError(
                "An ALIAS record has no TTL of its own — Route 53 uses the target's TTL. "
                "Leave the TTL empty for alias records."
            )
        if values:
            raise DnsRecordError("An ALIAS record cannot also have plain values.")
        dns_name = (alias_target.get("dns_name") or "").strip()
        target_zone = (alias_target.get("hosted_zone_id") or "").strip()
        if not dns_name:
            raise DnsRecordError("Alias target DNS name is required.")
        if not target_zone:
            raise DnsRecordError(
                "Alias target hosted zone ID is required — it identifies the AWS endpoint "
                "(ELB / CloudFront / S3 website / this same zone) the alias points at."
            )
        return {
            "name": canonical,
            "type": rtype,
            "ttl": None,
            "values": [],
            "alias_target": {
                "dns_name": dns_name,
                "hosted_zone_id": _zone_id(target_zone),
                "evaluate_target_health": bool(alias_target.get("evaluate_target_health", False)),
            },
        }

    if rtype not in SUPPORTED_RECORD_TYPES:
        raise DnsRecordError(
            f"Unsupported record type '{rtype}'. Supported: {', '.join(SUPPORTED_RECORD_TYPES)}."
        )
    if ttl is None:
        raise DnsRecordError("TTL is required for a non-alias record.")
    try:
        ttl_int = int(ttl)
    except (TypeError, ValueError):
        raise DnsRecordError("TTL must be a whole number of seconds.") from None
    if not (TTL_MIN <= ttl_int <= TTL_MAX):
        raise DnsRecordError(f"TTL must be between {TTL_MIN} and {TTL_MAX} seconds.")

    normalised = _normalise_values(rtype, list(values or []))
    if not normalised:
        raise DnsRecordError("At least one value is required.")

    return {
        "name": canonical,
        "type": rtype,
        "ttl": ttl_int,
        "values": normalised,
        "alias_target": None,
    }


def _build_record_set(clean: dict, *, existing: Optional[dict] = None) -> dict:
    """Assemble the raw ResourceRecordSet Route 53 expects.

    When modifying a record that carries a routing policy, the policy
    fields are copied from `existing`. UPSERT replaces the record set
    wholesale, so omitting them would quietly convert a weighted or
    failover record into a plain one — a change the user never asked for
    and wouldn't see until traffic shifted.
    """
    rs: dict = {"Name": clean["name"], "Type": clean["type"]}

    if clean["alias_target"]:
        rs["AliasTarget"] = {
            "HostedZoneId": clean["alias_target"]["hosted_zone_id"],
            "DNSName": clean["alias_target"]["dns_name"],
            "EvaluateTargetHealth": clean["alias_target"]["evaluate_target_health"],
        }
    else:
        rs["TTL"] = clean["ttl"]
        rs["ResourceRecords"] = [{"Value": v} for v in clean["values"]]

    if existing:
        for src, dest in (
            ("SetIdentifier", "SetIdentifier"),
            ("Weight", "Weight"),
            ("Region", "Region"),
            ("Failover", "Failover"),
            ("GeoLocation", "GeoLocation"),
            ("MultiValueAnswer", "MultiValueAnswer"),
            ("HealthCheckId", "HealthCheckId"),
        ):
            if existing.get(src) is not None:
                rs[dest] = existing[src]
    return rs


# ── Writes ────────────────────────────────────────────────────────────────

def _submit(client, zone_id: str, action: str, record_set: dict, comment: str) -> dict:
    """One-change ChangeResourceRecordSets call.

    Route 53's own InvalidChangeBatch message is the only place that
    explains *why* a change was refused (bad alias target, duplicate
    record, malformed value), so it's re-raised as a DnsRecordError and
    shown to the user verbatim rather than swallowed into a generic
    failure.
    """
    try:
        resp = client.change_resource_record_sets(
            HostedZoneId=_zone_id(zone_id),
            ChangeBatch={
                "Comment": comment[:255],
                "Changes": [{"Action": action, "ResourceRecordSet": record_set}],
            },
        )
    except client.exceptions.InvalidChangeBatch as e:
        raise DnsRecordError(f"Route 53 rejected this change: {e}") from e
    except client.exceptions.NoSuchHostedZone:
        raise DnsRecordError(f"No such hosted zone: {zone_id}") from None
    info = resp.get("ChangeInfo", {}) or {}
    return {
        "change_id": _zone_id(info.get("Id", "")),
        "status": info.get("Status"),
        "submitted_at": info.get("SubmittedAt").isoformat() if info.get("SubmittedAt") else None,
    }


def _change_comment(action: str, sr_number: str, actor_email: str) -> str:
    """Stamp the SR number and the acting user into Route 53's own change
    history, so the provenance of a change is visible in the AWS console
    too and not only in this portal's audit table."""
    return f"job-portal {action} by {actor_email} (SR {sr_number})"


def create_record(
    *,
    zone_id: str,
    name: str,
    record_type: str,
    ttl: Optional[int] = None,
    values: Optional[list[str]] = None,
    alias_target: Optional[dict] = None,
    sr_number: str,
    actor_email: str,
) -> dict:
    """Add a new record set. Fails if one already exists with the same
    name+type (Route 53 CREATE semantics) rather than overwriting it —
    see the module docstring."""
    sr = _require_sr_number(sr_number)
    client = _r53_client()
    zone = get_zone(zone_id)
    clean = _validate_record(
        zone=zone, name=name, rtype=record_type, ttl=ttl,
        values=values, alias_target=alias_target,
    )
    record_set = _build_record_set(clean)
    change = _submit(
        client, zone["id"], "CREATE", record_set,
        _change_comment("create", sr, actor_email),
    )
    return {
        "zone": zone,
        "record": _normalise_record(record_set),
        "change": change,
        "sr_number": sr,
    }


def modify_record(
    *,
    zone_id: str,
    name: str,
    record_type: str,
    ttl: Optional[int] = None,
    values: Optional[list[str]] = None,
    alias_target: Optional[dict] = None,
    set_identifier: Optional[str] = None,
    sr_number: str,
    actor_email: str,
) -> dict:
    """Replace an existing record set's TTL/values (or alias target).

    The record must already exist: UPSERT alone would happily create a
    brand-new record if the name were mistyped, which would look like a
    successful "modify" of something that was never there. Any routing
    policy on the existing record is carried over — see
    _build_record_set().
    """
    sr = _require_sr_number(sr_number)
    client = _r53_client()
    zone = get_zone(zone_id)
    clean = _validate_record(
        zone=zone, name=name, rtype=record_type, ttl=ttl,
        values=values, alias_target=alias_target,
    )

    existing = _find_raw_record(client, zone["id"], clean["name"], clean["type"], set_identifier)
    if existing is None:
        raise DnsRecordError(
            f"No existing {clean['type']} record for "
            f"'{unescape_name(clean['name']).rstrip('.')}' in {zone['display_name']} — "
            "use Add instead."
        )
    _reject_protected_type(existing.get("Type", ""))

    record_set = _build_record_set(clean, existing=existing)
    change = _submit(
        client, zone["id"], "UPSERT", record_set,
        _change_comment("modify", sr, actor_email),
    )
    return {
        "zone": zone,
        "record": _normalise_record(record_set),
        "previous": _normalise_record(existing),
        "change": change,
        "sr_number": sr,
    }


def delete_record(
    *,
    zone_id: str,
    name: str,
    record_type: str,
    set_identifier: Optional[str] = None,
    sr_number: str,
    actor_email: str,
) -> dict:
    """Remove a record set.

    Takes no ttl/values on purpose: Route 53 requires a DELETE to carry
    the record's exact current contents, so they are re-read here and
    the live version is what gets deleted. A caller cannot pass stale
    values and delete something other than what they were shown.
    """
    sr = _require_sr_number(sr_number)
    rtype = (record_type or "").strip().upper()
    _reject_protected_type(rtype)

    client = _r53_client()
    zone = get_zone(zone_id)
    existing = _find_raw_record(client, zone["id"], name, rtype, set_identifier)
    if existing is None:
        raise DnsRecordError(
            f"No {rtype} record for '{unescape_name(_canonical_name(name)).rstrip('.')}' "
            f"in {zone['display_name']}."
        )
    _reject_protected_type(existing.get("Type", ""))

    change = _submit(
        client, zone["id"], "DELETE", existing,
        _change_comment("delete", sr, actor_email),
    )
    return {
        "zone": zone,
        "record": _normalise_record(existing),
        "change": change,
        "sr_number": sr,
    }


def _find_raw_record(
    client, zone_id: str, name: str, rtype: str, set_identifier: Optional[str],
) -> Optional[dict]:
    """The live raw ResourceRecordSet matching name+type+set-identifier.

    Deliberately lists the whole zone and filters in Python instead of
    using StartRecordName/StartRecordType to jump straight to it. Route
    53 orders records by their ESCAPED name, so a targeted seek has to
    reproduce that escaping exactly to land in the right place — and
    these zones really do contain escaped names. The largest zone is 166
    records, one page at MaxItems=300, so the "inefficient" scan costs
    the same single API call the seek would have.
    """
    for rs in _list_raw_records(client, zone_id):
        if _same_record(rs, name, rtype, set_identifier):
            return rs
    return None


# ── Bulk import ───────────────────────────────────────────────────────────
"""
Bulk create/modify from an uploaded text file — one zone per upload, via
DnsRecordsPage.jsx's file picker. Deliberately narrower than the
single-record form in several ways, each a scope decision rather than an
oversight:

  * NO DELETE. The single-record Delete button is the only way to remove
    a record, unchanged. A batch-delete mistake across dozens of rows is
    much harder to notice and undo than a batch-create mistake, so this
    path never issues a Route 53 DELETE.
  * NO ALIAS. ALIAS needs an AWS hosted-zone-id target that is easy to
    get subtly wrong by hand and hard to validate from a text file; it
    already falls out of the code below for free, because "ALIAS" is not
    in SUPPORTED_RECORD_TYPES and _validate_record() rejects it the same
    way it would reject a typo'd type.
  * NO ROUTING-POLICY SIBLINGS. Every row is treated as the one PLAIN
    (no SetIdentifier) record for its name+type. Weighted/failover/
    geolocation siblings sharing a name+type are out of scope for a bulk
    text upload; use the single-record form for those.
  * ONE ChangeResourceRecordSets CALL PER ROW, not a single big
    ChangeBatch. Route 53 allows up to 1000 changes / 32000 chars in one
    batch, but a whole-batch InvalidChangeBatch failure does not reliably
    say WHICH change was the problem — and the design this was built to
    (see DnsRecordsPage.jsx's preview table) requires exact per-row
    pass/fail, not one verdict for the whole file. the organization's bulk files are a
    handful to a few hundred rows, comfortably inside Route 53's ~5
    changes/sec default zone quota even one call at a time.
  * APPLY RE-VALIDATES AGAINST LIVE ROUTE 53 STATE. apply_bulk_import()
    does not trust whatever CREATE/MODIFY/UNCHANGED verdicts the browser
    is holding from an earlier preview_bulk_import() call — it re-parses
    the same file content and re-classifies every row against a fresh
    ListResourceRecordSets right before writing. This avoids acting on a
    stale preview if the zone changed (someone else's edit, or the same
    user re-running an old preview) in between the two calls.

FORMAT AUTO-DETECTION
---------------------
Three formats are accepted. Plain-text CSV/TSV and BIND zone-file input
are told apart by _detect_bulk_format(): a CSV/TSV table with a header
row naming (at least) name/type/value columns, or BIND zone-file syntax
("name [ttl] [class] type rdata" per line, "$ORIGIN"/"$TTL" directives,
";" comments, "(" ")" line continuations for a long record split across
lines). All three formats let one record have several values by writing
several lines/rows that share a name+type — merged by
_merge_bulk_rows() — rather than one row/line listing all values.

The third format, .xlsx (Excel), is NOT told apart by sniffing content
the way CSV vs. zone-file is — .xlsx is a binary ZIP-based format, so
there is no "first line" to inspect before the bytes are even decoded.
Instead the caller (main.py's route, driven by the file the user
actually picked) passes .xlsx content through a SEPARATE field,
file_content_b64 (base64-encoded workbook bytes), rather than the plain-
text `file_content` field — see _parse_bulk_input()'s docstring. Only
the first worksheet of a workbook is read; same header-column names
(name/type/ttl/value) as CSV/TSV, same _merge_bulk_rows() call at the
end — so an .xlsx upload behaves identically to a CSV/TSV one from
_merge_bulk_rows() onward.
"""

_MAX_BULK_ROWS = 500

_CSV_NAME_HEADERS = frozenset({"name", "host", "hostname", "record", "record_name"})
_CSV_TYPE_HEADERS = frozenset({"type", "record_type", "rtype"})
_CSV_TTL_HEADERS = frozenset({"ttl"})
_CSV_VALUE_HEADERS = frozenset({"value", "values", "data", "target", "rdata"})

_ZONE_DIRECTIVE_ORIGIN_RE = re.compile(r"^\$ORIGIN\s+(\S+)", re.IGNORECASE)
_ZONE_DIRECTIVE_TTL_RE = re.compile(r"^\$TTL\s+(\d+)", re.IGNORECASE)
_ZONE_CLASS_TOKENS = frozenset({"IN", "CH", "HS"})


def _detect_bulk_format(text: str) -> str:
    """"csv" if the first non-blank, non-comment line looks like a header
    row naming at least a type column and a value column (comma or
    tab-delimited); "zone" (BIND zone-file style) otherwise. Only the
    first such line is inspected — a zone file's first record line will
    not happen to contain both a recognised type-header word and a
    value-header word as whole comma/tab-separated cells.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        delimiter = "\t" if "\t" in stripped else ","
        cells = [c.strip().lower() for c in stripped.split(delimiter)]
        if any(c in _CSV_TYPE_HEADERS for c in cells) and any(c in _CSV_VALUE_HEADERS for c in cells):
            return "csv"
        return "zone"
    return "zone"


def _strip_zone_comment(line: str) -> str:
    """Remove a ';'-to-end-of-line comment, but only outside double quotes
    — a TXT value like "a;b" must not be truncated at the semicolon."""
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ';' and not in_quotes:
            return line[:i]
    return line


def _join_paren_continuations(text: str) -> list[tuple[bool, str]]:
    """Collapse a BIND-style "( ... )" record that spans several physical
    lines into one logical line, so e.g. a long TXT record split across
    lines for readability parses as a single record. For the ordinary
    case of no parenthesised records, this returns one logical line per
    physical line, so line numbers reported elsewhere still line up with
    the file the user uploaded.

    Returns (leading_whitespace, joined_text) pairs rather than plain
    strings: leading_whitespace records whether the logical line's FIRST
    physical line started with whitespace — the standard BIND shorthand
    for "same name as the previous record" — which the join below would
    otherwise destroy by stripping every piece before joining them.
    """
    lines: list[tuple[bool, str]] = []
    buffer: list[str] = []
    leading_ws = False
    depth = 0
    at_line_start = True
    for raw_line in text.splitlines():
        line = _strip_zone_comment(raw_line)
        if at_line_start:
            leading_ws = bool(line) and line[0] in (" ", "\t")
            at_line_start = False
        depth += line.count("(") - line.count(")")
        buffer.append(line.strip())
        if depth <= 0:
            lines.append((leading_ws, " ".join(p for p in buffer if p)))
            buffer = []
            depth = 0
            at_line_start = True
    if buffer:
        lines.append((leading_ws, " ".join(p for p in buffer if p)))
    return lines


def _parse_zone_file_rows(text: str) -> list[dict]:
    """One dict per RR line: name/type/ttl/value plus line_no + raw text
    for error display. Handles "$ORIGIN"/"$TTL" directives, "@" for the
    origin, blank-name continuation of the previous line's name (standard
    BIND shorthand), and the common "name [ttl] [class] type rdata"
    token order. Lines with the same (name, type) are merged into one
    multi-value record afterwards by _merge_bulk_rows — a zone file
    expresses "two A records for this name" as two lines, not one line
    with two values.
    """
    rows: list[dict] = []
    default_ttl: Optional[str] = None
    origin: Optional[str] = None
    last_name: Optional[str] = None

    for line_no, (has_leading_space, logical) in enumerate(_join_paren_continuations(text), start=1):
        if not logical.strip():
            continue

        m = _ZONE_DIRECTIVE_ORIGIN_RE.match(logical)
        if m:
            origin = m.group(1)
            if not origin.endswith("."):
                origin += "."
            continue
        m = _ZONE_DIRECTIVE_TTL_RE.match(logical)
        if m:
            default_ttl = m.group(1)
            continue
        if logical.lstrip().startswith("$"):
            # $INCLUDE / $GENERATE / anything else — not supported.
            # Skipping rather than mis-parsing it as a record.
            continue

        tokens = logical.split()
        if not tokens:
            continue

        name = None if has_leading_space else tokens[0]
        rest = tokens if has_leading_space else tokens[1:]
        if not rest:
            rows.append({
                "line_no": line_no, "raw": logical, "name": name or last_name or "",
                "type": "", "ttl": None, "value": "",
                "parse_error": "Could not find a record type on this line.",
            })
            continue

        ttl: Optional[str] = None
        idx = 0
        if rest[idx].isdigit():
            ttl = rest[idx]
            idx += 1
        if idx < len(rest) and rest[idx].upper() in _ZONE_CLASS_TOKENS:
            idx += 1
        if idx < len(rest) and ttl is None and rest[idx].isdigit():
            ttl = rest[idx]
            idx += 1

        if idx >= len(rest):
            rows.append({
                "line_no": line_no, "raw": logical, "name": name or last_name or "",
                "type": "", "ttl": ttl, "value": "",
                "parse_error": "Could not find a record type on this line.",
            })
            continue

        rtype = rest[idx].upper()
        value = " ".join(rest[idx + 1:]).strip()

        resolved_name = name if name is not None else last_name
        if resolved_name is None:
            rows.append({
                "line_no": line_no, "raw": logical, "name": "", "type": rtype,
                "ttl": ttl, "value": value,
                "parse_error": "No record name given, and no previous line to continue from.",
            })
            continue
        if resolved_name == "@":
            resolved_name = origin or ""
        elif origin and not resolved_name.endswith("."):
            resolved_name = f"{resolved_name}.{origin}"
        last_name = name if name is not None else last_name

        rows.append({
            "line_no": line_no, "raw": logical, "name": resolved_name, "type": rtype,
            "ttl": ttl if ttl is not None else default_ttl, "value": value,
            "parse_error": None,
        })
    return rows


def _parse_csv_rows(text: str) -> list[dict]:
    """One dict per data row: name/type/ttl/value plus line_no + raw text.
    `value` may itself contain several ';'-separated values — kept as one
    string here so a per-row error can quote the row's original text
    verbatim; splitting happens in _merge_bulk_rows.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    delimiter = "\t" if "\t" in lines[0] else ","
    all_rows = list(csv.reader(lines, delimiter=delimiter))
    if not all_rows:
        return []

    header = [c.strip().lower() for c in all_rows[0]]

    def _find(names: frozenset) -> Optional[int]:
        for i, cell in enumerate(header):
            if cell in names:
                return i
        return None

    name_i, type_i, ttl_i, value_i = (
        _find(_CSV_NAME_HEADERS), _find(_CSV_TYPE_HEADERS),
        _find(_CSV_TTL_HEADERS), _find(_CSV_VALUE_HEADERS),
    )
    if name_i is None or type_i is None or value_i is None:
        raise DnsRecordError(
            "The uploaded file looks like a CSV/TSV but is missing a required column "
            "(need at least 'name', 'type' and 'value'). Found header: " + ", ".join(header)
        )

    def _cell(cells: list[str], i: Optional[int]) -> str:
        return cells[i].strip() if i is not None and i < len(cells) else ""

    rows: list[dict] = []
    for line_no, cells in enumerate(all_rows[1:], start=2):
        if not any(c.strip() for c in cells):
            continue
        raw = lines[line_no - 1] if line_no - 1 < len(lines) else delimiter.join(cells)
        rows.append({
            "line_no": line_no, "raw": raw,
            "name": _cell(cells, name_i), "type": _cell(cells, type_i).upper(),
            "ttl": _cell(cells, ttl_i) or None, "value": _cell(cells, value_i),
            "parse_error": None,
        })
    return rows


def _parse_xlsx_rows(file_bytes: bytes) -> list[dict]:
    """xlsx equivalent of _parse_csv_rows() — same output shape (one dict
    per data row: name/type/ttl/value plus line_no + raw text), same
    header-word matching (_CSV_NAME_HEADERS etc.), so it can feed straight
    into the same _merge_bulk_rows() used for CSV/TSV/zone-file input.
    Only the FIRST worksheet is read — a bulk-import file is expected to
    be a single flat table, not a multi-sheet workbook, and reading every
    sheet would risk silently importing records the user never meant to
    (e.g. a second "notes" or "old" sheet left in the file).

    `line_no` here is the worksheet ROW NUMBER (1-based, matching what
    Excel itself shows in the row headers), not a byte-offset line count
    — this keeps error messages like "row 7: ..." pointing at the exact
    row the user would click on in Excel.

    Raises DnsRecordError if the file cannot be opened as an .xlsx workbook
    at all (corrupt file, or actually some other format with an .xlsx
    extension) — this is a whole-file problem, not a per-row one.
    """
    try:
        workbook = openpyxl.load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as e:
        raise DnsRecordError(f"Could not read this as an Excel (.xlsx) file: {e}") from e

    try:
        sheet = workbook.worksheets[0]
        raw_rows = list(sheet.iter_rows(values_only=True))
    finally:
        workbook.close()

    def _cellstr(v) -> str:
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            # Excel stores every number as a float internally, so a TTL
            # typed as "300" comes back as 300.0 — render whole numbers
            # without the trailing ".0" so downstream int(ttl) parsing
            # (see _validate_record) doesn't need to special-case xlsx.
            return str(int(v))
        return str(v).strip()

    data_rows = [[_cellstr(c) for c in r] for r in raw_rows if any(c is not None and str(c).strip() for c in r)]
    if not data_rows:
        return []

    header = [c.strip().lower() for c in data_rows[0]]

    def _find(names: frozenset) -> Optional[int]:
        for i, cell in enumerate(header):
            if cell in names:
                return i
        return None

    name_i, type_i, ttl_i, value_i = (
        _find(_CSV_NAME_HEADERS), _find(_CSV_TYPE_HEADERS),
        _find(_CSV_TTL_HEADERS), _find(_CSV_VALUE_HEADERS),
    )
    if name_i is None or type_i is None or value_i is None:
        raise DnsRecordError(
            "The uploaded Excel file is missing a required column "
            "(need at least 'name', 'type' and 'value'). Found header: " + ", ".join(header)
        )

    def _cell(cells: list[str], i: Optional[int]) -> str:
        return cells[i].strip() if i is not None and i < len(cells) else ""

    rows: list[dict] = []
    for line_no, cells in enumerate(data_rows[1:], start=2):
        if not any(c.strip() for c in cells):
            continue
        rows.append({
            "line_no": line_no, "raw": ", ".join(cells),
            "name": _cell(cells, name_i), "type": _cell(cells, type_i).upper(),
            "ttl": _cell(cells, ttl_i) or None, "value": _cell(cells, value_i),
            "parse_error": None,
        })
    return rows


def _merge_bulk_rows(rows: list[dict]) -> list[dict]:
    """Group parsed lines/rows by (name, type) into one record group each,
    since both formats can legally express "one record, several values"
    as several lines/rows sharing a name+type. ';'-separated pieces
    inside a single `value` string (the CSV multi-value shorthand) are
    split here too, so either input style ends up in the same shape.
    Rows that failed to parse keep their own line-numbered group so one
    bad line doesn't get silently absorbed into a real record's group.
    """
    groups: "dict[tuple[str, str], dict]" = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        if row.get("parse_error"):
            key = (f"__error_line_{row['line_no']}__", "")
            groups[key] = {
                "line_nos": [row["line_no"]], "raws": [row["raw"]],
                "name": row["name"], "type": row["type"], "ttl": row["ttl"],
                "values": [], "parse_error": row["parse_error"],
            }
            order.append(key)
            continue

        key = (_canonical_name(row["name"]), (row["type"] or "").upper())
        pieces = [v.strip() for v in (row.get("value") or "").split(";") if v.strip()]
        if key not in groups:
            groups[key] = {
                "line_nos": [row["line_no"]], "raws": [row["raw"]],
                "name": row["name"], "type": row["type"], "ttl": row["ttl"],
                "values": list(pieces), "parse_error": None,
            }
            order.append(key)
        else:
            g = groups[key]
            g["line_nos"].append(row["line_no"])
            g["raws"].append(row["raw"])
            g["values"].extend(pieces)
            if g["ttl"] is None:
                g["ttl"] = row["ttl"]
    return [groups[k] for k in order]


def _parse_bulk_input(text: str, *, file_content_b64: Optional[str] = None) -> list[dict]:
    """Auto-detect format, parse, and merge same-name+type lines/rows into
    one record group each. Raises DnsRecordError only for whole-file
    problems (empty file, an unrecognised CSV header, too many rows) — a
    bad individual line becomes one ERROR-tagged row instead in
    _classify_bulk_row, so the rest of the file still previews.

    `text` carries CSV/TSV/BIND-zone-file content as before (the frontend
    reads those with FileReader.readAsText()). `file_content_b64`, if
    given, is an .xlsx workbook's raw bytes, base64-encoded — .xlsx is a
    binary ZIP-based format, so it cannot travel as plain text the way
    the other formats do; the frontend reads an .xlsx file with
    FileReader.readAsArrayBuffer() instead and base64-encodes it before
    sending. Exactly one of the two is expected to carry real content —
    if both are given, file_content_b64 wins, since a caller only sets it
    for an actual xlsx pick.
    """
    if file_content_b64:
        try:
            file_bytes = base64.b64decode(file_content_b64)
        except Exception as e:
            raise DnsRecordError(f"Could not decode the uploaded Excel file: {e}") from e
        if not file_bytes:
            raise DnsRecordError("The uploaded file is empty.")
        raw_rows = _parse_xlsx_rows(file_bytes)
        if not raw_rows:
            raise DnsRecordError("The uploaded file is empty.")
    else:
        if not (text or "").strip():
            raise DnsRecordError("The uploaded file is empty.")
        fmt = _detect_bulk_format(text)
        raw_rows = _parse_csv_rows(text) if fmt == "csv" else _parse_zone_file_rows(text)

    if len(raw_rows) > _MAX_BULK_ROWS:
        raise DnsRecordError(
            f"This file has {len(raw_rows)} records, more than the {_MAX_BULK_ROWS} allowed "
            "in a single bulk upload. Split it into smaller files."
        )
    return _merge_bulk_rows(raw_rows)


def _index_raw_records(raw_records: list[dict]) -> "dict[tuple[str, str], dict]":
    """Lookup by (canonical unescaped name, type). Bulk import only ever
    targets the plain record for a name+type (no SetIdentifier / routing
    policy siblings — see the module-level bulk-import docstring), so
    that pair is a sufficient key for this purpose even though Route 53
    in general also keys a record set on SetIdentifier.
    """
    index: "dict[tuple[str, str], dict]" = {}
    for rs in raw_records:
        key = (_canonical_name(unescape_name(rs.get("Name", ""))), rs.get("Type", ""))
        # If several siblings share a name+type (weighted/failover), keep
        # the one WITHOUT a SetIdentifier — that's the only shape bulk
        # import ever writes, so it's the only one worth diffing against.
        if key not in index or (
            rs.get("SetIdentifier") is None and index[key].get("SetIdentifier") is not None
        ):
            index[key] = rs
    return index


def _classify_bulk_row(zone: dict, group: dict, index: "dict[tuple[str, str], dict]") -> dict:
    """Validate one merged (name, type, ttl, values) group and decide
    whether applying it would CREATE, MODIFY, or be a no-op (UNCHANGED) —
    or is unfit to submit at all (ERROR). Makes no Route 53 calls; `index`
    is the zone's records already fetched once by the caller.
    """
    base = {
        "line_no": group["line_nos"][0] if group.get("line_nos") else None,
        "raw": " | ".join(group["raws"]) if len(group.get("raws") or []) > 1 else (
            (group.get("raws") or [""])[0]
        ),
        "name": group.get("name") or "",
        "type": (group.get("type") or "").upper(),
    }

    if group.get("parse_error"):
        return {
            **base, "action": "ERROR", "error": group["parse_error"],
            "ttl": None, "values": [], "previous_ttl": None, "previous_values": None,
        }

    ttl_raw = group.get("ttl")
    try:
        clean = _validate_record(
            zone=zone, name=group.get("name") or "", rtype=group.get("type") or "",
            ttl=(ttl_raw if ttl_raw not in (None, "") else None),
            values=group.get("values") or [], alias_target=None,
        )
    except DnsRecordError as e:
        return {
            **base, "action": "ERROR", "error": str(e),
            "ttl": None, "values": group.get("values") or [],
            "previous_ttl": None, "previous_values": None,
        }

    display_name = unescape_name(clean["name"]).rstrip(".")
    key = (_canonical_name(clean["name"]), clean["type"])
    existing = index.get(key)

    if existing is None:
        return {
            **base, "name": display_name, "type": clean["type"], "action": "CREATE",
            "ttl": clean["ttl"], "values": clean["values"], "error": None,
            "previous_ttl": None, "previous_values": None,
        }

    prev_ttl = existing.get("TTL")
    prev_values = [r.get("Value", "") for r in existing.get("ResourceRecords", []) or []]
    same_ttl = (prev_ttl is None and clean["ttl"] is None) or (prev_ttl == clean["ttl"])
    same_values = sorted(v.strip() for v in prev_values) == sorted(v.strip() for v in clean["values"])

    return {
        **base, "name": display_name, "type": clean["type"],
        "action": "UNCHANGED" if (same_ttl and same_values) else "MODIFY",
        "ttl": clean["ttl"], "values": clean["values"], "error": None,
        "previous_ttl": prev_ttl, "previous_values": prev_values,
    }


def preview_bulk_import(*, zone_id: str, file_content: str, file_content_b64: Optional[str] = None) -> dict:
    """Parse + classify every record in the uploaded file against the
    zone's LIVE Route 53 state. Makes zero writes — see the module's bulk
    import docstring for the format auto-detection and scope decisions.
    `file_content_b64` carries an .xlsx workbook's bytes, base64-encoded
    — see _parse_bulk_input()'s docstring for why .xlsx needs a separate
    field from the plain-text `file_content`.
    """
    zone = get_zone(zone_id)
    groups = _parse_bulk_input(file_content, file_content_b64=file_content_b64)
    client = _r53_client()
    index = _index_raw_records(_list_raw_records(client, zone["id"]))

    rows = [_classify_bulk_row(zone, g, index) for g in groups]
    counts = {
        "total": len(rows),
        "create": sum(1 for r in rows if r["action"] == "CREATE"),
        "modify": sum(1 for r in rows if r["action"] == "MODIFY"),
        "unchanged": sum(1 for r in rows if r["action"] == "UNCHANGED"),
        "error": sum(1 for r in rows if r["action"] == "ERROR"),
    }
    return {"zone": zone, "rows": rows, "counts": counts}


def apply_bulk_import(
    *, zone_id: str, file_content: str, sr_number: str, actor_email: str,
    file_content_b64: Optional[str] = None,
) -> dict:
    """Re-parses and re-classifies `file_content` (or `file_content_b64`
    for an .xlsx upload — see preview_bulk_import()'s docstring) against
    Route 53's CURRENT state (not whatever the browser cached from an
    earlier preview_bulk_import() call — see the module's bulk import
    docstring) and writes every CREATE/MODIFY row, ONE
    ChangeResourceRecordSets call per row so success/failure is reported
    per record rather than as a single pass/fail for the whole file.
    UNCHANGED and ERROR rows are never sent to Route 53. Never issues a
    DELETE.

    Returns a fresh batch_id the caller (main.py's route) uses to tag
    every dns_audit_store row from this one upload, plus the same
    per-row shape preview_bulk_import returns with an added `applied`
    bool and `result` string ("created" | "modified" | "unchanged" |
    "error").
    """
    sr = _require_sr_number(sr_number)
    preview = preview_bulk_import(zone_id=zone_id, file_content=file_content, file_content_b64=file_content_b64)
    zone = preview["zone"]
    batch_id = uuid.uuid4().hex

    results: list[dict] = []
    for row in preview["rows"]:
        outcome = dict(row)
        outcome["batch_id"] = batch_id
        if row["action"] == "CREATE":
            try:
                created = create_record(
                    zone_id=zone["id"], name=row["name"], record_type=row["type"],
                    ttl=row["ttl"], values=row["values"], alias_target=None,
                    sr_number=sr, actor_email=actor_email,
                )
                outcome["applied"] = True
                outcome["result"] = "created"
                outcome["change_id"] = created["change"]["change_id"]
            except DnsRecordError as e:
                outcome["applied"] = False
                outcome["result"] = "error"
                outcome["error"] = str(e)
        elif row["action"] == "MODIFY":
            try:
                modified = modify_record(
                    zone_id=zone["id"], name=row["name"], record_type=row["type"],
                    ttl=row["ttl"], values=row["values"], alias_target=None,
                    set_identifier=None, sr_number=sr, actor_email=actor_email,
                )
                outcome["applied"] = True
                outcome["result"] = "modified"
                outcome["change_id"] = modified["change"]["change_id"]
            except DnsRecordError as e:
                outcome["applied"] = False
                outcome["result"] = "error"
                outcome["error"] = str(e)
        elif row["action"] == "UNCHANGED":
            outcome["applied"] = False
            outcome["result"] = "unchanged"
        else:  # ERROR — never attempted
            outcome["applied"] = False
            outcome["result"] = "error"
        results.append(outcome)

    counts = {
        "total": len(results),
        "created": sum(1 for r in results if r.get("result") == "created"),
        "modified": sum(1 for r in results if r.get("result") == "modified"),
        "unchanged": sum(1 for r in results if r.get("result") == "unchanged"),
        "error": sum(1 for r in results if r.get("result") == "error"),
    }
    return {"zone": zone, "rows": results, "counts": counts, "batch_id": batch_id, "sr_number": sr}
