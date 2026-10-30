"""
Tests for app/notifications.py using moto's SES mock.

Covers: successful send, dedup skip (5-minute window), retry-then-succeed,
and retry-then-fail-and-log paths. ENV is forced to a non-"local" value for
all tests here so the real (moto-backed) SES send path is exercised
instead of the console-print mock.
"""

from __future__ import annotations

import importlib

import boto3
import pytest


@pytest.fixture
def ses_verified_identity(dynamodb_tables):
    """moto's mock_aws() is already active via dynamodb_tables; verify the
    sender identity so send_email() doesn't reject it."""
    client = boto3.client("ses", region_name="us-east-1")
    client.verify_email_identity(EmailAddress="admin.one@example.com")
    return client


@pytest.fixture
def notifications_module(dynamodb_tables, ses_verified_identity, monkeypatch):
    monkeypatch.setenv("ENV", "production")
    from app import notifications as notifications_module
    importlib.reload(notifications_module)
    # Speed up tests — no need to actually sleep 60s in the retry path.
    monkeypatch.setattr(notifications_module, "RETRY_DELAY_SECONDS", 0)
    return notifications_module


SAMPLE_JOB = {
    "id": "job-1",
    "title": "Sample job",
    "priority": "High",
    "category": "Networking",
    "due_date": None,
    "description": "A sample job description",
    "assignee_email": "admin.two@example.com",
    "assignee_name": "Admin Two",
    "created_by_email": "admin.one@example.com",
    "status": "Open",
    "ai_draft": {"severity": "high", "issue_summary": "Test summary"},
}


class TestSuccessfulSend:
    def test_notify_job_assigned_sends_without_error(self, notifications_module):
        notifications_module.notify_job_assigned(SAMPLE_JOB)
        # No exception raised == success; moto records the send internally.

    def test_notify_status_changed_sends_to_both_assignee_and_creator(self, notifications_module, monkeypatch):
        sent_to = []
        original_send = notifications_module._send_via_ses

        def _spy(to, subject, body):
            sent_to.append(to)
            return original_send(to, subject, body)

        monkeypatch.setattr(notifications_module, "_send_via_ses", _spy)
        notifications_module.notify_status_changed(SAMPLE_JOB, "Open", "Admin One")
        assert set(sent_to) == {"admin.two@example.com", "admin.one@example.com"}


class TestDedupSkip:
    def test_second_identical_send_within_window_is_skipped(self, notifications_module, monkeypatch):
        call_count = {"n": 0}
        original_send = notifications_module._send_via_ses

        def _counting_send(to, subject, body):
            call_count["n"] += 1
            return original_send(to, subject, body)

        monkeypatch.setattr(notifications_module, "_send_via_ses", _counting_send)

        notifications_module.notify_job_assigned(SAMPLE_JOB)
        assert call_count["n"] == 1

        # Identical notification again — should be deduped, not actually sent.
        notifications_module.notify_job_assigned(SAMPLE_JOB)
        assert call_count["n"] == 1

    def test_different_content_is_not_deduped(self, notifications_module, monkeypatch):
        call_count = {"n": 0}
        original_send = notifications_module._send_via_ses

        def _counting_send(to, subject, body):
            call_count["n"] += 1
            return original_send(to, subject, body)

        monkeypatch.setattr(notifications_module, "_send_via_ses", _counting_send)

        notifications_module.notify_job_assigned(SAMPLE_JOB)
        different_job = {**SAMPLE_JOB, "title": "A different job title"}
        notifications_module.notify_job_assigned(different_job)
        assert call_count["n"] == 2


class TestRetryThenSucceed:
    def test_first_attempt_fails_second_succeeds(self, notifications_module, monkeypatch):
        attempts = {"n": 0}
        original_send = notifications_module._send_via_ses

        def _fail_once_then_succeed(to, subject, body):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("simulated transient SES throttle")
            return original_send(to, subject, body)

        monkeypatch.setattr(notifications_module, "_send_via_ses", _fail_once_then_succeed)

        notifications_module.notify_job_assigned(SAMPLE_JOB)
        assert attempts["n"] == 2


class TestRetryThenFailAndLog:
    def test_both_attempts_fail_logs_error_and_does_not_raise(self, notifications_module, monkeypatch, caplog):
        def _always_fail(to, subject, body):
            raise RuntimeError("simulated persistent SES outage")

        monkeypatch.setattr(notifications_module, "_send_via_ses", _always_fail)

        import logging
        caplog.set_level(logging.ERROR, logger="app.notifications")

        # Must not raise — a notification failure should never crash the
        # request that triggered it.
        notifications_module.notify_job_assigned(SAMPLE_JOB)

        assert any("failed after retry" in record.message for record in caplog.records)


class TestLocalModeUsesMock:
    def test_local_env_never_calls_ses(self, dynamodb_tables, monkeypatch):
        monkeypatch.setenv("ENV", "local")
        from app import notifications as local_notifications
        importlib.reload(local_notifications)

        called = {"n": 0}
        monkeypatch.setattr(local_notifications, "_send_via_ses", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

        local_notifications.notify_job_assigned(SAMPLE_JOB)
        assert called["n"] == 0  # _send_via_ses must never be invoked in local mode
