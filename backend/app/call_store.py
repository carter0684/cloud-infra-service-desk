"""
Chat/Call feature (job-portal-chat-voice spec) — call session state
machine (ringing/active/ended, participant roster) and >=30-day call
history retention.

ENV=local: SQLite via SQLAlchemy (CallSession model in database.py).
ENV!=local: DynamoDB — job-portal-call-sessions (see data_stack.py's
comment: this single table serves BOTH live signaling state while a
call is ringing/active AND read-only history until its 30-day TTL).

This module owns the participant roster state machine
("ringing"|"joined"|"declined"|"timed_out"|"left") and delegates all
actual Chime SDK Meetings API calls to chime_client.py — this module
never calls boto3 directly, matching the design's "Chime SDK is scoped
to media only" intent.

Cost-control invariants enforced here (Requirement 10):
  - create_call() creates the Chime meeting but adds NO attendees — a
    ringing callee is never a joined Chime attendee, so nobody is ever
    billed for a call nobody answered (Requirement 10.3). Attendees are
    only added by accept_call()/invite_to_active_call() at the moment a
    specific Team_Member explicitly accepts.
  - Every path that can make the "joined" roster empty (decline-all,
    timeout-all, last-participant-leave, caller-cancel-while-ringing)
    calls chime_client.delete_meeting() and sets status="ended" — this
    is the ONLY thing that stops attendee-minute billing, so it must
    never be skipped on any terminal transition.

Group-call independence invariant (Requirement 6.3): one callee's
decline/timeout only ever mutates that callee's own participants[email]
entry — it never touches any other participant's entry and never ends
the call unless the roster of "still active" participants (ringing or
joined) has now reached zero.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import chime_client

ENV = os.environ.get("ENV", "local")

RING_TIMEOUT_SECONDS = 45
CALL_HISTORY_RETENTION_DAYS = 30
# Orphaned-meeting backstop sweep threshold (Task 4.7) — a call session
# with zero heartbeat/API activity for longer than this is force-ended
# regardless of its nominal status, as a defence against a crashed task
# leaving a Chime meeting billing indefinitely.
ORPHAN_MEETING_STALE_SECONDS = 120

_ACTIVE_STATES = {"ringing", "joined"}


class CallStoreError(ValueError):
    """Raised for user-facing validation/authorisation failures — never
    leaks internal detail, matching ChatStoreError's convention in
    chat_store.py."""


class CallNotFoundError(CallStoreError):
    """Raised when a call_id does not exist."""


class CallNotActiveError(CallStoreError):
    """Raised when an action (accept/decline/leave/invite) is attempted
    against a call whose status is already "ended"."""


def _new_id() -> str:
    return str(uuid.uuid4())


def _norm(email: str) -> str:
    return email.lower().strip()


def _active_participant_count(participants: dict) -> int:
    return sum(1 for p in participants.values() if p.get("state") in _ACTIVE_STATES)


def _ttl_epoch(ended_at: datetime) -> int:
    return int((ended_at + timedelta(days=CALL_HISTORY_RETENTION_DAYS)).timestamp())


if ENV == "local":
    from .database import SessionLocal, CallSession as _CallModel

    def _call_to_dict(c) -> dict:
        return {
            "call_id": c.call_id,
            "conversation_id": c.conversation_id,
            "type": c.type,
            "caller_email": c.caller_email,
            "chime_meeting_id": c.chime_meeting_id,
            "chime_media_region": c.chime_media_region,
            "chime_media_placement": c.chime_media_placement or {},
            "caller_attendee_id": c.caller_attendee_id,
            "caller_join_token": c.caller_join_token,
            "status": c.status,
            "started_at": c.started_at.isoformat() if c.started_at else None,
            "ended_at": c.ended_at.isoformat() if c.ended_at else None,
            "participants": c.participants or {},
        }

    def _get_call_row(db, call_id: str):
        row = db.query(_CallModel).filter(_CallModel.call_id == call_id).first()
        if not row:
            raise CallNotFoundError("Call not found.")
        return row

    def create_call(
        *,
        conversation_id: str,
        caller_email: str,
        callee_emails: list[str],
        call_type: str,
    ) -> dict:
        """
        Requirement 5.1/6.1: create the Chime meeting and place every
        callee in Ringing_State. The caller themselves is NOT placed in
        the participants roster in a "ringing" state — a call has no
        notion of the caller "answering their own call"; the caller is
        implicitly connected once the meeting exists (the frontend joins
        the caller's own client to the meeting immediately on
        create_call() success, matching Requirement 5.1's "delivers a
        ring notification... within 3 seconds" without waiting on any
        Accept from the caller's own side).

        Task 15.3 fix: that last sentence describes an intended
        behaviour that was never actually wired up — the caller was
        never added as a Chime attendee anywhere in this module, so
        there was no attendee_id/join_token this "implicitly connected"
        caller could have used to construct a MeetingSession. Fixed by
        calling chime_client.add_attendee() for the caller right here,
        storing the result as caller_attendee_id/caller_join_token
        (deliberately NOT a `participants[caller]` roster entry — see
        those columns' own comment in database.py for why)."""
        if call_type not in ("direct", "group"):
            raise CallStoreError("call_type must be 'direct' or 'group'.")
        caller = _norm(caller_email)
        callees = [_norm(e) for e in callee_emails]
        if not callees:
            raise CallStoreError("A call must have at least one callee.")

        call_id = _new_id()
        meeting = chime_client.create_meeting_for_call(call_id)
        caller_attendee = chime_client.add_attendee(meeting["chime_meeting_id"], caller)

        now = datetime.now(timezone.utc)
        participants = {
            e: {"state": "ringing", "joined_at": None} for e in callees
        }

        db = SessionLocal()
        try:
            c = _CallModel(
                call_id=call_id,
                conversation_id=conversation_id,
                type=call_type,
                caller_email=caller,
                chime_meeting_id=meeting["chime_meeting_id"],
                chime_media_region=meeting["media_region"],
                chime_media_placement=meeting.get("media_placement") or {},
                caller_attendee_id=caller_attendee["attendee_id"],
                caller_join_token=caller_attendee["join_token"],
                status="ringing",
                started_at=now.replace(tzinfo=None),
                ended_at=None,
                participants=participants,
            )
            db.add(c)
            db.commit()
            db.refresh(c)
            return _call_to_dict(c)
        finally:
            db.close()

    def get_call(call_id: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            row = db.query(_CallModel).filter(_CallModel.call_id == call_id).first()
            return _call_to_dict(row) if row else None
        finally:
            db.close()

    def _end_call_and_release_meeting(db, row) -> None:
        """Requirement 10.2: the ONLY function that ever transitions a
        call to status="ended" — always paired with delete_meeting() so
        Chime billing stops. Idempotent: calling this on an
        already-ended row is a no-op (chime_client.delete_meeting() is
        itself idempotent, but we also short-circuit here to avoid a
        redundant API call)."""
        if row.status == "ended":
            return
        if row.chime_meeting_id:
            chime_client.delete_meeting(row.chime_meeting_id)
        row.status = "ended"
        row.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

    def accept_call(call_id: str, *, callee_email: str) -> dict:
        """Requirement 5.4/6.2: join this specific callee's client to
        the Chime meeting (add_attendee) and mark their roster entry
        "joined". Never auto-joins any other callee (Requirement
        6.2)."""
        callee = _norm(callee_email)
        db = SessionLocal()
        try:
            row = _get_call_row(db, call_id)
            if row.status == "ended":
                raise CallNotActiveError("This call has already ended.")
            participants = dict(row.participants or {})
            entry = participants.get(callee)
            if not entry or entry.get("state") != "ringing":
                raise CallStoreError("You are not currently being rung for this call.")

            attendee = chime_client.add_attendee(row.chime_meeting_id, callee)
            entry["state"] = "joined"
            entry["joined_at"] = datetime.now(timezone.utc).isoformat()
            entry["attendee_id"] = attendee["attendee_id"]
            participants[callee] = entry
            row.participants = participants
            if row.status == "ringing":
                row.status = "active"
            db.commit()
            db.refresh(row)
            result = _call_to_dict(row)
            # Task 15.3 fix: media_placement was previously hardcoded to
            # None here — the row's own chime_media_placement (persisted
            # by create_call() from the SAME Chime CreateMeeting response
            # this meeting was created with) is the real value the
            # frontend's amazon-chime-sdk-js MeetingSession needs; no
            # second Chime API round-trip required, and this is
            # identical for every attendee of the same meeting.
            result["_join_info"] = {
                "chime_meeting_id": row.chime_meeting_id,
                "media_region": row.chime_media_region,
                "media_placement": row.chime_media_placement or {},
                "attendee_id": attendee["attendee_id"],
                "join_token": attendee["join_token"],
            }
            return result
        finally:
            db.close()

    def decline_call(call_id: str, *, callee_email: str) -> dict:
        """Requirement 5.5/6.3: mark this callee's entry "declined". For
        a Direct_Call there is only one callee, so this always ends the
        call. For a Group_Call, the call continues for any other
        ringing/joined participants (Requirement 6.3) — only ends if
        this decline drops the active-participant count to zero."""
        callee = _norm(callee_email)
        db = SessionLocal()
        try:
            row = _get_call_row(db, call_id)
            if row.status == "ended":
                raise CallNotActiveError("This call has already ended.")
            participants = dict(row.participants or {})
            entry = participants.get(callee)
            if not entry:
                raise CallStoreError("You are not a participant of this call.")
            entry["state"] = "declined"
            participants[callee] = entry
            row.participants = participants
            db.commit()
            if _active_participant_count(participants) == 0:
                _end_call_and_release_meeting(db, row)
            db.refresh(row)
            return _call_to_dict(row)
        finally:
            db.close()

    def cancel_call(call_id: str, *, caller_email: str) -> dict:
        """Requirement 5.7: caller cancels while ringing — ends the call
        for everyone regardless of individual callee states."""
        caller = _norm(caller_email)
        db = SessionLocal()
        try:
            row = _get_call_row(db, call_id)
            if row.caller_email != caller:
                raise CallStoreError("Only the caller may cancel this call.")
            if row.status == "ended":
                raise CallNotActiveError("This call has already ended.")
            _end_call_and_release_meeting(db, row)
            db.refresh(row)
            return _call_to_dict(row)
        finally:
            db.close()

    def leave_call(call_id: str, *, participant_email: str) -> dict:
        """Requirement 5.9/6.5/6.6: mark this participant's entry
        "left". If this drops the active-participant count to zero
        (last participant leaving), release the Chime meeting and end
        the call (Requirement 6.6) — otherwise the call continues for
        whoever remains (Requirement 6.5).

        Task 15.3 fix: the caller is deliberately never given a
        `participants` roster entry (see database.py's
        caller_attendee_id/caller_join_token columns for why) — but that
        meant this function had no path for the caller to leave/hang up
        their OWN call at all (Requirement 5.9's "either participant" is
        explicit that the caller must be able to end an active Direct_
        Call, not just a callee). Handled as a distinct branch: if `who`
        IS the caller, this call always ends immediately and releases
        the meeting, matching a Direct_Call's "either participant ends
        it -> disconnect both, release the meeting" semantics exactly
        (a Direct_Call only ever has the caller + one callee, so the
        caller leaving is definitionally "everyone able to leave has
        left"). For a Group_Call, the caller hanging up while other
        callees are still joined is intentionally treated the same way
        cancel_call() treats a caller cancelling while ringing — ending
        the call outright — rather than silently leaving the caller's
        own Chime attendee connected with no participants-roster
        tracking of that fact; Requirement 6.6 only guarantees the
        Group_Call survives OTHER participants leaving, not the
        original caller specifically hanging up."""
        who = _norm(participant_email)
        db = SessionLocal()
        try:
            row = _get_call_row(db, call_id)
            if row.status == "ended":
                raise CallNotActiveError("This call has already ended.")
            if who == row.caller_email:
                _end_call_and_release_meeting(db, row)
                db.refresh(row)
                return _call_to_dict(row)
            participants = dict(row.participants or {})
            entry = participants.get(who)
            if not entry:
                raise CallStoreError("You are not a participant of this call.")
            entry["state"] = "left"
            participants[who] = entry
            row.participants = participants
            db.commit()
            if _active_participant_count(participants) == 0:
                _end_call_and_release_meeting(db, row)
            db.refresh(row)
            return _call_to_dict(row)
        finally:
            db.close()

    def timeout_ring(call_id: str, *, callee_email: Optional[str] = None) -> dict:
        """
        Requirement 5.6/6.3: 45-second ring timeout. If callee_email is
        given, only that callee's entry is marked "timed_out" (a
        Group_Call's independent-per-callee timeout — Requirement 6.3).
        If callee_email is None, EVERY still-"ringing" entry is marked
        "timed_out" in one pass (used by the periodic sweep, which finds
        stale calls by call_id rather than by individual callee).
        Either way, the call only ends once the active-participant count
        reaches zero.
        """
        db = SessionLocal()
        try:
            row = _get_call_row(db, call_id)
            if row.status == "ended":
                return _call_to_dict(row)  # already ended — idempotent no-op
            participants = dict(row.participants or {})
            targets = [callee_email] if callee_email else [e for e, p in participants.items() if p.get("state") == "ringing"]
            for e in targets:
                e = _norm(e)
                entry = participants.get(e)
                if entry and entry.get("state") == "ringing":
                    entry["state"] = "timed_out"
                    participants[e] = entry
            row.participants = participants
            db.commit()
            if _active_participant_count(participants) == 0:
                _end_call_and_release_meeting(db, row)
            db.refresh(row)
            return _call_to_dict(row)
        finally:
            db.close()

    def invite_to_active_call(call_id: str, *, new_callee_email: str) -> dict:
        """Requirement 6.7: invite an additional Group_Conversation
        member to an already-active Group_Call, using the same
        Ringing_State/Accept/Decline behaviour as an initial
        invitation."""
        new_callee = _norm(new_callee_email)
        db = SessionLocal()
        try:
            row = _get_call_row(db, call_id)
            if row.status == "ended":
                raise CallNotActiveError("This call has already ended.")
            if row.type != "group":
                raise CallStoreError("Only Group_Calls support inviting additional participants.")
            participants = dict(row.participants or {})
            if participants.get(new_callee, {}).get("state") in _ACTIVE_STATES:
                raise CallStoreError("This person is already ringing or joined on this call.")
            participants[new_callee] = {"state": "ringing", "joined_at": None}
            row.participants = participants
            db.commit()
            db.refresh(row)
            return _call_to_dict(row)
        finally:
            db.close()

    def list_call_history(conversation_id: str, *, requester_email: str) -> list[dict]:
        """Requirement 11.3/11.4: chronological (most-recent-first) list
        of retained call records for this Conversation, scoped so the
        caller must have been either the caller or an invited
        participant on each returned call — never exposed to a
        Team_Member who was never part of a given call, even if they are
        currently a participant of the parent Conversation (a call that
        happened before they joined a group is not "theirs" to see; the
        route handler additionally enforces general Conversation
        participancy before calling this at all, per Requirement 9.3)."""
        requester = _norm(requester_email)
        db = SessionLocal()
        try:
            rows = (
                db.query(_CallModel)
                .filter(_CallModel.conversation_id == conversation_id)
                .order_by(_CallModel.started_at.desc())
                .all()
            )
            result = []
            for r in rows:
                participants = r.participants or {}
                if r.caller_email == requester or requester in participants:
                    result.append(_call_to_dict(r))
            return result
        finally:
            db.close()

    def list_stale_ringing_calls(*, older_than_seconds: int = RING_TIMEOUT_SECONDS) -> list[dict]:
        """Safety-net sweep target list (Task 4.5's periodic sweep):
        every call with at least one still-"ringing" participant entry
        whose call started_at is older than older_than_seconds — a
        safety net for the case where the in-process asyncio timer that
        started alongside create_call() (or invite_to_active_call())
        died with its owning task.

        Filters on status in (ringing, active) rather than status ==
        "ringing" alone: a Group_Call's top-level status flips to
        "active" the moment ANY one callee accepts, but other callees
        can still be independently "ringing" on that same call
        (Requirement 6.2/6.3's per-callee independence) — those callees
        must still be found and timed out by this sweep even though the
        call as a whole is no longer in Ringing_State."""
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=older_than_seconds)
        db = SessionLocal()
        try:
            rows = (
                db.query(_CallModel)
                .filter(_CallModel.status.in_(["ringing", "active"]), _CallModel.started_at < cutoff)
                .all()
            )
            stale = [r for r in rows if any(p.get("state") == "ringing" for p in (r.participants or {}).values())]
            return [_call_to_dict(r) for r in stale]
        finally:
            db.close()

    def list_orphaned_active_calls(*, older_than_seconds: int = ORPHAN_MEETING_STALE_SECONDS) -> list[dict]:
        """Task 4.7's backstop sweep target list: every call still in
        status="ringing"/"active" whose started_at is older than
        older_than_seconds with no other freshness signal available in
        local/SQLite mode (production mode instead checks each call's
        Chime meeting directly via chime_client.get_meeting() — see the
        DynamoDB branch). This local-mode approximation is sufficient
        for local dev; it is not the production cost-control path."""
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=older_than_seconds)
        db = SessionLocal()
        try:
            rows = (
                db.query(_CallModel)
                .filter(_CallModel.status.in_(["ringing", "active"]), _CallModel.started_at < cutoff)
                .all()
            )
            return [_call_to_dict(r) for r in rows]
        finally:
            db.close()

    def is_user_in_any_active_call(email: str) -> bool:
        """
        Requirement 8.3 (Presence "in call"): True iff this Team_Member
        currently has a "joined" participant entry on any call whose
        status is not yet "ended". Used by ws_connections.py's
        get_presence_state() via a caller-supplied flag (see that
        function's docstring on why this module does not import
        ws_connections.py directly).

        A low-frequency Presence-display query, not a hot path — scans
        job-portal-call-sessions filtered to status != "ended", same
        scale tradeoff already accepted for the sweep target lists
        above (a handful of concurrent calls at this Portal's scale).
        """
        who = _norm(email)
        db = SessionLocal()
        try:
            rows = db.query(_CallModel).filter(_CallModel.status != "ended").all()
            for r in rows:
                entry = (r.participants or {}).get(who)
                if entry and entry.get("state") == "joined":
                    return True
            return False
        finally:
            db.close()

    def list_active_call_ids_joined_by_user(email: str) -> list[str]:
        """Task 11.1: every call_id this Team_Member is currently
        "joined" to (status != "ended") — the enumeration
        leave_all_active_calls_for_user() below needs before it can
        call leave_call() (which requires a specific call_id) once per
        call. Same scan tradeoff as is_user_in_any_active_call()
        immediately above; kept as a separate function (rather than
        having that one return the list and wrapping it with bool())
        so its name/intent stays obvious at each call site."""
        who = _norm(email)
        db = SessionLocal()
        try:
            rows = db.query(_CallModel).filter(_CallModel.status != "ended").all()
            return [
                r.call_id
                for r in rows
                if (r.participants or {}).get(who, {}).get("state") == "joined"
            ]
        finally:
            db.close()

    def leave_all_active_calls_for_user(email: str) -> list[dict]:
        """Requirement 9.6 (Task 11.1): session-expiry/forced-logout
        cleanup — calls leave_call() for every active call this
        Team_Member is currently joined to, reusing that function's own
        existing "last participant leaving ends the call and releases
        the Chime meeting" behaviour rather than duplicating it here.
        Returns the list of updated call dicts (one per call left) for
        logging/testing; a call that ends up with zero participants as
        a result is fully released, same as if the user had called
        POST /api/calls/{id}/leave normally themselves."""
        results = []
        for call_id in list_active_call_ids_joined_by_user(email):
            try:
                results.append(leave_call(call_id, participant_email=email))
            except (CallStoreError, CallNotActiveError):
                # Another concurrent request (e.g. the user's own
                # explicit "Leave call" click, or the same cleanup
                # racing across two nearly-simultaneous 401s) may have
                # already left/ended this specific call between the
                # enumeration above and this call — not an error worth
                # surfacing, since the end state (not a participant of
                # this call anymore) is what we wanted anyway.
                continue
        return results

else:
    import boto3
    from boto3.dynamodb.conditions import Key

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    CALL_TABLE_NAME = os.environ.get("CALL_SESSIONS_TABLE_NAME", "job-portal-call-sessions")

    def _table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(CALL_TABLE_NAME)

    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _get_call_row(call_id: str) -> dict:
        resp = _table().get_item(Key={"call_id": call_id})
        item = resp.get("Item")
        if not item:
            raise CallNotFoundError("Call not found.")
        return item

    def create_call(
        *,
        conversation_id: str,
        caller_email: str,
        callee_emails: list[str],
        call_type: str,
    ) -> dict:
        if call_type not in ("direct", "group"):
            raise CallStoreError("call_type must be 'direct' or 'group'.")
        caller = _norm(caller_email)
        callees = [_norm(e) for e in callee_emails]
        if not callees:
            raise CallStoreError("A call must have at least one callee.")

        call_id = _new_id()
        meeting = chime_client.create_meeting_for_call(call_id)
        # Task 15.3 fix — see the local (SQLite) branch's identical
        # create_call() comment: the caller was never actually added as
        # a Chime attendee anywhere, on either branch, despite the
        # design's stated intent that the caller's own client joins the
        # meeting immediately. Stored as top-level caller_attendee_id/
        # caller_join_token, not a participants[caller] roster entry —
        # this module's own call-history listing functions below
        # already special-case caller_email separately from the
        # participants dict for exactly this reason.
        caller_attendee = chime_client.add_attendee(meeting["chime_meeting_id"], caller)

        now = _now_iso()
        participants = {e: {"state": "ringing", "joined_at": None} for e in callees}
        item = {
            "call_id": call_id,
            "conversation_id": conversation_id,
            "type": call_type,
            "caller_email": caller,
            "chime_meeting_id": meeting["chime_meeting_id"],
            "chime_media_region": meeting["media_region"],
            # Task 15.3 fix — see the local (SQLite) branch's identical
            # comment on chime_media_placement above: this is required
            # for amazon-chime-sdk-js's MeetingSession construction and
            # was previously discarded entirely on this DynamoDB branch
            # too (never written to the item, and accept_call() below
            # never included it in `_join_info` at all).
            "chime_media_placement": meeting.get("media_placement") or {},
            "caller_attendee_id": caller_attendee["attendee_id"],
            "caller_join_token": caller_attendee["join_token"],
            "status": "ringing",
            "started_at": now,
            "ended_at": None,
            "participants": participants,
        }
        _table().put_item(Item=item)
        return item

    def get_call(call_id: str) -> Optional[dict]:
        resp = _table().get_item(Key={"call_id": call_id})
        return resp.get("Item")

    def _end_call_and_release_meeting(item: dict) -> dict:
        if item.get("status") == "ended":
            return item
        if item.get("chime_meeting_id"):
            chime_client.delete_meeting(item["chime_meeting_id"])
        ended_at = datetime.now(timezone.utc)
        item["status"] = "ended"
        item["ended_at"] = ended_at.isoformat()
        item["ttl"] = _ttl_epoch(ended_at)
        _table().put_item(Item=item)
        return item

    def accept_call(call_id: str, *, callee_email: str) -> dict:
        callee = _norm(callee_email)
        item = _get_call_row(call_id)
        if item.get("status") == "ended":
            raise CallNotActiveError("This call has already ended.")
        participants = dict(item.get("participants") or {})
        entry = participants.get(callee)
        if not entry or entry.get("state") != "ringing":
            raise CallStoreError("You are not currently being rung for this call.")

        attendee = chime_client.add_attendee(item["chime_meeting_id"], callee)
        entry["state"] = "joined"
        entry["joined_at"] = _now_iso()
        entry["attendee_id"] = attendee["attendee_id"]
        participants[callee] = entry
        item["participants"] = participants
        if item.get("status") == "ringing":
            item["status"] = "active"
        _table().put_item(Item=item)
        item["_join_info"] = {
            "chime_meeting_id": item["chime_meeting_id"],
            "media_region": item.get("chime_media_region"),
            "media_placement": item.get("chime_media_placement") or {},
            "attendee_id": attendee["attendee_id"],
            "join_token": attendee["join_token"],
        }
        return item

    def decline_call(call_id: str, *, callee_email: str) -> dict:
        callee = _norm(callee_email)
        item = _get_call_row(call_id)
        if item.get("status") == "ended":
            raise CallNotActiveError("This call has already ended.")
        participants = dict(item.get("participants") or {})
        entry = participants.get(callee)
        if not entry:
            raise CallStoreError("You are not a participant of this call.")
        entry["state"] = "declined"
        participants[callee] = entry
        item["participants"] = participants
        if _active_participant_count(participants) == 0:
            return _end_call_and_release_meeting(item)
        _table().put_item(Item=item)
        return item

    def cancel_call(call_id: str, *, caller_email: str) -> dict:
        caller = _norm(caller_email)
        item = _get_call_row(call_id)
        if item.get("caller_email") != caller:
            raise CallStoreError("Only the caller may cancel this call.")
        if item.get("status") == "ended":
            raise CallNotActiveError("This call has already ended.")
        return _end_call_and_release_meeting(item)

    def leave_call(call_id: str, *, participant_email: str) -> dict:
        """Task 15.3 fix — see the local (SQLite) branch's identical
        leave_call() comment: the caller has no `participants` roster
        entry (by design), so without this branch the caller could
        never leave/hang up their own call via this route at all."""
        who = _norm(participant_email)
        item = _get_call_row(call_id)
        if item.get("status") == "ended":
            raise CallNotActiveError("This call has already ended.")
        if who == item.get("caller_email"):
            return _end_call_and_release_meeting(item)
        participants = dict(item.get("participants") or {})
        entry = participants.get(who)
        if not entry:
            raise CallStoreError("You are not a participant of this call.")
        entry["state"] = "left"
        participants[who] = entry
        item["participants"] = participants
        if _active_participant_count(participants) == 0:
            return _end_call_and_release_meeting(item)
        _table().put_item(Item=item)
        return item

    def timeout_ring(call_id: str, *, callee_email: Optional[str] = None) -> dict:
        item = _get_call_row(call_id)
        if item.get("status") == "ended":
            return item  # already ended — idempotent no-op
        participants = dict(item.get("participants") or {})
        targets = [callee_email] if callee_email else [e for e, p in participants.items() if p.get("state") == "ringing"]
        for e in targets:
            e = _norm(e)
            entry = participants.get(e)
            if entry and entry.get("state") == "ringing":
                entry["state"] = "timed_out"
                participants[e] = entry
        item["participants"] = participants
        if _active_participant_count(participants) == 0:
            return _end_call_and_release_meeting(item)
        _table().put_item(Item=item)
        return item

    def invite_to_active_call(call_id: str, *, new_callee_email: str) -> dict:
        new_callee = _norm(new_callee_email)
        item = _get_call_row(call_id)
        if item.get("status") == "ended":
            raise CallNotActiveError("This call has already ended.")
        if item.get("type") != "group":
            raise CallStoreError("Only Group_Calls support inviting additional participants.")
        participants = dict(item.get("participants") or {})
        if participants.get(new_callee, {}).get("state") in _ACTIVE_STATES:
            raise CallStoreError("This person is already ringing or joined on this call.")
        participants[new_callee] = {"state": "ringing", "joined_at": None}
        item["participants"] = participants
        _table().put_item(Item=item)
        return item

    def list_call_history(conversation_id: str, *, requester_email: str) -> list[dict]:
        """Requirement 11.3/11.4: real Query on ConversationCallHistoryIndex
        (PK conversation_id, SK started_at) instead of a scan — chronological
        order comes directly from the GSI's sort key. ScanIndexForward=False
        returns most-recent-first."""
        requester = _norm(requester_email)
        resp = _table().query(
            IndexName="ConversationCallHistoryIndex",
            KeyConditionExpression=Key("conversation_id").eq(conversation_id),
            ScanIndexForward=False,
        )
        result = []
        for item in resp.get("Items", []):
            participants = item.get("participants") or {}
            if item.get("caller_email") == requester or requester in participants:
                result.append(item)
        return result

    def list_stale_ringing_calls(*, older_than_seconds: int = RING_TIMEOUT_SECONDS) -> list[dict]:
        """Safety-net sweep target list — no GSI exists for "status +
        started_at" (a low-frequency admin/sweep query does not justify a
        5th table index), so this scans job-portal-call-sessions filtered
        to status in (ringing, active). Call volume at this Portal's
        scale (a handful of concurrent calls at most) makes this an
        acceptable tradeoff, matching the same reasoning already used for
        other low-volume scan paths in this codebase (chat_store.py's
        find_direct_conversation()).

        Filters on status in (ringing, active) rather than status ==
        "ringing" alone, then further filters in Python to calls with at
        least one still-"ringing" participant entry — see the local-mode
        branch's docstring above for why a Group_Call's top-level status
        alone is not sufficient (one callee accepting flips it to
        "active" while other callees can still be independently
        ringing, per Requirement 6.2/6.3)."""
        from boto3.dynamodb.conditions import Attr
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        resp = _table().scan(
            FilterExpression=Attr("status").is_in(["ringing", "active"]) & Attr("started_at").lt(cutoff_iso)
        )
        items = resp.get("Items", [])
        return [
            item for item in items
            if any(p.get("state") == "ringing" for p in (item.get("participants") or {}).values())
        ]

    def list_orphaned_active_calls(*, older_than_seconds: int = ORPHAN_MEETING_STALE_SECONDS) -> list[dict]:
        """Backstop sweep target list — same scan tradeoff as
        list_stale_ringing_calls() above, filtered to status in
        (ringing, active)."""
        from boto3.dynamodb.conditions import Attr
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        resp = _table().scan(
            FilterExpression=Attr("status").is_in(["ringing", "active"]) & Attr("started_at").lt(cutoff_iso)
        )
        return resp.get("Items", [])

    def is_user_in_any_active_call(email: str) -> bool:
        """Requirement 8.3 (Presence "in call") — see the local-mode
        branch's docstring above for the full rationale. Scans
        job-portal-call-sessions filtered to status != "ended", same
        low-frequency-query scale tradeoff as the sweep target lists
        above."""
        from boto3.dynamodb.conditions import Attr
        who = _norm(email)
        resp = _table().scan(FilterExpression=Attr("status").ne("ended"))
        for item in resp.get("Items", []):
            entry = (item.get("participants") or {}).get(who)
            if entry and entry.get("state") == "joined":
                return True
        return False

    def list_active_call_ids_joined_by_user(email: str) -> list[str]:
        """See the local-mode branch's docstring above — identical
        enumeration contract."""
        from boto3.dynamodb.conditions import Attr
        who = _norm(email)
        resp = _table().scan(FilterExpression=Attr("status").ne("ended"))
        return [
            item["call_id"]
            for item in resp.get("Items", [])
            if (item.get("participants") or {}).get(who, {}).get("state") == "joined"
        ]

    def leave_all_active_calls_for_user(email: str) -> list[dict]:
        """See the local-mode branch's docstring above — identical
        cleanup contract, reusing this branch's own leave_call()."""
        results = []
        for call_id in list_active_call_ids_joined_by_user(email):
            try:
                results.append(leave_call(call_id, participant_email=email))
            except (CallStoreError, CallNotActiveError):
                continue
        return results
