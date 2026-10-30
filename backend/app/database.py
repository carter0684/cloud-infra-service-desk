from sqlalchemy import create_engine, Column, String, DateTime, Text, JSON, Integer, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import uuid

DATABASE_URL = "sqlite:///./job_portal.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # Human-facing sequential ticket number (displayed as "INC-000123" by the
    # frontend) — added for the ITSM-style redesign so tickets have a short,
    # memorable reference instead of a raw UUID. Nullable because jobs
    # created before this column existed won't have one (see _migrate_db in
    # main.py, which backfills it for local/dev SQLite on next startup).
    ticket_number = Column(Integer, nullable=True, unique=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    category = Column(String, nullable=False)          # Networking/Compute/Storage/Security/AD/Identity/Other
    category_other = Column(String, nullable=True)     # free-text when category == "Other"
    priority = Column(String, nullable=False)          # ASAP/High/Medium/Low
    assignee_email = Column(String, nullable=False)
    assignee_name = Column(String, nullable=False)
    created_by_email = Column(String, nullable=False)
    created_by_name = Column(String, nullable=False)
    due_date = Column(String, nullable=True)           # ISO date string yyyy-mm-dd
    status = Column(String, default="Open")

    # AI draft (Bedrock mock)
    ai_draft = Column(JSON, nullable=True)             # full structured draft dict
    ai_analysed_at = Column(String, nullable=True)
    ai_analysed_by = Column(String, nullable=True)

    # Linked external tickets
    aws_support_case_id = Column(String, nullable=True)
    # Short numeric "Display ID" shown in the AWS Support Center UI (e.g.
    # "178548111300351") — what users actually recognise as "the case
    # number", as opposed to aws_support_case_id's long internal form
    # ("case-12345678910-2013-c4c1d2bf33c5cf47"). Only comes back from a
    # separate describe_cases call (create_case never returns it), so
    # this is nullable — a legacy row created before this field existed,
    # or a case whose describe_cases lookup failed, simply won't have one
    # and the frontend falls back to aws_support_case_id.
    aws_support_case_display_id = Column(String, nullable=True)
    aws_ams_sr_case_id = Column(String, nullable=True)
    aws_ams_sr_case_display_id = Column(String, nullable=True)
    aws_ams_sr_case_status = Column(String, nullable=True)
    aws_ams_incident_case_id = Column(String, nullable=True)
    aws_ams_incident_case_display_id = Column(String, nullable=True)
    aws_ams_incident_case_status = Column(String, nullable=True)
    freshservice_ticket_id = Column(String, nullable=True)
    freshservice_ticket_url = Column(String, nullable=True)

    # Audit trail list of {timestamp, actor, event}
    audit_log = Column(JSON, default=list)

    # Engineer notes/comments — list of {id, author, author_email, text, created_at}
    notes = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StandaloneCase(Base):
    """
    A tiny record of an AWS case (AWS Support / AMS Service Request / AMS
    Incident) raised directly from the AWS Cases page's "Create AWS Case"
    button — i.e. NOT via a Job's Escalation Actions. The real case data
    always lives in AWS Support itself (support:DescribeCases already
    returns it there regardless of how it was created); this row exists
    purely so:
      - ENV=local dev (no real AWS Support account to query) has
        something to list on the AWS Cases page after "creating" a mock
        case, mirroring how a Job's aws_support_case_id/aws_ams_*_case_id
        fields already work for Job-linked cases in _local_mock_cases().
      - the portal can show "raised via portal by <name>" attribution for
        these cases without depending on AWS Support's own free-text
        subject/communication body.
    Keyed by case_id (one row per case, never updated after creation).
    """
    __tablename__ = "standalone_cases"

    case_id = Column(String, primary_key=True)
    account = Column(String, nullable=False)        # "own" | "ams"
    case_type = Column(String, nullable=False)       # "aws_support" | "aws_ams_sr" | "aws_ams_incident"
    subject = Column(String, nullable=False)
    category = Column(String, nullable=True)
    priority = Column(String, nullable=True)
    # Short numeric "Display ID" shown in the AWS Support Center UI (e.g.
    # "178548111300351") — same distinction as Job.aws_support_case_display_id.
    display_id = Column(String, nullable=True)
    raised_by_email = Column(String, nullable=True)
    raised_by_name = Column(String, nullable=True)
    created_at = Column(String, nullable=True)       # ISO timestamp string


class KBDocument(Base):
    """Knowledge Base document uploaded for RAG-style AI enrichment."""
    __tablename__ = "kb_documents"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    filename = Column(String, nullable=False)
    file_type = Column(String, nullable=False)          # pdf / docx / txt / md etc.
    description = Column(String, nullable=True)         # optional user-provided label
    char_count = Column(Integer, default=0)
    chunk_count = Column(Integer, default=0)
    # Chunks stored as JSON list of {"chunk_id": n, "text": "..."}
    chunks = Column(JSON, default=list)
    uploaded_by = Column(String, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)


class TicketCounter(Base):
    """
    Singleton row (id=1) holding the next sequential ticket number to hand
    out. Incrementing it inside the same SQLAlchemy transaction as the Job
    insert (see job_store.py's _next_ticket_number_local()) is sufficient
    for this local-dev SQLite backend, which only ever runs a single
    process/connection. The production DynamoDB backend uses an atomic
    UpdateItem ADD instead (see dynamo_repository.py) since it can have
    multiple ECS tasks writing concurrently.
    """
    __tablename__ = "ticket_counters"

    id = Column(Integer, primary_key=True)
    value = Column(Integer, nullable=False, default=1000)


class HealthEventCheck(Base):
    """
    Shared per-event state for a single AWS Health event on the AWS
    Health Dashboard page (event_arn as primary key — one record per
    event, not per user). Started as just a checkbox (Task 10); extended
    (Task 12) with:
      - a free-text engineer remark shown next to "Last Updated"
      - a "finished" checkbox living inside the event-detail popup,
        separate from the row-level "checked" checkbox above
      - AMS Service Request linkage, so a Health event can be one-click
        escalated to the AMS support account (100000000001) without
        needing a Job first, and the All Cases page can show which
        Health event a given AMS SR case came from.
    """
    __tablename__ = "health_event_checks"

    event_arn = Column(String, primary_key=True)
    checked = Column(Boolean, default=False)
    checked_by_email = Column(String, nullable=True)
    checked_by_name = Column(String, nullable=True)
    checked_at = Column(String, nullable=True)  # ISO timestamp string

    # Engineer-typed free-text remark (Task 12, sub-feature 1)
    remark = Column(Text, nullable=True)
    remark_by_email = Column(String, nullable=True)
    remark_by_name = Column(String, nullable=True)
    remark_at = Column(String, nullable=True)

    # "Mark as finished" checkbox inside the event-detail popup (Task 12,
    # sub-feature 2) — distinct from the row-level `checked` above.
    finished = Column(Boolean, default=False)
    finished_by_email = Column(String, nullable=True)
    finished_by_name = Column(String, nullable=True)
    finished_at = Column(String, nullable=True)

    # AMS Service Request raised directly from this Health event, with no
    # Job involved (Task 12, sub-feature 3/4). service/event_type_code are
    # duplicated here (rather than re-fetched later) so the All Cases page
    # can show "linked to Health event <X>" without an extra AWS call.
    ams_sr_case_id = Column(String, nullable=True)
    ams_sr_case_status = Column(String, nullable=True)
    ams_sr_service = Column(String, nullable=True)
    ams_sr_event_type_code = Column(String, nullable=True)
    ams_sr_raised_by_email = Column(String, nullable=True)
    ams_sr_raised_by_name = Column(String, nullable=True)
    ams_sr_raised_at = Column(String, nullable=True)

    # Per-resource tick state inside the "Affected resources" table of the
    # event-detail popup — one entry per resource, keyed by a
    # resource_key (entity_value + account) computed server-side, not a
    # separate table/row per resource (an event can have many affected
    # resources; a single JSON map on the already-existing per-event row
    # avoids a schema/table change). Shape:
    #   {resource_key: {checked, checked_by_email, checked_by_name, checked_at}}
    resource_checks = Column(JSON, nullable=True, default=dict)

    # Portal Job created from this Health event (one-click "Create Job",
    # for events that need engineer follow-up regardless of whether an
    # AMS SR was also raised — the two are independent, a Job can exist
    # with or without ams_sr_case_id set). job_ticket_number is
    # duplicated here (not just job_id) so the dashboard can render
    # "INC-xxxxxx" without a separate Job lookup.
    job_id = Column(String, nullable=True)
    job_ticket_number = Column(Integer, nullable=True)
    job_created_by_email = Column(String, nullable=True)
    job_created_by_name = Column(String, nullable=True)
    job_created_at = Column(String, nullable=True)

    # Manual admin override: force this event into a specific dashboard
    # tab regardless of what its AWS eventTypeCategory/eventTypeCode would
    # normally route it to (drag-and-drop feature — e.g. an admin dragging
    # an AWS_SDK_PLANNED_LIFECYCLE_EVENT that AWS classifies as
    # scheduledChange out of "Scheduled changes" into the "Lifecycle
    # event" tab). Value is one of the dashboard's real tab keys
    # (_HEALTH_TAB_KEYS minus "event_log", which is a flat view, not a
    # real category) — see main.py's _group_health_events(). None means
    # "use automatic classification" (the default, pre-feature behaviour).
    category_override = Column(String, nullable=True)
    category_override_by_email = Column(String, nullable=True)
    category_override_by_name = Column(String, nullable=True)
    category_override_at = Column(String, nullable=True)


class FeedbackReport(Base):
    """
    User-submitted feedback or bug report, surfaced on the portal's
    "Feedback & Bug Report" page (sidebar). Any logged-in user can submit
    either type, but VISIBILITY differs by report_type (enforced in
    main.py, not here):
      - "feedback": visible to admins AND the original submitter only —
        a private two-way conversation between the two.
      - "bug": visible to admins ONLY, even to the person who submitted
        it (they lose visibility once submitted; only admins can see and
        comment on bug reports).
    replies is a threaded conversation (not a single admin_reply) so both
    sides can go back and forth on a "feedback" report.
    """
    __tablename__ = "feedback_reports"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    report_type = Column(String, nullable=False, default="feedback")   # "feedback" | "bug"
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    severity = Column(String, nullable=True)     # Low/Medium/High/Critical — bug reports only
    page_url = Column(String, nullable=True)      # which portal page the user was on
    status = Column(String, nullable=False, default="Open")  # Open/In Review/Resolved/Closed

    submitted_by_email = Column(String, nullable=False)
    submitted_by_name = Column(String, nullable=False)

    # Threaded replies — list of {id, author_email, author_name, is_admin,
    # text, created_at}. Who may post into this thread is enforced in
    # main.py based on report_type + actor (see _feedback_visible_to()).
    replies = Column(JSON, nullable=True, default=list)

    # AI triage note for a "bug" report (see bug_analysis.py): {status:
    # pending|done|failed, model, model_label, generated_at, analysis{...},
    # error}. Admin-only like the rest of a bug report; None until one has
    # been requested. (Production DynamoDB is schemaless — no migration.)
    ai_analysis = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CloudAccountEntry(Base):
    """
    Cloud account registry row (see cloud_account_store.py and
    .kiro/specs/cloud-account-onboarding). ENV=local only; production uses
    the DynamoDB table job-portal-cloud-accounts. The whole entry lives in
    `data` (JSON) so the local and DynamoDB shapes are identical.
    """
    __tablename__ = "cloud_accounts"

    account_key = Column(String, primary_key=True)   # "{provider}#{identifier}"
    data = Column(JSON, nullable=False)


class CloudAccountAuditEntry(Base):
    """Append-only audit trail for the cloud account registry (ENV=local)."""
    __tablename__ = "cloud_account_audit"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    account_key = Column(String, nullable=False, index=True)
    sk = Column(String, nullable=False)              # "{iso timestamp}#{uuid8}"
    data = Column(JSON, nullable=False)


class CostAdjustment(Base):
    """
    Manual cost adjustment line item for the AWS Cost Analysis page —
    lets a Cloud Manager record an extra fee/cost (or a correction) that
    isn't visible in AWS Cost Explorer for a given account + month, e.g.
    a partner-invoiced charge, a manual chargeback, or a known billing
    correction. Purely additive: these entries are layered on top of the
    live Cost Explorer numbers at read time (see cost_explorer.py's
    apply_manual_adjustments()) and are NEVER written back into or
    treated as authoritative Cost Explorer data — they exist only in this
    table, clearly separated in the API response as their own
    "adjustments" bucket per period so the underlying AWS numbers stay
    exactly what AWS reported.

    period is a "YYYY-MM" string identifying which month's bucket the
    adjustment belongs to (matches the "mtd" / "last_month" /
    "two_months_ago" buckets by comparing against the actual calendar
    month each of those maps to at read time — see cost_explorer.py).
    amount may be positive (extra cost) or negative (a credit/correction
    to reduce a previously overstated month) — the UI ensures it can
    never accidentally show as if it were an AWS Marketplace/negative
    "credits" bucket entry.
    """
    __tablename__ = "cost_adjustments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id = Column(String, nullable=False, index=True)
    period = Column(String, nullable=False)          # "YYYY-MM"
    label = Column(String, nullable=False)            # short description, e.g. "Partner invoice - Q3 support"
    amount = Column(String, nullable=False)           # stored as string; parsed as Decimal/float on read (see store)
    currency = Column(String, nullable=False, default="USD")
    note = Column(Text, nullable=True)

    created_by_email = Column(String, nullable=False)
    created_by_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_by_email = Column(String, nullable=True)
    updated_by_name = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PortalSetting(Base):
    """
    Generic single-row-per-key portal setting, managed via the Admin page.
    Currently used for the site-wide "Production Freeze" yellow banner
    (setting_key="freeze_notice") so an admin can edit its message and
    toggle it on/off without a code deploy — see app/settings_store.py.
    value is a JSON blob so each setting_key can have whatever shape it
    needs (for freeze_notice: {enabled, message}) without a schema change
    every time a new admin-editable setting is added.
    """
    __tablename__ = "portal_settings"

    setting_key = Column(String, primary_key=True)
    value = Column(JSON, nullable=False, default=dict)
    updated_by_email = Column(String, nullable=True)
    updated_by_name = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PortalUser(Base):
    """
    Portal user allowlist + role, managed via the Admin page.
    role: "admin" (can manage users) or "member" (regular access only).
    """
    __tablename__ = "portal_users"

    email = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False, default="member")   # "admin" | "member"
    added_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Last successful login (UTC) — stamped by user_store.update_last_login(),
    # called once per real login event (mock_login() for local dev,
    # login_landing() after the ALB OIDC challenge completes in
    # production) — deliberately NOT updated on every authenticated API
    # request (get_actor()/whoami()), which would turn this into a
    # "last seen" timestamp instead. Displayed in Hong Kong time on the
    # Admin page's User Management table (see AdminPage.jsx). Nullable
    # since a user who has never logged in yet (freshly added via the
    # Admin page) has no value here — see main.py's _migrate_db() for the
    # local SQLite backfill.
    last_login = Column(DateTime, nullable=True)
    # Profile photo — a small resized/compressed JPEG stored inline as a
    # "data:image/jpeg;base64,..." URI (see app/profile_photo.py). No S3
    # bucket exists anywhere in this portal's infra yet, and photos are
    # capped small enough (~256x256, JPEG) to comfortably fit this single
    # column rather than standing up a new bucket + IAM grant just for
    # this. Self-uploaded via /api/profile/photo, or set for another user
    # by an admin via /api/admin/users/{email}/photo.
    photo_data_url = Column(Text, nullable=True)
    # User Group memberships (Security Team / Infra Team / Cloud Infra
    # Team / Management Team / Billing Team — see app/settings_store.py's
    # GROUP_REGISTRY) — a JSON list of group_id strings, e.g.
    # ["security_team", "billing_team"]. A user can belong to zero or
    # more groups; group membership only ever ADDS category-level access
    # on top of the normal role-based Tab Access Control, never removes
    # any. Defaults to an empty list for users added before this feature
    # existed (see main.py's _migrate_db()).
    groups = Column(JSON, nullable=False, default=list)
    # Per-user WRITE grants — a JSON list of tab_id strings (from
    # settings_store.TAB_REGISTRY) this user may perform the write
    # action(s) for (e.g. "jobs" -> may create a new ticket, "dns_records"
    # -> may edit DNS records), independent of which User Groups they
    # belong to. Applies to EVERY tab (per the user's explicit choice,
    # since it's unknown which tabs will gain a write action in future),
    # though most tabs have no write action to gate at all today — see
    # main.py's require_tab_write(). Deliberately separate from `groups`
    # above: group membership grants READ (tab visibility) to everyone
    # in that group equally, but WRITE is always an individual grant,
    # made by a founding admin / Management Team / Security Team member
    # (user_store.is_portal_authority()), never inherited automatically
    # from group membership alone — except for dns_records and
    # s2s_vpn_profile_creation, whose existing group-based writer check
    # (_DNS_PRIVILEGED_GROUPS / _S2S_VPN_PRIVILEGED_GROUPS in main.py) is
    # additionally OR'd with this list, so someone already trusted via
    # those groups doesn't lose write access; this list is how someone
    # OUTSIDE those groups can still be granted write on those two tabs
    # individually. Defaults to an empty list for every user until a
    # portal authority explicitly grants a tab.
    tab_write = Column(JSON, nullable=False, default=list)


class CmdbSnapshot(Base):
    """
    CMDB — latest known resource inventory for one AWS account, used to
    diff against the next poll and detect added/removed/status-changed
    resources (see cmdb_store.diff_and_record()). One row per account_id;
    `resources` is the full list of normalized resource dicts (see
    cmdb.py's `_norm()`) as JSON. This table only ever holds the CURRENT
    state — the audit trail of what changed and when lives in
    CmdbHistoryEvent below.
    """
    __tablename__ = "cmdb_snapshots"

    account_id = Column(String, primary_key=True)
    resources = Column(JSON, nullable=False, default=list)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CmdbHistoryEvent(Base):
    """
    CMDB — one row per detected inventory change (a resource newly seen,
    no longer seen, or with a changed status) since the previous poll of
    that account. Append-only audit trail; never updated or deleted by
    normal operation. Powers the "when did this resource appear/
    disappear" history view on the CMDB page.
    """
    __tablename__ = "cmdb_history_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False)       # "added" | "removed" | "status_changed"
    resource_type = Column(String, nullable=False)     # "EC2 Instance", "RDS Instance", ...
    resource_id = Column(String, nullable=False)
    name = Column(String, nullable=True)
    region = Column(String, nullable=True)
    status = Column(String, nullable=True)
    previous_status = Column(String, nullable=True)    # only set for "status_changed" events
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)


class WhitelistAuditLog(Base):
    """
    Append-only audit trail for the "Whitelist (LMS)" tool — every time a
    user checks or adds an IP to one of the fixed WAF IPSet / Security
    Group targets (see app/whitelist_lms.py), one row is written here.
    Never updated or deleted by normal operation.

    VISIBILITY: unlike every other table in this file, rows here are
    NOT visible to every portal user — main.py's
    require_whitelist_audit_viewer() restricts GET /api/whitelist/audit-log
    to admins plus members of the "cloud_infra_team", "security_team", or
    "management_team" User Groups (settings_store.GROUP_REGISTRY), per
    the user's explicit requirement. This table itself has no row-level
    permission model of its own — the restriction lives entirely in the
    route handler, same as every other access-control check in this app.
    """
    __tablename__ = "whitelist_audit_log"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    action = Column(String, nullable=False)         # "check" | "add" | "add_failed"
    target_id = Column(String, nullable=False)       # e.g. "newlms_prod_backend"
    target_label = Column(String, nullable=False)    # human label at time of write, e.g. "newlms-prod-ipset-whitelist-backend"
    region = Column(String, nullable=False)           # "us-east-1" | "ap-southeast-1"
    ip_address = Column(String, nullable=False)
    sr_number = Column(String, nullable=True)         # Service Request number — only present for "add"/"add_failed"
    result = Column(String, nullable=False)           # "already_whitelisted" | "added" | "error"
    detail = Column(Text, nullable=True)              # error message, if result == "error"
    actor_email = Column(String, nullable=False)
    actor_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class DnsAuditLog(Base):
    """
    Append-only audit trail for the "DNS Record" tool — every time a user
    adds, modifies or deletes a Route 53 record set in the Core account
    account's hosted zones (see app/dns_records.py), one row is written
    here. Never updated or deleted by normal operation.

    VISIBILITY: like WhitelistAuditLog above, rows here are NOT visible
    to every portal user — main.py's require_dns_audit_viewer() restricts
    GET /api/dns/audit-log to admins plus members of the
    "cloud_infra_team", "security_team", or "management_team" User Groups
    (settings_store.GROUP_REGISTRY). The restriction lives entirely in
    the route handler; this table has no row-level permission model.

    WHY previous_value EXISTS HERE BUT NOT ON WhitelistAuditLog: a
    whitelist row is purely additive, so the "before" state is implied
    (the IP wasn't there). A DNS modify/delete destroys the old value,
    Route 53 keeps no history of its own, and "what was it before?" is
    the first question asked when a domain breaks — so the pre-change
    contents are captured at write time or they're gone for good.

    TTL IS A STRING, NOT AN INTEGER. In production these rows come back
    from DynamoDB, which returns numeric attributes as decimal.Decimal —
    a value FastAPI cannot serialise to JSON. Keeping the column a String
    means the local SQLite path and the DynamoDB path produce the same
    Python type, so the audit-log endpoint behaves identically in both.
    """
    __tablename__ = "dns_audit_log"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    action = Column(String, nullable=False)          # "create" | "modify" | "delete" | "*_failed"
    zone_id = Column(String, nullable=False)          # Route 53 hosted zone id, e.g. "Z08611652W2TA05EOPP20"
    zone_name = Column(String, nullable=False)        # human domain at time of write, e.g. "example-site-a.com"
    record_name = Column(String, nullable=False)      # fully-qualified record name, unescaped for display
    record_type = Column(String, nullable=False)      # "A" | "CNAME" | "MX" | "TXT" | ...
    ttl = Column(String, nullable=True)               # seconds, as text; NULL for ALIAS records (they have no TTL)
    value = Column(Text, nullable=True)               # new value(s) after the change; NULL for a delete
    previous_value = Column(Text, nullable=True)      # value(s) before a modify/delete; NULL for a create
    sr_number = Column(String, nullable=True)         # Service Request number — mandatory on every real write
    change_id = Column(String, nullable=True)         # Route 53 ChangeInfo id, for cross-referencing in AWS
    # Correlates every row written by ONE bulk file upload. NULL for
    # single-record edits. Without it a 40-row upload is 40 unrelated-looking
    # audit rows, and "show me everything that one import changed" — the
    # first question asked after a bad bulk change — is unanswerable.
    batch_id = Column(String, nullable=True)
    result = Column(String, nullable=False)           # "created" | "modified" | "deleted" | "error"
    detail = Column(Text, nullable=True)              # error message, if result == "error"
    actor_email = Column(String, nullable=False)
    actor_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class S2sVpnAuditLog(Base):
    """
    Append-only audit trail for the "S2S VPN Profile Creation" tool —
    every time a user creates a Customer Gateway, a Site-to-Site VPN
    connection, or a Transit Gateway static route in the network account
    (100000000003) via app/s2s_vpn_creation.py, one row is written here.
    Never updated or deleted by normal operation. Mirrors DnsAuditLog
    above exactly, for the same reason: this tool creates real,
    billable, hard-to-fully-undo AWS network infrastructure, so the
    audit trail must always tie a change back to who did it, when, and
    under which approved Service Request.

    VISIBILITY: main.py's require_s2s_vpn_audit_viewer() restricts GET
    /api/s2s-vpn/audit-log to admins plus members of the
    "cloud_infra_team"/"security_team" User Groups — narrower than DNS's
    three-group set, since this tool is infra/security-specific, not
    something Management Team would ever action. The restriction lives
    entirely in the route handler; this table has no row-level
    permission model of its own.
    """
    __tablename__ = "s2s_vpn_audit_log"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    action = Column(String, nullable=False)          # "create_customer_gateway" | "create_vpn_connection" | "create_tgw_route" | "*_failed"
    region = Column(String, nullable=False)          # one of s2s_vpn_creation.ALLOWED_REGIONS
    resource_type = Column(String, nullable=False)   # "customer_gateway" | "vpn_connection" | "tgw_route"
    resource_id = Column(String, nullable=True)       # cgw-.../vpn-...; NULL if the create failed before AWS returned an id
    resource_name = Column(String, nullable=True)     # Name tag, or the CIDR block for a route
    sr_number = Column(String, nullable=True)         # Service Request number — mandatory on every real write
    result = Column(String, nullable=False)           # "created" | "error"
    detail = Column(Text, nullable=True)              # error message, if result == "error"
    actor_email = Column(String, nullable=False)
    actor_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ChatConversation(Base):
    """
    Chat/Call feature (job-portal-chat-voice spec) — one row per Direct
    or Group conversation. participants is a JSON list of Team_Member
    email strings; membership visibility/removal semantics (Requirement
    1.6/1.7) are enforced in chat_store.py, not here. type is
    "direct"|"group"; name/owner_email are group-only (NULL for direct
    conversations).
    """
    __tablename__ = "chat_conversations"

    conversation_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    type = Column(String, nullable=False)  # "direct" | "group"
    name = Column(String, nullable=True)
    owner_email = Column(String, nullable=True)
    participants = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_activity_at = Column(DateTime, default=datetime.utcnow)


class ChatMessage(Base):
    """
    Chat/Call feature — message history. Ordered by created_at within a
    conversation (SQLite local mode uses a real ORDER BY; production
    DynamoDB uses message_id, a ULID, as the sort key for the same
    ordering — see chat_store.py). attachment is a JSON object
    ({s3_key, filename, content_type, size_bytes, kind}) or NULL for a
    text-only message. read_by is a JSON map of email -> read_at
    timestamp (Requirement 2.5 read receipts).
    """
    __tablename__ = "chat_messages"

    message_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String, nullable=False, index=True)
    sender_email = Column(String, nullable=False)
    content = Column(Text, nullable=True)
    attachment = Column(JSON, nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow, index=True)
    edited_at = Column(DateTime, nullable=True)
    read_by = Column(JSON, nullable=False, default=dict)


class CallSession(Base):
    """
    Chat/Call feature — live call signaling state AND >=30-day call
    history (Requirement 11), same row for both, matching the
    production DynamoDB CallSessionsTable's design (see
    data_stack.py's comment on that table). While status is
    "ringing"/"active" this row drives live signaling; once status
    becomes "ended" it's a read-only history record. participants is a
    JSON map of email -> {state, joined_at}. No TTL attribute exists in
    local/SQLite mode (local dev only, not production data — TTL-based
    cleanup is a DynamoDB-only concern, see call_store.py's production
    branch).
    """
    __tablename__ = "call_sessions"

    call_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String, nullable=False, index=True)
    type = Column(String, nullable=False)  # "direct" | "group"
    caller_email = Column(String, nullable=False)
    chime_meeting_id = Column(String, nullable=True)
    chime_media_region = Column(String, nullable=True)
    # job-portal-chat-voice Task 15.3 fix: the Chime SDK Meetings API's
    # CreateMeeting response includes a MediaPlacement object (AudioHostUrl/
    # SignalingUrl/ScreenDataUrl/etc.) that amazon-chime-sdk-js's
    # MeetingSession constructor requires — chime_client.py's
    # create_meeting_for_call() already returns it, but call_store.py was
    # discarding it entirely (never persisted, and accept_call()
    # hardcoded "media_placement": None in its response with a comment
    # claiming the callee could "fetch via get_meeting if needed" — no
    # such route/call ever existed). Persisting it here means every
    # accept_call() response (and the caller's own create_call()
    # response) can return the SAME real MediaPlacement without a second
    # Chime API round-trip.
    chime_media_placement = Column(JSON, nullable=True)
    # job-portal-chat-voice Task 15.3 fix (second gap found alongside
    # chime_media_placement above): Requirement 5.1's own create_call()
    # docstring says "the frontend joins the caller's own client to the
    # meeting immediately on create_call() success" — but nothing in
    # this codebase ever actually added the CALLER as a Chime attendee;
    # only accept_call() (for a callee who explicitly accepts) ever
    # called chime_client.add_attendee(). Deliberately stored here as
    # separate top-level columns rather than a `participants[caller]`
    # roster entry — main.py's invite_to_call_route() and this module's
    # own call-history listing functions both already special-case
    # `caller_email == requester` as an OR alongside "in participants"
    # (see those functions' comments), i.e. "the caller is never in the
    # participants roster" is an existing, relied-upon invariant
    # elsewhere in this codebase; adding the caller into that dict would
    # require auditing/changing every one of those call sites for no
    # real benefit over just giving the caller their own pair of fields.
    caller_attendee_id = Column(String, nullable=True)
    caller_join_token = Column(String, nullable=True)
    status = Column(String, nullable=False, default="ringing")  # ringing | active | ended
    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    ended_at = Column(DateTime, nullable=True)
    participants = Column(JSON, nullable=False, default=dict)


class WsConnection(Base):
    """
    Chat/Call feature — WebSocket connection registry (see ws_connections.py
    and ws_manager.py), matching the production DynamoDB WsConnectionsTable's
    design (data_stack.py). Each row represents one live WebSocket connection
    for one Team_Member, refreshed by a 15-second client heartbeat
    (ws_connections.heartbeat()). last_heartbeat_at is used in place of a real
    DynamoDB TTL attribute in local/SQLite mode (TTL-based auto-expiry is a
    DynamoDB-only concern — local mode instead treats any row whose
    last_heartbeat_at is older than the staleness window as effectively
    expired, see ws_connections.py's list_connections_for_user()).
    """
    __tablename__ = "ws_connections"

    connection_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_email = Column(String, nullable=False, index=True)
    connected_at = Column(DateTime, default=datetime.utcnow)
    last_heartbeat_at = Column(DateTime, default=datetime.utcnow, index=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
