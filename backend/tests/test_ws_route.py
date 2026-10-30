"""
Tests for @app.websocket("/ws") in main.py (job-portal-chat-voice Task
10.5), using Starlette/FastAPI's TestClient WebSocket testing support
(`client.websocket_connect(...)` as a context manager).

Uses the shared `chat_app`/`chat_client` fixtures (conftest.py) for
ENV=production + moto-mocked DynamoDB tables (ws_connections.py's
production branch needs the WsConnectionsTable) and the X-Actor-Email-
based get_actor override — but this route deliberately does NOT read
X-Actor-Email at all (see main.py's `_authenticate_ws()`): ENV=local
uses a `x_actor_email` QUERY PARAMETER (the browser WebSocket()
constructor cannot set custom headers, unlike a normal request), and
ENV!=local reads `x-amzn-oidc-data` from websocket.headers. Since
TestClient's websocket_connect() DOES support passing custom headers
(unlike a real browser), these tests exercise BOTH code paths directly:
the local-mode query-param path is tested by monkeypatching ENV back to
"local" for a couple of cases, and the production ALB-OIDC path is
tested via a monkeypatched auth_alb.verify_alb_oidc_token()/
_extract_email() pair (no real ALB public key / JWT signing needed).
"""

from __future__ import annotations

import importlib
import time

import pytest


@pytest.fixture
def ws_app(chat_app, monkeypatch):
    """chat_app (conftest.py) already reloaded main.py under
    ENV=production with chat_store/call_store/chat_attachments also
    reloaded — this fixture additionally reloads ws_connections.py (also
    ENV-dependent) so its production DynamoDB branch is active too, and
    returns the reloaded main module."""
    from app import ws_connections as ws_connections_module
    importlib.reload(ws_connections_module)
    return chat_app


def _wait_until(predicate, *, timeout_seconds: float = 2.0, interval_seconds: float = 0.02):
    """Polls `predicate()` until it returns a truthy value or
    `timeout_seconds` elapses, returning the last (possibly falsy)
    result. Necessary because main.py's WS route calls
    ws_connections.register_connection()/unregister_connection() on a
    background worker thread (Starlette TestClient's websocket_connect()
    runs each session on its own anyio blocking-portal thread), while
    ws_connections.py is backed by moto in these tests — and moto's own
    FAQ documents that it is "not designed for multithreaded access":
    writes made on one thread are not guaranteed to be immediately
    visible when read back from a different thread. A single
    `ws.send_text(...)` round trip narrows this window (it only
    resumes past receive_text() once the preceding register_connection()
    call has run) but does not close it entirely, since narrowing when
    the WORKER thread resumes says nothing about when its moto writes
    become visible to the MAIN thread's own moto view. Polling with a
    short timeout is the standard workaround for this documented
    limitation, rather than asserting on the very first read."""
    deadline = time.monotonic() + timeout_seconds
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval_seconds)
        result = predicate()
    return result


ALICE = "alice@example.com"
BOB = "bob@example.com"
UNKNOWN = "not-a-real-user@example.com"


class TestProductionOidcPath:
    """ENV=production (chat_app's default) — x-amzn-oidc-data header
    path, with auth_alb's verification functions monkeypatched so no
    real ALB public key fetch / JWT signature check is needed."""

    def _stub_valid_token(self, ws_app, monkeypatch, email: str):
        from app import auth_alb
        monkeypatch.setattr(auth_alb, "verify_alb_oidc_token", lambda token: {"email": email})
        # main.py's module-level `from . import auth_alb` import happened
        # at reload time above (inside the ws_app fixture chain); the
        # route function does `from . import auth_alb` freshly on every
        # call though, so patching the module object directly (as done
        # here) is visible regardless of import timing.

    def test_valid_token_connects_successfully(self, ws_app, monkeypatch):
        self._stub_valid_token(ws_app, monkeypatch, ALICE)
        client = ws_app.app
        from fastapi.testclient import TestClient
        with TestClient(client).websocket_connect("/ws", headers={"x-amzn-oidc-data": "fake-token"}) as ws:
            ws.send_text("ping")
            # No exception on send == the connection was accepted and is
            # alive; explicitly close from the client side.
            ws.close()

    def test_missing_header_is_rejected(self, ws_app, monkeypatch):
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with TestClient(ws_app.app).websocket_connect("/ws") as ws:
                ws.send_text("ping")

    def test_invalid_token_is_rejected(self, ws_app, monkeypatch):
        from app import auth_alb

        def _raise(token):
            raise auth_alb.AuthAlbError("bad signature")

        monkeypatch.setattr(auth_alb, "verify_alb_oidc_token", _raise)
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with TestClient(ws_app.app).websocket_connect("/ws", headers={"x-amzn-oidc-data": "garbage"}) as ws:
                ws.send_text("ping")

    def test_valid_token_but_non_allowlisted_email_is_rejected(self, ws_app, monkeypatch):
        self._stub_valid_token(ws_app, monkeypatch, UNKNOWN)
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with TestClient(ws_app.app).websocket_connect("/ws", headers={"x-amzn-oidc-data": "fake-token"}) as ws:
                ws.send_text("ping")

    def test_connection_is_registered_and_unregistered(self, ws_app, monkeypatch):
        """Requirement 9.1/9.6: a successful connect registers a row in
        ws_connections (so presence/fan-out can find it); a clean
        disconnect removes it immediately.

        Uses _wait_until(...) rather than a bare assertion immediately
        after connecting/disconnecting: main.py's WS route calls
        ws_connections.register_connection()/unregister_connection() on
        a background worker thread (Starlette TestClient's
        websocket_connect() runs each session on its own anyio
        blocking-portal thread), and ws_connections.py is backed by
        moto here — moto's own FAQ documents that it is "not designed
        for multithreaded access", so a write made on the worker thread
        is not guaranteed to be immediately visible when read back from
        the main (test) thread. See _wait_until()'s docstring above."""
        self._stub_valid_token(ws_app, monkeypatch, ALICE)
        from app import ws_connections
        from fastapi.testclient import TestClient

        assert ws_connections.is_user_online(ALICE) is False
        with TestClient(ws_app.app).websocket_connect("/ws", headers={"x-amzn-oidc-data": "fake-token"}) as ws:
            ws.send_text("ping")
            assert _wait_until(lambda: ws_connections.is_user_online(ALICE)) is True
            ws.close()
        assert _wait_until(lambda: ws_connections.is_user_online(ALICE) is False) is True

    def test_heartbeat_message_keeps_the_connection_fresh(self, ws_app, monkeypatch):
        self._stub_valid_token(ws_app, monkeypatch, ALICE)
        from app import ws_connections
        from fastapi.testclient import TestClient
        with TestClient(ws_app.app).websocket_connect("/ws", headers={"x-amzn-oidc-data": "fake-token"}) as ws:
            # See _wait_until()'s docstring: register_connection() runs
            # on a different (worker) thread than this assertion, and
            # moto (backing ws_connections here) does not guarantee
            # immediate cross-thread write visibility.
            connections_before = _wait_until(lambda: ws_connections.list_connections_for_user(ALICE))
            ws.send_text("ping")
            connections_after = ws_connections.list_connections_for_user(ALICE)
            assert len(connections_before) == 1
            assert len(connections_after) == 1
            assert connections_after[0]["connection_id"] == connections_before[0]["connection_id"]
            ws.close()


class TestMessageDeliveryToASecondConnectedClient:
    """Task 10.5's literal ask: a sent message is echoed to a second
    connected test client in the same conversation. Since outbound
    push is driven entirely by ws_manager.py's DynamoDB Streams
    poller/dispatcher (not this route directly — see main.py's WS
    section comment), and that poller is not exercised by moto in this
    suite (module-level design decision, see ws_manager.py's own
    docstring), this test instead calls ws_manager.dispatch_stream_record()
    directly against the two now-registered/locally-held connections —
    the same "pure decision function" test_ws_manager.py's own unit
    tests exercise, but here proven end-to-end against REAL WebSocket
    objects held by this route (not mock WebSockets), closing the loop
    Task 10.5 asks for without depending on moto's unrealistic Streams
    shard emulation."""

    def test_a_message_dispatched_via_ws_manager_reaches_the_second_client(self, ws_app, monkeypatch):
        from app import auth_alb, ws_manager
        from fastapi.testclient import TestClient

        token_to_email = {"token-a": ALICE, "token-b": BOB}
        monkeypatch.setattr(auth_alb, "verify_alb_oidc_token", lambda token: {"email": token_to_email[token]})

        client = TestClient(ws_app.app)
        with client.websocket_connect("/ws", headers={"x-amzn-oidc-data": "token-a"}) as ws_a:
            with client.websocket_connect("/ws", headers={"x-amzn-oidc-data": "token-b"}) as ws_b:
                import asyncio

                message_item = {
                    "conversation_id": "conv-1",
                    "message_id": "msg-1",
                    "sender_email": ALICE,
                    "content": "Hello Bob",
                }
                pushed = asyncio.run(
                    ws_manager.dispatch_stream_record(
                        table="messages",
                        event_type="message.new",
                        new_image=message_item,
                        conversation_participants=[ALICE, BOB],
                    )
                )
                # Exactly one push — to Bob's connection only (sender
                # Alice is excluded by resolve_recipients_for_message()).
                assert pushed == 1
                received = ws_b.receive_json()
                assert received["event_type"] == "message.new"
                assert received["data"]["content"] == "Hello Bob"

                ws_a.close()
                ws_b.close()
