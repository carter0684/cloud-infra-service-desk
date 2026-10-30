"""
Integration-style tests for the Chat REST routes wired into main.py
(job-portal-chat-voice Task 9.1-9.3/9.6): conversation, message, and
attachment endpoints.

Uses the shared `chat_app`/`chat_client` fixtures (conftest.py), which
set ENV=production + moto-mocked DynamoDB tables, reload chat_store/
call_store/chat_attachments THEN main.py itself (see conftest.py's
`chat_app` docstring for why that ordering matters), and override
main.get_actor so tests can keep authenticating via the familiar
X-Actor-Email header. See that fixture's docstring for full detail.
"""

from __future__ import annotations

import pytest

ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"
DAVE = "dave@example.com"
# A genuinely allowlisted (so ENV=production's auth_alb-backed get_actor
# still resolves them, giving a 401 rather than the intended 403) Team_
# Member who simply is NOT a participant of whatever conversation a
# given test targets — this is what "OUTSIDER" means throughout this
# file: outside the conversation, not outside the Portal's allowlist.
# Must be one of conftest.py's CHAT_ROUTES_KNOWN_USERS.
OUTSIDER = "dave@example.com"
UNKNOWN_TO_PORTAL = "not-a-real-user@example.com"


@pytest.fixture
def client(chat_client):
    return chat_client


def _headers(email: str) -> dict:
    return {"X-Actor-Email": email}


class TestListConversations:
    def test_empty_for_a_brand_new_user(self, client):
        resp = client.get("/api/chat/conversations", headers=_headers(ALICE))
        assert resp.status_code == 200
        assert resp.json() == {"conversations": []}

    def test_only_returns_conversations_the_caller_participates_in(self, client):
        client.post("/api/chat/conversations", json={"type": "direct", "participant_email": BOB}, headers=_headers(ALICE))
        resp = client.get("/api/chat/conversations", headers=_headers(CAROL))
        assert resp.json() == {"conversations": []}


class TestCreateDirectConversation:
    def test_creates_a_new_direct_conversation(self, client):
        resp = client.post("/api/chat/conversations", json={"type": "direct", "participant_email": BOB}, headers=_headers(ALICE))
        assert resp.status_code == 201
        body = resp.json()
        assert body["type"] == "direct"
        assert set(body["participants"]) == {ALICE, BOB}

    def test_reopens_existing_direct_conversation_instead_of_duplicating(self, client):
        """Requirement 1.2."""
        first = client.post("/api/chat/conversations", json={"type": "direct", "participant_email": BOB}, headers=_headers(ALICE))
        second = client.post("/api/chat/conversations", json={"type": "direct", "participant_email": ALICE}, headers=_headers(BOB))
        assert first.json()["conversation_id"] == second.json()["conversation_id"]

    def test_cannot_start_a_direct_conversation_with_yourself(self, client):
        resp = client.post("/api/chat/conversations", json={"type": "direct", "participant_email": ALICE}, headers=_headers(ALICE))
        assert resp.status_code == 400

    def test_unknown_participant_email_is_404(self, client):
        resp = client.post("/api/chat/conversations", json={"type": "direct", "participant_email": UNKNOWN_TO_PORTAL}, headers=_headers(ALICE))
        assert resp.status_code == 404

    def test_missing_participant_email_is_400(self, client):
        resp = client.post("/api/chat/conversations", json={"type": "direct"}, headers=_headers(ALICE))
        assert resp.status_code == 400


class TestCreateGroupConversation:
    def test_creates_a_group_with_three_participants(self, client):
        """Requirement 1.3: owner + at least two others = three or more."""
        resp = client.post(
            "/api/chat/conversations",
            json={"type": "group", "name": "Infra Team", "participant_emails": [BOB, CAROL]},
            headers=_headers(ALICE),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["owner_email"] == ALICE
        assert set(body["participants"]) == {ALICE, BOB, CAROL}

    def test_rejects_fewer_than_two_other_participants(self, client):
        resp = client.post(
            "/api/chat/conversations",
            json={"type": "group", "name": "Too Small", "participant_emails": [BOB]},
            headers=_headers(ALICE),
        )
        assert resp.status_code == 400

    def test_rejects_empty_name(self, client):
        resp = client.post(
            "/api/chat/conversations",
            json={"type": "group", "name": "", "participant_emails": [BOB, CAROL]},
            headers=_headers(ALICE),
        )
        assert resp.status_code == 400

    def test_rejects_name_over_100_characters(self, client):
        resp = client.post(
            "/api/chat/conversations",
            json={"type": "group", "name": "x" * 101, "participant_emails": [BOB, CAROL]},
            headers=_headers(ALICE),
        )
        assert resp.status_code == 400

    def test_unknown_participant_is_404(self, client):
        resp = client.post(
            "/api/chat/conversations",
            json={"type": "group", "name": "Infra Team", "participant_emails": [BOB, UNKNOWN_TO_PORTAL]},
            headers=_headers(ALICE),
        )
        assert resp.status_code == 404

    def test_invalid_type_is_400(self, client):
        resp = client.post("/api/chat/conversations", json={"type": "triangle"}, headers=_headers(ALICE))
        assert resp.status_code == 400


class TestUpdateConversation:
    def _create_group(self, client) -> str:
        resp = client.post(
            "/api/chat/conversations",
            json={"type": "group", "name": "Team", "participant_emails": [BOB, CAROL]},
            headers=_headers(ALICE),
        )
        return resp.json()["conversation_id"]

    def test_owner_can_rename(self, client):
        conv_id = self._create_group(client)
        resp = client.patch(f"/api/chat/conversations/{conv_id}", json={"name": "New Name"}, headers=_headers(ALICE))
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    def test_non_owner_participant_cannot_rename(self, client):
        conv_id = self._create_group(client)
        resp = client.patch(f"/api/chat/conversations/{conv_id}", json={"name": "New Name"}, headers=_headers(BOB))
        assert resp.status_code == 403

    def test_non_participant_gets_403_not_404(self, client):
        """Requirement 9.3: a non-participant is rejected, distinct from
        an unknown conversation_id (404)."""
        conv_id = self._create_group(client)
        resp = client.patch(f"/api/chat/conversations/{conv_id}", json={"name": "New Name"}, headers=_headers(DAVE))
        assert resp.status_code == 403

    def test_unknown_conversation_is_404(self, client):
        resp = client.patch("/api/chat/conversations/does-not-exist", json={"name": "New Name"}, headers=_headers(ALICE))
        assert resp.status_code == 404

    def test_owner_can_add_and_remove_participants(self, client):
        conv_id = self._create_group(client)
        resp = client.patch(
            f"/api/chat/conversations/{conv_id}",
            json={"add_participant_emails": [DAVE], "remove_participant_emails": [CAROL]},
            headers=_headers(ALICE),
        )
        assert resp.status_code == 200
        assert set(resp.json()["participants"]) == {ALICE, BOB, DAVE}

    def test_cannot_rename_a_direct_conversation(self, client):
        resp = client.post("/api/chat/conversations", json={"type": "direct", "participant_email": BOB}, headers=_headers(ALICE))
        conv_id = resp.json()["conversation_id"]
        resp = client.patch(f"/api/chat/conversations/{conv_id}", json={"name": "Nope"}, headers=_headers(ALICE))
        assert resp.status_code == 400


class TestMessages:
    def _create_direct(self, client) -> str:
        resp = client.post("/api/chat/conversations", json={"type": "direct", "participant_email": BOB}, headers=_headers(ALICE))
        return resp.json()["conversation_id"]

    def test_send_and_list_messages(self, client):
        conv_id = self._create_direct(client)
        resp = client.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "Hello"}, headers=_headers(ALICE))
        assert resp.status_code == 201
        assert resp.json()["content"] == "Hello"

        resp = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=_headers(BOB))
        assert resp.status_code == 200
        messages = resp.json()["messages"]
        assert len(messages) == 1
        assert messages[0]["sender_email"] == ALICE

    def test_send_rejects_empty_message_with_no_attachment(self, client):
        """Requirement 2.7."""
        conv_id = self._create_direct(client)
        resp = client.post(f"/api/chat/conversations/{conv_id}/messages", json={}, headers=_headers(ALICE))
        assert resp.status_code == 400

    def test_non_participant_cannot_send(self, client):
        conv_id = self._create_direct(client)
        resp = client.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "Hi"}, headers=_headers(OUTSIDER))
        assert resp.status_code == 403

    def test_non_participant_cannot_list(self, client):
        conv_id = self._create_direct(client)
        resp = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=_headers(OUTSIDER))
        assert resp.status_code == 403

    def test_unknown_conversation_is_404_for_send(self, client):
        resp = client.post("/api/chat/conversations/does-not-exist/messages", json={"content": "Hi"}, headers=_headers(ALICE))
        assert resp.status_code == 404


class TestEditMessage:
    def _create_and_send(self, client) -> tuple[str, str]:
        conv_resp = client.post("/api/chat/conversations", json={"type": "direct", "participant_email": BOB}, headers=_headers(ALICE))
        conv_id = conv_resp.json()["conversation_id"]
        msg_resp = client.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "Original"}, headers=_headers(ALICE))
        return conv_id, msg_resp.json()["message_id"]

    def test_sender_can_edit_within_window(self, client):
        conv_id, msg_id = self._create_and_send(client)
        resp = client.patch(
            f"/api/chat/messages/{msg_id}",
            params={"conversation_id": conv_id},
            json={"content": "Edited"},
            headers=_headers(ALICE),
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == "Edited"

    def test_non_sender_cannot_edit(self, client):
        conv_id, msg_id = self._create_and_send(client)
        resp = client.patch(
            f"/api/chat/messages/{msg_id}",
            params={"conversation_id": conv_id},
            json={"content": "Hijacked"},
            headers=_headers(BOB),
        )
        assert resp.status_code == 403

    def test_non_participant_gets_403_before_reaching_edit_logic(self, client):
        conv_id, msg_id = self._create_and_send(client)
        resp = client.patch(
            f"/api/chat/messages/{msg_id}",
            params={"conversation_id": conv_id},
            json={"content": "Nope"},
            headers=_headers(OUTSIDER),
        )
        assert resp.status_code == 403

    def test_edit_outside_window_is_rejected(self, client):
        import freezegun

        conv_id, msg_id = self._create_and_send(client)
        with freezegun.freeze_time("2099-01-01T00:20:00Z"):
            resp = client.patch(
                f"/api/chat/messages/{msg_id}",
                params={"conversation_id": conv_id},
                json={"content": "Too late"},
                headers=_headers(ALICE),
            )
        assert resp.status_code == 400


class TestMarkMessagesRead:
    """Requirement 2.5: POST .../messages/read updates Read_Receipt
    state (read_by) visible to the sender."""

    def _create_and_send(self, client) -> tuple[str, str]:
        conv_resp = client.post("/api/chat/conversations", json={"type": "direct", "participant_email": BOB}, headers=_headers(ALICE))
        conv_id = conv_resp.json()["conversation_id"]
        msg_resp = client.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "Hello"}, headers=_headers(ALICE))
        return conv_id, msg_resp.json()["message_id"]

    def test_participant_can_mark_a_message_read(self, client):
        conv_id, msg_id = self._create_and_send(client)
        resp = client.post(
            f"/api/chat/conversations/{conv_id}/messages/read",
            json={"message_ids": [msg_id]},
            headers=_headers(BOB),
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_read_by_is_visible_on_a_subsequent_list_call(self, client):
        """The sender (ALICE) must see BOB's read_at timestamp appear
        under read_by after BOB marks the message read."""
        conv_id, msg_id = self._create_and_send(client)
        client.post(
            f"/api/chat/conversations/{conv_id}/messages/read",
            json={"message_ids": [msg_id]},
            headers=_headers(BOB),
        )
        resp = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=_headers(ALICE))
        messages = resp.json()["messages"]
        assert messages[0]["message_id"] == msg_id
        assert BOB in messages[0]["read_by"]

    def test_non_participant_gets_403(self, client):
        conv_id, msg_id = self._create_and_send(client)
        resp = client.post(
            f"/api/chat/conversations/{conv_id}/messages/read",
            json={"message_ids": [msg_id]},
            headers=_headers(OUTSIDER),
        )
        assert resp.status_code == 403

    def test_unknown_conversation_is_404(self, client):
        resp = client.post(
            "/api/chat/conversations/does-not-exist/messages/read",
            json={"message_ids": ["msg-1"]},
            headers=_headers(ALICE),
        )
        assert resp.status_code == 404

    def test_unknown_message_id_is_a_silent_no_op_not_an_error(self, client):
        """chat_store.mark_read() silently skips message_ids it can't
        find within this conversation — this route must not surface
        that as an error, matching the "open a Conversation and mark
        whatever's visible as read" UX this exists for."""
        conv_id, _msg_id = self._create_and_send(client)
        resp = client.post(
            f"/api/chat/conversations/{conv_id}/messages/read",
            json={"message_ids": ["does-not-exist"]},
            headers=_headers(BOB),
        )
        assert resp.status_code == 200

    def test_empty_message_ids_list_is_a_no_op(self, client):
        conv_id, _msg_id = self._create_and_send(client)
        resp = client.post(
            f"/api/chat/conversations/{conv_id}/messages/read",
            json={"message_ids": []},
            headers=_headers(BOB),
        )
        assert resp.status_code == 200


class TestAttachmentPresign:
    def _create_direct(self, client) -> str:
        resp = client.post("/api/chat/conversations", json={"type": "direct", "participant_email": BOB}, headers=_headers(ALICE))
        return resp.json()["conversation_id"]

    def test_participant_can_get_a_presigned_put_url(self, client, chat_app, monkeypatch):
        conv_id = self._create_direct(client)
        monkeypatch.setattr(
            chat_app.chat_attachments,
            "_s3",
            lambda: type("Fake", (), {"generate_presigned_url": lambda *a, **k: "https://example.com/put"})(),
        )
        resp = client.post(
            f"/api/chat/conversations/{conv_id}/attachments/presign",
            json={"message_id": "msg-1", "filename": "photo.jpg", "content_type": "image/jpeg", "size_bytes": 1024},
            headers=_headers(ALICE),
        )
        assert resp.status_code == 200
        assert resp.json()["put_url"] == "https://example.com/put"

    def test_non_participant_cannot_presign(self, client):
        conv_id = self._create_direct(client)
        resp = client.post(
            f"/api/chat/conversations/{conv_id}/attachments/presign",
            json={"message_id": "msg-1", "filename": "photo.jpg", "content_type": "image/jpeg", "size_bytes": 1024},
            headers=_headers(OUTSIDER),
        )
        assert resp.status_code == 403

    def test_over_limit_photo_is_rejected(self, client):
        conv_id = self._create_direct(client)
        resp = client.post(
            f"/api/chat/conversations/{conv_id}/attachments/presign",
            json={"message_id": "msg-1", "filename": "photo.jpg", "content_type": "image/jpeg", "size_bytes": 11 * 1024 * 1024},
            headers=_headers(ALICE),
        )
        assert resp.status_code == 400

    def test_get_url_rejects_non_participant(self, client):
        conv_id = self._create_direct(client)
        resp = client.get(
            f"/api/chat/attachments/chat/{conv_id}/msg-1/att-1-file.txt/url",
            params={"conversation_id": conv_id},
            headers=_headers(OUTSIDER),
        )
        assert resp.status_code == 403

    def test_get_url_succeeds_for_participant(self, client, chat_app, monkeypatch):
        conv_id = self._create_direct(client)
        monkeypatch.setattr(
            chat_app.chat_attachments,
            "_s3",
            lambda: type("Fake", (), {"generate_presigned_url": lambda *a, **k: "https://example.com/get"})(),
        )
        resp = client.get(
            f"/api/chat/attachments/chat/{conv_id}/msg-1/att-1-file.txt/url",
            params={"conversation_id": conv_id},
            headers=_headers(BOB),
        )
        assert resp.status_code == 200
        assert resp.json()["url"] == "https://example.com/get"
