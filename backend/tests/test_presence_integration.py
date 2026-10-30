"""
Integration test for Presence transitions (Task 6.2, job-portal-chat-voice
spec) — exercises ws_connections.py's get_presence_state() combined with
call_store.py's is_user_in_any_active_call() the same way a route
handler composes them (see both modules' docstrings on why they don't
import each other directly).

Walks through the full Requirement 8.1/8.2/8.3 lifecycle in one test:
  connect            -> "online"
  join an active call -> "in_call"
  leave the call, still connected -> "online"
  disconnect (unregister) -> "offline"
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time


@pytest.fixture
def ws_connections(chat_tables):
    from app import ws_connections as ws_connections_module
    importlib.reload(ws_connections_module)
    return ws_connections_module


@pytest.fixture
def call_store(chat_tables):
    from app import call_store as call_store_module
    importlib.reload(call_store_module)
    return call_store_module


@pytest.fixture
def mock_chime(call_store, monkeypatch):
    mock = MagicMock()
    counter = {"meeting": 0, "attendee": 0}

    def _create_meeting(call_id, media_region=None):
        counter["meeting"] += 1
        return {"chime_meeting_id": f"meeting-{counter['meeting']}", "media_region": "us-east-1", "media_placement": {}}

    def _add_attendee(meeting_id, user_email):
        counter["attendee"] += 1
        return {"attendee_id": f"attendee-{counter['attendee']}", "join_token": f"token-{counter['attendee']}"}

    mock.create_meeting_for_call.side_effect = _create_meeting
    mock.add_attendee.side_effect = _add_attendee
    mock.delete_meeting.return_value = None

    monkeypatch.setattr(call_store, "chime_client", mock)
    return mock


ALICE = "alice@example.com"
BOB = "bob@example.com"
CONVERSATION_ID = "conv-1"


def _presence(ws_connections, call_store, email: str) -> str:
    """Mirrors how a route handler would compose these two modules —
    check call_store first, then pass the result into
    ws_connections.get_presence_state()."""
    in_call = call_store.is_user_in_any_active_call(email)
    return ws_connections.get_presence_state(email, in_active_call=in_call)


class TestFullPresenceLifecycle:
    def test_connect_join_call_leave_call_disconnect(self, ws_connections, call_store, mock_chime):
        # Requirement 8.1: not connected at all yet.
        assert _presence(ws_connections, call_store, BOB) == "offline"

        # connect -> "online"
        conn = ws_connections.register_connection(user_email=BOB)
        assert _presence(ws_connections, call_store, BOB) == "online"

        # a call is created and BOB joins it -> "in_call"
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=ALICE, callee_emails=[BOB], call_type="direct"
        )
        call_store.accept_call(call["call_id"], callee_email=BOB)
        assert _presence(ws_connections, call_store, BOB) == "in_call"

        # BOB leaves the call but stays connected -> back to "online"
        call_store.leave_call(call["call_id"], participant_email=BOB)
        assert _presence(ws_connections, call_store, BOB) == "online"

        # BOB disconnects cleanly -> "offline" immediately (no need to
        # wait out the staleness window for a CLEAN disconnect —
        # unregister_connection() removes the row right away).
        ws_connections.unregister_connection(conn["connection_id"])
        assert _presence(ws_connections, call_store, BOB) == "offline"

    def test_unclean_disconnect_becomes_offline_within_the_stale_window(self, ws_connections, call_store, mock_chime):
        """Requirement 8.2: an UNCLEAN disconnect (network loss — no
        close frame, no unregister_connection() call) must still be
        reflected as "offline" once the connection's heartbeat goes
        stale, within the 30-second window (implemented here with the
        45-second margin — see ws_connections.py's module docstring)."""
        with freeze_time("2026-08-01 10:00:00") as frozen:
            ws_connections.register_connection(user_email=BOB, connection_id="bob-conn")
            assert _presence(ws_connections, call_store, BOB) == "online"

            frozen.move_to("2026-08-01 10:01:00")  # +60s, no heartbeat sent — connection goes stale
            assert _presence(ws_connections, call_store, BOB) == "offline"

    def test_in_call_state_does_not_leak_to_other_participants(self, ws_connections, call_store, mock_chime):
        """A Group_Call where only one of two callees has joined must
        show "in_call" for that one callee and "online" (not "in_call")
        for the other, who is merely still ringing."""
        CAROL = "carol@example.com"
        ws_connections.register_connection(user_email=BOB)
        ws_connections.register_connection(user_email=CAROL)

        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=ALICE, callee_emails=[BOB, CAROL], call_type="group"
        )
        call_store.accept_call(call["call_id"], callee_email=BOB)

        assert _presence(ws_connections, call_store, BOB) == "in_call"
        assert _presence(ws_connections, call_store, CAROL) == "online"  # still just ringing, not joined
