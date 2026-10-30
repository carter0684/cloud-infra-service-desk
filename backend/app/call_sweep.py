"""
Chat/Call feature (job-portal-chat-voice spec) — ring-timeout scheduling
and periodic safety-net/backstop sweeps.

Two independent mechanisms guard the same Requirement 5.6/6.3/10.2/10.3
outcomes, matching design.md's "Backend-side 45-second ring timeout"
section:

  1. Per-call asyncio timer (schedule_ring_timeout()) — started
     in-process by whichever ECS task's route handler created the call
     (main.py's POST /api/calls handler calls this immediately after
     call_store.create_call() succeeds). Fires exactly once, 45 seconds
     later, and times out any callee(s) still "ringing" at that point.

  2. Periodic sweep (sweep_stale_ringing_calls(), every 10 seconds, ANY
     task) — a safety net for the case where the owning task's timer
     never fires because that task died mid-timer (crash, deploy,
     rolling restart). Finds every call still status="ringing" whose
     started_at is already older than the 45-second window and times it
     out the same way.

Because BOTH mechanisms can end up trying to time out the SAME call_id
(e.g. the owning task's timer fires at the same moment another task's
sweep also picks it up), double-processing is prevented two ways:
  - call_store.timeout_ring() is itself idempotent for an
    already-"ended" call (returns the row unchanged, does not call
    chime_client.delete_meeting() again) — see call_store.py.
  - This module additionally keeps a process-local
    _recently_processed_call_ids set (short in-memory dedup window) so
    two near-simultaneous timeout attempts from THIS process for the
    same call_id collapse into a single call_store.timeout_ring() call,
    avoiding a redundant DynamoDB write/read round trip even though the
    end result would be correct either way. This is a
    performance/cleanliness guard, not a correctness requirement — the
    correctness guarantee is timeout_ring()'s own idempotency, which
    also protects against a DIFFERENT process (another ECS task) racing
    on the same call_id, which this in-memory set cannot help with.

The orphaned-meeting backstop sweep (Requirement 10's cost-control
guarantee — Task 4.7) runs on the same 10-second interval and force-ends
any call session (ringing OR active) with no activity for over
ORPHAN_MEETING_STALE_SECONDS, as a defence against a crashed/stuck task
leaving a Chime meeting billing indefinitely with nobody left to clean
it up via the normal decline/timeout/leave paths.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import call_store

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 10

# Process-local, short-lived dedup guard — see module docstring. Not a
# distributed lock; only prevents redundant work WITHIN this one ECS
# task's process, which is the only scope where it's needed given
# timeout_ring()'s own cross-process idempotency.
_recently_processed_call_ids: set[str] = set()


async def _timeout_after_delay(call_id: str, delay_seconds: float) -> None:
    """Sleep for delay_seconds, then time out any callee(s) still
    ringing on this call. Swallows and logs any exception so a failure
    here (e.g. a transient DynamoDB error) never crashes the ECS task's
    event loop — the periodic sweep remains as a safety net regardless."""
    try:
        await asyncio.sleep(delay_seconds)
        if call_id in _recently_processed_call_ids:
            return
        _recently_processed_call_ids.add(call_id)
        try:
            call_store.timeout_ring(call_id)
            logger.info("call_sweep: ring timer fired for call %s", call_id)
        finally:
            _recently_processed_call_ids.discard(call_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("call_sweep: ring timer failed for call %s", call_id)


def schedule_ring_timeout(call_id: str, *, delay_seconds: float = call_store.RING_TIMEOUT_SECONDS) -> "asyncio.Task":
    """
    Start the in-process 45-second ring timer for a newly created call.
    Called by main.py's POST /api/calls route handler immediately after
    call_store.create_call() succeeds — must be called from within a
    running asyncio event loop (i.e. from an async route handler).

    Returns the created asyncio.Task so a caller could cancel it early
    if desired (not currently used — accept_call()/decline_call()/
    cancel_call() reaching status="ended" before the timer fires simply
    means timeout_ring() becomes a no-op when the timer does eventually
    fire, per call_store's idempotency, so explicit cancellation is an
    optimisation, not a correctness requirement).
    """
    return asyncio.create_task(_timeout_after_delay(call_id, delay_seconds))


def sweep_stale_ringing_calls() -> int:
    """
    Safety-net sweep (Task 4.5): find every call still status="ringing"
    older than the 45-second ring window and time it out. Returns the
    number of calls processed (for logging/testing).

    This is synchronous/blocking by design (matches call_store.py's
    synchronous DynamoDB calls) — the async wrapper loop below runs it
    in the event loop directly since DynamoDB calls here are expected to
    be fast relative to the 10-second sweep interval; if this ever
    becomes a bottleneck, this function is where an
    asyncio.to_thread(...) wrapper would be added.
    """
    stale = call_store.list_stale_ringing_calls()
    processed = 0
    for call in stale:
        call_id = call["call_id"]
        if call_id in _recently_processed_call_ids:
            continue
        _recently_processed_call_ids.add(call_id)
        try:
            call_store.timeout_ring(call_id)
            processed += 1
            logger.info("call_sweep: sweep timed out stale ringing call %s", call_id)
        except Exception:
            logger.exception("call_sweep: sweep failed to time out call %s", call_id)
        finally:
            _recently_processed_call_ids.discard(call_id)
    return processed


def sweep_orphaned_meetings() -> int:
    """
    Backstop sweep (Task 4.7, Requirement 10's cost-control guarantee):
    find every call session (ringing OR active) with no activity for
    over ORPHAN_MEETING_STALE_SECONDS and force-end it via
    call_store.timeout_ring() (for still-ringing calls) or a direct
    end-and-release (for still-active calls whose participants never
    called leave_call() — e.g. every client crashed without a clean
    disconnect). Returns the number of calls processed.

    A call already caught by sweep_stale_ringing_calls() within the same
    process in this sweep tick is skipped via the same dedup guard, but
    the two sweeps are independent and safe to run even if one of them
    is disabled — each is individually idempotent via call_store's own
    "ended is terminal" guarantee.
    """
    orphaned = call_store.list_orphaned_active_calls()
    processed = 0
    for call in orphaned:
        call_id = call["call_id"]
        if call_id in _recently_processed_call_ids:
            continue
        _recently_processed_call_ids.add(call_id)
        try:
            if call.get("status") == "ringing":
                call_store.timeout_ring(call_id)
            else:
                _force_end_orphaned_call(call_id)
            processed += 1
            logger.warning(
                "call_sweep: backstop sweep force-ended orphaned call %s (status was %s)",
                call_id, call.get("status"),
            )
        except Exception:
            logger.exception("call_sweep: backstop sweep failed to end orphaned call %s", call_id)
        finally:
            _recently_processed_call_ids.discard(call_id)
    return processed


def _force_end_orphaned_call(call_id: str) -> None:
    """
    Force-end a call whose status is "active" (or "ringing", though
    that path is normally handled via timeout_ring() above) but which
    has had no activity for over ORPHAN_MEETING_STALE_SECONDS — e.g.
    every participant's client crashed without a clean leave_call().
    Marks every still-active participant entry as "left" (the closest
    accurate description of "this person's client silently
    disappeared") and lets that drop the active-participant count to
    zero, which triggers call_store's own release-and-end path — this
    reuses leave_call()'s logic rather than duplicating the
    delete_meeting()+ttl bookkeeping here.
    """
    call = call_store.get_call(call_id)
    if not call or call.get("status") == "ended":
        return
    participants = call.get("participants") or {}
    active_emails = [e for e, p in participants.items() if p.get("state") in ("ringing", "joined")]
    for email in active_emails:
        try:
            call_store.leave_call(call_id, participant_email=email)
        except call_store.CallStoreError:
            # Call may have already ended as a side effect of a prior
            # iteration of this loop dropping the count to zero.
            break


async def _sweep_loop() -> None:
    """Runs sweep_stale_ringing_calls() and sweep_orphaned_meetings()
    every SWEEP_INTERVAL_SECONDS, forever, until cancelled (main.py
    cancels this task on shutdown). Each sweep failure is logged and
    swallowed so one bad iteration never stops future iterations."""
    while True:
        try:
            sweep_stale_ringing_calls()
            sweep_orphaned_meetings()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("call_sweep: periodic sweep iteration failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


_sweep_task: Optional["asyncio.Task"] = None


def start_background_sweeps() -> "asyncio.Task":
    """
    Start the periodic sweep loop — called once from main.py's FastAPI
    startup event handler. Returns the created asyncio.Task; main.py's
    shutdown handler should call stop_background_sweeps() with it (or
    just cancel it directly) to stop the loop cleanly on graceful
    shutdown.
    """
    global _sweep_task
    _sweep_task = asyncio.create_task(_sweep_loop())
    return _sweep_task


def stop_background_sweeps() -> None:
    """Cancel the periodic sweep loop started by start_background_sweeps().
    Safe to call even if it was never started (no-op)."""
    global _sweep_task
    if _sweep_task is not None:
        _sweep_task.cancel()
        _sweep_task = None
