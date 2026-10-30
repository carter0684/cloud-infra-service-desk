"""
Integration-style tests for the Call REST routes wired into main.py
(job-portal-chat-voice Task 9.4-9.6): /api/calls/* and the call-history
endpoint.

Uses the shared `chat_app`/`chat_client` fixtures (conftest.py). Chime
SDK Meetings itself is mocked via unittest.mock (no moto support for
that service), applied to call_store's ALREADY-BOUND `chime_client`
reference — same convention test_call_store.py's `mock_chime` fixture
uses — so no real boto3 chime-sdk-meetings calls happen.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"
DAVE = "dave@example.com"
OUTSIDER = "dave@example.com"


@pytest.fixture
def client(chat_client):
    return chat_client


@pytest.fixture
def mock_chime(chat_app, monkeypatch):
    """Same fake-Chime approach as test_call_store.py's mock_chime
    fixture, applied to the reloaded call_store module chat_app already
    imported."""
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

    monkeypatch.setattr(chat_app.call_store, "chime_client", mock)
    return mock


def _headers(email: str) -> dict:
    return {"X-Actor-Email": email}


def _create_direct_conversation(client, a: str, b: str) -> str:
    resp = client.post("/api/chat/conversations", json={"type": "direct", "participant_email": b}, headers=_headers(a))
    return resp.json()["conversation_id"]


def _create_group_conversation(client, owner: str, others: list[str], name: str = "Team") -> str:
    resp = client.post("/api/chat/conversations", json={"type": "group", "name": name, "participant_emails": others}, headers=_headers(owner))
    return resp.json()["conversation_id"]


class TestCreateCall:
    def test_direct_call_rings_the_other_participant(self, client, mock_chime):
        conv_id = _create_direct_conversation(client, ALICE, BOB)
        resp = client.post("/api/calls", json={"conversation_id": conv_id}, headers=_headers(ALICE))
        assert resp.status_code == 201
        body = resp.json()
        assert body["type"] == "direct"
        assert body["caller_email"] == ALICE
        assert set(body["participants"].keys()) == {BOB}
        assert body["participants"][BOB]["state"] == "ringing"
        mock_chime.create_meeting_for_call.assert_called_once()

    def test_group_call_defaults_to_ringing_every_other_participant(self, client, mock_chime):
        conv_id = _create_group_conversation(client, ALICE, [BOB, CAROL])
        resp = client.post("/api/calls", json={"conversation_id": conv_id}, headers=_headers(ALICE))
        assert resp.status_code == 201
        body = resp.json()
        assert body["type"] == "group"
        assert set(body["participants"].keys()) == {BOB, CAROL}

    def test_group_call_can_target_a_subset_of_participants(self, client, mock_chime):
        conv_id = _create_group_conversation(client, ALICE, [BOB, CAROL])
        resp = client.post("/api/calls", json={"conversation_id": conv_id, "callee_emails": [BOB]}, headers=_headers(ALICE))
        assert resp.status_code == 201
        assert set(resp.json()["participants"].keys()) == {BOB}

    def test_group_call_rejects_a_callee_not_in_the_conversation(self, client, mock_chime):
        conv_id = _create_group_conversation(client, ALICE, [BOB, CAROL])
        resp = client.post("/api/calls", json={"conversation_id": conv_id, "callee_emails": [DAVE]}, headers=_headers(ALICE))
        assert resp.status_code == 400

    def test_non_participant_cannot_start_a_call(self, client, mock_chime):
        conv_id = _create_direct_conversation(client, ALICE, BOB)
        resp = client.post("/api/calls", json={"conversation_id": conv_id}, headers=_headers(CAROL))
        assert resp.status_code == 403

    def test_unknown_conversation_is_404(self, client, mock_chime):
        resp = client.post("/api/calls", json={"conversation_id": "does-not-exist"}, headers=_headers(ALICE))
        assert resp.status_code == 404


class TestAcceptDeclineCancel:
    def _start_direct_call(self, client, mock_chime) -> str:
        conv_id = _create_direct_conversation(client, ALICE, BOB)
        resp = client.post("/api/calls", json={"conversation_id": conv_id}, headers=_headers(ALICE))
        return resp.json()["call_id"]

    def test_callee_can_accept(self, client, mock_chime):
        call_id = self._start_direct_call(client, mock_chime)
        resp = client.post(f"/api/calls/{call_id}/accept", headers=_headers(BOB))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "active"
        assert body["participants"][BOB]["state"] == "joined"
        assert "_join_info" in body
        # Caller (ALICE) was already added at create_call() time; accept
        # adds BOB, for a total of two add_attendee() calls.
        assert mock_chime.add_attendee.call_count == 2

    def test_callee_can_decline(self, client, mock_chime):
        call_id = self._start_direct_call(client, mock_chime)
        resp = client.post(f"/api/calls/{call_id}/decline", headers=_headers(BOB))
        assert resp.status_code == 200
        assert resp.json()["participants"][BOB]["state"] == "declined"
        # Requirement 5.5: a Direct_Call's sole callee declining ends
        # the call — DeleteMeeting is called.
        mock_chime.delete_meeting.assert_called_once()

    def test_caller_can_cancel_while_ringing(self, client, mock_chime):
        call_id = self._start_direct_call(client, mock_chime)
        resp = client.post(f"/api/calls/{call_id}/cancel", headers=_headers(ALICE))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ended"

    def test_accept_on_unknown_call_is_404(self, client, mock_chime):
        resp = client.post("/api/calls/does-not-exist/accept", headers=_headers(BOB))
        assert resp.status_code == 404

    def test_group_call_one_decline_does_not_affect_other_callees(self, client, mock_chime):
        """Requirement 6.3."""
        conv_id = _create_group_conversation(client, ALICE, [BOB, CAROL])
        create_resp = client.post("/api/calls", json={"conversation_id": conv_id}, headers=_headers(ALICE))
        call_id = create_resp.json()["call_id"]

        decline_resp = client.post(f"/api/calls/{call_id}/decline", headers=_headers(BOB))
        assert decline_resp.status_code == 200
        assert decline_resp.json()["participants"][BOB]["state"] == "declined"
        assert decline_resp.json()["participants"][CAROL]["state"] == "ringing"
        # Still someone (Carol) ringing — the meeting must not have been
        # torn down yet.
        mock_chime.delete_meeting.assert_not_called()

        accept_resp = client.post(f"/api/calls/{call_id}/accept", headers=_headers(CAROL))
        assert accept_resp.status_code == 200
        assert accept_resp.json()["participants"][CAROL]["state"] == "joined"
        assert accept_resp.json()["participants"][BOB]["state"] == "declined"


class TestLeaveAndInvite:
    def test_last_participant_leaving_ends_the_call(self, client, mock_chime):
        conv_id = _create_direct_conversation(client, ALICE, BOB)
        call_id = client.post("/api/calls", json={"conversation_id": conv_id}, headers=_headers(ALICE)).json()["call_id"]
        client.post(f"/api/calls/{call_id}/accept", headers=_headers(BOB))

        resp = client.post(f"/api/calls/{call_id}/leave", headers=_headers(BOB))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ended"
        mock_chime.delete_meeting.assert_called_once()

    def test_leave_on_unknown_call_is_404(self, client, mock_chime):
        resp = client.post("/api/calls/does-not-exist/leave", headers=_headers(ALICE))
        assert resp.status_code == 404

    def test_current_participant_can_invite_another_group_member(self, client, mock_chime):
        """Requirement 6.7."""
        conv_id = _create_group_conversation(client, ALICE, [BOB, CAROL])
        call_id = client.post("/api/calls", json={"conversation_id": conv_id, "callee_emails": [BOB]}, headers=_headers(ALICE)).json()["call_id"]

        resp = client.post(f"/api/calls/{call_id}/invite", json={"email": CAROL}, headers=_headers(ALICE))
        assert resp.status_code == 200
        assert resp.json()["participants"][CAROL]["state"] == "ringing"

    def test_cannot_invite_someone_outside_the_parent_conversation(self, client, mock_chime):
        conv_id = _create_group_conversation(client, ALICE, [BOB, CAROL])
        call_id = client.post("/api/calls", json={"conversation_id": conv_id, "callee_emails": [BOB]}, headers=_headers(ALICE)).json()["call_id"]

        resp = client.post(f"/api/calls/{call_id}/invite", json={"email": DAVE}, headers=_headers(ALICE))
        assert resp.status_code == 400

    def test_non_participant_cannot_invite(self, client, mock_chime):
        conv_id = _create_group_conversation(client, ALICE, [BOB, CAROL])
        call_id = client.post("/api/calls", json={"conversation_id": conv_id, "callee_emails": [BOB]}, headers=_headers(ALICE)).json()["call_id"]

        # Carol is a Conversation participant but not yet a participant
        # OF THIS SPECIFIC CALL (only Bob was rung) — must be rejected.
        resp = client.post(f"/api/calls/{call_id}/invite", json={"email": DAVE}, headers=_headers(CAROL))
        assert resp.status_code == 403


class TestCallHistory:
    def test_participant_can_list_call_history(self, client, mock_chime):
        conv_id = _create_direct_conversation(client, ALICE, BOB)
        call_id = client.post("/api/calls", json={"conversation_id": conv_id}, headers=_headers(ALICE)).json()["call_id"]
        client.post(f"/api/calls/{call_id}/cancel", headers=_headers(ALICE))

        resp = client.get(f"/api/chat/conversations/{conv_id}/calls", headers=_headers(BOB))
        assert resp.status_code == 200
        calls = resp.json()["calls"]
        assert len(calls) == 1
        assert calls[0]["call_id"] == call_id

    def test_non_participant_of_conversation_cannot_list_history(self, client, mock_chime):
        conv_id = _create_direct_conversation(client, ALICE, BOB)
        resp = client.get(f"/api/chat/conversations/{conv_id}/calls", headers=_headers(CAROL))
        assert resp.status_code == 403

    def test_unknown_conversation_is_404(self, client, mock_chime):
        resp = client.get("/api/chat/conversations/does-not-exist/calls", headers=_headers(ALICE))
        assert resp.status_code == 404
