"""
Freshservice bidirectional sync for the Cloud Infra Job Portal.

Inbound: verify_and_handle_webhook() — called by the ALB listener rule for
path /api/webhooks/freshservice* (Task 7.7, which intentionally has NO
authenticate-oidc action, since Freshservice's servers can't complete an
Azure AD login). Authenticity comes from a static shared API key that
Freshservice sends on every webhook call in a custom header, compared
(constant-time) against the value stored in Secrets Manager
(job-portal/freshservice-webhook-hmac-secret).

NOTE: this was originally designed for an HMAC-SHA256 signature over the
raw request body, but Freshservice's Workflow Automator "Trigger Webhook"
action only supports Basic Auth or a static "API key" header for outbound
webhook authentication — it cannot compute a per-request HMAC. Switched to
a static shared-secret header comparison to match what Freshservice can
actually send. The secret name was kept as-is to avoid an extra
Secrets Manager/IAM change; only the verification semantics changed.

Outbound: poll_freshservice_ticket_updates() — invoked on a 5-minute
EventBridge Scheduler cadence (Task 11) to catch ticket status changes made
directly in Freshservice's UI (i.e. not initiated by the Portal), so the
Portal's Job.status stays in sync even without an inbound webhook for that
particular change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Optional

import boto3
import httpx

from . import dynamo_repository as repo

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Empty = Freshservice integration is switched off (see org_config.features()).
FRESHSERVICE_DOMAIN = os.environ.get("FRESHSERVICE_DOMAIN", "")
FRESHSERVICE_API_KEY_SECRET_NAME = os.environ.get(
    "FRESHSERVICE_API_KEY_SECRET_NAME", "job-portal/freshservice-api-key"
)
FRESHSERVICE_WEBHOOK_HMAC_SECRET_NAME = os.environ.get(
    "FRESHSERVICE_WEBHOOK_HMAC_SECRET_NAME", "job-portal/freshservice-webhook-hmac-secret"
)

WEBHOOK_IDEMPOTENCY_TTL_SECONDS = 300


class WebhookSignatureError(Exception):
    """Raised when the inbound webhook's HMAC signature does not match."""


class WebhookPayloadError(Exception):
    """Raised for malformed JSON or missing required fields (maps to 400)."""


class WebhookMappingError(Exception):
    """Raised when a category/status value has no configured mapping (maps to 422)."""


def _get_secret(secret_name: str) -> str:
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]


def _verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Verify the inbound webhook using a static shared API key sent by
    Freshservice in a custom header (Freshservice's Workflow Automator
    "Trigger Webhook" action supports Basic Auth / a static API key
    header, but not a per-request HMAC signature — see module docstring).

    `raw_body` is accepted for call-site/signature compatibility and for
    the (harmless) possibility of switching back to real HMAC later, but
    is intentionally unused in the static-key comparison itself.
    """
    del raw_body  # unused in static-key mode; kept for interface stability
    expected = _get_secret(FRESHSERVICE_WEBHOOK_HMAC_SECRET_NAME)
    # constant-time compare
    return hmac.compare_digest(expected, signature_header or "")


def _freshservice_status_to_portal(fs_status: str) -> Optional[str]:
    mapping = repo.get_mapping("freshservice_to_status", fs_status)
    if mapping and mapping.get("external_value"):
        return mapping["external_value"]
    return None


def verify_and_handle_webhook(raw_body: bytes, signature_header: str) -> dict:
    """
    Validate an inbound Freshservice webhook and, if it's a new (non-duplicate)
    event, update the linked Job's status.

    Returns a dict describing the outcome (for a 200 JSON response body).
    Raises WebhookSignatureError / WebhookPayloadError / WebhookMappingError
    for the caller (main.py) to map to 401 / 400 / 422 respectively.
    """
    if not _verify_signature(raw_body, signature_header):
        raise WebhookSignatureError("HMAC signature mismatch")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as e:
        raise WebhookPayloadError(f"Invalid JSON body: {e}") from e

    ticket = payload.get("ticket") or payload
    fs_ticket_id = str(ticket.get("id") or ticket.get("ticket_id") or "")
    fs_status = ticket.get("status_name") or ticket.get("status")

    if not fs_ticket_id:
        raise WebhookPayloadError("Missing ticket id in webhook payload")

    event_key = payload.get("event_id") or f"{fs_ticket_id}:{fs_status}:{ticket.get('updated_at', '')}"
    dedup_key = f"fs_webhook:{event_key}"

    is_new = repo.check_and_set_webhook_idempotency(dedup_key, WEBHOOK_IDEMPOTENCY_TTL_SECONDS)
    if not is_new:
        return {"status": "duplicate_ignored", "ticket_id": fs_ticket_id}

    if fs_status is None:
        # Some webhook event types (e.g. note added) don't carry a status
        # change — nothing to sync, but still a valid/handled event.
        return {"status": "no_status_change", "ticket_id": fs_ticket_id}

    portal_status = _freshservice_status_to_portal(str(fs_status))
    if portal_status is None:
        raise WebhookMappingError(f"No status mapping configured for Freshservice status '{fs_status}'")

    job = _find_job_by_freshservice_ticket_id(fs_ticket_id)
    if job is None:
        logger.info("freshservice_sync: webhook for unknown/unlinked ticket %s — ignoring", fs_ticket_id)
        return {"status": "ticket_not_linked", "ticket_id": fs_ticket_id}

    if job["status"] != portal_status:
        repo.update_job_fields(job["id"], {"status": portal_status})
        repo.put_sync_log(
            job["id"],
            f"freshservice_webhook_status_{fs_ticket_id}_{portal_status}",
            {"fs_status": fs_status, "portal_status": portal_status},
        )

    return {"status": "synced", "ticket_id": fs_ticket_id, "job_id": job["id"], "new_status": portal_status}


def _find_job_by_freshservice_ticket_id(fs_ticket_id: str) -> Optional[dict]:
    """
    No GSI exists for freshservice_ticket_id (low cardinality of linked
    jobs makes a GSI unnecessary for this 3-person tool) — scan the small
    AllJobsIndex result set instead. Acceptable given expected table size.
    """
    result = repo.list_jobs(page=1, per_page=1000)
    for job in result["jobs"]:
        if job.get("freshservice_ticket_id") == fs_ticket_id:
            return job
    return None


def poll_freshservice_ticket_updates() -> dict:
    """
    Outbound poll: for every Job with a linked freshservice_ticket_id,
    fetch its current status from Freshservice and sync it into the Portal
    if it changed. Uses the WebhookIdempotency-style dedup pattern per-poll
    invocation to avoid double-processing when 2 ECS tasks both run the
    poll close together (Requirement 7.5).
    """
    api_key = _get_secret(FRESHSERVICE_API_KEY_SECRET_NAME)
    result = repo.list_jobs(page=1, per_page=1000)
    linked_jobs = [j for j in result["jobs"] if j.get("freshservice_ticket_id")]

    synced = 0
    checked = 0
    errors = 0

    for job in linked_jobs:
        fs_ticket_id = job["freshservice_ticket_id"]
        checked += 1
        try:
            resp = httpx.get(
                f"https://{FRESHSERVICE_DOMAIN}/api/v2/tickets/{fs_ticket_id}",
                auth=(api_key, "X"),
                timeout=15.0,
            )
            resp.raise_for_status()
            ticket = resp.json()["ticket"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            logger.warning("freshservice_sync: poll failed for ticket %s: %s", fs_ticket_id, e)
            errors += 1
            continue

        fs_status = ticket.get("status_name") or ticket.get("status")
        if fs_status is None:
            continue

        portal_status = _freshservice_status_to_portal(str(fs_status))
        if portal_status is None or portal_status == job["status"]:
            continue

        dedup_key = f"fs_poll:{fs_ticket_id}:{portal_status}"
        if not repo.check_and_set_webhook_idempotency(dedup_key, ttl_seconds=290):
            # Another concurrent poller (2nd ECS task) already handled this
            # exact transition within the last ~5 minutes.
            continue

        repo.update_job_fields(job["id"], {"status": portal_status})
        repo.put_sync_log(
            job["id"],
            f"freshservice_poll_status_{fs_ticket_id}_{portal_status}",
            {"fs_status": fs_status, "portal_status": portal_status},
        )
        synced += 1

    return {"checked": checked, "synced": synced, "errors": errors}
