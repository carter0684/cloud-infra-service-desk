"""
Tests for app/cmdb_store.py — pure diff logic + local (SQLite) snapshot/
history persistence path.

Runs with ENV=local (the test suite's default), which routes cmdb_store
through database.py's SQLAlchemy models (CmdbSnapshot / CmdbHistoryEvent)
rather than DynamoDB. Each test gets its own in-memory SQLite engine,
monkeypatched directly onto the `cmdb_store` module (it imported
`SessionLocal` by name at import time, so the module-level name — not
database.py's — is what must be patched), mirroring
test_cost_adjustment_store.py's isolation approach exactly.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import cmdb_store as store
from app.database import Base


@pytest.fixture
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(store, "SessionLocal", TestSessionLocal)
    yield TestSessionLocal
    engine.dispose()


def _res(rtype, rid, name="n", region="ap-southeast-1", status="running"):
    return {"resource_type": rtype, "resource_id": rid, "name": name, "region": region, "status": status, "tags": {}}


# ── Pure diff logic (no I/O) ──────────────────────────────────────────────

def test_diff_resources_detects_added():
    events = store._diff_resources([], [_res("EC2 Instance", "i-1")])
    assert len(events) == 1
    assert events[0]["event_type"] == "added"
    assert events[0]["resource_id"] == "i-1"
    assert events[0]["previous_status"] is None


def test_diff_resources_detects_removed():
    events = store._diff_resources([_res("EC2 Instance", "i-1")], [])
    assert len(events) == 1
    assert events[0]["event_type"] == "removed"
    assert events[0]["resource_id"] == "i-1"
    assert events[0]["status"] is None
    assert events[0]["previous_status"] == "running"


def test_diff_resources_detects_status_changed():
    prev = [_res("EC2 Instance", "i-1", status="running")]
    curr = [_res("EC2 Instance", "i-1", status="stopped")]
    events = store._diff_resources(prev, curr)
    assert len(events) == 1
    assert events[0]["event_type"] == "status_changed"
    assert events[0]["previous_status"] == "running"
    assert events[0]["status"] == "stopped"


def test_diff_resources_no_change_yields_no_events():
    prev = [_res("EC2 Instance", "i-1", status="running")]
    curr = [_res("EC2 Instance", "i-1", status="running")]
    assert store._diff_resources(prev, curr) == []


def test_diff_resources_same_id_different_type_is_add_and_remove():
    # Identity key is (resource_type, resource_id) — an S3 bucket and an
    # EC2 instance that happen to share a name/id string are unrelated.
    prev = [_res("EC2 Instance", "shared-name")]
    curr = [_res("S3 Bucket", "shared-name")]
    events = store._diff_resources(prev, curr)
    types = {e["event_type"] for e in events}
    assert types == {"added", "removed"}


def test_diff_resources_handles_multiple_resources_mixed_changes():
    prev = [
        _res("EC2 Instance", "i-1", status="running"),
        _res("EC2 Instance", "i-2", status="running"),
        _res("RDS Instance", "db-1", status="available"),
    ]
    curr = [
        _res("EC2 Instance", "i-1", status="stopped"),   # status changed
        _res("RDS Instance", "db-1", status="available"),  # unchanged
        _res("Lambda Function", "fn-1", status="Active"),  # added
        # i-2 removed
    ]
    events = store._diff_resources(prev, curr)
    by_id = {e["resource_id"]: e for e in events}
    assert by_id["i-1"]["event_type"] == "status_changed"
    assert by_id["i-2"]["event_type"] == "removed"
    assert by_id["fn-1"]["event_type"] == "added"
    assert "db-1" not in by_id


# ── Local (SQLite) persistence path ───────────────────────────────────────

def test_get_snapshot_empty_for_unknown_account(isolated_db):
    assert store.get_snapshot("999999999999") == []


def test_diff_and_record_first_fetch_records_all_as_added(isolated_db):
    current = [_res("EC2 Instance", "i-1"), _res("RDS Instance", "db-1")]
    events = store.diff_and_record("111111111111", current)
    assert len(events) == 2
    assert all(e["event_type"] == "added" for e in events)

    history = store.list_history("111111111111")
    assert len(history) == 2
    assert {h["resource_id"] for h in history} == {"i-1", "db-1"}


def test_diff_and_record_saves_snapshot_for_next_diff(isolated_db):
    store.diff_and_record("111111111111", [_res("EC2 Instance", "i-1", status="running")])
    snapshot = store.get_snapshot("111111111111")
    assert len(snapshot) == 1
    assert snapshot[0]["resource_id"] == "i-1"

    # Second fetch: same resource, different status -> one status_changed
    # event, no duplicate "added" event.
    events2 = store.diff_and_record("111111111111", [_res("EC2 Instance", "i-1", status="stopped")])
    assert len(events2) == 1
    assert events2[0]["event_type"] == "status_changed"

    history = store.list_history("111111111111")
    assert len(history) == 2  # 1 added (first fetch) + 1 status_changed (second fetch)


def test_diff_and_record_no_changes_between_fetches_records_nothing_new(isolated_db):
    resources = [_res("EC2 Instance", "i-1")]
    store.diff_and_record("111111111111", resources)
    events2 = store.diff_and_record("111111111111", resources)
    assert events2 == []
    assert len(store.list_history("111111111111")) == 1  # only the original "added" event


def test_list_history_filters_by_account(isolated_db):
    store.diff_and_record("111111111111", [_res("EC2 Instance", "i-1")])
    store.diff_and_record("222222222222", [_res("EC2 Instance", "i-2")])

    assert len(store.list_history()) == 2
    assert [h["resource_id"] for h in store.list_history("111111111111")] == ["i-1"]
    assert [h["resource_id"] for h in store.list_history("222222222222")] == ["i-2"]


def test_list_history_newest_first(isolated_db):
    store.diff_and_record("111111111111", [_res("EC2 Instance", "i-1", status="running")])
    store.diff_and_record("111111111111", [_res("EC2 Instance", "i-1", status="stopped")])
    history = store.list_history("111111111111")
    assert len(history) == 2
    assert history[0]["event_type"] == "status_changed"  # most recent event first
    assert history[1]["event_type"] == "added"


def test_list_history_respects_limit(isolated_db):
    for i in range(5):
        store.diff_and_record(f"11111111111{i}", [_res("EC2 Instance", f"i-{i}")])
    assert len(store.list_history(limit=3)) == 3


def test_reset_for_tests_clears_both_tables(isolated_db):
    store.diff_and_record("111111111111", [_res("EC2 Instance", "i-1")])
    store.reset_for_tests()
    assert store.get_snapshot("111111111111") == []
    assert store.list_history() == []
