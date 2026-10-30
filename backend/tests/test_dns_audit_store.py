"""
Tests for app/dns_audit_store.py — the append-only audit trail for the
DNS Record tool (ENV=local / SQLite path only, mirroring
test_whitelist_store.py's isolated-in-memory-engine approach exactly).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.dns_audit_store as dns_audit_store_module
from app.database import Base


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(dns_audit_store_module, "SessionLocal", TestSessionLocal)
    yield TestSessionLocal
    engine.dispose()


def _create(**overrides):
    kwargs = dict(
        action="create", zone_id="Z0861165ABC", zone_name="example-site-a.com",
        record_name="www.example-site-a.com.", record_type="A",
        ttl="300", value="203.0.113.10", sr_number="SR-000123",
        change_id="C0123456789", result="created",
        actor_email="alice@example.com", actor_name="Alice",
    )
    kwargs.update(overrides)
    return dns_audit_store_module.record(**kwargs)


def test_record_and_list_round_trip():
    _create()
    records = dns_audit_store_module.list_records()
    assert len(records) == 1
    r = records[0]
    assert r["action"] == "create"
    assert r["zone_name"] == "example-site-a.com"
    assert r["record_type"] == "A"
    assert r["value"] == "203.0.113.10"
    assert r["sr_number"] == "SR-000123"
    assert r["change_id"] == "C0123456789"
    assert r["result"] == "created"
    assert r["previous_value"] is None
    assert r["created_at"] is not None


def test_modify_keeps_both_new_and_previous_value():
    """A DNS modify destroys the old value and Route 53 keeps no history
    of its own, so "what was it before?" is only answerable if it was
    captured at write time."""
    _create(
        action="modify", result="modified",
        value="203.0.113.77", previous_value="203.0.113.10, 203.0.113.11",
    )
    r = dns_audit_store_module.list_records()[0]
    assert r["value"] == "203.0.113.77"
    assert r["previous_value"] == "203.0.113.10, 203.0.113.11"


def test_delete_records_the_removed_value_as_previous_value():
    """A deleted record has no current value — putting its payload in
    `value` would make an audit reader think it was still live."""
    _create(
        action="delete", result="deleted",
        value=None, previous_value="203.0.113.10",
    )
    r = dns_audit_store_module.list_records()[0]
    assert r["value"] is None
    assert r["previous_value"] == "203.0.113.10"


def test_failed_write_is_recorded_with_error_detail():
    _create(
        action="create_failed", result="error",
        detail="Route 53 rejected this change: it already exists",
        change_id=None, value=None,
    )
    r = dns_audit_store_module.list_records()[0]
    assert r["result"] == "error"
    assert "already exists" in r["detail"]


def test_ttl_is_stored_as_a_string_even_when_given_an_int():
    """DynamoDB returns numeric attributes as decimal.Decimal, which
    FastAPI cannot serialise — so TTL is normalised to text in both the
    SQLite and DynamoDB paths to keep the two identical."""
    _create(ttl=300)
    assert dns_audit_store_module.list_records()[0]["ttl"] == "300"


def test_alias_record_keeps_null_ttl():
    _create(ttl=None, value="ALIAS -> d1.cloudfront.net (zone Z2FDTNDATAQYW2)")
    r = dns_audit_store_module.list_records()[0]
    assert r["ttl"] is None
    assert r["value"].startswith("ALIAS ->")


def test_list_records_returns_newest_first():
    _create(record_name="first.example-site-a.com.")
    _create(record_name="second.example-site-a.com.")
    records = dns_audit_store_module.list_records()
    assert records[0]["record_name"] == "second.example-site-a.com."
    assert records[1]["record_name"] == "first.example-site-a.com."


def test_list_records_respects_limit():
    for i in range(5):
        _create(record_name=f"h{i}.example-site-a.com.")
    assert len(dns_audit_store_module.list_records(limit=2)) == 2


def test_list_records_empty_when_nothing_recorded():
    assert dns_audit_store_module.list_records() == []


def test_reset_for_tests_clears_the_table():
    _create()
    dns_audit_store_module.reset_for_tests()
    assert dns_audit_store_module.list_records() == []


def test_record_shape_rejects_positional_arguments():
    """Every field is keyword-only so a caller can never transpose two
    same-typed arguments (record_name/record_type, value/previous_value)
    and produce an audit row that is quietly wrong rather than obviously
    broken."""
    with pytest.raises(TypeError):
        dns_audit_store_module._record_shape(
            "create", "Z1", "example.com", "www.example.com.", "A",
            "created", "a@b.com", "A B",
        )
