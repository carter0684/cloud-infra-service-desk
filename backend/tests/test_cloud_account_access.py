"""Tests for the AWS access path, connection test and CloudFormation template
(.kiro/specs/cloud-account-onboarding, task 3)."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app import cloud_account_access as acc
from app import cloud_account_store as store

ACCT = "123456789012"
EXTERNAL_ID = "Zx9" + "a" * 40
TASK_ROLE = "arn:aws:iam::100000000002:role/JobPortalComputeStack-JobPortalTaskRoleABC123"


def _entry(**kw):
    e = {
        "account_key": f"aws#{ACCT}", "provider": "aws", "identifier": ACCT, "display_name": "Test",
        "status": "connected", "access_mode": "infraops", "external_id": EXTERNAL_ID,
        "role_name": f"JobPortal-CrossAccount-InfraOps-{ACCT[:4]}", "seeded": False,
    }
    e.update(kw)
    return e


def _client_error(code, message="raw aws text with arn:aws:iam::1:role/x and ExternalId=SECRET"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "AssumeRole")


class FakeSts:
    def __init__(self, expires_in=timedelta(hours=1), error=None):
        self.calls = []
        self.expires_in = expires_in
        self.error = error

    def assume_role(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return {"Credentials": {
            "AccessKeyId": "AKIAFAKEFAKEFAKE", "SecretAccessKey": "fake-secret", "SessionToken": "fake-token",
            "Expiration": datetime.now(timezone.utc) + self.expires_in,
        }}


@pytest.fixture
def sts(monkeypatch):
    fake = FakeSts()
    monkeypatch.setattr(acc, "_sts_client", lambda: fake)
    acc.invalidate()
    monkeypatch.setattr(acc, "_task_role_arn_cache", None)
    yield fake
    acc.invalidate()


@pytest.fixture
def registry(monkeypatch):
    """Serve entries to acc.session() without a database."""
    entries = {ACCT: _entry()}
    monkeypatch.setattr(store, "find", lambda provider, ident: entries.get(ident) if provider == "aws" else None)
    return entries


# ── Names and ARNs ──────────────────────────────────────────────────────────

def test_role_name_uses_the_first_four_digits():
    assert acc.role_name_for("100000000003") == "JobPortal-CrossAccount-InfraOps-1000"
    assert acc.role_name_for("100000000006") == "JobPortal-CrossAccount-InfraOps-1000"
    assert acc.role_arn_for("100000000003") == "arn:aws:iam::100000000003:role/JobPortal-CrossAccount-InfraOps-1000"


@pytest.mark.parametrize("bad", ["", "12345", "1234567890123", "12345678901a", None])
def test_role_name_rejects_anything_but_twelve_digits(bad):
    with pytest.raises(ValueError):
        acc.role_name_for(bad)


def test_existing_vpn_lookup_role_naming_is_unchanged_and_matches():
    from app import vpn_lookup

    assert vpn_lookup._cross_account_role_arn("100000000003").endswith(acc.role_name_for("100000000003"))


def test_task_role_arn_env_override(sts, monkeypatch):
    monkeypatch.setenv("JOBPORTAL_TASK_ROLE_ARN", TASK_ROLE)
    assert acc.task_role_arn() == TASK_ROLE


def test_task_role_arn_is_derived_from_the_assumed_role_identity(monkeypatch, sts):
    monkeypatch.delenv("JOBPORTAL_TASK_ROLE_ARN", raising=False)
    sts.get_caller_identity = lambda: {
        "Arn": "arn:aws:sts::100000000002:assumed-role/JobPortalComputeStack-JobPortalTaskRoleABC123/3f9a8b7c"
    }
    assert acc.task_role_arn() == TASK_ROLE


def test_task_role_arn_refuses_a_non_role_identity(monkeypatch, sts):
    monkeypatch.delenv("JOBPORTAL_TASK_ROLE_ARN", raising=False)
    sts.get_caller_identity = lambda: {"Arn": "arn:aws:iam::100000000002:user/someone"}
    with pytest.raises(acc.AccessError):
        acc.task_role_arn()


# ── Session ─────────────────────────────────────────────────────────────────

def test_session_assumes_the_right_role_with_the_accounts_own_external_id(sts, registry):
    acc.session(ACCT)
    assert len(sts.calls) == 1
    call = sts.calls[0]
    assert call["RoleArn"] == f"arn:aws:iam::{ACCT}:role/JobPortal-CrossAccount-InfraOps-1234"
    assert call["ExternalId"] == EXTERNAL_ID
    assert call["RoleSessionName"] == "jobportal-infra-ops"
    assert call["DurationSeconds"] == 3600


def test_two_accounts_use_their_own_external_ids(sts, registry):
    other = "999988887777"
    registry[other] = _entry(identifier=other, external_id="Q" * 40, role_name=acc.role_name_for(other))
    acc.session(ACCT)
    acc.session(other)
    assert {c["ExternalId"] for c in sts.calls} == {EXTERNAL_ID, "Q" * 40}


def test_session_is_cached_per_account(sts, registry):
    s1 = acc.session(ACCT)
    s2 = acc.session(ACCT)
    assert s1 is s2
    assert len(sts.calls) == 1


def test_session_is_refreshed_shortly_before_it_expires(sts, registry):
    sts.expires_in = timedelta(minutes=4)  # inside the 5-minute refresh margin
    acc.session(ACCT)
    acc.session(ACCT)
    assert len(sts.calls) == 2


def test_invalidate_forces_a_new_assume_role(sts, registry):
    acc.session(ACCT)
    acc.invalidate(ACCT)
    acc.session(ACCT)
    assert len(sts.calls) == 2


def test_session_refuses_unknown_offboarded_and_legacy_accounts(sts, registry):
    with pytest.raises(acc.AccessError):
        acc.session("000000000000")
    registry[ACCT] = _entry(status="offboarded")
    with pytest.raises(acc.AccessError):
        acc.session(ACCT)
    registry[ACCT] = _entry(access_mode="legacy")
    with pytest.raises(acc.AccessError):
        acc.session(ACCT)
    assert sts.calls == []  # never reached STS


def test_session_turns_sts_failures_into_safe_messages(sts, registry):
    sts.error = _client_error("AccessDenied")
    with pytest.raises(acc.AccessError) as exc:
        acc.session(ACCT)
    assert "External ID" in exc.value.message
    assert EXTERNAL_ID not in exc.value.message and "SECRET" not in exc.value.message


def test_client_is_scoped_to_the_requested_region(sts, registry):
    c = acc.client(ACCT, "ec2", region_name="ap-southeast-1")
    assert c.meta.region_name == "ap-southeast-1"


# ── classify_error ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc,code", [
    (_client_error("AccessDenied"), "access_denied"),
    (_client_error("ExpiredToken"), "portal_credentials"),
    (_client_error("Throttling"), "aws_error"),
    (EndpointConnectionError(endpoint_url="https://sts.amazonaws.com"), "network"),
    (RuntimeError("boom with SECRET"), "unexpected"),
])
def test_classify_error_codes_and_no_raw_text(exc, code):
    got_code, message = acc.classify_error(exc, ACCT, "infraops")
    assert got_code == code
    assert "SECRET" not in message and "arn:aws" not in message and EXTERNAL_ID not in message


def test_classify_error_other_client_error_shows_only_the_code():
    _, message = acc.classify_error(_client_error("Throttling"), ACCT, "infraops")
    assert "Throttling" in message


def test_classify_error_strips_odd_characters_from_the_error_code():
    _, message = acc.classify_error(_client_error("Bad<script>Code"), ACCT, "infraops")
    assert "<" not in message and ">" not in message


# ── Connection test ─────────────────────────────────────────────────────────

def test_infraops_connection_test_succeeds_when_the_caller_account_matches(sts, monkeypatch):
    monkeypatch.setattr(acc, "_caller_account", lambda creds: ACCT)
    result = acc.test_aws_connection(_entry())
    assert result == {"ok": True, "code": "ok", "message": "Connected."}
    assert sts.calls[0]["ExternalId"] == EXTERNAL_ID


def test_infraops_connection_test_detects_a_different_account(sts, monkeypatch):
    monkeypatch.setattr(acc, "_caller_account", lambda creds: "999999999999")
    result = acc.test_aws_connection(_entry())
    assert result["ok"] is False and result["code"] == "account_mismatch"


def test_infraops_connection_test_maps_access_denied(sts):
    sts.error = _client_error("AccessDenied")
    result = acc.test_aws_connection(_entry())
    assert (result["ok"], result["code"]) == (False, "access_denied")
    assert "trust policy" in result["message"]
    assert EXTERNAL_ID not in result["message"]


def test_infraops_connection_test_maps_network_errors(sts):
    sts.error = EndpointConnectionError(endpoint_url="https://sts.amazonaws.com")
    assert acc.test_aws_connection(_entry())["code"] == "network"


def test_connection_test_never_raises_on_unexpected_errors(sts):
    sts.error = RuntimeError("kaboom")
    result = acc.test_aws_connection(_entry())
    assert result["ok"] is False and result["code"] == "unexpected"
    assert "kaboom" not in result["message"]


def test_infraops_connection_test_without_an_external_id_fails_cleanly(sts):
    result = acc.test_aws_connection(_entry(external_id=None))
    assert result["ok"] is False
    assert sts.calls == []


class _CoreSts:
    def __init__(self, account="100000000003", error=None):
        self.assumed = []
        self.account = account
        self.error = error

    def assume_role(self, **kw):
        self.assumed.append(kw)
        if self.error:
            raise self.error
        return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "b", "SessionToken": "c"}}

    def get_caller_identity(self):
        return {"Account": self.account, "Arn": f"arn:aws:iam::{self.account}:user/core"}


class _CoreSession:
    def __init__(self, sts):
        self._sts = sts

    def client(self, name, **kw):
        assert name == "sts"
        return self._sts


def test_legacy_connection_test_uses_the_core_keys_and_portal_readonly_role(monkeypatch):
    from app import cost_explorer

    core_sts = _CoreSts()
    monkeypatch.setattr(cost_explorer, "_core_session", lambda: _CoreSession(core_sts))
    monkeypatch.setattr(acc, "_caller_account", lambda creds: "100000000003")
    result = acc.test_aws_connection(_entry(identifier="100000000003", access_mode="legacy", external_id=None))
    assert result["ok"] is True
    assert core_sts.assumed[0]["RoleArn"] == "arn:aws:iam::100000000003:role/PortalReadOnlyRole"
    assert "ExternalId" not in core_sts.assumed[0]


def test_legacy_connection_test_for_the_core_account_checks_the_core_keys_directly(monkeypatch):
    from app import cost_explorer

    core_sts = _CoreSts(account="100000000001")
    monkeypatch.setattr(cost_explorer, "_core_session", lambda: _CoreSession(core_sts))
    result = acc.test_aws_connection(_entry(identifier="100000000001", access_mode="legacy", external_id=None))
    assert result["ok"] is True
    assert core_sts.assumed == []  # no AssumeRole for the account whose keys we hold


def test_legacy_connection_test_reports_a_missing_role_without_raw_text(monkeypatch):
    from app import cost_explorer

    core_sts = _CoreSts(error=_client_error("AccessDenied"))
    monkeypatch.setattr(cost_explorer, "_core_session", lambda: _CoreSession(core_sts))
    result = acc.test_aws_connection(_entry(identifier="100000000003", access_mode="legacy", external_id=None))
    assert (result["ok"], result["code"]) == (False, "access_denied")
    assert "SECRET" not in result["message"]


def test_legacy_connection_test_reports_missing_portal_credentials(monkeypatch):
    from app import cost_explorer

    def no_creds():
        raise cost_explorer.CostAnalysisError("Could not load Core account credentials: secret")

    monkeypatch.setattr(cost_explorer, "_core_session", no_creds)
    result = acc.test_aws_connection(_entry(identifier="100000000003", access_mode="legacy", external_id=None))
    assert result["code"] == "portal_credentials"
    assert "secret" not in result["message"].lower()


# ── Client factories: new path only for infraops ────────────────────────────

class _Core:
    def __init__(self):
        self.clients = []

    def client(self, service, **kw):
        self.clients.append((service, kw))
        return ("core-client", service, kw.get("region_name"))


@pytest.fixture
def spy_access(monkeypatch):
    calls = []

    def fake_client(account_id, service, region_name="us-east-1", config=None):
        calls.append((account_id, service, region_name, config))
        return "infraops-client"

    monkeypatch.setattr(acc, "client", fake_client)
    return calls


def _mode(monkeypatch, mode):
    monkeypatch.setattr(store, "access_mode", lambda account_id: mode)


def test_cost_explorer_factory_takes_the_new_path_only_for_infraops(monkeypatch, spy_access):
    from app import cost_explorer

    _mode(monkeypatch, "infraops")
    assert cost_explorer._client_for_account(ACCT, "ce", _Core()) == "infraops-client"
    assert spy_access == [(ACCT, "ce", "us-east-1", None)]

    spy_access.clear()
    _mode(monkeypatch, "legacy")
    core = _Core()
    out = cost_explorer._client_for_account(cost_explorer.CORE_ACCOUNT_ID, "ce", core)
    assert out == ("core-client", "ce", "us-east-1")  # unchanged legacy behaviour
    assert spy_access == []


def test_cmdb_factory_passes_the_region_through(monkeypatch, spy_access):
    from app import cmdb

    _mode(monkeypatch, "infraops")
    assert cmdb._regional_client_for_account(ACCT, "ec2", "eu-west-1", _Core()) == "infraops-client"
    assert spy_access == [(ACCT, "ec2", "eu-west-1", None)]

    spy_access.clear()
    _mode(monkeypatch, "legacy")
    out = cmdb._regional_client_for_account(cmdb.CORE_ACCOUNT_ID, "ec2", "eu-west-1", _Core())
    assert out == ("core-client", "ec2", "eu-west-1")
    assert spy_access == []


def test_security_hub_factory_uses_the_security_hub_region(monkeypatch, spy_access):
    from app import security_hub

    _mode(monkeypatch, "infraops")
    assert security_hub._securityhub_client_for_account(ACCT, _Core()) == "infraops-client"
    assert spy_access == [(ACCT, "securityhub", security_hub._SECURITY_HUB_REGION, None)]

    spy_access.clear()
    _mode(monkeypatch, "legacy")
    out = security_hub._securityhub_client_for_account(security_hub.CORE_ACCOUNT_ID, _Core())
    assert out == ("core-client", "securityhub", security_hub._SECURITY_HUB_REGION)
    assert spy_access == []


def test_health_factory_passes_the_boto_config(monkeypatch, spy_access):
    from app import health_member_accounts as hma

    _mode(monkeypatch, "infraops")
    assert hma._health_client_for(ACCT) == "infraops-client"
    assert spy_access == [(ACCT, "health", "us-east-1", hma._BOTO_CONFIG)]

    spy_access.clear()
    _mode(monkeypatch, "legacy")
    monkeypatch.setattr(hma.escalation, "_get_ams_health_client", lambda: "ams-health")
    assert hma._health_client_for(hma.CORE_ACCOUNT_ID) == "ams-health"
    assert spy_access == []


# ── CloudFormation template ─────────────────────────────────────────────────

yaml = pytest.importorskip("yaml")


def _rendered(**kw):
    return acc.render_template(_entry(**kw), task_role=TASK_ROLE)


def test_template_file_ships_with_the_app_package():
    assert acc.TEMPLATE_PATH.exists()
    assert acc.TEMPLATE_PATH.parent.name == "cloud_account_templates"


def test_template_renders_to_valid_yaml_with_no_markers_left():
    text = _rendered()
    assert "{{" not in text and "}}" not in text
    doc = yaml.safe_load(text)
    assert doc["AWSTemplateFormatVersion"] == "2010-09-09"


def test_template_creates_exactly_one_role_named_with_the_first_four_digits():
    doc = yaml.safe_load(_rendered())
    resources = doc["Resources"]
    assert len(resources) == 1
    (res,) = resources.values()
    assert res["Type"] == "AWS::IAM::Role"
    assert res["Properties"]["RoleName"] == "JobPortal-CrossAccount-InfraOps-1234"


def test_template_trusts_only_the_task_role_and_requires_the_external_id():
    doc = yaml.safe_load(_rendered())
    (res,) = doc["Resources"].values()
    statements = res["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1
    st = statements[0]
    assert st["Effect"] == "Allow"
    assert st["Principal"] == {"AWS": TASK_ROLE}
    assert st["Action"] == "sts:AssumeRole"
    assert st["Condition"] == {"StringEquals": {"sts:ExternalId": EXTERNAL_ID}}


def test_template_grants_only_the_read_only_managed_policy_and_nothing_inline():
    doc = yaml.safe_load(_rendered())
    (res,) = doc["Resources"].values()
    props = res["Properties"]
    assert props["ManagedPolicyArns"] == ["arn:aws:iam::aws:policy/ReadOnlyAccess"]
    assert "Policies" not in props and "PermissionsBoundary" not in props


def test_template_has_no_write_iam_or_wildcard_grants():
    text = _rendered()
    doc = yaml.safe_load(text)
    (res,) = doc["Resources"].values()
    # Only the trust policy carries Allow statements; none grants "*" or write-type actions.
    for st in res["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
        assert st["Action"] == "sts:AssumeRole"
        assert st["Principal"] != {"AWS": "*"} and "root" not in str(st["Principal"])
    for forbidden in ("Administrator", "PowerUser", "\"*\""):
        assert forbidden not in text
    # No IAM action strings (ARNs contain "iam::", actions look like "iam:PassRole").
    assert not re.search(r"\biam:[A-Za-z*]", text)

    # Every action / condition-key style string in the document is one of the two sts names.
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(k)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and re.fullmatch(r"[a-z0-9-]+:[A-Za-z*]+", node):
            found.add(node)

    walk(doc)
    assert found == {"sts:AssumeRole", "sts:ExternalId"}


def test_template_outputs_the_role_arn():
    doc = yaml.safe_load(_rendered())
    assert "RoleArn" in doc["Outputs"]


def test_template_session_duration_parameter_matches_what_the_portal_requests():
    doc = yaml.safe_load(_rendered())
    p = doc["Parameters"]["MaxSessionDuration"]
    assert p["Default"] == 3600 and p["MinValue"] == 3600
    assert acc.SESSION_DURATION_SECONDS <= p["Default"]


def test_rendered_external_id_is_long_enough_and_safe():
    assert len(EXTERNAL_ID) >= 32
    assert EXTERNAL_ID in _rendered()


@pytest.mark.parametrize("field,value", [
    ("external_id", "short"),
    ("external_id", "a" * 40 + "\"\n  Evil: x"),
    ("external_id", None),
    ("role_name", "JobPortal-CrossAccount-InfraOps-12345"),
    ("role_name", "SomeOtherRole"),
    ("identifier", "12345"),
])
def test_render_rejects_values_that_could_break_the_yaml(field, value):
    entry = _entry()
    entry[field] = value
    with pytest.raises(ValueError):
        acc.render_template(entry, task_role=TASK_ROLE)


def test_render_rejects_a_malformed_task_role_arn():
    with pytest.raises(ValueError):
        acc.render_template(_entry(), task_role="arn:aws:iam::100000000002:role/x\"\nEvil: y")


# ── Role permissions ────────────────────────────────────────────────────────

CREDS = {"AccessKeyId": "AKIAFAKE", "SecretAccessKey": "fake-secret", "SessionToken": "fake-token"}
ROLE_ARN = f"arn:aws:iam::{ACCT}:role/JobPortal-CrossAccount-InfraOps-1234"
READONLY_ARN = "arn:aws:iam::aws:policy/ReadOnlyAccess"


class FakeIam:
    """Just enough of the IAM client for describe_role_permissions."""

    def __init__(self, *, allow=(), policies=None, inline=(), boundary=None, truncate=False,
                 get_role_error=None, simulate_error=None):
        self.allow = set(allow)
        self.policies = policies if policies is not None else [{"PolicyName": "ReadOnlyAccess", "PolicyArn": READONLY_ARN}]
        self.inline = list(inline)
        self.boundary = boundary
        self.truncate = truncate
        self.get_role_error = get_role_error
        self.simulate_error = simulate_error
        self.simulated = []

    def get_role(self, RoleName):
        if self.get_role_error:
            raise self.get_role_error
        role = {"Arn": ROLE_ARN, "MaxSessionDuration": 3600}
        if self.boundary:
            role["PermissionsBoundary"] = {"PermissionsBoundaryArn": self.boundary}
        return {"Role": role}

    def list_attached_role_policies(self, RoleName, Marker=None):
        if self.truncate and Marker is None:
            return {"AttachedPolicies": self.policies[:1], "IsTruncated": True, "Marker": "m1"}
        return {"AttachedPolicies": self.policies[1:] if self.truncate else self.policies, "IsTruncated": False}

    def list_role_policies(self, RoleName, Marker=None):
        return {"PolicyNames": self.inline, "IsTruncated": False}

    def simulate_principal_policy(self, PolicySourceArn, ActionNames, ResourceArns, Marker=None):
        if self.simulate_error:
            raise self.simulate_error
        self.simulated.extend(ActionNames)
        return {"EvaluationResults": [
            {"EvalActionName": a, "EvalDecision": "allowed" if a in self.allow else "implicitDeny"} for a in ActionNames
        ], "IsTruncated": False}


@pytest.fixture
def iam_world(monkeypatch):
    class W:
        iam = FakeIam()
        clients = []
    w = W()
    w.clients = []
    monkeypatch.setattr(acc, "_assume", lambda entry: dict(CREDS))

    def fake_boto_client(service, **kwargs):
        w.clients.append((service, kwargs))
        assert service == "iam"
        return w.iam

    monkeypatch.setattr(acc.boto3, "client", fake_boto_client)
    return w


def test_permissions_of_a_read_only_role_say_so(iam_world):
    r = acc.describe_role_permissions(_entry())
    assert r["ok"] is True
    assert r["identity"] == {"account": ACCT, "kind": "role", "role_name": "JobPortal-CrossAccount-InfraOps-1234",
                             "arn": ROLE_ARN, "max_session_duration": 3600, "permissions_boundary": None}
    assert r["managed_policies"] == [{"name": "ReadOnlyAccess", "arn": READONLY_ARN, "aws_managed": True}]
    assert r["inline_policies"] == []
    sim = r["simulation"]
    assert sim["available"] is True and sim["write_allowed"] == []
    assert sim["write_checked"] == len(acc.WRITE_PROBES)
    assert r["summary"].startswith("Read-only")
    assert iam_world.clients[0][0] == "iam"


def test_permissions_flag_every_write_action_the_role_allows(iam_world):
    iam_world.iam = FakeIam(allow={"s3:PutObject", "iam:CreateUser", "s3:GetObject"})
    r = acc.describe_role_permissions(_entry())
    assert r["simulation"]["write_allowed"] == ["iam:CreateUser", "s3:PutObject"]  # sorted, writes only
    assert r["simulation"]["data_read_allowed"] == ["s3:GetObject"]
    assert "can change things" in r["summary"] and "2 of" in r["summary"]


def test_permissions_probe_lists_contain_only_the_intended_kinds_of_actions():
    assert len(set(acc.WRITE_PROBES)) == len(acc.WRITE_PROBES)
    assert not set(acc.WRITE_PROBES) & set(acc.DATA_READ_PROBES)
    for action in acc.DATA_READ_PROBES:
        assert re.match(r"^[a-z0-9]+:(Get|Query|Scan|Filter|Receive)", action), action


def test_permissions_cover_customer_managed_inline_and_boundary(iam_world):
    custom = "arn:aws:iam::123456789012:policy/Extra"
    iam_world.iam = FakeIam(
        policies=[{"PolicyName": "ReadOnlyAccess", "PolicyArn": READONLY_ARN}, {"PolicyName": "Extra", "PolicyArn": custom}],
        inline=["Temp"], boundary="arn:aws:iam::123456789012:policy/Boundary",
    )
    r = acc.describe_role_permissions(_entry())
    assert [p["aws_managed"] for p in r["managed_policies"]] == [True, False]
    assert r["inline_policies"] == ["Temp"]
    assert r["identity"]["permissions_boundary"].endswith("/Boundary")


def test_permissions_follow_paginated_policy_lists(iam_world):
    iam_world.iam = FakeIam(
        policies=[{"PolicyName": "A", "PolicyArn": READONLY_ARN}, {"PolicyName": "B", "PolicyArn": READONLY_ARN + "2"}],
        truncate=True,
    )
    assert [p["name"] for p in acc.describe_role_permissions(_entry())["managed_policies"]] == ["A", "B"]


def test_permissions_when_the_role_cannot_read_its_own_policies(iam_world):
    iam_world.iam = FakeIam(get_role_error=_client_error("AccessDenied"))
    r = acc.describe_role_permissions(_entry())
    assert r["ok"] is True
    assert "AccessDenied" in r["policies_error"]
    assert "raw aws text" not in str(r) and "SECRET" not in str(r)
    assert r["simulation"]["available"] is True  # the write check still ran


def test_permissions_when_the_simulation_is_not_allowed(iam_world):
    iam_world.iam = FakeIam(simulate_error=_client_error("AccessDenied"))
    r = acc.describe_role_permissions(_entry())
    assert r["ok"] is True
    assert r["simulation"] == {"available": False, "reason": "AccessDenied"}
    assert "unknown" in r["summary"]
    assert "raw aws text" not in str(r)


def test_permissions_report_a_failed_assume_without_raw_text(monkeypatch):
    def deny(entry):
        raise _client_error("AccessDenied")

    monkeypatch.setattr(acc, "_assume", deny)
    r = acc.describe_role_permissions(_entry())
    assert (r["ok"], r["code"]) == (False, "access_denied")
    assert "raw aws text" not in r["message"] and "SECRET" not in r["message"]


def test_permissions_never_raise_on_unexpected_errors(monkeypatch):
    def boom(entry):
        raise RuntimeError("kaboom with SECRET")

    monkeypatch.setattr(acc, "_assume", boom)
    r = acc.describe_role_permissions(_entry())
    assert r["ok"] is False and "SECRET" not in r["message"]


def test_permissions_response_carries_no_credentials_or_external_id(iam_world):
    text = str(acc.describe_role_permissions(_entry()))
    for secret in ("fake-secret", "fake-token", "AKIAFAKE", _entry()["external_id"]):
        assert secret not in text


def test_permissions_for_a_legacy_account_use_the_portal_readonly_role(monkeypatch):
    from app import cost_explorer

    core_sts = _CoreSts()
    monkeypatch.setattr(cost_explorer, "_core_session", lambda: _CoreSession(core_sts))
    created = []
    iam = FakeIam()
    monkeypatch.setattr(acc.boto3, "client", lambda service, **kw: (created.append(service), iam)[1])
    r = acc.describe_role_permissions(_entry(identifier="100000000003", access_mode="legacy", external_id=None))
    assert r["ok"] is True
    assert r["identity"]["role_name"] == cost_explorer.READONLY_ROLE_NAME
    assert "ExternalId" not in str(core_sts.assumed)
    assert created == ["iam"]


def test_permissions_for_the_core_account_are_identity_only(monkeypatch):
    from app import cost_explorer

    core_sts = _CoreSts(account="100000000001")
    monkeypatch.setattr(cost_explorer, "_core_session", lambda: _CoreSession(core_sts))
    r = acc.describe_role_permissions(_entry(identifier="100000000001", access_mode="legacy", external_id=None))
    assert r["ok"] is True
    assert r["identity"]["kind"] == "iam_user"
    assert r["simulation"] == {"available": False, "reason": "direct_keys"}
    assert r["managed_policies"] == [] and r["inline_policies"] == []


# ── The hand-made InfraOps role next to PortalReadOnlyRole ─────────────────────

class _TaskSts:
    """STS client of the Portal task: records AssumeRole calls, can be told to fail."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Credentials": dict(CREDS)}


def test_shared_infraops_role_is_assumed_with_the_task_identity_and_described(iam_world, monkeypatch):
    sts = _TaskSts()
    monkeypatch.setattr(acc, "_sts_client", lambda: sts)
    iam_world.iam = FakeIam(allow={"ec2:RunInstances"})
    r = acc.describe_shared_infraops_role(_entry(identifier="100000000003", access_mode="legacy", external_id=None))
    call = sts.calls[0]
    assert call["RoleArn"] == "arn:aws:iam::100000000003:role/JobPortal-CrossAccount-InfraOps-1000"
    assert call["ExternalId"] == acc.SHARED_INFRAOPS_EXTERNAL_ID == "jobportal-infra-ops"
    assert r["ok"] is True and r["simulation"]["write_allowed"] == ["ec2:RunInstances"]
    assert "can change things" in r["summary"]
    assert "fake-secret" not in str(r) and "jobportal-infra-ops" not in str(r)


def test_shared_infraops_role_missing_in_the_account_is_reported_as_not_provisioned(monkeypatch):
    monkeypatch.setattr(acc, "_sts_client", lambda: _TaskSts(error=_client_error("AccessDenied")))
    r = acc.describe_shared_infraops_role(_entry(identifier="100000000004", access_mode="legacy", external_id=None))
    assert (r["ok"], r["code"]) == (False, "not_provisioned")
    assert "JobPortal-CrossAccount-InfraOps-1000" in r["message"]
    assert "SECRET" not in r["message"] and "raw aws text" not in r["message"]


def test_shared_infraops_role_other_failures_keep_their_own_code(monkeypatch):
    monkeypatch.setattr(acc, "_sts_client", lambda: _TaskSts(error=EndpointConnectionError(endpoint_url="https://sts")))
    r = acc.describe_shared_infraops_role(_entry(identifier="100000000003", access_mode="legacy", external_id=None))
    assert (r["ok"], r["code"]) == (False, "network")


def test_shared_infraops_role_probe_never_raises(monkeypatch):
    monkeypatch.setattr(acc, "_sts_client", lambda: _TaskSts(error=RuntimeError("kaboom with SECRET")))
    r = acc.describe_shared_infraops_role(_entry(identifier="100000000003", access_mode="legacy", external_id=None))
    assert r["ok"] is False and "SECRET" not in r["message"]
    r = acc.describe_shared_infraops_role({"identifier": "not-an-account"})
    assert r["ok"] is False
