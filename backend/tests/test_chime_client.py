"""
Unit tests for app/chime_client.py — the boto3 chime-sdk-meetings
wrapper for job-portal-chat-voice.

No moto support exists for chime-sdk-meetings, so every boto3 client
call is mocked directly via unittest.mock (matching the
monkeypatch.setattr(module.boto3, "client", ...) convention already
used in test_escalation.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from app import chime_client


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": f"simulated {code}"}},
        operation_name=operation,
    )


class TestCreateMeetingForCall:
    def test_create_meeting_success_returns_meeting_id_and_region(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.create_meeting.return_value = {
            "Meeting": {
                "MeetingId": "meeting-123",
                "MediaRegion": "us-east-1",
                "MediaPlacement": {"AudioHostUrl": "wss://example"},
            }
        }
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        result = chime_client.create_meeting_for_call("call-abc")

        assert result["chime_meeting_id"] == "meeting-123"
        assert result["media_region"] == "us-east-1"
        assert result["media_placement"] == {"AudioHostUrl": "wss://example"}
        mock_client.create_meeting.assert_called_once_with(
            ClientRequestToken="call-abc",
            MediaRegion="us-east-1",
            ExternalMeetingId="call-abc",
        )

    def test_create_meeting_uses_explicit_media_region_when_given(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.create_meeting.return_value = {
            "Meeting": {"MeetingId": "meeting-123", "MediaRegion": "eu-west-1", "MediaPlacement": {}}
        }
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        chime_client.create_meeting_for_call("call-abc", media_region="eu-west-1")

        _, kwargs = mock_client.create_meeting.call_args
        assert kwargs["MediaRegion"] == "eu-west-1"

    def test_create_meeting_client_error_raises_chime_client_error(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.create_meeting.side_effect = _client_error("ThrottlingException", "CreateMeeting")
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        with pytest.raises(chime_client.ChimeClientError) as exc_info:
            chime_client.create_meeting_for_call("call-abc")

        assert exc_info.value.code == "create_meeting_failed"
        assert "ThrottlingException" not in exc_info.value.message
        assert "simulated" not in exc_info.value.message

    def test_create_meeting_botocore_error_raises_chime_client_error(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.create_meeting.side_effect = BotoCoreError()
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        with pytest.raises(chime_client.ChimeClientError) as exc_info:
            chime_client.create_meeting_for_call("call-abc")

        assert exc_info.value.code == "create_meeting_failed"

    def test_create_meeting_missing_meeting_id_raises_chime_client_error(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.create_meeting.return_value = {"Meeting": {}}
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        with pytest.raises(chime_client.ChimeClientError) as exc_info:
            chime_client.create_meeting_for_call("call-abc")

        assert exc_info.value.code == "create_meeting_failed"


class TestAddAttendee:
    def test_add_attendee_success_returns_attendee_id_and_join_token(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.create_attendee.return_value = {
            "Attendee": {"AttendeeId": "attendee-1", "JoinToken": "token-xyz"}
        }
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        result = chime_client.add_attendee("meeting-123", "alice@example.com")

        assert result == {"attendee_id": "attendee-1", "join_token": "token-xyz"}
        mock_client.create_attendee.assert_called_once_with(
            MeetingId="meeting-123",
            ExternalUserId="alice@example.com",
        )

    def test_add_attendee_client_error_raises_chime_client_error(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.create_attendee.side_effect = _client_error("NotFoundException", "CreateAttendee")
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        with pytest.raises(chime_client.ChimeClientError) as exc_info:
            chime_client.add_attendee("meeting-123", "alice@example.com")

        assert exc_info.value.code == "add_attendee_failed"
        assert "NotFoundException" not in exc_info.value.message

    def test_add_attendee_incomplete_response_raises_chime_client_error(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.create_attendee.return_value = {"Attendee": {"AttendeeId": "attendee-1"}}  # no JoinToken
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        with pytest.raises(chime_client.ChimeClientError) as exc_info:
            chime_client.add_attendee("meeting-123", "alice@example.com")

        assert exc_info.value.code == "add_attendee_failed"


class TestDeleteMeeting:
    def test_delete_meeting_success(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.delete_meeting.return_value = {}
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        chime_client.delete_meeting("meeting-123")

        mock_client.delete_meeting.assert_called_once_with(MeetingId="meeting-123")

    def test_delete_meeting_not_found_is_treated_as_success(self, monkeypatch):
        """Idempotency: deleting an already-gone meeting must not raise —
        this matters for the orphaned-meeting backstop sweep racing with
        a normal leave_call()-triggered delete."""
        mock_client = MagicMock()
        mock_client.delete_meeting.side_effect = _client_error("NotFoundException", "DeleteMeeting")
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        chime_client.delete_meeting("meeting-123")  # must not raise

    def test_delete_meeting_other_client_error_raises_chime_client_error(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.delete_meeting.side_effect = _client_error("ThrottlingException", "DeleteMeeting")
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        with pytest.raises(chime_client.ChimeClientError) as exc_info:
            chime_client.delete_meeting("meeting-123")

        assert exc_info.value.code == "delete_meeting_failed"
        assert "ThrottlingException" not in exc_info.value.message

    def test_delete_meeting_botocore_error_raises_chime_client_error(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.delete_meeting.side_effect = BotoCoreError()
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        with pytest.raises(chime_client.ChimeClientError) as exc_info:
            chime_client.delete_meeting("meeting-123")

        assert exc_info.value.code == "delete_meeting_failed"


class TestGetMeeting:
    def test_get_meeting_success_returns_meeting_info(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.get_meeting.return_value = {
            "Meeting": {
                "MeetingId": "meeting-123",
                "MediaRegion": "us-east-1",
                "MediaPlacement": {"AudioHostUrl": "wss://example"},
            }
        }
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        result = chime_client.get_meeting("meeting-123")

        assert result["chime_meeting_id"] == "meeting-123"
        assert result["media_region"] == "us-east-1"

    def test_get_meeting_not_found_returns_none(self, monkeypatch):
        """A meeting that no longer exists is a normal, expected outcome
        for the backstop sweep — must return None, not raise."""
        mock_client = MagicMock()
        mock_client.get_meeting.side_effect = _client_error("NotFoundException", "GetMeeting")
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        result = chime_client.get_meeting("meeting-123")

        assert result is None

    def test_get_meeting_other_client_error_raises_chime_client_error(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.get_meeting.side_effect = _client_error("ThrottlingException", "GetMeeting")
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        with pytest.raises(chime_client.ChimeClientError) as exc_info:
            chime_client.get_meeting("meeting-123")

        assert exc_info.value.code == "get_meeting_failed"
        assert "ThrottlingException" not in exc_info.value.message

    def test_get_meeting_botocore_error_raises_chime_client_error(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.get_meeting.side_effect = BotoCoreError()
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        with pytest.raises(chime_client.ChimeClientError) as exc_info:
            chime_client.get_meeting("meeting-123")

        assert exc_info.value.code == "get_meeting_failed"

    def test_get_meeting_missing_meeting_id_returns_none(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.get_meeting.return_value = {"Meeting": {}}
        monkeypatch.setattr(chime_client, "_client", lambda: mock_client)

        result = chime_client.get_meeting("meeting-123")

        assert result is None
