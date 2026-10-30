"""
Tests for job-portal-chat-voice Task 11: GET /api/auth/logout's real-time
state cleanup (main.py's `_cleanup_realtime_state_for_logout()`), which
runs on both the voluntary "Sign out" click and the 8-hour ALB session
timeout / 401-triggered forceLogout() flow (AuthContext.jsx) — both
routed through this same backend endpoint (see logout()'s own
docstring).

Uses the shared `chat_app` fixture (conftest.py) for ENV=production +
moto-mocked DynamoDB tables, plus a locally-reloaded `ws_connections`
module (same pattern as test_ws_route.py's `ws_app` fixture) so its
production DynamoDB branch is active. auth_alb.verify_alb_oidc_token()
is monkeypatched the same way test_ws_route.py does, since no real ALB
public key / JWT signing is available in tests.
"""

from __future__ import annotations

import importlib
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def ws_app(chat_app, monkeypatch):
    from app import ws_connections as ws_connections_module
    importlib.reload(ws_connections_module)
    return chat_app


@pytest.fixture
def mock_chime(ws_app, monkeypatch):
    """Same fake-Chime approach as test_call_routes.py's mock_chime
    fixture — no moto support for chime-sdk-meetings, so this patches
    call_store's already-bound `chime_client` reference directly."""
    mock = MagicMock()
    counter = {"meeting": 0, "attendee": 0}

    def _create_meeting(call_id, media_region=None):
        counter["meeting"] += 1
        return {"chime_meeting_id": f"meeting-{counter['meeting']}", "media_region": media_region or "us-east-1", "media_placement": {}}

    def _add_attendee(meeting_id, user_email):
        counter["attendee"] += 1
        return {"attendee_id": f"attendee-{counter['attendee']}", "join_token": f"token-{counter['attendee']}"}

    mock.create_meeting_for_call.side_effect = _create_meeting
    mock.add_attendee.side_effect = _add_attendee
    mock.delete_meeting.return_value = None

    monkeypatch.setattr(ws_app.call_store, "chime_client", mock)
    return mock


def _wait_until(predicate, *, timeout_seconds: float = 2.0, interval_seconds: float = 0.02):
    """See test_ws_route.py's identical helper docstring: moto is not
    designed for cross-thread visibility, and main.py's WS route runs
    on a separate worker thread from this test's own assertions."""
    deadline = time.monotonic() + timeout_seconds
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval_seconds)
        result = predicate()
    return result


ALICE = "alice@example.com"
BOB = "bob@example.com"


def _stub_valid_token(monkeypatch, email: str):
    from app import auth_alb
    monkeypatch.setattr(auth_alb, "verify_alb_oidc_token", lambda token: {"email": email})


class TestLogoutClosesOpenWsConnection:
    """Task 11.1/11.2: a session-expiry signal (here: hitting
    GET /api/auth/logout, which is the one real sign-out path shared by
    both the voluntary "Sign out" button and the 8-hour timer's
    forceLogout()) while a WS connection is open closes that connection
    and removes its ws_connections registry row."""

    def test_logout_closes_the_connection_and_unregisters_it(self, ws_app, monkeypatch):
        from app import ws_connections
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        _stub_valid_token(monkeypatch, ALICE)
        client = TestClient(ws_app.app)

        with client.websocket_connect("/ws", headers={"x-amzn-oidc-data": "fake-token"}) as ws:
            ws.send_text("ping")  # force the register_connection() round trip (see _wait_until's rationale)
            assert _wait_until(lambda: ws_connections.is_user_online(ALICE)) is True

            # Simulate the session-expiry / forced-logout signal while
            # the WS connection above is still open. logout() reads
            # identity from the same x-amzn-oidc-data header (best
            # effort — see _cleanup_realtime_state_for_logout()'s
            # docstring), so this call must present ALICE's token too.
            logout_resp = client.get(
                "/api/auth/logout",
                headers={"x-amzn-oidc-data": "fake-token"},
                follow_redirects=False,
            )
            assert logout_resp.status_code == 302

            # The registry row is removed immediately (cross-task-safe
            # DynamoDB delete, no thread-visibility race here since it's
            # the SAME request/thread that ran register_connection()'s
            # matching unregister, from this same test's perspective —
            # but poll anyway for consistency with the rest of the
            # suite's established pattern).
            assert _wait_until(lambda: ws_connections.is_user_online(ALICE) is False) is True

            # The WebSocket itself was actually closed server-side (not
            # just forgotten) — the next client-side read observes the
            # close frame as a WebSocketDisconnect, not a hang.
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

    def test_logout_with_no_open_connection_is_a_harmless_no_op(self, ws_app, monkeypatch):
        """No WS connection open at all for this Team_Member — the
        best-effort cleanup finds nothing to do and the logout redirect
        still succeeds normally (Task 11.1's cleanup must never break
        the actual sign-out flow)."""
        from fastapi.testclient import TestClient

        _stub_valid_token(monkeypatch, ALICE)
        client = TestClient(ws_app.app)
        resp = client.get(
            "/api/auth/logout",
            headers={"x-amzn-oidc-data": "fake-token"},
            follow_redirects=False,
        )
        assert resp.status_code == 302

    def test_logout_without_oidc_header_is_a_harmless_no_op(self, ws_app):
        """The session had already fully expired before this redirect
        was even reached (no x-amzn-oidc-data header present at all,
        e.g. the ALB itself dropped it) — _cleanup_realtime_state_for_logout()
        must not raise, and the redirect must still succeed."""
        from fastapi.testclient import TestClient

        client = TestClient(ws_app.app)
        resp = client.get("/api/auth/logout", follow_redirects=False)
        assert resp.status_code == 302

    def test_logout_only_closes_the_connection_of_the_expiring_user(self, ws_app, monkeypatch):
        """A second Team_Member's own connection must be unaffected by
        the first Team_Member's logout — the bulk cleanup is scoped by
        email, not a blanket close-everything."""
        from app import auth_alb, ws_connections
        from fastapi.testclient import TestClient

        token_to_email = {"token-alice": ALICE, "token-bob": BOB}
        monkeypatch.setattr(auth_alb, "verify_alb_oidc_token", lambda token: {"email": token_to_email[token]})

        client = TestClient(ws_app.app)
        with client.websocket_connect("/ws", headers={"x-amzn-oidc-data": "token-alice"}) as ws_alice:
            with client.websocket_connect("/ws", headers={"x-amzn-oidc-data": "token-bob"}) as ws_bob:
                ws_alice.send_text("ping")
                ws_bob.send_text("ping")
                assert _wait_until(lambda: ws_connections.is_user_online(ALICE)) is True
                assert _wait_until(lambda: ws_connections.is_user_online(BOB)) is True

                logout_resp = client.get(
                    "/api/auth/logout",
                    headers={"x-amzn-oidc-data": "token-alice"},
                    follow_redirects=False,
                )
                assert logout_resp.status_code == 302

                assert _wait_until(lambda: ws_connections.is_user_online(ALICE) is False) is True
                # Bob's own connection is untouched.
                assert ws_connections.is_user_online(BOB) is True
                ws_bob.send_text("still-alive")

                ws_bob.close()


class TestLogoutEndsActiveCallParticipation:
    """Task 11.1: a Team_Member with an active Call participation at
    the moment their session ends has that participation ended too
    (call_store.leave_all_active_calls_for_user()), reusing leave_call()'s
    own existing "last participant leaving ends the call" behaviour."""

    def test_logout_leaves_every_active_call_the_user_has_joined(self, ws_app, mock_chime, monkeypatch):
        from app import call_store
        from fastapi.testclient import TestClient

        call = call_store.create_call(
            conversation_id="conv-1",
            caller_email=ALICE,
            callee_emails=[BOB],
            call_type="direct",
        )
        call_store.accept_call(call["call_id"], callee_email=BOB)
        # Bob (the callee) is the one with a real "joined" entry in the
        # participants map — the caller (Alice) is implicitly connected
        # without a participants-map entry of her own (see
        # create_call()'s own docstring) — so this test exercises
        # leave_all_active_calls_for_user() against Bob's session
        # ending, not Alice's.
        assert call_store.is_user_in_any_active_call(BOB) is True

        _stub_valid_token(monkeypatch, BOB)
        client = TestClient(ws_app.app)
        logout_resp = client.get(
            "/api/auth/logout",
            headers={"x-amzn-oidc-data": "fake-token"},
            follow_redirects=False,
        )
        assert logout_resp.status_code == 302

        assert call_store.is_user_in_any_active_call(BOB) is False
        updated = call_store.get_call(call["call_id"])
        # Bob was the only "joined" participant on this direct call, so
        # his departure drops the active-participant count to zero and
        # ends the call (leave_call()'s own existing behaviour, reused
        # by leave_all_active_calls_for_user() — see call_store.py).
        assert updated["status"] == "ended"
        mock_chime.delete_meeting.assert_called_once()
