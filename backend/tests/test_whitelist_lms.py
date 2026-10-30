"""
Tests for app/whitelist_lms.py — the "Whitelist (LMS)" tool's check/add
logic against WAFv2 IPSets and one EC2 Security Group. No real AWS calls:
whitelist_lms._client() is monkeypatched to return small fake wafv2/ec2
clients built for this file.

Region is no longer a caller-supplied parameter (see whitelist_lms.py's
"Region — AUTO-DETECTED" docstring section) — check_ip()/add_ip() figure
out which of VALID_REGIONS the target actually lives in by probing
us-east-1 first, then ap-southeast-1. _patch_clients() below lets each
test control which fake client (or None, simulating "not found in this
region") is returned per region, so the auto-detection fallback path
itself can be exercised directly.

The admin-editable target registry overlay (see whitelist_lms.py's
"Admin-editable target registry" docstring section) is persisted via
settings_store.py's generic key/value store, backed by SQLite in
ENV=local — the `isolated_settings_db` fixture below gives each test its
own in-memory engine (mirrors test_whitelist_store.py's isolated_db
fixture exactly) so no test can observe another test's overlay state.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.whitelist_lms as whitelist_lms_module
import app.settings_store as settings_store_module
from app.database import Base


@pytest.fixture(autouse=True)
def isolated_settings_db(monkeypatch):
    """Autouse: every test gets its own empty settings table, so the
    Whitelist (LMS) target-registry overlay (stored under
    WHITELIST_TARGETS_OVERLAY_KEY) never leaks between tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(settings_store_module, "SessionLocal", TestSessionLocal)
    yield
    engine.dispose()


class FakeWafv2:
    def __init__(self, ip_sets: dict[str, dict], update_error=None):
        # ip_sets: {name: {"id": ..., "addresses": [...], "lock_token": ...}}
        self._ip_sets = ip_sets
        self._update_error = update_error
        self.update_calls = []

    def list_ip_sets(self, **kwargs):
        return {"IPSets": [{"Name": name, "Id": info["id"]} for name, info in self._ip_sets.items()]}

    def get_ip_set(self, **kwargs):
        name = kwargs["Name"]
        info = self._ip_sets[name]
        return {
            "IPSet": {"Id": info["id"], "Name": name, "Addresses": list(info["addresses"]), "IPAddressVersion": "IPV4"},
            "LockToken": info["lock_token"],
        }

    def update_ip_set(self, **kwargs):
        self.update_calls.append(kwargs)
        if self._update_error:
            raise self._update_error
        # simulate the update actually landing, for realism
        name = kwargs["Name"]
        self._ip_sets[name]["addresses"] = kwargs["Addresses"]
        return {}


class FakeEc2:
    def __init__(self, security_groups: dict[str, dict], authorize_error=None):
        self._security_groups = security_groups
        self._authorize_error = authorize_error
        self.authorize_calls = []

    def describe_security_groups(self, **kwargs):
        group_id = kwargs["GroupIds"][0]
        sg = self._security_groups.get(group_id)
        return {"SecurityGroups": [sg] if sg else []}

    def authorize_security_group_ingress(self, **kwargs):
        self.authorize_calls.append(kwargs)
        if self._authorize_error:
            raise self._authorize_error
        return {}


def _patch_clients(monkeypatch, wafv2=None, ec2=None, wafv2_by_region=None, ec2_by_region=None):
    """
    Simple case: pass a single `wafv2`/`ec2` fake — it's returned for
    EVERY region probed (fine for tests where the resource is found on
    the very first region tried, us-east-1, since auto-detection never
    needs to fall through to ap-southeast-1).

    Region-aware case: pass `wafv2_by_region`/`ec2_by_region` as a dict
    {region: fake_or_None} to control exactly what each region returns —
    use this to exercise the "not found in us-east-1, found in
    ap-southeast-1" fallback path, or the "not found in either region"
    error path.
    """
    def _client(service, region):
        if service == "wafv2":
            if wafv2_by_region is not None:
                fake = wafv2_by_region.get(region)
                if fake is None:
                    return FakeWafv2({})  # empty client == "not found here"
                return fake
            return wafv2
        if service == "ec2":
            if ec2_by_region is not None:
                fake = ec2_by_region.get(region)
                if fake is None:
                    return FakeEc2({})
                return fake
            return ec2
        raise AssertionError(f"unexpected service {service}")

    monkeypatch.setattr(whitelist_lms_module, "_client", _client)
    # Auto-detected regions are cached per resource name/group-id in a
    # module-level dict that would otherwise leak across tests (a fake
    # client is a per-test stand-in, so a stale cache entry from a prior
    # test would make _locate_ip_set()/_locate_security_group() trust an
    # old region without re-probing) — clear it before every test.
    whitelist_lms_module._TARGET_REGION_CACHE.clear()


WAF_TARGET_ID = "pro_lms3_boswapp"                     # REGIONAL scope
WAF_TARGET_LABEL = "PRO-LMS3-BOSWAPP-Whitelist"
CF_WAF_TARGET_ID = "newlms_backend"                     # CLOUDFRONT scope
CF_WAF_TARGET_LABEL = "newlms-prod-ipset-whitelist-backend"
SG_TARGET_ID = "avpc_sftp_client_sg_02"
# Swapped 2026-09-16: sg-021238ff0c0e522cc (SG-02) -> sg-07cb8e11e83e9efb3
# (SG-03) — see whitelist_lms.py module docstring's "Target resources"
# section. The target_id (avpc_sftp_client_sg_02) is unchanged; only the
# underlying AWS group_id moved.
SG_GROUP_ID = "sg-07cb8e11e83e9efb3"


# ── list_targets ────────────────────────────────────────────────────────

def test_list_targets_returns_all_8_targets_without_internal_fields():
    targets = whitelist_lms_module.list_targets()
    assert len(targets) == 8
    for t in targets:
        # waf_scope/group_id/aws_lookup_name are intentionally NOT
        # exposed by the plain picker endpoint — they're internal AWS
        # API details, not something a regular user needs to see or
        # choose (see module docstring's "Region" section). linked_domain
        # and is_custom ARE exposed — see list_targets()'s docstring.
        assert set(t.keys()) == {"id", "label", "resource_type", "linked_domain", "is_custom"}
        assert t["is_custom"] is False
    ids = {t["id"] for t in targets}
    assert WAF_TARGET_ID in ids
    assert CF_WAF_TARGET_ID in ids
    assert SG_TARGET_ID in ids


def test_sg_target_points_at_sg_03_not_sg_02():
    """Confirms the SG-02 -> SG-03 swap took effect in the default
    registry (see module docstring's "Target resources" section)."""
    target = whitelist_lms_module._targets_by_id()[SG_TARGET_ID]
    assert target["group_id"] == "sg-07cb8e11e83e9efb3"
    assert "SG-03" in target["label"]
    assert "sg-021238ff0c0e522cc" not in target["label"]


def test_three_cloudfront_scope_targets_are_registered_correctly():
    """The 3 targets confirmed live under CLOUDFRONT ("Global") scope —
    see module docstring's "Target resources" section."""
    cf_ids = {"newlms_partnerportal", "newlms_backend", "newlms_lmsprod"}
    targets_by_id = whitelist_lms_module._targets_by_id()
    for target_id in cf_ids:
        target = targets_by_id[target_id]
        assert target["waf_scope"] == "cloudfront"
        assert target["resource_type"] == "waf_ipset"


# ── Input validation ────────────────────────────────────────────────────

def test_check_ip_rejects_unknown_target(monkeypatch):
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.check_ip("not_a_real_target", "1.2.3.4")


def test_check_ip_rejects_invalid_ip():
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.check_ip(WAF_TARGET_ID, "not-an-ip")


def test_add_ip_rejects_missing_sr_number(monkeypatch):
    fake_wafv2 = FakeWafv2({WAF_TARGET_LABEL: {"id": "ipset-1", "addresses": [], "lock_token": "token-1"}})
    _patch_clients(monkeypatch, wafv2=fake_wafv2)
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.add_ip(WAF_TARGET_ID, "1.2.3.4", "")
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.add_ip(WAF_TARGET_ID, "1.2.3.4", "   ")
    assert fake_wafv2.update_calls == []


# ── WAF IPSet: check ──────────────────────────────────────────────────────

def test_check_ip_waf_ipset_returns_true_when_already_present(monkeypatch):
    fake_wafv2 = FakeWafv2({WAF_TARGET_LABEL: {"id": "ipset-1", "addresses": ["1.2.3.4/32"], "lock_token": "token-1"}})
    _patch_clients(monkeypatch, wafv2=fake_wafv2)

    result = whitelist_lms_module.check_ip(WAF_TARGET_ID, "1.2.3.4")

    assert result["already_whitelisted"] is True
    assert result["target_label"] == WAF_TARGET_LABEL
    assert result["region"] == "us-east-1"  # first region probed and found here


def test_check_ip_waf_ipset_returns_false_when_not_present(monkeypatch):
    fake_wafv2 = FakeWafv2({WAF_TARGET_LABEL: {"id": "ipset-1", "addresses": ["9.9.9.9/32"], "lock_token": "token-1"}})
    _patch_clients(monkeypatch, wafv2=fake_wafv2)

    result = whitelist_lms_module.check_ip(WAF_TARGET_ID, "1.2.3.4")

    assert result["already_whitelisted"] is False


def test_check_ip_waf_ipset_matches_ip_within_a_wider_cidr(monkeypatch):
    """An IP already covered by a wider CIDR block already in the set
    (e.g. 1.2.3.0/24) should be treated as already whitelisted, not
    requiring a redundant /32 entry."""
    fake_wafv2 = FakeWafv2({WAF_TARGET_LABEL: {"id": "ipset-1", "addresses": ["1.2.3.0/24"], "lock_token": "token-1"}})
    _patch_clients(monkeypatch, wafv2=fake_wafv2)

    result = whitelist_lms_module.check_ip(WAF_TARGET_ID, "1.2.3.4")

    assert result["already_whitelisted"] is True


def test_check_ip_waf_ipset_not_found_in_either_region_raises_error(monkeypatch):
    _patch_clients(monkeypatch, wafv2_by_region={"us-east-1": FakeWafv2({}), "ap-southeast-1": FakeWafv2({})})
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.check_ip(WAF_TARGET_ID, "1.2.3.4")


def test_check_ip_waf_ipset_falls_back_to_second_region_when_not_in_first(monkeypatch):
    """Auto-detection: not found in us-east-1 -> falls back to
    ap-southeast-1 and finds it there."""
    ap_southeast_1_wafv2 = FakeWafv2({WAF_TARGET_LABEL: {"id": "ipset-1", "addresses": ["1.2.3.4/32"], "lock_token": "token-1"}})
    _patch_clients(monkeypatch, wafv2_by_region={"us-east-1": FakeWafv2({}), "ap-southeast-1": ap_southeast_1_wafv2})

    result = whitelist_lms_module.check_ip(WAF_TARGET_ID, "1.2.3.4")

    assert result["region"] == "ap-southeast-1"
    assert result["already_whitelisted"] is True


def test_check_ip_waf_ipset_caches_resolved_region_across_calls(monkeypatch):
    """Once a target's region is resolved, a second check_ip() call for
    the same target must not re-probe us-east-1 first — verified here by
    counting how many times each region's list_ip_sets() is actually
    called across two check_ip() invocations for the same target."""
    call_counts = {"us-east-1": 0, "ap-southeast-1": 0}

    class _CountingWafv2(FakeWafv2):
        def __init__(self, region, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._region = region

        def list_ip_sets(self, **kwargs):
            call_counts[self._region] += 1
            return super().list_ip_sets(**kwargs)

    us_east_1_wafv2 = _CountingWafv2("us-east-1", {})  # always empty, not found
    ap_southeast_1_wafv2 = _CountingWafv2(
        "ap-southeast-1", {WAF_TARGET_LABEL: {"id": "ipset-1", "addresses": ["1.2.3.4/32"], "lock_token": "token-1"}},
    )
    _patch_clients(monkeypatch, wafv2_by_region={"us-east-1": us_east_1_wafv2, "ap-southeast-1": ap_southeast_1_wafv2})

    first = whitelist_lms_module.check_ip(WAF_TARGET_ID, "1.2.3.4")
    assert first["region"] == "ap-southeast-1"
    assert call_counts["us-east-1"] == 1  # had to probe once to learn it's not there
    assert call_counts["ap-southeast-1"] == 1

    second = whitelist_lms_module.check_ip(WAF_TARGET_ID, "9.9.9.9")
    assert second["region"] == "ap-southeast-1"
    # us-east-1 must NOT be re-probed on the second call — the resolved
    # region is cached.
    assert call_counts["us-east-1"] == 1
    assert call_counts["ap-southeast-1"] == 2


# ── WAF IPSet: add ────────────────────────────────────────────────────────

def test_add_ip_waf_ipset_calls_update_ip_set_with_appended_cidr(monkeypatch):
    fake_wafv2 = FakeWafv2({WAF_TARGET_LABEL: {"id": "ipset-1", "addresses": ["9.9.9.9/32"], "lock_token": "token-1"}})
    _patch_clients(monkeypatch, wafv2=fake_wafv2)

    result = whitelist_lms_module.add_ip(WAF_TARGET_ID, "1.2.3.4", "SR-000123")

    assert result["already_whitelisted"] is False
    assert result["sr_number"] == "SR-000123"
    assert result["region"] == "us-east-1"
    assert len(fake_wafv2.update_calls) == 1
    call = fake_wafv2.update_calls[0]
    assert call["Name"] == WAF_TARGET_LABEL
    assert call["Scope"] == "REGIONAL"
    assert call["Id"] == "ipset-1"
    assert call["LockToken"] == "token-1"
    assert "1.2.3.4/32" in call["Addresses"]
    assert "9.9.9.9/32" in call["Addresses"]


def test_add_ip_waf_ipset_is_idempotent_when_already_present(monkeypatch):
    fake_wafv2 = FakeWafv2({WAF_TARGET_LABEL: {"id": "ipset-1", "addresses": ["1.2.3.4/32"], "lock_token": "token-1"}})
    _patch_clients(monkeypatch, wafv2=fake_wafv2)

    result = whitelist_lms_module.add_ip(WAF_TARGET_ID, "1.2.3.4", "SR-000123")

    assert result["already_whitelisted"] is True
    assert fake_wafv2.update_calls == []  # no AWS write call made — see add_ip()'s docstring


def test_add_ip_waf_ipset_wraps_update_failure_in_whitelist_lms_error(monkeypatch):
    fake_wafv2 = FakeWafv2(
        {WAF_TARGET_LABEL: {"id": "ipset-1", "addresses": [], "lock_token": "token-1"}},
        update_error=RuntimeError("WAFOptimisticLockException"),
    )
    _patch_clients(monkeypatch, wafv2=fake_wafv2)
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.add_ip(WAF_TARGET_ID, "1.2.3.4", "SR-000123")


# ── WAF IPSet: CLOUDFRONT scope ("Global" in the AWS console) ────────────
# See module docstring's "Target resources" section — these 3 targets are
# always us-east-1 with Scope=CLOUDFRONT, never auto-detected/probed
# against ap-southeast-1 the way REGIONAL-scope targets are.

def test_check_ip_cloudfront_ipset_uses_cloudfront_scope_and_fixed_region(monkeypatch):
    fake_wafv2 = FakeWafv2({CF_WAF_TARGET_LABEL: {"id": "cf-ipset-1", "addresses": ["1.2.3.4/32"], "lock_token": "token-1"}})
    seen_scopes = []
    original_list = fake_wafv2.list_ip_sets

    def _tracking_list(**kwargs):
        seen_scopes.append(kwargs.get("Scope"))
        return original_list(**kwargs)
    fake_wafv2.list_ip_sets = _tracking_list

    _patch_clients(monkeypatch, wafv2=fake_wafv2)

    result = whitelist_lms_module.check_ip(CF_WAF_TARGET_ID, "1.2.3.4")

    assert result["already_whitelisted"] is True
    assert result["region"] == "us-east-1"
    assert seen_scopes == ["CLOUDFRONT"]  # never queried with Scope=REGIONAL


def test_check_ip_cloudfront_ipset_never_falls_back_to_second_region(monkeypatch):
    """Unlike REGIONAL-scope targets, a CLOUDFRONT-scope target that is
    NOT found in us-east-1 must raise immediately — there is no second
    region to fall back to (CLOUDFRONT scope only ever exists via the
    us-east-1 API endpoint)."""
    _patch_clients(monkeypatch, wafv2_by_region={"us-east-1": FakeWafv2({}), "ap-southeast-1": FakeWafv2(
        {CF_WAF_TARGET_LABEL: {"id": "wrong", "addresses": [], "lock_token": "t"}},
    )})
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.check_ip(CF_WAF_TARGET_ID, "1.2.3.4")


def test_add_ip_cloudfront_ipset_calls_update_ip_set_with_cloudfront_scope(monkeypatch):
    fake_wafv2 = FakeWafv2({CF_WAF_TARGET_LABEL: {"id": "cf-ipset-1", "addresses": ["9.9.9.9/32"], "lock_token": "token-1"}})
    _patch_clients(monkeypatch, wafv2=fake_wafv2)

    result = whitelist_lms_module.add_ip(CF_WAF_TARGET_ID, "1.2.3.4", "SR-000456")

    assert result["already_whitelisted"] is False
    assert result["region"] == "us-east-1"
    assert len(fake_wafv2.update_calls) == 1
    call = fake_wafv2.update_calls[0]
    assert call["Scope"] == "CLOUDFRONT"
    assert call["Name"] == CF_WAF_TARGET_LABEL
    assert "1.2.3.4/32" in call["Addresses"]


def test_add_ip_cloudfront_ipset_is_idempotent_when_already_present(monkeypatch):
    fake_wafv2 = FakeWafv2({CF_WAF_TARGET_LABEL: {"id": "cf-ipset-1", "addresses": ["1.2.3.4/32"], "lock_token": "token-1"}})
    _patch_clients(monkeypatch, wafv2=fake_wafv2)

    result = whitelist_lms_module.add_ip(CF_WAF_TARGET_ID, "1.2.3.4", "SR-000456")

    assert result["already_whitelisted"] is True
    assert fake_wafv2.update_calls == []


# ── Security Group: check ─────────────────────────────────────────────────

def _sg_with_ingress(cidrs_ports: list[tuple[str, int, int, str]]) -> dict:
    return {
        "GroupId": SG_GROUP_ID,
        "IpPermissions": [
            {"IpProtocol": proto, "FromPort": from_p, "ToPort": to_p, "IpRanges": [{"CidrIp": cidr}]}
            for cidr, from_p, to_p, proto in cidrs_ports
        ],
    }


def test_check_ip_security_group_returns_true_when_already_present_on_default_port(monkeypatch):
    sg = _sg_with_ingress([("1.2.3.4/32", 22, 22, "tcp")])
    fake_ec2 = FakeEc2({SG_GROUP_ID: sg})
    _patch_clients(monkeypatch, ec2=fake_ec2)

    result = whitelist_lms_module.check_ip(SG_TARGET_ID, "1.2.3.4")

    assert result["already_whitelisted"] is True
    assert result["region"] == "us-east-1"  # first region probed and found here


def test_check_ip_security_group_returns_false_when_ip_present_but_wrong_port(monkeypatch):
    sg = _sg_with_ingress([("1.2.3.4/32", 443, 443, "tcp")])  # not port 22 (the default SFTP port)
    fake_ec2 = FakeEc2({SG_GROUP_ID: sg})
    _patch_clients(monkeypatch, ec2=fake_ec2)

    result = whitelist_lms_module.check_ip(SG_TARGET_ID, "1.2.3.4")

    assert result["already_whitelisted"] is False


def test_check_ip_security_group_not_found_in_either_region_raises_error(monkeypatch):
    _patch_clients(monkeypatch, ec2_by_region={"us-east-1": FakeEc2({}), "ap-southeast-1": FakeEc2({})})
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.check_ip(SG_TARGET_ID, "1.2.3.4")


def test_check_ip_security_group_falls_back_to_second_region_when_not_in_first(monkeypatch):
    """Matches the real deployment: this SG target lives in
    ap-southeast-1, so auto-detection should fall through from
    us-east-1."""
    sg = _sg_with_ingress([("1.2.3.4/32", 22, 22, "tcp")])
    _patch_clients(monkeypatch, ec2_by_region={"us-east-1": FakeEc2({}), "ap-southeast-1": FakeEc2({SG_GROUP_ID: sg})})

    result = whitelist_lms_module.check_ip(SG_TARGET_ID, "1.2.3.4")

    assert result["region"] == "ap-southeast-1"
    assert result["already_whitelisted"] is True


# ── Security Group: add ───────────────────────────────────────────────────

def test_add_ip_security_group_calls_authorize_ingress_with_default_port(monkeypatch):
    sg = _sg_with_ingress([])
    fake_ec2 = FakeEc2({SG_GROUP_ID: sg})
    _patch_clients(monkeypatch, ec2=fake_ec2)

    result = whitelist_lms_module.add_ip(SG_TARGET_ID, "1.2.3.4", "SR-000999")

    assert result["already_whitelisted"] is False
    assert result["region"] == "us-east-1"
    assert len(fake_ec2.authorize_calls) == 1
    call = fake_ec2.authorize_calls[0]
    assert call["GroupId"] == SG_GROUP_ID
    perm = call["IpPermissions"][0]
    assert perm["FromPort"] == 22
    assert perm["ToPort"] == 22
    assert perm["IpProtocol"] == "tcp"
    assert perm["IpRanges"][0]["CidrIp"] == "1.2.3.4/32"
    assert "SR-000999" in perm["IpRanges"][0]["Description"]


def test_add_ip_security_group_respects_custom_port_override(monkeypatch):
    sg = _sg_with_ingress([])
    fake_ec2 = FakeEc2({SG_GROUP_ID: sg})
    _patch_clients(monkeypatch, ec2=fake_ec2)

    whitelist_lms_module.add_ip(SG_TARGET_ID, "1.2.3.4", "SR-000999", port=443, protocol="tcp")

    call = fake_ec2.authorize_calls[0]
    assert call["IpPermissions"][0]["FromPort"] == 443


def test_add_ip_security_group_is_idempotent_when_already_present(monkeypatch):
    sg = _sg_with_ingress([("1.2.3.4/32", 22, 22, "tcp")])
    fake_ec2 = FakeEc2({SG_GROUP_ID: sg})
    _patch_clients(monkeypatch, ec2=fake_ec2)

    result = whitelist_lms_module.add_ip(SG_TARGET_ID, "1.2.3.4", "SR-000999")

    assert result["already_whitelisted"] is True
    assert fake_ec2.authorize_calls == []


def test_add_ip_security_group_wraps_authorize_failure_in_whitelist_lms_error(monkeypatch):
    sg = _sg_with_ingress([])
    fake_ec2 = FakeEc2({SG_GROUP_ID: sg}, authorize_error=RuntimeError("InvalidPermission.Duplicate"))
    _patch_clients(monkeypatch, ec2=fake_ec2)
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.add_ip(SG_TARGET_ID, "1.2.3.4", "SR-000999")


# ── Admin-editable target registry overlay ─────────────────────────────
# See whitelist_lms.py's "Admin-editable target registry" docstring
# section. resolve_waf_ipset_name()/resolve_security_group_id() reuse the
# same _client() monkeypatch as check_ip()/add_ip() above.

ACTOR = {"actor_email": "admin@example.com", "actor_name": "Admin"}


def test_resolve_waf_ipset_name_returns_single_match(monkeypatch):
    """A single shared fake (via wafv2=...) is returned for EVERY probe
    (both REGIONAL regions AND the CLOUDFRONT us-east-1 endpoint), so it
    would report 3 matches — use wafv2_by_region here to isolate the
    "found under REGIONAL in us-east-1 only" scenario, then separately
    force the CLOUDFRONT probe's underlying helper to report no match,
    since _patch_clients' region-keyed dict can't distinguish two
    different Scope values against the SAME region (us-east-1 serves
    both REGIONAL probes and the CLOUDFRONT probe)."""
    fake_wafv2 = FakeWafv2({"NewSet": {"id": "ipset-9", "addresses": ["1.2.3.4/32"], "lock_token": "t"}})
    _patch_clients(monkeypatch, wafv2_by_region={"us-east-1": fake_wafv2, "ap-southeast-1": FakeWafv2({})})
    original_find = whitelist_lms_module._find_ip_set_in_region

    def _find_regional_only(client, name, scope):
        if scope == "CLOUDFRONT":
            return None
        return original_find(client, name, scope)

    monkeypatch.setattr(whitelist_lms_module, "_find_ip_set_in_region", _find_regional_only)

    matches = whitelist_lms_module.resolve_waf_ipset_name("NewSet")

    assert len(matches) == 1
    assert matches[0]["waf_scope"] == "regional"
    assert matches[0]["region"] == "us-east-1"
    assert matches[0]["ip_set_id"] == "ipset-9"
    assert matches[0]["address_count"] == 1


def test_resolve_waf_ipset_name_returns_empty_list_when_not_found(monkeypatch):
    _patch_clients(monkeypatch, wafv2=FakeWafv2({}))
    matches = whitelist_lms_module.resolve_waf_ipset_name("DoesNotExist")
    assert matches == []


def test_resolve_waf_ipset_name_rejects_blank_input():
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.resolve_waf_ipset_name("   ")


def test_resolve_security_group_id_returns_match(monkeypatch):
    fake_ec2 = FakeEc2({"sg-newone": {"GroupId": "sg-newone", "GroupName": "New SG", "VpcId": "vpc-1", "IpPermissions": []}})
    _patch_clients(monkeypatch, ec2=fake_ec2)

    found = whitelist_lms_module.resolve_security_group_id("sg-newone")

    assert found == {"region": "us-east-1", "group_name": "New SG", "vpc_id": "vpc-1"}


def test_resolve_security_group_id_returns_none_when_not_found(monkeypatch):
    _patch_clients(monkeypatch, ec2=FakeEc2({}))
    assert whitelist_lms_module.resolve_security_group_id("sg-ghost") is None


def test_add_custom_waf_target_persists_and_appears_in_effective_targets(monkeypatch):
    fake_wafv2 = FakeWafv2({"CustomSet": {"id": "ipset-c1", "addresses": [], "lock_token": "t"}})
    _patch_clients(monkeypatch, wafv2=fake_wafv2)

    target = whitelist_lms_module.add_custom_waf_target(
        label="My Custom WAF Target", ip_set_name="CustomSet", waf_scope="regional",
        linked_domain="example.example.com", **ACTOR,
    )

    assert target["resource_type"] == "waf_ipset"
    assert target["aws_lookup_name"] == "CustomSet"
    assert target["linked_domain"] == "example.example.com"

    all_targets = whitelist_lms_module.list_targets_admin()
    assert any(t["id"] == target["id"] and t["is_custom"] is True for t in all_targets)
    # Total count is now 9 (8 defaults + 1 custom).
    assert len(all_targets) == 9


def test_add_custom_waf_target_rejects_name_that_does_not_resolve(monkeypatch):
    """A name that resolve_waf_ipset_name() can't find anywhere must be
    rejected, not silently registered as a dangling target."""
    _patch_clients(monkeypatch, wafv2=FakeWafv2({}))

    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.add_custom_waf_target(
            label="Bad Target", ip_set_name="TotallyMissingSet", waf_scope="regional", linked_domain=None, **ACTOR,
        )


def test_add_custom_waf_target_rejects_scope_mismatch(monkeypatch):
    """The IPSet exists, but only under CLOUDFRONT scope — claiming
    waf_scope='regional' for it must be rejected rather than silently
    accepted, since add_custom_waf_target() re-verifies the scope against
    a fresh resolve_waf_ipset_name() call rather than trusting the
    caller's waf_scope argument blindly."""
    fake_wafv2 = FakeWafv2({"CfOnlySet": {"id": "ipset-cf1", "addresses": [], "lock_token": "t"}})
    # Only the CLOUDFRONT-scope probe (us-east-1) finds it; both REGIONAL
    # probes (us-east-1 and ap-southeast-1) come back empty.
    _patch_clients(monkeypatch, wafv2_by_region={"us-east-1": FakeWafv2({}), "ap-southeast-1": FakeWafv2({})})
    # _patch_clients' wafv2_by_region path doesn't distinguish the
    # CLOUDFRONT probe (same region, different Scope) from the REGIONAL
    # probe of the same region, so directly monkeypatch resolve's
    # underlying helper to simulate "found only under cloudfront".
    monkeypatch.setattr(
        whitelist_lms_module, "_find_ip_set_in_region",
        lambda client, name, scope: (
            {"id": "ipset-cf1", "name": name, "scope": scope, "addresses": [], "ip_address_version": "IPV4", "lock_token": "t"}
            if scope == "CLOUDFRONT" else None
        ),
    )

    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.add_custom_waf_target(
            label="Mismatched Scope", ip_set_name="CfOnlySet", waf_scope="regional", linked_domain=None, **ACTOR,
        )


def test_add_custom_sg_target_persists_with_port_and_protocol(monkeypatch):
    fake_ec2 = FakeEc2({"sg-custom1": {"GroupId": "sg-custom1", "GroupName": "Custom SG", "VpcId": "vpc-2", "IpPermissions": []}})
    _patch_clients(monkeypatch, ec2=fake_ec2)

    target = whitelist_lms_module.add_custom_sg_target(
        label="Custom SFTP SG", group_id="sg-custom1", linked_domain="sftp2.example.com",
        port=2222, protocol="tcp", **ACTOR,
    )

    assert target["group_id"] == "sg-custom1"
    assert target["port"] == 2222
    assert target["protocol"] == "tcp"
    assert target["linked_domain"] == "sftp2.example.com"


def test_add_custom_sg_target_rejects_unresolvable_group_id(monkeypatch):
    _patch_clients(monkeypatch, ec2=FakeEc2({}))
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.add_custom_sg_target(
            label="Ghost SG", group_id="sg-ghost1", linked_domain=None, port=None, protocol=None, **ACTOR,
        )


def test_add_custom_sg_target_rejects_malformed_group_id():
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.add_custom_sg_target(
            label="Bad", group_id="not-a-group-id", linked_domain=None, port=None, protocol=None, **ACTOR,
        )


def test_update_target_edits_default_target_via_override(monkeypatch):
    updated = whitelist_lms_module.update_target(
        WAF_TARGET_ID, label="Renamed Label", linked_domain="renamed.example.com", **ACTOR,
    )
    assert updated["label"] == "Renamed Label"
    assert updated["linked_domain"] == "renamed.example.com"
    assert updated["is_custom"] is False
    # Underlying AWS-facing identity (waf_scope) is untouched by the edit.
    assert updated["waf_scope"] == "regional"


def test_update_target_edits_custom_target_in_place(monkeypatch):
    fake_ec2 = FakeEc2({"sg-editme": {"GroupId": "sg-editme", "GroupName": "n", "VpcId": "v", "IpPermissions": []}})
    _patch_clients(monkeypatch, ec2=fake_ec2)
    created = whitelist_lms_module.add_custom_sg_target(
        label="Original", group_id="sg-editme", linked_domain=None, port=None, protocol=None, **ACTOR,
    )

    updated = whitelist_lms_module.update_target(created["id"], label="Edited", port=8022, **ACTOR)

    assert updated["label"] == "Edited"
    assert updated["port"] == 8022
    assert updated["group_id"] == "sg-editme"  # unchanged


def test_update_target_rejects_port_protocol_on_non_sg_target():
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.update_target(WAF_TARGET_ID, port=443, **ACTOR)


def test_update_target_rejects_unknown_target_id():
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.update_target("not_a_real_target", label="x", **ACTOR)


def test_remove_default_target_soft_deletes_and_restore_brings_it_back():
    all_before = whitelist_lms_module.list_targets_admin()
    assert any(t["id"] == WAF_TARGET_ID for t in all_before)

    whitelist_lms_module.remove_target(WAF_TARGET_ID, **ACTOR)
    all_after_remove = whitelist_lms_module.list_targets_admin()
    assert not any(t["id"] == WAF_TARGET_ID for t in all_after_remove)
    assert len(all_after_remove) == 7

    whitelist_lms_module.restore_default_target(WAF_TARGET_ID, **ACTOR)
    all_after_restore = whitelist_lms_module.list_targets_admin()
    assert any(t["id"] == WAF_TARGET_ID for t in all_after_restore)
    assert len(all_after_restore) == 8


def test_remove_custom_target_deletes_outright_not_soft(monkeypatch):
    fake_ec2 = FakeEc2({"sg-temp1": {"GroupId": "sg-temp1", "GroupName": "n", "VpcId": "v", "IpPermissions": []}})
    _patch_clients(monkeypatch, ec2=fake_ec2)
    created = whitelist_lms_module.add_custom_sg_target(
        label="Temp", group_id="sg-temp1", linked_domain=None, port=None, protocol=None, **ACTOR,
    )
    assert len(whitelist_lms_module.list_targets_admin()) == 9

    whitelist_lms_module.remove_target(created["id"], **ACTOR)

    all_after = whitelist_lms_module.list_targets_admin()
    assert len(all_after) == 8
    assert not any(t["id"] == created["id"] for t in all_after)
    # Restoring a custom target's id must fail — restore only applies to
    # built-in defaults.
    with pytest.raises(whitelist_lms_module.WhitelistLmsError):
        whitelist_lms_module.restore_default_target(created["id"], **ACTOR)


def test_check_ip_against_custom_waf_target_uses_aws_lookup_name(monkeypatch):
    """A custom target's display label can differ from the real AWS
    IPSet Name — check_ip() must query AWS using aws_lookup_name, not
    the display label."""
    fake_wafv2 = FakeWafv2({"RealAwsName": {"id": "ipset-x", "addresses": ["5.5.5.5/32"], "lock_token": "t"}})
    _patch_clients(monkeypatch, wafv2=fake_wafv2)

    created = whitelist_lms_module.add_custom_waf_target(
        label="Friendly Display Name", ip_set_name="RealAwsName", waf_scope="regional", linked_domain=None, **ACTOR,
    )

    result = whitelist_lms_module.check_ip(created["id"], "5.5.5.5")
    assert result["already_whitelisted"] is True
    assert result["target_label"] == "Friendly Display Name"
