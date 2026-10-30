"""
Tests for GET /api/users/directory (job-portal-chat-voice Task 8) —
main.py's list_users_directory() route.

main.py is imported here (a first for this test suite — see the
docstring note below on why that's safe). No other test file imports
app.main, deliberately: main.py's @app.on_event("startup") hook (NOT
run merely by importing the module, only when a real ASGI lifecycle
starts) is what calls database.init_db()/_migrate_db() against the
real local ./job_portal.db SQLite file — TestClient(app) used as a
plain call (not a `with` context manager) does NOT trigger startup
events, so no real DB file is touched. user_store.list_users() is
monkeypatched below for the same reason: it would otherwise call
_seed_defaults_if_empty(), which DOES write to the real SQLite file.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app import user_store
from app.database import Base

ADMIN_USER = {
    "email": "admin@example.com",
    "name": "Admin Person",
    "role": "admin",
    "added_by": "system",
    "created_at": "2026-01-01T00:00:00",
    "photo_data_url": "data:image/jpeg;base64,AAA",
    "groups": ["management-team"],
    "last_login": "2026-08-01T00:00:00",
    "tab_write": ["cost-analysis"],
}
MEMBER_USER = {
    "email": "member@example.com",
    "name": "Member Person",
    "role": "member",
    "added_by": "admin@example.com",
    "created_at": "2026-01-02T00:00:00",
    "photo_data_url": None,
    "groups": [],
    "last_login": None,
    "tab_write": [],
}


@pytest.fixture
def fake_directory(monkeypatch):
    """Replace user_store.list_users() with a fixed, fully-populated
    (including every admin-only field) fake roster so this test never
    touches the real local SQLite DB and can assert precisely on which
    fields the route strips out.

    Also forces ENV=local and reloads main.py fresh — this test module
    is NOT the only one in the suite that imports app.main any more
    (test_chat_routes.py / test_call_routes.py's `chat_app` fixture in
    conftest.py also does, under ENV=production, which swaps
    main.get_actor to the real OIDC-header-based auth_alb.get_actor and
    installs dependency_overrides on that reloaded main.app object). If
    one of those tests runs earlier in the same pytest session, this
    file's own `from app import main` import at module load time would
    otherwise still be pointing at THAT altered module object. Reloading
    here, unconditionally, makes this file self-contained regardless of
    test execution order — matching every other *_store.py fixture's
    "reload after setting the env var this module reads at import time"
    convention elsewhere in this suite."""
    monkeypatch.setenv("ENV", "local")
    importlib.reload(main)
    # Task 13.3-adjacent fix (Requirement 8.4): list_users_directory()
    # now also calls _get_presence_for_email(), which reaches into BOTH
    # call_store.py and ws_connections.py — both ENV-dependent at
    # import time, same as main.py itself. Without reloading them here
    # too, they can be left over from whichever OTHER test module ran
    # earlier in this same pytest session and reloaded them under
    # ENV=production (e.g. test_ws_route.py's `chat_app` fixture in
    # conftest.py) — causing a real boto3/DynamoDB call here instead of
    # the local SQLite branch, which fails outright with no AWS
    # credentials configured in this test environment.
    from app import call_store as call_store_module
    from app import ws_connections as ws_connections_module
    importlib.reload(call_store_module)
    importlib.reload(ws_connections_module)

    # Same reload's local-SQLite branch still needs an actual
    # `call_sessions` table to query against — main.py's own
    # @app.on_event("startup") hook (database.init_db()) is what
    # normally creates every table against the real ./job_portal.db
    # file, but per this fixture's own docstring above, TestClient(app)
    # used as a plain function call never triggers that. A dedicated
    # in-memory engine (same StaticPool pattern every other *_store.py
    # test fixture in this suite uses) is monkeypatched directly onto
    # call_store_module's SessionLocal name so
    # is_user_in_any_active_call() below has a real (empty) table to
    # scan instead of erroring on a missing one.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(call_store_module, "SessionLocal", TestSessionLocal)
    # ws_connections.py's local branch does `from .database import
    # SessionLocal` too (its OWN copy of the name, independent of
    # call_store_module's — each module binds it into its own
    # namespace at import time), so it needs the same monkeypatch
    # separately; Base.metadata.create_all() above already created
    # every table (including WsConnection's) against this one shared
    # in-memory engine, so both modules end up pointed at the same
    # empty database.
    monkeypatch.setattr(ws_connections_module, "SessionLocal", TestSessionLocal)

    monkeypatch.setattr(user_store, "list_users", lambda: [ADMIN_USER, MEMBER_USER])
    # get_actor_local() (ENV=local) requires the caller's X-Actor-Email
    # header to be an allowlisted user — TEAM_MEMBERS is a live view
    # backed by user_store.is_allowed(), so patch that too rather than
    # relying on any real seeded allowlist data.
    monkeypatch.setattr(user_store, "is_allowed", lambda email: email in {ADMIN_USER["email"], MEMBER_USER["email"]})


class TestListUsersDirectoryRouteFunctionDirectly:
    """Calls main.list_users_directory() as a plain function — the
    dependency (Depends(get_actor)) resolution itself is exercised
    separately below via TestClient, so this class isolates just the
    filtering behaviour Task 8.1 asks for."""

    def test_returns_only_email_name_photo_data_url_and_presence(self, fake_directory):
        # Neither fake user has ever registered a WS connection or joined
        # a call in this test's isolated store state, so both resolve to
        # "offline" — see TestPresenceInDirectory below for the
        # online/in_call cases.
        result = main.list_users_directory(actor=MEMBER_USER["email"])
        assert result == {
            "users": [
                {"email": ADMIN_USER["email"], "name": ADMIN_USER["name"], "photo_data_url": ADMIN_USER["photo_data_url"], "presence": "offline"},
                {"email": MEMBER_USER["email"], "name": MEMBER_USER["name"], "photo_data_url": None, "presence": "offline"},
            ]
        }

    def test_admin_only_fields_are_absent(self, fake_directory):
        result = main.list_users_directory(actor=MEMBER_USER["email"])
        for entry in result["users"]:
            for admin_only_field in ("role", "added_by", "groups", "last_login", "tab_write", "created_at"):
                assert admin_only_field not in entry

    def test_presence_field_is_present(self, fake_directory):
        """Requirement 8.4: every entry carries a presence snapshot,
        one of the three states get_presence_state() can return."""
        result = main.list_users_directory(actor=MEMBER_USER["email"])
        for entry in result["users"]:
            assert entry["presence"] in ("online", "in_call", "offline")

    def test_missing_name_falls_back_to_email(self, fake_directory, monkeypatch):
        monkeypatch.setattr(user_store, "list_users", lambda: [{**MEMBER_USER, "name": ""}])
        result = main.list_users_directory(actor=MEMBER_USER["email"])
        assert result["users"][0]["name"] == MEMBER_USER["email"]


class TestListUsersDirectoryEndpointViaHttp:
    """Full FastAPI dependency-injection path via TestClient — confirms
    a genuinely NON-admin caller (get_actor resolves them, no
    require_admin/require_user_admin dependency involved) can reach the
    route over real HTTP, per Task 8.2's literal ask."""

    def test_non_admin_caller_gets_200_and_filtered_body(self, fake_directory):
        client = TestClient(main.app)
        resp = client.get("/api/users/directory", headers={"X-Actor-Email": MEMBER_USER["email"]})
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "users": [
                {"email": ADMIN_USER["email"], "name": ADMIN_USER["name"], "photo_data_url": ADMIN_USER["photo_data_url"], "presence": "offline"},
                {"email": MEMBER_USER["email"], "name": MEMBER_USER["name"], "photo_data_url": None, "presence": "offline"},
            ]
        }
        for entry in body["users"]:
            for admin_only_field in ("role", "added_by", "groups", "last_login", "tab_write"):
                assert admin_only_field not in entry

    def test_unauthenticated_caller_is_rejected(self, fake_directory):
        """Requirement: still requires SOME valid actor — just not an
        admin one — so a request with no/invalid X-Actor-Email must
        still 401, same as every other Depends(get_actor) route."""
        client = TestClient(main.app)
        resp = client.get("/api/users/directory")
        assert resp.status_code == 401
