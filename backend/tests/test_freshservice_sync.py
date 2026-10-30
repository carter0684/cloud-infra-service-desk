"""
Tests for app/freshservice_sync.py — inbound webhook handler.

Covers: valid static API key header, invalid/missing key (->
WebhookSignatureError, maps to 401 in main.py), malformed JSON (->
WebhookPayloadError, maps to 400), unmapped category/status (->
WebhookMappingError, maps to 422), and duplicate event (-> 200 no-op via
idempotency dedup).

NOTE: verification uses a static shared API key (not a per-request HMAC
signature) because Freshservice's Workflow Automator "Trigger Webhook"
action can only send Basic Auth / a fixed "API key" header, not compute an
HMAC over the outgoing body. See freshservice_sync.py module docstring.
"""

from __future__ import annotations

import importlib
import json

import boto3
import pytest


FAKE_API_KEY = "test-webhook-api-key"


@pytest.fixture
def fs_sync_module(dynamodb_tables):
    from app import freshservice_sync as fs_sync_module
    importlib.reload(fs_sync_module)
    return fs_sync_module


@pytest.fixture
def secretsmanager_with_hmac_secret(dynamodb_tables):
    client = boto3.client("secretsmanager", region_name="us-east-1")
    client.create_secret(
        Name="job-portal/freshservice-webhook-hmac-secret",
        SecretString=FAKE_API_KEY,
    )
    return client


class TestValidSignature:
    def test_valid_signature_with_no_status_change_returns_no_status_change(
        self, fs_sync_module, secretsmanager_with_hmac_secret,
    ):
        payload = {"ticket": {"id": 100}}  # no status field -> note-added-style event
        body = json.dumps(payload).encode("utf-8")
        result = fs_sync_module.verify_and_handle_webhook(body, FAKE_API_KEY)
        assert result["status"] == "no_status_change"
        assert result["ticket_id"] == "100"

    def test_valid_signature_unlinked_ticket_returns_ticket_not_linked(
        self, fs_sync_module, secretsmanager_with_hmac_secret,
    ):
        from app import dynamo_repository as repo
        repo.put_mapping("freshservice_to_status", "Open", "Open")

        payload = {"ticket": {"id": 200, "status_name": "Open"}}
        body = json.dumps(payload).encode("utf-8")
        result = fs_sync_module.verify_and_handle_webhook(body, FAKE_API_KEY)
        assert result["status"] == "ticket_not_linked"

    def test_valid_signature_linked_ticket_syncs_status(
        self, fs_sync_module, secretsmanager_with_hmac_secret,
    ):
        from app import dynamo_repository as repo
        repo.put_mapping("freshservice_to_status", "Closed", "Closed")
        job = repo.create_job(
            title="t", description="d", category="Networking", category_other=None,
            priority="High", assignee_email="admin.two@example.com",
            assignee_name="Admin Two", created_by_email="admin.one@example.com",
            created_by_name="Admin One", due_date=None, audit_log=[],
        )
        repo.update_job_fields(job["id"], {"freshservice_ticket_id": "300", "status": "In Progress"})

        payload = {"ticket": {"id": 300, "status_name": "Closed"}}
        body = json.dumps(payload).encode("utf-8")
        result = fs_sync_module.verify_and_handle_webhook(body, FAKE_API_KEY)

        assert result["status"] == "synced"
        assert result["new_status"] == "Closed"
        refetched = repo.get_job(job["id"])
        assert refetched["status"] == "Closed"


class TestInvalidSignature:
    def test_invalid_signature_raises_webhook_signature_error(
        self, fs_sync_module, secretsmanager_with_hmac_secret,
    ):
        payload = {"ticket": {"id": 100, "status_name": "Open"}}
        body = json.dumps(payload).encode("utf-8")
        with pytest.raises(fs_sync_module.WebhookSignatureError):
            fs_sync_module.verify_and_handle_webhook(body, "clearly-wrong-api-key")

    def test_missing_signature_header_raises_webhook_signature_error(
        self, fs_sync_module, secretsmanager_with_hmac_secret,
    ):
        payload = {"ticket": {"id": 100, "status_name": "Open"}}
        body = json.dumps(payload).encode("utf-8")
        with pytest.raises(fs_sync_module.WebhookSignatureError):
            fs_sync_module.verify_and_handle_webhook(body, "")


class TestMalformedJson:
    def test_malformed_json_raises_webhook_payload_error(
        self, fs_sync_module, secretsmanager_with_hmac_secret,
    ):
        body = b"{not valid json at all"
        with pytest.raises(fs_sync_module.WebhookPayloadError):
            fs_sync_module.verify_and_handle_webhook(body, FAKE_API_KEY)

    def test_valid_json_missing_ticket_id_raises_webhook_payload_error(
        self, fs_sync_module, secretsmanager_with_hmac_secret,
    ):
        payload = {"ticket": {"status_name": "Open"}}  # no id at all
        body = json.dumps(payload).encode("utf-8")
        with pytest.raises(fs_sync_module.WebhookPayloadError):
            fs_sync_module.verify_and_handle_webhook(body, FAKE_API_KEY)


class TestUnmappedCategoryOrStatus:
    def test_unmapped_status_raises_webhook_mapping_error(
        self, fs_sync_module, secretsmanager_with_hmac_secret,
    ):
        # Deliberately do NOT put_mapping() for this status.
        payload = {"ticket": {"id": 400, "status_name": "SomeUnmappedStatus"}}
        body = json.dumps(payload).encode("utf-8")
        with pytest.raises(fs_sync_module.WebhookMappingError):
            fs_sync_module.verify_and_handle_webhook(body, FAKE_API_KEY)


class TestDuplicateEvent:
    def test_duplicate_event_is_a_no_op_on_second_call(
        self, fs_sync_module, secretsmanager_with_hmac_secret,
    ):
        from app import dynamo_repository as repo
        repo.put_mapping("freshservice_to_status", "Open", "Open")

        payload = {"ticket": {"id": 500, "status_name": "Open", "event_id": "evt-fixed-123"}}
        body = json.dumps(payload).encode("utf-8")

        first = fs_sync_module.verify_and_handle_webhook(body, FAKE_API_KEY)
        assert first["status"] == "ticket_not_linked"  # first call processes normally

        second = fs_sync_module.verify_and_handle_webhook(body, FAKE_API_KEY)
        assert second["status"] == "duplicate_ignored"
        assert second["ticket_id"] == "500"
