"""
Chat/Call feature (job-portal-chat-voice spec) — file/photo attachment
presigned upload/download.

Adapted from note_attachments.py's conventions (_safe_filename(),
custom *AttachmentError class) but using PRESIGNED PUT instead of
backend-proxied upload: the browser uploads the file bytes directly to
S3 using a presigned URL this module issues, never passing the file
body through the FastAPI backend's own request body. This is necessary
because chat Attachments allow files up to 50MB (Requirement 4.2),
which should not be proxied through the backend the way
note_attachments.py's smaller 20MB cap is.

Key structure: chat/{conversation_id}/{message_id}/{attachment_id}-{safe_filename}
(per design.md) — matches note_attachments.py's job/note-scoped key
convention, adapted to this feature's conversation/message scoping.

Access control (Requirement 4.7/9.5): generate_presigned_get_url()
verifies the requesting Team_Member is a CURRENT participant of the
owning Conversation (via chat_store.get_conversation()) before issuing
a GET URL — never issued to a non-participant, and never a permanent
public link. generate_presigned_put_url() does NOT itself verify
participancy — main.py's route handler (POST
/api/chat/conversations/{id}/attachments/presign) is responsible for
that check before calling into this module, since only the route
handler has an authenticated request context; this module stays a
plain S3/validation helper with no notion of "who is calling".
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Optional

import boto3
from botocore.config import Config

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET_NAME = os.environ.get("CHAT_ATTACHMENTS_BUCKET_NAME", "")

# Requirement 4.1/4.2: photo vs. non-photo file size caps.
MAX_PHOTO_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# Requirement 4.1: accepted photo content types — anything else (even if
# it happens to have an image/* prefix outside this allow-list, e.g. a
# rare or legacy image format) is treated as a non-photo "file" for
# size-limit purposes, matching the plainly-listed JPEG/PNG/GIF/WEBP set.
PHOTO_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}

# Per design.md: both the presigned PUT (upload) and presigned GET
# (view/download) URLs expire after 5 minutes. GET is regenerated fresh
# on every call (never cached/stored) — see generate_presigned_get_url().
PRESIGNED_PUT_EXPIRY_SECONDS = 300
PRESIGNED_GET_EXPIRY_SECONDS = 300


class ChatAttachmentError(ValueError):
    """Raised for any invalid upload/access request — message is safe
    to show the end user, matching NoteAttachmentError's convention."""


_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        # Force SigV4 explicitly — same reasoning as note_attachments.py:
        # this bucket has SSE-KMS as its default encryption, and S3
        # requires SigV4 for any request against an SSE-KMS object,
        # including presigned URLs (both PUT and GET) generated below.
        _s3_client = boto3.client(
            "s3",
            region_name=AWS_REGION,
            config=Config(signature_version="s3v4"),
        )
    return _s3_client


def _safe_filename(filename: str) -> str:
    """Strip path separators and any character that isn't safe in an S3
    key, so a malicious/odd filename can't traverse the key namespace or
    break URL encoding. Identical behaviour to note_attachments.py's
    helper of the same name."""
    base = os.path.basename(filename or "file")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return cleaned[:150] or "file"


def is_photo_content_type(content_type: Optional[str]) -> bool:
    return (content_type or "").lower() in PHOTO_CONTENT_TYPES


def _max_bytes_for(content_type: Optional[str]) -> int:
    return MAX_PHOTO_BYTES if is_photo_content_type(content_type) else MAX_FILE_BYTES


def build_attachment_key(conversation_id: str, message_id: str, attachment_id: str, filename: str) -> str:
    """Requirement matches design.md's key structure exactly:
    chat/{conversation_id}/{message_id}/{attachment_id}-{safe_filename}."""
    safe_name = _safe_filename(filename)
    return f"chat/{conversation_id}/{message_id}/{attachment_id}-{safe_name}"


def generate_presigned_put_url(
    *,
    conversation_id: str,
    message_id: str,
    filename: str,
    content_type: Optional[str],
    size_bytes: int,
) -> dict:
    """
    Requirement 4.1/4.2/4.3: validate the applicable size limit BEFORE
    issuing a presigned PUT URL — a photo (recognised content type) is
    capped at MAX_PHOTO_BYTES, everything else at MAX_FILE_BYTES.
    Raises ChatAttachmentError with a message naming the limit that was
    exceeded if size_bytes is over the applicable cap, so the caller can
    reject the upload before it starts, per Requirement 4.3.

    Returns {"attachment_id", "s3_key", "put_url", "expires_in_seconds"}.
    The caller (main.py's route handler) is expected to have ALREADY
    verified the requester is a participant of conversation_id before
    calling this — see module docstring.
    """
    if not BUCKET_NAME:
        raise ChatAttachmentError("Attachment storage is not configured on this server")
    if size_bytes <= 0:
        raise ChatAttachmentError("Attachment cannot be empty")

    content_type = content_type or "application/octet-stream"
    is_photo = is_photo_content_type(content_type)
    max_bytes = _max_bytes_for(content_type)
    if size_bytes > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        kind = "photo" if is_photo else "file"
        raise ChatAttachmentError(f"This {kind} exceeds the {limit_mb} MB limit for {kind}s")

    attachment_id = str(uuid.uuid4())
    s3_key = build_attachment_key(conversation_id, message_id, attachment_id, filename)

    put_url = _s3().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": s3_key,
            "ContentType": content_type,
            "ServerSideEncryption": "aws:kms",
        },
        ExpiresIn=PRESIGNED_PUT_EXPIRY_SECONDS,
    )

    return {
        "attachment_id": attachment_id,
        "s3_key": s3_key,
        "put_url": put_url,
        "expires_in_seconds": PRESIGNED_PUT_EXPIRY_SECONDS,
        "content_type": content_type,
        "is_photo": is_photo,
        "filename": filename,
        "size_bytes": size_bytes,
    }


def generate_presigned_get_url(*, s3_key: str, conversation_id: str, requester_email: str) -> str:
    """
    Requirement 4.7/9.5: only issue a GET URL if requester_email is a
    CURRENT participant of conversation_id — checked via
    chat_store.get_conversation() on every call (never cached), so a
    removed participant is denied immediately per Requirement 1.7's
    "revoke access to new activity" semantics extended to attachment
    access. Raises ChatAttachmentError if the conversation does not
    exist or the requester is not a participant.

    Regenerated fresh on every call — never cached/stored, matching
    note_attachments.py's approach, since a stale URL would 403 once it
    expires.
    """
    if not BUCKET_NAME:
        raise ChatAttachmentError("Attachment storage is not configured on this server")

    # Imported here (not at module top level) to avoid a hard import
    # dependency for callers that only need generate_presigned_put_url()
    # (e.g. test_chat_attachments.py's upload-only test cases) and to
    # keep this module's import graph simple — chat_store.py itself has
    # no dependency on this module, so no circular import risk either
    # way; this is purely about minimising what gets imported when.
    from . import chat_store

    conversation = chat_store.get_conversation(conversation_id)
    if not conversation:
        raise ChatAttachmentError("Conversation not found")

    requester = requester_email.lower().strip()
    participants = {p.lower().strip() for p in (conversation.get("participants") or [])}
    if requester not in participants:
        raise ChatAttachmentError("You do not have access to this attachment")

    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET_NAME, "Key": s3_key},
        ExpiresIn=PRESIGNED_GET_EXPIRY_SECONDS,
    )
