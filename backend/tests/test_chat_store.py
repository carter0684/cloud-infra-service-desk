"""
Tests for app/chat_store.py — conversation and message persistence
layer (job-portal-chat-voice spec).

Uses the `chat_tables` fixture (conftest.py), which sets ENV=production
and spins up moto-mocked DynamoDB tables matching data_stack.py's
ChatConversationsTable/ChatParticipantsTable/ChatMessagesTable schemas —
so these tests exercise the production (DynamoDB) branch of
chat_store.py, not the local SQLite branch.
"""

from __future__ import annotations

import importlib
import time

import pytest
from freezegun import freeze_time


@pytest.fixture
def chat_store(chat_tables):
    """Reload the module fresh so its module-level TABLE_NAME constants
    pick up the ENV=production env var set by the chat_tables fixture —
    same pattern as test_escalation.py's escalation_module fixture."""
    from app import chat_store as chat_store_module
    importlib.reload(chat_store_module)
    return chat_store_module


class TestDirectConversationCreation:
    def test_create_direct_conversation(self, chat_store):
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        assert conv["type"] == "direct"
        assert set(conv["participants"]) == {"alice@example.com", "bob@example.com"}
        assert conv["name"] is None
        assert conv["owner_email"] is None

    def test_reopens_existing_direct_conversation_instead_of_duplicating(self, chat_store):
        """Requirement 1.2: reopening an existing Direct_Conversation
        must not create a duplicate."""
        first = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        second = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        assert first["conversation_id"] == second["conversation_id"]

    def test_reopens_existing_direct_conversation_order_independent(self, chat_store):
        """Requirement 1.2: order of the two participants must not
        matter — (A,B) and (B,A) are the same conversation."""
        first = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        second = chat_store.create_direct_conversation("bob@example.com", "alice@example.com")
        assert first["conversation_id"] == second["conversation_id"]

    def test_find_direct_conversation_returns_none_when_absent(self, chat_store):
        assert chat_store.find_direct_conversation("alice@example.com", "carol@example.com") is None


class TestGroupConversationCreation:
    def test_create_group_conversation(self, chat_store):
        conv = chat_store.create_group_conversation(
            name="Ops Team",
            owner_email="alice@example.com",
            participant_emails=["bob@example.com", "carol@example.com"],
        )
        assert conv["type"] == "group"
        assert conv["name"] == "Ops Team"
        assert conv["owner_email"] == "alice@example.com"
        # Requirement 1.3: owner is included as a participant automatically
        assert set(conv["participants"]) == {
            "alice@example.com",
            "bob@example.com",
            "carol@example.com",
        }

    def test_rename_group_by_owner(self, chat_store):
        conv = chat_store.create_group_conversation(
            name="Ops Team",
            owner_email="alice@example.com",
            participant_emails=["bob@example.com", "carol@example.com"],
        )
        renamed = chat_store.rename_group(conv["conversation_id"], "Ops & Infra Team")
        assert renamed["name"] == "Ops & Infra Team"

    def test_rename_group_returns_none_for_direct_conversation(self, chat_store):
        """rename_group() must refuse to operate on a Direct_Conversation
        (only groups have a name/owner)."""
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        assert chat_store.rename_group(conv["conversation_id"], "Not A Group") is None


class TestParticipantVisibilityRules:
    def test_add_participant_grants_future_but_not_retroactive_visibility(self, chat_store):
        """Requirement 1.6: a Team_Member added to a Group_Conversation
        sees Messages sent from that point onward, not messages sent
        before they were added. list_messages() returns full retained
        history regardless of caller (filtering by joined_at is the
        route handler's responsibility per chat_store.py's module
        docstring) — this test verifies add_participant() itself does
        not delete or rewrite any prior message history, and that the
        new participant is correctly added to the conversation."""
        conv = chat_store.create_group_conversation(
            name="Ops Team",
            owner_email="alice@example.com",
            participant_emails=["bob@example.com"],
        )
        chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content="before dan joins", attachment=None)

        updated = chat_store.add_participant(conv["conversation_id"], "dan@example.com")
        assert "dan@example.com" in updated["participants"]

        # Prior message history is untouched by adding a participant.
        messages = chat_store.list_messages(conv["conversation_id"])
        assert len(messages) == 1
        assert messages[0]["content"] == "before dan joins"

        # dan now sees this conversation in his list.
        dans_conversations = chat_store.list_conversations_for_user("dan@example.com")
        assert any(c["conversation_id"] == conv["conversation_id"] for c in dans_conversations)

    def test_remove_participant_revokes_future_access_but_keeps_prior_history(self, chat_store):
        """Requirement 1.7: removal immediately revokes access to NEW
        activity (list_conversations_for_user no longer returns it) but
        does not delete the removed Team_Member's prior view of message
        history (list_messages() for the conversation is unaffected)."""
        conv = chat_store.create_group_conversation(
            name="Ops Team",
            owner_email="alice@example.com",
            participant_emails=["bob@example.com"],
        )
        chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content="hello bob", attachment=None)

        chat_store.remove_participant(conv["conversation_id"], "bob@example.com")

        bobs_conversations = chat_store.list_conversations_for_user("bob@example.com")
        assert not any(c["conversation_id"] == conv["conversation_id"] for c in bobs_conversations)

        # Message history is preserved regardless of current participant list.
        messages = chat_store.list_messages(conv["conversation_id"])
        assert len(messages) == 1
        assert messages[0]["content"] == "hello bob"

    def test_list_conversations_for_user_only_returns_own_conversations(self, chat_store):
        conv_ab = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        chat_store.create_direct_conversation("carol@example.com", "dan@example.com")

        alice_conversations = chat_store.list_conversations_for_user("alice@example.com")
        assert len(alice_conversations) == 1
        assert alice_conversations[0]["conversation_id"] == conv_ab["conversation_id"]


class TestMessageSendAndList:
    def test_send_and_list_messages_ascending_chronological_order(self, chat_store):
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        for text in ["first", "second", "third"]:
            chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content=text, attachment=None)
            time.sleep(0.01)  # ensure distinct millisecond-precision message_id prefixes

        messages = chat_store.list_messages(conv["conversation_id"])
        assert [m["content"] for m in messages] == ["first", "second", "third"]

    def test_send_message_rejects_empty_content_with_no_attachment(self, chat_store):
        """Requirement 2.7."""
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        with pytest.raises(chat_store.ChatStoreError):
            chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content="", attachment=None)

    def test_send_message_with_attachment_only_succeeds(self, chat_store):
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        attachment = {"s3_key": "chat/x/y/z-photo.jpg", "filename": "photo.jpg", "content_type": "image/jpeg", "size_bytes": 1024, "kind": "photo"}
        msg = chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content=None, attachment=attachment)
        assert msg["attachment"] == attachment
        assert msg["content"] is None

    def test_send_message_touches_conversation_last_activity(self, chat_store):
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        original_last_activity = conv["last_activity_at"]
        time.sleep(0.01)
        chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content="hi", attachment=None)
        updated = chat_store.get_conversation(conv["conversation_id"])
        assert updated["last_activity_at"] != original_last_activity


class TestMessageEditWindow:
    def test_edit_within_window_succeeds(self, chat_store):
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        msg = chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content="oops", attachment=None)

        edited = chat_store.edit_message(
            msg["message_id"], editor_email="alice@example.com", new_content="fixed", conversation_id=conv["conversation_id"]
        )
        assert edited["content"] == "fixed"
        assert edited["edited_at"] is not None

    def test_edit_outside_window_is_rejected(self, chat_store):
        """Requirement 3.4/3.7: server-side UTC time comparison — edit
        attempted more than 10 minutes after sent_at must be rejected."""
        with freeze_time("2026-01-01 12:00:00"):
            conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
            msg = chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content="oops", attachment=None)

        with freeze_time("2026-01-01 12:10:01"):  # 10 minutes + 1 second later
            with pytest.raises(chat_store.EditWindowExpiredError):
                chat_store.edit_message(
                    msg["message_id"], editor_email="alice@example.com", new_content="too late", conversation_id=conv["conversation_id"]
                )

    def test_edit_just_before_window_expiry_succeeds(self, chat_store):
        """Boundary check just inside the 10-minute window."""
        with freeze_time("2026-01-01 12:00:00"):
            conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
            msg = chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content="oops", attachment=None)

        with freeze_time("2026-01-01 12:09:59"):  # 9 minutes 59 seconds later
            edited = chat_store.edit_message(
                msg["message_id"], editor_email="alice@example.com", new_content="fixed in time", conversation_id=conv["conversation_id"]
            )
        assert edited["content"] == "fixed in time"

    def test_edit_by_non_sender_is_rejected_regardless_of_window(self, chat_store):
        """Requirement 3.5."""
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        msg = chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content="hello", attachment=None)

        with pytest.raises(chat_store.NotMessageSenderError):
            chat_store.edit_message(
                msg["message_id"], editor_email="bob@example.com", new_content="hijacked", conversation_id=conv["conversation_id"]
            )

    def test_edit_rejects_empty_new_content(self, chat_store):
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        msg = chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content="hello", attachment=None)

        with pytest.raises(chat_store.ChatStoreError):
            chat_store.edit_message(
                msg["message_id"], editor_email="alice@example.com", new_content="   ", conversation_id=conv["conversation_id"]
            )

    def test_edit_rejects_attachment_only_message(self, chat_store):
        """Requirement 3.6: a Message with only an Attachment and no
        text is not eligible for text editing."""
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        attachment = {"s3_key": "chat/x/y/z-photo.jpg", "filename": "photo.jpg", "content_type": "image/jpeg", "size_bytes": 1024, "kind": "photo"}
        msg = chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content=None, attachment=attachment)

        with pytest.raises(chat_store.ChatStoreError):
            chat_store.edit_message(
                msg["message_id"], editor_email="alice@example.com", new_content="adding text now", conversation_id=conv["conversation_id"]
            )


class TestReadReceipts:
    def test_mark_read_updates_read_by_map(self, chat_store):
        """Requirement 2.5."""
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        msg = chat_store.send_message(conv["conversation_id"], sender_email="alice@example.com", content="hello", attachment=None)

        chat_store.mark_read(conv["conversation_id"], [msg["message_id"]], reader_email="bob@example.com")

        updated = chat_store.get_message(msg["message_id"], conversation_id=conv["conversation_id"])
        assert "bob@example.com" in updated["read_by"]

    def test_mark_read_ignores_unknown_message_ids_without_error(self, chat_store):
        conv = chat_store.create_direct_conversation("alice@example.com", "bob@example.com")
        # Should not raise even though this message_id doesn't exist.
        chat_store.mark_read(conv["conversation_id"], ["nonexistent-id"], reader_email="bob@example.com")
