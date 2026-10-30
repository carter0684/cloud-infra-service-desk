"""
Tests for app/dns_records.py — the DNS Record tab's Route 53 wrapper.

NO REAL AWS CALLS, AND IN PARTICULAR NO REAL ChangeResourceRecordSets:
dns_records._r53_client() is monkeypatched to a FakeRoute53 built here.
That matters more than usual for this module — the live version of these
functions writes to the public hosted zones for example-site-a.com,
example-site-c.com and 24 others, so a test that accidentally reached
AWS would not fail, it would change production DNS.

The fake reproduces the three Route 53 behaviours this module exists to
handle, since none of them can be exercised against a stub that just
returns a flat list:
  * ListHostedZones / ListResourceRecordSets paginate (Marker and
    NextRecordName/Type/Identifier respectively).
  * ChangeResourceRecordSets raises InvalidChangeBatch, reached through
    client.exceptions.
  * Record names come back backslash-octal-escaped.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import app.dns_records as dns


# ── Fake Route 53 ─────────────────────────────────────────────────────────

class FakeExceptions:
    InvalidChangeBatch = type("InvalidChangeBatch", (Exception,), {})
    NoSuchHostedZone = type("NoSuchHostedZone", (Exception,), {})


class FakeRoute53:
    def __init__(self, zones=None, records=None, page_size=None, change_error=None):
        self._zones = zones or []
        self._records = records or {}
        self._page_size = page_size
        self._change_error = change_error
        self.exceptions = FakeExceptions()
        self.list_calls: list[dict] = []
        self.change_calls: list[dict] = []

    def list_hosted_zones(self, **kwargs):
        size = self._page_size or int(kwargs.get("MaxItems", "100"))
        start = int(kwargs.get("Marker") or 0)
        page = self._zones[start:start + size]
        end = start + size
        if end < len(self._zones):
            return {"HostedZones": page, "IsTruncated": True, "NextMarker": str(end)}
        return {"HostedZones": page, "IsTruncated": False}

    def get_hosted_zone(self, Id):
        bare = Id.rsplit("/", 1)[-1]
        for z in self._zones:
            if z["Id"].rsplit("/", 1)[-1] == bare:
                return {"HostedZone": z}
        raise self.exceptions.NoSuchHostedZone(f"no zone {Id}")

    def list_resource_record_sets(self, **kwargs):
        self.list_calls.append(dict(kwargs))
        all_rs = self._records.get(kwargs["HostedZoneId"], [])
        start = 0
        if kwargs.get("StartRecordName"):
            for i, rs in enumerate(all_rs):
                if (
                    rs["Name"] == kwargs["StartRecordName"]
                    and rs["Type"] == kwargs["StartRecordType"]
                    and (rs.get("SetIdentifier") or None) == (kwargs.get("StartRecordIdentifier") or None)
                ):
                    start = i
                    break
        size = self._page_size or int(kwargs.get("MaxItems", "300"))
        page = all_rs[start:start + size]
        end = start + size
        if end < len(all_rs):
            nxt = all_rs[end]
            resp = {
                "ResourceRecordSets": page, "IsTruncated": True,
                "NextRecordName": nxt["Name"], "NextRecordType": nxt["Type"],
            }
            if nxt.get("SetIdentifier"):
                resp["NextRecordIdentifier"] = nxt["SetIdentifier"]
            return resp
        return {"ResourceRecordSets": page, "IsTruncated": False}

    def change_resource_record_sets(self, **kwargs):
        self.change_calls.append(kwargs)
        if self._change_error:
            raise self._change_error
        return {
            "ChangeInfo": {
                "Id": "/change/C0123456789",
                "Status": "PENDING",
                "SubmittedAt": datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc),
            }
        }


ZONE = {
    "Id": "/hostedzone/Z0861165ABC",
    "Name": "example-site-a.com.",
    "Config": {"PrivateZone": False, "Comment": "main site"},
    "ResourceRecordSetCount": 128,
}

A_RECORD = {
    "Name": "www.example-site-a.com.",
    "Type": "A",
    "TTL": 300,
    "ResourceRecords": [{"Value": "203.0.113.10"}, {"Value": "203.0.113.11"}],
}
NS_RECORD = {
    "Name": "example-site-a.com.",
    "Type": "NS",
    "TTL": 172800,
    "ResourceRecords": [{"Value": "ns-1.awsdns-00.com."}],
}
ALIAS_RECORD = {
    "Name": "cdn.example-site-a.com.",
    "Type": "A",
    "AliasTarget": {
        "HostedZoneId": "Z2FDTNDATAQYW2",
        "DNSName": "d123abc.cloudfront.net.",
        "EvaluateTargetHealth": False,
    },
}
WEIGHTED_RECORD = {
    "Name": "api.example-site-a.com.",
    "Type": "A",
    "TTL": 60,
    "SetIdentifier": "blue",
    "Weight": 80,
    "ResourceRecords": [{"Value": "203.0.113.20"}],
}
ESCAPED_RECORD = {
    # Real shape seen in onetecosolutions.com: "\040" is a literal space.
    "Name": "\\040_verify.example-site-a.com.",
    "Type": "TXT",
    "TTL": 300,
    "ResourceRecords": [{"Value": '"token-abc"'}],
}

ALL_RECORDS = [A_RECORD, NS_RECORD, ALIAS_RECORD, WEIGHTED_RECORD, ESCAPED_RECORD]


@pytest.fixture
def fake(monkeypatch):
    """Default fake: one zone, the five records above, no pagination."""
    client = FakeRoute53(zones=[ZONE], records={"Z0861165ABC": list(ALL_RECORDS)})
    monkeypatch.setattr(dns, "_r53_client", lambda: client)
    return client


def _patch(monkeypatch, client):
    monkeypatch.setattr(dns, "_r53_client", lambda: client)
    return client


# ── Name handling ─────────────────────────────────────────────────────────

def test_unescape_name_decodes_octal_escapes():
    assert dns.unescape_name("\\040_verify.example.com.") == " _verify.example.com."
    assert dns.unescape_name("\\052.example.com.") == "*.example.com."
    assert dns.unescape_name("www.example.com.") == "www.example.com."


def test_canonical_name_lowercases_and_adds_trailing_dot():
    assert dns._canonical_name("WWW.Example.COM") == "www.example.com."
    assert dns._canonical_name("www.example.com.") == "www.example.com."


def test_same_record_matches_across_escaping_and_case():
    assert dns._same_record(ESCAPED_RECORD, " _verify.example-site-a.com", "TXT", None)


def test_same_record_distinguishes_weighted_siblings():
    assert dns._same_record(WEIGHTED_RECORD, "api.example-site-a.com", "A", "blue")
    assert not dns._same_record(WEIGHTED_RECORD, "api.example-site-a.com", "A", "green")
    # No set-identifier supplied must NOT match a record that has one —
    # otherwise a delete would remove an arbitrary sibling.
    assert not dns._same_record(WEIGHTED_RECORD, "api.example-site-a.com", "A", None)


# ── Reads ─────────────────────────────────────────────────────────────────

def test_list_zones_strips_prefix_and_sorts(monkeypatch):
    zones = [
        {"Id": "/hostedzone/ZB", "Name": "zzz.com.", "Config": {}, "ResourceRecordSetCount": 2},
        {"Id": "/hostedzone/ZA", "Name": "aaa.com.", "Config": {"PrivateZone": True}, "ResourceRecordSetCount": 5},
    ]
    _patch(monkeypatch, FakeRoute53(zones=zones))
    result = dns.list_zones()
    assert [z["display_name"] for z in result] == ["aaa.com", "zzz.com"]
    assert result[0]["id"] == "ZA"
    assert result[0]["private"] is True
    assert result[1]["private"] is False


def test_list_zones_follows_marker_pagination(monkeypatch):
    zones = [
        {"Id": f"/hostedzone/Z{i}", "Name": f"d{i:02d}.com.", "Config": {}, "ResourceRecordSetCount": 1}
        for i in range(7)
    ]
    _patch(monkeypatch, FakeRoute53(zones=zones, page_size=3))
    assert len(dns.list_zones()) == 7


def test_list_records_normalises_every_record_shape(fake):
    result = dns.list_records("Z0861165ABC")
    assert result["count"] == 5
    assert result["zone"]["display_name"] == "example-site-a.com"
    by_key = {(r["display_name"], r["type"], r["set_identifier"] or ""): r for r in result["records"]}

    a = by_key[("www.example-site-a.com.", "A", "")]
    assert a["ttl"] == 300
    assert a["values"] == ["203.0.113.10", "203.0.113.11"]
    assert a["is_alias"] is False
    assert a["editable"] is True

    alias = by_key[("cdn.example-site-a.com.", "A", "")]
    assert alias["is_alias"] is True
    assert alias["ttl"] is None                      # ALIAS records have no TTL of their own
    assert alias["values"] == []
    assert alias["alias_target"]["dns_name"] == "d123abc.cloudfront.net."

    weighted = by_key[("api.example-site-a.com.", "A", "blue")]
    assert weighted["weight"] == 80

    escaped = by_key[(" _verify.example-site-a.com.", "TXT", "")]
    assert escaped["name"] == "\\040_verify.example-site-a.com."   # raw form preserved for writes


def test_list_records_marks_ns_and_soa_not_editable(fake):
    ns = next(r for r in dns.list_records("Z0861165ABC")["records"] if r["type"] == "NS")
    assert ns["editable"] is False
    assert "NS" in ns["protected_reason"]


def test_list_records_follows_record_pagination(monkeypatch):
    many = [
        {"Name": f"h{i:03d}.example-site-a.com.", "Type": "A", "TTL": 300,
         "ResourceRecords": [{"Value": f"203.0.113.{i}"}]}
        for i in range(10)
    ]
    client = _patch(monkeypatch, FakeRoute53(zones=[ZONE], records={"Z0861165ABC": many}, page_size=3))
    result = dns.list_records("Z0861165ABC")
    assert result["count"] == 10
    assert len(client.list_calls) > 1               # proves the loop actually paginated


def test_list_records_drops_start_identifier_when_not_returned(monkeypatch):
    """Route 53 rejects StartRecordIdentifier sent without a matching
    name+type, so it must be removed once a page boundary no longer falls
    between weighted siblings."""
    records = [
        WEIGHTED_RECORD,
        {**WEIGHTED_RECORD, "SetIdentifier": "green", "Weight": 20},
        A_RECORD,
        ALIAS_RECORD,
    ]
    client = _patch(monkeypatch, FakeRoute53(zones=[ZONE], records={"Z0861165ABC": records}, page_size=1))
    dns.list_records("Z0861165ABC")
    assert any("StartRecordIdentifier" not in c for c in client.list_calls[1:])


def test_get_zone_unknown_id_raises_dns_record_error(fake):
    with pytest.raises(dns.DnsRecordError, match="No such hosted zone"):
        dns.get_zone("ZDOESNOTEXIST")


# ── Guard rails ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("rtype", ["NS", "SOA"])
def test_create_refuses_protected_types(fake, rtype):
    with pytest.raises(dns.DnsRecordError, match="cannot be added, modified or deleted"):
        dns.create_record(
            zone_id="Z0861165ABC", name="example-site-a.com", record_type=rtype,
            ttl=300, values=["ns-9.awsdns-00.com."],
            sr_number="SR-1", actor_email="a@b.com",
        )
    assert fake.change_calls == []


@pytest.mark.parametrize("rtype", ["NS", "SOA"])
def test_delete_refuses_protected_types(fake, rtype):
    with pytest.raises(dns.DnsRecordError, match="cannot be added, modified or deleted"):
        dns.delete_record(
            zone_id="Z0861165ABC", name="example-site-a.com", record_type=rtype,
            sr_number="SR-1", actor_email="a@b.com",
        )
    assert fake.change_calls == []


@pytest.mark.parametrize("sr", ["", "   ", None])
def test_writes_require_an_sr_number(fake, sr):
    with pytest.raises(dns.DnsRecordError, match="Service Request"):
        dns.create_record(
            zone_id="Z0861165ABC", name="new.example-site-a.com", record_type="A",
            ttl=300, values=["203.0.113.99"], sr_number=sr, actor_email="a@b.com",
        )
    assert fake.change_calls == []


def test_create_rejects_name_outside_the_zone(fake):
    with pytest.raises(dns.DnsRecordError, match="not inside the zone"):
        dns.create_record(
            zone_id="Z0861165ABC", name="www.someoneelse.com", record_type="A",
            ttl=300, values=["203.0.113.99"], sr_number="SR-1", actor_email="a@b.com",
        )


def test_create_rejects_unsupported_type(fake):
    with pytest.raises(dns.DnsRecordError, match="Unsupported record type"):
        dns.create_record(
            zone_id="Z0861165ABC", name="x.example-site-a.com", record_type="WKS",
            ttl=300, values=["whatever"], sr_number="SR-1", actor_email="a@b.com",
        )


def test_create_requires_ttl_for_standard_record(fake):
    with pytest.raises(dns.DnsRecordError, match="TTL is required"):
        dns.create_record(
            zone_id="Z0861165ABC", name="x.example-site-a.com", record_type="A",
            values=["203.0.113.99"], sr_number="SR-1", actor_email="a@b.com",
        )


def test_create_requires_at_least_one_value(fake):
    with pytest.raises(dns.DnsRecordError, match="At least one value"):
        dns.create_record(
            zone_id="Z0861165ABC", name="x.example-site-a.com", record_type="A",
            ttl=300, values=["", "  "], sr_number="SR-1", actor_email="a@b.com",
        )


def test_create_rejects_ttl_on_alias_record(fake):
    with pytest.raises(dns.DnsRecordError, match="ALIAS record has no TTL"):
        dns.create_record(
            zone_id="Z0861165ABC", name="new.example-site-a.com", record_type="A",
            ttl=300,
            alias_target={"dns_name": "d1.cloudfront.net", "hosted_zone_id": "Z2FDTNDATAQYW2"},
            sr_number="SR-1", actor_email="a@b.com",
        )


def test_create_alias_requires_target_hosted_zone_id(fake):
    with pytest.raises(dns.DnsRecordError, match="hosted zone ID is required"):
        dns.create_record(
            zone_id="Z0861165ABC", name="new.example-site-a.com", record_type="A",
            alias_target={"dns_name": "d1.cloudfront.net", "hosted_zone_id": ""},
            sr_number="SR-1", actor_email="a@b.com",
        )


def test_create_rejects_out_of_range_ttl(fake):
    with pytest.raises(dns.DnsRecordError, match="TTL must be between"):
        dns.create_record(
            zone_id="Z0861165ABC", name="x.example-site-a.com", record_type="A",
            ttl=-1, values=["203.0.113.99"], sr_number="SR-1", actor_email="a@b.com",
        )


# ── TXT / SPF quoting ─────────────────────────────────────────────────────

def test_txt_value_is_quoted_automatically(fake):
    dns.create_record(
        zone_id="Z0861165ABC", name="_dmarc.example-site-a.com", record_type="TXT",
        ttl=300, values=["v=DMARC1; p=none"], sr_number="SR-1", actor_email="a@b.com",
    )
    sent = fake.change_calls[0]["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
    assert sent["ResourceRecords"] == [{"Value": '"v=DMARC1; p=none"'}]


def test_already_quoted_txt_value_is_left_alone(fake):
    """A pre-quoted value may be a deliberate multi-string TXT record —
    re-quoting it would corrupt it."""
    dns.create_record(
        zone_id="Z0861165ABC", name="_dkim.example-site-a.com", record_type="TXT",
        ttl=300, values=['"part-one" "part-two"'], sr_number="SR-1", actor_email="a@b.com",
    )
    sent = fake.change_calls[0]["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
    assert sent["ResourceRecords"] == [{"Value": '"part-one" "part-two"'}]


def test_overlong_unquoted_txt_value_is_rejected_with_guidance(fake):
    with pytest.raises(dns.DnsRecordError, match="at most 255"):
        dns.create_record(
            zone_id="Z0861165ABC", name="_big.example-site-a.com", record_type="TXT",
            ttl=300, values=["x" * 256], sr_number="SR-1", actor_email="a@b.com",
        )


# ── Create ────────────────────────────────────────────────────────────────

def test_create_uses_route53_create_action_not_upsert(fake):
    """CREATE fails on an existing record; UPSERT would silently overwrite
    live values when a user retyped an existing hostname."""
    result = dns.create_record(
        zone_id="Z0861165ABC", name="new.example-site-a.com", record_type="A",
        ttl=120, values=["203.0.113.99"], sr_number="SR-42", actor_email="a@b.com",
    )
    change = fake.change_calls[0]["ChangeBatch"]["Changes"][0]
    assert change["Action"] == "CREATE"
    assert change["ResourceRecordSet"]["Name"] == "new.example-site-a.com."
    assert change["ResourceRecordSet"]["TTL"] == 120
    assert result["change"]["change_id"] == "C0123456789"
    assert result["change"]["status"] == "PENDING"
    assert result["sr_number"] == "SR-42"


def test_create_stamps_sr_number_and_actor_into_route53_comment(fake):
    dns.create_record(
        zone_id="Z0861165ABC", name="new.example-site-a.com", record_type="A",
        ttl=120, values=["203.0.113.99"], sr_number="SR-42", actor_email="admin@example.com",
    )
    comment = fake.change_calls[0]["ChangeBatch"]["Comment"]
    assert "SR-42" in comment and "admin@example.com" in comment


def test_create_alias_sends_alias_target_and_no_ttl(fake):
    dns.create_record(
        zone_id="Z0861165ABC", name="new.example-site-a.com", record_type="A",
        alias_target={
            "dns_name": "d999.cloudfront.net",
            "hosted_zone_id": "Z2FDTNDATAQYW2",
            "evaluate_target_health": True,
        },
        sr_number="SR-7", actor_email="a@b.com",
    )
    rs = fake.change_calls[0]["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
    assert rs["AliasTarget"] == {
        "HostedZoneId": "Z2FDTNDATAQYW2",
        "DNSName": "d999.cloudfront.net",
        "EvaluateTargetHealth": True,
    }
    assert "TTL" not in rs and "ResourceRecords" not in rs


def test_invalid_change_batch_is_surfaced_to_the_user(monkeypatch):
    err = FakeExceptions.InvalidChangeBatch("Tried to create resource record set but it already exists")
    _patch(monkeypatch, FakeRoute53(zones=[ZONE], records={"Z0861165ABC": list(ALL_RECORDS)}, change_error=err))
    with pytest.raises(dns.DnsRecordError, match="already exists"):
        dns.create_record(
            zone_id="Z0861165ABC", name="www.example-site-a.com", record_type="A",
            ttl=300, values=["203.0.113.10"], sr_number="SR-1", actor_email="a@b.com",
        )


# ── Modify ────────────────────────────────────────────────────────────────

def test_modify_uses_upsert_and_reports_previous_value(fake):
    result = dns.modify_record(
        zone_id="Z0861165ABC", name="www.example-site-a.com", record_type="A",
        ttl=60, values=["203.0.113.77"], sr_number="SR-9", actor_email="a@b.com",
    )
    change = fake.change_calls[0]["ChangeBatch"]["Changes"][0]
    assert change["Action"] == "UPSERT"
    assert change["ResourceRecordSet"]["ResourceRecords"] == [{"Value": "203.0.113.77"}]
    assert result["previous"]["values"] == ["203.0.113.10", "203.0.113.11"]


def test_modify_refuses_when_the_record_does_not_exist(fake):
    """UPSERT alone would create it and report success, making a typo look
    like a successful edit of something that never existed."""
    with pytest.raises(dns.DnsRecordError, match="use Add instead"):
        dns.modify_record(
            zone_id="Z0861165ABC", name="nope.example-site-a.com", record_type="A",
            ttl=60, values=["203.0.113.77"], sr_number="SR-9", actor_email="a@b.com",
        )
    assert fake.change_calls == []


def test_modify_preserves_routing_policy_fields(fake):
    """UPSERT replaces the whole record set, so dropping Weight/
    SetIdentifier would quietly demote a weighted record to a plain one."""
    dns.modify_record(
        zone_id="Z0861165ABC", name="api.example-site-a.com", record_type="A",
        ttl=30, values=["203.0.113.21"], set_identifier="blue",
        sr_number="SR-9", actor_email="a@b.com",
    )
    rs = fake.change_calls[0]["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
    assert rs["SetIdentifier"] == "blue"
    assert rs["Weight"] == 80


def test_modify_can_convert_standard_record_to_alias(fake):
    dns.modify_record(
        zone_id="Z0861165ABC", name="www.example-site-a.com", record_type="A",
        alias_target={"dns_name": "d55.cloudfront.net", "hosted_zone_id": "Z2FDTNDATAQYW2"},
        sr_number="SR-9", actor_email="a@b.com",
    )
    rs = fake.change_calls[0]["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
    assert rs["AliasTarget"]["DNSName"] == "d55.cloudfront.net"
    assert "TTL" not in rs


# ── Delete ────────────────────────────────────────────────────────────────

def test_delete_sends_the_live_record_contents(fake):
    """Route 53 rejects a DELETE whose values don't match current state,
    and a stale browser tab must not be able to delete a version other
    than the one it displayed — so delete re-reads before writing."""
    result = dns.delete_record(
        zone_id="Z0861165ABC", name="www.example-site-a.com", record_type="A",
        sr_number="SR-11", actor_email="a@b.com",
    )
    change = fake.change_calls[0]["ChangeBatch"]["Changes"][0]
    assert change["Action"] == "DELETE"
    assert change["ResourceRecordSet"] == A_RECORD
    assert result["record"]["values"] == ["203.0.113.10", "203.0.113.11"]


def test_delete_targets_the_right_weighted_sibling(fake):
    dns.delete_record(
        zone_id="Z0861165ABC", name="api.example-site-a.com", record_type="A",
        set_identifier="blue", sr_number="SR-11", actor_email="a@b.com",
    )
    rs = fake.change_calls[0]["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
    assert rs["SetIdentifier"] == "blue"


def test_delete_missing_record_raises_before_calling_aws(fake):
    with pytest.raises(dns.DnsRecordError, match="No A record"):
        dns.delete_record(
            zone_id="Z0861165ABC", name="ghost.example-site-a.com", record_type="A",
            sr_number="SR-11", actor_email="a@b.com",
        )
    assert fake.change_calls == []


def test_delete_handles_escaped_record_name(fake):
    """The record whose Route 53 name is "\\040_verify..." must be
    deletable using the human form shown in the UI."""
    dns.delete_record(
        zone_id="Z0861165ABC", name=" _verify.example-site-a.com", record_type="TXT",
        sr_number="SR-11", actor_email="a@b.com",
    )
    rs = fake.change_calls[0]["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
    assert rs["Name"] == "\\040_verify.example-site-a.com."
