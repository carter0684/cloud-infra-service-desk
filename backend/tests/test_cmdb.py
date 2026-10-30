"""
Tests for app/cmdb.py — the CMDB live-inventory fetch/cache module: tag
normalization, per-service resource listers, region discovery, per-
account/per-service error isolation, org-wide aggregation, the ENV=local
mock inventory, the 10-minute cache (TTL/force-refresh/last-known-good
fallback), and the Bedrock-backed AI impact analysis.

None of these tests make real AWS or Bedrock network calls: every boto3
client is a small fake object built for this file (mirroring
test_billing_by_service.py's FakeCeClient approach), and
ai_engine._get_client()/_invoke() are monkeypatched exactly like
test_ai_engine_cost_summary.py does for analyse_cost_summary().
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app import ai_engine
from app import cmdb as cmdb_module


@pytest.fixture(autouse=True)
def _reset_cmdb_cache():
    """Autouse: every test in this file starts and ends with a clean
    cmdb.py process-local cache, so no test can observe another test's
    cached inventory or single-flight state (they run in the same
    process/module, like cost_explorer's equivalent conftest fixture)."""
    cmdb_module.reset_cache_for_tests()
    yield
    cmdb_module.reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _run_background_fetch_synchronously(monkeypatch):
    """get_cmdb_inventory() now kicks off the real org-wide fetch on a
    background thread (see cmdb.py's module docstring on why — a fetch
    that can take up to ~15 minutes must never block the calling/HTTP
    thread, or CPU on the shared Fargate task). Tests need the fetch to
    have actually completed by the time get_cmdb_inventory() returns so
    assertions on its result are deterministic, so this monkeypatches
    the one seam (_start_background_thread) to run the target function
    inline instead of on a real separate thread — everything else about
    the single-flight/caching logic under test is unaffected."""
    monkeypatch.setattr(cmdb_module, "_start_background_thread", lambda target: target())


@pytest.fixture(autouse=True)
def _stub_cmdb_store_get_all_snapshots_by_default(monkeypatch):
    """get_cmdb_inventory() now calls cmdb_store.get_all_snapshots() (via
    _hydrate_cache_from_store()) on every call where the in-process cache
    is empty. cmdb_store.py's own ENV constant is evaluated once at
    *import time* from the real os.environ (not from cmdb_module.ENV,
    which individual tests monkeypatch per-test) — so without this
    default stub, every test that doesn't explicitly care about
    hydration would still fall through to cmdb_store's real ENV=local
    SQLite branch and read whatever happens to be sitting in the actual
    local dev job_portal.db file on disk, silently leaking real
    developer-machine data into unrelated test assertions. Default to
    "nothing in the shared store" here; tests that specifically exercise
    hydration override this via their own monkeypatch.setattr call."""
    monkeypatch.setattr("app.cmdb_store.get_all_snapshots", lambda: {})


# ── Fake boto3 clients (no real AWS/network calls) ────────────────────────

class FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return iter(self._pages)


class FakeEc2:
    def __init__(self, pages=None, regions=None, raise_on_describe_regions=False):
        self._pages = pages or []
        self._regions = regions or []
        self._raise_on_describe_regions = raise_on_describe_regions

    def get_paginator(self, name):
        assert name == "describe_instances"
        return FakePaginator(self._pages)

    def describe_regions(self, AllRegions=False):
        if self._raise_on_describe_regions:
            raise RuntimeError("describe_regions failed")
        return {"Regions": [{"RegionName": r} for r in self._regions]}


class FakeRds:
    def __init__(self, pages):
        self._pages = pages

    def get_paginator(self, name):
        assert name == "describe_db_instances"
        return FakePaginator(self._pages)


class FakeLambda:
    def __init__(self, pages):
        self._pages = pages

    def get_paginator(self, name):
        assert name == "list_functions"
        return FakePaginator(self._pages)


class FakeS3:
    def __init__(self, buckets):
        self._buckets = buckets

    def list_buckets(self):
        return {"Buckets": self._buckets}


class FakeDdb:
    def __init__(self, table_pages, describe_map=None):
        self._table_pages = table_pages
        self._describe_map = describe_map or {}

    def get_paginator(self, name):
        assert name == "list_tables"
        return FakePaginator(self._table_pages)

    def describe_table(self, TableName):
        if TableName not in self._describe_map:
            raise RuntimeError(f"no such table {TableName}")
        return {"Table": self._describe_map[TableName]}


class FailingDescribeDdb(FakeDdb):
    def describe_table(self, TableName):
        raise RuntimeError("describe_table failed")


class FakeElbv2:
    def __init__(self, pages):
        self._pages = pages

    def get_paginator(self, name):
        assert name == "describe_load_balancers"
        return FakePaginator(self._pages)


# ── _tags_to_dict ──────────────────────────────────────────────────────────

def test_tags_to_dict_handles_key_value_list():
    assert cmdb_module._tags_to_dict([{"Key": "Name", "Value": "web-1"}, {"Key": "env", "Value": "prod"}]) == {
        "Name": "web-1", "env": "prod",
    }


def test_tags_to_dict_handles_plain_dict_passthrough():
    assert cmdb_module._tags_to_dict({"a": "1"}) == {"a": "1"}


def test_tags_to_dict_handles_none_and_empty_list():
    assert cmdb_module._tags_to_dict(None) == {}
    assert cmdb_module._tags_to_dict([]) == {}


def test_tags_to_dict_ignores_entries_without_a_key():
    assert cmdb_module._tags_to_dict([{"Value": "orphan"}, {"Key": "ok", "Value": "1"}]) == {"ok": "1"}


# ── _norm ──────────────────────────────────────────────────────────────────

def test_norm_falls_back_to_resource_id_when_name_missing():
    row = cmdb_module._norm("EC2 Instance", "i-1", None, "ap-southeast-1", "running", {})
    assert row["name"] == "i-1"


def test_norm_omits_extra_key_when_not_provided():
    row = cmdb_module._norm("EC2 Instance", "i-1", "n", "ap-southeast-1", "running", {})
    assert "extra" not in row


def test_norm_includes_extra_when_provided():
    row = cmdb_module._norm("EC2 Instance", "i-1", "n", "ap-southeast-1", "running", {}, extra={"a": 1})
    assert row["extra"] == {"a": 1}


def test_norm_defaults_tags_to_empty_dict_when_falsy():
    row = cmdb_module._norm("EC2 Instance", "i-1", "n", "ap-southeast-1", "running", None)
    assert row["tags"] == {}


# ── Per-service listers ────────────────────────────────────────────────────

def test_list_ec2_instances_normalizes_rows():
    pages = [{"Reservations": [{"Instances": [
        {
            "InstanceId": "i-1", "State": {"Name": "running"},
            "Tags": [{"Key": "Name", "Value": "web-1"}],
            "InstanceType": "t3.micro", "PrivateIpAddress": "10.0.0.1",
            "PlatformDetails": "Linux/UNIX",
        },
    ]}]}]
    rows = cmdb_module._list_ec2_instances(FakeEc2(pages=pages), "ap-southeast-1")
    assert len(rows) == 1
    r = rows[0]
    assert r["resource_type"] == cmdb_module.RESOURCE_TYPE_EC2
    assert r["resource_id"] == "i-1"
    assert r["name"] == "web-1"
    assert r["region"] == "ap-southeast-1"
    assert r["status"] == "running"
    assert r["tags"] == {"Name": "web-1"}
    assert r["extra"] == {"instance_type": "t3.micro", "private_ip": "10.0.0.1", "platform": "Linux/UNIX"}


def test_list_ec2_instances_handles_multiple_reservations_and_pages():
    pages = [
        {"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "running"}}]}]},
        {"Reservations": [{"Instances": [{"InstanceId": "i-2", "State": {"Name": "stopped"}}]}]},
    ]
    rows = cmdb_module._list_ec2_instances(FakeEc2(pages=pages), "us-east-1")
    assert {r["resource_id"] for r in rows} == {"i-1", "i-2"}


def test_list_rds_instances_normalizes_rows():
    pages = [{"DBInstances": [{
        "DBInstanceIdentifier": "db-1", "DBInstanceStatus": "available",
        "Engine": "postgres", "DBInstanceClass": "db.t3.micro", "MultiAZ": True,
        "TagList": [{"Key": "environment", "Value": "prod"}],
    }]}]
    rows = cmdb_module._list_rds_instances(FakeRds(pages), "ap-southeast-1")
    assert len(rows) == 1
    r = rows[0]
    assert r["resource_type"] == cmdb_module.RESOURCE_TYPE_RDS
    assert r["resource_id"] == "db-1"
    assert r["name"] == "db-1"
    assert r["status"] == "available"
    assert r["tags"] == {"environment": "prod"}
    assert r["extra"] == {"engine": "postgres", "instance_class": "db.t3.micro", "multi_az": True}


def test_list_lambda_functions_normalizes_rows_with_no_tags():
    pages = [{"Functions": [{
        "FunctionName": "fn-1", "State": "Active", "Runtime": "python3.13",
        "MemorySize": 256, "LastModified": "2026-01-01T00:00:00Z",
    }]}]
    rows = cmdb_module._list_lambda_functions(FakeLambda(pages), "us-east-1")
    assert len(rows) == 1
    r = rows[0]
    assert r["resource_type"] == cmdb_module.RESOURCE_TYPE_LAMBDA
    assert r["resource_id"] == "fn-1"
    assert r["status"] == "Active"
    assert r["tags"] == {}
    assert r["extra"] == {"runtime": "python3.13", "memory_mb": 256, "last_modified": "2026-01-01T00:00:00Z"}


def test_list_lambda_functions_defaults_state_to_active_when_missing():
    rows = cmdb_module._list_lambda_functions(FakeLambda([{"Functions": [{"FunctionName": "fn-1"}]}]), "us-east-1")
    assert rows[0]["status"] == "Active"


def test_list_s3_buckets_normalizes_rows_and_serializes_creation_date():
    buckets = [{"Name": "my-bucket", "CreationDate": datetime(2026, 1, 1, 12, 0, 0)}]
    rows = cmdb_module._list_s3_buckets(FakeS3(buckets), "global")
    assert len(rows) == 1
    r = rows[0]
    assert r["resource_type"] == cmdb_module.RESOURCE_TYPE_S3
    assert r["resource_id"] == "my-bucket"
    assert r["region"] == "global"
    assert r["status"] == "active"
    assert r["extra"]["created"] == "2026-01-01T12:00:00"


def test_list_s3_buckets_handles_missing_creation_date():
    rows = cmdb_module._list_s3_buckets(FakeS3([{"Name": "bkt"}]), "global")
    assert rows[0]["extra"]["created"] is None


def test_list_dynamodb_tables_normalizes_rows_with_status_and_item_count():
    ddb = FakeDdb([{"TableNames": ["t1", "t2"]}], {
        "t1": {"TableStatus": "ACTIVE", "ItemCount": 42},
        "t2": {"TableStatus": "CREATING", "ItemCount": 0},
    })
    rows = cmdb_module._list_dynamodb_tables(ddb, "us-east-1")
    by_id = {r["resource_id"]: r for r in rows}
    assert by_id["t1"]["status"] == "ACTIVE"
    assert by_id["t1"]["extra"]["item_count"] == 42
    assert by_id["t2"]["status"] == "CREATING"


def test_list_dynamodb_tables_handles_describe_table_failure_gracefully():
    ddb = FailingDescribeDdb([{"TableNames": ["t1"]}])
    rows = cmdb_module._list_dynamodb_tables(ddb, "us-east-1")
    assert len(rows) == 1
    assert rows[0]["status"] is None
    assert rows[0]["extra"]["item_count"] is None


def test_list_dynamodb_tables_paginates_table_names():
    pages = [{"TableNames": ["t1"]}, {"TableNames": ["t2"]}]
    ddb = FakeDdb(pages, {"t1": {"TableStatus": "ACTIVE"}, "t2": {"TableStatus": "ACTIVE"}})
    rows = cmdb_module._list_dynamodb_tables(ddb, "us-east-1")
    assert {r["resource_id"] for r in rows} == {"t1", "t2"}


def test_list_load_balancers_normalizes_rows():
    pages = [{"LoadBalancers": [{
        "LoadBalancerName": "lb-1", "State": {"Code": "active"},
        "Type": "application", "Scheme": "internal",
    }]}]
    rows = cmdb_module._list_load_balancers(FakeElbv2(pages), "ap-southeast-1")
    assert len(rows) == 1
    r = rows[0]
    assert r["resource_type"] == cmdb_module.RESOURCE_TYPE_ELB
    assert r["resource_id"] == "lb-1"
    assert r["status"] == "active"
    assert r["extra"] == {"type": "application", "scheme": "internal"}


# ── _regions_for_account ───────────────────────────────────────────────────

def test_regions_for_account_returns_regions_from_describe_regions(monkeypatch):
    fake_ec2 = FakeEc2(regions=["ap-southeast-1", "us-east-1"])
    monkeypatch.setattr(cmdb_module, "_client_for_account", lambda *a, **k: fake_ec2)
    regions = cmdb_module._regions_for_account("111111111111", object())
    assert regions == ["ap-southeast-1", "us-east-1"]


def test_regions_for_account_falls_back_on_describe_regions_failure(monkeypatch):
    fake_ec2 = FakeEc2(raise_on_describe_regions=True)
    monkeypatch.setattr(cmdb_module, "_client_for_account", lambda *a, **k: fake_ec2)
    regions = cmdb_module._regions_for_account("111111111111", object())
    assert regions == cmdb_module._FALLBACK_REGIONS


def test_regions_for_account_falls_back_when_response_is_empty(monkeypatch):
    fake_ec2 = FakeEc2(regions=[])
    monkeypatch.setattr(cmdb_module, "_client_for_account", lambda *a, **k: fake_ec2)
    regions = cmdb_module._regions_for_account("111111111111", object())
    assert regions == cmdb_module._FALLBACK_REGIONS


def test_regions_for_account_falls_back_when_client_construction_fails(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("cannot build client")

    monkeypatch.setattr(cmdb_module, "_client_for_account", _raise)
    regions = cmdb_module._regions_for_account("111111111111", object())
    assert regions == cmdb_module._FALLBACK_REGIONS


# ── _regional_client_for_account ───────────────────────────────────────────

def test_regional_client_for_account_core_uses_core_session_directly():
    calls = []

    class FakeCoreSession:
        def client(self, service, region_name=None):
            calls.append((service, region_name))
            return "fake-core-client"

    result = cmdb_module._regional_client_for_account(
        cmdb_module.CORE_ACCOUNT_ID, "ec2", "ap-southeast-1", FakeCoreSession(),
    )
    assert result == "fake-core-client"
    assert calls == [("ec2", "ap-southeast-1")]


def test_regional_client_for_account_other_account_assumes_role_then_builds_client(monkeypatch):
    class FakeSts:
        def assume_role(self, RoleArn, RoleSessionName):
            assert RoleArn == f"arn:aws:iam::111111111111:role/{cmdb_module.READONLY_ROLE_NAME}"
            assert RoleSessionName == "job-portal-cmdb"
            return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST"}}

    class FakeCoreSession:
        def client(self, service, region_name=None):
            assert service == "sts"
            assert region_name == "us-east-1"
            return FakeSts()

    captured = {}

    def fake_boto3_client(service, region_name=None, aws_access_key_id=None, aws_secret_access_key=None, aws_session_token=None):
        captured.update(
            service=service, region_name=region_name,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
        )
        return "fake-assumed-client"

    monkeypatch.setattr(cmdb_module.boto3, "client", fake_boto3_client)
    result = cmdb_module._regional_client_for_account("111111111111", "rds", "us-east-1", FakeCoreSession())

    assert result == "fake-assumed-client"
    assert captured["service"] == "rds"
    assert captured["region_name"] == "us-east-1"
    assert captured["aws_access_key_id"] == "AK"
    assert captured["aws_secret_access_key"] == "SK"
    assert captured["aws_session_token"] == "ST"


# ── _fetch_account_inventory: per-service error isolation ─────────────────

def _regional_client_router(mapping):
    def _router(account_id, service, region, core_session):
        value = mapping[service]
        if isinstance(value, Exception):
            raise value
        return value
    return _router


def test_fetch_account_inventory_isolates_per_service_errors(monkeypatch):
    monkeypatch.setattr(cmdb_module, "_regions_for_account", lambda *a, **k: ["ap-southeast-1"])
    monkeypatch.setattr(cmdb_module, "_regional_client_for_account", _regional_client_router({
        "s3": FakeS3([{"Name": "bucket-1"}]),
        "ec2": RuntimeError("ec2 boom"),
        "rds": FakeRds([{"DBInstances": [{"DBInstanceIdentifier": "db-1", "DBInstanceStatus": "available"}]}]),
        "lambda": FakeLambda([{"Functions": []}]),
        "dynamodb": FakeDdb([{"TableNames": []}]),
        "elbv2": FakeElbv2([{"LoadBalancers": []}]),
    }))

    row = cmdb_module._fetch_account_inventory("111111111111", "TestAcct", object())

    assert row["error"] is None
    types = {r["resource_type"] for r in row["resources"]}
    assert cmdb_module.RESOURCE_TYPE_S3 in types
    assert cmdb_module.RESOURCE_TYPE_RDS in types
    assert cmdb_module.RESOURCE_TYPE_EC2 not in types  # ec2 failed for this region, isolated
    assert row["resource_counts"][cmdb_module.RESOURCE_TYPE_RDS] == 1


def test_fetch_account_inventory_s3_failure_does_not_block_regional_services(monkeypatch):
    monkeypatch.setattr(cmdb_module, "_regions_for_account", lambda *a, **k: ["ap-southeast-1"])
    monkeypatch.setattr(cmdb_module, "_regional_client_for_account", _regional_client_router({
        "s3": RuntimeError("s3 boom"),
        "ec2": FakeEc2(pages=[{"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "running"}}]}]}]),
        "rds": FakeRds([{"DBInstances": []}]),
        "lambda": FakeLambda([{"Functions": []}]),
        "dynamodb": FakeDdb([{"TableNames": []}]),
        "elbv2": FakeElbv2([{"LoadBalancers": []}]),
    }))

    row = cmdb_module._fetch_account_inventory("111111111111", "TestAcct", object())

    assert row["error"] is None
    types = {r["resource_type"] for r in row["resources"]}
    assert cmdb_module.RESOURCE_TYPE_S3 not in types
    assert cmdb_module.RESOURCE_TYPE_EC2 in types


def test_fetch_account_inventory_uses_provided_regions_bypassing_region_lookup(monkeypatch):
    def _raise(*a, **k):
        raise AssertionError("should not be called when regions is provided explicitly")

    monkeypatch.setattr(cmdb_module, "_regions_for_account", _raise)
    monkeypatch.setattr(cmdb_module, "_regional_client_for_account", _regional_client_router({
        "s3": FakeS3([]),
        "ec2": FakeEc2(pages=[{"Reservations": []}]),
        "rds": FakeRds([{"DBInstances": []}]),
        "lambda": FakeLambda([{"Functions": []}]),
        "dynamodb": FakeDdb([{"TableNames": []}]),
        "elbv2": FakeElbv2([{"LoadBalancers": []}]),
    }))

    row = cmdb_module._fetch_account_inventory("111111111111", "TestAcct", object(), regions=["us-east-1"])
    assert row["error"] is None
    assert row["resources"] == []


def test_fetch_account_inventory_returns_error_row_on_unexpected_failure(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("catastrophic failure")

    monkeypatch.setattr(cmdb_module, "_regions_for_account", _raise)
    row = cmdb_module._fetch_account_inventory("111111111111", "TestAcct", object())
    assert row["error"] == "Could not fetch CMDB inventory for this account"
    assert row["resources"] == []
    assert row["resource_counts"] == {}


# ── _build_inventory ───────────────────────────────────────────────────────

def test_build_inventory_aggregates_counts_totals_and_errors():
    accounts_result = [
        {
            "account_id": "1", "account_name": "A",
            "resources": [cmdb_module._norm(cmdb_module.RESOURCE_TYPE_EC2, "i-1", "n", "r", "running", {})],
            "resource_counts": {cmdb_module.RESOURCE_TYPE_EC2: 1},
            "error": None,
        },
        {"account_id": "2", "account_name": "B", "resources": [], "resource_counts": {}, "error": "boom"},
    ]
    inv = cmdb_module._build_inventory(accounts_result)
    assert inv["account_count"] == 2
    assert inv["error_count"] == 1
    assert inv["total_resources"] == 1
    assert inv["resource_type_totals"] == {cmdb_module.RESOURCE_TYPE_EC2: 1}
    assert inv["is_mock"] is False
    assert inv["cache_age_seconds"] == 0
    assert "as_of" in inv


def test_build_inventory_handles_empty_accounts_list():
    inv = cmdb_module._build_inventory([])
    assert inv["account_count"] == 0
    assert inv["error_count"] == 0
    assert inv["total_resources"] == 0
    assert inv["resource_type_totals"] == {}


# ── _mock_inventory (ENV=local) ────────────────────────────────────────────

_ALL_RESOURCE_TYPES = {
    cmdb_module.RESOURCE_TYPE_EC2, cmdb_module.RESOURCE_TYPE_RDS,
    cmdb_module.RESOURCE_TYPE_LAMBDA, cmdb_module.RESOURCE_TYPE_S3,
    cmdb_module.RESOURCE_TYPE_DYNAMODB, cmdb_module.RESOURCE_TYPE_ELB,
}


def test_mock_inventory_shape():
    inv = cmdb_module._mock_inventory()
    assert inv["is_mock"] is True
    assert inv["account_count"] == 5
    assert inv["total_resources"] > 0
    assert inv["error_count"] == 0
    for account in inv["accounts"]:
        assert account["error"] is None
        for r in account["resources"]:
            assert r["resource_type"] in _ALL_RESOURCE_TYPES


def test_mock_inventory_is_deterministic():
    inv1 = cmdb_module._mock_inventory()
    inv2 = cmdb_module._mock_inventory()
    assert inv1["accounts"] == inv2["accounts"]
    assert inv1["total_resources"] == inv2["total_resources"]


def test_mock_inventory_covers_all_resource_types():
    inv = cmdb_module._mock_inventory()
    all_types = set()
    for account in inv["accounts"]:
        all_types.update(account["resource_counts"].keys())
    assert cmdb_module.RESOURCE_TYPE_EC2 in all_types
    assert cmdb_module.RESOURCE_TYPE_LAMBDA in all_types
    assert cmdb_module.RESOURCE_TYPE_S3 in all_types


# ── get_cmdb_inventory: ENV=local path ──────────────────────────────────────

def test_get_cmdb_inventory_local_returns_mock(monkeypatch):
    monkeypatch.setattr(cmdb_module, "ENV", "local")
    monkeypatch.delenv("CORE_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("CORE_SECRET_ACCESS_KEY", raising=False)
    inv = cmdb_module.get_cmdb_inventory()
    assert inv["is_mock"] is True
    assert inv["fetch_status"] == "ready"


def test_should_use_mock_inventory_true_when_local_and_no_core_env_vars(monkeypatch):
    monkeypatch.setattr(cmdb_module, "ENV", "local")
    monkeypatch.delenv("CORE_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("CORE_SECRET_ACCESS_KEY", raising=False)
    assert cmdb_module._should_use_mock_inventory() is True


def test_should_use_mock_inventory_false_when_local_but_core_env_vars_set(monkeypatch):
    """A developer can opt a local run into real cross-account CMDB data
    by exporting CORE_ACCESS_KEY_ID/CORE_SECRET_ACCESS_KEY before
    starting the backend — without touching the global ENV flag, so every
    other local-mode feature (Jobs/Freshservice/AWS Support mocks) stays
    unaffected."""
    monkeypatch.setattr(cmdb_module, "ENV", "local")
    monkeypatch.setenv("CORE_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.setenv("CORE_SECRET_ACCESS_KEY", "fakesecret")
    assert cmdb_module._should_use_mock_inventory() is False


def test_should_use_mock_inventory_false_when_env_is_not_local(monkeypatch):
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.delenv("CORE_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("CORE_SECRET_ACCESS_KEY", raising=False)
    assert cmdb_module._should_use_mock_inventory() is False


def test_should_use_mock_inventory_true_when_only_one_core_var_set(monkeypatch):
    """Both env vars must be present — a partially-configured shell must
    still fall back to mock data rather than attempting a real fetch with
    missing credentials."""
    monkeypatch.setattr(cmdb_module, "ENV", "local")
    monkeypatch.setenv("CORE_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.delenv("CORE_SECRET_ACCESS_KEY", raising=False)
    assert cmdb_module._should_use_mock_inventory() is True


def test_get_cmdb_inventory_local_with_core_env_vars_fetches_real_path(monkeypatch):
    """When CORE_ACCESS_KEY_ID/SECRET are set, get_cmdb_inventory()
    must take the real cross-account fetch path (via _fetch_all_accounts),
    not _mock_inventory() — even though ENV is still 'local'."""
    monkeypatch.setattr(cmdb_module, "ENV", "local")
    monkeypatch.setenv("CORE_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.setenv("CORE_SECRET_ACCESS_KEY", "fakesecret")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())
    monkeypatch.setattr("app.cmdb_store.diff_and_record", lambda *a, **k: [])

    calls = {"n": 0}

    def fake_fetch_all(core_session):
        calls["n"] += 1
        return [_account_row(account_id="111111111111", account_name="RealAcct")]

    monkeypatch.setattr(cmdb_module, "_fetch_all_accounts", fake_fetch_all)

    inv = cmdb_module.get_cmdb_inventory()
    assert calls["n"] == 1
    assert inv["is_mock"] is False
    assert inv["account_count"] == 1


# ── get_cmdb_inventory: production caching / refresh / fallback ──────────

def _account_row(account_id="1", account_name="A", resources=None, error=None):
    resources = resources or []
    counts: dict = {}
    for r in resources:
        counts[r["resource_type"]] = counts.get(r["resource_type"], 0) + 1
    return {"account_id": account_id, "account_name": account_name, "resources": resources, "resource_counts": counts, "error": error}


def test_get_cmdb_inventory_hydrates_empty_cache_from_shared_store(monkeypatch):
    """Reproduces the "History drawer shows data but CMDB page shows 0
    accounts" bug: with 2+ ECS tasks behind an ALB with no session
    stickiness, a request can land on a task whose in-process cache has
    never been populated even though another task (or a previous
    deployment) already wrote a full inventory to the shared DynamoDB/
    SQLite snapshot store. get_cmdb_inventory() must hydrate its own
    empty cache from that shared store before falling back to "pending",
    and must NOT kick off a redundant background fetch once hydrated
    (the hydrated data counts as a fresh cache hit)."""
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())

    stored_resources = {
        "111111111111": [cmdb_module._norm(cmdb_module.RESOURCE_TYPE_EC2, "i-1", "n", "ap-southeast-1", "running", {})],
        "222222222222": [cmdb_module._norm(cmdb_module.RESOURCE_TYPE_S3, "b-1", "b-1", "global", "active", {})],
    }
    monkeypatch.setattr("app.cmdb_store.get_all_snapshots", lambda: stored_resources)

    fetch_calls = {"n": 0}

    def fake_fetch_all(core_session):
        fetch_calls["n"] += 1
        return []

    monkeypatch.setattr(cmdb_module, "_fetch_all_accounts", fake_fetch_all)

    inv = cmdb_module.get_cmdb_inventory()

    assert fetch_calls["n"] == 0  # hydrated cache counts as fresh, no redundant fetch kicked off
    assert inv["fetch_status"] == "ready"
    assert inv["account_count"] == 2
    assert inv["total_resources"] == 2
    assert inv.get("hydrated_from_store") is True


def test_get_cmdb_inventory_hydration_noop_when_store_also_empty(monkeypatch):
    """If the shared store has nothing either (e.g. the very first fetch
    ever, org-wide), hydration is a no-op and the normal "pending" +
    kick-off-a-fetch behavior applies unchanged."""
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())
    monkeypatch.setattr("app.cmdb_store.get_all_snapshots", lambda: {})
    monkeypatch.setattr(cmdb_module, "_fetch_all_accounts", lambda core_session: [_account_row()])
    monkeypatch.setattr("app.cmdb_store.diff_and_record", lambda *a, **k: [])

    inv = cmdb_module.get_cmdb_inventory()
    assert inv["fetch_status"] == "ready"  # the autouse fixture runs the fetch synchronously
    assert inv["account_count"] == 1


def test_get_cmdb_inventory_hydration_survives_store_error(monkeypatch):
    """A broken snapshot store (e.g. DynamoDB throttled/unreachable) must
    never break the read path — hydration failure is swallowed and the
    normal pending/fetch-kickoff flow proceeds."""
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())

    def _raise():
        raise RuntimeError("dynamodb unreachable")

    monkeypatch.setattr("app.cmdb_store.get_all_snapshots", _raise)
    monkeypatch.setattr(cmdb_module, "_fetch_all_accounts", lambda core_session: [_account_row()])
    monkeypatch.setattr("app.cmdb_store.diff_and_record", lambda *a, **k: [])

    inv = cmdb_module.get_cmdb_inventory()
    assert inv["fetch_status"] == "ready"
    assert inv["account_count"] == 1


def test_get_cmdb_inventory_caches_within_ttl(monkeypatch):
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())
    monkeypatch.setattr("app.cmdb_store.diff_and_record", lambda *a, **k: [])

    call_count = {"n": 0}

    def fake_fetch_all(core_session):
        call_count["n"] += 1
        return [_account_row()]

    monkeypatch.setattr(cmdb_module, "_fetch_all_accounts", fake_fetch_all)

    first = cmdb_module.get_cmdb_inventory()
    second = cmdb_module.get_cmdb_inventory()
    assert call_count["n"] == 1
    assert second["account_count"] == first["account_count"]
    assert second["cache_age_seconds"] >= 0


def test_get_cmdb_inventory_force_refresh_bypasses_cache(monkeypatch):
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())
    monkeypatch.setattr("app.cmdb_store.diff_and_record", lambda *a, **k: [])

    call_count = {"n": 0}

    def fake_fetch_all(core_session):
        call_count["n"] += 1
        return [_account_row()]

    monkeypatch.setattr(cmdb_module, "_fetch_all_accounts", fake_fetch_all)

    cmdb_module.get_cmdb_inventory()
    cmdb_module.get_cmdb_inventory(force_refresh=True)
    assert call_count["n"] == 2


def test_get_cmdb_inventory_falls_back_to_last_known_good_when_all_accounts_fail(monkeypatch):
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())
    monkeypatch.setattr("app.cmdb_store.diff_and_record", lambda *a, **k: [])

    good_row = _account_row(resources=[cmdb_module._norm(cmdb_module.RESOURCE_TYPE_EC2, "i-1", "n", "r", "running", {})])
    bad_row = _account_row(error="boom")

    state = {"n": 0}

    def fake_fetch_all(core_session):
        state["n"] += 1
        return [good_row] if state["n"] == 1 else [bad_row]

    monkeypatch.setattr(cmdb_module, "_fetch_all_accounts", fake_fetch_all)

    first = cmdb_module.get_cmdb_inventory()
    assert first["error_count"] == 0
    assert first["total_resources"] == 1

    second = cmdb_module.get_cmdb_inventory(force_refresh=True)
    assert second.get("stale_fallback") is True
    assert second["total_resources"] == 1  # served from last known-good, not the failed empty fetch


def test_get_cmdb_inventory_returns_fresh_failed_result_when_no_last_known_good(monkeypatch):
    """If every account fails on the very first fetch (no last-known-good
    to fall back to yet), the failed result itself must still be
    returned rather than raising or hanging."""
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())
    monkeypatch.setattr("app.cmdb_store.diff_and_record", lambda *a, **k: [])
    monkeypatch.setattr(cmdb_module, "_fetch_all_accounts", lambda core_session: [_account_row(error="boom")])

    inv = cmdb_module.get_cmdb_inventory()
    assert inv["error_count"] == 1
    assert not inv.get("stale_fallback")


def test_get_cmdb_inventory_history_failure_does_not_break_read_path(monkeypatch):
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())

    def _raise(*a, **k):
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr("app.cmdb_store.diff_and_record", _raise)
    monkeypatch.setattr(cmdb_module, "_fetch_all_accounts", lambda core_session: [_account_row()])

    inv = cmdb_module.get_cmdb_inventory()
    assert inv["account_count"] == 1
    assert inv["error_count"] == 0


def test_get_cmdb_inventory_skips_history_recording_for_errored_accounts(monkeypatch):
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())

    recorded = []
    monkeypatch.setattr("app.cmdb_store.diff_and_record", lambda account_id, resources: recorded.append(account_id))
    monkeypatch.setattr(cmdb_module, "_fetch_all_accounts", lambda core_session: [
        _account_row(account_id="1"), _account_row(account_id="2", error="boom"),
    ])

    cmdb_module.get_cmdb_inventory()
    assert recorded == ["1"]


# ── analyse_resource_impact (Bedrock, mocked) ─────────────────────────────

def _resource(**overrides) -> dict:
    base = {
        "resource_type": cmdb_module.RESOURCE_TYPE_RDS,
        "resource_id": "db-prod-1",
        "name": "db-prod-1",
        "region": "ap-southeast-1",
        "status": "available",
        "tags": {"environment": "prod"},
        "extra": {"engine": "postgres"},
    }
    base.update(overrides)
    return base


def test_analyse_resource_impact_happy_path(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())

    def fake_invoke(client, model_id, prompt, max_tokens=800):
        assert model_id == ai_engine.MODEL_ANALYSIS
        return json.dumps({
            "impact_summary": "Production DB down.",
            "severity": "critical",
            "likely_affected": ["App A", "App B", "App C", "App D", "App E", "extra ignored"],
            "recommendations": ["Check replica", "Verify backups", "Alert on-call", "Review DR plan", "extra ignored"],
            "bottom_line": "Escalate immediately.",
        })

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = cmdb_module.analyse_resource_impact(_resource(), "111111111111", "TestAcct")

    assert result["model_used"] == ai_engine.MODEL_ANALYSIS
    assert result["severity"] == "critical"
    assert result["impact_summary"] == "Production DB down."
    assert len(result["likely_affected"]) == 5     # capped at 5
    assert len(result["recommendations"]) == 4     # capped at 4
    assert result["bottom_line"] == "Escalate immediately."
    assert result["bedrock_region"] == ai_engine.BEDROCK_REGION
    assert "generated_at" in result


def test_analyse_resource_impact_invalid_severity_defaults_to_medium(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())

    def fake_invoke(client, model_id, prompt, max_tokens=800):
        return json.dumps({"impact_summary": "x", "severity": "catastrophic", "likely_affected": [], "recommendations": [], "bottom_line": "y"})

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = cmdb_module.analyse_resource_impact(_resource(), "1", "A")
    assert result["severity"] == "medium"


def test_analyse_resource_impact_falls_back_on_invocation_error(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())
    calls = []

    def fake_invoke(client, model_id, prompt, max_tokens=800):
        calls.append(model_id)
        if len(calls) == 1:  # first attempt fails; the retry (same model) succeeds
            raise RuntimeError("bedrock down")
        return json.dumps({"impact_summary": "Fallback worked.", "severity": "low", "likely_affected": [], "recommendations": [], "bottom_line": "ok"})

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = cmdb_module.analyse_resource_impact(_resource(), "1", "A")

    assert calls == [ai_engine.MODEL_ANALYSIS, ai_engine.MODEL_ANALYSIS_FALLBACK]
    assert result["model_used"] == ai_engine.MODEL_ANALYSIS_FALLBACK
    assert result["impact_summary"] == "Fallback worked."


def test_analyse_resource_impact_falls_back_on_unparseable_response(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())
    calls = []

    def fake_invoke(client, model_id, prompt, max_tokens=800):
        calls.append(model_id)
        if len(calls) == 1:  # first attempt fails; the retry (same model) succeeds
            return "I cannot help with that."
        return json.dumps({"impact_summary": "Fallback worked.", "severity": "low", "likely_affected": [], "recommendations": [], "bottom_line": "ok"})

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = cmdb_module.analyse_resource_impact(_resource(), "1", "A")

    assert calls == [ai_engine.MODEL_ANALYSIS, ai_engine.MODEL_ANALYSIS_FALLBACK]
    assert result["model_used"] == ai_engine.MODEL_ANALYSIS_FALLBACK


def test_analyse_resource_impact_falls_back_on_missing_impact_summary(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())
    calls = []

    def fake_invoke(client, model_id, prompt, max_tokens=800):
        calls.append(model_id)
        if len(calls) == 1:  # first attempt fails; the retry (same model) succeeds
            return json.dumps({"severity": "low", "likely_affected": [], "recommendations": [], "bottom_line": ""})
        return json.dumps({"impact_summary": "Fallback worked.", "severity": "low", "likely_affected": [], "recommendations": [], "bottom_line": ""})

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = cmdb_module.analyse_resource_impact(_resource(), "1", "A")

    assert calls == [ai_engine.MODEL_ANALYSIS, ai_engine.MODEL_ANALYSIS_FALLBACK]
    assert result["model_used"] == ai_engine.MODEL_ANALYSIS_FALLBACK


def test_analyse_resource_impact_static_fallback_when_both_models_fail(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())

    def fake_invoke(client, model_id, prompt, max_tokens=800):
        raise RuntimeError("bedrock completely down")

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = cmdb_module.analyse_resource_impact(_resource(), "1", "A")

    assert result["model_used"] == "fallback-static"
    assert result["severity"] == "medium"
    assert "temporarily unavailable" in result["impact_summary"]
    assert result["likely_affected"] == []
    assert len(result["recommendations"]) == 2


def test_analyse_resource_impact_static_fallback_when_response_never_valid(monkeypatch):
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())

    def fake_invoke(client, model_id, prompt, max_tokens=800):
        return "not json, ever"

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    result = cmdb_module.analyse_resource_impact(_resource(), "1", "A")

    assert result["model_used"] == "fallback-static"
    assert "unassessed" in result["bottom_line"]


def test_analyse_resource_impact_works_with_untagged_resource(monkeypatch):
    """A resource with no tags/extra metadata (the common case for newly
    discovered infra) must still produce a usable prompt/response, not
    crash on empty dicts."""
    monkeypatch.setattr(ai_engine, "_get_client", lambda: object())

    def fake_invoke(client, model_id, prompt, max_tokens=800):
        assert "(no tags set)" in prompt
        assert "(none)" in prompt
        return json.dumps({"impact_summary": "Unknown impact.", "severity": "medium", "likely_affected": [], "recommendations": [], "bottom_line": "Review manually."})

    monkeypatch.setattr(ai_engine, "_invoke", fake_invoke)
    bare_resource = {
        "resource_type": cmdb_module.RESOURCE_TYPE_S3, "resource_id": "bkt-1",
        "name": "bkt-1", "region": "global", "status": "active", "tags": {},
    }
    result = cmdb_module.analyse_resource_impact(bare_resource, "1", "A")
    assert result["impact_summary"] == "Unknown impact."


# ── EC2 Topology (Illumio-style diagram) ───────────────────────────────────

class FakeEc2Topology:
    """Fake EC2 client covering describe_instances, describe_security_groups,
    describe_flow_logs, and describe_network_interfaces — the four EC2
    calls the topology functions make (the last one for the mandatory
    inbound-peer "trigger" classification / resolve logic)."""

    def __init__(self, instance=None, security_groups=None, flow_logs=None, network_interfaces=None):
        self._instance = instance
        self._security_groups = security_groups or []
        self._flow_logs = flow_logs or []
        self._network_interfaces = network_interfaces or []

    def describe_instances(self, InstanceIds):
        if self._instance and self._instance["InstanceId"] in InstanceIds:
            return {"Reservations": [{"Instances": [self._instance]}]}
        return {"Reservations": []}

    def describe_security_groups(self, GroupIds):
        return {"SecurityGroups": [sg for sg in self._security_groups if sg["GroupId"] in GroupIds]}

    def describe_flow_logs(self, Filters):
        return {"FlowLogs": self._flow_logs}

    def describe_network_interfaces(self, Filters):
        filters = {f["Name"]: f["Values"] for f in Filters}
        matches = self._network_interfaces
        if "addresses.private-ip-address" in filters:
            wanted = set(filters["addresses.private-ip-address"])
            matches = [
                eni for eni in matches
                if wanted & {a.get("PrivateIpAddress") for a in eni.get("PrivateIpAddresses", [])}
            ]
        if "vpc-id" in filters:
            wanted_vpcs = set(filters["vpc-id"])
            matches = [eni for eni in matches if eni.get("VpcId") in wanted_vpcs]
        if "description" in filters:
            import fnmatch
            patterns = filters["description"]
            matches = [eni for eni in matches if any(fnmatch.fnmatchcase(eni.get("Description", ""), p) for p in patterns)]
        return {"NetworkInterfaces": matches}


class FakeLogsClient:
    def __init__(self, query_results=None, status="Complete"):
        self._query_results = query_results or []
        self._status = status
        self.started = False
        self.stopped = False

    def start_query(self, logGroupName, startTime, endTime, queryString):
        self.started = True
        return {"queryId": "q-1"}

    def get_query_results(self, queryId):
        return {"status": self._status, "results": self._query_results}

    def stop_query(self, queryId):
        self.stopped = True


class FakeCloudWatchClient:
    def __init__(self, netin_total=0, netout_total=0, raise_error=False):
        self._netin_total = netin_total
        self._netout_total = netout_total
        self._raise_error = raise_error

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime):
        if self._raise_error:
            raise RuntimeError("cloudwatch down")
        return {"MetricDataResults": [
            {"Id": "netin", "Values": [self._netin_total]},
            {"Id": "netout", "Values": [self._netout_total]},
        ]}


def _fake_instance(instance_id="i-topo1", vpc_id="vpc-1", subnet_id="subnet-1", sg_ids=None, private_ip="10.0.0.5", public_ip=None, name="web-1"):
    sg_ids = sg_ids or ["sg-1"]
    return {
        "InstanceId": instance_id, "VpcId": vpc_id, "SubnetId": subnet_id,
        "PrivateIpAddress": private_ip, "PublicIpAddress": public_ip,
        "SecurityGroups": [{"GroupId": g} for g in sg_ids],
        "State": {"Name": "running"}, "InstanceType": "t3.micro",
        "Tags": [{"Key": "Name", "Value": name}],
    }


def _fake_security_group(group_id="sg-1", name="web-sg"):
    return {
        "GroupId": group_id, "GroupName": name,
        "IpPermissions": [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
        "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
    }


@pytest.fixture(autouse=True)
def _fast_flow_log_query(monkeypatch):
    """Avoid any real time.sleep() delay in _query_flow_log_peers' polling
    loop during tests (the fakes above always resolve on the first
    get_query_results call, but this keeps the test suite fast/robust
    even if that ever changes)."""
    monkeypatch.setattr(cmdb_module.time, "sleep", lambda *a, **k: None)


def _patch_topology_clients(monkeypatch, ec2=None, logs=None, cloudwatch=None, regions=("ap-southeast-1",)):
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())
    monkeypatch.setattr(cmdb_module, "_regions_for_account", lambda *a, **k: list(regions))

    def _router(account_id, service, region, core_session):
        if service == "ec2":
            return ec2
        if service == "logs":
            return logs
        if service == "cloudwatch":
            return cloudwatch
        raise AssertionError(f"unexpected service requested: {service}")

    monkeypatch.setattr(cmdb_module, "_regional_client_for_account", _router)


def test_find_target_instance_returns_matching_instance():
    inst = _fake_instance()
    ec2 = FakeEc2Topology(instance=inst)
    found = cmdb_module._find_target_instance(ec2, "i-topo1")
    assert found["InstanceId"] == "i-topo1"


def test_find_target_instance_returns_none_when_not_found():
    ec2 = FakeEc2Topology(instance=_fake_instance())
    assert cmdb_module._find_target_instance(ec2, "i-does-not-exist") is None


def test_find_target_instance_returns_none_on_api_error():
    class RaisingEc2:
        def describe_instances(self, InstanceIds):
            raise RuntimeError("api down")
    assert cmdb_module._find_target_instance(RaisingEc2(), "i-1") is None


def test_security_group_nodes_builds_rule_summaries():
    ec2 = FakeEc2Topology(security_groups=[_fake_security_group()])
    nodes = cmdb_module._security_group_nodes(ec2, ["sg-1"])
    assert len(nodes) == 1
    n = nodes[0]
    assert n["id"] == "sg-sg-1"
    assert n["kind"] == "security_group"
    assert n["label"] == "web-sg"
    assert any("443" in r for r in n["inbound_rules"])
    assert any("All" in r for r in n["outbound_rules"])


def test_security_group_nodes_empty_when_no_ids():
    ec2 = FakeEc2Topology()
    assert cmdb_module._security_group_nodes(ec2, []) == []


def test_security_group_nodes_returns_empty_on_api_error():
    class RaisingEc2:
        def describe_security_groups(self, GroupIds):
            raise RuntimeError("boom")
    assert cmdb_module._security_group_nodes(RaisingEc2(), ["sg-1"]) == []


def test_vpc_flow_log_destination_returns_active_cw_logs_group():
    ec2 = FakeEc2Topology(flow_logs=[
        {"FlowLogStatus": "ACTIVE", "LogDestinationType": "s3", "LogGroupName": None},
        {"FlowLogStatus": "ACTIVE", "LogDestinationType": "cloud-watch-logs", "LogGroupName": "/vpc/flowlogs"},
    ])
    assert cmdb_module._vpc_flow_log_destination(ec2, "vpc-1") == "/vpc/flowlogs"


def test_vpc_flow_log_destination_ignores_inactive_flow_logs():
    ec2 = FakeEc2Topology(flow_logs=[
        {"FlowLogStatus": "INACTIVE", "LogDestinationType": "cloud-watch-logs", "LogGroupName": "/vpc/flowlogs"},
    ])
    assert cmdb_module._vpc_flow_log_destination(ec2, "vpc-1") is None


def test_vpc_flow_log_destination_returns_none_when_only_s3_destination():
    ec2 = FakeEc2Topology(flow_logs=[
        {"FlowLogStatus": "ACTIVE", "LogDestinationType": "s3", "LogGroupName": None},
    ])
    assert cmdb_module._vpc_flow_log_destination(ec2, "vpc-1") is None


def test_vpc_flow_log_destination_returns_none_when_no_flow_logs():
    assert cmdb_module._vpc_flow_log_destination(FakeEc2Topology(flow_logs=[]), "vpc-1") is None


def test_vpc_flow_log_destination_returns_none_on_api_error():
    class RaisingEc2:
        def describe_flow_logs(self, Filters):
            raise RuntimeError("boom")
    assert cmdb_module._vpc_flow_log_destination(RaisingEc2(), "vpc-1") is None


def _flow_log_row(srcaddr, dstaddr, dstport, action, total_bytes, flow_count):
    return [
        {"field": "srcaddr", "value": srcaddr},
        {"field": "dstaddr", "value": dstaddr},
        {"field": "dstport", "value": dstport},
        {"field": "action", "value": action},
        {"field": "total_bytes", "value": str(total_bytes)},
        {"field": "flow_count", "value": str(flow_count)},
    ]


def test_query_flow_log_peers_classifies_inbound_and_outbound():
    rows = [
        _flow_log_row("198.51.100.1", "10.0.0.5", "443", "ACCEPT", 1000, 5),
        _flow_log_row("10.0.0.5", "198.51.100.2", "443", "ACCEPT", 2000, 3),
    ]
    logs = FakeLogsClient(query_results=rows)
    result = cmdb_module._query_flow_log_peers(logs, "/vpc/flowlogs", ["10.0.0.5"])
    assert result["truncated"] is False
    peers = {p["peer_ip"]: p for p in result["peers"]}
    assert peers["198.51.100.1"]["direction"] == "inbound"
    assert peers["198.51.100.1"]["bytes"] == 1000
    assert peers["198.51.100.2"]["direction"] == "outbound"
    assert peers["198.51.100.2"]["bytes"] == 2000


def test_query_flow_log_peers_supports_multiple_ips():
    """Load Balancers have one ENI/IP per AZ — the query must scope to
    ANY of them, not just a single IP."""
    rows = [_flow_log_row("198.51.100.1", "10.0.1.10", "443", "ACCEPT", 500, 2)]
    logs = FakeLogsClient(query_results=rows)
    result = cmdb_module._query_flow_log_peers(logs, "/vpc/flowlogs", ["10.0.1.10", "10.0.2.10"])
    assert logs.started is True
    assert len(result["peers"]) == 1


def test_query_flow_log_peers_returns_empty_when_no_ips():
    logs = FakeLogsClient()
    result = cmdb_module._query_flow_log_peers(logs, "/vpc/flowlogs", [])
    assert result == {"peers": [], "truncated": False}
    assert logs.started is False


def test_query_flow_log_peers_returns_empty_on_start_query_error():
    class RaisingLogs:
        def start_query(self, **kwargs):
            raise RuntimeError("logs down")
    result = cmdb_module._query_flow_log_peers(RaisingLogs(), "/vpc/flowlogs", ["10.0.0.5"])
    assert result == {"peers": [], "truncated": False}


def test_query_flow_log_peers_handles_non_complete_status_gracefully():
    logs = FakeLogsClient(status="Failed")
    result = cmdb_module._query_flow_log_peers(logs, "/vpc/flowlogs", ["10.0.0.5"])
    assert result["peers"] == []
    assert result["truncated"] is True
    assert logs.stopped is True


def test_query_flow_log_peers_skips_rows_missing_addresses():
    rows = [[{"field": "action", "value": "ACCEPT"}, {"field": "total_bytes", "value": "10"}]]
    logs = FakeLogsClient(query_results=rows)
    result = cmdb_module._query_flow_log_peers(logs, "/vpc/flowlogs", ["10.0.0.5"])
    assert result["peers"] == []


# ── Peer IP resolution / mandatory inbound "trigger" classification ───────

def test_resolve_peer_ip_public_ip_is_always_user():
    ec2 = FakeEc2Topology()
    result = cmdb_module._resolve_peer_ip(ec2, "8.8.8.8")
    assert result == {"classification": "user"}


def test_resolve_peer_ip_private_ip_matching_ec2_instance_eni():
    eni = {"VpcId": "vpc-1", "Description": "", "Attachment": {"InstanceId": "i-peer1"},
           "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.2.5", "Primary": True}]}
    ec2 = FakeEc2Topology(network_interfaces=[eni])
    result = cmdb_module._resolve_peer_ip(ec2, "10.0.2.5")
    assert result["classification"] == "resource"
    assert result["resource_type"] == cmdb_module.RESOURCE_TYPE_EC2
    assert result["resource_id"] == "i-peer1"


def test_resolve_peer_ip_private_ip_matching_elb_eni():
    eni = {"VpcId": "vpc-1", "Description": "ELB app/my-alb/abc123def456", "Attachment": {},
           "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.3.9", "Primary": True}]}
    ec2 = FakeEc2Topology(network_interfaces=[eni])
    result = cmdb_module._resolve_peer_ip(ec2, "10.0.3.9")
    assert result["classification"] == "resource"
    assert result["resource_type"] == cmdb_module.RESOURCE_TYPE_ELB
    assert result["resource_id"] == "my-alb"


def test_resolve_peer_ip_private_ip_with_no_matching_eni_is_user():
    ec2 = FakeEc2Topology(network_interfaces=[])
    result = cmdb_module._resolve_peer_ip(ec2, "10.0.9.9")
    assert result == {"classification": "user"}


def test_resolve_peer_ip_degrades_to_user_on_api_error():
    class RaisingEc2:
        def describe_network_interfaces(self, Filters):
            raise RuntimeError("boom")
    result = cmdb_module._resolve_peer_ip(RaisingEc2(), "10.0.9.9")
    assert result == {"classification": "user"}


def test_user_node_shape():
    node = cmdb_module._user_node()
    assert node["id"] == cmdb_module._USER_NODE_ID
    assert node["kind"] == "user"
    assert node["external_ips"] == []


def test_append_peers_to_graph_groups_unresolved_and_public_under_one_user_node():
    ec2 = FakeEc2Topology(network_interfaces=[])
    peers = [
        {"peer_ip": "8.8.8.8", "direction": "inbound", "action": "ACCEPT", "dst_port": "443", "bytes": 100, "flow_count": 1},
        {"peer_ip": "10.0.9.9", "direction": "inbound", "action": "ACCEPT", "dst_port": "443", "bytes": 200, "flow_count": 2},
    ]
    nodes, edges = [], []
    cmdb_module._append_peers_to_graph(ec2, "root-1", peers, nodes, edges)

    user_nodes = [n for n in nodes if n["kind"] == "user"]
    assert len(user_nodes) == 1
    assert set(user_nodes[0]["external_ips"]) == {"8.8.8.8", "10.0.9.9"}
    assert len(edges) == 2
    assert all(e["source"] == cmdb_module._USER_NODE_ID for e in edges)


def test_append_peers_to_graph_gives_resolved_resource_its_own_green_inbound_edge():
    eni = {"VpcId": "vpc-1", "Description": "", "Attachment": {"InstanceId": "i-peer1"},
           "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.2.5", "Primary": True}]}
    ec2 = FakeEc2Topology(network_interfaces=[eni])
    peers = [{"peer_ip": "10.0.2.5", "direction": "inbound", "action": "ACCEPT", "dst_port": "443", "bytes": 100, "flow_count": 1}]
    nodes, edges = [], []
    cmdb_module._append_peers_to_graph(ec2, "root-1", peers, nodes, edges)

    resource_nodes = [n for n in nodes if n["kind"] == "resource"]
    assert len(resource_nodes) == 1
    assert resource_nodes[0]["resolved"] is True
    assert resource_nodes[0]["resource_type"] == cmdb_module.RESOURCE_TYPE_EC2
    assert not any(n["kind"] == "user" for n in nodes)
    edge = edges[0]
    assert edge["source"] == resource_nodes[0]["id"]
    assert edge["target"] == "root-1"
    assert edge["direction"] == "inbound"
    assert edge["animated"] is True


def test_append_peers_to_graph_deduplicates_resolved_resource_by_ip():
    eni = {"VpcId": "vpc-1", "Description": "", "Attachment": {"InstanceId": "i-peer1"},
           "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.2.5", "Primary": True}]}
    ec2 = FakeEc2Topology(network_interfaces=[eni])
    peers = [
        {"peer_ip": "10.0.2.5", "direction": "inbound", "action": "ACCEPT", "dst_port": "443", "bytes": 100, "flow_count": 1},
        {"peer_ip": "10.0.2.5", "direction": "outbound", "action": "ACCEPT", "dst_port": "5432", "bytes": 50, "flow_count": 1},
    ]
    nodes, edges = [], []
    cmdb_module._append_peers_to_graph(ec2, "root-1", peers, nodes, edges)
    resource_nodes = [n for n in nodes if n["kind"] == "resource"]
    assert len(resource_nodes) == 1
    assert len(edges) == 2


def test_query_network_activity_metrics_reports_activity_flags():
    cw = FakeCloudWatchClient(netin_total=500, netout_total=0)
    result = cmdb_module._query_network_activity_metrics(cw, "i-1")
    assert result["has_inbound"] is True
    assert result["has_outbound"] is False
    assert result["bytes_in"] == 500
    assert result["bytes_out"] == 0


def test_query_network_activity_metrics_returns_false_flags_on_error():
    cw = FakeCloudWatchClient(raise_error=True)
    result = cmdb_module._query_network_activity_metrics(cw, "i-1")
    assert result == {"has_inbound": False, "has_outbound": False, "bytes_in": 0, "bytes_out": 0}


def test_get_ec2_topology_raises_when_instance_not_found(monkeypatch):
    ec2 = FakeEc2Topology(instance=None)
    _patch_topology_clients(monkeypatch, ec2=ec2)
    with pytest.raises(cmdb_module.CmdbTopologyError):
        cmdb_module.get_ec2_topology("111111111111", "i-missing")


def test_get_ec2_topology_uses_flow_logs_when_available(monkeypatch):
    inst = _fake_instance()
    ec2 = FakeEc2Topology(instance=inst, security_groups=[_fake_security_group()],
                           flow_logs=[{"FlowLogStatus": "ACTIVE", "LogDestinationType": "cloud-watch-logs", "LogGroupName": "/vpc/fl"}])
    rows = [_flow_log_row("198.51.100.1", "10.0.0.5", "443", "ACCEPT", 1000, 5)]
    logs = FakeLogsClient(query_results=rows)
    _patch_topology_clients(monkeypatch, ec2=ec2, logs=logs)

    topo = cmdb_module.get_ec2_topology("111111111111", "i-topo1")

    assert topo["has_flow_logs"] is True
    assert topo["instance_id"] == "i-topo1"
    assert topo["resource_type"] == cmdb_module.RESOURCE_TYPE_EC2
    node_kinds = {n["kind"] for n in topo["nodes"]}
    # 198.51.100.1 is a public IP with no matching ENI -> mandatory
    # "trigger" classification groups it under the shared User node,
    # not a bare "peer" node (the old, pre-classification shape).
    assert {"instance", "vpc", "subnet", "security_group", "user"} <= node_kinds
    traffic_edges = [e for e in topo["edges"] if e["kind"] == "traffic"]
    assert len(traffic_edges) == 1
    assert traffic_edges[0]["direction"] == "inbound"
    assert traffic_edges[0]["source"] == cmdb_module._USER_NODE_ID
    assert traffic_edges[0]["animated"] is True
    # Every context ("will access") edge must be the yellow-animated kind.
    context_edges = [e for e in topo["edges"] if e["kind"] == "context"]
    assert len(context_edges) >= 3
    assert all(e["animated"] is True for e in context_edges)


def test_get_ec2_topology_resolved_inbound_peer_gets_its_own_resource_node(monkeypatch):
    """A private peer IP that matches another EC2 instance's ENI must
    become its own 'resource' node with a green (animated) inbound edge
    — distinct from the generic User bucket."""
    inst = _fake_instance()
    peer_eni = {"VpcId": "vpc-1", "Description": "", "Attachment": {"InstanceId": "i-peer1"},
                "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.2.5", "Primary": True}]}
    ec2 = FakeEc2Topology(instance=inst, security_groups=[_fake_security_group()],
                           flow_logs=[{"FlowLogStatus": "ACTIVE", "LogDestinationType": "cloud-watch-logs", "LogGroupName": "/vpc/fl"}],
                           network_interfaces=[peer_eni])
    rows = [_flow_log_row("10.0.2.5", "10.0.0.5", "443", "ACCEPT", 1000, 5)]
    logs = FakeLogsClient(query_results=rows)
    _patch_topology_clients(monkeypatch, ec2=ec2, logs=logs)

    topo = cmdb_module.get_ec2_topology("111111111111", "i-topo1")

    resource_nodes = [n for n in topo["nodes"] if n["kind"] == "resource"]
    assert len(resource_nodes) == 1
    assert resource_nodes[0]["resource_type"] == cmdb_module.RESOURCE_TYPE_EC2
    assert not any(n["kind"] == "user" for n in topo["nodes"])
    traffic_edges = [e for e in topo["edges"] if e["kind"] == "traffic"]
    assert traffic_edges[0]["source"] == resource_nodes[0]["id"]
    assert traffic_edges[0]["animated"] is True


def test_get_ec2_topology_falls_back_to_metrics_when_no_flow_log(monkeypatch):
    inst = _fake_instance()
    ec2 = FakeEc2Topology(instance=inst, security_groups=[_fake_security_group()], flow_logs=[])
    cw = FakeCloudWatchClient(netin_total=100, netout_total=0)
    _patch_topology_clients(monkeypatch, ec2=ec2, cloudwatch=cw)

    topo = cmdb_module.get_ec2_topology("111111111111", "i-topo1")

    assert topo["has_flow_logs"] is False
    net_nodes = [n for n in topo["nodes"] if n["kind"] == "network"]
    assert len(net_nodes) == 1
    traffic_edges = {e["direction"]: e for e in topo["edges"] if e["kind"] == "traffic"}
    assert traffic_edges["inbound"]["animated"] is True
    assert traffic_edges["outbound"]["animated"] is False


def test_get_ec2_topology_falls_back_to_metrics_when_flow_log_query_yields_no_peers(monkeypatch):
    """Even when a usable flow log exists, a zero-peer result (e.g.
    nothing observed in the lookback window) must still fall back to the
    metrics-based generic indicator rather than leaving the diagram with
    no traffic edges at all."""
    inst = _fake_instance()
    ec2 = FakeEc2Topology(instance=inst, security_groups=[_fake_security_group()],
                           flow_logs=[{"FlowLogStatus": "ACTIVE", "LogDestinationType": "cloud-watch-logs", "LogGroupName": "/vpc/fl"}])
    logs = FakeLogsClient(query_results=[])
    cw = FakeCloudWatchClient(netin_total=0, netout_total=50)
    _patch_topology_clients(monkeypatch, ec2=ec2, logs=logs, cloudwatch=cw)

    topo = cmdb_module.get_ec2_topology("111111111111", "i-topo1")

    assert topo["has_flow_logs"] is False
    assert any(n["kind"] == "network" for n in topo["nodes"])


def test_get_ec2_topology_probes_multiple_regions_until_found(monkeypatch):
    inst = _fake_instance()

    class RegionAwareEc2Router:
        """Only the second region's client actually has the instance —
        exercises get_ec2_topology's per-region probing loop."""
        def __init__(self):
            self.calls = []

        def client_for(self, region):
            self.calls.append(region)
            if region == "us-east-1":
                return FakeEc2Topology(instance=inst, security_groups=[_fake_security_group()], flow_logs=[])
            return FakeEc2Topology(instance=None)

    router = RegionAwareEc2Router()
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())
    monkeypatch.setattr(cmdb_module, "_regions_for_account", lambda *a, **k: ["ap-southeast-1", "us-east-1"])
    cw = FakeCloudWatchClient()

    def _router(account_id, service, region, core_session):
        if service == "ec2":
            return router.client_for(region)
        if service == "cloudwatch":
            return cw
        raise AssertionError(f"unexpected service {service}")

    monkeypatch.setattr(cmdb_module, "_regional_client_for_account", _router)

    topo = cmdb_module.get_ec2_topology("111111111111", "i-topo1")
    assert router.calls == ["ap-southeast-1", "us-east-1"]
    assert topo["nodes"][0]["region"] == "us-east-1"


def test_get_ec2_topology_handles_instance_with_no_vpc(monkeypatch):
    """An EC2-Classic-style instance (no VpcId — extremely rare now, but
    the code must not crash) should still return instance + SG nodes,
    just no vpc/subnet nodes and no flow-log lookup attempted."""
    inst = _fake_instance(vpc_id=None, subnet_id=None)
    ec2 = FakeEc2Topology(instance=inst, security_groups=[_fake_security_group()])
    cw = FakeCloudWatchClient()
    _patch_topology_clients(monkeypatch, ec2=ec2, cloudwatch=cw)

    topo = cmdb_module.get_ec2_topology("111111111111", "i-topo1")
    node_kinds = {n["kind"] for n in topo["nodes"]}
    assert "vpc" not in node_kinds
    assert "subnet" not in node_kinds
    assert topo["has_flow_logs"] is False


def test_get_ec2_topology_uses_mock_in_local_env(monkeypatch):
    monkeypatch.setattr(cmdb_module, "ENV", "local")
    monkeypatch.delenv("CORE_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("CORE_SECRET_ACCESS_KEY", raising=False)

    topo = cmdb_module.get_ec2_topology("100000000001", "i-fake123")
    assert topo["is_mock"] is True
    assert topo["instance_id"] == "i-fake123"
    assert len(topo["nodes"]) > 0
    assert len(topo["edges"]) > 0


def test_mock_ec2_topology_shape():
    topo = cmdb_module._mock_ec2_topology("100000000001", "i-abc")
    assert topo["has_flow_logs"] is True
    kinds = {n["kind"] for n in topo["nodes"]}
    assert {"instance", "vpc", "subnet", "security_group", "resource", "user"} <= kinds
    traffic_edges = [e for e in topo["edges"] if e["kind"] == "traffic"]
    assert any(e["action"] == "REJECT" for e in traffic_edges)
    assert any(e["action"] == "ACCEPT" and e["direction"] == "inbound" for e in traffic_edges)
    context_edges = [e for e in topo["edges"] if e["kind"] == "context"]
    assert len(context_edges) >= 3
    assert all(e["animated"] is True for e in context_edges)


# ── RDS Topology ───────────────────────────────────────────────────────────

class FakeRdsTopology:
    """Fake RDS client covering describe_db_instances — the one RDS call
    get_rds_topology() makes."""

    def __init__(self, db_instance=None):
        self._db_instance = db_instance

    def describe_db_instances(self, DBInstanceIdentifier):
        if self._db_instance and self._db_instance["DBInstanceIdentifier"] == DBInstanceIdentifier:
            return {"DBInstances": [self._db_instance]}
        return {"DBInstances": []}


def _fake_db_instance(db_id="db-topo1", vpc_id="vpc-1", subnet_ids=("subnet-1",), sg_ids=("sg-1",), engine="postgres"):
    return {
        "DBInstanceIdentifier": db_id,
        "Engine": engine,
        "DBInstanceClass": "db.t3.micro",
        "DBInstanceStatus": "available",
        "Endpoint": {"Address": f"{db_id}.abc123.ap-southeast-1.rds.amazonaws.com"},
        "DBSubnetGroup": {"VpcId": vpc_id, "Subnets": [{"SubnetIdentifier": s} for s in subnet_ids]},
        "VpcSecurityGroups": [{"VpcSecurityGroupId": g} for g in sg_ids],
    }


def _patch_rds_topology_clients(monkeypatch, rds=None, ec2=None, logs=None, cloudwatch=None, regions=("ap-southeast-1",)):
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())
    monkeypatch.setattr(cmdb_module, "_regions_for_account", lambda *a, **k: list(regions))

    def _router(account_id, service, region, core_session):
        if service == "rds":
            return rds
        if service == "ec2":
            return ec2
        if service == "logs":
            return logs
        if service == "cloudwatch":
            return cloudwatch
        raise AssertionError(f"unexpected service requested: {service}")

    monkeypatch.setattr(cmdb_module, "_regional_client_for_account", _router)


def test_find_target_db_instance_returns_matching_db():
    db = _fake_db_instance()
    rds = FakeRdsTopology(db_instance=db)
    found = cmdb_module._find_target_db_instance(rds, "db-topo1")
    assert found["DBInstanceIdentifier"] == "db-topo1"


def test_find_target_db_instance_returns_none_on_api_error():
    class RaisingRds:
        def describe_db_instances(self, DBInstanceIdentifier):
            raise RuntimeError("api down")
    assert cmdb_module._find_target_db_instance(RaisingRds(), "db-1") is None


def test_get_rds_topology_raises_when_db_not_found(monkeypatch):
    rds = FakeRdsTopology(db_instance=None)
    _patch_rds_topology_clients(monkeypatch, rds=rds, ec2=FakeEc2Topology())
    with pytest.raises(cmdb_module.CmdbTopologyError):
        cmdb_module.get_rds_topology("111111111111", "db-missing")


def test_get_rds_topology_builds_vpc_subnet_sg_context_with_yellow_edges(monkeypatch):
    db = _fake_db_instance()
    rds = FakeRdsTopology(db_instance=db)
    ec2 = FakeEc2Topology(security_groups=[_fake_security_group()], flow_logs=[])
    cw = FakeCloudWatchClient(netin_total=10, netout_total=0)
    _patch_rds_topology_clients(monkeypatch, rds=rds, ec2=ec2, cloudwatch=cw)

    topo = cmdb_module.get_rds_topology("111111111111", "db-topo1")

    assert topo["instance_id"] == "db-topo1"
    assert topo["resource_type"] == cmdb_module.RESOURCE_TYPE_RDS
    node_kinds = {n["kind"] for n in topo["nodes"]}
    assert {"instance", "vpc", "subnet", "security_group"} <= node_kinds
    root = next(n for n in topo["nodes"] if n["kind"] == "instance")
    assert root["engine"] == "postgres"
    context_edges = [e for e in topo["edges"] if e["kind"] == "context"]
    assert len(context_edges) >= 2
    assert all(e["animated"] is True for e in context_edges)
    # No RDS ENI found by the fake -> no private_ip -> falls back to metrics.
    assert topo["has_flow_logs"] is False


def test_get_rds_topology_uses_flow_logs_when_rds_eni_and_flow_log_available(monkeypatch):
    db = _fake_db_instance()
    rds = FakeRdsTopology(db_instance=db)
    rds_eni = {"VpcId": "vpc-1", "Description": "RDSNetworkInterface",
               "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.1.50", "Primary": True}]}
    ec2 = FakeEc2Topology(security_groups=[_fake_security_group()], network_interfaces=[rds_eni],
                           flow_logs=[{"FlowLogStatus": "ACTIVE", "LogDestinationType": "cloud-watch-logs", "LogGroupName": "/vpc/fl"}])
    rows = [_flow_log_row("198.51.100.5", "10.0.1.50", "5432", "ACCEPT", 4096, 8)]
    logs = FakeLogsClient(query_results=rows)
    _patch_rds_topology_clients(monkeypatch, rds=rds, ec2=ec2, logs=logs)

    topo = cmdb_module.get_rds_topology("111111111111", "db-topo1")

    assert topo["has_flow_logs"] is True
    root = next(n for n in topo["nodes"] if n["kind"] == "instance")
    assert root["private_ip"] == "10.0.1.50"
    traffic_edges = [e for e in topo["edges"] if e["kind"] == "traffic"]
    assert len(traffic_edges) == 1
    assert traffic_edges[0]["source"] == cmdb_module._USER_NODE_ID


def test_get_rds_topology_uses_mock_in_local_env(monkeypatch):
    monkeypatch.setattr(cmdb_module, "ENV", "local")
    monkeypatch.delenv("CORE_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("CORE_SECRET_ACCESS_KEY", raising=False)

    topo = cmdb_module.get_rds_topology("100000000001", "db-fake123")
    assert topo["is_mock"] is True
    assert topo["instance_id"] == "db-fake123"
    assert topo["resource_type"] == cmdb_module.RESOURCE_TYPE_RDS


def test_query_rds_activity_metrics_reports_flags():
    cw = FakeCloudWatchClient(netin_total=300, netout_total=0)
    result = cmdb_module._query_rds_activity_metrics(cw, "db-1")
    assert result["has_inbound"] is True
    assert result["has_outbound"] is False


def test_query_rds_activity_metrics_returns_false_on_error():
    cw = FakeCloudWatchClient(raise_error=True)
    result = cmdb_module._query_rds_activity_metrics(cw, "db-1")
    assert result == {"has_inbound": False, "has_outbound": False, "bytes_in": 0, "bytes_out": 0}


# ── Load Balancer Topology ─────────────────────────────────────────────────

class FakeElbv2Topology:
    """Fake elbv2 client covering describe_load_balancers,
    describe_target_groups, describe_target_health."""

    def __init__(self, load_balancer=None, target_groups=None, target_health=None):
        self._lb = load_balancer
        self._target_groups = target_groups or []
        self._target_health = target_health or {}

    def describe_load_balancers(self, Names):
        if self._lb and self._lb["LoadBalancerName"] in Names:
            return {"LoadBalancers": [self._lb]}
        return {"LoadBalancers": []}

    def describe_target_groups(self, LoadBalancerArn):
        return {"TargetGroups": self._target_groups}

    def describe_target_health(self, TargetGroupArn):
        return {"TargetHealthDescriptions": self._target_health.get(TargetGroupArn, [])}


def _fake_load_balancer(name="my-alb", vpc_id="vpc-1", sg_ids=("sg-1",), subnet_ids=("subnet-1",), lb_type="application"):
    return {
        "LoadBalancerName": name,
        "LoadBalancerArn": f"arn:aws:elasticloadbalancing:ap-southeast-1:111111111111:loadbalancer/app/{name}/abc123",
        "VpcId": vpc_id,
        "Type": lb_type,
        "Scheme": "internet-facing",
        "DNSName": f"{name}-123.ap-southeast-1.elb.amazonaws.com",
        "State": {"Code": "active"},
        "SecurityGroups": list(sg_ids),
        "AvailabilityZones": [{"SubnetId": s} for s in subnet_ids],
    }


def _patch_lb_topology_clients(monkeypatch, elbv2=None, ec2=None, logs=None, cloudwatch=None, regions=("ap-southeast-1",)):
    monkeypatch.setattr(cmdb_module, "ENV", "production")
    monkeypatch.setattr(cmdb_module, "_core_session", lambda: object())
    monkeypatch.setattr(cmdb_module, "_regions_for_account", lambda *a, **k: list(regions))

    def _router(account_id, service, region, core_session):
        if service == "elbv2":
            return elbv2
        if service == "ec2":
            return ec2
        if service == "logs":
            return logs
        if service == "cloudwatch":
            return cloudwatch
        raise AssertionError(f"unexpected service requested: {service}")

    monkeypatch.setattr(cmdb_module, "_regional_client_for_account", _router)


def test_find_target_load_balancer_returns_matching_lb():
    lb = _fake_load_balancer()
    elbv2 = FakeElbv2Topology(load_balancer=lb)
    found = cmdb_module._find_target_load_balancer(elbv2, "my-alb")
    assert found["LoadBalancerName"] == "my-alb"


def test_get_lb_topology_raises_when_lb_not_found(monkeypatch):
    elbv2 = FakeElbv2Topology(load_balancer=None)
    _patch_lb_topology_clients(monkeypatch, elbv2=elbv2, ec2=FakeEc2Topology())
    with pytest.raises(cmdb_module.CmdbTopologyError):
        cmdb_module.get_lb_topology("111111111111", "missing-lb")


def test_get_lb_topology_builds_target_group_chain_with_yellow_context_edges(monkeypatch):
    lb = _fake_load_balancer()
    tg = {"TargetGroupArn": "arn:aws:elasticloadbalancing:ap-southeast-1:111111111111:targetgroup/my-tg/xyz",
          "TargetGroupName": "my-tg", "Protocol": "HTTP", "Port": 8080}
    health = {tg["TargetGroupArn"]: [
        {"Target": {"Id": "i-target1"}, "TargetHealth": {"State": "healthy"}},
        {"Target": {"Id": "i-target2"}, "TargetHealth": {"State": "unhealthy"}},
    ]}
    elbv2 = FakeElbv2Topology(load_balancer=lb, target_groups=[tg], target_health=health)
    ec2 = FakeEc2Topology(security_groups=[_fake_security_group()], flow_logs=[])
    cw = FakeCloudWatchClient(netin_total=0, netout_total=0)
    _patch_lb_topology_clients(monkeypatch, elbv2=elbv2, ec2=ec2, cloudwatch=cw)

    topo = cmdb_module.get_lb_topology("111111111111", "my-alb")

    assert topo["resource_type"] == cmdb_module.RESOURCE_TYPE_ELB
    node_kinds = {n["kind"] for n in topo["nodes"]}
    assert {"instance", "vpc", "subnet", "security_group", "target_group", "target"} <= node_kinds
    target_nodes = {n["id"]: n for n in topo["nodes"] if n["kind"] == "target"}
    assert target_nodes["target-i-target1"]["health"] == "healthy"
    assert target_nodes["target-i-target2"]["health"] == "unhealthy"
    context_edges = [e for e in topo["edges"] if e["kind"] == "context"]
    assert all(e["animated"] is True for e in context_edges)
    # LB -> TargetGroup -> Target(x2) = at least 3 chain edges, plus SG/VPC context.
    assert len(context_edges) >= 5


def test_get_lb_topology_uses_flow_logs_across_multiple_eni_ips(monkeypatch):
    lb = _fake_load_balancer()
    elbv2 = FakeElbv2Topology(load_balancer=lb, target_groups=[])
    lb_eni_1 = {"VpcId": "vpc-1", "Description": "ELB app/my-alb/abc123",
                "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.1.10", "Primary": True}]}
    lb_eni_2 = {"VpcId": "vpc-1", "Description": "ELB app/my-alb/abc123",
                "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.2.10", "Primary": True}]}
    ec2 = FakeEc2Topology(security_groups=[_fake_security_group()], network_interfaces=[lb_eni_1, lb_eni_2],
                           flow_logs=[{"FlowLogStatus": "ACTIVE", "LogDestinationType": "cloud-watch-logs", "LogGroupName": "/vpc/fl"}])
    rows = [_flow_log_row("203.0.113.9", "10.0.1.10", "443", "ACCEPT", 8192, 20)]
    logs = FakeLogsClient(query_results=rows)
    _patch_lb_topology_clients(monkeypatch, elbv2=elbv2, ec2=ec2, logs=logs)

    topo = cmdb_module.get_lb_topology("111111111111", "my-alb")

    assert topo["has_flow_logs"] is True
    traffic_edges = [e for e in topo["edges"] if e["kind"] == "traffic"]
    assert len(traffic_edges) == 1
    assert traffic_edges[0]["source"] == cmdb_module._USER_NODE_ID


def test_get_lb_topology_falls_back_to_processed_bytes_metric_when_no_flow_log(monkeypatch):
    lb = _fake_load_balancer()
    elbv2 = FakeElbv2Topology(load_balancer=lb, target_groups=[])
    ec2 = FakeEc2Topology(security_groups=[_fake_security_group()], flow_logs=[])
    cw = FakeCloudWatchClient(netin_total=999, netout_total=0)
    _patch_lb_topology_clients(monkeypatch, elbv2=elbv2, ec2=ec2, cloudwatch=cw)

    topo = cmdb_module.get_lb_topology("111111111111", "my-alb")

    assert topo["has_flow_logs"] is False
    net_nodes = [n for n in topo["nodes"] if n["kind"] == "network"]
    assert len(net_nodes) == 1
    traffic_edges = {e["direction"]: e for e in topo["edges"] if e["kind"] == "traffic"}
    assert traffic_edges["inbound"]["animated"] is True


def test_get_lb_topology_uses_mock_in_local_env(monkeypatch):
    monkeypatch.setattr(cmdb_module, "ENV", "local")
    monkeypatch.delenv("CORE_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("CORE_SECRET_ACCESS_KEY", raising=False)

    topo = cmdb_module.get_lb_topology("100000000001", "my-alb")
    assert topo["is_mock"] is True
    assert topo["instance_id"] == "my-alb"
    assert topo["resource_type"] == cmdb_module.RESOURCE_TYPE_ELB
    kinds = {n["kind"] for n in topo["nodes"]}
    assert {"target_group", "target"} <= kinds


def test_query_lb_activity_metrics_reports_flags():
    cw = FakeCloudWatchClient(netin_total=1500, netout_total=0)
    result = cmdb_module._query_lb_activity_metrics(cw, "my-alb", "application")
    assert result["has_inbound"] is True
    assert result["has_outbound"] is True  # ProcessedBytes doesn't distinguish direction


def test_query_lb_activity_metrics_returns_false_on_error():
    cw = FakeCloudWatchClient(raise_error=True)
    result = cmdb_module._query_lb_activity_metrics(cw, "my-alb", "application")
    assert result == {"has_inbound": False, "has_outbound": False, "bytes_in": 0, "bytes_out": 0}


def test_mock_rds_topology_shape():
    topo = cmdb_module._mock_rds_topology("100000000001", "db-abc")
    assert topo["resource_type"] == cmdb_module.RESOURCE_TYPE_RDS
    kinds = {n["kind"] for n in topo["nodes"]}
    assert {"instance", "vpc", "subnet", "security_group", "resource", "user"} <= kinds


def test_mock_lb_topology_shape():
    topo = cmdb_module._mock_lb_topology("100000000001", "my-lb")
    assert topo["resource_type"] == cmdb_module.RESOURCE_TYPE_ELB
    kinds = {n["kind"] for n in topo["nodes"]}
    assert {"instance", "vpc", "subnet", "security_group", "target_group", "target", "resource", "user"} <= kinds
    chain_edges = [e for e in topo["edges"] if e["kind"] == "context" and ("tg-" in e["target"] or "target-" in e["target"])]
    assert len(chain_edges) >= 3
