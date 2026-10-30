"""
Tests for app/ws_manager.py — in-process WebSocket connection map and
the cross-task fan-out dispatch decision logic (job-portal-chat-voice
spec).

Only dispatch_stream_record(), ConnectionManager, and the two pure
resolve_recipients_for_*() functions are exercised here — the real
boto3 dynamodbstreams polling loop (_poll_one_shard/_poll_table_stream)
is intentionally NOT unit-tested (see ws_manager.py's module docstring
on why moto's Streams support isn't a meaningful substitute for a real
shard/iterator flow). This mirrors design.md's stated approach: the
fan-out DECISION logic is what needs coverage, not the boto3 plumbing
around it.

Uses the `chat_tables` fixture (conftest.py) so ws_connections.py's
production (DynamoDB) branch backs list_connections_for_user() calls
made inside dispatch_stream_record().
"""

from __future__ import annotations

import asyncio
import importlib

import pytest


class FakeWebSocket:
    """Minimal stand-in for Starlette's WebSocket — only needs
    send_json() to match what ConnectionManager.send_to_connection()
    calls. Records every payload sent for assertions, and can be made
    to simulate a dead/broken socket via should_fail."""

    def __init__(self, *, should_fail: bool = False):
        self.sent: list[dict] = []
        self.should_fail = should_fail

    async def send_json(self, payload: dict) -> None:
        if self.should_fail:
            raise RuntimeError("simulated broken WebSocket")
        self.sent.append(payload)


@pytest.fixture
def ws_connections(chat_tables):
    from app import ws_connections as ws_connections_module
    importlib.reload(ws_connections_module)
    return ws_connections_module


@pytest.fixture
def ws_manager(ws_connections, monkeypatch):
    """Reload ws_manager AFTER ws_connections has already been reloaded
    for this test (ws_manager imports ws_connections at module load time
    via `from . import ws_connections` — reloading ws_manager picks up
    the already-reloaded module object). Also gives each test its own
    fresh ConnectionManager instance rather than sharing the
    module-level singleton across tests."""
    from app import ws_manager as ws_manager_module
    importlib.reload(ws_manager_module)
    monkeypatch.setattr(ws_manager_module, "connection_manager", ws_manager_module.ConnectionManager())
    return ws_manager_module


ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"


def _run(coro):
    return asyncio.run(coro)


class TestConnectionManager:
    def test_register_and_has_connection(self, ws_manager):
        cm = ws_manager.ConnectionManager()
        ws = FakeWebSocket()
        cm.register("conn-1", ws)
        assert cm.has_connection("conn-1") is True
        assert cm.local_connection_ids() == {"conn-1"}

    def test_unregister_removes_the_connection(self, ws_manager):
        cm = ws_manager.ConnectionManager()
        cm.register("conn-1", FakeWebSocket())
        cm.unregister("conn-1")
        assert cm.has_connection("conn-1") is False

    def test_unregister_unknown_connection_does_not_error(self, ws_manager):
        cm = ws_manager.ConnectionManager()
        cm.unregister("does-not-exist")  # must not raise

    def test_send_to_connection_delivers_payload(self, ws_manager):
        cm = ws_manager.ConnectionManager()
        ws = FakeWebSocket()
        cm.register("conn-1", ws)

        result = _run(cm.send_to_connection("conn-1", {"event_type": "test"}))

        assert result is True
        assert ws.sent == [{"event_type": "test"}]

    def test_send_to_connection_returns_false_for_unknown_connection(self, ws_manager):
        cm = ws_manager.ConnectionManager()
        result = _run(cm.send_to_connection("does-not-exist", {"event_type": "test"}))
        assert result is False

    def test_send_to_connection_drops_dead_socket_and_returns_false(self, ws_manager):
        cm = ws_manager.ConnectionManager()
        cm.register("conn-1", FakeWebSocket(should_fail=True))

        result = _run(cm.send_to_connection("conn-1", {"event_type": "test"}))

        assert result is False
        assert cm.has_connection("conn-1") is False  # cleaned up

    def test_send_to_local_connections_only_pushes_to_locally_held_ids(self, ws_manager):
        cm = ws_manager.ConnectionManager()
        ws1, ws2 = FakeWebSocket(), FakeWebSocket()
        cm.register("conn-1", ws1)
        cm.register("conn-2", ws2)

        sent_count = _run(
            cm.send_to_local_connections({"conn-1", "conn-2", "conn-on-another-task"}, {"event_type": "test"})
        )

        assert sent_count == 2
        assert ws1.sent == [{"event_type": "test"}]
        assert ws2.sent == [{"event_type": "test"}]

    def test_send_to_local_connections_returns_zero_when_none_are_local(self, ws_manager):
        cm = ws_manager.ConnectionManager()
        sent_count = _run(cm.send_to_local_connections({"conn-on-another-task"}, {"event_type": "test"}))
        assert sent_count == 0


class TestResolveRecipientsForMessage:
    def test_excludes_the_sender(self, ws_manager):
        recipients = ws_manager.resolve_recipients_for_message(
            {"sender_email": ALICE}, conversation_participants=[ALICE, BOB]
        )
        assert recipients == {BOB}

    def test_includes_all_other_participants(self, ws_manager):
        recipients = ws_manager.resolve_recipients_for_message(
            {"sender_email": ALICE}, conversation_participants=[ALICE, BOB, CAROL]
        )
        assert recipients == {BOB, CAROL}

    def test_normalises_email_case(self, ws_manager):
        recipients = ws_manager.resolve_recipients_for_message(
            {"sender_email": "Alice@example.com"}, conversation_participants=["ALICE@example.com", "Bob@example.com"]
        )
        assert recipients == {BOB}


class TestResolveRecipientsForCall:
    def test_includes_caller_and_all_participants(self, ws_manager):
        recipients = ws_manager.resolve_recipients_for_call(
            {"caller_email": ALICE, "participants": {BOB: {"state": "ringing"}, CAROL: {"state": "joined"}}}
        )
        assert recipients == {ALICE, BOB, CAROL}

    def test_includes_declined_and_timed_out_participants_too(self, ws_manager):
        """A roster update is relevant even to a declined/timed-out
        participant if their client is still open on the conversation."""
        recipients = ws_manager.resolve_recipients_for_call(
            {"caller_email": ALICE, "participants": {BOB: {"state": "declined"}, CAROL: {"state": "timed_out"}}}
        )
        assert recipients == {ALICE, BOB, CAROL}


class TestDispatchStreamRecordForMessages:
    def test_pushes_to_a_locally_held_recipient_connection(self, ws_manager, ws_connections):
        ws = FakeWebSocket()
        ws_manager.connection_manager.register("bob-conn-1", ws)
        ws_connections.register_connection(user_email=BOB, connection_id="bob-conn-1")

        sent_count = _run(
            ws_manager.dispatch_stream_record(
                table="messages",
                event_type="message.new",
                new_image={"sender_email": ALICE, "content": "hello"},
                conversation_participants=[ALICE, BOB],
            )
        )

        assert sent_count == 1
        assert ws.sent == [
            {"event_type": "message.new", "table": "messages", "data": {"sender_email": ALICE, "content": "hello"}}
        ]

    def test_does_not_push_and_does_not_error_when_recipient_not_held_locally(self, ws_manager, ws_connections):
        """The recipient's connection_id is registered in the
        cross-task DynamoDB registry (as if it belongs to a DIFFERENT
        ECS task) but never registered in THIS process's
        ConnectionManager — dispatch must complete with zero local
        pushes and no exception."""
        ws_connections.register_connection(user_email=BOB, connection_id="bob-conn-on-another-task")

        sent_count = _run(
            ws_manager.dispatch_stream_record(
                table="messages",
                event_type="message.new",
                new_image={"sender_email": ALICE, "content": "hello"},
                conversation_participants=[ALICE, BOB],
            )
        )

        assert sent_count == 0

    def test_does_not_push_to_sender_even_if_locally_connected(self, ws_manager, ws_connections):
        ws = FakeWebSocket()
        ws_manager.connection_manager.register("alice-conn-1", ws)
        ws_connections.register_connection(user_email=ALICE, connection_id="alice-conn-1")

        sent_count = _run(
            ws_manager.dispatch_stream_record(
                table="messages",
                event_type="message.new",
                new_image={"sender_email": ALICE, "content": "hello"},
                conversation_participants=[ALICE, BOB],
            )
        )

        assert sent_count == 0
        assert ws.sent == []

    def test_pushes_to_multiple_simultaneous_connections_for_the_same_recipient(self, ws_manager, ws_connections):
        """Requirement 8.7: a recipient with two connections (e.g.
        desktop + mobile) both connected to THIS same task must both
        receive the push."""
        ws_desktop, ws_mobile = FakeWebSocket(), FakeWebSocket()
        ws_manager.connection_manager.register("bob-desktop", ws_desktop)
        ws_manager.connection_manager.register("bob-mobile", ws_mobile)
        ws_connections.register_connection(user_email=BOB, connection_id="bob-desktop")
        ws_connections.register_connection(user_email=BOB, connection_id="bob-mobile")

        sent_count = _run(
            ws_manager.dispatch_stream_record(
                table="messages",
                event_type="message.new",
                new_image={"sender_email": ALICE, "content": "hello"},
                conversation_participants=[ALICE, BOB],
            )
        )

        assert sent_count == 2
        assert len(ws_desktop.sent) == 1
        assert len(ws_mobile.sent) == 1


class TestDispatchStreamRecordForCalls:
    def test_pushes_ring_event_to_locally_held_callee(self, ws_manager, ws_connections):
        ws = FakeWebSocket()
        ws_manager.connection_manager.register("bob-conn-1", ws)
        ws_connections.register_connection(user_email=BOB, connection_id="bob-conn-1")

        sent_count = _run(
            ws_manager.dispatch_stream_record(
                table="call_sessions",
                event_type="call.ring",
                new_image={"caller_email": ALICE, "status": "ringing", "participants": {BOB: {"state": "ringing"}}},
            )
        )

        assert sent_count == 1
        assert ws.sent[0]["event_type"] == "call.ring"

    def test_ended_call_session_is_never_forwarded(self, ws_manager, ws_connections):
        """Per design.md: stream records for the terminal "ended" write
        are ignored by the fan-out dispatcher."""
        ws = FakeWebSocket()
        ws_manager.connection_manager.register("bob-conn-1", ws)
        ws_connections.register_connection(user_email=BOB, connection_id="bob-conn-1")

        sent_count = _run(
            ws_manager.dispatch_stream_record(
                table="call_sessions",
                event_type="call.ended",
                new_image={
                    "caller_email": ALICE,
                    "status": "ended",
                    "participants": {BOB: {"state": "declined"}},
                },
            )
        )

        assert sent_count == 0
        assert ws.sent == []

    def test_pushes_roster_update_to_caller_even_though_not_in_participants_map(self, ws_manager, ws_connections):
        ws = FakeWebSocket()
        ws_manager.connection_manager.register("alice-conn-1", ws)
        ws_connections.register_connection(user_email=ALICE, connection_id="alice-conn-1")

        sent_count = _run(
            ws_manager.dispatch_stream_record(
                table="call_sessions",
                event_type="call.roster_update",
                new_image={
                    "caller_email": ALICE,
                    "status": "active",
                    "participants": {BOB: {"state": "joined"}, CAROL: {"state": "ringing"}},
                },
            )
        )

        assert sent_count == 1

    def test_caller_join_credentials_are_stripped_from_the_broadcast(self, ws_manager, ws_connections):
        """Security fix (Task 15.0): the Caller's own Chime attendee
        credential (caller_attendee_id/caller_join_token) must never
        be forwarded to a Callee's WebSocket, since every recipient
        (caller + every participant) receives this exact same
        broadcast — a Callee receiving the Caller's join_token could
        impersonate the Caller's own Chime attendee in the meeting."""
        ws = FakeWebSocket()
        ws_manager.connection_manager.register("bob-conn-1", ws)
        ws_connections.register_connection(user_email=BOB, connection_id="bob-conn-1")

        _run(
            ws_manager.dispatch_stream_record(
                table="call_sessions",
                event_type="call.ring",
                new_image={
                    "caller_email": ALICE,
                    "status": "ringing",
                    "participants": {BOB: {"state": "ringing"}},
                    "caller_attendee_id": "attendee-secret",
                    "caller_join_token": "join-token-secret",
                    "chime_meeting_id": "meeting-1",
                },
            )
        )

        pushed_data = ws.sent[0]["data"]
        assert "caller_attendee_id" not in pushed_data
        assert "caller_join_token" not in pushed_data
        # Non-sensitive fields must still be present.
        assert pushed_data["chime_meeting_id"] == "meeting-1"


class TestDispatchStreamRecordUnknownTable:
    def test_unknown_table_returns_zero_without_error(self, ws_manager):
        sent_count = _run(
            ws_manager.dispatch_stream_record(table="something_else", event_type="x", new_image={})
        )
        assert sent_count == 0
