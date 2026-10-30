"""
Tests for app/dynamo_repository.py using moto's DynamoDB mock.

Covers: create/read/update, GSI queries (AssigneeIndex, AllJobsIndex), and
the round-trip serialization property from Requirement 12.4 — serialising
a value into DynamoDB and reading it back must produce an identical value
and type (no int->float coercion, no numeric-to-string leakage).
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def repo(dynamodb_tables):
    """Import dynamo_repository fresh per-test so its module-level table
    name constants pick up the moto-mocked region/credentials cleanly."""
    from app import dynamo_repository as repo_module
    importlib.reload(repo_module)
    return repo_module


def _make_job(repo, **overrides):
    defaults = dict(
        title="Test job",
        description="A test job description",
        category="Networking",
        category_other=None,
        priority="High",
        assignee_email="admin.two@example.com",
        assignee_name="Admin Two",
        created_by_email="admin.one@example.com",
        created_by_name="Admin One",
        due_date=None,
        audit_log=[{"timestamp": "2026-01-01T00:00:00", "actor": "Admin One",
                     "actor_email": "admin.one@example.com", "event": "created"}],
    )
    defaults.update(overrides)
    return repo.create_job(**defaults)


class TestJobCreateReadUpdate:
    def test_create_job_returns_expected_shape(self, repo):
        job = _make_job(repo)
        assert job["id"]
        assert job["title"] == "Test job"
        assert job["status"] == "Open"
        assert job["assignee_email"] == "admin.two@example.com"
        assert job["notes"] == []
        assert job["ai_draft"] is None

    def test_get_job_returns_created_job(self, repo):
        created = _make_job(repo)
        fetched = repo.get_job(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["title"] == created["title"]

    def test_get_job_missing_returns_none(self, repo):
        assert repo.get_job("does-not-exist") is None

    def test_update_job_fields_persists_and_returns_new_state(self, repo):
        job = _make_job(repo)
        updated = repo.update_job_fields(job["id"], {"status": "Pending Review", "title": "Renamed"})
        assert updated["status"] == "Pending Review"
        assert updated["title"] == "Renamed"

        refetched = repo.get_job(job["id"])
        assert refetched["status"] == "Pending Review"
        assert refetched["title"] == "Renamed"

    def test_update_job_fields_bumps_updated_at(self, repo):
        job = _make_job(repo)
        original_updated_at = job["updated_at"]
        updated = repo.update_job_fields(job["id"], {"status": "Pending Review"})
        assert updated["updated_at"] != original_updated_at

    def test_update_job_fields_no_fields_returns_current_state(self, repo):
        job = _make_job(repo)
        result = repo.update_job_fields(job["id"], {})
        assert result["id"] == job["id"]

    def test_delete_job_removes_it(self, repo):
        job = _make_job(repo)
        repo.delete_job(job["id"])
        assert repo.get_job(job["id"]) is None


class TestJobListingAndGSIs:
    def test_list_jobs_all_jobs_index_returns_all(self, repo):
        _make_job(repo, title="Job A")
        _make_job(repo, title="Job B")
        _make_job(repo, title="Job C")
        result = repo.list_jobs(page=1, per_page=50)
        assert result["total"] == 3
        assert len(result["jobs"]) == 3

    def test_list_jobs_single_assignee_uses_assignee_index(self, repo):
        _make_job(repo, title="Admin Two's job", assignee_email="admin.two@example.com")
        _make_job(repo, title="Admin Three's job", assignee_email="admin.three@example.com")
        result = repo.list_jobs(assignee=["admin.two@example.com"])
        assert result["total"] == 1
        assert result["jobs"][0]["title"] == "Admin Two's job"

    def test_list_jobs_filters_by_status(self, repo):
        j1 = _make_job(repo, title="Open job")
        j2 = _make_job(repo, title="In progress job")
        repo.update_job_fields(j2["id"], {"status": "In Progress"})

        result = repo.list_jobs(status=["In Progress"])
        assert result["total"] == 1
        assert result["jobs"][0]["title"] == "In progress job"

    def test_list_jobs_filters_by_priority_and_category(self, repo):
        _make_job(repo, title="High net", priority="High", category="Networking")
        _make_job(repo, title="Low storage", priority="Low", category="Storage")

        result = repo.list_jobs(priority=["High"], category=["Networking"])
        assert result["total"] == 1
        assert result["jobs"][0]["title"] == "High net"

    def test_list_jobs_text_search_matches_title_and_description(self, repo):
        _make_job(repo, title="Firewall outage", description="something else")
        _make_job(repo, title="Unrelated", description="mentions firewall here")
        _make_job(repo, title="Totally different", description="nothing relevant")

        result = repo.list_jobs(q="firewall")
        assert result["total"] == 2

    def test_list_jobs_pagination(self, repo):
        for i in range(5):
            _make_job(repo, title=f"Job {i}")

        page1 = repo.list_jobs(page=1, per_page=2)
        page2 = repo.list_jobs(page=2, per_page=2)
        assert page1["total"] == 5
        assert len(page1["jobs"]) == 2
        assert len(page2["jobs"]) == 2
        # Different pages should not return the same job ids
        page1_ids = {j["id"] for j in page1["jobs"]}
        page2_ids = {j["id"] for j in page2["jobs"]}
        assert page1_ids.isdisjoint(page2_ids)


class TestKBDocuments:
    def test_create_and_list_kb_document(self, repo):
        doc = repo.create_kb_document(
            filename="runbook.pdf",
            file_type="pdf",
            description="Test runbook",
            char_count=100,
            chunk_count=1,
            chunks=[{"chunk_id": 0, "text": "some runbook content"}],
            uploaded_by="admin.one@example.com",
        )
        assert doc["id"]
        assert doc["filename"] == "runbook.pdf"

        docs = repo.list_kb_documents()
        assert len(docs) == 1
        assert docs[0]["id"] == doc["id"]

    def test_get_kb_document_missing_returns_none(self, repo):
        assert repo.get_kb_document("nonexistent") is None

    def test_delete_kb_document(self, repo):
        doc = repo.create_kb_document(
            filename="temp.txt", file_type="txt", description=None,
            char_count=10, chunk_count=1, chunks=[{"chunk_id": 0, "text": "x"}],
            uploaded_by="admin.one@example.com",
        )
        repo.delete_kb_document(doc["id"])
        assert repo.get_kb_document(doc["id"]) is None


class TestMappings:
    def test_put_get_list_delete_mapping_roundtrip(self, repo):
        repo.put_mapping("category_to_freshservice", "Networking", "Network")
        fetched = repo.get_mapping("category_to_freshservice", "Networking")
        assert fetched["external_value"] == "Network"

        repo.put_mapping("category_to_freshservice", "Compute", "Servers")
        all_mappings = repo.list_mappings("category_to_freshservice")
        assert len(all_mappings) == 2

        repo.delete_mapping("category_to_freshservice", "Networking")
        assert repo.get_mapping("category_to_freshservice", "Networking") is None
        assert len(repo.list_mappings("category_to_freshservice")) == 1


class TestSyncLog:
    def test_put_and_list_sync_log(self, repo):
        job = _make_job(repo)
        repo.put_sync_log(job["id"], "event1", {"info": "test"})
        repo.put_sync_log(job["id"], "event2", {"info": "test2"})
        entries = repo.list_sync_log(job["id"])
        assert len(entries) == 2


class TestIdempotencyDedup:
    def test_webhook_idempotency_first_call_is_new(self, repo):
        assert repo.check_and_set_webhook_idempotency("key1") is True

    def test_webhook_idempotency_second_call_is_duplicate(self, repo):
        repo.check_and_set_webhook_idempotency("key1")
        assert repo.check_and_set_webhook_idempotency("key1") is False

    def test_notification_dedup_first_call_is_new(self, repo):
        assert repo.check_and_set_notification_dedup("notify1") is True

    def test_notification_dedup_second_call_is_duplicate(self, repo):
        repo.check_and_set_notification_dedup("notify1")
        assert repo.check_and_set_notification_dedup("notify1") is False

    def test_different_keys_are_independent(self, repo):
        assert repo.check_and_set_webhook_idempotency("keyA") is True
        assert repo.check_and_set_webhook_idempotency("keyB") is True


class TestNumericRoundTripFidelity:
    """
    Requirement 12.4: serialize -> deserialize must produce identical field
    values AND types. DynamoDB stores all numbers as Decimal internally;
    dynamo_repository.py's _to_dynamo()/_from_dynamo() must convert
    transparently so callers never see a Decimal, and ints/floats must not
    get coerced into each other or into strings.
    """

    def test_integer_field_round_trips_as_int(self, repo):
        doc = repo.create_kb_document(
            filename="f.txt", file_type="txt", description=None,
            char_count=12345, chunk_count=3,
            chunks=[{"chunk_id": 0, "text": "a"}, {"chunk_id": 1, "text": "b"}, {"chunk_id": 2, "text": "c"}],
            uploaded_by="admin.one@example.com",
        )
        fetched = repo.get_kb_document(doc["id"])
        assert fetched["char_count"] == 12345
        assert isinstance(fetched["char_count"], int)
        assert fetched["chunk_count"] == 3
        assert isinstance(fetched["chunk_count"], int)

    def test_float_value_in_nested_structure_round_trips_as_float(self, repo):
        job = _make_job(repo)
        # ai_draft is an arbitrary nested dict — exercise a float value inside it
        draft = {"confidence_score": 0.87, "severity": "high", "step_count": 4}
        updated = repo.update_job_fields(job["id"], {"ai_draft": draft})
        assert isinstance(updated["ai_draft"]["confidence_score"], float)
        assert updated["ai_draft"]["confidence_score"] == 0.87
        assert isinstance(updated["ai_draft"]["step_count"], int)
        assert updated["ai_draft"]["step_count"] == 4

        refetched = repo.get_job(job["id"])
        assert isinstance(refetched["ai_draft"]["confidence_score"], float)
        assert refetched["ai_draft"]["confidence_score"] == 0.87
        assert isinstance(refetched["ai_draft"]["step_count"], int)

    def test_zero_and_negative_numbers_round_trip_correctly(self, repo):
        job = _make_job(repo)
        updated = repo.update_job_fields(job["id"], {"ai_draft": {"delta": -3, "ratio": -0.5, "zero": 0}})
        assert updated["ai_draft"]["delta"] == -3 and isinstance(updated["ai_draft"]["delta"], int)
        assert updated["ai_draft"]["ratio"] == -0.5 and isinstance(updated["ai_draft"]["ratio"], float)
        assert updated["ai_draft"]["zero"] == 0 and isinstance(updated["ai_draft"]["zero"], int)

    def test_numbers_in_list_round_trip_correctly(self, repo):
        job = _make_job(repo)
        updated = repo.update_job_fields(job["id"], {"ai_draft": {"scores": [1, 2.5, 3, -1.25]}})
        scores = updated["ai_draft"]["scores"]
        assert scores == [1, 2.5, 3, -1.25]
        assert isinstance(scores[0], int)
        assert isinstance(scores[1], float)
        assert isinstance(scores[2], int)
        assert isinstance(scores[3], float)
