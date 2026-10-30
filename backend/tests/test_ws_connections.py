"""
Tests for app/ws_connections.py — WebSocket connection registry
(job-portal-chat-voice spec).

Uses the `chat_tables` fixture (conftest.py), which sets ENV=production
and spins up moto-mocked DynamoDB tables matching data_stack.py's
WsConnectionsTable schema (including UserEmailIndex) — so these tests
exercise the production (DynamoDB) branch of ws_connections.py.
"""

from __future__ import annotations

import importlib

import pytest
from freezegun import freeze_time


@pytest.fixture
def ws_connections(chat_tables):
    """Reload the module fresh so its module-level TABLE_NAME constant
    picks up the ENV=production env var set by the chat_tables fixture —
    same pattern as test_chat_store.py's chat_store fixture."""
    from app import ws_connections as ws_connections_module
    importlib.reload(ws_connections_module)
    return ws_connections_module


ALICE = "alice@example.com"
BOB = "bob@example.com"


class TestRegisterAndGet:
    def test_register_connection_creates_a_row(self, ws_connections):
        conn = ws_connections.register_connection(user_email=ALICE)
        assert conn["user_email"] == ALICE
        assert conn["connection_id"]
        assert conn["connected_at"] is not None
        assert conn["last_heartbeat_at"] is not None

    def test_register_connection_accepts_an_explicit_connection_id(self, ws_connections):
        conn = ws_connections.register_connection(user_email=ALICE, connection_id="my-fixed-id")
        assert conn["connection_id"] == "my-fixed-id"

    def test_register_normalises_email_case_and_whitespace(self, ws_connections):
        conn = ws_connections.register_connection(user_email="  Alice@example.com  ")
        assert conn["user_email"] == "alice@example.com"


class TestListConnectionsForUser:
    def test_returns_only_this_users_connections(self, ws_connections):
        ws_connections.register_connection(user_email=ALICE)
        ws_connections.register_connection(user_email=BOB)

        alice_conns = ws_connections.list_connections_for_user(ALICE)
        assert len(alice_conns) == 1
        assert alice_conns[0]["user_email"] == ALICE

    def test_returns_no_connections_for_a_user_with_none(self, ws_connections):
        assert ws_connections.list_connections_for_user("nobody@example.com") == []

    def test_multiple_simultaneous_connections_per_user_are_all_returned(self, ws_connections):
        """Requirement 8.7: a Team_Member can have multiple simultaneous
        WS_Connections (e.g. desktop and mobile at once) — all of them
        must be discoverable, not just the most recent one."""
        ws_connections.register_connection(user_email=ALICE, connection_id="conn-desktop")
        ws_connections.register_connection(user_email=ALICE, connection_id="conn-mobile")

        conns = ws_connections.list_connections_for_user(ALICE)
        ids = {c["connection_id"] for c in conns}
        assert ids == {"conn-desktop", "conn-mobile"}

    def test_is_user_online_true_with_at_least_one_fresh_connection(self, ws_connections):
        ws_connections.register_connection(user_email=ALICE)
        assert ws_connections.is_user_online(ALICE) is True

    def test_is_user_online_false_with_no_connections(self, ws_connections):
        assert ws_connections.is_user_online(ALICE) is False


class TestHeartbeat:
    def test_heartbeat_on_existing_connection_returns_true(self, ws_connections):
        conn = ws_connections.register_connection(user_email=ALICE)
        assert ws_connections.heartbeat(conn["connection_id"]) is True

    def test_heartbeat_on_unknown_connection_returns_false(self, ws_connections):
        assert ws_connections.heartbeat("does-not-exist") is False

    def test_heartbeat_refreshes_ttl_so_connection_stays_fresh(self, ws_connections):
        """A connection that keeps heartbeating must remain in
        list_connections_for_user()'s results even past the staleness
        window measured from its ORIGINAL register time."""
        with freeze_time("2026-08-01 10:00:00") as frozen:
            conn = ws_connections.register_connection(user_email=ALICE)

            # advance well past STALE_CONNECTION_SECONDS since registration...
            frozen.move_to("2026-08-01 10:01:00")  # +60s
            # ...but heartbeat right before checking, refreshing the TTL
            ws_connections.heartbeat(conn["connection_id"])

            conns = ws_connections.list_connections_for_user(ALICE)
            assert len(conns) == 1


class TestUnregisterConnection:
    def test_unregister_removes_the_connection_immediately(self, ws_connections):
        conn = ws_connections.register_connection(user_email=ALICE)
        ws_connections.unregister_connection(conn["connection_id"])

        assert ws_connections.list_connections_for_user(ALICE) == []
        assert ws_connections.is_user_online(ALICE) is False

    def test_unregister_on_unknown_connection_does_not_error(self, ws_connections):
        ws_connections.unregister_connection("does-not-exist")  # must not raise

    def test_unregister_only_removes_the_named_connection(self, ws_connections):
        ws_connections.register_connection(user_email=ALICE, connection_id="conn-a")
        ws_connections.register_connection(user_email=ALICE, connection_id="conn-b")

        ws_connections.unregister_connection("conn-a")

        remaining = ws_connections.list_connections_for_user(ALICE)
        assert len(remaining) == 1
        assert remaining[0]["connection_id"] == "conn-b"


class TestStaleness:
    def test_a_connection_with_no_heartbeat_becomes_stale_after_the_window(self, ws_connections):
        """Requirement 8.2: a WS_Connection that never re-establishes
        within (approximately) 30 seconds is marked offline — this
        module implements that via a 45-second staleness cutoff with
        margin, see module docstring."""
        with freeze_time("2026-08-01 10:00:00") as frozen:
            ws_connections.register_connection(user_email=ALICE, connection_id="conn-a")

            frozen.move_to("2026-08-01 10:00:10")  # +10s — still fresh
            assert len(ws_connections.list_connections_for_user(ALICE)) == 1

            frozen.move_to("2026-08-01 10:01:00")  # +60s total — past the 45s cutoff
            assert ws_connections.list_connections_for_user(ALICE) == []
            assert ws_connections.is_user_online(ALICE) is False


class TestGetPresenceState:
    def test_online_when_a_fresh_connection_exists_and_not_in_call(self, ws_connections):
        ws_connections.register_connection(user_email=ALICE)
        assert ws_connections.get_presence_state(ALICE) == "online"

    def test_offline_when_no_connection_exists(self, ws_connections):
        assert ws_connections.get_presence_state(ALICE) == "offline"

    def test_offline_even_if_in_active_call_flag_is_false_and_no_connection(self, ws_connections):
        assert ws_connections.get_presence_state(ALICE, in_active_call=False) == "offline"

    def test_in_call_takes_priority_over_online(self, ws_connections):
        """Requirement 8.3: a Team_Member joined to an active Call is
        reported as "in_call", not merely "online", even though they
        also have a fresh WS_Connection (which they always do while in
        a call)."""
        ws_connections.register_connection(user_email=ALICE)
        assert ws_connections.get_presence_state(ALICE, in_active_call=True) == "in_call"

    def test_in_call_even_without_a_ws_connection(self, ws_connections):
        """Edge case: in_active_call=True should report "in_call"
        regardless of the WS_Connection state — the flag is
        authoritative once True, per get_presence_state()'s own
        docstring (in_active_call takes priority)."""
        assert ws_connections.get_presence_state(ALICE, in_active_call=True) == "in_call"
