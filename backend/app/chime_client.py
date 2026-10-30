"""
Chat feature (job-portal-chat-voice spec) — thin wrapper around
`boto3.client("chime-sdk-meetings")`.

Design intent (see design.md's "Voice/video media, screen share" row):
Chime SDK is scoped to MEDIA ONLY. This module never touches this
Portal's own DynamoDB tables — call_store.py owns the call-session state
machine and calls into this module purely to create/join/tear down the
underlying Chime meeting resource. Chime SDK's own messaging/data-message
features are deliberately not used, so this module has no notion of
"conversation" or "participant state" beyond the raw meeting/attendee
IDs it hands back.

Every function raises a typed ChimeClientError on failure and never
leaks the raw boto3/botocore exception text to the caller (matching the
EscalationError convention in escalation.py) — callers (call_store.py,
and ultimately main.py's route handlers) only ever see a safe, generic
message plus a `code` they can branch on if needed.

IAM: the ECS task role's `ChimeSdkMeetings` policy statement
(compute_stack.py) grants CreateMeeting/CreateAttendee/
CreateMeetingWithAttendees/DeleteMeeting/GetMeeting/GetAttendee with
resources=["*"] — the Chime SDK Meetings API does not support
resource-level ARN scoping.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Chime SDK Meetings media region — this is a SEPARATE concept from the
# AWS_REGION the boto3 client itself is created in (the Meetings API is
# only available in a handful of regions and a meeting's media plane can
# be pinned to a different region than the control-plane API call).
# us-east-1 is always a valid Chime SDK media region and is used as the
# default here; call_store.py may pass a different value in the future
# if regional-latency selection is ever added.
DEFAULT_MEDIA_REGION = os.environ.get("CHIME_MEDIA_REGION", "us-east-1")


class ChimeClientError(Exception):
    """Typed error for Chime SDK Meetings API failures. `code` is a
    short machine-readable tag (e.g. "create_meeting_failed") the caller
    can branch on; `message` is always safe to show to the end user —
    never contains raw botocore exception text or AWS request IDs."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _client():
    return boto3.client("chime-sdk-meetings", region_name=AWS_REGION)


def create_meeting_for_call(call_id: str, *, media_region: Optional[str] = None) -> dict:
    """
    Create a Chime SDK meeting for a Call (Direct_Call or Group_Call).

    `call_id` is used as the ClientRequestToken (idempotency — a retried
    create_call() for the same call_id will not create a second Chime
    meeting) and as ExternalMeetingId (so the meeting resource can be
    correlated back to this Portal's own call_id when inspecting it via
    the AWS console/CLI).

    Returns {"chime_meeting_id": str, "media_region": str,
    "media_placement": dict} on success.
    Raises ChimeClientError("create_meeting_failed", ...) on failure.
    """
    region = media_region or DEFAULT_MEDIA_REGION
    try:
        resp = _client().create_meeting(
            ClientRequestToken=call_id,
            MediaRegion=region,
            ExternalMeetingId=call_id,
        )
    except (ClientError, BotoCoreError) as e:
        logger.error("chime_client: create_meeting failed for call %s: %s", call_id, e)
        raise ChimeClientError("create_meeting_failed", "Could not start the call — please try again.") from e

    meeting = resp.get("Meeting") or {}
    meeting_id = meeting.get("MeetingId")
    if not meeting_id:
        logger.error("chime_client: create_meeting returned no MeetingId for call %s: %s", call_id, resp)
        raise ChimeClientError("create_meeting_failed", "Could not start the call — please try again.")

    return {
        "chime_meeting_id": meeting_id,
        "media_region": meeting.get("MediaRegion", region),
        "media_placement": meeting.get("MediaPlacement") or {},
    }


def add_attendee(meeting_id: str, user_email: str) -> dict:
    """
    Add an attendee to an existing Chime meeting — called when a Callee
    accepts a ring (or when an already-active Group_Call is joined via
    invite_to_active_call()).

    `user_email` is used as ExternalUserId so the returned AttendeeId can
    be correlated back to this Portal's own participant identity when
    the frontend renders roster tiles.

    Returns {"attendee_id": str, "join_token": str} on success — the
    frontend's amazon-chime-sdk-js MeetingSession is constructed from
    this join_token plus the meeting info from create_meeting_for_call().
    Raises ChimeClientError("add_attendee_failed", ...) on failure.
    """
    try:
        resp = _client().create_attendee(
            MeetingId=meeting_id,
            ExternalUserId=user_email,
        )
    except (ClientError, BotoCoreError) as e:
        logger.error("chime_client: create_attendee failed for meeting %s (%s): %s", meeting_id, user_email, e)
        raise ChimeClientError("add_attendee_failed", "Could not join the call — please try again.") from e

    attendee = resp.get("Attendee") or {}
    attendee_id = attendee.get("AttendeeId")
    join_token = attendee.get("JoinToken")
    if not attendee_id or not join_token:
        logger.error(
            "chime_client: create_attendee returned an incomplete Attendee for meeting %s (%s): %s",
            meeting_id, user_email, resp,
        )
        raise ChimeClientError("add_attendee_failed", "Could not join the call — please try again.")

    return {"attendee_id": attendee_id, "join_token": join_token}


def delete_meeting(meeting_id: str) -> None:
    """
    End a Chime meeting — called when the last participant leaves an
    active call, when a ring times out with nobody accepting, or by the
    orphaned-meeting backstop sweep. This is the action that actually
    stops Chime SDK billing for the meeting (Requirement 10.2/10.3): a
    meeting with no attendees left in it does not automatically get
    deleted by AWS on its own on any short timeline this Portal can rely
    on, so call_store.py must always call this explicitly.

    Idempotent from the caller's point of view — deleting an
    already-deleted (or never-existed) meeting_id is treated as success,
    not an error, since the desired end state ("this meeting resource is
    gone") is already true. This matters for the backstop sweep, which
    may race with a normal leave_call()-triggered delete.
    """
    try:
        _client().delete_meeting(MeetingId=meeting_id)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "NotFoundException":
            logger.info("chime_client: delete_meeting for %s — meeting already gone, treating as success", meeting_id)
            return
        logger.error("chime_client: delete_meeting failed for %s: %s", meeting_id, e)
        raise ChimeClientError("delete_meeting_failed", "Could not end the call cleanly.") from e
    except BotoCoreError as e:
        logger.error("chime_client: delete_meeting failed for %s: %s", meeting_id, e)
        raise ChimeClientError("delete_meeting_failed", "Could not end the call cleanly.") from e


def get_meeting(meeting_id: str) -> Optional[dict]:
    """
    Fetch a Chime meeting's current state — used by the orphaned-meeting
    backstop sweep to confirm a meeting still exists (and by extension is
    still incurring cost) before force-deleting it, and available for any
    future diagnostics/health-check use.

    Returns the same shape as create_meeting_for_call()'s success value,
    or None if the meeting does not exist (already deleted/expired) —
    this is a normal, expected outcome, not an error.
    Raises ChimeClientError("get_meeting_failed", ...) for any other
    failure (e.g. a transient API error, throttling).
    """
    try:
        resp = _client().get_meeting(MeetingId=meeting_id)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "NotFoundException":
            return None
        logger.error("chime_client: get_meeting failed for %s: %s", meeting_id, e)
        raise ChimeClientError("get_meeting_failed", "Could not check the call's status.") from e
    except BotoCoreError as e:
        logger.error("chime_client: get_meeting failed for %s: %s", meeting_id, e)
        raise ChimeClientError("get_meeting_failed", "Could not check the call's status.") from e

    meeting = resp.get("Meeting") or {}
    meeting_id_out = meeting.get("MeetingId")
    if not meeting_id_out:
        return None

    return {
        "chime_meeting_id": meeting_id_out,
        "media_region": meeting.get("MediaRegion", DEFAULT_MEDIA_REGION),
        "media_placement": meeting.get("MediaPlacement") or {},
    }
