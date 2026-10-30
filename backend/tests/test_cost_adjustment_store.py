"""
Tests for app/cost_adjustment_store.py — local (SQLite) CRUD path.

Runs with ENV=local (the test suite's default), which routes
cost_adjustment_store through database.py's SQLAlchemy models rather than
DynamoDB. To keep these tests fully isolated from the real dev
`job_portal.db` file (and from each other), each test gets its own
in-memory SQLite engine/session, monkeypatched directly onto the
`cost_adjustment_store` module (it imported `SessionLocal` by name at
import time, so the module-level name — not database.py's — is what must
be patched).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import cost_adjustment_store as store
from app.database import Base


@pytest.fixture
def isolated_db(monkeypatch):
    """Fresh in-memory SQLite DB per test, wired into cost_adjustment_store
    in place of the real ./job_portal.db file."""
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


def test_create_and_get_adjustment(isolated_db):
    created = store.create_adjustment(
        account_id="111111111111", period="2026-08", label="Partner invoice",
        amount=123.45, currency="USD", note="Q3 support fee",
        actor_email="admin@example.com", actor_name="Admin User",
    )
    assert created["account_id"] == "111111111111"
    assert created["period"] == "2026-08"
    assert created["label"] == "Partner invoice"
    assert created["amount"] == 123.45
    assert created["currency"] == "USD"
    assert created["note"] == "Q3 support fee"
    assert created["created_by_email"] == "admin@example.com"
    assert created["created_by_name"] == "Admin User"
    assert created["updated_by_email"] is None
    assert "id" in created and created["id"]

    fetched = store.get_adjustment(created["id"])
    assert fetched == created


def test_get_adjustment_missing_returns_none(isolated_db):
    assert store.get_adjustment("does-not-exist") is None


def test_list_adjustments_filters_by_account_and_sorts_newest_first(isolated_db):
    a1 = store.create_adjustment(
        account_id="111111111111", period="2026-06", label="Old",
        amount=10.0, currency="USD", note=None,
        actor_email="a@example.com", actor_name="A",
    )
    a2 = store.create_adjustment(
        account_id="111111111111", period="2026-08", label="New",
        amount=20.0, currency="USD", note=None,
        actor_email="a@example.com", actor_name="A",
    )
    other_account = store.create_adjustment(
        account_id="222222222222", period="2026-08", label="Other account",
        amount=30.0, currency="USD", note=None,
        actor_email="a@example.com", actor_name="A",
    )

    all_items = store.list_adjustments()
    assert len(all_items) == 3

    filtered = store.list_adjustments(account_id="111111111111")
    assert {i["id"] for i in filtered} == {a1["id"], a2["id"]}
    # Newest period first.
    assert filtered[0]["id"] == a2["id"]
    assert filtered[1]["id"] == a1["id"]

    assert [i["id"] for i in store.list_adjustments(account_id="222222222222")] == [other_account["id"]]


def test_update_adjustment_partial_fields(isolated_db):
    created = store.create_adjustment(
        account_id="111111111111", period="2026-08", label="Original label",
        amount=50.0, currency="USD", note="original note",
        actor_email="admin@example.com", actor_name="Admin",
    )

    updated = store.update_adjustment(
        created["id"], label="Updated label", amount=None, currency=None, note=None,
        actor_email="editor@example.com", actor_name="Editor",
    )
    assert updated["label"] == "Updated label"
    # Untouched fields remain as before.
    assert updated["amount"] == 50.0
    assert updated["currency"] == "USD"
    assert updated["note"] == "original note"
    assert updated["updated_by_email"] == "editor@example.com"
    assert updated["updated_by_name"] == "Editor"

    updated2 = store.update_adjustment(
        created["id"], label=None, amount=75.5, currency="EUR", note="new note",
        actor_email="editor2@example.com", actor_name="Editor2",
    )
    assert updated2["label"] == "Updated label"  # unchanged from previous update
    assert updated2["amount"] == 75.5
    assert updated2["currency"] == "EUR"
    assert updated2["note"] == "new note"


def test_update_adjustment_missing_returns_none(isolated_db):
    result = store.update_adjustment(
        "does-not-exist", label="X", amount=None, currency=None, note=None,
        actor_email="a@example.com", actor_name="A",
    )
    assert result is None


def test_delete_adjustment(isolated_db):
    created = store.create_adjustment(
        account_id="111111111111", period="2026-08", label="To delete",
        amount=1.0, currency="USD", note=None,
        actor_email="a@example.com", actor_name="A",
    )
    assert store.delete_adjustment(created["id"]) is True
    assert store.get_adjustment(created["id"]) is None
    # Deleting again returns False, doesn't raise.
    assert store.delete_adjustment(created["id"]) is False


@pytest.mark.parametrize("bad_amount", ["not-a-number", float("nan"), float("inf"), float("-inf")])
def test_safe_amount_rejects_invalid_values(isolated_db, bad_amount):
    with pytest.raises(ValueError):
        store.create_adjustment(
            account_id="111111111111", period="2026-08", label="Bad amount",
            amount=bad_amount, currency="USD", note=None,
            actor_email="a@example.com", actor_name="A",
        )


def test_amount_rounded_to_2_decimal_places(isolated_db):
    created = store.create_adjustment(
        account_id="111111111111", period="2026-08", label="Rounding check",
        amount=10.999, currency="USD", note=None,
        actor_email="a@example.com", actor_name="A",
    )
    assert created["amount"] == 11.0

    created2 = store.create_adjustment(
        account_id="111111111111", period="2026-08", label="Rounding check 2",
        amount=10.001, currency="USD", note=None,
        actor_email="a@example.com", actor_name="A",
    )
    assert created2["amount"] == 10.0
