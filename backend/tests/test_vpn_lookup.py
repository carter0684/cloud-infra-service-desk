"""
Tests for app/vpn_lookup.py — the "VPN Profile Lookup" tool: given a
site's WAN IP, scans every region of a target AWS account for a matching
Customer Gateway + Site-to-Site VPN connection and returns the full
tunnel/IKE/IPsec/DPD profile.

None of these tests make real AWS calls: this module's own cross-account
plumbing (_cross_account_session, _regions_for_account,
_regional_client_for_account) is monkeypatched directly, and a small fake
EC2 client stands in for describe_customer_gateways/
describe_vpn_connections. Unlike every other cross-account tool in this
codebase, vpn_lookup.py does NOT go through cost_explorer._core_session
— it assumes JobPortal-CrossAccount-InfraOps-<acct> directly using the
ECS task's own identity (see vpn_lookup.py's module docstring) — so these
tests patch this module's own _cross_account_session, not
app.cost_explorer._core_session.
"""

from __future__ import annotations

import pytest

import app.vpn_lookup as vpn_lookup_module


@pytest.fixture(autouse=True)
def _force_real_path(monkeypatch):
    """Every test in this file exercises the real (non-mock) code path —
    _should_use_mock_inventory() is cmdb.py's own function (imported into
    vpn_lookup.py), so patch it here rather than relying on ENV, exactly
    like tests would patch a locally-defined flag."""
    monkeypatch.setattr(vpn_lookup_module, "_should_use_mock_inventory", lambda: False)


class FakeEc2:
    def __init__(self, customer_gateways=None, vpn_connections=None):
        self._customer_gateways = customer_gateways or []
        self._vpn_connections = vpn_connections or []
        self.describe_customer_gateways_calls = []
        self.describe_vpn_connections_calls = []

    def describe_customer_gateways(self, **kwargs):
        self.describe_customer_gateways_calls.append(kwargs)
        wan_ip = kwargs["Filters"][0]["Values"][0]
        return {"CustomerGateways": [c for c in self._customer_gateways if c.get("IpAddress") == wan_ip]}

    def describe_vpn_connections(self, **kwargs):
        self.describe_vpn_connections_calls.append(kwargs)
        cgw_ids = set(kwargs["Filters"][0]["Values"])
        return {"VpnConnections": [v for v in self._vpn_connections if v.get("CustomerGatewayId") in cgw_ids]}


def _tunnel_option(outside_ip=None, cidr="169.254.240.84/30", psk="examplepsk1", **overrides):
    base = {
        "OutsideIpAddress": outside_ip,
        "TunnelInsideCidr": cidr,
        "PreSharedKey": psk,
        "IkeVersions": [{"Value": "ikev2"}],
        "Phase1EncryptionAlgorithms": [{"Value": "AES256"}],
        "Phase1IntegrityAlgorithms": [{"Value": "SHA2-256"}],
        "Phase1DHGroupNumbers": [{"Value": 14}],
        "Phase1LifetimeSeconds": 28800,
        "Phase2EncryptionAlgorithms": [{"Value": "AES256"}],
        "Phase2IntegrityAlgorithms": [{"Value": "SHA2-256"}],
        "Phase2DHGroupNumbers": [{"Value": 14}],
        "Phase2LifetimeSeconds": 3600,
        "DpdTimeoutSeconds": 30,
        "DpdTimeoutAction": "clear",
    }
    base.update(overrides)
    return base


def _vgw_entry(outside_ip, status="UP", accepted_route_count=0):
    return {"OutsideIpAddress": outside_ip, "Status": status, "StatusMessage": "", "AcceptedRouteCount": accepted_route_count, "LastStatusChange": None}


def _sample_vpn_connection():
    return {
        "VpnConnectionId": "vpn-0ab9ab4ce4f1e66ed",
        "State": "available",
        "CustomerGatewayId": "cgw-01234567abcde1234",
        "VpnGatewayId": "vgw-01234567abcde1234",
        "TransitGatewayId": None,
        "Tags": [{"Key": "Name", "Value": "YUL-US Permanent"}],
        "Options": {
            "StaticRoutesOnly": True,
            "TunnelOptions": [
                _tunnel_option(cidr="169.254.240.84/30", psk="pOlNN9P2PdLMsOY2JqLIWj_wIJAlygo5"),
                _tunnel_option(cidr="169.254.136.44/30", psk="YkPjJii5AcZ6WaMZP27RkJtc1vk6X83h"),
            ],
        },
        "VgwTelemetry": [
            _vgw_entry("198.51.100.168"),
            _vgw_entry("198.51.100.4"),
        ],
    }


def _sample_customer_gateway(wan_ip="203.0.113.18"):
    return {"CustomerGatewayId": "cgw-01234567abcde1234", "IpAddress": wan_ip, "State": "available"}


def _patch_common(monkeypatch, ec2_by_region: dict, regions: list[str]):
    monkeypatch.setattr(vpn_lookup_module, "_regions_for_account", lambda *a, **k: regions)

    def _regional_client(account_id, service, region):
        assert service == "ec2"
        return ec2_by_region.get(region, FakeEc2())

    monkeypatch.setattr(vpn_lookup_module, "_regional_client_for_account", _regional_client)
    # This module assumes JobPortal-CrossAccount-InfraOps-<acct> directly
    # via the ECS task's own identity — not app.cost_explorer's Core account
    # session — so patch this module's own _cross_account_session.
    monkeypatch.setattr(vpn_lookup_module, "_cross_account_session", lambda *a, **k: object())


# ── Input validation ───────────────────────────────────────────────────────

def test_search_rejects_empty_wan_ip(monkeypatch):
    _patch_common(monkeypatch, {}, [])
    with pytest.raises(vpn_lookup_module.VpnLookupError):
        vpn_lookup_module.search_vpn_by_wan_ip("")
    with pytest.raises(vpn_lookup_module.VpnLookupError):
        vpn_lookup_module.search_vpn_by_wan_ip("   ")


def test_search_rejects_invalid_ip_format(monkeypatch):
    _patch_common(monkeypatch, {}, [])
    with pytest.raises(vpn_lookup_module.VpnLookupError):
        vpn_lookup_module.search_vpn_by_wan_ip("not-an-ip")
    with pytest.raises(vpn_lookup_module.VpnLookupError):
        vpn_lookup_module.search_vpn_by_wan_ip("999.999.999.999")


def test_search_raises_vpn_lookup_error_when_credentials_unavailable(monkeypatch):
    monkeypatch.setattr(vpn_lookup_module, "_regions_for_account", lambda *a, **k: ["us-east-1"])

    def _raise(*a, **k):
        raise RuntimeError("could not assume JobPortal-CrossAccount-InfraOps-1000")

    monkeypatch.setattr(vpn_lookup_module, "_cross_account_session", _raise)
    with pytest.raises(vpn_lookup_module.VpnLookupError):
        vpn_lookup_module.search_vpn_by_wan_ip("203.0.113.18")


# ── Happy path: single matching region ──────────────────────────────────────

def test_search_finds_connection_in_matching_region(monkeypatch):
    wan_ip = "203.0.113.18"
    fake_ec2 = FakeEc2(
        customer_gateways=[_sample_customer_gateway(wan_ip)],
        vpn_connections=[_sample_vpn_connection()],
    )
    _patch_common(monkeypatch, {"us-east-1": fake_ec2, "ap-southeast-1": FakeEc2()}, ["us-east-1", "ap-southeast-1"])

    result = vpn_lookup_module.search_vpn_by_wan_ip(wan_ip, account_id="100000000003")

    assert result["wan_ip"] == wan_ip
    assert result["account_id"] == "100000000003"
    assert result["account_name"] == "network"
    assert result["is_mock"] is False
    assert sorted(result["regions_scanned"]) == ["ap-southeast-1", "us-east-1"]
    assert result["errors"] == []
    assert len(result["connections"]) == 1

    conn = result["connections"][0]
    assert conn["vpn_connection_id"] == "vpn-0ab9ab4ce4f1e66ed"
    assert conn["name"] == "YUL-US Permanent"
    assert conn["region"] == "us-east-1"
    assert conn["state"] == "available"
    assert conn["customer_gateway_id"] == "cgw-01234567abcde1234"
    assert conn["customer_gateway_ip"] == wan_ip
    assert conn["routing_type"] == "Static (no BGP)"
    assert len(conn["tunnels"]) == 2


def test_tunnel_fields_match_expected_example_values(monkeypatch):
    wan_ip = "203.0.113.18"
    fake_ec2 = FakeEc2(
        customer_gateways=[_sample_customer_gateway(wan_ip)],
        vpn_connections=[_sample_vpn_connection()],
    )
    _patch_common(monkeypatch, {"us-east-1": fake_ec2}, ["us-east-1"])

    result = vpn_lookup_module.search_vpn_by_wan_ip(wan_ip, account_id="100000000003")
    conn = result["connections"][0]
    t1, t2 = conn["tunnels"]

    assert t1["tunnel_number"] == 1
    assert t1["aws_outside_ip"] == "198.51.100.168"
    assert t1["aws_inside_ip"] == "169.254.240.85/30"
    assert t1["local_outside_ip"] == wan_ip
    assert t1["local_inside_ip"] == "169.254.240.86/30"
    assert t1["pre_shared_key"] == "pOlNN9P2PdLMsOY2JqLIWj_wIJAlygo5"
    assert t1["ike"]["version"] == "IKEv2"
    assert t1["ike"]["encryption"] == "AES256"
    assert t1["ike"]["integrity"] == "SHA2-256"
    assert t1["ike"]["dh_group"] == "14"
    assert t1["ike"]["lifetime_seconds"] == 28800
    assert t1["ike"]["authentication"] == "Pre-Shared Key"
    assert t1["ipsec"]["encryption"] == "AES256"
    assert t1["ipsec"]["integrity"] == "SHA2-256"
    assert t1["ipsec"]["dh_group"] == "14"
    assert t1["ipsec"]["lifetime_seconds"] == 3600
    assert t1["ipsec"]["mode"] == "Tunnel"
    assert t1["dpd"]["timeout_seconds"] == 30
    assert t1["dpd"]["action"] == "clear"

    assert t2["tunnel_number"] == 2
    assert t2["aws_outside_ip"] == "198.51.100.4"
    assert t2["aws_inside_ip"] == "169.254.136.45/30"
    assert t2["local_inside_ip"] == "169.254.136.46/30"
    assert t2["pre_shared_key"] == "YkPjJii5AcZ6WaMZP27RkJtc1vk6X83h"


def test_firewall_rules_reference_both_tunnel_outside_ips(monkeypatch):
    wan_ip = "203.0.113.18"
    fake_ec2 = FakeEc2(
        customer_gateways=[_sample_customer_gateway(wan_ip)],
        vpn_connections=[_sample_vpn_connection()],
    )
    _patch_common(monkeypatch, {"us-east-1": fake_ec2}, ["us-east-1"])

    result = vpn_lookup_module.search_vpn_by_wan_ip(wan_ip, account_id="100000000003")
    rules = result["connections"][0]["firewall_rules"]

    assert any("UDP 500" in r and "198.51.100.168" in r and "198.51.100.4" in r for r in rules)
    assert any("UDP 4500" in r for r in rules)
    assert any("IP Protocol 50" in r for r in rules)


# ── No match / multi-region behavior ────────────────────────────────────────

def test_search_returns_empty_connections_when_no_customer_gateway_matches(monkeypatch):
    _patch_common(monkeypatch, {"us-east-1": FakeEc2(), "ap-southeast-1": FakeEc2()}, ["us-east-1", "ap-southeast-1"])

    result = vpn_lookup_module.search_vpn_by_wan_ip("1.2.3.4", account_id="100000000003")

    assert result["connections"] == []
    assert result["errors"] == []


def test_search_excludes_deleted_customer_gateway_state(monkeypatch):
    wan_ip = "203.0.113.18"
    deleted_cgw = {"CustomerGatewayId": "cgw-deleted", "IpAddress": wan_ip, "State": "deleted"}
    fake_ec2 = FakeEc2(customer_gateways=[deleted_cgw], vpn_connections=[])
    _patch_common(monkeypatch, {"us-east-1": fake_ec2}, ["us-east-1"])

    result = vpn_lookup_module.search_vpn_by_wan_ip(wan_ip, account_id="100000000003")
    assert result["connections"] == []


def test_search_excludes_deleted_vpn_connections(monkeypatch):
    wan_ip = "203.0.113.18"
    conn = _sample_vpn_connection()
    conn["State"] = "deleted"
    fake_ec2 = FakeEc2(customer_gateways=[_sample_customer_gateway(wan_ip)], vpn_connections=[conn])
    _patch_common(monkeypatch, {"us-east-1": fake_ec2}, ["us-east-1"])

    result = vpn_lookup_module.search_vpn_by_wan_ip(wan_ip, account_id="100000000003")
    assert result["connections"] == []


def test_search_scans_multiple_regions_and_only_matching_region_returns_connection(monkeypatch):
    wan_ip = "203.0.113.18"
    fake_ec2_match = FakeEc2(customer_gateways=[_sample_customer_gateway(wan_ip)], vpn_connections=[_sample_vpn_connection()])
    fake_ec2_no_match = FakeEc2()
    _patch_common(monkeypatch, {
        "us-east-1": fake_ec2_match, "ap-southeast-1": fake_ec2_no_match, "eu-west-1": fake_ec2_no_match,
    }, ["us-east-1", "ap-southeast-1", "eu-west-1"])

    result = vpn_lookup_module.search_vpn_by_wan_ip(wan_ip, account_id="100000000003")

    assert len(result["connections"]) == 1
    assert result["connections"][0]["region"] == "us-east-1"
    assert sorted(result["regions_scanned"]) == ["ap-southeast-1", "eu-west-1", "us-east-1"]


def test_search_isolates_per_region_failures_into_errors_list(monkeypatch):
    wan_ip = "203.0.113.18"
    fake_ec2_match = FakeEc2(customer_gateways=[_sample_customer_gateway(wan_ip)], vpn_connections=[_sample_vpn_connection()])

    class FailingEc2:
        def describe_customer_gateways(self, **kwargs):
            raise RuntimeError("region boom")

    _patch_common(monkeypatch, {"us-east-1": fake_ec2_match, "ap-southeast-1": FailingEc2()}, ["us-east-1", "ap-southeast-1"])

    result = vpn_lookup_module.search_vpn_by_wan_ip(wan_ip, account_id="100000000003")

    assert len(result["connections"]) == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["region"] == "ap-southeast-1"


# ── Mock path (ENV=local, no real credentials) ──────────────────────────────

def test_mock_search_returns_deterministic_result_for_example_wan_ip(monkeypatch):
    monkeypatch.setattr(vpn_lookup_module, "_should_use_mock_inventory", lambda: True)
    result = vpn_lookup_module.search_vpn_by_wan_ip("203.0.113.18", account_id="100000000003")
    assert result["is_mock"] is True
    assert len(result["connections"]) == 1
    assert result["connections"][0]["vpn_connection_id"] == "vpn-0ab9ab4ce4f1e66ed"


def test_mock_search_returns_empty_for_other_wan_ips(monkeypatch):
    monkeypatch.setattr(vpn_lookup_module, "_should_use_mock_inventory", lambda: True)
    result = vpn_lookup_module.search_vpn_by_wan_ip("1.2.3.4", account_id="100000000003")
    assert result["is_mock"] is True
    assert result["connections"] == []


# ── Helper functions ─────────────────────────────────────────────────────

def test_tunnel_inside_ips_computes_aws_and_local_addresses():
    aws_ip, local_ip = vpn_lookup_module._tunnel_inside_ips("169.254.240.84/30")
    assert aws_ip == "169.254.240.85/30"
    assert local_ip == "169.254.240.86/30"


def test_tunnel_inside_ips_returns_none_for_missing_cidr():
    assert vpn_lookup_module._tunnel_inside_ips(None) == (None, None)


def test_tunnel_inside_ips_returns_none_for_invalid_cidr():
    assert vpn_lookup_module._tunnel_inside_ips("not-a-cidr") == (None, None)


def test_values_joins_list_of_value_dicts():
    assert vpn_lookup_module._values([{"Value": "AES256"}, {"Value": "AES128"}]) == "AES256, AES128"
    assert vpn_lookup_module._values([]) is None
    assert vpn_lookup_module._values(None) is None


def test_ike_versions_maps_to_display_labels():
    assert vpn_lookup_module._ike_versions([{"Value": "ikev2"}]) == "IKEv2"
    assert vpn_lookup_module._ike_versions([{"Value": "ikev1"}, {"Value": "ikev2"}]) == "IKEv1, IKEv2"


# ── Cross-account assume-role (JobPortal-CrossAccount-InfraOps-*) ──────────
# This module deliberately does NOT use Core account static credentials or
# PortalReadOnlyRole (unlike every other cross-account tool in this
# codebase) — it uses the ECS task's own identity to assume a dedicated
# per-account InfraOps role. These tests verify that specific contract:
# role ARN naming, ExternalId, and session caching.

class _FakeStsClient:
    """Stands in for boto3.client("sts") — records every assume_role call
    so tests can assert on RoleArn/ExternalId/RoleSessionName without
    hitting real AWS. boto3.client("sts") itself is never mocked at a
    lower level since the whole point being tested is that THIS module
    calls plain boto3.client("sts") (i.e. relies on the ECS task's own
    container credentials) rather than going through any Core account
    session object."""

    def __init__(self):
        self.assume_role_calls = []

    def assume_role(self, **kwargs):
        self.assume_role_calls.append(kwargs)
        from datetime import datetime, timedelta, timezone
        return {
            "Credentials": {
                "AccessKeyId": "ASIAFAKE",
                "SecretAccessKey": "fakesecret",
                "SessionToken": "faketoken",
                "Expiration": datetime.now(timezone.utc) + timedelta(hours=1),
            }
        }


def test_cross_account_role_arn_uses_first_four_digits_of_account_id():
    arn = vpn_lookup_module._cross_account_role_arn("100000000003")
    assert arn == "arn:aws:iam::100000000003:role/JobPortal-CrossAccount-InfraOps-1000"


def test_cross_account_session_calls_sts_assume_role_with_expected_params(monkeypatch):
    vpn_lookup_module._cross_account_session_cache.clear()
    fake_sts = _FakeStsClient()
    monkeypatch.setattr(vpn_lookup_module.boto3, "client", lambda *a, **k: fake_sts)

    vpn_lookup_module._cross_account_session("100000000003")

    assert len(fake_sts.assume_role_calls) == 1
    call = fake_sts.assume_role_calls[0]
    assert call["RoleArn"] == "arn:aws:iam::100000000003:role/JobPortal-CrossAccount-InfraOps-1000"
    assert call["ExternalId"] == "jobportal-infra-ops"
    assert call["RoleSessionName"] == "jobportal-infra-ops"
    assert call["DurationSeconds"] == 3600


def test_cross_account_session_is_cached_across_calls(monkeypatch):
    vpn_lookup_module._cross_account_session_cache.clear()
    fake_sts = _FakeStsClient()
    monkeypatch.setattr(vpn_lookup_module.boto3, "client", lambda *a, **k: fake_sts)

    session1 = vpn_lookup_module._cross_account_session("100000000003")
    session2 = vpn_lookup_module._cross_account_session("100000000003")

    assert session1 is session2
    assert len(fake_sts.assume_role_calls) == 1  # second call served from cache


def test_cross_account_session_raises_vpn_lookup_error_on_assume_role_failure(monkeypatch):
    vpn_lookup_module._cross_account_session_cache.clear()

    class _FailingSts:
        def assume_role(self, **kwargs):
            raise RuntimeError("AccessDenied: not authorized to assume this role")

    monkeypatch.setattr(vpn_lookup_module.boto3, "client", lambda *a, **k: _FailingSts())

    with pytest.raises(vpn_lookup_module.VpnLookupError):
        vpn_lookup_module._cross_account_session("100000000003")
