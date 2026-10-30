"""
Tests for app/call_sweep.py — 45-second ring-timeout scheduling and the
periodic safety-net/orphaned-meeting backstop sweeps (job-portal-chat-
voice spec).

Uses the `chat_tables` fixture (conftest.py) for the production
(DynamoDB) branch of call_store.py, and a mocked chime_client (same
approach as test_call_store.py) so no real boto3/Chime SDK calls
happen. asyncio functions are exercised via asyncio.run() — no
pytest-asyncio dependency is added, matching this test suite's existing
convention of plain synchronous tests wherever possible.

freeze_time is used to control call_store's own time-based reads
(started_at comparisons in list_stale_ringing_calls()/
list_orphaned_active_calls()) — it does NOT affect asyncio's own timer
resolution, so schedule_ring_timeout()'s real 45-second in-process timer
is tested with a small explicit delay_seconds override rather than
waiting out the real default.
"""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time


@pytest.fixture
def call_store(chat_tables):
    from app import call_store as call_store_module
    importlib.reload(call_store_module)
    return call_store_module


@pytest.fixture
def call_sweep(call_store):
    """Reload call_sweep AFTER call_store has already been reloaded for
    this test (call_sweep imports call_store at module load time via
    `from . import call_store` — reloading call_sweep picks up the
    already-reloaded call_store module object). Also clears the
    process-local dedup guard between tests so no test can observe
    another test's leftover state."""
    from app import call_sweep as call_sweep_module
    importlib.reload(call_sweep_module)
    call_sweep_module._recently_processed_call_ids.clear()
    return call_sweep_module


@pytest.fixture
def mock_chime(call_store, monkeypatch):
    mock = MagicMock()
    counter = {"meeting": 0, "attendee": 0}

    def _create_meeting(call_id, media_region=None):
        counter["meeting"] += 1
        return {"chime_meeting_id": f"meeting-{counter['meeting']}", "media_region": "us-east-1", "media_placement": {}}

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


class TestScheduleRingTimeout:
    def test_timer_times_out_a_still_ringing_call_after_delay(self, call_store, call_sweep, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )

        async def _run():
            task = call_sweep.schedule_ring_timeout(call["call_id"], delay_seconds=0.05)
            await task

        asyncio.run(_run())

        result = call_store.get_call(call["call_id"])
        assert result["status"] == "ended"
        assert result["participants"][CALLEE]["state"] == "timed_out"
        mock_chime.delete_meeting.assert_called_once()

    def test_timer_is_a_no_op_if_call_already_accepted_before_it_fires(self, call_store, call_sweep, mock_chime):
        call = call_store.create_call(
            conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
        )
        call_store.accept_call(call["call_id"], callee_email=CALLEE)

        async def _run():
            task = call_sweep.schedule_ring_timeout(call["call_id"], delay_seconds=0.05)
            await task

        asyncio.run(_run())

        result = call_store.get_call(call["call_id"])
        assert result["status"] == "active"  # unchanged — accept already happened
        assert result["participants"][CALLEE]["state"] == "joined"
        mock_chime.delete_meeting.assert_not_called()

    def test_timer_exception_is_swallowed_and_does_not_propagate(self, call_store, call_sweep, mock_chime, monkeypatch):
        """A transient failure inside the timer callback must not crash
        the ECS task's event loop — it should be logged and swallowed,
        leaving the periodic sweep as the safety net for that call."""
        def _boom(call_id, callee_email=None):
            raise RuntimeError("simulated transient failure")

        monkeypatch.setattr(call_store, "timeout_ring", _boom)

        async def _run():
            task = call_sweep.schedule_ring_timeout("fake-call-id", delay_seconds=0.05)
            await task  # must not raise

        asyncio.run(_run())  # no exception propagates out


class TestSweepStaleRingingCalls:
    def test_sweep_times_out_a_stale_ringing_call(self, call_store, call_sweep, mock_chime):
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )

        with freeze_time("2026-08-01 10:01:00"):  # 60s later, > 45s ring window
            processed = call_sweep.sweep_stale_ringing_calls()

        assert processed == 1
        result = call_store.get_call(call["call_id"])
        assert result["status"] == "ended"
        mock_chime.delete_meeting.assert_called_once()

    def test_sweep_ignores_calls_within_the_ring_window(self, call_store, call_sweep, mock_chime):
        with freeze_time("2026-08-01 10:00:00"):
            call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )

        with freeze_time("2026-08-01 10:00:10"):
            processed = call_sweep.sweep_stale_ringing_calls()

        assert processed == 0
        mock_chime.delete_meeting.assert_not_called()

    def test_sweep_does_not_double_process_the_same_call_within_one_process(
        self, call_store, call_sweep, mock_chime, monkeypatch
    ):
        """Simulates the in-process timer and the periodic sweep both
        reaching the same call_id at effectively the same moment — the
        dedup guard must prevent a second timeout_ring() call for the
        SAME call_id while the first is still "in flight" (represented
        here by not clearing the dedup set before calling sweep a second
        time, as sweep_stale_ringing_calls() itself always clears its
        own guard entry in its `finally` block once its own call
        completes — so this test instead directly seeds the guard set to
        simulate a concurrent in-flight timer)."""
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )

        call_sweep._recently_processed_call_ids.add(call["call_id"])

        timeout_spy = MagicMock(wraps=call_store.timeout_ring)
        monkeypatch.setattr(call_store, "timeout_ring", timeout_spy)

        with freeze_time("2026-08-01 10:01:00"):
            processed = call_sweep.sweep_stale_ringing_calls()

        assert processed == 0  # skipped — already "in flight" per the guard
        timeout_spy.assert_not_called()

    def test_sweep_is_idempotent_against_an_already_ended_call(self, call_store, call_sweep, mock_chime):
        """Even without the in-memory guard, calling timeout_ring() on
        an already-ended call must be a safe no-op — this exercises
        call_store's own idempotency (relied on as the CROSS-process
        safety net, since the in-memory guard only helps within one
        process)."""
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )
            call_store.cancel_call(call["call_id"], caller_email=CALLER)

        mock_chime.delete_meeting.reset_mock()

        with freeze_time("2026-08-01 10:01:00"):
            # The already-ended call won't even be returned by
            # list_stale_ringing_calls() (it filters status="ringing"),
            # but call timeout_ring() on it directly to prove the
            # underlying idempotency this whole module relies on.
            call_store.timeout_ring(call["call_id"])

        mock_chime.delete_meeting.assert_not_called()

    def test_sweep_in_group_call_only_times_out_still_ringing_callees(self, call_store, call_sweep, mock_chime):
        """Requirement 6.3: a periodic sweep pass must not disturb an
        already-joined callee's state on a Group_Call."""
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID,
                caller_email=CALLER,
                callee_emails=[CALLEE, CALLEE_2],
                call_type="group",
            )
            call_store.accept_call(call["call_id"], callee_email=CALLEE)

        with freeze_time("2026-08-01 10:01:00"):
            processed = call_sweep.sweep_stale_ringing_calls()

        assert processed == 1
        result = call_store.get_call(call["call_id"])
        assert result["status"] == "active"  # still active — CALLEE remains joined
        assert result["participants"][CALLEE]["state"] == "joined"
        assert result["participants"][CALLEE_2]["state"] == "timed_out"
        mock_chime.delete_meeting.assert_not_called()


class TestSweepOrphanedMeetings:
    def test_sweep_force_ends_an_orphaned_active_call(self, call_store, call_sweep, mock_chime):
        """Requirement 10.2/10.3 backstop — every client crashed without
        a clean leave_call(), so the normal cleanup paths never fired;
        the backstop sweep must still release the Chime meeting."""
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )
            call_store.accept_call(call["call_id"], callee_email=CALLEE)

        with freeze_time("2026-08-01 10:03:00"):  # 180s later, > 120s orphan threshold
            processed = call_sweep.sweep_orphaned_meetings()

        assert processed == 1
        result = call_store.get_call(call["call_id"])
        assert result["status"] == "ended"
        mock_chime.delete_meeting.assert_called_once()

    def test_sweep_ignores_active_calls_within_the_orphan_threshold(self, call_store, call_sweep, mock_chime):
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )
            call_store.accept_call(call["call_id"], callee_email=CALLEE)

        with freeze_time("2026-08-01 10:01:00"):  # only 60s later, under the 120s threshold
            processed = call_sweep.sweep_orphaned_meetings()

        assert processed == 0
        mock_chime.delete_meeting.assert_not_called()

    def test_sweep_ignores_already_ended_calls(self, call_store, call_sweep, mock_chime):
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )
            call_store.cancel_call(call["call_id"], caller_email=CALLER)

        mock_chime.delete_meeting.reset_mock()

        with freeze_time("2026-08-01 10:03:00"):
            processed = call_sweep.sweep_orphaned_meetings()

        assert processed == 0
        mock_chime.delete_meeting.assert_not_called()

    def test_sweep_group_call_orphan_releases_meeting_once_all_forced_to_leave(
        self, call_store, call_sweep, mock_chime
    ):
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID,
                caller_email=CALLER,
                callee_emails=[CALLEE, CALLEE_2],
                call_type="group",
            )
            call_store.accept_call(call["call_id"], callee_email=CALLEE)
            call_store.accept_call(call["call_id"], callee_email=CALLEE_2)

        with freeze_time("2026-08-01 10:03:00"):
            processed = call_sweep.sweep_orphaned_meetings()

        assert processed == 1
        result = call_store.get_call(call["call_id"])
        assert result["status"] == "ended"
        assert result["participants"][CALLEE]["state"] == "left"
        assert result["participants"][CALLEE_2]["state"] == "left"
        mock_chime.delete_meeting.assert_called_once()

    def test_sweep_does_not_double_process_the_same_call_within_one_process(self, call_store, call_sweep, mock_chime):
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )
            call_store.accept_call(call["call_id"], callee_email=CALLEE)

        call_sweep._recently_processed_call_ids.add(call["call_id"])

        with freeze_time("2026-08-01 10:03:00"):
            processed = call_sweep.sweep_orphaned_meetings()

        assert processed == 0
        mock_chime.delete_meeting.assert_not_called()


class TestBothSweepsTogether:
    def test_stale_ringing_call_is_not_also_picked_up_by_orphan_sweep_in_the_same_tick(
        self, call_store, call_sweep, mock_chime
    ):
        """A call that is stale-ringing (>45s) is also, by definition,
        older than the 120s orphan threshold only once it crosses that
        larger window too — verify that running both sweeps back-to-back
        for a call that is stale-ringing but NOT yet orphan-stale only
        results in ONE timeout_ring() call (from the ringing sweep), and
        the orphan sweep correctly finds nothing to do since the call is
        no longer "ringing" (it's "ended") by the time it runs."""
        with freeze_time("2026-08-01 10:00:00"):
            call = call_store.create_call(
                conversation_id=CONVERSATION_ID, caller_email=CALLER, callee_emails=[CALLEE], call_type="direct"
            )

        with freeze_time("2026-08-01 10:01:00"):  # 60s later: stale-ringing, not yet orphan-stale (120s)
            ring_processed = call_sweep.sweep_stale_ringing_calls()
            orphan_processed = call_sweep.sweep_orphaned_meetings()

        assert ring_processed == 1
        assert orphan_processed == 0  # call is already "ended" by the time this runs
        mock_chime.delete_meeting.assert_called_once()
