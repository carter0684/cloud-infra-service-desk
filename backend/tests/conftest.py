"""
Shared pytest fixtures for the Cloud Infra Job Portal backend test suite.

All AWS calls in these tests are mocked via `moto` — no real AWS credentials
or network access are used or required. Table schemas mirror
job-portal/infra-cdk/infra_cdk/stacks/data_stack.py exactly so tests catch
schema drift between the CDK stack and dynamo_repository.py.
"""

from __future__ import annotations

import importlib
import os

import boto3
import pytest
from moto import mock_aws

# Must be set before dynamo_repository/notifications/escalation/freshservice_sync
# are imported anywhere, since they read AWS_REGION at import time via
# os.environ.get(...) module-level constants.
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
# Belt-and-suspenders: make sure no test can ever reach real AWS via the
# EC2/ECS instance-metadata credential provider (which boto3 falls back to
# when no explicit/env credentials are found), and force dummy values even
# if a developer's shell already exports real AWS_* env vars.
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"

# ── Organisation-level configuration used by the test-suite ─────────────────
# The application reads everything about the deploying organisation from
# environment variables (see app/org_config.py). The tests use these fictional
# values; they must be set BEFORE any `app.*` module is imported.
_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
_TEST_ORG_ENV = {
    "ORG_NAME": "Example Corp",
    "PORTAL_DOMAIN": "portal.example.com",
    "API_DOMAIN": "portal-api.example.com",
    "ORG_EMAIL_DOMAIN": "example.com",
    "SEED_ADMIN_EMAILS": "admin.one@example.com,admin.two@example.com,admin.three@example.com",
    "PORTAL_FOUNDER_EMAILS": "admin.one@example.com",
    "SEED_USER_NAMES": "admin.one@example.com=Admin One,admin.two@example.com=Admin Two,"
                       "admin.three@example.com=Admin Three,member.one@example.com=Member One,"
                       "member.two@example.com=Member Two,member.three@example.com=Member Three,"
                       "member.four@example.com=Member Four",
    "SES_SENDER_ADDRESS": "admin.one@example.com",
    "PORTAL_HOST_ACCOUNT_ID": "100000000002",
    "CORE_ACCOUNT_ID": "100000000001",
    "KNOWN_BUSINESS_UNITS": "BU-A,BU-B,BU-C,BU-D",
    "CLOUD_ACCOUNT_SEED_FILE": os.path.join(_FIXTURES_DIR, "sample_accounts.json"),
    "FRESHSERVICE_DOMAIN": "example.freshservice.com",
    "S2S_VPN_ACCOUNT_ID": "100000000003",
    "VPN_LOOKUP_ACCOUNT_ID": "100000000003",
    "WHITELIST_LMS_ACCOUNT_ID": "100000000004",
    "SCREENSAVER_BUCKET_NAME": "screensaver-100000000001",
    "SCREENSAVER_CLOUDFRONT_DOMAIN": "screensaver.example.com",
    "SCREENSAVER_CLOUDFRONT_DISTRIBUTION_ID": "EXAMPLEDIST123",
    "RESOURCE_INVENTORY_BUCKET": "example-resource-inventory",
    "MERAKI_ORG_ID": "123456",
    "S2S_VPN_TGW_CONFIG": (
        '{"us-east-1": {"name": "TGW-US", "id": "tgw-00000000000000001", "route_table_name": "TGW-US-RT", "route_table_id": "tgw-rtb-00000000000000001"},'
        ' "ap-southeast-1": {"name": "TGW-SG", "id": "tgw-00000000000000002", "route_table_name": "TGW-SG-RT", "route_table_id": "tgw-rtb-00000000000000002"},'
        ' "eu-west-2": {"name": "TGW-UK", "id": "tgw-00000000000000003", "route_table_name": "TGW-UK-RT", "route_table_id": "tgw-rtb-00000000000000003"},'
        ' "ap-south-1": {"name": "TGW-IN", "id": "tgw-00000000000000004", "route_table_name": "TGW-IN-RT", "route_table_id": "tgw-rtb-00000000000000004"}}'
    ),
    "PATCHING_DEFAULT_TEAMS_MEETINGS": (
        '[{"id": "webapp", "name": "WebApp", "url": "https://teams.microsoft.com/l/meetup-join/19%3ameeting_SEEDLINKONE%40thread.v2/0?context=%7b%22Tid%22%3a%22tenant-id%22%7d"},'
        ' {"id": "lms", "name": "LMS", "url": "https://teams.microsoft.com/l/meetup-join/19%3ameeting_SEEDLINKTWO%40thread.v2/0?context=%7b%22Tid%22%3a%22tenant-id%22%7d"},'
        ' {"id": "ibe", "name": "IBE", "url": "https://teams.microsoft.com/l/meetup-join/19%3ameeting_SEEDLINKTHREE%40thread.v2/0?context=%7b%22Tid%22%3a%22tenant-id%22%7d"}]'
    ),
}
for _k, _v in _TEST_ORG_ENV.items():
    os.environ.setdefault(_k, _v)

AWS_REGION = "us-east-1"

JOBS_TABLE = "job-portal-jobs"
KB_DOCUMENTS_TABLE = "job-portal-kb-documents"
MAPPINGS_TABLE = "job-portal-mappings"
SYNC_LOG_TABLE = "job-portal-sync-log"
WEBHOOK_IDEMPOTENCY_TABLE = "job-portal-webhook-idempotency"
NOTIFICATION_DEDUP_TABLE = "job-portal-notification-dedup"

# Chat/Call feature (job-portal-chat-voice spec) tables — mirror
# data_stack.py's ChatConversationsTable/ChatParticipantsTable/
# ChatMessagesTable/CallSessionsTable/WsConnectionsTable exactly.
CHAT_CONVERSATIONS_TABLE = "job-portal-chat-conversations"
CHAT_PARTICIPANTS_TABLE = "job-portal-chat-participants"
CHAT_MESSAGES_TABLE = "job-portal-chat-messages"
CALL_SESSIONS_TABLE = "job-portal-call-sessions"
WS_CONNECTIONS_TABLE = "job-portal-ws-connections"


@pytest.fixture
def aws_credentials(monkeypatch):
    """Ensure boto3 never accidentally reaches real AWS during tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)


@pytest.fixture
def dynamodb_tables(aws_credentials):
    """
    Spin up moto-mocked DynamoDB and create all 6 Portal tables with the
    same key schema + GSIs as data_stack.py. Yields the boto3 resource.
    """
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=AWS_REGION)

        ddb.create_table(
            TableName=JOBS_TABLE,
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "job_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "assignee_email", "AttributeType": "S"},
                {"AttributeName": "entity_type", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "StatusIndex",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "AssigneeIndex",
                    "KeySchema": [
                        {"AttributeName": "assignee_email", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "AllJobsIndex",
                    "KeySchema": [
                        {"AttributeName": "entity_type", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        ddb.create_table(
            TableName=KB_DOCUMENTS_TABLE,
            KeySchema=[{"AttributeName": "doc_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "doc_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        ddb.create_table(
            TableName=MAPPINGS_TABLE,
            KeySchema=[
                {"AttributeName": "mapping_type", "KeyType": "HASH"},
                {"AttributeName": "portal_value", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "mapping_type", "AttributeType": "S"},
                {"AttributeName": "portal_value", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        ddb.create_table(
            TableName=SYNC_LOG_TABLE,
            KeySchema=[
                {"AttributeName": "job_id", "KeyType": "HASH"},
                {"AttributeName": "event_key", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "job_id", "AttributeType": "S"},
                {"AttributeName": "event_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        ddb.create_table(
            TableName=WEBHOOK_IDEMPOTENCY_TABLE,
            KeySchema=[{"AttributeName": "dedup_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "dedup_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        ddb.create_table(
            TableName=NOTIFICATION_DEDUP_TABLE,
            KeySchema=[{"AttributeName": "dedup_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "dedup_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        yield ddb


@pytest.fixture
def chat_tables(aws_credentials, monkeypatch):
    """
    Spin up moto-mocked DynamoDB with the 5 Chat/Call feature tables
    (job-portal-chat-voice spec), mirroring data_stack.py's schema
    exactly (partition/sort keys, GSIs, TTL attribute names). Also sets
    ENV=production so chat_store.py/call_store.py/ws_connections.py
    import their DynamoDB branch rather than the local SQLite branch —
    these modules select their code path at import time based on
    os.environ.get("ENV", "local"), so ENV must be set BEFORE the module
    under test is first imported in a given test file (see each test
    file's own note on import ordering).
    """
    monkeypatch.setenv("ENV", "production")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=AWS_REGION)

        ddb.create_table(
            TableName=CHAT_CONVERSATIONS_TABLE,
            KeySchema=[{"AttributeName": "conversation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "conversation_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        ddb.create_table(
            TableName=CHAT_PARTICIPANTS_TABLE,
            KeySchema=[
                {"AttributeName": "user_email", "KeyType": "HASH"},
                {"AttributeName": "conversation_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_email", "AttributeType": "S"},
                {"AttributeName": "conversation_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        ddb.create_table(
            TableName=CHAT_MESSAGES_TABLE,
            KeySchema=[
                {"AttributeName": "conversation_id", "KeyType": "HASH"},
                {"AttributeName": "message_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "conversation_id", "AttributeType": "S"},
                {"AttributeName": "message_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
        )

        ddb.create_table(
            TableName=CALL_SESSIONS_TABLE,
            KeySchema=[{"AttributeName": "call_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "call_id", "AttributeType": "S"},
                {"AttributeName": "conversation_id", "AttributeType": "S"},
                {"AttributeName": "started_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "ConversationCallHistoryIndex",
                    "KeySchema": [
                        {"AttributeName": "conversation_id", "KeyType": "HASH"},
                        {"AttributeName": "started_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
            StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
        )

        ddb.create_table(
            TableName=WS_CONNECTIONS_TABLE,
            KeySchema=[{"AttributeName": "connection_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "connection_id", "AttributeType": "S"},
                {"AttributeName": "user_email", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "UserEmailIndex",
                    "KeySchema": [{"AttributeName": "user_email", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        yield ddb


# job-portal-chat-voice Task 9.6: shared fixtures for the REST-route
# integration tests (test_chat_routes.py / test_call_routes.py) that
# exercise main.py's Chat/Call endpoints end-to-end via FastAPI's
# TestClient.
CHAT_ROUTES_KNOWN_USERS = {
    "alice@example.com",
    "bob@example.com",
    "carol@example.com",
    "dave@example.com",
}
CHAT_ATTACHMENTS_BUCKET_NAME = "job-portal-chat-attachments-100000000002"


@pytest.fixture
def chat_app(chat_tables, monkeypatch):
    """Reload chat_store/call_store/chat_attachments (production
    DynamoDB branch, since chat_tables sets ENV=production) THEN main.py
    itself — main.py's `from . import chat_store` (etc.) bindings at its
    own top level are frozen to whichever module object existed the
    last time main.py itself was imported/reloaded, same reload-
    ordering constraint every other *_store.py test fixture in this
    suite already follows (see test_call_sweep.py's note on call_sweep
    needing call_store reloaded first).

    Also overrides main.get_actor via FastAPI's dependency_overrides so
    tests can keep using the familiar X-Actor-Email header instead of a
    real ALB-injected OIDC token — ENV=production (required so
    chat_store/call_store pick their DynamoDB branch) would otherwise
    swap main.get_actor to the real auth_alb.get_actor, which these
    tests have no way to satisfy.

    user_store.is_allowed() is monkeypatched directly (no dedicated
    users-table moto fixture) to a small fixed roster — same approach as
    test_users_directory.py — since these tests only need a small,
    known-good set of emails, not the full user_store persistence layer.
    """
    monkeypatch.setenv("CHAT_ATTACHMENTS_BUCKET_NAME", CHAT_ATTACHMENTS_BUCKET_NAME)

    from app import chat_store as chat_store_module
    from app import call_store as call_store_module
    from app import chat_attachments as chat_attachments_module
    from app import user_store

    importlib.reload(chat_store_module)
    importlib.reload(call_store_module)
    importlib.reload(chat_attachments_module)

    monkeypatch.setattr(user_store, "is_allowed", lambda email: email.lower().strip() in CHAT_ROUTES_KNOWN_USERS)

    from app import main as main_module
    importlib.reload(main_module)

    from fastapi import Header, HTTPException

    def _get_actor_via_header(x_actor_email: str = Header(default=None)) -> str:
        if not x_actor_email or x_actor_email.lower().strip() not in CHAT_ROUTES_KNOWN_USERS:
            raise HTTPException(401, "Unauthorised — invalid or missing X-Actor-Email header")
        return x_actor_email

    main_module.app.dependency_overrides[main_module.get_actor] = _get_actor_via_header
    return main_module


@pytest.fixture
def chat_client(chat_app):
    from fastapi.testclient import TestClient
    return TestClient(chat_app.app)


@pytest.fixture(autouse=True)
def _reset_cost_explorer_cache():
    """Autouse: clear cost_explorer.py's process-local cache before and
    after every test so no test can observe another test's cached Cost
    Explorer summary (they run in the same process/module)."""
    from app import cost_explorer

    cost_explorer.reset_cache_for_tests()
    yield
    cost_explorer.reset_cache_for_tests()
