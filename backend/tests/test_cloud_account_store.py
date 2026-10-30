"""Tests for the cloud account registry (.kiro/specs/cloud-account-onboarding, task 1).

The EXPECTED_* data below is a frozen copy of what cost_explorer.ACCOUNTS /
ACCOUNT_BU and billing_by_service.ACCOUNT_ALIAS contained before the registry
existed. It is deliberately NOT read from those modules (they now read the
registry): this is the "nothing changes for existing accounts" guarantee.
"""

import types

import pytest

from app import cloud_account_seed as seed
from app import cloud_account_store as s

# ── Frozen pre-registry data ────────────────────────────────────────────────

# billing_by_service.ACCOUNT_ALIAS (30 accounts, in this order)
EXPECTED_ALIAS = [
    ("100000000001", "core"), ("100000000003", "network"), ("100000000002", "it-core-sandbox"),
    ("100000000004", "was-prod"), ("100000000010", "was-preprod"), ("100000000020", "lms1-sandbox"),
    ("100000000016", "prod-lms"), ("100000000014", "lms-sandbox"), ("100000000018", "app-a-sandbox"),
    ("100000000021", "app-a"), ("100000000008", "cicd"), ("100000000023", "app-b-sandbox"),
    ("100000000022", "app-b-prod"), ("100000000028", "app-c-prod"), ("100000000011", "app-c-sandbox"),
    ("100000000029", "app-c-cicd-sandbox"), ("100000000009", "ibe"), ("100000000012", "ecomm-ibe-sandbox"),
    ("100000000007", "app-d-sandbox"), ("100000000006", "app-d-prod"), ("100000000024", "ecomm-prod"),
    ("100000000017", "ecomm-sandbox"), ("100000000015", "bi-prod"), ("100000000005", "bi-sandbox"),
    ("100000000027", "hr-system"), ("100000000031", "cdp-prod"), ("100000000013", "cdp-sandbox"),
    ("100000000025", "100000000025"), ("100000000030", "100000000030"), ("100000000019", "100000000019"),
]

# cost_explorer.ACCOUNTS keys (26 accounts)
EXPECTED_CORE_IDS = [
    "100000000001", "100000000003", "100000000002", "100000000004", "100000000010", "100000000020",
    "100000000016", "100000000014", "100000000018", "100000000021", "100000000008", "100000000023",
    "100000000022", "100000000028", "100000000011", "100000000029", "100000000009", "100000000012",
    "100000000007", "100000000006", "100000000024", "100000000017", "100000000015", "100000000005",
    "100000000031", "100000000013",
]

# cost_explorer.ACCOUNT_BU (24 entries; the rest fell back to "Other")
EXPECTED_BU = {
    "100000000001": "BU-A", "100000000003": "BU-A", "100000000002": "BU-A", "100000000004": "BU-A",
    "100000000010": "BU-A", "100000000020": "BU-A", "100000000016": "BU-A", "100000000014": "BU-A",
    "100000000018": "BU-B", "100000000021": "BU-B", "100000000008": "BU-B", "100000000023": "BU-C",
    "100000000022": "BU-C", "100000000028": "BU-D", "100000000011": "BU-D", "100000000029": "BU-D",
    "100000000009": "BU-D", "100000000012": "BU-A", "100000000007": "BU-D", "100000000006": "BU-D",
    "100000000024": "BU-A", "100000000017": "BU-A", "100000000015": "BU-A", "100000000005": "BU-A",
}

ALIAS_IDS = [i for i, _ in EXPECTED_ALIAS]
CORE_IDS_IN_ALIAS_ORDER = [i for i in ALIAS_IDS if i in EXPECTED_CORE_IDS]

GUID = "11111111-2222-3333-4444-555555555555"


# ── Fixtures ────────────────────────────────────────────────────────────────

def _reset(store):
    with store._lock:
        store._snap.update(entries=None, loaded_at=0.0, from_storage=False)


def _raw_update(store, provider, identifier, fields):
    """Write straight to storage, bypassing invalidate() — what ANOTHER ECS task's write looks like."""
    key = store.make_key(provider, identifier)
    if store._use_local():
        return store._local_update(key, fields, None)
    return store._ddb_update(key, fields, None)


@pytest.fixture(params=["local", "dynamodb"])
def store(request, monkeypatch):
    from app import azure_monitor

    def _no_azure():
        raise azure_monitor.AzureError("no azure credentials in tests")

    monkeypatch.setattr(azure_monitor, "_get_credentials", _no_azure)

    if request.param == "local":
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from app import database

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        database.Base.metadata.create_all(engine)
        monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
        monkeypatch.setenv("ENV", "local")
        _reset(s)
        yield s
        _reset(s)
        return

    import boto3
    from moto import mock_aws

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=s.AWS_REGION)
        ddb.create_table(
            TableName=s.ACCOUNTS_TABLE_NAME, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "account_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "account_key", "AttributeType": "S"}],
        )
        ddb.create_table(
            TableName=s.AUDIT_TABLE_NAME, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "account_key", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "account_key", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
        )
        _reset(s)
        yield s
        _reset(s)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(s, "time", types.SimpleNamespace(monotonic=lambda: now["t"]))
    return now


def _maps():
    return (
        s.LiveAccountMap("name", "core"),
        s.LiveAccountMap("name", "billing"),
        s.LiveAccountMap("bu", None),
    )


def _draft(identifier="123456789012", **kw):
    kw.setdefault("external_id", "x" * 40)
    return s.new_entry("aws", identifier, "New Account", actor="admin@x.com", **kw)


# ── Built-in seed data ──────────────────────────────────────────────────────

def test_seed_module_matches_the_frozen_pre_registry_data():
    assert [(i, n) for i, n, _bu, _c in seed.AWS_SEED] == EXPECTED_ALIAS
    assert [i for i, _n, _bu, core in seed.AWS_SEED if core] == CORE_IDS_IN_ALIAS_ORDER
    assert len(CORE_IDS_IN_ALIAS_ORDER) == 26
    for account_id, _name, bu, _core in seed.AWS_SEED:
        assert bu == EXPECTED_BU.get(account_id, "Other"), account_id


# ── Seed ────────────────────────────────────────────────────────────────────

def test_seed_creates_30_aws_entries_with_the_expected_fields(store):
    assert store.seed_if_needed() == 30
    rows = {e["identifier"]: e for e in store.list_all() if e["provider"] == "aws"}
    assert list(sorted(rows)) == sorted(ALIAS_IDS)
    for account_id, name in EXPECTED_ALIAS:
        e = rows[account_id]
        assert e["display_name"] == name
        assert e["business_unit"] == EXPECTED_BU.get(account_id, "Other")
        assert e["scope_billing"] is True
        assert e["scope_core"] is (account_id in EXPECTED_CORE_IDS)
        assert e["access_mode"] == "legacy"
        assert e["status"] == "pending"
        assert e["seeded"] is True and e["never_connected"] is False
        assert e["protected"] is (account_id in ("100000000002", "100000000001"))
        assert "external_id" not in e


def test_seed_is_idempotent_and_never_overwrites_admin_changes(store):
    store.seed_if_needed()
    store.update_fields("aws", "100000000003", {
        "display_name": "Renamed By Admin", "business_unit": "BU-B", "scope_core": False,
    }, actor="admin@x.com")
    assert store.seed_if_needed() == 0  # nothing missing -> nothing created
    e = store.get("aws", "100000000003")
    assert (e["display_name"], e["business_unit"], e["scope_core"]) == ("Renamed By Admin", "BU-B", False)
    assert len([x for x in store.list_all() if x["provider"] == "aws"]) == 30


def test_seed_only_fills_in_missing_entries(store):
    store.seed_if_needed()
    assert store.delete_draft("aws", "100000000003") is False  # seeded -> cannot be deleted...
    # ...so simulate a missing entry by seeding a partial registry instead
    _reset(store)
    other = store.new_entry("aws", "111111111111", "Mine", actor="a")
    assert store.put_new(other) is True
    assert store.seed_if_needed() == 0  # the 30 already exist; 111... is untouched
    assert store.get("aws", "111111111111")["display_name"] == "Mine"


def test_seed_registers_the_configured_azure_subscription(store, monkeypatch):
    from app import azure_monitor

    monkeypatch.setattr(azure_monitor, "_get_credentials", lambda: {"subscription_id": GUID})
    store.seed_if_needed()
    e = store.get("azure", GUID)
    assert e["access_mode"] == "arm" and e["seeded"] is True
    assert store.is_visible(e)  # established -> shown without waiting for a test
    store.seed_if_needed()
    assert len([x for x in store.list_all() if x["provider"] == "azure"]) == 1


def test_seed_without_azure_credentials_still_seeds_aws(store):
    assert store.seed_if_needed() == 30
    assert [x for x in store.list_all() if x["provider"] == "azure"] == []


# ── Live maps equal today's lists ───────────────────────────────────────────

def test_live_maps_equal_the_pre_registry_lists_after_seed(store):
    store.seed_if_needed()
    core, billing, bu = _maps()

    assert list(billing) == ALIAS_IDS  # same ids, same order as ACCOUNT_ALIAS
    assert dict(billing) == dict(EXPECTED_ALIAS)
    assert len(billing) == 30

    assert list(core) == CORE_IDS_IN_ALIAS_ORDER  # same ids, same order as the old ACCOUNTS
    assert len(core) == 26

    assert {k: v for k, v in bu.items()} == {i: EXPECTED_BU.get(i, "Other") for i in ALIAS_IDS}


def test_core_scope_excludes_the_four_billing_only_accounts(store):
    store.seed_if_needed()
    core, billing, _ = _maps()
    for account_id in ("100000000027", "100000000025", "100000000030", "100000000019"):
        assert account_id in billing
        assert account_id not in core


def test_the_only_intended_name_differences_are_the_two_alias_renames(store):
    store.seed_if_needed()
    core, _, _ = _maps()
    old_core_names = {"100000000001": "Core account", "100000000028": "prodteco"}
    for account_id, old in old_core_names.items():
        assert core[account_id] != old
    assert core["100000000001"] == "core"
    assert core["100000000028"] == "app-c-prod"


def test_live_map_behaves_like_a_read_only_dict(store):
    store.seed_if_needed()
    core, billing, _ = _maps()
    assert "100000000003" in billing
    assert billing.get("100000000003") == "network"
    assert billing.get("000000000000", "fallback") == "fallback"
    assert billing["100000000003"] == "network"
    with pytest.raises(KeyError):
        billing["000000000000"]
    assert dict(billing.items()) == dict(EXPECTED_ALIAS)
    assert list(billing.keys()) == ALIAS_IDS
    assert billing == dict(EXPECTED_ALIAS)
    with pytest.raises(TypeError):
        billing["111111111111"] = "x"


def test_live_maps_follow_registry_changes_without_a_new_import(store):
    store.seed_if_needed()
    core, billing, bu = _maps()
    assert billing["100000000003"] == "network"
    store.update_fields("aws", "100000000003", {"display_name": "network-renamed", "business_unit": "BU-C"}, actor="a")
    assert billing["100000000003"] == "network-renamed"
    assert core["100000000003"] == "network-renamed"
    assert bu["100000000003"] == "BU-C"
    store.update_fields("aws", "100000000003", {"status": "offboarded"}, actor="a")
    assert "100000000003" not in billing and "100000000003" not in core and "100000000003" not in bu
    assert len(billing) == 29 and len(core) == 25


def test_scope_flags_control_which_module_sees_an_account(store):
    store.seed_if_needed()
    core, billing, _ = _maps()
    store.update_fields("aws", "100000000004", {"scope_core": False}, actor="a")
    assert "100000000004" not in core and "100000000004" in billing
    store.update_fields("aws", "100000000004", {"scope_core": True, "scope_billing": False}, actor="a")
    assert "100000000004" in core and "100000000004" not in billing


# ── Visibility ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("entry,expected", [
    ({"status": "offboarded", "access_mode": "legacy"}, False),
    ({"status": "offboarded", "access_mode": "infraops"}, False),
    ({"status": "pending", "access_mode": "legacy"}, True),      # established: shown whatever the last test said
    ({"status": "failed", "access_mode": "legacy"}, True),
    ({"status": "connected", "access_mode": "legacy"}, True),
    ({"status": "pending", "access_mode": "infraops"}, False),   # new account: hidden until connected
    ({"status": "failed", "access_mode": "infraops"}, False),
    ({"status": "connected", "access_mode": "infraops"}, True),
    ({"status": "pending", "access_mode": "arm", "seeded": True}, True),
    ({"status": "pending", "access_mode": "arm"}, False),
    ({"status": "connected", "access_mode": "arm"}, True),
])
def test_is_visible_rule(entry, expected):
    assert s.is_visible(entry) is expected


def test_new_account_appears_only_once_connected_and_disappears_when_offboarded(store):
    store.seed_if_needed()
    _, billing, _ = _maps()
    store.put_new(_draft())
    assert "123456789012" not in billing
    store.update_fields("aws", "123456789012", {"status": "failed"}, actor="a")
    assert "123456789012" not in billing
    store.update_fields("aws", "123456789012", {"status": "connected", "never_connected": False}, actor="a")
    assert billing["123456789012"] == "New Account"
    assert list(billing)[-1] == "123456789012"  # after the seeded accounts
    store.update_fields("aws", "123456789012", {"status": "offboarded"}, actor="a")
    assert "123456789012" not in billing


def test_new_account_defaults_to_both_scopes(store):
    store.put_new(_draft())
    e = store.get("aws", "123456789012")
    assert e["scope_core"] is True and e["scope_billing"] is True


def test_access_mode_defaults_to_legacy_for_unknown_accounts(store):
    store.seed_if_needed()
    assert store.access_mode("100000000003") == "legacy"
    assert store.access_mode("000000000000") == "legacy"
    store.put_new(_draft())
    assert store.access_mode("123456789012") == "infraops"


# ── Snapshot cache and fallbacks ────────────────────────────────────────────

def test_empty_registry_falls_back_to_the_builtin_list(store):
    core, billing, _ = _maps()
    assert list(billing) == ALIAS_IDS
    assert list(core) == CORE_IDS_IN_ALIAS_ORDER
    assert store.access_mode("100000000003") == "legacy"


def test_unreadable_registry_falls_back_to_the_builtin_list(store, monkeypatch):
    def boom():
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(store, "list_all", boom)
    _, billing, _ = _maps()
    assert list(billing) == ALIAS_IDS


def test_unreadable_registry_keeps_the_last_good_snapshot(store, monkeypatch, clock):
    store.seed_if_needed()
    store.update_fields("aws", "100000000003", {"display_name": "custom"}, actor="a")
    _, billing, _ = _maps()
    assert billing["100000000003"] == "custom"

    def boom():
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(store, "list_all", boom)
    clock["t"] += store.SNAPSHOT_TTL_SECONDS + 1  # force a reload attempt
    assert billing["100000000003"] == "custom"  # not reverted to the built-in name


def test_other_tasks_changes_appear_after_the_ttl_but_own_writes_are_immediate(store, clock):
    store.seed_if_needed()
    _, billing, _ = _maps()
    assert billing["100000000003"] == "network"

    _raw_update(store, "aws", "100000000003", {"display_name": "from-other-task"})
    assert billing["100000000003"] == "network"  # still cached
    clock["t"] += store.SNAPSHOT_TTL_SECONDS - 1
    assert billing["100000000003"] == "network"
    clock["t"] += 2
    assert billing["100000000003"] == "from-other-task"

    store.update_fields("aws", "100000000003", {"display_name": "from-this-task"}, actor="a")
    assert billing["100000000003"] == "from-this-task"  # own write: no waiting


def test_snapshot_ttl_is_within_the_60_second_requirement():
    assert s.SNAPSHOT_TTL_SECONDS <= 60


# ── Conditional writes ──────────────────────────────────────────────────────

def test_update_fields_sets_only_the_given_fields_and_stamps_the_actor(store):
    store.put_new(_draft())
    before = store.get("aws", "123456789012")
    out = store.update_fields("aws", "123456789012", {"display_name": "Changed"}, actor="bob@x.com")
    assert out["display_name"] == "Changed"
    assert out["business_unit"] == before["business_unit"]
    assert out["external_id"] == before["external_id"]
    assert out["updated_by"] == "bob@x.com"
    assert out["updated_at"] >= before["updated_at"]


def test_update_fields_on_a_missing_entry_returns_none_and_creates_nothing(store):
    assert store.update_fields("aws", "999999999999", {"display_name": "x"}, actor="a") is None
    assert store.get("aws", "999999999999") is None


def test_update_fields_expect_only_writes_when_the_condition_holds(store):
    store.put_new(_draft())
    assert store.update_fields("aws", "123456789012", {"status": "connected"}, actor="a",
                               expect={"status": "failed"}) is None
    assert store.get("aws", "123456789012")["status"] == "pending"
    out = store.update_fields("aws", "123456789012", {"status": "connected"}, actor="a",
                              expect={"status": "pending"})
    assert out["status"] == "connected"


def test_put_new_refuses_a_duplicate_and_keeps_the_original(store):
    assert store.put_new(_draft()) is True
    dup = _draft()
    dup["display_name"] = "Imposter"
    assert store.put_new(dup) is False
    assert store.get("aws", "123456789012")["display_name"] == "New Account"


def test_same_identifier_under_another_provider_is_a_different_entry(store):
    assert store.put_new(_draft(GUID)) is True
    azure = store.new_entry("azure", GUID, "Sub", actor="a", access_mode="arm")
    assert store.put_new(azure) is True


def test_delete_draft_only_removes_never_connected_unseeded_entries(store):
    store.seed_if_needed()
    assert store.delete_draft("aws", "100000000003") is False  # seeded
    store.put_new(_draft())
    assert store.delete_draft("aws", "123456789012") is True
    assert store.get("aws", "123456789012") is None

    store.put_new(_draft())
    store.update_fields("aws", "123456789012", {"status": "connected", "never_connected": False}, actor="a")
    assert store.delete_draft("aws", "123456789012") is False  # was connected -> offboard only
    assert store.get("aws", "123456789012") is not None
    assert store.delete_draft("aws", "000000000000") is False  # missing


# ── Audit ───────────────────────────────────────────────────────────────────

def test_audit_entries_are_listed_newest_first_and_scrub_external_ids(store):
    key = store.make_key("aws", "123456789012")
    store.append_audit(key, actor="a@x.com", action="create",
                       after={"display_name": "New", "external_id": "SECRET-VALUE", "nested": {"external_id": "S2"}})
    store.append_audit(key, actor="b@x.com", action="rename",
                       before={"display_name": "New"}, after={"display_name": "Newer"}, note="renamed")
    rows = store.list_audit("aws", "123456789012")
    assert [r["action"] for r in rows] == ["rename", "create"]
    assert rows[0]["note"] == "renamed"
    assert "SECRET-VALUE" not in repr(rows) and "S2" not in repr(rows)
    assert rows[1]["after"] == {"display_name": "New", "nested": {}}


def test_audit_list_is_scoped_to_one_account_and_honours_the_limit(store):
    k1, k2 = store.make_key("aws", "111111111111"), store.make_key("aws", "222222222222")
    for i in range(5):
        store.append_audit(k1, actor="a", action=f"a{i}")
    store.append_audit(k2, actor="a", action="other")
    assert len(store.list_audit("aws", "111111111111", limit=3)) == 3
    assert [r["action"] for r in store.list_audit("aws", "222222222222")] == ["other"]


def test_audit_write_failure_never_raises(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(store, "_local_audit_add", boom)
    monkeypatch.setattr(store, "_ddb_audit_add", boom)
    store.append_audit("aws#1", actor="a", action="x")  # must not raise
