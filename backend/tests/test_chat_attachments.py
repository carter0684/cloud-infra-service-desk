"""
Tests for app/chat_attachments.py — presigned PUT/GET generation for
chat file/photo attachments (job-portal-chat-voice spec).

Uses moto's S3 mock (via the `chat_attachments_bucket` fixture below) for
the presigned-URL-issuing calls, and the `chat_tables` fixture
(conftest.py) so chat_store.py's production (DynamoDB) branch backs
generate_presigned_get_url()'s participant check.
"""

from __future__ import annotations

import importlib

import boto3
import pytest
from moto import mock_aws

BUCKET_NAME = "job-portal-chat-attachments-100000000002"


@pytest.fixture
def chat_attachments_bucket(chat_tables, monkeypatch):
    """chat_tables already wraps everything in a single mock_aws()
    context (see conftest.py) — S3 buckets created inside that SAME
    context are visible to any boto3 S3 client created for the
    remainder of the test, so this fixture just creates the bucket
    within that already-active mock, rather than opening a second
    mock_aws() context (moto does not support nesting two separate
    mock_aws() contexts against the same account cleanly)."""
    monkeypatch.setenv("CHAT_ATTACHMENTS_BUCKET_NAME", BUCKET_NAME)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET_NAME)
    return s3


@pytest.fixture
def chat_attachments(chat_attachments_bucket):
    """Reload AFTER the bucket env var is set, so the module's
    module-level BUCKET_NAME constant picks it up — same
    importlib.reload pattern as every other *_store.py test fixture in
    this suite."""
    from app import chat_attachments as chat_attachments_module
    importlib.reload(chat_attachments_module)
    return chat_attachments_module


@pytest.fixture
def chat_store(chat_tables):
    from app import chat_store as chat_store_module
    importlib.reload(chat_store_module)
    return chat_store_module


CONVERSATION_ID = "conv-1"
MESSAGE_ID = "msg-1"
ALICE = "alice@example.com"
BOB = "bob@example.com"
OUTSIDER = "outsider@example.com"


class TestBuildAttachmentKey:
    def test_key_matches_the_documented_structure(self, chat_attachments):
        key = chat_attachments.build_attachment_key(CONVERSATION_ID, MESSAGE_ID, "att-1", "My Report.pdf")
        assert key == f"chat/{CONVERSATION_ID}/{MESSAGE_ID}/att-1-My_Report.pdf"

    def test_filename_is_sanitised(self, chat_attachments):
        key = chat_attachments.build_attachment_key(CONVERSATION_ID, MESSAGE_ID, "att-1", "../../etc/passwd")
        assert "../" not in key
        assert key.startswith(f"chat/{CONVERSATION_ID}/{MESSAGE_ID}/att-1-")


class TestIsPhotoContentType:
    @pytest.mark.parametrize("content_type", ["image/jpeg", "image/png", "image/gif", "image/webp", "IMAGE/PNG"])
    def test_recognised_photo_types(self, chat_attachments, content_type):
        assert chat_attachments.is_photo_content_type(content_type) is True

    @pytest.mark.parametrize("content_type", ["application/pdf", "video/mp4", None, ""])
    def test_non_photo_types(self, chat_attachments, content_type):
        assert chat_attachments.is_photo_content_type(content_type) is False


class TestGeneratePresignedPutUrl:
    def test_photo_within_10mb_limit_succeeds(self, chat_attachments):
        result = chat_attachments.generate_presigned_put_url(
            conversation_id=CONVERSATION_ID,
            message_id=MESSAGE_ID,
            filename="photo.jpg",
            content_type="image/jpeg",
            size_bytes=5 * 1024 * 1024,  # 5 MB
        )
        assert result["put_url"]
        assert result["is_photo"] is True
        assert result["s3_key"].startswith(f"chat/{CONVERSATION_ID}/{MESSAGE_ID}/")

    def test_photo_over_10mb_limit_is_rejected(self, chat_attachments):
        with pytest.raises(chat_attachments.ChatAttachmentError, match="10 MB"):
            chat_attachments.generate_presigned_put_url(
                conversation_id=CONVERSATION_ID,
                message_id=MESSAGE_ID,
                filename="photo.jpg",
                content_type="image/jpeg",
                size_bytes=11 * 1024 * 1024,  # 11 MB
            )

    def test_photo_exactly_at_10mb_limit_succeeds(self, chat_attachments):
        result = chat_attachments.generate_presigned_put_url(
            conversation_id=CONVERSATION_ID,
            message_id=MESSAGE_ID,
            filename="photo.jpg",
            content_type="image/jpeg",
            size_bytes=10 * 1024 * 1024,  # exactly 10 MB
        )
        assert result["put_url"]

    def test_non_photo_file_within_50mb_limit_succeeds(self, chat_attachments):
        result = chat_attachments.generate_presigned_put_url(
            conversation_id=CONVERSATION_ID,
            message_id=MESSAGE_ID,
            filename="report.pdf",
            content_type="application/pdf",
            size_bytes=40 * 1024 * 1024,  # 40 MB
        )
        assert result["put_url"]
        assert result["is_photo"] is False

    def test_non_photo_file_over_50mb_limit_is_rejected(self, chat_attachments):
        with pytest.raises(chat_attachments.ChatAttachmentError, match="50 MB"):
            chat_attachments.generate_presigned_put_url(
                conversation_id=CONVERSATION_ID,
                message_id=MESSAGE_ID,
                filename="report.pdf",
                content_type="application/pdf",
                size_bytes=51 * 1024 * 1024,  # 51 MB
            )

    def test_a_large_file_misdeclared_as_a_photo_content_type_is_still_capped_at_10mb(self, chat_attachments):
        """Requirement 4.1/4.3: the 10MB cap applies based on the
        DECLARED content type being a recognised photo type — a caller
        cannot bypass the stricter photo limit by simply sending 40MB
        of bytes under an image/jpeg content type."""
        with pytest.raises(chat_attachments.ChatAttachmentError, match="10 MB"):
            chat_attachments.generate_presigned_put_url(
                conversation_id=CONVERSATION_ID,
                message_id=MESSAGE_ID,
                filename="huge.jpg",
                content_type="image/jpeg",
                size_bytes=40 * 1024 * 1024,
            )

    def test_zero_byte_upload_is_rejected(self, chat_attachments):
        with pytest.raises(chat_attachments.ChatAttachmentError):
            chat_attachments.generate_presigned_put_url(
                conversation_id=CONVERSATION_ID,
                message_id=MESSAGE_ID,
                filename="empty.txt",
                content_type="text/plain",
                size_bytes=0,
            )

    def test_missing_content_type_defaults_to_non_photo_file_limit(self, chat_attachments):
        result = chat_attachments.generate_presigned_put_url(
            conversation_id=CONVERSATION_ID,
            message_id=MESSAGE_ID,
            filename="unknown",
            content_type=None,
            size_bytes=20 * 1024 * 1024,  # 20 MB — over photo cap, under file cap
        )
        assert result["is_photo"] is False

    def test_each_call_generates_a_unique_attachment_id(self, chat_attachments):
        first = chat_attachments.generate_presigned_put_url(
            conversation_id=CONVERSATION_ID, message_id=MESSAGE_ID, filename="a.txt", content_type="text/plain", size_bytes=100
        )
        second = chat_attachments.generate_presigned_put_url(
            conversation_id=CONVERSATION_ID, message_id=MESSAGE_ID, filename="a.txt", content_type="text/plain", size_bytes=100
        )
        assert first["attachment_id"] != second["attachment_id"]
        assert first["s3_key"] != second["s3_key"]


class TestGeneratePresignedGetUrl:
    def test_participant_can_get_a_url(self, chat_attachments, chat_store):
        conv = chat_store.create_direct_conversation(ALICE, BOB)
        url = chat_attachments.generate_presigned_get_url(
            s3_key=f"chat/{conv['conversation_id']}/{MESSAGE_ID}/att-1-file.txt",
            conversation_id=conv["conversation_id"],
            requester_email=ALICE,
        )
        assert url.startswith("https://")

    def test_other_participant_can_also_get_a_url(self, chat_attachments, chat_store):
        conv = chat_store.create_direct_conversation(ALICE, BOB)
        url = chat_attachments.generate_presigned_get_url(
            s3_key=f"chat/{conv['conversation_id']}/{MESSAGE_ID}/att-1-file.txt",
            conversation_id=conv["conversation_id"],
            requester_email=BOB,
        )
        assert url.startswith("https://")

    def test_non_participant_is_rejected(self, chat_attachments, chat_store):
        """Requirement 4.7/9.5: attachment access is restricted to
        Conversation participants."""
        conv = chat_store.create_direct_conversation(ALICE, BOB)
        with pytest.raises(chat_attachments.ChatAttachmentError):
            chat_attachments.generate_presigned_get_url(
                s3_key=f"chat/{conv['conversation_id']}/{MESSAGE_ID}/att-1-file.txt",
                conversation_id=conv["conversation_id"],
                requester_email=OUTSIDER,
            )

    def test_unknown_conversation_is_rejected(self, chat_attachments, chat_store):
        with pytest.raises(chat_attachments.ChatAttachmentError):
            chat_attachments.generate_presigned_get_url(
                s3_key="chat/does-not-exist/msg/att-1-file.txt",
                conversation_id="does-not-exist",
                requester_email=ALICE,
            )

    def test_removed_participant_loses_access(self, chat_attachments, chat_store):
        """Requirement 1.7's revocation semantics extended to attachment
        access — a Team_Member removed from a Group_Conversation must
        immediately lose access to new attachment GET URLs."""
        conv = chat_store.create_group_conversation(name="Team", owner_email=ALICE, participant_emails=[BOB])
        chat_store.remove_participant(conv["conversation_id"], BOB)

        with pytest.raises(chat_attachments.ChatAttachmentError):
            chat_attachments.generate_presigned_get_url(
                s3_key=f"chat/{conv['conversation_id']}/{MESSAGE_ID}/att-1-file.txt",
                conversation_id=conv["conversation_id"],
                requester_email=BOB,
            )
