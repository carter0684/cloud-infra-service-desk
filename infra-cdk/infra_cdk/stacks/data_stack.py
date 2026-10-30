"""
DataStack — DynamoDB tables (KMS CMK encrypted) for the Cloud Infra Job Portal.
"""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    aws_dynamodb as dynamodb,
    aws_kms as kms,
    aws_s3 as s3,
)
from constructs import Construct

from ..config import CONFIG


class DataStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Single customer-managed key for all Portal tables, annual auto-rotation
        self.cmk = kms.Key(
            self,
            "JobPortalCmk",
            alias="alias/job-portal-cmk",
            description="CMK for Cloud Infra Job Portal DynamoDB tables",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        common_kwargs = dict(
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.cmk,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
        )

        # ── Jobs ──────────────────────────────────────────────────────────
        self.jobs_table = dynamodb.Table(
            self,
            "JobsTable",
            table_name="job-portal-jobs",
            partition_key=dynamodb.Attribute(name="job_id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )
        self.jobs_table.add_global_secondary_index(
            index_name="StatusIndex",
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
        )
        self.jobs_table.add_global_secondary_index(
            index_name="AssigneeIndex",
            partition_key=dynamodb.Attribute(name="assignee_email", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
        )
        self.jobs_table.add_global_secondary_index(
            index_name="AllJobsIndex",
            partition_key=dynamodb.Attribute(name="entity_type", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
        )

        # ── KBDocuments ───────────────────────────────────────────────────
        self.kb_documents_table = dynamodb.Table(
            self,
            "KBDocumentsTable",
            table_name="job-portal-kb-documents",
            partition_key=dynamodb.Attribute(name="doc_id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── Mappings (category/status <-> Freshservice) ──────────────────
        self.mappings_table = dynamodb.Table(
            self,
            "MappingsTable",
            table_name="job-portal-mappings",
            partition_key=dynamodb.Attribute(name="mapping_type", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="portal_value", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── SyncLog (Freshservice sync events, 90-day TTL) ───────────────
        self.sync_log_table = dynamodb.Table(
            self,
            "SyncLogTable",
            table_name="job-portal-sync-log",
            partition_key=dynamodb.Attribute(name="job_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="event_key", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="ttl",
            **common_kwargs,
        )

        # ── WebhookIdempotency (short TTL dedup for inbound webhooks) ────
        self.webhook_idempotency_table = dynamodb.Table(
            self,
            "WebhookIdempotencyTable",
            table_name="job-portal-webhook-idempotency",
            partition_key=dynamodb.Attribute(name="dedup_key", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="ttl",
            **common_kwargs,
        )

        # ── NotificationDedup (5-minute TTL dedup for notifications) ─────
        self.notification_dedup_table = dynamodb.Table(
            self,
            "NotificationDedupTable",
            table_name="job-portal-notification-dedup",
            partition_key=dynamodb.Attribute(name="dedup_key", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="ttl",
            **common_kwargs,
        )

        # ── PortalUsers (allowlist + role, managed via Admin page) ───────
        self.users_table = dynamodb.Table(
            self,
            "PortalUsersTable",
            table_name="job-portal-users",
            partition_key=dynamodb.Attribute(name="email", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── HealthEventChecks (AWS Health Dashboard checkbox acknowledgements) ──
        # One shared checkbox per AWS Health event ARN — records who last
        # ticked/unticked it and when, for the "Checked by X on Y" label
        # on the AWS Health Dashboard page.
        self.health_event_checks_table = dynamodb.Table(
            self,
            "HealthEventChecksTable",
            table_name="job-portal-health-event-checks",
            partition_key=dynamodb.Attribute(name="event_arn", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── FeedbackReports (sidebar "Feedback & Bug Report" tab) ────────
        # One row per submitted feedback item or bug report. All portal
        # users can list/create; admins can update status/reply.
        self.feedback_reports_table = dynamodb.Table(
            self,
            "FeedbackReportsTable",
            table_name="job-portal-feedback-reports",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── CostAdjustments (Cost Analysis page — manual extra fee/cost entries) ──
        # Admin-entered line items layered on top of live Cost Explorer
        # data at read time (see cost_explorer.apply_manual_adjustments());
        # never modifies the AWS-reported numbers themselves. Partition
        # key account_id + sort key id lets us query all adjustments for
        # one account cheaply; listing across all accounts falls back to
        # a scan (low-volume, admin-curated table).
        self.cost_adjustments_table = dynamodb.Table(
            self,
            "CostAdjustmentsTable",
            table_name="job-portal-cost-adjustments",
            partition_key=dynamodb.Attribute(name="account_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── CmdbSnapshots (CMDB — current resource inventory per account) ──
        # One row per AWS account holding its full current resource list
        # (see app/cmdb_store.py). Overwritten on every successful fetch;
        # used only to diff against the NEXT fetch to detect added/
        # removed/status-changed resources — the audit trail itself lives
        # in CmdbHistoryTable below.
        self.cmdb_snapshots_table = dynamodb.Table(
            self,
            "CmdbSnapshotsTable",
            table_name="job-portal-cmdb-snapshots",
            partition_key=dynamodb.Attribute(name="account_id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── CmdbHistory (CMDB — append-only added/removed/status-changed audit trail) ──
        # Partition key account_id + sort key id lets us query one
        # account's history cheaply; listing across all accounts falls
        # back to a scan (bounded by resource churn, not request volume —
        # same tradeoff CostAdjustmentsTable makes).
        self.cmdb_history_table = dynamodb.Table(
            self,
            "CmdbHistoryTable",
            table_name="job-portal-cmdb-history",
            partition_key=dynamodb.Attribute(name="account_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── Settings (generic admin-editable site settings, e.g. the yellow
        # Production Freeze banner) — tiny, low-volume, one row per
        # setting_key (see app/settings_store.py).
        self.settings_table = dynamodb.Table(
            self,
            "SettingsTable",
            table_name="job-portal-settings",
            partition_key=dynamodb.Attribute(name="setting_key", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── StandaloneCases (AWS Cases page — cases raised directly via
        # "Create AWS Case", not through a Job's Escalation Actions) ────
        # Tiny bookkeeping table, one row per portal-created case (see
        # app/standalone_case_store.py) — the real case data always lives
        # in AWS Support itself.
        self.standalone_cases_table = dynamodb.Table(
            self,
            "StandaloneCasesTable",
            table_name="job-portal-standalone-cases",
            partition_key=dynamodb.Attribute(name="case_id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── WhitelistAuditLog (Whitelist (LMS) tool — append-only audit trail) ──
        # One row per check/add attempt against the fixed WAF IPSet /
        # Security Group targets in app/whitelist_lms.py (see
        # app/whitelist_store.py). Visibility is restricted to admins +
        # Cloud Infra/Security/Management Team members at the route-handler
        # level (require_whitelist_audit_viewer() in main.py) — this table
        # itself has no access-control notion of its own. Single partition
        # key "id" (a uuid4 per record, same shape as FeedbackReportsTable
        # above) — low-volume, so listing across all records falls back to
        # a scan sorted by created_at in whitelist_store.list_records(),
        # same tradeoff CmdbHistoryTable/CostAdjustmentsTable make for
        # their own "list everything" paths.
        #
        # THIS TABLE WAS MISSING FROM THIS STACK ENTIRELY UNTIL NOW — its
        # absence (no table, no IAM grant on the task role below) is what
        # caused every /api/whitelist/lms/check, /add, and /audit-log call
        # to fail with a 500 (AccessDeniedException: dynamodb:PutItem/Scan
        # on a table ARN that had never been created), which surfaced to
        # the user as a frontend "Network Error".
        self.whitelist_audit_log_table = dynamodb.Table(
            self,
            "WhitelistAuditLogTable",
            table_name="job-portal-whitelist-audit-log",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── DnsAuditLog (DNS Record tool — append-only audit trail) ─────
        # One row per create/modify/delete attempt against the Route 53
        # hosted zones in the Core account (see app/dns_records.py and
        # app/dns_audit_store.py). Same shape and same single "id"
        # partition key as WhitelistAuditLogTable above, and the same
        # scan-and-sort read path, for the same reason: DNS changes are
        # rare enough that a full scan is cheaper than maintaining a GSI.
        #
        # Visibility is restricted to admins + Cloud Infra/Security/
        # Management Team at the route-handler level
        # (require_dns_audit_viewer() in main.py); the table has no
        # access-control notion of its own.
        #
        # A SEPARATE TABLE, not extra rows in job-portal-settings: that
        # table is a key/value store keyed by setting_key with one row per
        # setting, which is the wrong shape for append-only audit rows.
        #
        # DO NOT ADD A FEATURE LIKE THIS WITHOUT ALL THREE PIECES —
        # the table here, the IAM grant on the task role, and the
        # TABLE_NAME env var, both in compute_stack.py. Omitting any one
        # of them is what made every /api/whitelist/lms/* call return 500
        # (AccessDeniedException on PutItem/Scan against a table that had
        # never been created), which reached the user as a bare frontend
        # "Network Error" with nothing in the UI to explain it.
        self.dns_audit_log_table = dynamodb.Table(
            self,
            "DnsAuditLogTable",
            table_name="job-portal-dns-audit-log",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── S2sVpnAuditLog (S2S VPN Profile Creation tool — append-only
        # audit trail) ────────────────────────────────────────────────
        # One row per Customer Gateway / Site-to-Site VPN connection /
        # Transit Gateway static route create attempt against the network
        # account (see app/s2s_vpn_creation.py and
        # app/s2s_vpn_audit_store.py). Same shape and same single "id"
        # partition key + scan-and-sort read path as DnsAuditLogTable
        # above, for the same reason: this tool's writes are rare enough
        # that a full scan is cheaper than maintaining a GSI.
        #
        # Visibility is restricted to admins + Cloud Infra/Security Team
        # at the route-handler level (require_s2s_vpn_audit_viewer() in
        # main.py); the table has no access-control notion of its own.
        #
        # DO NOT ADD A FEATURE LIKE THIS WITHOUT ALL THREE PIECES — the
        # table here, the IAM grant on the task role, and the TABLE_NAME
        # env var, both in compute_stack.py. Omitting any one of them is
        # what made DnsAuditLogTable's absence a silent AccessDeniedException
        # 500 the first time around — see that table's comment above.
        self.s2s_vpn_audit_log_table = dynamodb.Table(
            self,
            "S2sVpnAuditLogTable",
            table_name="job-portal-s2s-vpn-audit-log",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── CloudAccounts registry + audit trail (Admin > Cloud Accounts;
        # .kiro/specs/cloud-account-onboarding) ───────────────────────────
        # Single source of truth for which AWS accounts / Azure
        # subscriptions the Portal reports on (app/cloud_account_store.py).
        # ~30-100 small rows, read as a whole by a 30-second in-process
        # snapshot cache, so a scan is fine and there is no GSI.
        # PK "{provider}#{identifier}" e.g. "aws#100000000003". Holds the
        # per-account External_Id for the InfraOps role, so it sits behind
        # the shared CMK like every other table and is RETAINed.
        #
        # THREE PIECES ARE REQUIRED FOR ANY NEW TABLE (see the S2sVpnAuditLog
        # note above): this table, the IAM grant on the task role and the
        # TABLE_NAME env var, both in compute_stack.py.
        self.cloud_accounts_table = dynamodb.Table(
            self,
            "CloudAccountsTable",
            table_name="job-portal-cloud-accounts",
            partition_key=dynamodb.Attribute(name="account_key", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # Append-only audit trail for the registry: who changed which
        # account and how. Never holds External_Ids or raw provider errors.
        # PK account_key, SK "{iso timestamp}#{uuid8}" so the newest entries
        # for one account are a Query with ScanIndexForward=False.
        self.cloud_account_audit_table = dynamodb.Table(
            self,
            "CloudAccountAuditTable",
            table_name="job-portal-cloud-account-audit",
            partition_key=dynamodb.Attribute(name="account_key", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── ChatConversations (Chat/Call feature — one row per Direct or
        # Group conversation) ────────────────────────────────────────────
        # PK conversation_id (uuid4). Holds type ("direct"|"group"), name/
        # owner_email (group only), participants (list of emails),
        # created_at, last_activity_at. Read via get_item on conversation
        # open; the "list my conversations" query does NOT scan this
        # table — see ChatParticipantsTable below for that lookup, same
        # "dedicated lookup table over a scan" reasoning already used by
        # CmdbHistoryTable/CostAdjustmentsTable elsewhere in this file,
        # but here backed by a real Query instead of a scan-and-sort,
        # since this is read on every page load (see app/chat_store.py).
        self.chat_conversations_table = dynamodb.Table(
            self,
            "ChatConversationsTable",
            table_name="job-portal-chat-conversations",
            partition_key=dynamodb.Attribute(name="conversation_id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── ChatParticipants (Chat/Call feature — per-user conversation
        # membership index) ──────────────────────────────────────────────
        # PK user_email, SK conversation_id. One row per (user,
        # conversation) pair. Exists purely so "list my conversations"
        # (Requirement 1.5 in job-portal-chat-voice spec) is a Query on
        # user_email instead of a scan+filter over ChatConversationsTable,
        # whose participants live in a list attribute that DynamoDB
        # cannot index directly.
        self.chat_participants_table = dynamodb.Table(
            self,
            "ChatParticipantsTable",
            table_name="job-portal-chat-participants",
            partition_key=dynamodb.Attribute(name="user_email", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="conversation_id", type=dynamodb.AttributeType.STRING),
            **common_kwargs,
        )

        # ── ChatMessages (Chat/Call feature — message history) ──────────
        # PK conversation_id, SK message_id (ULID — sortable by creation
        # time). Query with ScanIndexForward=True returns ascending
        # chronological order directly from the sort key, no in-Python
        # sort needed (a deliberate improvement over the scan-and-sort
        # pattern used by FeedbackReportsTable, justified because message
        # volume per conversation can be large enough that scan-and-sort
        # would not scale). Streams (NEW_AND_OLD_IMAGES) enabled so every
        # ECS task can observe new/edited messages and forward them only
        # to the WebSocket connections it personally holds — this is the
        # cross-task fan-out mechanism described in the chat-voice design
        # doc, needed because the ECS service runs 2-6 tasks behind the
        # ALB with no target-group stickiness.
        self.chat_messages_table = dynamodb.Table(
            self,
            "ChatMessagesTable",
            table_name="job-portal-chat-messages",
            partition_key=dynamodb.Attribute(name="conversation_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="message_id", type=dynamodb.AttributeType.STRING),
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            **common_kwargs,
        )

        # ── CallSessions (Chat/Call feature — live call signaling state
        # AND >=30-day call history, same table for both) ────────────────
        # PK call_id (uuid4). While status is "ringing"/"active" this row
        # drives the live ring/roster/accept/decline signaling fan-out
        # (via Streams, same mechanism as ChatMessagesTable above). Once
        # status becomes "ended" the row becomes a read-only history
        # record (who was invited, who joined/declined/timed out, start/
        # end time) until its TTL expires. TTL attribute "ttl" is set by
        # the backend to ended_at + 30 days, satisfying the >=30-day call
        # record retention requirement — this is NOT a short-lived
        # coordination-only table like NotificationDedupTable/
        # WebhookIdempotencyTable above.
        #
        # GSI ConversationCallHistoryIndex (PK conversation_id, SK
        # started_at) lets "list past calls for this conversation" be a
        # Query in chronological order instead of a scan — same "real
        # query pattern justifies a real index" reasoning as
        # ChatMessagesTable, since call history is a user-facing read
        # path (see app/call_store.py's list_call_history()).
        self.call_sessions_table = dynamodb.Table(
            self,
            "CallSessionsTable",
            table_name="job-portal-call-sessions",
            partition_key=dynamodb.Attribute(name="call_id", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="ttl",
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            **common_kwargs,
        )
        self.call_sessions_table.add_global_secondary_index(
            index_name="ConversationCallHistoryIndex",
            partition_key=dynamodb.Attribute(name="conversation_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="started_at", type=dynamodb.AttributeType.STRING),
        )

        # ── WsConnections (Chat/Call feature — cross-task WebSocket
        # connection registry) ───────────────────────────────────────────
        # PK connection_id (uuid4, generated on WS connect). GSI
        # UserEmailIndex (PK user_email) lets a task look up which
        # connection_id(s) belong to a given Team_Member so it can decide
        # whether it personally holds that connection (see
        # app/ws_connections.py / app/ws_manager.py). TTL attribute "ttl"
        # is refreshed by a 15-second heartbeat from each connected
        # client; an unclean disconnect (network loss, tab crash) is
        # detected within the TTL window rather than lingering forever,
        # keeping Presence ("online"/"offline") accurate per Requirement
        # 8.2's 30-second offline threshold.
        self.ws_connections_table = dynamodb.Table(
            self,
            "WsConnectionsTable",
            table_name="job-portal-ws-connections",
            partition_key=dynamodb.Attribute(name="connection_id", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="ttl",
            **common_kwargs,
        )
        self.ws_connections_table.add_global_secondary_index(
            index_name="UserEmailIndex",
            partition_key=dynamodb.Attribute(name="user_email", type=dynamodb.AttributeType.STRING),
        )

        # ── ChatAttachmentsBucket (Chat/Call feature — file/photo
        # attachments sent in conversations) ─────────────────────────────
        # Same shape as NoteAttachmentsBucket above (KMS CMK encrypted,
        # BLOCK_ALL public access, versioned, 90-day noncurrent-version
        # expiry) but — UNLIKE NoteAttachmentsBucket, whose uploads are
        # backend-proxied (see app/note_attachments.py) — this bucket
        # uses presigned PUT so the browser uploads directly to S3,
        # bypassing the FastAPI request body entirely. This is necessary
        # because chat attachments allow up to 50MB files, which should
        # not be proxied through the backend. Presigned GET (view/
        # download) is likewise generated fresh per request by the
        # backend after verifying the requester is a participant of the
        # owning conversation (see app/chat_attachments.py).
        self.chat_attachments_bucket = s3.Bucket(
            self,
            "ChatAttachmentsBucket",
            bucket_name=f"job-portal-chat-attachments-{self.account}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.cmk,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    noncurrent_version_expiration=Duration.days(90),
                ),
            ],
            cors=[
                # Presigned PUT uploads happen directly browser -> S3
                # (see app/chat_attachments.py's
                # generate_presigned_put_url()), same reasoning as
                # NoteAttachmentsBucket's CORS rule below.
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.PUT, s3.HttpMethods.GET],
                    allowed_origins=[
                        f"https://{CONFIG['custom_domain']}",
                        "http://localhost:5200",
                    ],
                    allowed_headers=["*"],
                    max_age=3000,
                ),
            ],
        )

        # ── NoteAttachmentsBucket (Engineer Notes — photo/file attachments) ──
        # Unlike profile photos (small, single, resized thumbnail — kept
        # inline as base64 on the DynamoDB row, see app/profile_photo.py),
        # Engineer Notes attachments must support arbitrary files (PDFs,
        # logs, zips, screenshots) and potentially many per job growing
        # over its lifetime — which would blow past DynamoDB's 400KB/item
        # limit. This is the first S3 bucket the backend's ECS task role
        # can actually write to (the only pre-existing bucket,
        # FrontendBucket in frontend_stack.py, is a private
        # CloudFront-OAC-only static-asset bucket unrelated to this).
        # Fully private (BLOCK_ALL public access) — access is only ever
        # via short-lived presigned GET/PUT URLs the backend generates
        # per-request (see app/note_attachments.py), so the bucket itself
        # is never exposed directly.
        self.note_attachments_bucket = s3.Bucket(
            self,
            "NoteAttachmentsBucket",
            bucket_name=f"job-portal-note-attachments-{self.account}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.cmk,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            enforce_ssl=True,
            lifecycle_rules=[
                # Old versions (overwritten/deleted attachments) don't need
                # to be kept forever — noncurrent versions expire after 90
                # days to bound storage cost growth over time.
                s3.LifecycleRule(
                    noncurrent_version_expiration=Duration.days(90),
                ),
            ],
            cors=[
                # Presigned PUT uploads happen directly browser -> S3
                # (see app/note_attachments.py's generate_presigned_put_url())
                # so the browser needs S3's own CORS headers, not just the
                # backend's. Scoped to this portal's real origins only.
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.PUT, s3.HttpMethods.GET],
                    allowed_origins=[
                        f"https://{CONFIG['custom_domain']}",
                        "http://localhost:5200",
                    ],
                    allowed_headers=["*"],
                    max_age=3000,
                ),
            ],
        )
