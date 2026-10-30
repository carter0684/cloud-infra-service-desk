"""
Tests for app/call_store.py — call session state machine and >=30-day
call history retention (job-portal-chat-voice spec).

Uses the `chat_tables` fixture (conftest.py), which sets ENV=production
and spins up moto-mocked DynamoDB tables matching data_stack.py's
CallSessionsTable schema (including ConversationCallHistoryIndex) — so
these tests exercise the production (DynamoDB) branch of call_store.py.

chime_client.py itself is mocked directly (unittest.mock) rather than
relying on moto, since moto has no chime-sdk-meetings support — this
mirrors test_chime_client.py's own approach and keeps these tests
focused purely on call_store.py's state-machine logic.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time


@pytest.fixture
def call_store(chat_tables):
    """Reload the module fresh so its module-level TABLE_NAME constant
    picks up the ENV=production env var set by the chat_tables fixture —
    same pattern as test_chat_store.py's chat_store fixture."""
    from app import call_store as call_store_module
    importlib.reload(call_store_module)
    return call_store_module


@pytest.fixture
def mock_chime(call_store, monkeypatch):
    """Replace call_store.chime_client's four entry points with a
    MagicMock that returns predictable, sequential fake IDs — no real
    boto3/Chime SDK calls happen in these tests."""
    mock = MagicMock()
    counter = {"meeting": 0, "attendee": 0}

    def _create_meeting(call_id, media_region=None):
        counter["meeting"] += 1
        return {
            "chime_meeting_id": f"meeting-{counter['meeting']}",
            "media_region": media_region or "us-east-1",
            "media_placement": {},
        }

    def _add_attendee(meeting_id, user_email):
        counter["attendee"] += 1
        return {"attendee_id": f"attendee-{counter['attendee']}", "join_token": f"token-{counter['attendee']}"}

    mock.create_meeting_for_call.side_effect = _create_meeting
    mock.add_attendee.side_effect = _add_attendee
    mock.delete_meeting.return_value = None

    monkeypatch.setattr(call_store, "chime_client", mock)
    return mock


CONVERSATION_ID = "conv-1"
CALLER = "alice@example.com"
CALLEE = "bob@example.com"
CALLEE_2 = "carol@example.com"


class TestCreateCall:
    def test_create_direct_call_places_callee_in_ringing_state(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        assert call["status"] == "ringing"
        assert call["caller_email"] == CALLER
        assert call["participants"][CALLEE]["state"] == "ringing"
        assert call["chime_meeting_id"] == "meeting-1"
        mock_chime.create_meeting_for_call.assert_called_once()

    def test_create_call_only_adds_caller_as_attendee(self, call_store, mock_chime):
        """Requirement 10.3: nobody who is merely being rung (a callee)
        is ever billed as a joined attendee before accepting — but the
        Caller, who actively initiated the Call, is added as an
        attendee immediately so their own client can join the Chime
        meeting right away. create_call() must add exactly the Caller
        and nobody else."""
        call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        mock_chime.add_attendee.assert_called_once_with("meeting-1", CALLER)

    def test_create_group_call_rings_every_callee(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID,
            caller_email=CALLER,
            callee_emails=[CALLEE, CALLEE_2],
            call_type="group",
        )
        assert call["participants"][CALLEE]["state"] == "ringing"
        assert call["participants"][CALLEE_2]["state"] == "ringing"

    def test_create_call_rejects_invalid_call_type(self, call_store, mock_chime):
        with pytest.raises(call_store.CallStoreError):
            call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="webinar"
            )

    def test_create_call_rejects_no_callees(self, call_store, mock_chime):
        with pytest.raises(call_store.CallStoreError):
            call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[], call_type="direct"
            )


class TestDirectCallFlow:
    def test_accept_call_joins_attendee_and_activates_call(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        accepted = call_store.accept_call(call["call_id"], callee_email=CALLEE)

        assert accepted["status"] == "active"
        assert accepted["participants"][CALLEE]["state"] == "joined"
        # Caller was already added at create_call() time; accept_call()
        # adds the callee, for a total of two add_attendee() calls.
        assert mock_chime.add_attendee.call_count == 2
        mock_chime.add_attendee.assert_any_call("meeting-1", CALLEE)

    def test_decline_call_ends_direct_call_and_releases_meeting(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        declined = call_store.decline_call(call["call_id"], callee_email=CALLEE)

        assert declined["status"] == "ended"
        assert declined["participants"][CALLEE]["state"] == "declined"
        mock_chime.delete_meeting.assert_called_once_with("meeting-1")

    def test_cancel_call_by_caller_ends_call(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        cancelled = call_store.cancel_call(call["call_id"], caller_email=CALLER)

        assert cancelled["status"] == "ended"
        mock_chime.delete_meeting.assert_called_once_with("meeting-1")

    def test_cancel_call_by_non_caller_is_rejected(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        with pytest.raises(call_store.CallStoreError):
            call_store.cancel_call(call["call_id"], caller_email=CALLEE)

    def test_timeout_ring_ends_direct_call_with_no_accept(self, call_store, mock_chime):
        """Requirement 5.6."""
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        timed_out = call_store.timeout_ring(call["call_id"])

        assert timed_out["status"] == "ended"
        assert timed_out["participants"][CALLEE]["state"] == "timed_out"
        mock_chime.delete_meeting.assert_called_once_with("meeting-1")

    def test_leave_call_by_last_participant_ends_call(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        call_store.accept_call(call["call_id"], callee_email=CALLEE)
        left = call_store.leave_call(call["call_id"], participant_email=CALLEE)

        assert left["status"] == "ended"
        assert left["participants"][CALLEE]["state"] == "left"
        mock_chime.delete_meeting.assert_called_once_with("meeting-1")

    def test_actions_on_already_ended_call_raise_call_not_active_error(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        call_store.cancel_call(call["call_id"], caller_email=CALLER)

        with pytest.raises(call_store.CallNotActiveError):
            call_store.accept_call(call["call_id"], callee_email=CALLEE)


class TestGroupCallIndependence:
    """Requirement 6.2/6.3: one callee's accept/decline/timeout must
    never affect any other callee, and the call only ends once the
    active-participant count reaches zero."""

    def test_one_callee_accepting_does_not_join_others(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID,
            caller_email=CALLER,
            callee_emails=[CALLEE, CALLEE_2],
            call_type="group",
        )
        accepted = call_store.accept_call(call["call_id"], callee_email=CALLEE)

        assert accepted["participants"][CALLEE]["state"] == "joined"
        assert accepted["participants"][CALLEE_2]["state"] == "ringing"
        # Caller was already added at create_call() time; accept_call()
        # adds CALLEE, for a total of two add_attendee() calls. CALLEE_2
        # is still only ringing and must not have been added.
        assert mock_chime.add_attendee.call_count == 2
        mock_chime.add_attendee.assert_any_call("meeting-1", CALLEE)

    def test_one_callee_declining_does_not_end_call_for_others(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID,
            caller_email=CALLER,
            callee_emails=[CALLEE, CALLEE_2],
            call_type="group",
        )
        call_store.accept_call(call["call_id"], callee_email=CALLEE)
        declined = call_store.decline_call(call["call_id"], callee_email=CALLEE_2)

        assert declined["status"] == "active"
        assert declined["participants"][CALLEE]["state"] == "joined"
        assert declined["participants"][CALLEE_2]["state"] == "declined"
        mock_chime.delete_meeting.assert_not_called()

    def test_one_callee_timing_out_does_not_end_call_for_others(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID,
            caller_email=CALLER,
            callee_emails=[CALLEE, CALLEE_2],
            call_type="group",
        )
        call_store.accept_call(call["call_id"], callee_email=CALLEE)
        timed_out = call_store.timeout_ring(call["call_id"], callee_email=CALLEE_2)

        assert timed_out["status"] == "active"
        assert timed_out["participants"][CALLEE]["state"] == "joined"
        assert timed_out["participants"][CALLEE_2]["state"] == "timed_out"
        mock_chime.delete_meeting.assert_not_called()

    def test_all_callees_declining_ends_the_call(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID,
            caller_email=CALLER,
            callee_emails=[CALLEE, CALLEE_2],
            call_type="group",
        )
        call_store.decline_call(call["call_id"], callee_email=CALLEE)
        final = call_store.decline_call(call["call_id"], callee_email=CALLEE_2)

        assert final["status"] == "ended"
        mock_chime.delete_meeting.assert_called_once()

    def test_last_participant_leaving_group_call_ends_it(self, call_store, mock_chime):
        """Requirement 6.6."""
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID,
            caller_email=CALLER,
            callee_emails=[CALLEE, CALLEE_2],
            call_type="group",
        )
        call_store.accept_call(call["call_id"], callee_email=CALLEE)
        call_store.accept_call(call["call_id"], callee_email=CALLEE_2)

        after_first_leave = call_store.leave_call(call["call_id"], participant_email=CALLEE)
        assert after_first_leave["status"] == "active"
        mock_chime.delete_meeting.assert_not_called()

        after_last_leave = call_store.leave_call(call["call_id"], participant_email=CALLEE_2)
        assert after_last_leave["status"] == "ended"
        mock_chime.delete_meeting.assert_called_once_with("meeting-1")

    def test_invite_to_active_call_adds_new_ringing_participant(self, call_store, mock_chime):
        """Requirement 6.7."""
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="group"
        )
        call_store.accept_call(call["call_id"], callee_email=CALLEE)
        invited = call_store.invite_to_active_call(call["call_id"], new_callee_email=CALLEE_2)

        assert invited["participants"][CALLEE_2]["state"] == "ringing"
        # Caller (at create_call) and CALLEE (at accept_call) have been
        # added so far — CALLEE_2 is merely ringing and must not be.
        assert mock_chime.add_attendee.call_count == 2

    def test_invite_to_direct_call_is_rejected(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        with pytest.raises(call_store.CallStoreError):
            call_store.invite_to_active_call(call["call_id"], new_callee_email=CALLEE_2)


class TestCallHistory:
    def test_call_history_is_chronological_most_recent_first(self, call_store, mock_chime):
        with freeze_time("2026-08-01 10:00:00"):
            first = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )
            call_store.cancel_call(first["call_id"], caller_email=CALLER)

        with freeze_time("2026-08-01 11:00:00"):
            second = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )
            call_store.cancel_call(second["call_id"], caller_email=CALLER)

        history = call_store.list_call_history(CONVERSATION_ID, requester_email=CALLER)

        assert len(history) == 2
        assert history[0]["call_id"] == second["call_id"]
        assert history[1]["call_id"] == first["call_id"]

    def test_call_history_is_scoped_to_call_participants_only(self, call_store, mock_chime):
        """Requirement 11.4: a Team_Member who was never invited to a
        given call (even if currently a Conversation participant) must
        not see that call's record."""
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        call_store.cancel_call(call["call_id"], caller_email=CALLER)

        history_for_caller = call_store.list_call_history(CONVERSATION_ID, requester_email=CALLER)
        history_for_callee = call_store.list_call_history(CONVERSATION_ID, requester_email=CALLEE)
        history_for_outsider = call_store.list_call_history(CONVERSATION_ID, requester_email=CALLEE_2)

        assert len(history_for_caller) == 1
        assert len(history_for_callee) == 1
        assert len(history_for_outsider) == 0

    def test_call_history_includes_only_this_conversation(self, call_store, mock_chime):
        call_a = call_store.create_call(
            conversation_id="conv-a", caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        call_store.cancel_call(call_a["call_id"], caller_email=CALLER)

        call_b = call_store.create_call(
            conversation_id="conv-b", caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        call_store.cancel_call(call_b["call_id"], caller_email=CALLER)

        history_a = call_store.list_call_history("conv-a", requester_email=CALLER)
        assert len(history_a) == 1
        assert history_a[0]["call_id"] == call_a["call_id"]

    def test_ended_call_has_ttl_set_to_ended_at_plus_30_days(self, call_store, mock_chime):
        """Requirement 11.2."""
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )
            ended = call_store.cancel_call(call["call_id"], caller_email=CALLER)

        ended_at_epoch = int(__import__("datetime").datetime.fromisoformat(ended["ended_at"]).timestamp())
        expected_ttl = ended_at_epoch + (30 * 24 * 60 * 60)
        assert ended["ttl"] == expected_ttl


class TestSweepTargetLists:
    def test_list_stale_ringing_calls_finds_old_ringing_calls(self, call_store, mock_chime):
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )

        with freeze_time("2026-08-01 10:01:00"):  # 60s later, > 45s ring timeout
            stale = call_store.list_stale_ringing_calls()

        assert len(stale) == 1
        assert stale[0]["call_id"] == call["call_id"]

    def test_list_stale_ringing_calls_ignores_recent_ringing_calls(self, call_store, mock_chime):
        with freeze_time("2026-08-01 10:00:00"):
            call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )

        with freeze_time("2026-08-01 10:00:10"):  # only 10s later
            stale = call_store.list_stale_ringing_calls()

        assert len(stale) == 0

    def test_list_stale_ringing_calls_ignores_already_accepted_calls(self, call_store, mock_chime):
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )
            call_store.accept_call(call["call_id"], callee_email=CALLEE)

        with freeze_time("2026-08-01 10:01:00"):
            stale = call_store.list_stale_ringing_calls()

        assert len(stale) == 0

    def test_list_orphaned_active_calls_finds_calls_stale_beyond_threshold(self, call_store, mock_chime):
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )
            call_store.accept_call(call["call_id"], callee_email=CALLEE)

        with freeze_time("2026-08-01 10:03:00"):  # 180s later, > 120s orphan threshold
            orphaned = call_store.list_orphaned_active_calls()

        assert len(orphaned) == 1
        assert orphaned[0]["call_id"] == call["call_id"]

    def test_list_orphaned_active_calls_ignores_ended_calls(self, call_store, mock_chime):
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )
            call_store.cancel_call(call["call_id"], caller_email=CALLER)

        with freeze_time("2026-08-01 10:03:00"):
            orphaned = call_store.list_orphaned_active_calls()

        assert len(orphaned) == 0


class TestIsUserInAnyActiveCall:
    def test_true_once_joined_to_an_active_call(self, call_store, mock_chime):
        """Requirement 8.3 (Presence "in call")."""
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        assert call_store.is_user_in_any_active_call(CALLEE) is False  # still just ringing

        call_store.accept_call(call["call_id"], callee_email=CALLEE)
        assert call_store.is_user_in_any_active_call(CALLEE) is True

    def test_false_after_leaving_the_call(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE, CALLEE_2], call_type="group"
        )
        call_store.accept_call(call["call_id"], callee_email=CALLEE)
        call_store.accept_call(call["call_id"], callee_email=CALLEE_2)
        assert call_store.is_user_in_any_active_call(CALLEE) is True

        call_store.leave_call(call["call_id"], participant_email=CALLEE)
        assert call_store.is_user_in_any_active_call(CALLEE) is False
        assert call_store.is_user_in_any_active_call(CALLEE_2) is True  # unaffected

    def test_false_once_the_call_has_ended(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        call_store.accept_call(call["call_id"], callee_email=CALLEE)
        assert call_store.is_user_in_any_active_call(CALLEE) is True

        call_store.leave_call(call["call_id"], participant_email=CALLEE)  # last participant leaves — call ends
        assert call_store.is_user_in_any_active_call(CALLEE) is False

    def test_false_for_a_user_never_involved_in_any_call(self, call_store, mock_chime):
        assert call_store.is_user_in_any_active_call(CALLEE_2) is False

    def test_false_for_a_declined_callee(self, call_store, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE, CALLEE_2], call_type="group"
        )
        call_store.accept_call(call["call_id"], callee_email=CALLEE)
        call_store.decline_call(call["call_id"], callee_email=CALLEE_2)

        assert call_store.is_user_in_any_active_call(CALLEE) is True
        assert call_store.is_user_in_any_active_call(CALLEE_2) is False
