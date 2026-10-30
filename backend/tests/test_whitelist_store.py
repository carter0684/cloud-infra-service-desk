"""
Tests for app/whitelist_store.py — the append-only audit trail for the
Whitelist (LMS) tool (ENV=local / SQLite path only, mirroring
test_cmdb_store.py's isolated-in-memory-engine approach exactly).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.whitelist_store as whitelist_store_module
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
    monkeypatch.setattr(whitelist_store_module, "SessionLocal", TestSessionLocal)
    yield TestSessionLocal
    engine.dispose()


def test_record_and_list_round_trip():
    whitelist_store_module.record(
        action="check", target_id="newlms_backend", target_label="newlms-prod-ipset-whitelist-backend",
        region="us-east-1", ip_address="1.2.3.4", result="not_whitelisted",
        actor_email="alice@example.com", actor_name="Alice",
    )
    records = whitelist_store_module.list_records()
    assert len(records) == 1
    r = records[0]
    assert r["action"] == "check"
    assert r["target_id"] == "newlms_backend"
    assert r["ip_address"] == "1.2.3.4"
    assert r["result"] == "not_whitelisted"
    assert r["actor_email"] == "alice@example.com"
    assert r["sr_number"] is None
    assert r["created_at"] is not None


def test_record_stores_sr_number_and_detail_for_add_actions():
    whitelist_store_module.record(
        action="add", target_id="prod_wasapp", target_label="PROD-WASAPP-Whitelist",
        region="ap-southeast-1", ip_address="5.6.7.8", sr_number="SR-000123",
        result="added", actor_email="bob@example.com", actor_name="Bob",
    )
    records = whitelist_store_module.list_records()
    assert records[0]["sr_number"] == "SR-000123"
    assert records[0]["result"] == "added"


def test_record_stores_error_detail_for_add_failed_actions():
    whitelist_store_module.record(
        action="add_failed", target_id="prod_wasapp", target_label="PROD-WASAPP-Whitelist",
        region="ap-southeast-1", ip_address="5.6.7.8", sr_number="SR-000999",
        result="error", detail="Could not update IPSet: boom",
        actor_email="carol@example.com", actor_name="Carol",
    )
    records = whitelist_store_module.list_records()
    assert records[0]["result"] == "error"
    assert "boom" in records[0]["detail"]


def test_list_records_returns_newest_first():
    whitelist_store_module.record(
        action="check", target_id="a", target_label="a-label", region="us-east-1", ip_address="1.1.1.1",
        result="not_whitelisted", actor_email="x@example.com", actor_name="X",
    )
    whitelist_store_module.record(
        action="check", target_id="b", target_label="b-label", region="us-east-1", ip_address="2.2.2.2",
        result="not_whitelisted", actor_email="x@example.com", actor_name="X",
    )
    records = whitelist_store_module.list_records()
    assert records[0]["target_id"] == "b"
    assert records[1]["target_id"] == "a"


def test_list_records_respects_limit():
    for i in range(5):
        whitelist_store_module.record(
            action="check", target_id=f"t{i}", target_label=f"label{i}", region="us-east-1",
            ip_address=f"1.1.1.{i}", result="not_whitelisted", actor_email="x@example.com", actor_name="X",
        )
    records = whitelist_store_module.list_records(limit=2)
    assert len(records) == 2


def test_list_records_empty_when_nothing_recorded():
    assert whitelist_store_module.list_records() == []
