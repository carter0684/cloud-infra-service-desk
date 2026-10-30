"""
Tests for app/escalation.py — Freshservice and AWS Support integrations.

- raise_aws_support_case(): uses moto's Support mock (real boto3 call
  against a fake AWS backend, no network access).
- raise_freshservice_ticket(): Freshservice has no moto mock (it's not an
  AWS service) — its httpx.post call is mocked directly, and Secrets
  Manager (for the API key) is covered by moto.

Covers success, timeout, and API-error paths for both integrations.
"""

from __future__ import annotations

import importlib

import boto3
import httpx
import pytest
from moto import mock_aws


@pytest.fixture
def escalation_module(dynamodb_tables):
    """dynamodb_tables gives us moto's mock_aws context (for the Mappings/
    SyncLog tables escalation.py reads/writes) already active; reload the
    module fresh so its module-level constants pick up test env vars."""
    from app import escalation as escalation_module
    importlib.reload(escalation_module)
    return escalation_module


@pytest.fixture
def secretsmanager_with_freshservice_key(dynamodb_tables):
    """moto is already active via dynamodb_tables' mock_aws() context —
    just create the secret escalation.py expects to read."""
    client = boto3.client("secretsmanager", region_name="us-east-1")
    client.create_secret(
        Name="job-portal/freshservice-api-key",
        SecretString="fake-fs-api-key-for-testing",
    )
    return client


class TestAmsOrganizationHealthEvents:
    """Tests for list_ams_organization_health_events() — the data source
    behind the AWS Health Dashboard. Covers the bug where events past a
    hardcoded page-500 cutoff were silently dropped (fixed 2026-08-05)."""

    def _fake_client(self, monkeypatch, escalation_module, org_pages, describe_events_pages=None):
        """Build a fake boto3 'health' client whose describe_events_for_organization
        (and optionally describe_events) return the given list-of-pages in
        sequence. Each page is (events_list, next_token_or_None). A page
        that is an Exception instance is raised instead of returned."""
        org_iter = iter(org_pages)
        de_iter = iter(describe_events_pages or [])

        class _FakeHealthClient:
            def describe_events_for_organization(self, **kwargs):
                page = next(org_iter)
                if isinstance(page, Exception):
                    raise page
                events, next_token = page
                resp = {"events": events}
                if next_token:
                    resp["nextToken"] = next_token
                return resp

            def describe_events(self, **kwargs):
                page = next(de_iter)
                if isinstance(page, Exception):
                    raise page
                events, next_token = page
                resp = {"events": events}
                if next_token:
                    resp["nextToken"] = next_token
                return resp

        monkeypatch.setattr(escalation_module, "_get_ams_health_client", lambda: _FakeHealthClient())

    def test_pages_past_the_old_500_event_cutoff(self, escalation_module, monkeypatch):
        """Regression test: previously the pagination loop stopped as soon
        as len(events) >= 500, even with a nextToken still available. An
        org with >500 events must now get ALL of them back, including
        ones from later pages (e.g. a real Lambda EOL 'Action Required'
        event that used to sit past index 500)."""
        # 6 pages of 100 events each = 600 events total, all with distinct
        # arns so we can prove the tail-end event survives.
        pages = []
        for page_num in range(6):
            events = [{"arn": f"arn:aws:health:us-east-1::event/E/T/{page_num}-{i}"} for i in range(100)]
            next_token = f"token-{page_num + 1}" if page_num < 5 else None
            pages.append((events, next_token))
        self._fake_client(monkeypatch, escalation_module, pages)

        result = escalation_module.list_ams_organization_health_events()

        assert len(result) == 600
        # The event from the LAST page (would have been dropped by the
        # old >= 500 cutoff) must be present.
        assert any(e["arn"] == "arn:aws:health:us-east-1::event/E/T/5-99" for e in result)

    def test_partial_org_failure_returns_events_fetched_so_far_no_fallback(self, escalation_module, monkeypatch):
        """If describe_events_for_organization succeeds for a few pages
        then fails, the events already fetched must be returned as-is —
        NOT discarded in favor of the much-smaller account-level
        describe_events() fallback (that fallback is only for when the
        org-wide call fails before fetching anything)."""
        page1_events = [{"arn": "arn:aws:health:us-east-1::event/E/T/page1-a"}]
        pages = [
            (page1_events, "token-2"),
            RuntimeError("simulated Health API throttling mid-pagination"),
        ]
        # Fallback client should never be called for this scenario — leave
        # describe_events_pages empty so calling it would raise StopIteration.
        self._fake_client(monkeypatch, escalation_module, pages, describe_events_pages=[])

        result = escalation_module.list_ams_organization_health_events()

        assert result == page1_events

    def test_complete_org_failure_falls_back_to_account_level_describe_events(self, escalation_module, monkeypatch):
        """If describe_events_for_organization fails on the very first
        call (zero events fetched), fall back to the plain account-level
        describe_events() so the page still shows something."""
        fallback_events = [{"arn": "arn:aws:health:us-east-1::event/E/T/fallback-a"}]
        self._fake_client(
            monkeypatch, escalation_module,
            org_pages=[RuntimeError("AccessDeniedException: not the org management account")],
            describe_events_pages=[(fallback_events, None)],
        )

        result = escalation_module.list_ams_organization_health_events()

        assert result == fallback_events

    def test_both_org_and_account_level_calls_fail_raises_escalation_error(self, escalation_module, monkeypatch):
        self._fake_client(
            monkeypatch, escalation_module,
            org_pages=[RuntimeError("org call denied")],
            describe_events_pages=[RuntimeError("account-level call also denied")],
        )

        with pytest.raises(escalation_module.EscalationError) as exc_info:
            escalation_module.list_ams_organization_health_events()
        assert exc_info.value.system == "aws_ams"


class TestAwsSupportEscalation:
    def test_raise_aws_support_case_success(self, escalation_module):
        result = escalation_module.raise_aws_support_case(
            job_id="job-123",
            title="Test escalation",
            description="Something is broken",
            category="Networking",
            priority="High",
        )
        assert "case_id" in result
        assert result["case_id"]

    def test_raise_aws_support_case_logs_sync_log_entry(self, escalation_module):
        result = escalation_module.raise_aws_support_case(
            job_id="job-123",
            title="Test escalation",
            description="Something is broken",
            category="Networking",
            priority="High",
        )
        from app import dynamo_repository as repo
        entries = repo.list_sync_log("job-123")
        assert any(result["case_id"] in e["event_key"] for e in entries)

    def test_raise_aws_support_case_maps_priority_to_severity(self, escalation_module, monkeypatch):
        captured = {}
        client = boto3.client("support", region_name="us-east-1")
        original_create_case = client.create_case

        def _spy_create_case(**kwargs):
            captured.update(kwargs)
            return original_create_case(**kwargs)

        monkeypatch.setattr(
            escalation_module.boto3, "client",
            lambda service, region_name=None: client if service == "support" else boto3.client(service, region_name=region_name),
        )
        client.create_case = _spy_create_case

        escalation_module.raise_aws_support_case(
            job_id="job-456", title="Urgent issue", description="desc",
            category="Security", priority="ASAP",
        )
        assert captured.get("severityCode") == "urgent"

    def test_raise_aws_support_case_unexpected_error_raises_escalation_error(self, escalation_module, monkeypatch):
        class _BrokenClient:
            class exceptions:
                class ClientError(Exception):
                    pass

            def create_case(self, **kwargs):
                raise RuntimeError("simulated transport failure")

        monkeypatch.setattr(escalation_module.boto3, "client", lambda *a, **k: _BrokenClient())

        with pytest.raises(escalation_module.EscalationError) as exc_info:
            escalation_module.raise_aws_support_case(
                job_id="job-789", title="t", description="d", category="Other", priority="Low",
            )
        assert exc_info.value.system == "aws_support"
        # Safe-to-show message, no raw exception text leaked
        assert "simulated transport failure" not in exc_info.value.message


class TestFreshserviceEscalation:
    def test_raise_freshservice_ticket_success(self, escalation_module, secretsmanager_with_freshservice_key, monkeypatch):
        class _FakeResponse:
            status_code = 201
            text = ""

            def json(self):
                return {"ticket": {"id": 42}}

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse())

        result = escalation_module.raise_freshservice_ticket(
            job_id="job-1", title="Ticket title", description="Ticket body",
            category="Networking", priority="High", requester_email="admin.one@example.com",
        )
        assert result["ticket_id"] == "42"
        assert "42" in result["ticket_url"]

    def test_raise_freshservice_ticket_timeout_raises_escalation_error(self, escalation_module, secretsmanager_with_freshservice_key, monkeypatch):
        def _raise_timeout(*a, **k):
            raise httpx.TimeoutException("connect timeout")

        monkeypatch.setattr(httpx, "post", _raise_timeout)

        with pytest.raises(escalation_module.EscalationError) as exc_info:
            escalation_module.raise_freshservice_ticket(
                job_id="job-2", title="t", description="d",
                category="Networking", priority="High", requester_email="admin.one@example.com",
            )
        assert exc_info.value.system == "freshservice"
        assert "did not respond in time" in exc_info.value.message

    def test_raise_freshservice_ticket_api_error_status_raises_escalation_error(self, escalation_module, secretsmanager_with_freshservice_key, monkeypatch):
        class _FakeErrorResponse:
            status_code = 500
            text = "Internal Server Error details that should not leak"

            def json(self):
                return {}

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeErrorResponse())

        with pytest.raises(escalation_module.EscalationError) as exc_info:
            escalation_module.raise_freshservice_ticket(
                job_id="job-3", title="t", description="d",
                category="Networking", priority="High", requester_email="admin.one@example.com",
            )
        assert exc_info.value.system == "freshservice"
        assert "Internal Server Error details" not in exc_info.value.message
        assert "500" in exc_info.value.message

    def test_raise_freshservice_ticket_missing_secret_raises_escalation_error(self, escalation_module):
        # secretsmanager_with_freshservice_key fixture NOT used — the secret
        # doesn't exist, so _get_secret() should fail and be caught.
        with pytest.raises(escalation_module.EscalationError) as exc_info:
            escalation_module.raise_freshservice_ticket(
                job_id="job-4", title="t", description="d",
                category="Networking", priority="High", requester_email="admin.one@example.com",
            )
        assert exc_info.value.system == "freshservice"

    def test_raise_freshservice_ticket_unexpected_response_shape_raises_escalation_error(self, escalation_module, secretsmanager_with_freshservice_key, monkeypatch):
        class _MalformedResponse:
            status_code = 201
            text = ""

            def json(self):
                return {"unexpected": "shape"}  # missing "ticket" key

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _MalformedResponse())

        with pytest.raises(escalation_module.EscalationError) as exc_info:
            escalation_module.raise_freshservice_ticket(
                job_id="job-5", title="t", description="d",
                category="Networking", priority="High", requester_email="admin.one@example.com",
            )
        assert exc_info.value.system == "freshservice"
