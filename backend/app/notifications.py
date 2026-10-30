"""
Email notifications for the Cloud Infra Job Portal — AWS SES backed.

Replaces the local mock (print-to-console) implementation with real
boto3 SES send_email() calls, gated behind a dedup check against the
NotificationDedup DynamoDB table (5-minute window — Requirement covers
avoiding duplicate sends when e.g. 2 ECS tasks race on the same event)
and a single 60-second retry with a final CloudWatch-logged failure entry
if the retry also fails, so a transient SES throttle/outage never crashes
the request that triggered the notification.

Local dev mode (ENV=local or unset) keeps the original print-to-console
behavior so `job-portal/README.md`'s local mock workflow is unaffected.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone

import boto3

from . import dynamo_repository as repo

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ENV = os.environ.get("ENV", "local")
# Must be an Amazon SES identity you have verified. Set SES_SENDER_ADDRESS.
SES_SENDER_ADDRESS = os.environ.get("SES_SENDER_ADDRESS", "portal@example.com")
NOTIFICATION_DEDUP_WINDOW_SECONDS = 300
RETRY_DELAY_SECONDS = 60


def _build_team() -> dict:
    """Founding admins (SEED_ADMIN_EMAILS) who receive 'ticket raised'
    notifications, with optional friendly names from SEED_USER_NAMES."""
    from . import user_store

    return {
        email: user_store.SEED_ADMIN_NAMES.get(email, email)
        for email in sorted(user_store.SEED_ADMIN_EMAILS)
    }


TEAM = _build_team()


def _ses_client():
    return boto3.client("ses", region_name=AWS_REGION)


def _dedup_key(to: str, subject: str, body: str) -> str:
    digest = hashlib.sha256(f"{to}|{subject}|{body}".encode("utf-8")).hexdigest()
    return f"notify:{digest}"


def _send_via_ses(to: str, subject: str, body: str) -> None:
    client = _ses_client()
    client.send_email(
        Source=SES_SENDER_ADDRESS,
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
        },
    )


def _send_mock(to: str, subject: str, body: str) -> None:
    print(f"\n{'='*60}")
    print(f"📧  MOCK EMAIL  [{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}]")
    print(f"To : {to}")
    print(f"Sub: {subject}")
    print(f"---")
    print(body)
    print(f"{'='*60}\n")


def _send(to: str, subject: str, body: str):
    """
    Send a single notification email, with dedup + retry-once-then-log-failure
    semantics when ENV != local (i.e. running against real AWS SES).
    """
    if ENV == "local":
        _send_mock(to, subject, body)
        return

    dedup_key = _dedup_key(to, subject, body)
    is_new = repo.check_and_set_notification_dedup(dedup_key, NOTIFICATION_DEDUP_WINDOW_SECONDS)
    if not is_new:
        logger.info("notifications: skipped duplicate send to %s (dedup window)", to)
        return

    try:
        _send_via_ses(to, subject, body)
        return
    except Exception as e:
        logger.warning("notifications: first SES send attempt to %s failed: %s", to, e)

    time.sleep(RETRY_DELAY_SECONDS)

    try:
        _send_via_ses(to, subject, body)
    except Exception as e:
        logger.error(
            "notifications: SES send to %s failed after retry — giving up. subject=%r error=%s",
            to, subject, e,
        )


def notify_job_assigned(job: dict):
    _send(
        job["assignee_email"],
        f"[Job Portal] New job assigned to you: {job['title']}",
        f"Hi {job['assignee_name']},\n\n"
        f"A new job has been assigned to you.\n\n"
        f"Title   : {job['title']}\n"
        f"Priority: {job['priority']}\n"
        f"Category: {job['category']}\n"
        f"Due     : {job.get('due_date') or 'Not set'}\n"
        f"Desc    : {job['description']}\n\n"
        f"View at: https://localhost:5200/jobs/{job['id']}\n"
    )


def notify_status_changed(job: dict, old_status: str, actor_name: str):
    recipients = {job["assignee_email"], job["created_by_email"]}
    for email in recipients:
        name = TEAM.get(email, email)
        _send(
            email,
            f"[Job Portal] Status updated: {job['title']}",
            f"Hi {name},\n\n"
            f"Job status changed by {actor_name}.\n\n"
            f"Title : {job['title']}\n"
            f"From  : {old_status}\n"
            f"To    : {job['status']}\n\n"
            f"View at: https://localhost:5200/jobs/{job['id']}\n"
        )


def notify_ai_complete(job: dict, actor_email: str):
    actor_name = TEAM.get(actor_email, actor_email)
    ai_draft = job.get("ai_draft") or {}
    _send(
        actor_email,
        f"[Job Portal] AI analysis complete: {job['title']}",
        f"Hi {actor_name},\n\n"
        f"AI analysis is complete for '{job['title']}'.\n\n"
        f"Severity: {ai_draft.get('severity', 'N/A')}\n"
        f"Summary : {ai_draft.get('issue_summary', 'N/A')}\n\n"
        f"View at: https://localhost:5200/jobs/{job['id']}\n"
    )


def notify_ticket_raised(job: dict, system: str, ticket_id: str):
    for email, name in TEAM.items():
        _send(
            email,
            f"[Job Portal] Ticket raised in {system}: {job['title']}",
            f"Hi {name},\n\n"
            f"A support ticket has been raised for '{job['title']}'.\n\n"
            f"System   : {system}\n"
            f"Ticket ID: {ticket_id}\n\n"
            f"View at: https://localhost:5200/jobs/{job['id']}\n"
        )
