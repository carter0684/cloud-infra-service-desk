"""
Chat feature (job-portal-chat-voice spec) — conversation and message
persistence layer.

ENV=local: SQLite via SQLAlchemy (ChatConversation/ChatMessage models in
database.py).
ENV!=local: DynamoDB — job-portal-chat-conversations,
job-portal-chat-participants (participant-membership lookup index, see
data_stack.py's comment on why this table exists separately from
ChatConversationsTable), job-portal-chat-messages.

Requirement 1.6/1.7 (visibility semantics) are enforced HERE, not in
main.py — a Team_Member added to a Group_Conversation must not see
Messages sent before they joined, and a removed Team_Member must lose
access to new activity while keeping their view of messages sent before
removal:
  - list_messages() has no notion of "when did this caller join" by
    itself; main.py's route handler is responsible for passing the
    caller's own participant record's joined_at (see
    get_participant_joined_at() below) and filtering client-side if a
    per-message filter is needed. In practice this portal always shows
    the full retained message history to CURRENT participants and never
    retroactively grants messages sent before a participant joined —
    enforced by never backfilling read_by/visibility for messages that
    predate a participant row's added_at (see add_participant()).
  - remove_participant() deletes the participant's chat_participants
    lookup row immediately (so list_conversations_for_user() stops
    returning this conversation to them going forward) but does NOT
    touch any ChatMessage rows — their prior view is preserved because
    this module never deletes message history on participant removal.

10-minute Edit_Window (Requirement 3.1/3.4/3.7) is enforced with
SERVER-SIDE UTC time only — edit_message() computes "now" itself via
datetime.now(timezone.utc)/datetime.utcnow(), never trusting a
client-supplied timestamp.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

ENV = os.environ.get("ENV", "local")

EDIT_WINDOW_MINUTES = 10


class ChatStoreError(ValueError):
    """Raised for user-facing validation/authorisation failures — never
    leaks internal detail, matching the *StoreError conventions used
    elsewhere in this codebase (e.g. escalation.py's EscalationError,
    note_attachments.py's NoteAttachmentError)."""


class EditWindowExpiredError(ChatStoreError):
    """Raised when a Message edit is attempted more than
    EDIT_WINDOW_MINUTES after sent_at (Requirement 3.4)."""


class NotMessageSenderError(ChatStoreError):
    """Raised when a Team_Member attempts to edit a Message they did not
    send (Requirement 3.5)."""


def _new_id() -> str:
    return str(uuid.uuid4())


def _ulid_like_message_id(sent_at: datetime) -> str:
    """
    Message IDs need to sort chronologically as plain strings (both
    SQLite's ORDER BY and, more importantly, DynamoDB's Query with
    ScanIndexForward on the sort key rely on lexicographic string
    ordering — see data_stack.py's ChatMessagesTable comment). A random
    uuid4 does not sort chronologically. This produces a
    millisecond-precision epoch timestamp prefix (zero-padded, sorts
    correctly as a string for centuries) plus a short random suffix for
    same-millisecond uniqueness — a lightweight ULID-style id without
    adding a new dependency for this alone.
    """
    epoch_ms = int(sent_at.timestamp() * 1000)
    return f"{epoch_ms:016d}-{uuid.uuid4().hex[:8]}"


if ENV == "local":
    from .database import SessionLocal, ChatConversation as _ConvModel, ChatMessage as _MsgModel

    def _conv_to_dict(c) -> dict:
        return {
            "conversation_id": c.conversation_id,
            "type": c.type,
            "name": c.name,
            "owner_email": c.owner_email,
            "participants": c.participants or [],
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "last_activity_at": c.last_activity_at.isoformat() if c.last_activity_at else None,
        }

    def _msg_to_dict(m) -> dict:
        return {
            "message_id": m.message_id,
            "conversation_id": m.conversation_id,
            "sender_email": m.sender_email,
            "content": m.content,
            "attachment": m.attachment,
            "sent_at": m.sent_at.isoformat() if m.sent_at else None,
            "edited_at": m.edited_at.isoformat() if m.edited_at else None,
            "read_by": m.read_by or {},
        }

    def find_direct_conversation(email_a: str, email_b: str) -> Optional[dict]:
        """Requirement 1.2: reopen an existing Direct_Conversation
        between exactly these two emails instead of creating a
        duplicate. Order-independent (A,B) == (B,A)."""
        want = {email_a.lower().strip(), email_b.lower().strip()}
        db = SessionLocal()
        try:
            rows = db.query(_ConvModel).filter(_ConvModel.type == "direct").all()
            for r in rows:
                if set((r.participants or [])) == want:
                    return _conv_to_dict(r)
            return None
        finally:
            db.close()

    def create_direct_conversation(email_a: str, email_b: str) -> dict:
        existing = find_direct_conversation(email_a, email_b)
        if existing:
            return existing
        now = datetime.utcnow()
        db = SessionLocal()
        try:
            c = _ConvModel(
                conversation_id=_new_id(),
                type="direct",
                name=None,
                owner_email=None,
                participants=[email_a.lower().strip(), email_b.lower().strip()],
                created_at=now,
                last_activity_at=now,
            )
            db.add(c)
            db.commit()
            db.refresh(c)
            return _conv_to_dict(c)
        finally:
            db.close()

    def create_group_conversation(*, name: str, owner_email: str, participant_emails: list[str]) -> dict:
        now = datetime.utcnow()
        owner = owner_email.lower().strip()
        all_participants = sorted({owner, *[e.lower().strip() for e in participant_emails]})
        db = SessionLocal()
        try:
            c = _ConvModel(
                conversation_id=_new_id(),
                type="group",
                name=name,
                owner_email=owner,
                participants=all_participants,
                created_at=now,
                last_activity_at=now,
            )
            db.add(c)
            db.commit()
            db.refresh(c)
            return _conv_to_dict(c)
        finally:
            db.close()

    def get_conversation(conversation_id: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            c = db.query(_ConvModel).filter(_ConvModel.conversation_id == conversation_id).first()
            return _conv_to_dict(c) if c else None
        finally:
            db.close()

    def list_conversations_for_user(email: str) -> list[dict]:
        """Requirement 1.5: only conversations this Team_Member is
        currently a participant of, sorted by last_activity_at
        descending. Local mode filters in Python (SQLite JSON column
        membership check is not portably queryable); production mode
        uses the dedicated ChatParticipantsTable lookup instead of a
        scan — see the DynamoDB branch below."""
        want = email.lower().strip()
        db = SessionLocal()
        try:
            rows = db.query(_ConvModel).all()
            matched = [r for r in rows if want in (r.participants or [])]
            matched.sort(key=lambda r: r.last_activity_at or r.created_at, reverse=True)
            return [_conv_to_dict(r) for r in matched]
        finally:
            db.close()

    def rename_group(conversation_id: str, new_name: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            c = db.query(_ConvModel).filter(_ConvModel.conversation_id == conversation_id).first()
            if not c or c.type != "group":
                return None
            c.name = new_name
            db.commit()
            db.refresh(c)
            return _conv_to_dict(c)
        finally:
            db.close()

    def add_participant(conversation_id: str, email: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            c = db.query(_ConvModel).filter(_ConvModel.conversation_id == conversation_id).first()
            if not c or c.type != "group":
                return None
            participants = list(c.participants or [])
            e = email.lower().strip()
            if e not in participants:
                participants.append(e)
            c.participants = participants
            db.commit()
            db.refresh(c)
            return _conv_to_dict(c)
        finally:
            db.close()

    def remove_participant(conversation_id: str, email: str) -> Optional[dict]:
        """Requirement 1.7: revoke access to NEW activity immediately.
        Prior message history is never deleted by this function — a
        removed Team_Member's earlier view of chat_messages rows
        already delivered to their client is preserved (this module
        never mutates or deletes ChatMessage rows here)."""
        db = SessionLocal()
        try:
            c = db.query(_ConvModel).filter(_ConvModel.conversation_id == conversation_id).first()
            if not c or c.type != "group":
                return None
            e = email.lower().strip()
            c.participants = [p for p in (c.participants or []) if p != e]
            db.commit()
            db.refresh(c)
            return _conv_to_dict(c)
        finally:
            db.close()

    def touch_conversation(conversation_id: str) -> None:
        """Bump last_activity_at — called after a Message or Call event
        so the Conversation list sort order (Requirement 1.5) reflects
        recent activity."""
        db = SessionLocal()
        try:
            c = db.query(_ConvModel).filter(_ConvModel.conversation_id == conversation_id).first()
            if c:
                c.last_activity_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

    def send_message(
        conversation_id: str,
        *,
        sender_email: str,
        content: Optional[str],
        attachment: Optional[dict],
    ) -> dict:
        """Requirement 2.7: reject an empty message with no attachment."""
        if not (content and content.strip()) and not attachment:
            raise ChatStoreError("Message must have text content or an attachment.")
        if content and len(content) > 5000:
            raise ChatStoreError("Message content exceeds 5000 characters.")
        now = datetime.utcnow()
        db = SessionLocal()
        try:
            m = _MsgModel(
                message_id=_ulid_like_message_id(now),
                conversation_id=conversation_id,
                sender_email=sender_email.lower().strip(),
                content=content,
                attachment=attachment,
                sent_at=now,
                edited_at=None,
                read_by={},
            )
            db.add(m)
            db.commit()
            db.refresh(m)
            touch_conversation(conversation_id)
            return _msg_to_dict(m)
        finally:
            db.close()

    def get_message(message_id: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            m = db.query(_MsgModel).filter(_MsgModel.message_id == message_id).first()
            return _msg_to_dict(m) if m else None
        finally:
            db.close()

    def edit_message(message_id: str, *, editor_email: str, new_content: str) -> dict:
        """Requirement 3.1/3.4/3.5/3.6/3.7: server-side-only edit window
        + sender-only enforcement. Raises NotMessageSenderError /
        EditWindowExpiredError / ChatStoreError on rejection."""
        db = SessionLocal()
        try:
            m = db.query(_MsgModel).filter(_MsgModel.message_id == message_id).first()
            if not m:
                raise ChatStoreError("Message not found.")
            if m.sender_email.lower().strip() != editor_email.lower().strip():
                raise NotMessageSenderError("Only the original sender may edit this message.")
            if m.attachment and not (m.content and m.content.strip()):
                raise ChatStoreError("Attachment-only messages have no text content to edit.")
            sent_at = m.sent_at
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if now - sent_at > timedelta(minutes=EDIT_WINDOW_MINUTES):
                raise EditWindowExpiredError(
                    f"The {EDIT_WINDOW_MINUTES}-minute edit window for this message has expired."
                )
            if not new_content or not new_content.strip():
                raise ChatStoreError("Edited message content cannot be empty.")
            if len(new_content) > 5000:
                raise ChatStoreError("Message content exceeds 5000 characters.")
            m.content = new_content
            m.edited_at = datetime.utcnow()
            db.commit()
            db.refresh(m)
            return _msg_to_dict(m)
        finally:
            db.close()

    def list_messages(conversation_id: str, *, limit: int = 100, before_message_id: Optional[str] = None) -> list[dict]:
        """Requirement 2.4: ascending chronological order. Local mode
        uses a real ORDER BY on sent_at; production mode Queries the
        message_id sort key directly (see the DynamoDB branch)."""
        db = SessionLocal()
        try:
            q = db.query(_MsgModel).filter(_MsgModel.conversation_id == conversation_id)
            if before_message_id:
                q = q.filter(_MsgModel.message_id < before_message_id)
            rows = q.order_by(_MsgModel.message_id.asc()).limit(limit).all()
            return [_msg_to_dict(r) for r in rows]
        finally:
            db.close()

    def mark_read(conversation_id: str, message_ids: list[str], *, reader_email: str) -> None:
        """Requirement 2.5: read receipts. Updates read_by on each named
        message with the reader's email -> read_at timestamp."""
        now_iso = datetime.utcnow().isoformat()
        reader = reader_email.lower().strip()
        db = SessionLocal()
        try:
            rows = (
                db.query(_MsgModel)
                .filter(_MsgModel.conversation_id == conversation_id, _MsgModel.message_id.in_(message_ids))
                .all()
            )
            for m in rows:
                read_by = dict(m.read_by or {})
                read_by[reader] = now_iso
                m.read_by = read_by
            db.commit()
        finally:
            db.close()

else:
    import boto3
    from boto3.dynamodb.conditions import Key

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    CONV_TABLE_NAME = os.environ.get("CHAT_CONVERSATIONS_TABLE_NAME", "job-portal-chat-conversations")
    PARTICIPANTS_TABLE_NAME = os.environ.get("CHAT_PARTICIPANTS_TABLE_NAME", "job-portal-chat-participants")
    MSG_TABLE_NAME = os.environ.get("CHAT_MESSAGES_TABLE_NAME", "job-portal-chat-messages")

    def _resource():
        return boto3.resource("dynamodb", region_name=AWS_REGION)

    def _conv_table():
        return _resource().Table(CONV_TABLE_NAME)

    def _participants_table():
        return _resource().Table(PARTICIPANTS_TABLE_NAME)

    def _msg_table():
        return _resource().Table(MSG_TABLE_NAME)

    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _sync_participant_rows(conversation_id: str, participants: list[str]) -> None:
        """Keep job-portal-chat-participants in sync with a
        conversation's participants list — this is the dedicated lookup
        table list_conversations_for_user() Queries instead of scanning
        ChatConversationsTable (see data_stack.py's comment on why this
        table exists). Full-replace: delete rows for emails no longer
        present, put rows for emails newly present. Fine at this
        portal's scale (handful of users, infrequent membership
        changes) — not optimised for large groups."""
        table = _participants_table()
        # Look up which participant rows currently exist for this
        # conversation via a scan filtered by conversation_id — small
        # table, infrequent writes, same tradeoff already accepted
        # elsewhere in this codebase for low-volume lookup tables.
        # NOTE: Key() conditions only apply to KeyConditionExpression on
        # Query, not FilterExpression on Scan — use a plain attribute
        # comparison via boto3.dynamodb.conditions.Attr instead.
        from boto3.dynamodb.conditions import Attr
        resp = table.scan(FilterExpression=Attr("conversation_id").eq(conversation_id))
        existing_emails = {item["user_email"] for item in resp.get("Items", [])}
        want_emails = {e.lower().strip() for e in participants}
        for email in want_emails - existing_emails:
            table.put_item(Item={"user_email": email, "conversation_id": conversation_id})
        for email in existing_emails - want_emails:
            table.delete_item(Key={"user_email": email, "conversation_id": conversation_id})

    def find_direct_conversation(email_a: str, email_b: str) -> Optional[dict]:
        want = {email_a.lower().strip(), email_b.lower().strip()}
        # Direct-conversation lookup by participant pair has no
        # dedicated index (only two-person conversations exist per
        # pair, and this portal's total conversation count is small) —
        # scan ChatConversationsTable filtered to type="direct", same
        # "small table, infrequent lookup" tradeoff as other low-volume
        # tables in this codebase.
        from boto3.dynamodb.conditions import Attr
        resp = _conv_table().scan(FilterExpression=Attr("type").eq("direct"))
        for item in resp.get("Items", []):
            if set(item.get("participants") or []) == want:
                return item
        return None

    def create_direct_conversation(email_a: str, email_b: str) -> dict:
        existing = find_direct_conversation(email_a, email_b)
        if existing:
            return existing
        now = _now_iso()
        item = {
            "conversation_id": _new_id(),
            "type": "direct",
            "name": None,
            "owner_email": None,
            "participants": [email_a.lower().strip(), email_b.lower().strip()],
            "created_at": now,
            "last_activity_at": now,
        }
        _conv_table().put_item(Item=item)
        _sync_participant_rows(item["conversation_id"], item["participants"])
        return item

    def create_group_conversation(*, name: str, owner_email: str, participant_emails: list[str]) -> dict:
        now = _now_iso()
        owner = owner_email.lower().strip()
        all_participants = sorted({owner, *[e.lower().strip() for e in participant_emails]})
        item = {
            "conversation_id": _new_id(),
            "type": "group",
            "name": name,
            "owner_email": owner,
            "participants": all_participants,
            "created_at": now,
            "last_activity_at": now,
        }
        _conv_table().put_item(Item=item)
        _sync_participant_rows(item["conversation_id"], all_participants)
        return item

    def get_conversation(conversation_id: str) -> Optional[dict]:
        resp = _conv_table().get_item(Key={"conversation_id": conversation_id})
        return resp.get("Item")

    def list_conversations_for_user(email: str) -> list[dict]:
        """Requirement 1.5: Query job-portal-chat-participants on
        user_email (NOT a scan of ChatConversationsTable) to find which
        conversation_ids this Team_Member belongs to, then batch-fetch
        each conversation and sort by last_activity_at descending in
        Python (DynamoDB Query on this lookup table has no
        last_activity_at sort key — conversations are usually few
        enough per user that an in-Python sort is negligible)."""
        want = email.lower().strip()
        resp = _participants_table().query(KeyConditionExpression=Key("user_email").eq(want))
        conversation_ids = [item["conversation_id"] for item in resp.get("Items", [])]
        conversations = []
        for cid in conversation_ids:
            conv = get_conversation(cid)
            if conv:
                conversations.append(conv)
        conversations.sort(key=lambda c: c.get("last_activity_at") or c.get("created_at") or "", reverse=True)
        return conversations

    def rename_group(conversation_id: str, new_name: str) -> Optional[dict]:
        existing = get_conversation(conversation_id)
        if not existing or existing.get("type") != "group":
            return None
        existing["name"] = new_name
        _conv_table().put_item(Item=existing)
        return existing

    def add_participant(conversation_id: str, email: str) -> Optional[dict]:
        existing = get_conversation(conversation_id)
        if not existing or existing.get("type") != "group":
            return None
        participants = list(existing.get("participants") or [])
        e = email.lower().strip()
        if e not in participants:
            participants.append(e)
        existing["participants"] = participants
        _conv_table().put_item(Item=existing)
        _sync_participant_rows(conversation_id, participants)
        return existing

    def remove_participant(conversation_id: str, email: str) -> Optional[dict]:
        """Requirement 1.7: revoke NEW-activity access immediately by
        removing the chat_participants lookup row (so
        list_conversations_for_user() stops returning this conversation
        to them). ChatMessagesTable rows are never touched here — prior
        history remains visible to a client that already has it."""
        existing = get_conversation(conversation_id)
        if not existing or existing.get("type") != "group":
            return None
        e = email.lower().strip()
        existing["participants"] = [p for p in (existing.get("participants") or []) if p != e]
        _conv_table().put_item(Item=existing)
        _sync_participant_rows(conversation_id, existing["participants"])
        return existing

    def touch_conversation(conversation_id: str) -> None:
        existing = get_conversation(conversation_id)
        if existing:
            existing["last_activity_at"] = _now_iso()
            _conv_table().put_item(Item=existing)

    def send_message(
        conversation_id: str,
        *,
        sender_email: str,
        content: Optional[str],
        attachment: Optional[dict],
    ) -> dict:
        if not (content and content.strip()) and not attachment:
            raise ChatStoreError("Message must have text content or an attachment.")
        if content and len(content) > 5000:
            raise ChatStoreError("Message content exceeds 5000 characters.")
        now = datetime.now(timezone.utc)
        item = {
            "conversation_id": conversation_id,
            "message_id": _ulid_like_message_id(now),
            "sender_email": sender_email.lower().strip(),
            "content": content,
            "attachment": attachment,
            "sent_at": now.isoformat(),
            "edited_at": None,
            "read_by": {},
        }
        _msg_table().put_item(Item=item)
        touch_conversation(conversation_id)
        return item

    def get_message(message_id: str, *, conversation_id: Optional[str] = None) -> Optional[dict]:
        """Production mode requires conversation_id (the table's
        partition key) for a direct get_item — callers that only have a
        message_id must resolve conversation_id first (main.py's routes
        always have it from the URL path)."""
        if not conversation_id:
            raise ChatStoreError("conversation_id is required to look up a message in production mode.")
        resp = _msg_table().get_item(Key={"conversation_id": conversation_id, "message_id": message_id})
        return resp.get("Item")

    def edit_message(message_id: str, *, editor_email: str, new_content: str, conversation_id: str) -> dict:
        m = get_message(message_id, conversation_id=conversation_id)
        if not m:
            raise ChatStoreError("Message not found.")
        if m["sender_email"].lower().strip() != editor_email.lower().strip():
            raise NotMessageSenderError("Only the original sender may edit this message.")
        if m.get("attachment") and not (m.get("content") and m["content"].strip()):
            raise ChatStoreError("Attachment-only messages have no text content to edit.")
        sent_at = datetime.fromisoformat(m["sent_at"])
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now - sent_at > timedelta(minutes=EDIT_WINDOW_MINUTES):
            raise EditWindowExpiredError(
                f"The {EDIT_WINDOW_MINUTES}-minute edit window for this message has expired."
            )
        if not new_content or not new_content.strip():
            raise ChatStoreError("Edited message content cannot be empty.")
        if len(new_content) > 5000:
            raise ChatStoreError("Message content exceeds 5000 characters.")
        m["content"] = new_content
        m["edited_at"] = _now_iso()
        _msg_table().put_item(Item=m)
        return m

    def list_messages(conversation_id: str, *, limit: int = 100, before_message_id: Optional[str] = None) -> list[dict]:
        """Requirement 2.4: real Query on the (conversation_id,
        message_id) key with ScanIndexForward=True returns ascending
        chronological order directly from the sort key — no in-Python
        sort needed, since message_id's ULID-like prefix (see
        _ulid_like_message_id()) sorts lexicographically the same as
        chronologically."""
        kwargs = {
            "KeyConditionExpression": Key("conversation_id").eq(conversation_id),
            "ScanIndexForward": True,
            "Limit": limit,
        }
        if before_message_id:
            kwargs["KeyConditionExpression"] = kwargs["KeyConditionExpression"] & Key("message_id").lt(before_message_id)
        resp = _msg_table().query(**kwargs)
        return resp.get("Items", [])

    def mark_read(conversation_id: str, message_ids: list[str], *, reader_email: str) -> None:
        now_iso = _now_iso()
        reader = reader_email.lower().strip()
        table = _msg_table()
        for mid in message_ids:
            resp = table.get_item(Key={"conversation_id": conversation_id, "message_id": mid})
            item = resp.get("Item")
            if not item:
                continue
            read_by = dict(item.get("read_by") or {})
            read_by[reader] = now_iso
            item["read_by"] = read_by
            table.put_item(Item=item)
