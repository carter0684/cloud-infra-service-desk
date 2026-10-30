import os

from fastapi import (
    FastAPI, Depends, HTTPException, Header, UploadFile, File, Form, Request, BackgroundTasks,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone
import csv
import io
import json
import re
import uuid

from . import org_config
from . import job_store
from . import user_store
from .notifications import (
    notify_job_assigned, notify_status_changed,
    notify_ai_complete, notify_ticket_raised
)
from .ai_engine import (
    analyse_job, analyse_health_event, ai_extract_job_fields_bedrock,
    analyse_security_hub_summary, analyse_security_finding,
    _get_client, _invoke,
    chat_with_grounding,
    MODEL_PRIMARY, MODEL_FALLBACK, MODEL_CHAT,
)
from .file_parser import extract_text_from_file, ai_extract_job_fields
from .kb_store import chunk_text, retrieve_relevant_chunks, format_kb_context
from . import fs_calendar
from . import security_portal
from . import world_holidays
from . import sr_ticket
from . import connected_portal_credentials
from . import health_ack_store
from . import cost_adjustment_store
from . import feedback_store
from . import bug_analysis
from . import settings_store
from . import whitelist_store
from . import dns_audit_store
from . import s2s_vpn_audit_store
from . import standalone_case_store
from .cache_utils import TTLCache

ENV = os.environ.get("ENV", "local")

app = FastAPI(title="Cloud Infra Job Portal API")

app.add_middleware(
    CORSMiddleware,
    # The frontend (CloudFront, PORTAL_DOMAIN) and the backend (ALB, API_DOMAIN)
    # live on different hosts, so every API call is a genuine cross-origin
    # request and those origins must be explicitly allowed (see org_config.py).
    allow_origins=org_config.cors_origins(),
    # Defence-in-depth: also allow any *.cloudfront.net distribution domain
    # directly, and any sub-domain of ORG_EMAIL_DOMAIN when it is configured.
    # Both suffixes are AWS/organisation-controlled, not attacker-choosable.
    allow_origin_regex=org_config.cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    # SQLite is a local-dev-only convenience. In production (ENV != local)
    # persistence goes through DynamoDB instead (see job_store.py), and the
    # container's non-root filesystem has no writable path for a SQLite
    # file anyway — skip DB init entirely so the app doesn't crash-loop on
    # startup. All routes below go through job_store.py, which branches on
    # ENV internally, so no other code here needs to know which backend is
    # active.
    if ENV == "local":
        from .database import init_db, engine
        init_db()
        _migrate_db(engine)

    # Cloud account registry (Admin > Cloud Accounts): create any missing built-in
    # entries and verify the established accounts, on a background thread so a slow
    # AWS/Azure call can never delay startup. Idempotent and safe to run on every
    # task. Skipped for local development (it would call AWS/Azure) unless
    # CLOUD_ACCOUNTS_BOOTSTRAP=1.
    if ENV != "local" or os.environ.get("CLOUD_ACCOUNTS_BOOTSTRAP") == "1":
        import threading as _threading
        from . import cloud_account_service as _cloud_account_service

        _threading.Thread(target=_cloud_account_service.bootstrap, name="cloud-account-bootstrap", daemon=True).start()

    # job-portal-chat-voice Task 10.2/5.3: start the DynamoDB Streams
    # poller/dispatcher that fans out new/edited Messages and Call
    # signaling events to whichever local WebSocket connections this
    # ECS task holds. This function is declared `async def` (changed
    # from a plain `def`) specifically so this runs as a coroutine on
    # the app's own running event loop — ws_manager.start_stream_pollers()
    # calls asyncio.create_task() internally, which requires an active
    # loop in the calling context; a sync startup handler would be run
    # via Starlette's threadpool executor instead, where there is no
    # such loop. Local mode still calls this (see that function's own
    # docstring on why it's a safe no-op there — no real DynamoDB
    # Streams exist against the local SQLite backend).
    from . import ws_manager
    ws_manager.start_stream_pollers()


@app.on_event("shutdown")
async def shutdown():
    """Task 10.2: graceful cleanup of the background Streams pollers on
    container stop/redeploy — not strictly required for correctness
    (ECS simply kills the process), but avoids noisy "Task was
    destroyed but it is pending" asyncio warnings in the logs."""
    from . import ws_manager
    ws_manager.stop_stream_pollers()


def _migrate_db(engine):
    """
    Apply additive schema migrations for columns added after initial DB creation.
    SQLite doesn't support ALTER TABLE ADD COLUMN IF NOT EXISTS, so we check first.
    Local-dev-only (see startup(), only called when ENV == "local").
    """
    from sqlalchemy import text, inspect
    with engine.connect() as conn:
        inspector = inspect(engine)

        # jobs table — add notes column if missing
        jobs_cols = [c["name"] for c in inspector.get_columns("jobs")]
        if "notes" not in jobs_cols:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN notes JSON"))
            conn.commit()

        # jobs table — add ticket_number column if missing, then backfill
        # existing rows (created before this column existed) with
        # sequential numbers in created_at order, and seed the counter
        # table so the *next* new job continues from the highest existing
        # number rather than colliding with it.
        jobs_cols = [c["name"] for c in inspector.get_columns("jobs")]
        if "ticket_number" not in jobs_cols:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN ticket_number INTEGER"))
            conn.commit()
            rows = conn.execute(text("SELECT id FROM jobs ORDER BY created_at ASC")).fetchall()
            next_num = 1001
            for (job_id,) in rows:
                conn.execute(
                    text("UPDATE jobs SET ticket_number = :n WHERE id = :id"),
                    {"n": next_num, "id": job_id},
                )
                next_num += 1
            conn.commit()
            highest = next_num - 1 if rows else 1000
            existing_counter = conn.execute(text("SELECT id FROM ticket_counters WHERE id = 1")).fetchone()
            if existing_counter:
                conn.execute(text("UPDATE ticket_counters SET value = :v WHERE id = 1"), {"v": highest})
            else:
                conn.execute(text("INSERT INTO ticket_counters (id, value) VALUES (1, :v)"), {"v": highest})
            conn.commit()

        # health_event_checks table — add Task 12 columns (remark/finished/
        # AMS SR linkage) if this DB file predates them.
        health_cols = [c["name"] for c in inspector.get_columns("health_event_checks")]
        _new_health_cols = {
            "remark": "TEXT", "remark_by_email": "VARCHAR", "remark_by_name": "VARCHAR", "remark_at": "VARCHAR",
            "finished": "BOOLEAN", "finished_by_email": "VARCHAR", "finished_by_name": "VARCHAR", "finished_at": "VARCHAR",
            "ams_sr_case_id": "VARCHAR", "ams_sr_case_status": "VARCHAR",
            "ams_sr_service": "VARCHAR", "ams_sr_event_type_code": "VARCHAR",
            "ams_sr_raised_by_email": "VARCHAR", "ams_sr_raised_by_name": "VARCHAR", "ams_sr_raised_at": "VARCHAR",
            "resource_checks": "JSON",
            "job_id": "VARCHAR", "job_ticket_number": "INTEGER",
            "job_created_by_email": "VARCHAR", "job_created_by_name": "VARCHAR", "job_created_at": "VARCHAR",
            "category_override": "VARCHAR", "category_override_by_email": "VARCHAR",
            "category_override_by_name": "VARCHAR", "category_override_at": "VARCHAR",
        }
        for col_name, col_type in _new_health_cols.items():
            if col_name not in health_cols:
                conn.execute(text(f"ALTER TABLE health_event_checks ADD COLUMN {col_name} {col_type}"))
                conn.commit()

        # portal_users table — add photo_data_url column if this DB file
        # predates the profile photo feature.
        users_cols = [c["name"] for c in inspector.get_columns("portal_users")]
        if "photo_data_url" not in users_cols:
            conn.execute(text("ALTER TABLE portal_users ADD COLUMN photo_data_url TEXT"))
            conn.commit()

        # portal_users table — add groups column (User Groups feature —
        # Security Team / Infra Team / Cloud Infra Team / Management Team
        # / Billing Team) if this DB file predates it.
        users_cols = [c["name"] for c in inspector.get_columns("portal_users")]
        if "groups" not in users_cols:
            conn.execute(text("ALTER TABLE portal_users ADD COLUMN groups JSON"))
            conn.commit()
            conn.execute(text("UPDATE portal_users SET groups = '[]' WHERE groups IS NULL"))
            conn.commit()

        # portal_users table — add last_login column (User Management
        # "Last Login" column, HKT) if this DB file predates it. Left
        # NULL for existing rows — they'll get a real value the next
        # time that user actually logs in.
        users_cols = [c["name"] for c in inspector.get_columns("portal_users")]
        if "last_login" not in users_cols:
            conn.execute(text("ALTER TABLE portal_users ADD COLUMN last_login DATETIME"))
            conn.commit()

        # portal_users table — add tab_write column (per-user WRITE
        # grant matrix, independent of User Groups) if this DB file
        # predates it. Defaults to an empty list, same convention as
        # `groups` above — every existing user starts with NO write
        # grants until a founding admin/Management Team/Security Team
        # member explicitly assigns one.
        users_cols = [c["name"] for c in inspector.get_columns("portal_users")]
        if "tab_write" not in users_cols:
            conn.execute(text("ALTER TABLE portal_users ADD COLUMN tab_write JSON"))
            conn.commit()
            conn.execute(text("UPDATE portal_users SET tab_write = '[]' WHERE tab_write IS NULL"))
            conn.commit()

        # jobs table — add the AWS Support/AMS "Display ID" columns (short
        # numeric case number shown in the AWS Support Center UI, e.g.
        # "178548111300351") if this DB file predates them.
        jobs_cols = [c["name"] for c in inspector.get_columns("jobs")]
        _new_display_id_cols = (
            "aws_support_case_display_id", "aws_ams_sr_case_display_id", "aws_ams_incident_case_display_id",
        )
        for col_name in _new_display_id_cols:
            if col_name not in jobs_cols:
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col_name} VARCHAR"))
                conn.commit()

        # kb_documents table — created by init_db(), nothing to migrate yet

        # feedback_reports table — add the `replies` JSON column (threaded
        # conversation) if this DB file predates it (it originally shipped
        # with a single admin_reply/admin_replied_by_email/_name/_at set of
        # columns instead — those are simply left as unused dead columns on
        # legacy local dev DBs rather than dropped, since SQLite can't drop
        # columns without a full table rebuild and this is local-dev-only).
        feedback_cols = [c["name"] for c in inspector.get_columns("feedback_reports")]
        if "replies" not in feedback_cols:
            conn.execute(text("ALTER TABLE feedback_reports ADD COLUMN replies JSON"))
            conn.commit()
            conn.execute(text("UPDATE feedback_reports SET replies = '[]' WHERE replies IS NULL"))
            conn.commit()
        # ai_analysis: AI triage note for bug reports (local SQLite only;
        # DynamoDB just starts carrying the attribute).
        if "ai_analysis" not in feedback_cols:
            conn.execute(text("ALTER TABLE feedback_reports ADD COLUMN ai_analysis JSON"))
            conn.commit()

        # dns_audit_log table — add `batch_id` (correlates every row written
        # by one bulk file upload) if this DB file predates the bulk-import
        # feature. Only needed on the local SQLite path: the production
        # DynamoDB table is schemaless, so it simply starts carrying the new
        # attribute with no migration at all.
        if inspector.has_table("dns_audit_log"):
            dns_audit_cols = [c["name"] for c in inspector.get_columns("dns_audit_log")]
            if "batch_id" not in dns_audit_cols:
                conn.execute(text("ALTER TABLE dns_audit_log ADD COLUMN batch_id VARCHAR"))
                conn.commit()


# ── Constants ─────────────────────────────────────────────────────────────────

class _TeamMembersDict:
    """
    Dict-like view of the allowlisted portal users, backed live by
    user_store (DB / DynamoDB) instead of a hardcoded dict. Kept as a
    drop-in replacement for the old TEAM_MEMBERS dict so all existing
    call sites (TEAM_MEMBERS[x], .get(x), .items(), `x in TEAM_MEMBERS`)
    keep working unmodified while the underlying data now comes from the
    Admin-managed user_store (Task: multi-user allowlist + admin page).
    """

    def _snapshot(self) -> dict:
        return {u["email"]: u["name"] for u in user_store.list_users()}

    def __getitem__(self, key):
        return self._snapshot()[key]

    def __contains__(self, key):
        return user_store.is_allowed(key)

    def get(self, key, default=None):
        return self._snapshot().get(key, default)

    def items(self):
        return self._snapshot().items()

    def keys(self):
        return self._snapshot().keys()

    def values(self):
        return self._snapshot().values()


TEAM_MEMBERS = _TeamMembersDict()

VALID_CATEGORIES = ["Networking", "Compute", "Storage", "Security", "AD/Identity", "Other"]
VALID_PRIORITIES = ["ASAP", "High", "Medium", "Low"]

# Fields the "Sort By" filter (All Tickets tab) can sort on. The label is
# shown in the frontend dropdown; job_store.list_jobs() maps each key to
# the underlying column/attribute for both the SQLite and DynamoDB
# backends. PRIORITY_RANK gives "priority" a meaningful urgency order
# (ASAP highest) instead of sorting alphabetically.
JOB_SORT_FIELDS = {
    "created_at": "Created Date",
    "due_date": "Due Date",
    "ticket_number": "Ticket #",
    "title": "Title",
    "priority": "Priority",
    "status": "Status",
}
PRIORITY_RANK = {"ASAP": 0, "High": 1, "Medium": 2, "Low": 3}
VALID_STATUSES = [
    "Open", "Review by Engineer", "Pending Release", "Progress", "On Hold",
    "Fail and Rollback", "Resolved", "Pending Review", "Closed",
]

# Valid transitions map: current_status -> list of allowed next statuses
# Main path: Open -> Review by Engineer -> Pending Release -> Progress
#            -> Resolved -> Pending Review -> Closed
# On Hold is a pause point off Progress (resumes back into Progress).
# Fail and Rollback is reached from Progress or Pending Review and loops
# back to Progress so the fix can be redone.
STATUS_TRANSITIONS = {
    "Open":               ["Review by Engineer"],
    "Review by Engineer": ["Pending Release"],
    "Pending Release":    ["Progress"],
    "Progress":           ["On Hold", "Fail and Rollback", "Resolved"],
    "On Hold":            ["Progress"],
    "Fail and Rollback":  ["Progress"],
    "Resolved":           ["Pending Review"],
    "Pending Review":     ["Closed", "Fail and Rollback"],
    "Closed":             [],
}


# ── Pydantic Models ───────────────────────────────────────────────────────────

class CreateJobBody(BaseModel):
    title: str
    description: str
    category: str
    category_other: Optional[str] = None
    priority: str
    assignee_email: str
    due_date: Optional[str] = None


class UpdateJobBody(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    category_other: Optional[str] = None
    priority: Optional[str] = None
    assignee_email: Optional[str] = None
    due_date: Optional[str] = None


class StatusChangeBody(BaseModel):
    new_status: str


class EscalateBody(BaseModel):
    # "freshservice" | "aws_support" | "both" | "aws_ams_sr" | "aws_ams_incident"
    # aws_ams_sr / aws_ams_incident raise a case directly against the AWS
    # Managed Services (AMS) support account (100000000001) instead of
    # this account, using separate cross-account credentials.
    action: str
    ai_draft: dict     # the (possibly edited) AI draft to use


# ── Helpers ───────────────────────────────────────────────────────────────────
# NOTE: all Job/KBDocument persistence now goes through job_store.py, which
# already returns/accepts plain dicts (not ORM objects) and branches
# internally on ENV to pick SQLite (local) or DynamoDB (production). Route
# handlers below work with dicts uniformly regardless of backend.


def job_to_dict(j: dict) -> dict:
    """job_store already returns dicts in this exact shape — kept as a
    passthrough so existing call sites (and any future ones) don't need to
    care whether the value came from job_store directly or was constructed
    inline."""
    return j


def add_audit(job: dict, actor_email: str, event: str) -> dict:
    """Append an audit entry and persist the updated job. Returns the fresh
    job dict from the store (so callers see the true persisted state,
    including updated_at)."""
    actor_name = TEAM_MEMBERS.get(actor_email, actor_email)
    log = list(job.get("audit_log") or [])
    log.append({
        "timestamp": datetime.utcnow().isoformat(),
        "actor": actor_name,
        "actor_email": actor_email,
        "event": event,
    })
    updated = job_store.update_job_fields(job["id"], {"audit_log": log})
    return updated if updated else job


def get_actor_local(x_actor_email: Optional[str] = Header(default=None)) -> str:
    """
    Local mock auth: frontend sends the selected user's email in X-Actor-Email header.
    """
    if not x_actor_email or x_actor_email not in TEAM_MEMBERS:
        raise HTTPException(401, "Unauthorised — invalid or missing X-Actor-Email header")
    return x_actor_email


if ENV == "local":
    get_actor = get_actor_local
else:
    # Production: verify the ALB-injected Azure AD OIDC JWT instead of the
    # local mock header (Task 5.12 follow-up — see auth_alb.py).
    from .auth_alb import get_actor as get_actor_alb
    get_actor = get_actor_alb


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/api/auth/team")
def get_team():
    """Return team members for local mock login dropdown."""
    return [{"email": e, "name": n} for e, n in TEAM_MEMBERS.items()]


@app.post("/api/auth/login")
def mock_login(body: dict):
    """
    Local mock SSO — accepts any allowlisted team email.
    Production: replace with Azure AD OIDC callback handler.
    """
    email = body.get("email", "").lower().strip()
    if email not in TEAM_MEMBERS:
        raise HTTPException(401, "Unauthorised — not a permitted team member")
    user_store.update_last_login(email)
    return {
        "email": email, "name": TEAM_MEMBERS[email], "is_admin": user_store.is_admin(email),
        "photo_data_url": user_store.get_photo_url(email),
    }


@app.get("/api/auth/whoami")
def whoami(actor: str = Depends(get_actor)):
    """
    Task 12: identity endpoint for the deployed frontend. In production
    (ENV != local) this only succeeds if the request carries a valid
    ALB-injected Azure AD OIDC session (get_actor -> auth_alb.get_actor);
    a 401 here means the browser hasn't completed the ALB login flow yet
    (see GET / below), which the SPA's AuthProvider treats as "not logged
    in" rather than an error.
    """
    return {
        "email": actor,
        "name": TEAM_MEMBERS.get(actor, actor),
        "is_admin": user_store.is_admin(actor),
        "photo_data_url": user_store.get_photo_url(actor),
        # Founding admin — can never be locked out;
        # see user_store.SEED_ADMIN_EMAILS. Portal founder (optional)
        # — a distinct, singular designation, purely for display; see
        # user_store.PORTAL_FOUNDER_EMAILS.
        "is_founding_admin": user_store.is_seed_admin(actor),
        "is_portal_founder": user_store.is_portal_founder(actor),
        # "User Admin" role (narrower than is_admin — can add new portal
        # users and see only the ones they added) and whether this actor
        # is a "portal authority" (founding admin, or on the Management
        # Team / Security Team) who may make authorization/delegation
        # decisions for other users — Group membership, group->category
        # access, per-user tab WRITE grants, and promoting/demoting
        # Admin/User Admin. The frontend uses these instead of
        # hardcoding founding-admin emails.
        "is_user_admin": user_store.is_user_admin(actor),
        "is_portal_authority": user_store.is_portal_authority(actor),
        # Cloud Accounts page: founding admins + Management Team only.
        "can_manage_cloud_accounts": user_store.is_cloud_accounts_manager(actor),
        # Per-user tab WRITE grants (see PortalUser.tab_write's docstring
        # in database.py) — the frontend uses this to show/hide
        # write-triggering buttons (e.g. "+ New Ticket") without a
        # second API round-trip. Admins implicitly have every tab_id
        # here regardless of what's actually stored, since is_admin()
        # always passes require_tab_write() — same "admin can never be
        # locked out" precedent used everywhere else in this app.
        "tab_write": (
            [tab["id"] for tab in settings_store.TAB_REGISTRY]
            if user_store.is_admin(actor) else user_store.get_user_tab_write(actor)
        ),
    }


# ── Admin: manage portal users/permissions ───────────────────────────────────

def require_whitelist_audit_viewer(actor: str = Depends(get_actor)) -> str:
    """FastAPI dependency: 403 unless the caller is an admin OR belongs
    to at least one of the three User Groups explicitly named for this
    audit log (Cloud Infra Team / Security Team / Management Team) — per
    the user's explicit requirement that these records are NOT visible
    to every portal user, unlike every other "read-only, visible to
    everyone" feature in this app. This is the first place in the
    codebase that gates DATA (not a whole tab/route) by group
    membership — see settings_store.tabs_granted_by_groups() for the
    existing tab-level equivalent this deliberately does NOT reuse
    (that function grants TAB visibility; this function must still deny
    access even if 'whitelist_lms' the TAB is visible to everyone, since
    the audit log is a sub-resource of that tab with its own, narrower
    rule)."""
    if user_store.is_admin(actor):
        return actor
    allowed_groups = {"cloud_infra_team", "security_team", "management_team"}
    if not (set(user_store.get_user_groups(actor)) & allowed_groups):
        raise HTTPException(403, "Not authorized to view the Whitelist audit log")
    return actor


def require_admin(actor: str = Depends(get_actor)) -> str:
    """FastAPI dependency: 403 if the caller is not an admin."""
    if not user_store.is_admin(actor):
        raise HTTPException(403, "Admin access required")
    return actor


def require_cloud_accounts_manager(actor: str = Depends(get_actor)) -> str:
    """FastAPI dependency: 403 unless the caller may view/change the Cloud Accounts
    page — founding admins and Management Team members only
    (user_store.is_cloud_accounts_manager()). A plain admin is NOT enough."""
    if not user_store.is_cloud_accounts_manager(actor):
        raise HTTPException(403, "Cloud Accounts is restricted to founding admins and the Management Team")
    return actor


def require_user_admin(actor: str = Depends(get_actor)) -> str:
    """FastAPI dependency: 403 unless the caller is a full admin OR holds
    the narrower "User Admin" role (user_store.is_user_admin()). Gates
    ONLY the "add a new user" action (admin_add_user) and the "list
    users" action (admin_list_users, which self-filters what a plain
    User Admin sees — see that route) — every other /api/admin/* route
    stays on require_admin above, since a User Admin should not be able
    to export data, rotate connected-portal credentials, edit the
    freeze notice, manage cost adjustments, etc."""
    if not user_store.is_user_admin(actor):
        raise HTTPException(403, "User Admin access required")
    return actor


def require_portal_authority(actor: str = Depends(get_actor)) -> str:
    """FastAPI dependency: 403 unless the caller is a "portal authority"
    (user_store.is_portal_authority() — founding admin, or a member of
    the Management Team / Security Team User Groups), per the user's
    explicit, repeated choice through this feature's design. Gates every
    authorization/delegation decision in the portal: User Group
    membership changes, group->category access changes, per-user tab
    WRITE grants, and promoting/demoting a user to/from "admin" or
    "user_admin". Deliberately stricter than require_admin: a plain
    "admin" store row does not automatically qualify unless they are
    also a founding admin or on one of those two teams.

    Named require_user_admin_manager in an earlier iteration of this
    feature when it only gated Admin/User Admin role changes; kept as
    an importable alias below for anything still referencing that name."""
    if not user_store.is_portal_authority(actor):
        raise HTTPException(
            403,
            "Only founding admins, the Management Team, or the Security Team may perform this action",
        )
    return actor


# Backward-compatible alias — see require_portal_authority()'s docstring.
require_user_admin_manager = require_portal_authority


def require_tab_write(tab_id: str):
    """FastAPI dependency FACTORY: returns a dependency that 403s unless
    the caller may perform tab_id's write action — user_store.is_admin()
    (full admins always may) OR tab_id is in their individual
    tab_write grant list (user_store.get_user_tab_write() — see
    PortalUser.tab_write's docstring in database.py). Deliberately does
    NOT consult User Group membership at all: per the user's explicit
    choice, WRITE is never inherited from a group the way tab
    visibility (READ) is — every write grant is an individual decision
    made by a portal authority (require_portal_authority above).

    Use as Depends(require_tab_write("jobs")), NOT
    Depends(require_tab_write) — this returns the actual dependency
    function, it is not one itself.
    """
    def _dependency(actor: str = Depends(get_actor)) -> str:
        if user_store.is_admin(actor):
            return actor
        if tab_id not in user_store.get_user_tab_write(actor):
            raise HTTPException(
                403,
                f"You do not have write access to this feature — ask a founding admin, "
                f"the Management Team, or the Security Team to grant it.",
            )
        return actor
    return _dependency


# The three User Groups (settings_store.GROUP_REGISTRY) trusted with the
# DNS Record tab's privileged operations, shared by both dependencies
# below. Same three groups as require_whitelist_audit_viewer() uses, but
# named once here because the DNS tab consults them from two places
# (writes AND audit reads) and the two must never drift apart.
_DNS_PRIVILEGED_GROUPS = {"cloud_infra_team", "security_team", "management_team"}


def require_dns_writer(actor: str = Depends(get_actor)) -> str:
    """FastAPI dependency: 403 unless the caller may CHANGE production DNS.

    Deliberately stricter than Whitelist (LMS), whose /check and /add are
    open to any authenticated user. Adding an IP to an allowlist is a
    contained, reversible act; writing to these Route 53 zones can take
    example-site-a.com or example-site-c.com off the air, so the
    blast radius does not justify the same openness. Restricted to admins
    plus the Cloud Infra / Security / Management Teams, per the user's
    explicit choice when this tab was specified.

    READS are NOT gated by this — listing zones and records goes through
    plain get_actor plus the ordinary 'dns_records' tab flag, so anyone
    who can see the tab can look up DNS, while only these groups can
    change it.

    ALSO grants access if 'dns_records' is in the caller's individual
    tab_write list (user_store.get_user_tab_write()) — this is how a
    portal authority (require_portal_authority) can grant DNS write to
    someone OUTSIDE the three groups above, e.g. a new joiner who is on
    the Security Team but not yet trusted with DNS changes: they get
    'dns_records' read via their group as normal, but write is withheld
    until individually granted here. The two checks are OR'd — being in
    a privileged group is still sufficient on its own, exactly as before.
    """
    if user_store.is_admin(actor):
        return actor
    if set(user_store.get_user_groups(actor)) & _DNS_PRIVILEGED_GROUPS:
        return actor
    if "dns_records" in user_store.get_user_tab_write(actor):
        return actor
    raise HTTPException(
        403,
        "Not authorized to change DNS records — this is limited to admins, the "
        "Cloud Infra, Security, and Management Teams, or users individually granted write access.",
    )


def require_dns_audit_viewer(actor: str = Depends(get_actor)) -> str:
    """FastAPI dependency: 403 unless the caller may read the DNS audit log.

    Same rule and same rationale as require_whitelist_audit_viewer() — a
    sub-resource of a tab with its own, narrower rule, so it must still
    deny access even when the 'dns_records' TAB itself is visible to
    everyone. Kept as a separate function rather than reusing the
    whitelist one so that either audit log's membership can be changed
    later without silently changing the other's.
    """
    if user_store.is_admin(actor):
        return actor
    if not (set(user_store.get_user_groups(actor)) & _DNS_PRIVILEGED_GROUPS):
        raise HTTPException(403, "Not authorized to view the DNS audit log")
    return actor


# Narrower than _DNS_PRIVILEGED_GROUPS — S2S VPN Profile Creation writes
# real Transit Gateway/Customer Gateway/VPN connection infrastructure in
# the network account, which is a Cloud Infra / Security Team action, not
# something Management Team would ever need to do (unlike DNS, which
# Management Team is included on). Shared by both dependencies below so
# writes and audit-log reads never drift apart, same convention as
# _DNS_PRIVILEGED_GROUPS.
_S2S_VPN_PRIVILEGED_GROUPS = {"cloud_infra_team", "security_team"}


def require_vpn_writer(actor: str = Depends(get_actor)) -> str:
    """FastAPI dependency: 403 unless the caller may CREATE real S2S VPN
    infrastructure (Customer Gateways, VPN connections, Transit Gateway
    static routes) in the network account.

    Restricted to admins plus the Cloud Infra / Security Teams — this
    creates real, billable, hard-to-fully-undo AWS network resources
    (same risk class as DNS writes, arguably higher: a bad Transit
    Gateway route can black-hole traffic across every VPC attached to
    that TGW, not just one domain), so the blast radius does not justify
    opening this to every logged-in user.

    READS (listing existing Customer Gateways for the "reuse an existing
    one?" popup) are NOT gated by this — they go through plain get_actor
    plus the ordinary 's2s_vpn_profile_creation' tab flag, mirroring the
    DNS tab's read/write asymmetry.

    ALSO grants access if 's2s_vpn_profile_creation' is in the caller's
    individual tab_write list (user_store.get_user_tab_write()) — same
    OR'd exception mechanism as require_dns_writer() above, letting a
    portal authority grant this specific write to someone outside the
    Cloud Infra/Security Teams without changing their group membership.
    """
    if user_store.is_admin(actor):
        return actor
    if set(user_store.get_user_groups(actor)) & _S2S_VPN_PRIVILEGED_GROUPS:
        return actor
    if "s2s_vpn_profile_creation" in user_store.get_user_tab_write(actor):
        return actor
    raise HTTPException(
        403,
        "Not authorized to create S2S VPN infrastructure — this is limited to admins, the "
        "Cloud Infra and Security Teams, or users individually granted write access.",
    )


def require_s2s_vpn_audit_viewer(actor: str = Depends(get_actor)) -> str:
    """FastAPI dependency: 403 unless the caller may read the S2S VPN
    Profile Creation audit log. Same rule as require_vpn_writer() above,
    kept as a separate function (rather than reused directly) so either
    rule's membership can be changed later without silently changing the
    other's — same convention as require_dns_audit_viewer()."""
    if user_store.is_admin(actor):
        return actor
    if not (set(user_store.get_user_groups(actor)) & _S2S_VPN_PRIVILEGED_GROUPS):
        raise HTTPException(403, "Not authorized to view the S2S VPN Profile Creation audit log")
    return actor


class AddUserBody(BaseModel):
    email: str
    name: str
    role: str = "member"   # "admin" | "user_admin" | "member"


class UpdateUserRoleBody(BaseModel):
    role: str   # "admin" | "user_admin" | "member"


class UpdateUserGroupsBody(BaseModel):
    groups: list[str]   # group_ids from settings_store.GROUP_REGISTRY


# Roles someone WITHOUT is_portal_authority() (i.e. not a founding
# admin and not on the Management/Security Team) may assign when adding
# a new user — "member" only. Deciding who becomes "admin" OR
# "user_admin" is exclusively a portal-authority decision, per the
# user's explicit request, so even a plain full "admin" (one who is
# themselves neither a founding admin nor on either team) cannot hand
# out either of those two roles here — same restriction as
# admin_update_user_role/admin_update_user_user_admin_role below. Kept
# as a constant so admin_add_user's check can't drift from this comment.
_UNRESTRICTED_ASSIGNABLE_ROLES = {"member"}


@app.get("/api/admin/users")
def admin_list_users(actor: str = Depends(require_user_admin)):
    """List portal users. Full admins see everyone; a plain "User Admin"
    (not also a full admin) sees only themselves plus the users they
    personally added (added_by == their own email) — per the user's
    explicit choice that User Admins should be scoped to "their team"
    rather than the whole allowlist."""
    all_users = user_store.list_users()
    if user_store.is_admin(actor):
        return {"users": all_users}
    actor_norm = actor.lower().strip()
    visible = [u for u in all_users if u["email"] == actor_norm or (u.get("added_by") or "").lower().strip() == actor_norm]
    return {"users": visible}


def _get_presence_for_email(email: str) -> str:
    """job-portal-chat-voice Requirement 8.1-8.4: resolve one
    Team_Member's current Presence state ("online" | "in_call" |
    "offline") for inclusion in a REST response snapshot (the user
    directory, and any future participant-list endpoint). Thin wrapper
    around ws_connections.get_presence_state() + call_store's
    in-active-call flag — see ws_connections.py's own docstring for why
    those two modules don't import each other directly. Function-local
    import (not module-level) for the same reason the WS route below
    imports ws_connections locally: this module is defined far below
    main.py's module-level `from . import chat_store, call_store` block
    (line ~6891), so a module-level import here would be a forward
    reference at class-definition/decoration time."""
    from . import call_store, ws_connections

    in_call = call_store.is_user_in_any_active_call(email)
    return ws_connections.get_presence_state(email, in_active_call=in_call)


@app.get("/api/users/directory")
def list_users_directory(actor: str = Depends(get_actor)):
    """job-portal-chat-voice Requirement 1: any logged-in user (NOT
    admin-gated, unlike admin_list_users above) may look up the full
    portal user directory to start a new chat/call — used by the
    "new conversation" / "invite to call" picker. Deliberately strips
    every admin-only field (role/added_by/groups/last_login/tab_write)
    down to just what the chat UI needs to render a contact: email,
    name, photo_data_url.

    Requirement 8.4: each entry also carries a `presence` snapshot
    ("online" | "in_call" | "offline") at the moment of this request —
    the frontend's ChatContext treats this as the INITIAL presence map
    and layers live `presence.update` WS events (see websocket_route
    below) and its own periodic re-fetch of this same endpoint on top,
    since a `presence.update` broadcast only reaches WS connections
    held by the SAME ECS task that produced it (no cross-task pub/sub
    broker exists per design.md's fan-out design — see
    websocket_route's own comment on this)."""
    return {
        "users": [
            {
                "email": u["email"],
                "name": u.get("name") or u["email"],
                "photo_data_url": u.get("photo_data_url"),
                "presence": _get_presence_for_email(u["email"]),
            }
            for u in user_store.list_users()
        ]
    }


@app.post("/api/admin/users", status_code=201)
def admin_add_user(body: AddUserBody, actor: str = Depends(require_user_admin)):
    """Add a new user to the portal allowlist. Anyone with User Admin
    (or full admin) access may add a new "member" — but assigning the
    new user "admin" or "user_admin" outright at add-time is restricted
    to founding admins / Management Team (is_user_admin_manager), same
    as promoting/demoting an EXISTING user's role via
    admin_update_user_role / admin_update_user_user_admin_role below —
    per the user's explicit request that only those two groups may
    decide who becomes Admin or User Admin, full stop, not just via the
    dedicated toggle buttons."""
    email = body.email.lower().strip()
    if not email or "@" not in email:
        raise HTTPException(400, "A valid email address is required")
    if body.role not in ("admin", "user_admin", "member"):
        raise HTTPException(400, "role must be 'admin', 'user_admin', or 'member'")
    if body.role not in _UNRESTRICTED_ASSIGNABLE_ROLES and not user_store.is_portal_authority(actor):
        raise HTTPException(
            403,
            "Only founding admins, the Management Team, or the Security Team may add a user as "
            "Admin or User Admin — add them as a member first, then use the role toggle.",
        )
    name = body.name.strip() or email
    user = user_store.upsert_user(email=email, name=name, role=body.role, added_by=actor)
    return user


@app.patch("/api/admin/users/{email}")
def admin_update_user_role(email: str, body: UpdateUserRoleBody, actor: str = Depends(require_portal_authority)):
    """Change a user's role to "admin" or "member" — restricted to
    portal authorities (founding admin / Management Team / Security
    Team — require_portal_authority), NOT every plain full admin, per
    the user's explicit follow-up request. Promoting/demoting the
    "user_admin" role specifically is handled by
    admin_update_user_user_admin_role below, gated the same way — kept
    as a separate endpoint rather than folding it into this one so the
    two role dimensions can't drift apart."""
    if body.role not in ("admin", "member"):
        raise HTTPException(400, "role must be 'admin' or 'member' — use the User Admin toggle for that role")
    existing = user_store.get_user(email)
    is_seed = user_store.is_seed_admin(email)
    if not existing and not is_seed:
        raise HTTPException(404, "User not found")
    name = existing["name"] if existing else user_store.get_display_name(email)
    if is_seed and body.role != "admin":
        raise HTTPException(400, "Cannot demote a founding admin (the founding admins)")
    user = user_store.upsert_user(email=email.lower().strip(), name=name, role=body.role, added_by=actor)
    return user


class UpdateUserUserAdminRoleBody(BaseModel):
    is_user_admin: bool


@app.patch("/api/admin/users/{email}/user-admin-role")
def admin_update_user_user_admin_role(
    email: str, body: UpdateUserUserAdminRoleBody, actor: str = Depends(require_portal_authority),
):
    """Grant or revoke the narrower "User Admin" role — restricted to
    portal authorities (founding admin / Management Team / Security
    Team — require_portal_authority), per the user's explicit choice.
    No-ops (returns the user unchanged) if the target is already a full
    "admin", since User Admin is strictly weaker and granting it would
    be meaningless; likewise revoking it from a non-user_admin is a
    no-op."""
    email = email.lower().strip()
    existing = user_store.get_user(email)
    is_seed = user_store.is_seed_admin(email)
    if not existing and not is_seed:
        raise HTTPException(404, "User not found")
    current_role = (existing or {}).get("role") or ("admin" if is_seed else "member")
    if current_role == "admin":
        return existing or {"email": email, "role": "admin"}
    new_role = "user_admin" if body.is_user_admin else "member"
    if current_role == new_role:
        return existing
    name = existing["name"] if existing else user_store.get_display_name(email)
    return user_store.upsert_user(email=email, name=name, role=new_role, added_by=actor)


@app.delete("/api/admin/users/{email}")
def admin_remove_user(email: str, actor: str = Depends(require_admin)):
    """Remove a user's portal access (admin only)."""
    email = email.lower().strip()
    if user_store.is_seed_admin(email):
        raise HTTPException(400, "Cannot remove a founding admin (the founding admins)")
    if email == actor.lower().strip():
        raise HTTPException(400, "You cannot remove your own access")
    removed = user_store.delete_user(email)
    if not removed:
        raise HTTPException(404, "User not found")
    return {"removed": email}


# ── User Groups (Security Team / Infra Team / Cloud Infra Team /
# Management Team / Billing Team) ─────────────────────────────────────────
# A second, additive layer on top of the per-role Tab Access Control
# above — lets an admin put users into fixed teams and grant each team
# access to whole sidebar CATEGORIES. See settings_store.py's "User
# Groups & Category Access" section docstring for the full semantics
# (group grants are purely additive, never remove role-based access).

@app.get("/api/admin/settings/groups")
def admin_get_groups(actor: str = Depends(require_admin)):
    """Group registry + category registry + the current group->category
    access matrix + the full tab registry — used to render the Admin
    page's User Groups section AND its per-user tab_write "Can Do"
    checklist (the tab registry has id+label pairs the frontend needs
    to render checkbox labels; my-tab-access only returns a bool dict
    without labels, and adminGetGroups was already the closest existing
    endpoint that a portal authority hits when opening this UI)."""
    return {
        "groups": settings_store.GROUP_REGISTRY,
        "categories": settings_store.CATEGORY_REGISTRY,
        "access": settings_store.get_group_category_access(),
        "tabs": settings_store.TAB_REGISTRY,
    }


class GroupCategoryAccessBody(BaseModel):
    access: dict  # {group_id: [category_id, ...]}


@app.put("/api/admin/settings/groups")
def admin_set_group_category_access(body: GroupCategoryAccessBody, actor: str = Depends(require_portal_authority)):
    """Update which categories each group is granted — restricted to
    portal authorities (founding admin / Management Team / Security
    Team), NOT every plain full admin, per the user's explicit request
    that this decision sits with the same trusted set as Admin/User
    Admin role changes and per-user tab WRITE grants."""
    return settings_store.set_group_category_access(
        body.access, actor_email=actor, actor_name=TEAM_MEMBERS.get(actor, actor),
    )


@app.patch("/api/admin/users/{email}/groups")
def admin_update_user_groups(email: str, body: UpdateUserGroupsBody, actor: str = Depends(require_portal_authority)):
    """Set which groups a user belongs to — restricted to portal
    authorities (founding admin / Management Team / Security Team), NOT
    every plain full admin, same rationale as
    admin_set_group_category_access above. Unknown group_ids are
    rejected outright (rather than silently dropped) so a typo in a
    future API caller surfaces immediately instead of quietly no-op'ing."""
    email = email.lower().strip()
    unknown = [g for g in body.groups if g not in settings_store.GROUP_IDS]
    if unknown:
        raise HTTPException(400, f"Unknown group_id(s): {', '.join(unknown)}")
    if not user_store.is_allowed(email):
        raise HTTPException(404, "User not found")
    user = user_store.set_user_groups(email, body.groups, actor)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


# ── Per-user tab WRITE grants ─────────────────────────────────────────────
# A THIRD, independent layer on top of role (admin/user_admin/member) and
# User Group membership above. Group membership only ever grants READ
# (tab visibility) — every member of a group sees the same tabs as their
# teammates. WRITE (the ability to perform whatever write action a given
# tab has — create a ticket, edit a DNS record, etc.) is never inherited
# from group membership; it is always an individual grant made by a
# portal authority (founding admin / Management Team / Security Team),
# per the user's explicit choice. Applies to every tab_id in
# settings_store.TAB_REGISTRY — most tabs have no write action to gate
# at all today (see main.py's require_tab_write() for which ones
# currently do), but the grant list itself is intentionally general so a
# future tab's write action can be gated the same way without another
# schema change. dns_records and s2s_vpn_profile_creation are a partial
# exception: their EXISTING group-based writer check
# (_DNS_PRIVILEGED_GROUPS / _S2S_VPN_PRIVILEGED_GROUPS) still applies
# too, OR'd with this list — so a Security/Cloud Infra/Management Team
# member keeps write access via their group as before, and this list is
# how someone OUTSIDE those groups can still be individually granted
# write on those two tabs.

class UpdateUserTabWriteBody(BaseModel):
    tab_write: list[str]   # tab_ids from settings_store.TAB_REGISTRY


@app.patch("/api/admin/users/{email}/tab-write")
def admin_update_user_tab_write(email: str, body: UpdateUserTabWriteBody, actor: str = Depends(require_portal_authority)):
    """Set which tabs a user has WRITE access to — restricted to portal
    authorities (founding admin / Management Team / Security Team),
    same rationale as admin_update_user_groups above. Unknown tab_ids
    are rejected outright, same convention as admin_update_user_groups's
    unknown group_id handling."""
    email = email.lower().strip()
    unknown = [t for t in body.tab_write if t not in settings_store.TAB_IDS]
    if unknown:
        raise HTTPException(400, f"Unknown tab_id(s): {', '.join(unknown)}")
    if not user_store.is_allowed(email):
        raise HTTPException(404, "User not found")
    user = user_store.set_user_tab_write(email, body.tab_write, actor)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


# ── Profile photos ────────────────────────────────────────────────────────────
# Self-service (any logged-in user, for their own account) + admin-managed
# (an admin can set/clear ANY user's photo, e.g. uploading one on behalf
# of a teammate who hasn't done it themselves yet). No S3 bucket exists
# in this portal's infra — see app/profile_photo.py's docstring — so
# photos are resized to a small JPEG and stored inline as a
# "data:image/jpeg;base64,..." string directly on the user row.

@app.post("/api/profile/photo")
async def upload_my_profile_photo(
    file: UploadFile = File(...),
    actor: str = Depends(get_actor),
):
    """Upload/replace the caller's own profile photo."""
    from .profile_photo import process_upload, ProfilePhotoError

    content = await file.read()
    try:
        data_url = process_upload(content)
    except ProfilePhotoError as e:
        raise HTTPException(400, str(e))

    user = user_store.set_user_photo(actor, data_url, actor)
    if user is None:
        # Should not normally happen (the caller is, by definition, an
        # authenticated allowlisted user) — but guard against a store
        # inconsistency rather than a raw 500.
        raise HTTPException(404, "Your user record could not be found")
    return {"email": actor, "photo_data_url": user["photo_data_url"]}


@app.delete("/api/profile/photo")
def remove_my_profile_photo(actor: str = Depends(get_actor)):
    """Remove the caller's own profile photo (revert to the initials
    placeholder shown in the sidebar)."""
    user = user_store.set_user_photo(actor, None, actor)
    if user is None:
        raise HTTPException(404, "Your user record could not be found")
    return {"email": actor, "photo_data_url": None}


@app.post("/api/admin/users/{email}/photo")
async def admin_upload_user_photo(
    email: str,
    file: UploadFile = File(...),
    actor: str = Depends(require_admin),
):
    """Set/replace another user's profile photo (admin only) — e.g. an
    admin uploading a headshot on behalf of a teammate."""
    from .profile_photo import process_upload, ProfilePhotoError

    email = email.lower().strip()
    if not user_store.is_allowed(email):
        raise HTTPException(404, "User not found")

    content = await file.read()
    try:
        data_url = process_upload(content)
    except ProfilePhotoError as e:
        raise HTTPException(400, str(e))

    user = user_store.set_user_photo(email, data_url, actor)
    if user is None:
        raise HTTPException(404, "User not found")
    return {"email": email, "photo_data_url": user["photo_data_url"]}


@app.delete("/api/admin/users/{email}/photo")
def admin_remove_user_photo(email: str, actor: str = Depends(require_admin)):
    """Clear another user's profile photo (admin only)."""
    email = email.lower().strip()
    if not user_store.is_allowed(email):
        raise HTTPException(404, "User not found")
    user = user_store.set_user_photo(email, None, actor)
    if user is None:
        raise HTTPException(404, "User not found")
    return {"email": email, "photo_data_url": None}


# ── Site settings: Production Freeze banner (yellow notice) ─────────────────
# Lets an admin edit the message text and toggle the banner's visibility
# from the Admin page instead of needing a frontend code change + deploy
# every time the freeze window dates change or the notice needs updating.

class FreezeNoticeBody(BaseModel):
    enabled: bool
    message: str


@app.get("/api/settings/freeze-notice")
def get_freeze_notice(actor: str = Depends(get_actor)):
    """Any logged-in user can read the current banner content/visibility
    — this is what ProductionFreezeNotice.jsx polls on every page load."""
    return settings_store.get_freeze_notice()


@app.get("/api/admin/settings/freeze-notice")
def admin_get_freeze_notice(actor: str = Depends(require_admin)):
    """Same content as the public endpoint above, but admin-gated — used
    by the Admin page's edit form so it doesn't need a second code path."""
    return settings_store.get_freeze_notice()


@app.put("/api/admin/settings/freeze-notice")
def admin_set_freeze_notice(body: FreezeNoticeBody, actor: str = Depends(require_admin)):
    """Update the freeze banner's message and/or enabled flag (admin only)."""
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "message must not be empty")
    if len(message) > 4000:
        raise HTTPException(400, "message must be 4000 characters or fewer")
    return settings_store.set_freeze_notice(
        enabled=body.enabled,
        message=message,
        actor_email=actor,
        actor_name=TEAM_MEMBERS.get(actor, actor),
    )


# ── Connected Portal Credential Updates (Admin page) ─────────────────────────
# Lets an admin rotate the credentials for the two external "connected
# portal" systems this backend proxies to on the user's behalf
# (Freshservice Change Calendar: API key; SR Ticket Information:
# username + password) without needing an out-of-band
# `aws secretsmanager put-secret-value` call every time one changes.
# See app/connected_portal_credentials.py.

class ConnectedPortalCredentialsUpdateBody(BaseModel):
    # Which fields apply depends on the system's auth_type (see
    # connected_portal_credentials.py): Freshservice Change Calendar takes
    # api_key only; SR Ticket Information takes username + password.
    # Fields that don't apply to a system are rejected with a 400.
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None


@app.get("/api/admin/connected-portals")
def admin_list_connected_portals(actor: str = Depends(require_admin)):
    """Each connected system's auth_type and non-secret state (username
    for a password system, api_key_configured yes/no for an API-key
    system) — never a password or key, so the Admin page can show
    what's configured without round-tripping a secret to the browser."""
    return {"systems": connected_portal_credentials.list_systems()}


@app.put("/api/admin/connected-portals/{system_id}")
def admin_update_connected_portal(
    system_id: str, body: ConnectedPortalCredentialsUpdateBody, actor: str = Depends(require_admin)
):
    """Overwrites the given system's Secrets Manager credentials and
    forces this ECS task to re-authenticate with them immediately."""
    try:
        return connected_portal_credentials.update_credentials(
            system_id, body.username, body.password, body.api_key
        )
    except connected_portal_credentials.ConnectedPortalCredentialsError as e:
        raise HTTPException(400, e.message)


# ── Cloud Accounts (Admin page) ──────────────────────────────────────────────
# Onboard / verify / rename / offboard the AWS accounts and Azure subscriptions
# the Portal reports on. Every route is restricted to founding admins and the
# Management Team (require_cloud_accounts_manager) and thin: the logic lives in
# app/cloud_account_service.py (see .kiro/specs/cloud-account-onboarding). The
# External_Id only ever leaves in the prepare / setup / template responses, which
# are marked no-store. Route order matters: the fixed paths come before the
# /{provider}/{identifier} ones.

from fastapi import Response as _FastApiResponse
from . import cloud_account_service as _cloud_accounts


def _cloud_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except _cloud_accounts.ServiceError as e:
        raise HTTPException(e.status, e.message)


def _no_store(response: _FastApiResponse) -> None:
    response.headers["Cache-Control"] = "no-store"


class CloudAccountPrepareBody(BaseModel):
    account_id: str


class CloudAccountConnectBody(BaseModel):
    display_name: str
    business_unit: Optional[str] = None
    confirm_new_business_unit: bool = False


class CloudAzureAddBody(BaseModel):
    subscription_id: str
    display_name: str
    business_unit: Optional[str] = None
    confirm_new_business_unit: bool = False


class CloudAccountUpdateBody(BaseModel):
    display_name: Optional[str] = None
    business_unit: Optional[str] = None
    scope_core: Optional[bool] = None
    scope_billing: Optional[bool] = None
    confirm_new_business_unit: bool = False


class CloudAccountOffboardBody(BaseModel):
    risk_confirmation: Optional[str] = None


@app.get("/api/admin/cloud-accounts")
def admin_list_cloud_accounts(actor: str = Depends(require_cloud_accounts_manager)):
    """Every registry entry (any status) with counts per provider and status. Never includes an External_Id."""
    return _cloud_call(_cloud_accounts.list_with_counts)


@app.get("/api/admin/cloud-accounts/azure/guide")
def admin_azure_guide(subscription_id: Optional[str] = None, actor: str = Depends(require_cloud_accounts_manager)):
    """The Service Principal's application ID and the Azure CLI command that grants it Reader on a subscription."""
    from . import azure_monitor

    try:
        return azure_monitor.azure_guide(subscription_id)
    # AzureError is a ValueError subclass, so it must be caught first.
    except azure_monitor.AzureError:
        raise HTTPException(503, "The Portal's Azure credentials are not configured.")
    except ValueError:
        raise HTTPException(400, "An Azure subscription ID is a GUID like 00000000-0000-0000-0000-000000000000.")


@app.post("/api/admin/cloud-accounts/test-all")
def admin_test_all_cloud_accounts(actor: str = Depends(require_cloud_accounts_manager)):
    """Re-test every non-offboarded account (parallel, bounded)."""
    return _cloud_call(_cloud_accounts.test_all, actor)


@app.post("/api/admin/cloud-accounts/aws/prepare")
def admin_prepare_aws_account(body: CloudAccountPrepareBody, response: _FastApiResponse, actor: str = Depends(require_cloud_accounts_manager)):
    """Start adding an AWS account: returns its role name and External_Id (same value on every repeat)."""
    _no_store(response)
    return _cloud_call(_cloud_accounts.prepare_aws, body.account_id, actor)


@app.get("/api/admin/cloud-accounts/aws/{account_id}/setup")
def admin_aws_account_setup(account_id: str, response: _FastApiResponse, actor: str = Depends(require_cloud_accounts_manager)):
    """The setup data of an existing draft (reopening the setup screen)."""
    _no_store(response)
    return _cloud_call(_cloud_accounts.get_setup, account_id)


@app.get("/api/admin/cloud-accounts/aws/{account_id}/template")
def admin_aws_account_template(account_id: str, actor: str = Depends(require_cloud_accounts_manager)):
    """Download the CloudFormation template for this account (read-only role)."""
    filename, text = _cloud_call(_cloud_accounts.template_for, account_id, actor)
    return _FastApiResponse(
        content=text,
        media_type="application/x-yaml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@app.post("/api/admin/cloud-accounts/aws/{account_id}/connect")
def admin_connect_aws_account(account_id: str, body: CloudAccountConnectBody, actor: str = Depends(require_cloud_accounts_manager)):
    """Save the name / BU and run the connection test."""
    return _cloud_call(
        _cloud_accounts.connect_aws, account_id, body.display_name, actor,
        business_unit=body.business_unit, confirm_new_bu=body.confirm_new_business_unit,
    )


@app.post("/api/admin/cloud-accounts/azure")
def admin_add_azure_subscription(body: CloudAzureAddBody, actor: str = Depends(require_cloud_accounts_manager)):
    """Register an Azure subscription and run the connection test."""
    return _cloud_call(
        _cloud_accounts.add_azure, body.subscription_id, body.display_name, actor,
        business_unit=body.business_unit, confirm_new_bu=body.confirm_new_business_unit,
    )


@app.get("/api/admin/cloud-accounts/{provider}/{identifier}")
def admin_get_cloud_account(provider: str, identifier: str, actor: str = Depends(require_cloud_accounts_manager)):
    """One entry with its latest audit entries."""
    return _cloud_call(_cloud_accounts.detail, provider, identifier)


@app.patch("/api/admin/cloud-accounts/{provider}/{identifier}")
def admin_update_cloud_account(provider: str, identifier: str, body: CloudAccountUpdateBody, actor: str = Depends(require_cloud_accounts_manager)):
    """Rename, change the Business Unit, or change the AWS scopes. Changes nothing in the cloud account."""
    changes = body.model_dump(exclude_unset=True)
    confirm = bool(changes.pop("confirm_new_business_unit", False))
    return _cloud_call(_cloud_accounts.update, provider, identifier, changes, actor, confirm_new_bu=confirm)


@app.post("/api/admin/cloud-accounts/{provider}/{identifier}/test")
def admin_test_cloud_account(provider: str, identifier: str, actor: str = Depends(require_cloud_accounts_manager)):
    return _cloud_call(_cloud_accounts.test_one, provider, identifier, actor)


@app.get("/api/admin/cloud-accounts/{provider}/{identifier}/permissions")
def admin_cloud_account_permissions(provider: str, identifier: str, actor: str = Depends(require_cloud_accounts_manager)):
    """What the role the Portal assumes in this AWS account can do (attached policies and a
    write-access check). Only works when the Portal can assume the role."""
    return _cloud_call(_cloud_accounts.role_permissions, provider, identifier, actor)


@app.post("/api/admin/cloud-accounts/{provider}/{identifier}/offboard")
def admin_offboard_cloud_account(provider: str, identifier: str, body: CloudAccountOffboardBody, actor: str = Depends(require_cloud_accounts_manager)):
    """Offboard. The Portal's own account and the Core account need the exact typed risk confirmation."""
    return _cloud_call(_cloud_accounts.offboard, provider, identifier, actor, risk_confirmation=body.risk_confirmation)


@app.post("/api/admin/cloud-accounts/{provider}/{identifier}/onboard")
def admin_onboard_cloud_account(provider: str, identifier: str, actor: str = Depends(require_cloud_accounts_manager)):
    """Bring an offboarded account back (tested first)."""
    return _cloud_call(_cloud_accounts.onboard, provider, identifier, actor)


@app.delete("/api/admin/cloud-accounts/{provider}/{identifier}")
def admin_discard_cloud_account_draft(provider: str, identifier: str, actor: str = Depends(require_cloud_accounts_manager)):
    """Discard an account that never connected."""
    return _cloud_call(_cloud_accounts.discard_draft, provider, identifier, actor)


# ── Site settings: sidebar tab visibility (User-Group-based) ─────────────
# Which sidebar tabs a user can see/navigate to is now determined SOLELY
# by which User Groups they belong to (Security Team / Infra Team /
# Cloud Infra Team / Management Team / Billing Team / Helpdesk Team — see
# settings_store.GROUP_REGISTRY/CATEGORY_REGISTRY), with one exception:
# admins always see every tab. The previous per-role ("member"/"admin")
# Tab Access Control matrix has been removed entirely per user request —
# there is no longer any role-based layer here at all.

@app.get("/api/settings/my-tab-access")
def get_my_tab_access(actor: str = Depends(get_actor)):
    """
    Any logged-in user: resolves which sidebar tabs THIS caller can see,
    e.g. {"dashboard": true, "cmdb": false, ...}. Sidebar links + route
    guards in App.jsx both key off this — a tab this user can't see also
    can't be reached by typing the URL directly.

    Admins always see every tab in TAB_REGISTRY (same "admin can never
    be locked out" precedent used everywhere else in this app). Everyone
    else sees exactly the tabs granted by their User Group memberships
    (Security Team / Infra Team / etc. — see
    settings_store.tabs_granted_by_groups()); a user in no groups sees
    no tabs at all until an admin assigns them to one.
    """
    if user_store.is_admin(actor):
        return {tab["id"]: True for tab in settings_store.TAB_REGISTRY}

    user_group_ids = user_store.get_user_groups(actor)
    granted_by_groups = settings_store.tabs_granted_by_groups(user_group_ids)
    # A few tabs are read-only company-wide pages (e.g. Website Monitoring)
    # and are visible to every logged-in user, whatever their groups.
    granted_by_groups = granted_by_groups | settings_store.EVERYONE_TAB_IDS
    return {tab["id"]: (tab["id"] in granted_by_groups) for tab in settings_store.TAB_REGISTRY}


# ── Admin: raw data export ────────────────────────────────────────────────────
# Lets admins pull the portal's underlying data (jobs, users, KB documents)
# out as JSON or CSV for offline analysis / backup / reporting — without
# needing direct DB/DynamoDB console access. Admin-only (require_admin),
# same as the user-management routes above.

def _all_jobs() -> list[dict]:
    """job_store.list_jobs() is paginated (per_page defaults to 50) for the
    normal Jobs-list UI; export needs every job, so page once with a very
    large per_page instead of looping — the portal's job count is small
    enough (internal tool, not high-volume) that this is simpler and safe."""
    return job_store.list_jobs(page=1, per_page=1_000_000)["jobs"]


def _jobs_csv_rows(jobs: list[dict]) -> tuple[list[str], list[list]]:
    columns = [
        "ticket_number", "id", "title", "description", "category", "category_other", "priority",
        "assignee_email", "assignee_name", "created_by_email", "created_by_name",
        "status", "due_date", "created_at", "updated_at",
        "aws_support_case_id", "freshservice_ticket_id", "freshservice_ticket_url",
        "ai_analysed_at", "ai_analysed_by",
        "notes_count", "audit_log_count", "notes_json", "audit_log_json", "ai_draft_json",
    ]
    rows = []
    for j in jobs:
        rows.append([
            j.get("ticket_number"), j.get("id"), j.get("title"), j.get("description"), j.get("category"), j.get("category_other"),
            j.get("priority"), j.get("assignee_email"), j.get("assignee_name"),
            j.get("created_by_email"), j.get("created_by_name"), j.get("status"), j.get("due_date"),
            j.get("created_at"), j.get("updated_at"),
            j.get("aws_support_case_id"), j.get("freshservice_ticket_id"), j.get("freshservice_ticket_url"),
            j.get("ai_analysed_at"), j.get("ai_analysed_by"),
            len(j.get("notes") or []), len(j.get("audit_log") or []),
            json.dumps(j.get("notes") or [], ensure_ascii=False),
            json.dumps(j.get("audit_log") or [], ensure_ascii=False),
            json.dumps(j.get("ai_draft") or {}, ensure_ascii=False),
        ])
    return columns, rows


def _csv_response(columns: list[str], rows: list[list], filename: str) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/admin/export/jobs")
def admin_export_jobs(format: str = "json", actor: str = Depends(require_admin)):
    """Export every job with full raw fields (audit log, notes, AI draft
    included). format=json (default) returns the raw list of job dicts;
    format=csv returns a flattened spreadsheet-friendly download."""
    jobs = _all_jobs()
    if format == "csv":
        columns, rows = _jobs_csv_rows(jobs)
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        return _csv_response(columns, rows, f"jobs-export-{stamp}.csv")
    if format != "json":
        raise HTTPException(400, "format must be 'json' or 'csv'")
    return {"exported_at": datetime.utcnow().isoformat(), "count": len(jobs), "jobs": jobs}


@app.get("/api/admin/export/users")
def admin_export_users(format: str = "json", actor: str = Depends(require_admin)):
    """Export the full portal user allowlist (email/name/role/added_by)."""
    users = user_store.list_users()
    if format == "csv":
        columns = ["email", "name", "role", "added_by", "created_at"]
        rows = [[u.get("email"), u.get("name"), u.get("role"), u.get("added_by"), u.get("created_at")] for u in users]
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        return _csv_response(columns, rows, f"users-export-{stamp}.csv")
    if format != "json":
        raise HTTPException(400, "format must be 'json' or 'csv'")
    return {"exported_at": datetime.utcnow().isoformat(), "count": len(users), "users": users}


@app.get("/api/admin/export/kb")
def admin_export_kb(format: str = "json", actor: str = Depends(require_admin)):
    """Export Knowledge Base document metadata + chunk text (no binary
    file contents are stored server-side to begin with — only parsed text
    chunks — so this already is the raw underlying data)."""
    docs = job_store.list_kb_documents()
    if format == "csv":
        columns = ["id", "filename", "file_type", "description", "char_count", "chunk_count", "uploaded_by", "uploaded_at"]
        rows = [[d.get("id"), d.get("filename"), d.get("file_type"), d.get("description"),
                  d.get("char_count"), d.get("chunk_count"), d.get("uploaded_by"), d.get("uploaded_at")] for d in docs]
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        return _csv_response(columns, rows, f"kb-export-{stamp}.csv")
    if format != "json":
        raise HTTPException(400, "format must be 'json' or 'csv'")
    return {"exported_at": datetime.utcnow().isoformat(), "count": len(docs), "documents": docs}


@app.get("/api/admin/export/all")
def admin_export_all(actor: str = Depends(require_admin)):
    """Single combined JSON dump of jobs + users + KB documents — a full
    raw backup of the portal's data in one call."""
    return {
        "exported_at": datetime.utcnow().isoformat(),
        "exported_by": actor,
        "jobs": _all_jobs(),
        "users": user_store.list_users(),
        "kb_documents": job_store.list_kb_documents(),
    }


# The ALB's authenticate-oidc listener action preserves whatever path the
# browser originally requested and restores it after the Azure AD round
# trip completes (it's stored in ALB's own signed session cookie, not
# something this app needs to manage). So the frontend's "Sign in with
# Microsoft" button simply top-level-navigates to this exact path on the
# ALB's own domain; once this route actually executes, the OIDC challenge
# has already succeeded and get_actor() has already validated the
# resulting x-amzn-oidc-data header — all this route has to do is bounce
# the browser back to the deployed SPA so the user lands somewhere useful
# instead of a bare backend response. FRONTEND_URL is set as a container
# env var once the CloudFront distribution domain is known (Task 12,
# compute_stack.py); defaults to the local Vite dev server for local runs
# against a non-local ENV (shouldn't normally happen, but keeps this route
# harmless rather than crashing if it does).
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5200")


@app.get("/login-landing")
def login_landing(actor: str = Depends(get_actor)):
    from fastapi.responses import RedirectResponse
    # This route only executes once per real login (right after the ALB's
    # Azure AD OIDC challenge succeeds) — NOT on every authenticated API
    # request — so it's the correct place to stamp last_login for
    # production. See database.py's PortalUser.last_login docstring.
    user_store.update_last_login(actor)
    return RedirectResponse(url=f"{FRONTEND_URL}/jobs", status_code=302)


# Non-secret public identifier — see compute_stack.py's AZURE_TENANT_ID
# comment for why this is safe to pass through as a plain env var rather
# than a Secrets Manager secret (it's already visible in every browser's
# OAuth redirect URL during the ALB's own login flow).
AZURE_AD_TENANT_ID = os.environ.get("AZURE_AD_TENANT_ID", "")


async def _cleanup_realtime_state_for_logout(request: Request) -> None:
    """
    Task 11.1 (job-portal-chat-voice Requirement 9.6): when a Team_Member's
    session ends — either a real 8-hour ALB session_timeout expiry
    (AuthContext.jsx's forceLogout(), triggered by its own client-side
    timer or by any API call's 401) or a voluntary "Sign out" click
    (also routed through this same endpoint per signOut()'s docstring)
    — close any open WebSocket connection for that Team_Member and end
    their participation in any Call they are still "joined" to,
    consistent with logout() being the one real sign-out path both
    flows share.

    Best-effort and NEVER allowed to block or fail the actual logout
    redirect below: this function swallows every exception itself.
    Identity is extracted the exact same way _authenticate_ws() does
    for the ALB-OIDC path (verify_alb_oidc_token()/_extract_email(),
    unmodified) — but unlike get_actor(), a failure to resolve an email
    here is NOT an error; it just means there is no known real-time
    state to clean up for this specific request (e.g. the ALB session
    had already fully expired before this redirect was even reached,
    in which case the WS connection's own 45-second heartbeat
    staleness window — see ws_connections.STALE_CONNECTION_SECONDS —
    is the fallback that reclaims it instead).

    Only reaches into THIS ECS task's own local
    ws_manager.connection_manager to actually close a live WebSocket
    object — a connection living on a different task cannot be closed
    from here (no cross-task pub/sub broker exists per design.md's
    fan-out design). ws_connections.unregister_all_connections_for_user()
    is still called first regardless, since that part IS cross-task-safe
    (it just deletes DynamoDB rows) — so even a connection on another
    task is immediately reported offline via Presence, and that other
    task's own heartbeat/TTL staleness path reclaims the socket itself
    shortly after, the same as any other unclean disconnect.
    """
    try:
        token = request.headers.get("x-amzn-oidc-data")
        if not token:
            return
        from . import auth_alb
        claims = auth_alb.verify_alb_oidc_token(token)
        email = auth_alb._extract_email(claims)
        if not email:
            return

        from . import call_store, ws_connections, ws_manager
        call_store.leave_all_active_calls_for_user(email)
        removed_connection_ids = ws_connections.unregister_all_connections_for_user(email)
        for connection_id in removed_connection_ids:
            await ws_manager.connection_manager.close_connection(connection_id, code=1000)
    except Exception:
        _logger.exception("logout: best-effort real-time state cleanup failed — continuing with sign-out anyway")


@app.get("/api/auth/logout")
async def logout(request: Request):
    """
    Real sign-out, not just clearing frontend state. The ALB's
    authenticate-oidc action (compute_stack.py) issues its own
    AWSELBAuthSessionCookie-* cookies with an 8-hour absolute lifetime
    (session_timeout=Duration.hours(8)) — those cookies are set by the
    ALB itself, not by this application, so this app can't intercept or
    directly invalidate them server-side. What it CAN do (confirmed via
    AWS's own guidance and multiple corroborating reports on this exact
    ALB+OIDC pattern — e.g. repost.aws "How to Modify AWSELBAuthSessionCookie
    to Log Off User") is respond with Set-Cookie headers that overwrite
    those same cookie names with an already-expired date, which makes the
    BROWSER discard them immediately. That alone is still not a complete
    sign-out though: Azure AD itself keeps its own separate SSO session,
    so the very next ALB OIDC challenge would silently re-issue a fresh
    token from that still-live Azure AD session without ever showing a
    login prompt — clearing the ALB cookie but skipping this would look
    like "signed out" for a split second before silently signing back in.
    The fix (same sources) is to ALSO redirect through Azure AD's own
    v2.0 end-session endpoint, which is what the redirect below does.

    Deliberately has NO get_actor() dependency — a user must be able to
    hit this even if their session has already expired (get_actor would
    401 before this ever runs), and there's nothing sensitive being read
    or returned here, only cookies being cleared and a redirect issued.
    (Task 11.1: this is also why _cleanup_realtime_state_for_logout()
    re-derives identity itself, best-effort, from the same
    x-amzn-oidc-data header rather than depending on get_actor().)

    In local dev (ENV == "local") there's no ALB and no Azure AD tenant
    configured, so this just returns 200 — the frontend's local-mode
    logout() already handles clearing localStorage itself and never
    calls this endpoint in the first place (see AuthContext.jsx).
    """
    if ENV == "local":
        return {"status": "ok"}

    await _cleanup_realtime_state_for_logout(request)

    from fastapi.responses import RedirectResponse
    post_logout_redirect = f"{FRONTEND_URL}/"
    if AZURE_AD_TENANT_ID:
        logout_url = (
            f"https://login.microsoftonline.com/{AZURE_AD_TENANT_ID}/oauth2/v2.0/logout"
            f"?post_logout_redirect_uri={post_logout_redirect}"
        )
    else:
        # Tenant ID missing (shouldn't happen in a correctly deployed
        # stack, but fail toward "still clears the ALB cookie" rather
        # than 500ing) — skips the Azure AD leg, so a live Azure AD SSO
        # session could still silently re-issue a token on the next ALB
        # challenge, but the ALB cookie itself is still cleared below.
        logout_url = post_logout_redirect
    response = RedirectResponse(url=logout_url, status_code=302)
    # ALB splits the session token across multiple numbered cookies once
    # it's too large for one (commonly -0 and -1, occasionally more) —
    # clearing a fixed handful covers every deployment this app has
    # actually seen without hardcoding an exact count that could change
    # if the token grows (e.g. more OIDC claims added later).
    for i in range(4):
        response.delete_cookie(f"AWSELBAuthSessionCookie-{i}", path="/")
    return response


# ── Jobs ──────────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
def list_jobs(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    category: Optional[str] = None,
    assignee: Optional[str] = None,
    q: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
    # Sort By filter (All Tickets tab) — sort_by is one of the
    # JOB_SORT_FIELDS keys below; sort_order is "asc" (increase) or
    # "desc" (decrease). Defaults preserve the original behavior
    # (newest ticket first).
    sort_by: str = "created_at",
    sort_order: str = "desc",
    actor: str = Depends(get_actor),
):
    if sort_by not in JOB_SORT_FIELDS:
        raise HTTPException(400, f"Invalid sort_by: {sort_by}. Must be one of {list(JOB_SORT_FIELDS)}")
    if sort_order not in ("asc", "desc"):
        raise HTTPException(400, "Invalid sort_order: must be 'asc' or 'desc'")
    return job_store.list_jobs(
        status=status.split(",") if status else None,
        priority=priority.split(",") if priority else None,
        category=category.split(",") if category else None,
        assignee=assignee.split(",") if assignee else None,
        q=q,
        page=page,
        per_page=per_page,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, actor: str = Depends(get_actor)):
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return j


@app.post("/api/jobs", status_code=201)
def create_job(
    body: CreateJobBody,
    actor: str = Depends(require_tab_write("jobs")),
):
    # Validate
    if body.category not in VALID_CATEGORIES:
        raise HTTPException(400, f"Invalid category: {body.category}")
    if body.category == "Other" and not (body.category_other or "").strip():
        raise HTTPException(400, "category_other is required when category is 'Other'")
    if body.priority not in VALID_PRIORITIES:
        raise HTTPException(400, f"Invalid priority: {body.priority}")
    if body.assignee_email not in TEAM_MEMBERS:
        raise HTTPException(400, f"Invalid assignee email: {body.assignee_email}")

    actor_name = TEAM_MEMBERS[actor]
    assignee_name = TEAM_MEMBERS[body.assignee_email]

    j = job_store.create_job(
        title=body.title.strip()[:200],
        description=body.description.strip()[:5000],
        category=body.category,
        category_other=(body.category_other or "").strip()[:200] or None,
        priority=body.priority,
        assignee_email=body.assignee_email,
        assignee_name=assignee_name,
        created_by_email=actor,
        created_by_name=actor_name,
        due_date=body.due_date or None,
        audit_log=[{
            "timestamp": datetime.utcnow().isoformat(),
            "actor": actor_name,
            "actor_email": actor,
            "event": f"Job created and assigned to {assignee_name}",
        }],
    )
    notify_job_assigned(j)
    return j


@app.patch("/api/jobs/{job_id}")
def update_job(
    job_id: str,
    body: UpdateJobBody,
    actor: str = Depends(get_actor),
):
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    if j["created_by_email"] != actor and not user_store.is_admin(actor):
        raise HTTPException(403, "Only the job creator (or an admin) can edit this job")
    if j["status"] == "Closed":
        raise HTTPException(400, "Cannot edit a Closed job")

    fields = {}
    changes = []

    if body.title is not None and body.title != j["title"]:
        fields["title"] = body.title.strip()[:200]
        changes.append("title updated")
    if body.description is not None and body.description != j["description"]:
        fields["description"] = body.description.strip()[:5000]
        changes.append("description updated")
    if body.category is not None and body.category != j["category"]:
        if body.category not in VALID_CATEGORIES:
            raise HTTPException(400, f"Invalid category: {body.category}")
        fields["category"] = body.category
        changes.append(f"category → {body.category}")
    if body.category_other is not None:
        fields["category_other"] = body.category_other.strip()[:200] or None
    if body.priority is not None and body.priority != j["priority"]:
        if body.priority not in VALID_PRIORITIES:
            raise HTTPException(400, f"Invalid priority: {body.priority}")
        fields["priority"] = body.priority
        changes.append(f"priority → {body.priority}")
    if body.due_date is not None:
        fields["due_date"] = body.due_date or None
        changes.append(f"due date → {body.due_date or 'cleared'}")

    prev_assignee = j["assignee_email"]
    if body.assignee_email is not None and body.assignee_email != j["assignee_email"]:
        if body.assignee_email not in TEAM_MEMBERS:
            raise HTTPException(400, f"Invalid assignee email: {body.assignee_email}")
        fields["assignee_email"] = body.assignee_email
        fields["assignee_name"] = TEAM_MEMBERS[body.assignee_email]
        changes.append(f"reassigned to {TEAM_MEMBERS[body.assignee_email]}")

    if changes:
        j = job_store.update_job_fields(job_id, fields)
        j = add_audit(j, actor, f"Edited: {', '.join(changes)}")
        # Notify new assignee if reassigned
        if body.assignee_email and body.assignee_email != prev_assignee:
            notify_job_assigned(j)

    return j


@app.delete("/api/jobs/{job_id}", status_code=200)
def delete_job(
    job_id: str,
    actor: str = Depends(get_actor),
):
    """
    Delete a job outright — for cases created by mistake (wrong info,
    duplicate, test entry, etc.). Restricted to the job's own creator or
    an admin, same permission model as update_job() above. Deliberately
    NOT restricted by status (unlike editing, which blocks Closed jobs) —
    a wrongly-created job should be removable regardless of what status it
    ended up in, since the whole point is undoing a mistake.

    This is a hard delete (no soft-delete/trash), matching the "if wrong
    created" framing — jobs raised by genuine mistake should disappear
    completely, not linger as a hidden record. Any already-raised external
    tickets (Freshservice/AWS Support) are NOT touched here; those are
    separate systems and must be cancelled there independently if needed.
    """
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    if j["created_by_email"] != actor and not user_store.is_admin(actor):
        raise HTTPException(403, "Only the job creator (or an admin) can delete this job")
    job_store.delete_job(job_id)
    return {"deleted": job_id}


@app.post("/api/jobs/{job_id}/status")
def change_status(
    job_id: str,
    body: StatusChangeBody,
    background_tasks: BackgroundTasks,
    actor: str = Depends(get_actor),
):
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")

    allowed = STATUS_TRANSITIONS.get(j["status"], [])
    if body.new_status not in allowed:
        raise HTTPException(
            400,
            f"Cannot transition from '{j['status']}' to '{body.new_status}'. "
            f"Allowed next statuses: {allowed}"
        )

    old_status = j["status"]
    actor_name = TEAM_MEMBERS[actor]
    j = job_store.update_job_fields(job_id, {"status": body.new_status})
    j = add_audit(j, actor, f"Status changed: {old_status} → {body.new_status}")
    # Email is sent AFTER the response goes out (BackgroundTasks) — SES can
    # take 60s+ to retry on failure (see notifications.py), which was
    # blocking this endpoint's response and making the clicking user's own
    # browser look stale until a manual refresh, even though the status was
    # already correctly persisted (hence teammates saw it fine immediately).
    background_tasks.add_task(notify_status_changed, j, old_status, actor_name)
    return j


@app.get("/api/jobs/{job_id}/transitions")
def get_transitions(job_id: str, actor: str = Depends(get_actor)):
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return {
        "current_status": j["status"],
        "allowed_next": STATUS_TRANSITIONS.get(j["status"], []),
        "all_statuses": VALID_STATUSES,
    }


# ── AI Analysis ───────────────────────────────────────────────────────────────

@app.post("/api/jobs/{job_id}/analyse")
def ai_analyse(
    job_id: str,
    background_tasks: BackgroundTasks,
    actor: str = Depends(get_actor),
):
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")

    # Retrieve relevant KB chunks for RAG enrichment
    query = f"{j['title']} {j['description']} {j['category']}"
    kb_docs = job_store.list_kb_documents()
    kb_chunks = retrieve_relevant_chunks(query, kb_docs, top_k=5)
    kb_context = format_kb_context(kb_chunks)

    draft = analyse_job(
        title=j["title"],
        description=j["description"],
        category=j["category"],
        priority=j["priority"],
        audit_log=j.get("audit_log") or [],
        kb_context=kb_context,
    )
    draft["kb_chunks_used"] = len(kb_chunks)
    j = job_store.update_job_fields(job_id, {
        "ai_draft": draft,
        "ai_analysed_at": datetime.utcnow().isoformat(),
        "ai_analysed_by": actor,
    })
    j = add_audit(j, actor, f"AI analysis completed via AWS Bedrock ({draft.get('model_used', 'unknown model')})")
    # Deferred to BackgroundTasks — see change_status() above for why.
    background_tasks.add_task(notify_ai_complete, j, actor)
    return j


# ── Escalation ────────────────────────────────────────────────────────────────

@app.post("/api/jobs/{job_id}/escalate")
async def escalate(
    job_id: str,
    body: EscalateBody,
    background_tasks: BackgroundTasks,
    actor: str = Depends(get_actor),
):
    """
    Escalate a job to Freshservice, AWS Support, or both.
    Runs the external API calls in a thread pool (run_in_executor) so the
    async event loop keeps the ALB connection alive — avoids the 502 that
    happened when a synchronous handler blocked for >60 s waiting on the
    external APIs.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")

    valid_actions = ("freshservice", "aws_support", "both", "aws_ams_sr", "aws_ams_incident")
    if body.action not in valid_actions:
        raise HTTPException(400, f"action must be one of: {', '.join(valid_actions)}")

    # Require AI draft for any AWS Support-family action (own account or AMS)
    if body.action in ("aws_support", "both", "aws_ams_sr", "aws_ams_incident") and not j.get("ai_draft"):
        raise HTTPException(400, "AI analysis required before escalating to AWS Support")

    results = {}
    fields = {}
    loop = asyncio.get_event_loop()

    if body.action in ("freshservice", "both"):
        if ENV == "local":
            fs_ticket_id = f"INC-{datetime.utcnow().strftime('%Y%m%d')}-{str(uuid.uuid4())[:5].upper()}"
            fs_ticket_url = f"https://yourcompany.freshservice.com/incidents/{fs_ticket_id}"
        else:
            from .escalation import raise_freshservice_ticket, EscalationError
            try:
                fs_result = await loop.run_in_executor(
                    None,
                    lambda: raise_freshservice_ticket(
                        job_id=j["id"],
                        title=j["title"],
                        description=j["description"],
                        category=j["category"],
                        priority=j["priority"],
                        requester_email=j["created_by_email"],
                    )
                )
            except EscalationError as e:
                raise HTTPException(502, e.message)
            except Exception as e:
                _logger.error("escalate/freshservice unexpected: %s", e)
                raise HTTPException(502, "Freshservice escalation failed")
            fs_ticket_id = fs_result["ticket_id"]
            fs_ticket_url = fs_result["ticket_url"]

        fields["freshservice_ticket_id"] = fs_ticket_id
        fields["freshservice_ticket_url"] = fs_ticket_url
        results["freshservice"] = {"ticket_id": fs_ticket_id, "url": fs_ticket_url}

    if body.action in ("aws_support", "both"):
        aws_case_display_id = None
        if ENV == "local":
            aws_case_id = f"case-{datetime.utcnow().strftime('%Y%m%d%H%M')}-{str(uuid.uuid4())[:6]}"
            aws_case_display_id = str(uuid.uuid4().int)[:15]  # mock a plausible-looking numeric display ID
        else:
            from .escalation import raise_aws_support_case, EscalationError
            try:
                aws_result = await loop.run_in_executor(
                    None,
                    lambda: raise_aws_support_case(
                        job_id=j["id"],
                        title=j["title"],
                        description=j["description"],
                        category=j["category"],
                        priority=j["priority"],
                    )
                )
            except EscalationError as e:
                raise HTTPException(502, e.message)
            except Exception as e:
                _logger.error("escalate/aws_support unexpected: %s", e)
                raise HTTPException(502, "AWS Support escalation failed")
            aws_case_id = aws_result["case_id"]
            aws_case_display_id = aws_result.get("display_id")

        fields["aws_support_case_id"] = aws_case_id
        fields["aws_support_case_display_id"] = aws_case_display_id
        results["aws_support"] = {
            "case_id": aws_case_id,
            "display_id": aws_case_display_id,
            "url": f"https://support.console.aws.amazon.com/support/home#/case/?caseId={aws_case_id}",
        }
        # Record who raised this case (via standalone_case_store, same
        # store the "Create AWS Case" button uses) so the All Cases page
        # can show a "Raised By" column regardless of which of the 3
        # case-origin paths (standalone button / Job escalation / Health
        # event) actually created it.
        standalone_case_store.record_case(
            aws_case_id, account="own", case_type="aws_support", subject=j["title"],
            category=j["category"], priority=j["priority"], display_id=aws_case_display_id,
            actor_email=actor, actor_name=TEAM_MEMBERS.get(actor, actor),
        )

    if body.action in ("aws_ams_sr", "aws_ams_incident"):
        case_type = "sr" if body.action == "aws_ams_sr" else "incident"
        ams_case_display_id = None
        if ENV == "local":
            ams_case_id = f"case-ams-{case_type}-{datetime.utcnow().strftime('%Y%m%d%H%M')}-{str(uuid.uuid4())[:6]}"
            ams_case_display_id = str(uuid.uuid4().int)[:15]  # mock a plausible-looking numeric display ID
        else:
            from .escalation import raise_aws_ams_case, EscalationError
            try:
                ams_result = await loop.run_in_executor(
                    None,
                    lambda: raise_aws_ams_case(
                        job_id=j["id"],
                        title=j["title"],
                        description=j["description"],
                        category=j["category"],
                        priority=j["priority"],
                        case_type=case_type,
                    )
                )
            except EscalationError as e:
                raise HTTPException(502, e.message)
            except Exception as e:
                _logger.error("escalate/aws_ams unexpected: %s", e)
                raise HTTPException(502, "AWS AMS escalation failed")
            ams_case_id = ams_result["case_id"]
            ams_case_display_id = ams_result.get("display_id")

        result_key = "aws_ams_sr" if case_type == "sr" else "aws_ams_incident"
        ams_case_status = ams_result.get("status") if ENV != "local" else "opened"
        fields[f"{result_key}_case_id"] = ams_case_id
        fields[f"{result_key}_case_display_id"] = ams_case_display_id
        fields[f"{result_key}_case_status"] = ams_case_status
        results[result_key] = {
            "case_id": ams_case_id,
            "display_id": ams_case_display_id,
            "status": ams_case_status,
            # AMS support cases live under the AMS account, not this one —
            # link points at that account's Support console (viewer needs
            # console access to 100000000001 to actually open it).
            "url": f"https://support.console.aws.amazon.com/support/home#/case/?caseId={ams_case_id}",
        }
        # Same "who raised this" bookkeeping as the aws_support branch above.
        standalone_case_store.record_case(
            ams_case_id, account="ams", case_type=result_key, subject=j["title"],
            category=j["category"], priority=j["priority"], display_id=ams_case_display_id,
            actor_email=actor, actor_name=TEAM_MEMBERS.get(actor, actor),
        )

    _SYSTEM_LABELS = {
        "freshservice": "Freshservice",
        "aws_support": "AWS Support",
        "aws_ams_sr": "AWS AMS Service Request",
        "aws_ams_incident": "AWS AMS Incident",
    }

    j = job_store.update_job_fields(job_id, fields)
    for system, detail in results.items():
        ticket_id = detail.get("ticket_id") or detail.get("case_id")
        label = _SYSTEM_LABELS.get(system, system.replace('_', ' ').title())
        j = add_audit(j, actor, f"{label} ticket raised: {ticket_id}")
        # Deferred to BackgroundTasks — see change_status() above for why.
        # This loop can raise up to 4 notification emails (Freshservice +
        # AWS Support/AMS across `both`/dual actions), each of which could
        # individually take 60s+ to retry on an SES failure — that stacked
        # up to minutes of blocked response time before this fix.
        background_tasks.add_task(
            notify_ticket_raised, j, "Freshservice" if system == "freshservice" else "AWS Support", ticket_id
        )

    return {**j, "escalation_results": results}


@app.post("/api/jobs/{job_id}/refresh-ams-status")
async def refresh_ams_status(
    job_id: str,
    actor: str = Depends(get_actor),
):
    """
    Re-fetch the current status of this job's AWS AMS case(s) (SR and/or
    Incident) directly from the AMS support account and persist it. Manual
    refresh (no webhook/polling exists for AMS the way Freshservice has
    one) — call this whenever the user wants an up-to-date status.
    """
    import asyncio

    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")

    case_targets = []
    if j.get("aws_ams_sr_case_id"):
        case_targets.append(("aws_ams_sr", j["aws_ams_sr_case_id"]))
    if j.get("aws_ams_incident_case_id"):
        case_targets.append(("aws_ams_incident", j["aws_ams_incident_case_id"]))

    if not case_targets:
        raise HTTPException(400, "This job has no AWS AMS case raised yet")

    loop = asyncio.get_event_loop()
    fields = {}
    statuses = {}

    if ENV == "local":
        for result_key, case_id in case_targets:
            fields[f"{result_key}_case_status"] = "opened"
            statuses[result_key] = {"case_id": case_id, "status": "opened"}
    else:
        from .escalation import get_aws_ams_case_status, EscalationError
        for result_key, case_id in case_targets:
            try:
                status_result = await loop.run_in_executor(
                    None, lambda cid=case_id: get_aws_ams_case_status(cid)
                )
            except EscalationError as e:
                raise HTTPException(502, e.message)
            except Exception as e:
                _logger.error("refresh_ams_status unexpected for job %s: %s", job_id, e)
                raise HTTPException(502, "Could not refresh AWS AMS case status")
            fields[f"{result_key}_case_status"] = status_result.get("status")
            # Backfill display_id for jobs escalated before this field
            # existed (or whose earlier describe_cases lookup failed) —
            # only set it if we don't already have one, so a fresh
            # refresh never overwrites a good value with a bad one.
            if status_result.get("display_id") and not j.get(f"{result_key}_case_display_id"):
                fields[f"{result_key}_case_display_id"] = status_result["display_id"]
            statuses[result_key] = status_result

    old_statuses = {
        rk: j.get(f"{rk}_case_status") for rk, _ in case_targets
    }
    j = job_store.update_job_fields(job_id, fields)

    for result_key, _ in case_targets:
        new_status = fields.get(f"{result_key}_case_status")
        if new_status and new_status != old_statuses.get(result_key):
            label = "AWS AMS Service Request" if result_key == "aws_ams_sr" else "AWS AMS Incident"
            j = add_audit(j, actor, f"{label} status updated: {old_statuses.get(result_key) or 'unknown'} → {new_status}")

    return {**j, "ams_status_results": statuses}


# ── AWS Support / AMS Cases (All Cases page) ─────────────────────────────────
# Lists every case in BOTH accounts (this portal's own AWS Support account,
# and the AWS AMS support account 100000000001) — including cases raised
# manually outside the portal, not just ones created via /escalate — and
# links each case to the Job that raised it, when there is one (matched via
# job_store.build_case_to_job_map(), a reverse lookup over the 3 case-id
# fields a job can carry: aws_support_case_id, aws_ams_sr_case_id,
# aws_ams_incident_case_id).

class CaseReplyBody(BaseModel):
    account: str    # "own" | "ams"
    body: str
    # Optional: attachmentSetId returned from POST /api/cases/attachments —
    # attaches whatever files were uploaded there to this reply.
    attachment_set_id: Optional[str] = None


class CreateCaseBody(BaseModel):
    # "aws_support" (this portal's own AWS Support account) |
    # "aws_ams_sr" | "aws_ams_incident" (AMS support account 100000000001)
    case_type: str
    subject: str
    description: str
    category: str
    priority: str
    # Optional: attachmentSetId returned from POST /api/cases/attachments —
    # attaches whatever files were uploaded there to the new case.
    attachment_set_id: Optional[str] = None


def _local_mock_cases() -> list[dict]:
    """ENV=local has no real AWS Support account to query — return the
    portal's own already-raised case IDs (if any) as a minimal mock list
    so the All Cases page still has something to render locally."""
    jobs = job_store.list_jobs(page=1, per_page=1_000_000)["jobs"]
    cases = []
    for j in jobs:
        if j.get("aws_support_case_id"):
            cases.append({
                "case_id": j["aws_support_case_id"], "account": "own", "case_type": "AWS Support",
                "subject": j["title"], "status": "opened", "service_code": None,
                "display_id": j.get("aws_support_case_display_id") or j["aws_support_case_id"],
                "time_created": j.get("created_at"),
            })
        if j.get("aws_ams_sr_case_id"):
            cases.append({
                "case_id": j["aws_ams_sr_case_id"], "account": "ams", "case_type": "AMS Service Request",
                "subject": j["title"], "status": j.get("aws_ams_sr_case_status") or "opened",
                "service_code": None,
                "display_id": j.get("aws_ams_sr_case_display_id") or j["aws_ams_sr_case_id"],
                "time_created": j.get("created_at"),
            })
        if j.get("aws_ams_incident_case_id"):
            cases.append({
                "case_id": j["aws_ams_incident_case_id"], "account": "ams", "case_type": "AMS Incident",
                "subject": j["title"], "status": j.get("aws_ams_incident_case_status") or "opened",
                "service_code": None,
                "display_id": j.get("aws_ams_incident_case_display_id") or j["aws_ams_incident_case_id"],
                "time_created": j.get("created_at"),
            })
    # Also mock in AMS SR cases raised directly from a Health event (no Job
    # involved — see raise_sr_for_health_event()) so the local dev
    # environment can exercise the Cases<->Health-event cross-link too.
    for arn, check in health_ack_store.list_checks().items():
        if check.get("ams_sr_case_id"):
            cases.append({
                "case_id": check["ams_sr_case_id"], "account": "ams", "case_type": "AMS Service Request",
                "subject": f"AWS Health event: {check.get('ams_sr_service')} — {check.get('ams_sr_event_type_code')}",
                "status": check.get("ams_sr_case_status") or "opened",
                "service_code": None, "display_id": check["ams_sr_case_id"],
                "time_created": check.get("ams_sr_raised_at"),
            })
    # Cases raised directly via the AWS Cases page's "Create AWS Case"
    # button (no Job or Health event involved) — see
    # standalone_case_store.py and the POST /api/cases endpoint below.
    _CASE_TYPE_LABEL = {
        "aws_support": "AWS Support", "aws_ams_sr": "AMS Service Request", "aws_ams_incident": "AMS Incident",
    }
    for sc in standalone_case_store.list_cases():
        cases.append({
            "case_id": sc["case_id"], "account": sc["account"],
            "case_type": _CASE_TYPE_LABEL.get(sc["case_type"], sc["case_type"]),
            "subject": sc["subject"], "status": "opened", "service_code": None,
            "display_id": sc.get("display_id") or sc["case_id"], "time_created": sc.get("created_at"),
        })
    return cases


@app.get("/api/cases")
async def list_cases(actor: str = Depends(get_actor)):
    """
    List every case across both AWS accounts, newest first, each annotated
    with `linked_job` (None if the case wasn't raised by / linked to any
    Job in this portal) and `raised_by_email`/`raised_by_name` (which
    portal user raised this case — None only for cases raised entirely
    outside the portal, e.g. directly in the AWS Support console).
    """
    import asyncio

    case_map = job_store.build_case_to_job_map()
    health_event_map = health_ack_store.build_case_to_health_event_map()

    if ENV == "local":
        raw_cases = _local_mock_cases()
    else:
        from .escalation import list_aws_support_cases, list_aws_ams_cases, describe_case_type, EscalationError
        loop = asyncio.get_event_loop()
        raw_cases = []
        errors = []
        try:
            own_cases = await loop.run_in_executor(None, list_aws_support_cases)
            for c in own_cases:
                raw_cases.append({
                    "case_id": c.get("caseId"), "account": "own",
                    "case_type": describe_case_type("own", c.get("serviceCode")),
                    "subject": c.get("subject"), "status": c.get("status"),
                    "service_code": c.get("serviceCode"), "display_id": c.get("displayId") or c.get("caseId"),
                    "time_created": c.get("timeCreated"),
                })
        except EscalationError as e:
            errors.append({"account": "own", "message": e.message})
        except Exception as e:
            _logger.error("list_cases/own unexpected: %s", e)
            errors.append({"account": "own", "message": "Could not list AWS Support cases"})

        try:
            ams_cases = await loop.run_in_executor(None, list_aws_ams_cases)
            for c in ams_cases:
                service_code = c.get("serviceCode")
                raw_cases.append({
                    "case_id": c.get("caseId"), "account": "ams",
                    "case_type": describe_case_type("ams", service_code),
                    "subject": c.get("subject"), "status": c.get("status"),
                    "service_code": service_code, "display_id": c.get("displayId") or c.get("caseId"),
                    "time_created": c.get("timeCreated"),
                })
        except EscalationError as e:
            errors.append({"account": "ams", "message": e.message})
        except Exception as e:
            _logger.error("list_cases/ams unexpected: %s", e)
            errors.append({"account": "ams", "message": "Could not list AWS AMS cases"})

        # Native Security Incident Response cases (the SIR console's own
        # case system — NOT returned by the Support API above). A failure
        # here (e.g. missing IAM permission) only adds a partial_error
        # banner; it never hides the Support cases.
        try:
            from .escalation import list_security_ir_cases
            raw_cases.extend(await loop.run_in_executor(None, list_security_ir_cases))
        except EscalationError as e:
            errors.append({"account": "ams", "message": e.message})
        except Exception as e:
            _logger.error("list_cases/security-ir unexpected: %s", e)
            errors.append({"account": "ams", "message": "Could not list Security Incident Response cases"})

        if not raw_cases and errors:
            # Both accounts failed — surface the first error rather than a
            # silently empty page.
            raise HTTPException(502, errors[0]["message"])

    # {case_id: {raised_by_email, raised_by_name}} — every case raised
    # through the portal (standalone Create-Case button, Job Escalation
    # Actions, or Health Dashboard "Raise SR") writes a row here (see
    # standalone_case_store.record_case() call sites). Built once here,
    # same pattern as case_map/health_event_map above, rather than
    # querying per-case.
    raised_by_map = {sc["case_id"]: sc for sc in standalone_case_store.list_cases()}

    for c in raw_cases:
        c["linked_job"] = case_map.get(c["case_id"])
        # A case can be linked to a Job OR to a Health event (raised
        # directly from the Health Dashboard, sub-feature 3) — never both,
        # since Job-based escalation and event-based escalation are
        # mutually exclusive paths that write to different stores.
        c["linked_health_event"] = health_event_map.get(c["case_id"]) if not c["linked_job"] else None

        raised_by = raised_by_map.get(c["case_id"])
        if raised_by:
            c["raised_by_email"] = raised_by.get("raised_by_email")
            c["raised_by_name"] = raised_by.get("raised_by_name")
        elif c["linked_job"]:
            # Fallback for cases raised before this "who raised it"
            # tracking existed (no standalone_case_store row) but that
            # ARE linked to a Job — use the Job's creator as a best-effort
            # proxy (may not be exactly who clicked "Escalate" if that
            # differed from who created the Job, but is far better than
            # showing nothing).
            c["raised_by_email"] = c["linked_job"].get("created_by_email")
            c["raised_by_name"] = c["linked_job"].get("created_by_name")
        else:
            c["raised_by_email"] = None
            c["raised_by_name"] = None

    raw_cases.sort(key=lambda c: c.get("time_created") or "", reverse=True)
    result = {"cases": raw_cases, "count": len(raw_cases)}
    if ENV != "local" and errors:
        result["partial_errors"] = errors
    return result


@app.post("/api/cases", status_code=201)
async def create_case(body: CreateCaseBody, actor: str = Depends(require_tab_write("cases"))):
    """
    Create a new AWS case directly from the AWS Cases page — i.e. without
    needing a Job first (previously the only way to raise a case was via
    a Job's Escalation Actions). Only supports the two AMS support
    account (100000000001) case families — "aws_ams_sr" / "aws_ams_incident"
    — same cross-account credentials as Job escalation. "aws_support"
    (this portal's own AWS Support account) is deliberately NOT offered
    here per user request (own-account cases still go through a Job's
    Escalation Actions as before). Runs the AWS Support API call in a
    thread pool (same pattern as /api/jobs/{job_id}/escalate) so the
    async event loop isn't blocked.
    """
    import asyncio

    valid_case_types = ("aws_ams_sr", "aws_ams_incident")
    if body.case_type not in valid_case_types:
        raise HTTPException(400, f"case_type must be one of: {', '.join(valid_case_types)}")
    if not body.subject.strip():
        raise HTTPException(400, "subject is required")
    if not body.description.strip():
        raise HTTPException(400, "description is required")

    subject = body.subject.strip()[:255]
    description = body.description.strip()
    actor_name = TEAM_MEMBERS.get(actor, actor)
    account = "ams"

    display_id = None
    if ENV == "local":
        prefix = {"aws_ams_sr": "case-ams-sr", "aws_ams_incident": "case-ams-incident"}[body.case_type]
        case_id = f"{prefix}-{datetime.utcnow().strftime('%Y%m%d%H%M')}-{str(uuid.uuid4())[:6]}"
        display_id = str(uuid.uuid4().int)[:15]  # mock a plausible-looking numeric display ID
    else:
        from .escalation import raise_aws_ams_case, EscalationError
        loop = asyncio.get_event_loop()
        case_type = "sr" if body.case_type == "aws_ams_sr" else "incident"
        try:
            result = await loop.run_in_executor(
                None,
                lambda: raise_aws_ams_case(
                    job_id=None, title=subject, description=description,
                    category=body.category, priority=body.priority, case_type=case_type,
                    attachment_set_id=body.attachment_set_id,
                )
            )
        except EscalationError as e:
            raise HTTPException(502, e.message)
        except Exception as e:
            _logger.error("create_case unexpected: %s", e)
            raise HTTPException(502, "Could not create the AWS case")
        case_id = result["case_id"]
        display_id = result.get("display_id")

    standalone_case_store.record_case(
        case_id, account=account, case_type=body.case_type, subject=subject,
        category=body.category, priority=body.priority, display_id=display_id,
        actor_email=actor, actor_name=actor_name,
    )
    return {
        "case_id": case_id, "display_id": display_id, "account": account, "case_type": body.case_type,
        "url": f"https://support.console.aws.amazon.com/support/home#/case/?caseId={case_id}",
    }


@app.post("/api/cases/attachments")
async def upload_case_attachments(
    account: str = Form(...),
    attachment_set_id: Optional[str] = Form(default=None),
    files: List[UploadFile] = File(...),
    actor: str = Depends(get_actor),
):
    """
    Upload up to 3 files (AWS Support's own per-request limit, 5 MB each)
    to a new or existing AWS Support attachment set. Called before
    creating a case (POST /api/cases) or replying to one (POST
    /api/cases/{case_id}/reply) — pass the returned attachment_set_id
    along with that follow-up request.

    `account` disambiguates which AWS Support account the attachment set
    belongs to ("own" or "ams") — it must match the account the case
    will be created in/replied to, since attachment sets aren't shared
    across accounts. All types of files AWS Support itself accepts are
    allowed here (no extension allow-list) — only size/count are
    enforced, matching AWS's own AddAttachmentsToSet limits.

    The attachment set expires 1 hour after creation (AWS-enforced), so
    upload immediately before submitting the case/reply that uses it.
    """
    if account not in ("own", "ams"):
        raise HTTPException(400, "account must be 'own' or 'ams'")
    if not files:
        raise HTTPException(400, "No files provided")
    if len(files) > 3:
        raise HTTPException(400, "AWS Support allows a maximum of 3 attachments per upload")

    file_tuples = []
    for f in files:
        content = await f.read()
        if len(content) == 0:
            raise HTTPException(400, f"'{f.filename}' is empty")
        if len(content) > 5 * 1024 * 1024:
            raise HTTPException(400, f"'{f.filename}' is too large ({len(content)//1024} KB) — AWS Support allows a maximum of 5 MB per attachment")
        file_tuples.append((f.filename or "attachment.bin", content))

    if ENV == "local":
        # No real AWS Support account locally — mock a plausible attachment set ID.
        return {"attachment_set_id": attachment_set_id or f"set-{uuid.uuid4()}", "files": [f[0] for f in file_tuples]}

    import asyncio
    from .escalation import add_attachments_to_set, EscalationError
    loop = asyncio.get_event_loop()
    try:
        new_set_id = await loop.run_in_executor(
            None, lambda: add_attachments_to_set(account, file_tuples, attachment_set_id)
        )
    except EscalationError as e:
        raise HTTPException(400, e.message)
    except Exception as e:
        _logger.error("upload_case_attachments unexpected: %s", e)
        raise HTTPException(502, "Could not upload attachment(s) to AWS Support")

    return {"attachment_set_id": new_set_id, "files": [f[0] for f in file_tuples]}


@app.get("/api/cases/{case_id}")
async def get_case(case_id: str, account: str, source: Optional[str] = None, actor: str = Depends(get_actor)):
    """
    Full case detail (including recent communications) for the case
    detail/reply view. `account` query param disambiguates which AWS
    account owns this case ID: "own" or "ams".

    `source=security_ir` selects a native AWS Security Incident Response
    case (read-only; always the AMS account) instead of a Support API case.
    """
    import asyncio

    if account not in ("own", "ams"):
        raise HTTPException(400, "account query param must be 'own' or 'ams'")

    if source:
        from .escalation import SECURITY_IR_SOURCE, get_security_ir_case_detail, EscalationError
        if source != SECURITY_IR_SOURCE or account != "ams":
            raise HTTPException(400, "source must be 'security_ir' and account 'ams'")
        if ENV == "local":
            raise HTTPException(404, "Security Incident Response cases are not available in local mode")
        loop = asyncio.get_event_loop()
        try:
            sir = await loop.run_in_executor(None, lambda: get_security_ir_case_detail(case_id))
        except EscalationError as e:
            raise HTTPException(502, e.message)
        except Exception as e:
            _logger.error("get_case/security-ir unexpected for %s: %s", case_id, e)
            raise HTTPException(502, "Could not fetch Security Incident Response case")
        return {**sir, "linked_job": None, "linked_health_event": None, "raised_by_email": None, "raised_by_name": None}

    case_map = job_store.build_case_to_job_map()
    linked_health_event = None if case_map.get(case_id) else health_ack_store.find_by_ams_sr_case_id(case_id)
    linked_job = case_map.get(case_id)

    # Same "raised by" resolution as list_cases() above — standalone_case_
    # store row first, falling back to the linked Job's creator.
    raised_by = standalone_case_store.get_case(case_id)
    if raised_by:
        raised_by_email, raised_by_name = raised_by.get("raised_by_email"), raised_by.get("raised_by_name")
    elif linked_job:
        raised_by_email, raised_by_name = linked_job.get("created_by_email"), linked_job.get("created_by_name")
    else:
        raised_by_email, raised_by_name = None, None

    if ENV == "local":
        cases = {c["case_id"]: c for c in _local_mock_cases()}
        case = cases.get(case_id)
        if not case:
            raise HTTPException(404, f"Case {case_id} not found")
        detail = {
            "caseId": case_id, "displayId": case.get("display_id"),
            "subject": case["subject"], "status": case["status"],
            "serviceCode": None, "categoryCode": None, "severityCode": "normal",
            "timeCreated": case.get("time_created"),
            "recentCommunications": {"communications": []},
        }
    else:
        from .escalation import get_case_detail, EscalationError
        loop = asyncio.get_event_loop()
        try:
            detail = await loop.run_in_executor(None, lambda: get_case_detail(case_id, account))
        except EscalationError as e:
            raise HTTPException(502, e.message)
        except Exception as e:
            _logger.error("get_case unexpected for %s: %s", case_id, e)
            raise HTTPException(502, "Could not fetch case detail")

    communications = detail.get("recentCommunications", {}).get("communications", [])
    return {
        "case_id": detail.get("caseId", case_id),
        # boto3's describe_cases() also returns "displayId" — the short
        # numeric case number shown in the AWS Support Center UI (e.g.
        # "178668889700727") — separately from "caseId", which is the
        # long internal identifier (e.g.
        # "case-100000000001-muen-2026-f6b4310b3419582d") used only for
        # API calls. Without this, the case detail page fell back to
        # showing the internal caseId as if it were the case number.
        "display_id": detail.get("displayId") or detail.get("caseId", case_id),
        "account": account,
        "subject": detail.get("subject"),
        "status": detail.get("status"),
        "service_code": detail.get("serviceCode"),
        "category_code": detail.get("categoryCode"),
        "severity_code": detail.get("severityCode"),
        "time_created": detail.get("timeCreated"),
        "communications": [
            {
                "body": c.get("body"),
                "submitted_by": c.get("submittedBy"),
                "time_created": c.get("timeCreated"),
            }
            for c in communications
        ],
        "linked_job": linked_job,
        "linked_health_event": {
            "event_arn": linked_health_event["event_arn"],
            "service": linked_health_event.get("ams_sr_service"),
            "event_type_code": linked_health_event.get("ams_sr_event_type_code"),
        } if linked_health_event else None,
        "raised_by_email": raised_by_email,
        "raised_by_name": raised_by_name,
    }


class SecurityIrReplyBody(BaseModel):
    body: str


@app.post("/api/cases/{case_id}/security-ir-reply")
async def reply_to_security_ir_case(
    case_id: str,
    body: SecurityIrReplyBody,
    actor: str = Depends(require_tab_write("cases")),
):
    """
    Post a comment on a native AWS Security Incident Response case (AMS
    account). Separate from /reply (Support API) because SIR has its own
    comment API, and gated by the "cases" tab WRITE grant — a reply on a
    live security incident goes to AWS responders under the shared
    infra_portal identity, so it needs the same explicit grant as creating
    a case. Text only; the acting Portal user's email is prefixed into the
    comment (see escalation.add_security_ir_comment).
    """
    import asyncio

    if ENV == "local":
        raise HTTPException(404, "Security Incident Response cases are not available in local mode")
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(400, "Reply body cannot be empty")

    from .escalation import add_security_ir_comment, EscalationError
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: add_security_ir_comment(case_id, text, actor))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except EscalationError as e:
        raise HTTPException(502, e.message)
    except Exception as e:
        _logger.error("reply_to_security_ir_case unexpected for %s: %s", case_id, e)
        raise HTTPException(502, "Could not send comment to Security Incident Response")
    _logger.info("security-ir comment posted on case %s by %s", case_id, actor)
    return {"ok": True, "comment_id": result.get("comment_id")}


@app.post("/api/cases/{case_id}/reply")
async def reply_to_case(
    case_id: str,
    body: CaseReplyBody,
    actor: str = Depends(get_actor),
):
    """
    Add a reply (communication) to an existing AWS Support / AMS case.
    """
    import asyncio

    if body.account not in ("own", "ams"):
        raise HTTPException(400, "account must be 'own' or 'ams'")
    if not body.body.strip():
        raise HTTPException(400, "Reply body cannot be empty")

    reply_text = body.body.strip()[:4000]

    if ENV == "local":
        result = {"result": True, "case_id": case_id}
    else:
        from .escalation import add_case_communication, EscalationError
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, lambda: add_case_communication(case_id, body.account, reply_text, body.attachment_set_id)
            )
        except EscalationError as e:
            raise HTTPException(502, e.message)
        except Exception as e:
            _logger.error("reply_to_case unexpected for %s: %s", case_id, e)
            raise HTTPException(502, "Could not send reply to case")

    # If this case is linked to a Job, log the reply in that job's audit
    # trail too — keeps the Job's history complete even though the reply
    # itself lives in AWS Support, not the portal's own data.
    case_map = job_store.build_case_to_job_map()
    linked = case_map.get(case_id)
    if linked and linked.get("job_id"):
        j = job_store.get_job(linked["job_id"])
        if j:
            label = "AWS AMS" if body.account == "ams" else "AWS Support"
            add_audit(j, actor, f"Reply sent to {label} case {case_id}")

    return {"sent": True, "case_id": case_id, "result": result}


# ── AWS Health Dashboard (Your organization health) ──────────────────────────
# Org-wide AWS Health events, fetched via the AMS support account
# (100000000001) — the only account in this Organization with permission
# to call describe_events_for_organization() (see escalation.py). Grouped
# into the same 4 tabs the AWS Health console itself uses: Open and recent
# issues / Scheduled changes / Other notifications / Event log (a flat
# view of everything). Each event is annotated with its shared
# checked/remark/finished/AMS-SR state from health_ack_store.py.

_HEALTH_TAB_KEYS = (
    "open_and_recent_issues", "scheduled_changes", "retirements",
    "lifecycle_events", "other_notifications", "event_log",
)

# 5-minute server-side cache (see cache_utils.py) — describe_events_for_
# organization() pages through every org-wide AWS Health event on every
# call, which was making the Health Dashboard page noticeably slow to
# load. A single cache key is fine here (unlike Cost Analysis/Billing by
# Service, this endpoint has no per-request filter params).
_HEALTH_DASHBOARD_CACHE = TTLCache(ttl_seconds=5 * 60)

# AI executive summaries (Bedrock) are now also rendered automatically on
# the main Dashboard's "Executive Summary" section (not just on-demand via
# each source page's own "AI Analysis" button), so every Dashboard page
# load would otherwise trigger a fresh Bedrock invocation for both Cost
# Analysis and Billing by Service. Cached for 15 minutes to keep Bedrock
# spend/latency reasonable for a page every management user is expected to
# load frequently, while still refreshing often enough to stay useful.
_COST_AI_SUMMARY_CACHE = TTLCache(ttl_seconds=15 * 60)
_BILLING_AI_SUMMARY_CACHE = TTLCache(ttl_seconds=15 * 60)
_SECURITY_HUB_AI_SUMMARY_CACHE = TTLCache(ttl_seconds=15 * 60)

# Event-type codes that are pulled OUT of "Scheduled changes" into their
# own dedicated tabs (per user request) — these are all AWS-classified
# eventTypeCategory=="scheduledChange" events, but grouped separately here
# since they represent distinct operational concerns (forced retirement /
# eviction of a resource vs. a routine maintenance window) that engineers
# want to review on their own, uncluttered by every other scheduled change.
_RETIREMENT_EVENT_CODES = {
    "AWS_CLOUDSHELL_PERSISTENCE_EXPIRING",
    "AWS_EKS_FARGATE_POD_EVICTION",
    "AWS_ECS_TASK_PATCHING_RETIREMENT",
    "AWS_DIRECTCONNECT_MAINTENANCE_SCHEDULED",
}
_LIFECYCLE_EVENT_CODES = {
    "AWS_RDS_ENGINE_UPGRADE",
    "AWS_LAMBDA_PLANNED_LIFECYCLE_EVENT",
    "AWS_EC2_PLANNED_LIFECYCLE_EVENT",
    "AWS_EKS_PLANNED_LIFECYCLE_EVENT",
    "AWS_RDS_PLANNED_LIFECYCLE_EVENT",
    "AWS_MQ_PLANNED_LIFECYCLE_EVENT",
    "AWS_SSM_PLANNED_LIFECYCLE_EVENT",
    "AWS_BILLING_PLANNED_LIFECYCLE_EVENT",
    "AWS_CLOUDWATCHSYNTHETICS_PLANNED_LIFECYCLE_EVENT",
    "AWS_CLOUDWATCH_PLANNED_LIFECYCLE_EVENT",
    "AWS_ORGANIZATIONS_PLANNED_LIFECYCLE_EVENT",
    "AWS_ECR_PLANNED_LIFECYCLE_EVENT",
    "AWS_ES_PLANNED_LIFECYCLE_EVENT",
    "AWS_S3_PLANNED_LIFECYCLE_EVENT",
    "AWS_SDK_PLANNED_LIFECYCLE_EVENT",
    "AWS_HEALTH_PLANNED_LIFECYCLE_EVENT",
    "AWS_TRUSTEDADVISOR_PLANNED_LIFECYCLE_EVENT",
    "AWS_MOBILETARGETING_PLANNED_LIFECYCLE_EVENT",
    "AWS_QDEVELOPER_PLANNED_LIFECYCLE_EVENT",
    "AWS_DS_PLANNED_LIFECYCLE_EVENT",
    "AWS_REDSHIFT_PLANNED_LIFECYCLE_EVENT",
}

# Event-type codes pulled OUT of whatever AWS classifies them as into
# "Other notifications" specifically (per user request) — same carve-out
# pattern as retirements/lifecycle above, just routed to a different
# dedicated tab.
_OTHER_NOTIFICATION_EVENT_CODES = {
    "AWS_EKS_FARGATE_POD_EVICTIONS",
}

# Admin drag-and-drop feature: an admin can manually move an individual
# event to a different tab than what its AWS eventTypeCategory/
# eventTypeCode would normally place it in (e.g. dragging an
# AWS_SDK_PLANNED_LIFECYCLE_EVENT — AWS-classified as scheduledChange —
# out of "Scheduled changes" into "Lifecycle event"). The override is
# persisted per-event (health_ack_store.set_category_override, keyed by
# ARN) and always takes precedence over the automatic
# category/event-code-based routing in _group_health_events() below.
# "event_log" is intentionally excluded — it's a flat view of every
# event, not a real category an event can be filed under.
_OVERRIDABLE_TAB_KEYS = {k for k in _HEALTH_TAB_KEYS if k != "event_log"}


def _local_mock_health_events() -> list[dict]:
    """ENV=local has no real Organization/AMS account to query — return a
    small fixed set of mock events so the Health Dashboard page still has
    something to render and exercise locally."""
    now = datetime.utcnow()
    return [
        {
            "arn": "arn:aws:health:us-east-1::event/EC2/AWS_EC2_OPERATIONAL_ISSUE/local-mock-issue-1",
            "service": "EC2", "eventTypeCode": "AWS_EC2_OPERATIONAL_ISSUE", "eventTypeCategory": "issue",
            "region": "us-east-1", "statusCode": "open",
            "startTime": now.isoformat(), "lastUpdatedTime": now.isoformat(),
        },
        {
            "arn": "arn:aws:health:ap-southeast-1::event/RDS/AWS_RDS_MAINTENANCE_SCHEDULED/local-mock-change-1",
            "service": "RDS", "eventTypeCode": "AWS_RDS_MAINTENANCE_SCHEDULED", "eventTypeCategory": "scheduledChange",
            "region": "ap-southeast-1", "statusCode": "upcoming",
            "startTime": now.isoformat(), "lastUpdatedTime": now.isoformat(),
        },
        {
            "arn": "arn:aws:health:global::event/IAM/AWS_IAM_ACCOUNT_NOTIFICATION/local-mock-notif-1",
            "service": "IAM", "eventTypeCode": "AWS_IAM_ACCOUNT_NOTIFICATION", "eventTypeCategory": "accountNotification",
            "region": "global", "statusCode": "closed",
            "startTime": now.isoformat(), "lastUpdatedTime": now.isoformat(),
        },
    ]


def _local_mock_health_event_detail(event_arn: str) -> dict:
    return {
        "event_arn": event_arn,
        "event_description": (
            "This is a local-dev mock event description. In production this "
            "field is AWS's own narrative for the event, fetched via "
            "describe_event_details(_for_organization) — e.g. what happened, "
            "which resources/actions are affected, and any recommended steps."
        ),
        "event_status_code": "open",
        "affected_accounts": ["100000000001"],
        "affected_entities": [
            {"entity_value": "i-0123456789abcdef0", "aws_account_id": "100000000001",
             "status_code": "impaired", "last_updated_time": datetime.utcnow().isoformat()},
        ],
    }


def _norm_health_event(raw: dict, checks_map: dict) -> dict:
    """Shape a raw boto3 Health event dict (or the local mock shape above)
    into the flat structure the frontend renders, merging in this event's
    shared checked/remark/finished/AMS-SR state (if any) from
    health_ack_store.list_checks()."""
    arn = raw.get("arn")
    check = checks_map.get(arn) or {}
    return {
        "arn": arn,
        "service": raw.get("service"),
        "event_type_code": raw.get("eventTypeCode"),
        "event_type_category": raw.get("eventTypeCategory"),
        "region": raw.get("region"),
        "status_code": raw.get("statusCode"),
        "start_time": raw.get("startTime").isoformat() if hasattr(raw.get("startTime"), "isoformat") else raw.get("startTime"),
        "last_updated_time": raw.get("lastUpdatedTime").isoformat() if hasattr(raw.get("lastUpdatedTime"), "isoformat") else raw.get("lastUpdatedTime"),
        "checked": bool(check.get("checked")),
        "checked_by_email": check.get("checked_by_email"),
        "checked_by_name": check.get("checked_by_name"),
        "checked_at": check.get("checked_at"),
        "remark": check.get("remark"),
        "remark_by_name": check.get("remark_by_name"),
        "remark_at": check.get("remark_at"),
        "finished": bool(check.get("finished")),
        "finished_by_name": check.get("finished_by_name"),
        "finished_at": check.get("finished_at"),
        "ams_sr_case_id": check.get("ams_sr_case_id"),
        "ams_sr_case_status": check.get("ams_sr_case_status"),
        "ams_sr_raised_by_name": check.get("ams_sr_raised_by_name"),
        "ams_sr_raised_at": check.get("ams_sr_raised_at"),
        "job_id": check.get("job_id"),
        "job_ticket_number": check.get("job_ticket_number"),
        "job_created_by_name": check.get("job_created_by_name"),
        "job_created_at": check.get("job_created_at"),
        "category_override": check.get("category_override"),
        "category_override_by_name": check.get("category_override_by_name"),
        "category_override_at": check.get("category_override_at"),
        # Set only for events the organization API never returned and that
        # were found via a member account's own Health API — the account(s)
        # that see the event. None for ordinary organization events.
        "member_account_ids": raw.get("_member_account_ids"),
    }


def _org_or_member_event_detail(event_arn: str) -> dict:
    """Event detail from the organization APIs; if those know nothing about
    this ARN (an account-specific event the organization view never
    returned), read it from the member account(s) that do see it."""
    from .escalation import get_ams_organization_health_event_detail
    from . import health_member_accounts

    detail = get_ams_organization_health_event_detail(event_arn)
    if detail.get("affected_accounts") or detail.get("affected_entities") or detail.get("event_description"):
        return detail
    try:
        member_detail = health_member_accounts.get_member_event_detail(event_arn)
    except Exception as e:
        _logger.warning("member-account detail lookup failed for %s: %s", event_arn, e)
        member_detail = None
    return member_detail or detail


_CATEGORY_KEY_TO_TAB = {
    "issue": "open_and_recent_issues",
    "scheduledChange": "scheduled_changes",
    "accountNotification": "other_notifications",
}


def _group_health_events(events: list[dict]) -> dict:
    """Group normalized events into the AWS Health console-style tabs.
    event_log is a flat view of everything (sorted newest-updated-first);
    the other tabs filter by eventTypeCategory, with two carve-outs:
    "Retirements" and "Lifecycle event" pull specific event_type_code
    values out of scheduledChange into their own dedicated tabs (per user
    request) rather than leaving them mixed into "Scheduled changes".

    An admin's manual category_override (drag-and-drop between tabs, see
    health_ack_store.set_category_override) always takes precedence over
    this automatic classification, for that one event only."""
    by_category = {"issue": [], "scheduledChange": [], "accountNotification": []}
    retirements: list[dict] = []
    lifecycle_events: list[dict] = []
    for e in events:
        override = e.get("category_override")
        if override in _OVERRIDABLE_TAB_KEYS:
            if override == "retirements":
                retirements.append(e)
            elif override == "lifecycle_events":
                lifecycle_events.append(e)
            else:
                # override is one of open_and_recent_issues /
                # scheduled_changes / other_notifications — translate the
                # tab key back to the by_category dict's AWS-style key.
                inverse = {v: k for k, v in _CATEGORY_KEY_TO_TAB.items()}
                by_category[inverse[override]].append(e)
            continue

        cat = e.get("event_type_category")
        code = e.get("event_type_code")
        if code in _OTHER_NOTIFICATION_EVENT_CODES:
            # Carve-out applies regardless of AWS's own eventTypeCategory
            # for this code (per user request) — checked before the
            # scheduledChange-only carve-outs below since a code could in
            # principle land in either bucket otherwise.
            by_category["accountNotification"].append(e)
        elif cat == "scheduledChange" and code in _RETIREMENT_EVENT_CODES:
            retirements.append(e)
        elif cat == "scheduledChange" and code in _LIFECYCLE_EVENT_CODES:
            lifecycle_events.append(e)
        elif cat in by_category:
            by_category[cat].append(e)
        else:
            # investigation / any future category AWS adds — surface it
            # under "Other notifications" rather than dropping it silently.
            by_category["accountNotification"].append(e)

    event_log = sorted(events, key=lambda e: e.get("last_updated_time") or "", reverse=True)

    result = {
        "open_and_recent_issues": by_category["issue"],
        "scheduled_changes": by_category["scheduledChange"],
        "retirements": retirements,
        "lifecycle_events": lifecycle_events,
        "other_notifications": by_category["accountNotification"],
        "event_log": event_log,
    }
    result["counts"] = {k: len(result[k]) for k in _HEALTH_TAB_KEYS}
    return result


def _local_mock_cost_summary() -> dict:
    """ENV=local has no real cross-account Cost Explorer access — return a
    small fixed set of mock account rows so the Cost Analysis page still
    has something to render and exercise locally. Shape mirrors the real
    get_organization_cost_summary() output, including the per-period
    usage/marketplace/shield/support/credits breakdown, per-account
    per-service breakdown (for the drill-down modal), month-over-month
    trend %, org-wide top services for both MTD and last month (for the
    charts), and BU subtotals (see cost_explorer.py ACCOUNT_BU) so the
    finance-workbook-style UI can be built and tested without real AWS
    calls.

    Uses a locally-scoped random.Random(42) (never the global `random`
    module state) so this mock generator can't interfere with any other
    code in the same process that also uses `random`, while still being
    fully deterministic across reloads."""
    import random as _random

    from .cost_explorer import ACCOUNTS, ACCOUNT_BU, _pct_change, _top_services, _round2

    rng = _random.Random(42)

    MOCK_SERVICES = [
        "Amazon Elastic Compute Cloud - Compute", "Amazon Relational Database Service",
        "Amazon Simple Storage Service", "Amazon Elastic Container Service",
        "AWS Lambda", "Amazon CloudFront", "Amazon Virtual Private Cloud",
        "Amazon Elastic Block Store", "AWS Key Management Service",
        "Amazon CloudWatch", "Amazon Route 53", "Elastic Load Balancing",
        "Amazon DynamoDB", "AWS Glue", "Amazon OpenSearch Service",
        "Amazon SageMaker", "AWS Backup", "Amazon ElastiCache",
    ]

    def mock_service_breakdown(usage_total: float) -> tuple[list[dict], dict[str, float]]:
        """Returns (top_N_folded_list, full_untruncated_map) — mirrors
        cost_explorer.py's split between the per-account truncated list
        and the full map kept for org-wide aggregation."""
        weights = [rng.uniform(0.3, 1.0) for _ in MOCK_SERVICES]
        wsum = sum(weights) or 1.0
        full_map = {svc: _round2(usage_total * (w / wsum)) for svc, w in zip(MOCK_SERVICES, weights)}
        return _top_services(full_map), full_map

    def mock_period(scale: float) -> dict:
        usage = _round2(rng.uniform(50, 20000) * scale)
        marketplace = _round2(usage * rng.uniform(0, 0.15))
        shield = _round2(usage * rng.uniform(0, 0.01))
        support = _round2(usage * rng.uniform(0.02, 0.08))
        credits = -_round2(usage * rng.uniform(0, 0.05))
        subtotal = _round2(usage + marketplace + shield + support + credits)
        return {
            "usage": usage, "marketplace": marketplace, "shield": shield,
            "support": support, "credits": credits,
            # Manual adjustments (extra fees/corrections) start at 0.0
            # here — mirrors cost_explorer._empty_breakdown()'s shape
            # exactly, so mock and real summaries always have the same
            # bucket keys. Populated by apply_manual_adjustments() in
            # _cost_summary_with_adjustments() when any exist.
            "adjustments": 0.0, "subtotal": subtotal,
        }

    accounts = []
    full_services_mtd_by_account: list[dict[str, float]] = []
    full_services_last_month_by_account: list[dict[str, float]] = []
    for acct_id, name in ACCOUNTS.items():
        two_months_ago = mock_period(rng.uniform(0.85, 1.15))
        last_month = mock_period(1.0)
        mtd = mock_period(rng.uniform(0.05, 0.4))
        services_mtd, full_mtd = mock_service_breakdown(mtd["usage"])
        services_last_month, full_last_month = mock_service_breakdown(last_month["usage"])
        full_services_mtd_by_account.append(full_mtd)
        full_services_last_month_by_account.append(full_last_month)
        accounts.append({
            "account_id": acct_id, "account_name": name,
            "bu": ACCOUNT_BU.get(acct_id, "Other"),
            "mtd": mtd, "last_month": last_month, "two_months_ago": two_months_ago,
            "services_mtd": services_mtd,
            "services_last_month": services_last_month,
            "mtd_vs_last_month_pct": _pct_change(mtd["subtotal"], last_month["subtotal"]),
            "last_month_vs_prior_pct": _pct_change(last_month["subtotal"], two_months_ago["subtotal"]),
            "mtd_cost": mtd["subtotal"], "last_month_cost": last_month["subtotal"],
            "currency": "USD", "error": None,
        })
    accounts.sort(key=lambda r: r["mtd_cost"], reverse=True)

    bu_totals: dict = {}
    for a in accounts:
        bt = bu_totals.setdefault(a["bu"], {"bu": a["bu"], "mtd_cost": 0.0, "last_month_cost": 0.0, "account_count": 0})
        bt["mtd_cost"] += a["mtd_cost"]
        bt["last_month_cost"] += a["last_month_cost"]
        bt["account_count"] += 1
    business_units = sorted(
        [{**bt, "mtd_cost": _round2(bt["mtd_cost"]), "last_month_cost": _round2(bt["last_month_cost"])} for bt in bu_totals.values()],
        key=lambda b: b["mtd_cost"], reverse=True,
    )

    # Org-wide top services from the FULL (untruncated) per-account maps,
    # not each account's already-folded services_mtd/services_last_month —
    # keeps long-tail spend represented at the org level.
    def aggregate(full_maps: list[dict[str, float]]) -> list[dict]:
        totals: dict[str, float] = {}
        for m in full_maps:
            for svc, amount in m.items():
                totals[svc] = totals.get(svc, 0.0) + amount
        return _top_services(totals, limit=12)

    top_services_mtd = aggregate(full_services_mtd_by_account)
    top_services_last_month = aggregate(full_services_last_month_by_account)

    total_mtd = _round2(sum(a["mtd_cost"] for a in accounts))
    total_last_month = _round2(sum(a["last_month_cost"] for a in accounts))
    total_two_months_ago = _round2(sum(a["two_months_ago"]["subtotal"] for a in accounts))

    return {
        "accounts": accounts,
        "business_units": business_units,
        "top_services_mtd": top_services_mtd,
        "top_services_last_month": top_services_last_month,
        "total_mtd_cost": total_mtd,
        "total_last_month_cost": total_last_month,
        "total_two_months_ago_cost": total_two_months_ago,
        "mtd_vs_last_month_pct": _pct_change(total_mtd, total_last_month),
        "last_month_vs_prior_pct": _pct_change(total_last_month, total_two_months_ago),
        "currency": "USD",
        "account_count": len(accounts),
        "error_count": 0,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cache_age_seconds": 0,
        "is_mock": True,
    }


def _cost_summary_with_adjustments(force_refresh: bool = False) -> dict:
    """Fetch the org-wide cost summary (real Cost Explorer or local mock)
    and layer any admin-entered manual adjustments on top of it. Shared
    by both the table route and the AI-summary route so the AI briefing
    always reasons about the same adjusted numbers the dashboard shows."""
    from .cost_explorer import apply_manual_adjustments

    if ENV == "local":
        base_summary = _local_mock_cost_summary()
    else:
        from .cost_explorer import get_organization_cost_summary
        base_summary = get_organization_cost_summary(force_refresh=force_refresh)

    adjustments = cost_adjustment_store.list_adjustments()
    if not adjustments:
        return base_summary
    return apply_manual_adjustments(base_summary, adjustments)


@app.get("/api/cost-analysis/organization")
def get_org_cost_analysis(refresh: bool = False, actor: str = Depends(get_actor)):
    """
    Cost Analysis tab — month-to-date and previous-month AWS spend across
    the whole AWS Organization. Core account (100000000001) is queried
    directly; every other account via sts:AssumeRole(PortalReadOnlyRole).
    Results are cached server-side for 10 minutes; pass ?refresh=true to
    bypass the cache. Any admin-entered manual adjustments (extra fees /
    corrections — see /api/cost-analysis/adjustments) are layered on top
    of the live AWS numbers before being returned; the underlying
    Cost Explorer data is never modified by an adjustment.
    """
    from .cost_explorer import CostAnalysisError
    try:
        return _cost_summary_with_adjustments(force_refresh=refresh)
    except CostAnalysisError as e:
        _logger.error("get_org_cost_analysis: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch AWS Cost Analysis data")
    except Exception:
        _logger.exception("get_org_cost_analysis unexpected error")
        raise HTTPException(502, "Could not fetch AWS Cost Analysis data")


@app.get("/api/cost-analysis/ai-summary")
def get_cost_analysis_ai_summary(refresh: bool = False, actor: str = Depends(get_actor)):
    """
    On-demand AI executive summary of the org-wide Cost Analysis data —
    for the "AI Analysis" button on the Cost Analysis page. Reuses the
    same cached (and adjustment-layered) cost summary the main table
    renders from (or the local mock in ENV=local) and asks Bedrock (Nova
    Pro/Nova 2 Lite) to turn it into a management-ready briefing: trend,
    key cost drivers, and concrete cost-control recommendations.
    Read-only — never changes any AWS resource.
    """
    if ENV == "local":
        # Local/offline mode: never call Bedrock. Return a deterministic,
        # clearly-labelled mock AI summary derived from the same mock cost
        # data (with any manual adjustments applied) the dashboard
        # renders, so the "AI Analysis" button is fully exercisable
        # without network/Bedrock access.
        cost_summary = _cost_summary_with_adjustments()
        top_bu = (cost_summary.get("business_units") or [{"bu": "N/A"}])[0]
        return {
            "executive_summary": (
                f"(Local mock) MTD spend is {cost_summary['currency']} {cost_summary['total_mtd_cost']:,.2f} "
                f"across {cost_summary['account_count']} accounts; last full month was "
                f"{cost_summary['currency']} {cost_summary['total_last_month_cost']:,.2f}."
            ),
            "trend": "stable",
            "key_cost_drivers": [f"{top_bu['bu']} business unit is the largest MTD spender (mock data)"],
            "recommendations": [
                "This is local mock data — no live Bedrock call was made.",
                "Run with ENV=production against real Cost Explorer data for a real AI summary.",
            ],
            "bottom_line": "Local mock summary — for development only.",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model_used": "local-mock",
            "bedrock_region": "n/a",
            "is_mock": True,
        }

    from .ai_engine import analyse_cost_summary
    from .cost_explorer import CostAnalysisError
    try:
        cost_summary = _cost_summary_with_adjustments(force_refresh=refresh)
    except CostAnalysisError:
        _logger.exception("get_cost_analysis_ai_summary: could not load cost summary")
        raise HTTPException(502, "Could not generate AI cost analysis summary")
    except Exception:
        _logger.exception("get_cost_analysis_ai_summary: unexpected error loading cost summary")
        raise HTTPException(502, "Could not generate AI cost analysis summary")

    try:
        # Cached for 15 minutes (see _COST_AI_SUMMARY_CACHE above) — the
        # Dashboard's Executive Summary section calls this on every page
        # load, so this avoids a fresh Bedrock invocation every time.
        return _COST_AI_SUMMARY_CACHE.get_or_fetch(
            "org", lambda: analyse_cost_summary(cost_summary), force_refresh=refresh,
        )
    except Exception:
        _logger.exception("get_cost_analysis_ai_summary: unexpected error generating AI summary")
        raise HTTPException(502, "Could not generate AI cost analysis summary")


# ── Cost Analysis — manual adjustments (extra fees / corrections) ───────────

class CreateCostAdjustmentBody(BaseModel):
    account_id: str
    period: Optional[str] = None    # "YYYY-MM"; defaults to current MTD month if omitted
    label: str
    amount: float
    currency: str = "USD"
    note: Optional[str] = None


class UpdateCostAdjustmentBody(BaseModel):
    label: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    note: Optional[str] = None


@app.get("/api/cost-analysis/adjustments")
def list_cost_adjustments(account_id: Optional[str] = None, actor: str = Depends(get_actor)):
    """List manual cost adjustments (any portal user can view — visible
    read-only in the dashboard's per-account breakdown), optionally
    filtered to one account_id."""
    return cost_adjustment_store.list_adjustments(account_id=account_id)


@app.post("/api/cost-analysis/adjustments", status_code=201)
def create_cost_adjustment(body: CreateCostAdjustmentBody, actor: str = Depends(require_admin)):
    """
    Add a manual cost adjustment for one account/month — e.g. an extra
    partner-invoiced fee or a known billing correction that Cost Explorer
    doesn't show. Admin-only: this directly changes the numbers everyone
    sees on the dashboard, so it's restricted the same way user
    management is.
    """
    from .cost_explorer import ACCOUNTS, period_key_for

    account_id = body.account_id.strip()
    if account_id not in ACCOUNTS:
        raise HTTPException(400, f"Unknown AWS account_id: {account_id}")

    label = body.label.strip()[:200]
    if not label:
        raise HTTPException(400, "label is required")

    period = (body.period or "").strip() or period_key_for("mtd")
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        raise HTTPException(400, "period must be in YYYY-MM format")

    currency = (body.currency or "USD").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise HTTPException(400, "currency must be a 3-letter code, e.g. USD")

    try:
        amount = float(body.amount)
    except (TypeError, ValueError):
        raise HTTPException(400, "amount must be a number")
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise HTTPException(400, "amount must be a finite number")

    actor_name = TEAM_MEMBERS.get(actor, actor)
    return cost_adjustment_store.create_adjustment(
        account_id=account_id, period=period, label=label, amount=amount,
        currency=currency, note=(body.note or "").strip()[:1000] or None,
        actor_email=actor, actor_name=actor_name,
    )


@app.patch("/api/cost-analysis/adjustments/{adjustment_id}")
def update_cost_adjustment(adjustment_id: str, body: UpdateCostAdjustmentBody, actor: str = Depends(require_admin)):
    """Edit an existing manual cost adjustment (admin-only)."""
    label = body.label.strip()[:200] if body.label is not None else None
    if label is not None and not label:
        raise HTTPException(400, "label cannot be empty")

    amount = None
    if body.amount is not None:
        try:
            amount = float(body.amount)
        except (TypeError, ValueError):
            raise HTTPException(400, "amount must be a number")
        if amount != amount or amount in (float("inf"), float("-inf")):
            raise HTTPException(400, "amount must be a finite number")

    currency = None
    if body.currency is not None:
        currency = body.currency.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise HTTPException(400, "currency must be a 3-letter code, e.g. USD")

    note = body.note.strip()[:1000] if body.note is not None else None

    actor_name = TEAM_MEMBERS.get(actor, actor)
    updated = cost_adjustment_store.update_adjustment(
        adjustment_id, label=label, amount=amount, currency=currency, note=note,
        actor_email=actor, actor_name=actor_name,
    )
    if not updated:
        raise HTTPException(404, "Adjustment not found")
    return updated


@app.delete("/api/cost-analysis/adjustments/{adjustment_id}")
def delete_cost_adjustment(adjustment_id: str, actor: str = Depends(require_admin)):
    """Remove a manual cost adjustment (admin-only)."""
    ok = cost_adjustment_store.delete_adjustment(adjustment_id)
    if not ok:
        raise HTTPException(404, "Adjustment not found")
    return {"result": True, "id": adjustment_id}



# ── Billing by Service — per-account, per-month AWS spend by service ───────
# Ported from the standalone billing_by_service.py CLI script: mirrors the
# Billing Console's "Bills -> Charges by service" view per account/month,
# with EC2 Compute+Other merged, Data Transfer usage types folded into
# their own row, and AWS Marketplace items detected + labelled. Read-only
# — any portal user can view/export; no adjustments or writes involved.

@app.get("/api/billing-by-service/accounts")
def list_billing_by_service_accounts(actor: str = Depends(get_actor)):
    """Account picker data (id + alias) for the Billing by Service page."""
    from .billing_by_service import list_accounts
    return {"accounts": list_accounts()}


def _parse_billing_months(months_param: Optional[str], month_param: Optional[str]) -> list[str]:
    if months_param:
        months = [m.strip() for m in months_param.split(",") if m.strip()]
    elif month_param:
        months = [month_param.strip()]
    else:
        raise HTTPException(400, "Specify 'month' (YYYY-MM) or 'months' (comma-separated YYYY-MM list)")
    if not months:
        raise HTTPException(400, "No valid months provided")
    if len(months) > 24:
        raise HTTPException(400, "Too many months requested (max 24)")
    for m in months:
        if not re.fullmatch(r"\d{4}-\d{2}", m):
            raise HTTPException(400, f"Invalid month format: {m!r} (expected YYYY-MM)")
    return months


def _parse_billing_accounts(accounts_param: Optional[str]) -> list[str]:
    from .billing_by_service import ACCOUNT_ALIAS

    if not accounts_param:
        return list(ACCOUNT_ALIAS.keys())
    account_ids = [a.strip() for a in accounts_param.split(",") if a.strip()]
    unknown = [a for a in account_ids if a not in ACCOUNT_ALIAS]
    if unknown:
        raise HTTPException(400, f"Unknown account_id(s): {', '.join(unknown)}")
    return account_ids


@app.get("/api/billing-by-service/report")
def get_billing_by_service_report(
    month: Optional[str] = None,
    months: Optional[str] = None,
    accounts: Optional[str] = None,
    refresh: bool = False,
    actor: str = Depends(get_actor),
):
    """
    Per-account, per-month AWS spend by service — same shape/classification
    as the Billing Console's "Charges by service" view. Any portal user can
    view (read-only, no adjustments involved).

    Query params:
      - month: single 'YYYY-MM' (mutually exclusive with `months`)
      - months: comma-separated 'YYYY-MM' list, e.g. "2025-10,2025-11,2026-03"
      - accounts: comma-separated AWS account IDs; omit for all known accounts
      - refresh: bypass the 10-minute server-side cache
    """
    from .billing_by_service import get_billing_report
    from .cost_explorer import CostAnalysisError

    parsed_months = _parse_billing_months(months, month)
    parsed_accounts = _parse_billing_accounts(accounts)
    try:
        return get_billing_report(parsed_accounts, parsed_months, force_refresh=refresh)
    except CostAnalysisError as e:
        _logger.error("get_billing_by_service_report: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch AWS billing-by-service data")
    except Exception:
        _logger.exception("get_billing_by_service_report unexpected error")
        raise HTTPException(502, "Could not fetch AWS billing-by-service data")


@app.get("/api/billing-by-service/export")
def export_billing_by_service(
    month: Optional[str] = None,
    months: Optional[str] = None,
    accounts: Optional[str] = None,
    refresh: bool = False,
    use_template: bool = True,
    actor: str = Depends(get_actor),
):
    """
    Same data as GET /report, returned as an Excel workbook download.

    By default (use_template=True) this auto-fills the fetched numbers
    into the team's existing master tracking workbook (one tab per AWS
    account, matching the exact format already in use — header row of
    real month-date cells, per-service rows, a Grand Total row, plus a
    set of manually-maintained helper/notes columns that are left
    untouched): existing service rows are updated in place, brand new
    services get a new row appended just above the Grand Total row (whose
    SUM() formulas are extended to match), and a new month column is
    appended if the requested month(s) aren't in the template yet.

    Pass use_template=false to instead get the original plain export
    (one sheet per account, header row: alias | item | month1 | month2 |
    ...), with no dependency on the master template file.
    """
    from .billing_by_service import (
        get_billing_report, build_workbook, workbook_to_bytes,
        merge_report_into_template, load_template_bytes,
    )
    from .cost_explorer import CostAnalysisError

    parsed_months = _parse_billing_months(months, month)
    parsed_accounts = _parse_billing_accounts(accounts)
    try:
        report = get_billing_report(parsed_accounts, parsed_months, force_refresh=refresh)
    except CostAnalysisError as e:
        _logger.error("export_billing_by_service: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch AWS billing-by-service data")
    except Exception:
        _logger.exception("export_billing_by_service unexpected error")
        raise HTTPException(502, "Could not fetch AWS billing-by-service data")

    if use_template:
        try:
            template_bytes = load_template_bytes()
            xlsx_bytes = merge_report_into_template(report, template_bytes)
            filename = "AWS Cost-Raw-data.xlsx"
        except FileNotFoundError:
            _logger.warning("export_billing_by_service: no master template on disk, falling back to plain export")
            xlsx_bytes = workbook_to_bytes(build_workbook(report))
            stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            filename = f"billing-by-service-{stamp}.xlsx"
        except Exception:
            _logger.exception("export_billing_by_service: template merge failed, falling back to plain export")
            xlsx_bytes = workbook_to_bytes(build_workbook(report))
            stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            filename = f"billing-by-service-{stamp}.xlsx"
    else:
        xlsx_bytes = workbook_to_bytes(build_workbook(report))
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        filename = f"billing-by-service-{stamp}.xlsx"

    return StreamingResponse(
        iter([xlsx_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/billing-by-service/ai-summary")
def get_billing_by_service_ai_summary(
    month: Optional[str] = None,
    months: Optional[str] = None,
    accounts: Optional[str] = None,
    refresh: bool = False,
    actor: str = Depends(get_actor),
):
    """
    On-demand AI executive summary of the Billing by Service data — the
    per-account/per-service counterpart to /api/cost-analysis/ai-summary.
    Used by both the Billing by Service page's own "AI Analysis" button
    and the main Dashboard's automatic "Executive Summary" section.
    Defaults to the current month across all known accounts when no
    query params are given (the Dashboard calls it with no params).
    Cached for 15 minutes server-side; pass ?refresh=true to bypass.
    Read-only — never changes any AWS resource.
    """
    from .billing_by_service import get_billing_report, ACCOUNT_ALIAS
    from .cost_explorer import CostAnalysisError

    parsed_months = _parse_billing_months(months, month) if (months or month) else [datetime.utcnow().strftime("%Y-%m")]
    parsed_accounts = _parse_billing_accounts(accounts)

    if ENV == "local":
        # Local/offline mode: never call Bedrock. Return a deterministic,
        # clearly-labelled mock AI summary derived from the same mock
        # billing data the page renders, mirroring the cost-analysis
        # local-mock branch above.
        try:
            billing_report = get_billing_report(parsed_accounts, parsed_months, force_refresh=refresh)
        except CostAnalysisError as e:
            raise HTTPException(502, str(e))
        valid_accounts = [a for a in billing_report.get("accounts", []) if not a.get("error")]
        latest_month = parsed_months[-1]
        total_latest = sum((a.get("totals", {}).get(latest_month, 0) or 0) for a in valid_accounts)
        top_account = max(valid_accounts, key=lambda a: a.get("totals", {}).get(latest_month, 0) or 0, default=None)
        return {
            "executive_summary": (
                f"(Local mock) Total spend across {len(valid_accounts)} account(s) for "
                f"{billing_report.get('month_labels', parsed_months)[-1]} is USD {total_latest:,.2f}."
            ),
            "trend": "stable",
            "key_cost_drivers": [f"{top_account['alias']} is the largest spender this month (mock data)"] if top_account else [],
            "recommendations": [
                "This is local mock data — no live Bedrock call was made.",
                "Run with ENV=production against real Billing data for a real AI summary.",
            ],
            "bottom_line": "Local mock summary — for development only.",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model_used": "local-mock",
            "bedrock_region": "n/a",
            "is_mock": True,
        }

    from .ai_engine import analyse_billing_summary
    try:
        billing_report = get_billing_report(parsed_accounts, parsed_months, force_refresh=refresh)
    except CostAnalysisError:
        _logger.exception("get_billing_by_service_ai_summary: could not load billing report")
        raise HTTPException(502, "Could not generate AI billing-by-service summary")
    except Exception:
        _logger.exception("get_billing_by_service_ai_summary: unexpected error loading billing report")
        raise HTTPException(502, "Could not generate AI billing-by-service summary")

    try:
        # Cached for 15 minutes (see _BILLING_AI_SUMMARY_CACHE above),
        # keyed by the exact (accounts, months) combination so different
        # filter selections don't share a stale cache entry.
        cache_key = (tuple(sorted(parsed_accounts)), tuple(parsed_months))
        return _BILLING_AI_SUMMARY_CACHE.get_or_fetch(
            cache_key, lambda: analyse_billing_summary(billing_report), force_refresh=refresh,
        )
    except Exception:
        _logger.exception("get_billing_by_service_ai_summary: unexpected error generating AI summary")
        raise HTTPException(502, "Could not generate AI billing-by-service summary")


# ── RI & Savings Plan Saved Cost — per-account, per-month realized savings ──
# Savings Plan net savings, per-account per-service — CUR-style
# (GetCostAndUsage filtered to RECORD_TYPE=SavingsPlanCoveredUsage,
# grouped by SERVICE, queried independently PER ACCOUNT — same
# per-account AssumeRole fan-out as Billing by Service, not a single
# shared org-wide call), matching the Billing Console's Bills > Savings
# breakdown. See app/ri_sp_savings.py docstring for why the
# commitment-utilization APIs (GetSavingsPlansUtilization /
# GetReservationUtilization) aren't used here, and why a single
# the organization-Core-scoped org-wide call was tried and found to silently miss
# accounts that have real Savings Plan coverage. Read-only for everyone,
# same convention as Cost Analysis / Billing by Service. There is no
# manual "Refresh" button on this page — the report cache is refreshed
# once a day at 08:00 HKT (see ri_sp_savings._current_refresh_epoch()).

@app.get("/api/ri-sp-savings/accounts")
def list_ri_sp_savings_accounts(actor: str = Depends(get_actor)):
    """Account picker data (id + alias) for the RI & Savings Plan Saved
    Cost page — same account superset Billing by Service uses."""
    from .ri_sp_savings import list_accounts
    return {"accounts": list_accounts()}


@app.get("/api/ri-sp-savings/report")
def get_ri_sp_savings_report_endpoint(
    month: Optional[str] = None,
    months: Optional[str] = None,
    accounts: Optional[str] = None,
    refresh: bool = False,
    actor: str = Depends(get_actor),
):
    """
    Per-account, per-month realized savings from Reserved Instances and
    Savings Plans. Any portal user can view (read-only, no adjustments
    involved).

    Query params:
      - month: single 'YYYY-MM' (mutually exclusive with `months`)
      - months: comma-separated 'YYYY-MM' list, e.g. "2025-10,2025-11,2026-03"
      - accounts: comma-separated AWS account IDs; omit for all known accounts
      - refresh: force-bypass the daily 08:00 HKT cache (kept for
        internal/manual troubleshooting use — there is no button for this
        in the UI per user request)
    """
    from .ri_sp_savings import get_ri_sp_savings_report
    from .cost_explorer import CostAnalysisError

    parsed_months = _parse_billing_months(months, month)
    parsed_accounts = _parse_billing_accounts(accounts)
    try:
        return get_ri_sp_savings_report(parsed_accounts, parsed_months, force_refresh=refresh)
    except CostAnalysisError as e:
        _logger.error("get_ri_sp_savings_report_endpoint: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch AWS RI/Savings Plan savings data")
    except Exception:
        _logger.exception("get_ri_sp_savings_report_endpoint unexpected error")
        raise HTTPException(502, "Could not fetch AWS RI/Savings Plan savings data")


# ── AWS Security Hub tab — findings across all accounts + AI analysis ────────
# Same account superset as Billing by Service / Savings Plan Cost. Any
# logged-in portal user can view (read-only) — findings/remediation
# guidance is informational, never mutates any AWS resource or Security
# Hub workflow state.

@app.get("/api/security-hub/accounts")
def list_security_hub_accounts(actor: str = Depends(get_actor)):
    """Account picker data (id + alias) for the AWS Security Hub page —
    same account superset Billing by Service / Savings Plan Cost use."""
    from .security_hub import list_accounts
    return {"accounts": list_accounts()}


@app.get("/api/security-hub/summary")
def get_security_hub_summary_endpoint(
    accounts: Optional[str] = None,
    refresh: bool = False,
    actor: str = Depends(get_actor),
):
    """
    Active (RecordState=ACTIVE, WorkflowStatus=NEW) Security Hub findings
    across the requested accounts (or all known accounts if omitted),
    grouped per account with org-wide severity totals. Cached for 10
    minutes; pass refresh=true to force a fresh fetch.
    """
    from .security_hub import get_security_hub_summary
    from .cost_explorer import CostAnalysisError

    parsed_accounts = _parse_billing_accounts(accounts)
    try:
        return get_security_hub_summary(parsed_accounts, force_refresh=refresh)
    except CostAnalysisError as e:
        _logger.error("get_security_hub_summary_endpoint: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch AWS Security Hub findings")
    except Exception:
        _logger.exception("get_security_hub_summary_endpoint unexpected error")
        raise HTTPException(502, "Could not fetch AWS Security Hub findings")


@app.get("/api/security-hub/finding-ai-analysis")
def get_security_hub_finding_ai_analysis(
    account_id: str,
    finding_id: str,
    actor: str = Depends(get_actor),
):
    """
    On-demand, per-finding AI analysis — automatically triggered when a
    user clicks a specific finding row on the AWS Security Hub tab (the
    click itself is the trigger, no separate button). Fetches the
    finding's current data directly (not from the cached org-wide
    summary), looks up which IAM users/groups/roles are actually impacted
    (for IAM-policy findings — see security_hub.get_impacted_principals()),
    and returns a concrete, step-by-step remediation plan. Read-only /
    advisory only — never changes the finding's workflow status or any
    IAM entity.
    """
    from .billing_by_service import ACCOUNT_ALIAS
    from .security_hub import get_finding_by_id, get_impacted_principals
    from .cost_explorer import CostAnalysisError

    if account_id not in ACCOUNT_ALIAS:
        raise HTTPException(400, f"Unknown AWS account_id: {account_id}")

    try:
        finding = get_finding_by_id(account_id, finding_id)
    except CostAnalysisError as e:
        _logger.error("get_security_hub_finding_ai_analysis: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch this finding")
    except Exception:
        _logger.exception("get_security_hub_finding_ai_analysis: could not fetch finding")
        raise HTTPException(502, "Could not fetch this finding")

    if finding is None:
        raise HTTPException(404, "Finding not found (it may have been resolved or its workflow status changed)")

    try:
        impacted = get_impacted_principals(account_id, finding)
    except Exception as e:
        _logger.warning("get_security_hub_finding_ai_analysis: impacted-principals lookup failed for %s: %s", finding_id, e)
        impacted = {"supported": False, "error": "Could not look up impacted users/groups"}

    try:
        ai_result = analyse_security_finding(finding, ACCOUNT_ALIAS.get(account_id, account_id), account_id, impacted)
    except Exception:
        _logger.exception("get_security_hub_finding_ai_analysis: unexpected error generating AI analysis")
        raise HTTPException(502, "Could not generate AI analysis for this finding")

    ai_result["impacted_principals"] = impacted
    return ai_result


@app.get("/api/security-hub/ai-summary")
def get_security_hub_ai_summary(
    accounts: Optional[str] = None,
    refresh: bool = False,
    actor: str = Depends(get_actor),
):
    """
    On-demand AI executive summary + prioritized remediation plan for the
    current AWS Security Hub findings — the "AI Analysis" button on the
    AWS Security Hub tab. Cached for 15 minutes per exact account
    selection (same convention as Cost Analysis / Billing by Service AI
    summaries) so repeated clicks don't re-invoke Bedrock unnecessarily.
    Read-only / advisory — never changes any finding's workflow status.
    """
    from .security_hub import get_security_hub_summary
    from .cost_explorer import CostAnalysisError

    parsed_accounts = _parse_billing_accounts(accounts)
    try:
        findings_summary = get_security_hub_summary(parsed_accounts, force_refresh=refresh)
    except CostAnalysisError:
        _logger.exception("get_security_hub_ai_summary: could not load findings summary")
        raise HTTPException(502, "Could not generate AI security summary")
    except Exception:
        _logger.exception("get_security_hub_ai_summary: unexpected error loading findings summary")
        raise HTTPException(502, "Could not generate AI security summary")

    try:
        cache_key = tuple(sorted(parsed_accounts))
        return _SECURITY_HUB_AI_SUMMARY_CACHE.get_or_fetch(
            cache_key, lambda: analyse_security_hub_summary(findings_summary), force_refresh=refresh,
        )
    except Exception:
        _logger.exception("get_security_hub_ai_summary: unexpected error generating AI summary")
        raise HTTPException(502, "Could not generate AI security summary")


# ── AWS WAF Rule Monitoring tab (Security section) — per-account Web ACL
# inventory (which resources each ACL is linked to, and its rule list),
# with per-Web-ACL and per-rule Blocked/Counted/Allowed CloudWatch metrics
# fetched strictly on-demand (see waf_monitoring.py module docstring's
# cost-tier design). Same account superset and cross-account
# AssumeRole(PortalReadOnlyRole) pattern as Security Hub / Billing by
# Service. Read-only for everyone — never modifies any Web ACL, rule, or
# association.
#
# Frontend has two tabs: "Web ACLs by Account" (structural only, tier 1 —
# no CloudWatch at all) and "By Action" (tiers 2-4, opened on demand).
#
# Cost tiers (cheapest -> most expensive; each gated behind an explicit
# user click except tier 1):
#   1. GET /accounts, GET /summary            — structural only, no CloudWatch
#   2. GET /web-acl-metrics                   — one Web ACL's aggregate chart
#   3. GET /rule-metrics                      — one rule's own chart
#   3b. GET /action-breakdown                 — one account + one action -> totals + impacted rules across every Web ACL in that account
#   4. GET /sampled-requests                  — live GetSampledRequests log

@app.get("/api/waf-monitoring/accounts")
def list_waf_monitoring_accounts(actor: str = Depends(get_actor)):
    """Account picker data (id + alias) for the WAF Rule Monitoring page —
    same account superset Billing by Service / Security Hub use."""
    from .waf_monitoring import list_accounts
    return {"accounts": list_accounts()}


@app.get("/api/waf-monitoring/summary")
def get_waf_monitoring_summary_endpoint(
    accounts: Optional[str] = None,
    refresh: bool = False,
    actor: str = Depends(get_actor),
):
    """
    STRUCTURAL-ONLY summary: every WAFv2 Web ACL (REGIONAL + CLOUDFRONT
    scope) across the requested accounts (or all known accounts if
    omitted), each with its associated resources and rule list
    (name/priority/type/action). Deliberately makes NO CloudWatch calls
    (see waf_monitoring.py module docstring's cost-tier design) — that's
    why there's no blocked/counted/allowed count anywhere in this
    response. Cached for 10 minutes; pass refresh=true to force a fresh
    fetch. Per-Web-ACL and per-rule metrics are separate, on-demand
    endpoints below (web-acl-metrics / rule-metrics), fetched only when
    the user explicitly expands a chart.
    """
    from .waf_monitoring import get_waf_summary
    from .cost_explorer import CostAnalysisError

    parsed_accounts = _parse_billing_accounts(accounts)
    try:
        return get_waf_summary(parsed_accounts, force_refresh=refresh)
    except CostAnalysisError as e:
        _logger.error("get_waf_monitoring_summary_endpoint: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch AWS WAF Web ACL data")
    except Exception:
        _logger.exception("get_waf_monitoring_summary_endpoint unexpected error")
        raise HTTPException(502, "Could not fetch AWS WAF Web ACL data")


@app.get("/api/waf-monitoring/web-acl-metrics")
def get_waf_monitoring_web_acl_metrics(
    account_id: str,
    web_acl_name: str,
    web_acl_id: str,
    scope: str,
    time_range: str = "3h",
    actor: str = Depends(get_actor),
):
    """
    On-demand Web-ACL-level aggregate ("Rule=ALL") Blocked/Counted/
    Allowed CloudWatch time series (AWS/WAFV2 metrics) for one Web ACL,
    over a selectable time range (1h/3h/12h/1d/3d/1w). Fired only when a
    user expands a Web ACL card's "Metrics" chart on the WAF Rule
    Monitoring page — never as part of the structural summary — to keep
    CloudWatch API costs proportional to actual usage.
    """
    from .waf_monitoring import get_web_acl_metrics, TIME_RANGE_CHOICES
    from .cost_explorer import CostAnalysisError
    from .billing_by_service import ACCOUNT_ALIAS

    if account_id not in ACCOUNT_ALIAS:
        raise HTTPException(400, f"Unknown AWS account_id: {account_id}")
    if scope not in ("REGIONAL", "CLOUDFRONT"):
        raise HTTPException(400, "scope must be REGIONAL or CLOUDFRONT")
    if time_range not in TIME_RANGE_CHOICES:
        raise HTTPException(400, f"time_range must be one of {TIME_RANGE_CHOICES}")

    try:
        return get_web_acl_metrics(account_id, web_acl_name, web_acl_id, scope, time_range)
    except CostAnalysisError as e:
        _logger.error("get_waf_monitoring_web_acl_metrics: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch Web ACL metrics")
    except Exception:
        _logger.exception("get_waf_monitoring_web_acl_metrics unexpected error")
        raise HTTPException(502, "Could not fetch Web ACL metrics")


@app.get("/api/waf-monitoring/rule-metrics")
def get_waf_monitoring_rule_metrics(
    account_id: str,
    web_acl_name: str,
    web_acl_id: str,
    scope: str,
    rule_name: str,
    time_range: str = "3h",
    actor: str = Depends(get_actor),
):
    """
    On-demand per-rule Blocked/Counted/Allowed CloudWatch time series
    (AWS/WAFV2 metrics), scoped to one specific rule inside one Web ACL,
    over a selectable time range. Fired only when a user clicks into a
    rule's own detail view (one level deeper than the Web-ACL-level
    chart) — never eagerly.
    """
    from .waf_monitoring import get_rule_metrics, TIME_RANGE_CHOICES
    from .cost_explorer import CostAnalysisError
    from .billing_by_service import ACCOUNT_ALIAS

    if account_id not in ACCOUNT_ALIAS:
        raise HTTPException(400, f"Unknown AWS account_id: {account_id}")
    if scope not in ("REGIONAL", "CLOUDFRONT"):
        raise HTTPException(400, "scope must be REGIONAL or CLOUDFRONT")
    if time_range not in TIME_RANGE_CHOICES:
        raise HTTPException(400, f"time_range must be one of {TIME_RANGE_CHOICES}")

    try:
        return get_rule_metrics(account_id, web_acl_name, web_acl_id, scope, rule_name, time_range)
    except CostAnalysisError as e:
        _logger.error("get_waf_monitoring_rule_metrics: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch rule metrics")
    except Exception:
        _logger.exception("get_waf_monitoring_rule_metrics unexpected error")
        raise HTTPException(502, "Could not fetch rule metrics")


@app.get("/api/waf-monitoring/action-breakdown")
def get_waf_monitoring_action_breakdown(
    account_id: str,
    action: str,
    time_range: str = "3h",
    actor: str = Depends(get_actor),
):
    """
    On-demand "By Action" breakdown, ACCOUNT-LEVEL: the caller picks one
    AWS account and one action (Block/Allow/Count/CAPTCHA/Challenge) —
    there is no Web ACL selector on this endpoint (or the frontend tab it
    backs). Every Web ACL already known to exist in that account (via
    the cached structural summary) is queried for just that single
    action and merged into one account-wide total plus a combined
    "impacted rules" list, each entry tagged with which Web ACL it came
    from. Fired only when a user opens the "By Action" tab, picks an
    account, AND picks an action — never eagerly, and never for all five
    actions at once (keeps the CloudWatch cost to ~1 GetMetricData call
    per Web ACL in the account, not per rule and not per action).
    """
    from .waf_monitoring import get_account_action_breakdown, TIME_RANGE_CHOICES, ACTION_CHOICES
    from .cost_explorer import CostAnalysisError
    from .billing_by_service import ACCOUNT_ALIAS

    if account_id not in ACCOUNT_ALIAS:
        raise HTTPException(400, f"Unknown AWS account_id: {account_id}")
    if action not in ACTION_CHOICES:
        raise HTTPException(400, f"action must be one of {ACTION_CHOICES}")
    if time_range not in TIME_RANGE_CHOICES:
        raise HTTPException(400, f"time_range must be one of {TIME_RANGE_CHOICES}")

    try:
        return get_account_action_breakdown(account_id, action, time_range)
    except CostAnalysisError as e:
        _logger.error("get_waf_monitoring_action_breakdown: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch action breakdown")
    except Exception:
        _logger.exception("get_waf_monitoring_action_breakdown unexpected error")
        raise HTTPException(502, "Could not fetch action breakdown")


@app.get("/api/waf-monitoring/sampled-requests")
def get_waf_monitoring_sampled_requests(
    account_id: str,
    web_acl_name: str,
    web_acl_id: str,
    scope: str,
    rule_name: str,
    actor: str = Depends(get_actor),
):
    """
    Live sample of recently-matched requests, ACROSS EVERY ACTION
    (BLOCK/ALLOW/COUNT/CAPTCHA/CHALLENGE — not just blocked) for one
    specific rule inside one Web ACL. Called automatically as soon as a
    user expands a rule's metrics chart on the WAF Rule Monitoring page.
    Read-only.

    AWS LIMITATION: GetSampledRequests only ever covers roughly the last
    3 hours and returns at most ~500 samples per call — this is an AWS
    API constraint, not something this endpoint's parameters can extend.
    It always covers the fixed last-3h window regardless of whatever
    time_range the metrics chart itself is showing.
    """
    from .waf_monitoring import get_sampled_blocked_requests
    from .cost_explorer import CostAnalysisError
    from .billing_by_service import ACCOUNT_ALIAS

    if account_id not in ACCOUNT_ALIAS:
        raise HTTPException(400, f"Unknown AWS account_id: {account_id}")
    if scope not in ("REGIONAL", "CLOUDFRONT"):
        raise HTTPException(400, "scope must be REGIONAL or CLOUDFRONT")

    try:
        return get_sampled_blocked_requests(account_id, web_acl_name, web_acl_id, scope, rule_name)
    except CostAnalysisError as e:
        _logger.error("get_waf_monitoring_sampled_requests: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch sampled requests for this rule")
    except Exception:
        _logger.exception("get_waf_monitoring_sampled_requests unexpected error")
        raise HTTPException(502, "Could not fetch sampled requests for this rule")


# ── Local Infra Hardware Monitor (Catalogue) — Cisco Meraki Access Point
# monitoring dashboard. See app/meraki_monitor.py module docstring for
# the full data-source rationale (single org, ~10 calls/sec rate limit,
# fan-out cost control over 91 networks). Entirely read-only — no
# endpoint here ever modifies anything in Meraki — so, mirroring the WAF
# Rule Monitoring precedent exactly, every route below uses plain
# get_actor (any logged-in user); access is controlled purely via the
# 'meraki_monitor' tab flag (TAB_REGISTRY/CATEGORY_REGISTRY in
# settings_store.py), not a dedicated require_* dependency.

@app.get("/api/meraki-monitor/access-points")
def get_meraki_access_points(refresh: bool = False, actor: str = Depends(get_actor)):
    """Full AP inventory (Site / AP Name / Tag / Status) — also serves
    the 'WiFi Status' topic directly, since AP status IS WiFi status at
    the device level."""
    from .meraki_monitor import list_access_points, MerakiError

    try:
        return list_access_points(force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_access_points: %s", e)
        raise HTTPException(502, "Could not fetch Meraki access point inventory")
    except Exception:
        _logger.exception("get_meraki_access_points unexpected error")
        raise HTTPException(502, "Could not fetch Meraki access point inventory")


@app.get("/api/meraki-monitor/overview")
def get_meraki_overview(refresh: bool = False, actor: str = Depends(get_actor)):
    """Overview topic — org-wide Devices Up/Down (+ alerting/dormant) counts."""
    from .meraki_monitor import get_devices_overview, MerakiError

    try:
        return get_devices_overview(force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_overview: %s", e)
        raise HTTPException(502, "Could not fetch Meraki devices overview")
    except Exception:
        _logger.exception("get_meraki_overview unexpected error")
        raise HTTPException(502, "Could not fetch Meraki devices overview")


@app.get("/api/meraki-monitor/availability-history")
def get_meraki_availability_history(timespan: int = 30 * 24 * 3600, refresh: bool = False, actor: str = Depends(get_actor)):
    """AP Network Monitoring tab — every AP up/down status-change event
    in the org over the requested lookback window (Meraki's native
    'availabilities change history' log, not a derived metric). Frontend
    applies Site/Date/Status filters and converts timestamps to HKT
    (UTC+8) client-side."""
    from .meraki_monitor import get_ap_availability_history, MerakiError

    try:
        return get_ap_availability_history(timespan=timespan, force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_availability_history: %s", e)
        raise HTTPException(502, "Could not fetch Meraki AP availability history")
    except Exception:
        _logger.exception("get_meraki_availability_history unexpected error")
        raise HTTPException(502, "Could not fetch Meraki AP availability history")


def _meraki_availability_rows_for_xlsx(events: list[dict]) -> tuple[list[str], list[list]]:
    """Shared row-building for both the CSV and XLSX export branches
    below — every timestamp is converted to HKT (UTC+8) text, matching
    what the AP Network Monitoring tab already shows on-screen, so the
    downloaded file reads identically to the page."""
    from datetime import timedelta

    columns = ["Date / Time (HKT)", "Site", "AP Name", "Serial", "Model", "Status", "From", "To"]
    rows = []
    for e in events:
        occurred_at = e.get("occurred_at")
        hkt_str = "—"
        if occurred_at:
            try:
                dt = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                hkt = dt.astimezone(timezone.utc) + timedelta(hours=8)
                hkt_str = hkt.strftime("%Y-%m-%d %H:%M:%S HKT")
            except (ValueError, AttributeError):
                hkt_str = occurred_at
        direction = e.get("direction")
        rows.append([
            hkt_str, e.get("site") or "—", e.get("name") or "—", e.get("serial") or "—",
            e.get("model") or "—", "Up" if direction == "up" else "Down",
            e.get("old_status") or "—", e.get("new_status") or "—",
        ])
    return columns, rows


@app.get("/api/meraki-monitor/availability-history/export")
def export_meraki_availability_history(
    start: str, end: str, format: str = "xlsx", site: Optional[str] = None,
    status: Optional[str] = None, refresh: bool = False, actor: str = Depends(get_actor),
):
    """AP Network Monitoring tab's period-select download — `start`/`end`
    are ISO8601 timestamps (max 31 days apart, Meraki's hard cap for this
    report), `format` is 'xlsx' or 'csv'. Optional `site`/`status` narrow
    the export to match whatever filters the user currently has applied
    on-screen, so the downloaded file matches what they're looking at."""
    from .meraki_monitor import get_ap_availability_history_range, MerakiError

    try:
        data = get_ap_availability_history_range(start, end, force_refresh=refresh)
    except MerakiError as e:
        _logger.error("export_meraki_availability_history: %s", e)
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("export_meraki_availability_history unexpected error")
        raise HTTPException(502, "Could not fetch Meraki AP availability history")

    events = data.get("events") or []
    if site:
        events = [e for e in events if e.get("site") == site]
    if status in ("up", "down"):
        events = [e for e in events if e.get("direction") == status]

    columns, rows = _meraki_availability_rows_for_xlsx(events)
    stamp_start = start[:10].replace("-", "")
    stamp_end = end[:10].replace("-", "")

    if format == "csv":
        return _csv_response(columns, rows, f"ap-network-monitoring-{stamp_start}-{stamp_end}.csv")

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "AP Up-Down Log"
    ws.append(columns)
    for row in rows:
        ws.append(row)
    for col_idx in range(1, len(columns) + 1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = 20
    buf = io.BytesIO()
    wb.save(buf)
    xlsx_bytes = buf.getvalue()

    return StreamingResponse(
        iter([xlsx_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="ap-network-monitoring-{stamp_start}-{stamp_end}.xlsx"'},
    )


@app.get("/api/meraki-monitor/channel-utilization/by-network")
def get_meraki_channel_utilization_by_network(timespan: int = 3600, refresh: bool = False, actor: str = Depends(get_actor)):
    """Current-snapshot channel utilization (Wi-Fi / non-Wi-Fi / total %)
    per network — powers 'Avg AP Channel Utilization by Network' and
    'Top AP Channel Utilization by Network (802.11)'."""
    from .meraki_monitor import get_channel_utilization_by_network, MerakiError

    try:
        return get_channel_utilization_by_network(timespan=timespan, force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_channel_utilization_by_network: %s", e)
        raise HTTPException(502, "Could not fetch channel utilization by network")
    except Exception:
        _logger.exception("get_meraki_channel_utilization_by_network unexpected error")
        raise HTTPException(502, "Could not fetch channel utilization by network")


@app.get("/api/meraki-monitor/channel-utilization/by-device")
def get_meraki_channel_utilization_by_device(timespan: int = 3600, refresh: bool = False, actor: str = Depends(get_actor)):
    """Current-snapshot channel utilization per AP device — powers 'AP
    WiFi Channel Utilization (Top 10 APs)', 'Avg AP Channel Utilization
    by Device', 'Top AP Channel Utilization by Device (Non-802.11)',
    'Top 10 APs — Wi-Fi Channel Util %', 'Top 10 APs — Non-Wi-Fi Channel
    Util %' (all derived by sorting/slicing this same list client-side)."""
    from .meraki_monitor import get_channel_utilization_by_device, MerakiError

    try:
        return get_channel_utilization_by_device(timespan=timespan, force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_channel_utilization_by_device: %s", e)
        raise HTTPException(502, "Could not fetch channel utilization by device")
    except Exception:
        _logger.exception("get_meraki_channel_utilization_by_device unexpected error")
        raise HTTPException(502, "Could not fetch channel utilization by device")


@app.get("/api/meraki-monitor/channel-utilization/change/by-network")
def get_meraki_channel_utilization_change_by_network(
    timespan: int = 7200, interval: int = 3600, refresh: bool = False, actor: str = Depends(get_actor),
):
    """'Top Change in AP Channel Utilization by Network' — a two-point
    delta (Meraki has no native 'change' metric; computed here by diffing
    the first and last interval bucket over `timespan`)."""
    from .meraki_monitor import get_channel_utilization_change_by_network, MerakiError

    try:
        return get_channel_utilization_change_by_network(timespan=timespan, interval=interval, force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_channel_utilization_change_by_network: %s", e)
        raise HTTPException(502, "Could not fetch channel utilization change by network")
    except Exception:
        _logger.exception("get_meraki_channel_utilization_change_by_network unexpected error")
        raise HTTPException(502, "Could not fetch channel utilization change by network")


@app.get("/api/meraki-monitor/channel-utilization/change/by-device")
def get_meraki_channel_utilization_change_by_device(
    timespan: int = 7200, interval: int = 3600, refresh: bool = False, actor: str = Depends(get_actor),
):
    """'Top Change in AP Channel Utilization by Device' — same two-point
    delta approach as the by-network version, scoped to individual APs."""
    from .meraki_monitor import get_channel_utilization_change_by_device, MerakiError

    try:
        return get_channel_utilization_change_by_device(timespan=timespan, interval=interval, force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_channel_utilization_change_by_device: %s", e)
        raise HTTPException(502, "Could not fetch channel utilization change by device")
    except Exception:
        _logger.exception("get_meraki_channel_utilization_change_by_device unexpected error")
        raise HTTPException(502, "Could not fetch channel utilization change by device")


@app.get("/api/meraki-monitor/packet-loss/by-network")
def get_meraki_packet_loss_by_network(timespan: int = 3600, refresh: bool = False, actor: str = Depends(get_actor)):
    """'Top 10 Networks — Wireless Loss %'."""
    from .meraki_monitor import get_packet_loss_by_network, MerakiError

    try:
        return get_packet_loss_by_network(timespan=timespan, force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_packet_loss_by_network: %s", e)
        raise HTTPException(502, "Could not fetch packet loss by network")
    except Exception:
        _logger.exception("get_meraki_packet_loss_by_network unexpected error")
        raise HTTPException(502, "Could not fetch packet loss by network")


@app.get("/api/meraki-monitor/top-networks-by-clients")
def get_meraki_top_networks_by_clients(refresh: bool = False, actor: str = Depends(get_actor)):
    """'Wireless Client Count by Network' — every network in the org,
    sorted by current client count (frontend slices the top N as needed)."""
    from .meraki_monitor import get_top_networks_by_clients, MerakiError

    try:
        return get_top_networks_by_clients(force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_top_networks_by_clients: %s", e)
        raise HTTPException(502, "Could not fetch top networks by client count")
    except Exception:
        _logger.exception("get_meraki_top_networks_by_clients unexpected error")
        raise HTTPException(502, "Could not fetch top networks by client count")


@app.get("/api/meraki-monitor/top-devices-by-clients")
def get_meraki_top_devices_by_clients(refresh: bool = False, actor: str = Depends(get_actor)):
    """'Top 10 APs — Client Count'."""
    from .meraki_monitor import get_top_devices_by_clients, MerakiError

    try:
        return get_top_devices_by_clients(force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_top_devices_by_clients: %s", e)
        raise HTTPException(502, "Could not fetch top devices by client count")
    except Exception:
        _logger.exception("get_meraki_top_devices_by_clients unexpected error")
        raise HTTPException(502, "Could not fetch top devices by client count")


@app.get("/api/meraki-monitor/latency/top-networks")
def get_meraki_top_latency_networks(timespan: int = 3600, refresh: bool = False, actor: str = Depends(get_actor)):
    """'Wireless Average Latency per Network' and 'Top 10 Networks — Avg
    Latency (ms)'. See meraki_monitor.py's 'Fan-out cost control' —
    limited to the busiest networks by client count, not all networks in
    the org, to stay within Meraki's per-organization rate limit."""
    from .meraki_monitor import get_top_latency_networks, MerakiError

    try:
        return get_top_latency_networks(timespan=timespan, force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_top_latency_networks: %s", e)
        raise HTTPException(502, "Could not fetch latency by network")
    except Exception:
        _logger.exception("get_meraki_top_latency_networks unexpected error")
        raise HTTPException(502, "Could not fetch latency by network")


@app.get("/api/meraki-monitor/events")
def get_meraki_events_endpoint(
    network_id: Optional[str] = None, per_page: int = 100, refresh: bool = False, actor: str = Depends(get_actor),
):
    """Meraki event log — serves BOTH the 'Meraki Error Logs' topic
    (Date/Time, AP Name, Category, SSID, Device Name, Error Description)
    and the 'Logs' topic's 'source:meraki' filter (every row here already
    originates from Meraki, so this table trivially satisfies that
    filter). Pass network_id to scope to one network; omit to fan out
    across the busiest networks (see meraki_monitor.py docstring)."""
    from .meraki_monitor import get_meraki_events, MerakiError

    try:
        return get_meraki_events(network_id=network_id, per_page=per_page, force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_events_endpoint: %s", e)
        raise HTTPException(502, "Could not fetch Meraki event log")
    except Exception:
        _logger.exception("get_meraki_events_endpoint unexpected error")
        raise HTTPException(502, "Could not fetch Meraki event log")


@app.get("/api/meraki-monitor/networks")
def get_meraki_networks(refresh: bool = False, actor: str = Depends(get_actor)):
    """Network (site) id->name list — used as the Logs tab's network
    filter dropdown."""
    from .meraki_monitor import list_networks, MerakiError

    try:
        return list_networks(force_refresh=refresh)
    except MerakiError as e:
        _logger.error("get_meraki_networks: %s", e)
        raise HTTPException(502, "Could not fetch Meraki network list")
    except Exception:
        _logger.exception("get_meraki_networks unexpected error")
        raise HTTPException(502, "Could not fetch Meraki network list")


# ── SonicWall sub-tab (still under the 'meraki_monitor' tab flag — this
# is a second data source inside the same "Local Infra Hardware Monitor"
# page, not a separate permission-gated page). See
# app/sonicwall_monitor.py's module docstring for the scope note: the
# public MySonicWall MSSP API only exposes tenant/cloud-service
# provisioning ratios, not real-time device up/down telemetry, so this
# is presented as a "simulated" health view per the user's own request.

@app.get("/api/sonicwall-monitor/overview")
def get_sonicwall_overview(refresh: bool = False, actor: str = Depends(get_actor)):
    """Org-wide summary — tenant/product counts and overall provisioning %."""
    from .sonicwall_monitor import get_overview, SonicWallError

    try:
        return get_overview(force_refresh=refresh)
    except SonicWallError as e:
        _logger.error("get_sonicwall_overview: %s", e)
        raise HTTPException(502, "Could not fetch SonicWall overview")
    except Exception:
        _logger.exception("get_sonicwall_overview unexpected error")
        raise HTTPException(502, "Could not fetch SonicWall overview")


@app.get("/api/sonicwall-monitor/tenants")
def get_sonicwall_tenants(refresh: bool = False, actor: str = Depends(get_actor)):
    """Full MSSP tenant list with per-service provisioning ratios —
    powers the SonicWall sub-tab's tenant table."""
    from .sonicwall_monitor import list_tenants, SonicWallError

    try:
        return list_tenants(force_refresh=refresh)
    except SonicWallError as e:
        _logger.error("get_sonicwall_tenants: %s", e)
        raise HTTPException(502, "Could not fetch SonicWall tenants")
    except Exception:
        _logger.exception("get_sonicwall_tenants unexpected error")
        raise HTTPException(502, "Could not fetch SonicWall tenants")


# ── Fortigate Firewall sub-tab (still under the 'meraki_monitor' tab
# flag — a third data source inside the same "Local Infra Hardware
# Monitor" page, alongside Cisco Meraki and MySonicWall above). See
# app/fortigate_monitor.py's module docstring for the FortiManager Cloud
# 3-step session auth flow. Unlike SonicWall's provisioning-ratio proxy,
# this IS genuine real-time device connectivity status, so it is NOT
# labelled "simulated".

def _fortigate_error_detail(e: Exception, fallback: str) -> str:
    """Surfaces the specific "too many login sessions" message as-is
    (it's actionable for whoever sees it — tells them to ask a
    FortiManager admin to disconnect stale sessions), falling back to a
    generic message for every other FortigateError so internal details
    (URLs, raw API error codes) aren't leaked to the frontend."""
    msg = str(e)
    if "too many active sessions" in msg or "Too many login sessions" in msg:
        return msg
    return fallback


@app.get("/api/fortigate-monitor/overview")
def get_fortigate_overview(refresh: bool = False, actor: str = Depends(get_actor)):
    """Org-wide summary — device counts and overall up %."""
    from .fortigate_monitor import get_overview, FortigateError

    try:
        return get_overview(force_refresh=refresh)
    except FortigateError as e:
        _logger.error("get_fortigate_overview: %s", e)
        raise HTTPException(502, _fortigate_error_detail(e, "Could not fetch Fortigate overview"))
    except Exception:
        _logger.exception("get_fortigate_overview unexpected error")
        raise HTTPException(502, "Could not fetch Fortigate overview")


@app.get("/api/fortigate-monitor/devices")
def get_fortigate_devices(refresh: bool = False, actor: str = Depends(get_actor)):
    """Full managed-device list with real-time connection/config status —
    powers the Fortigate Firewall sub-tab's device table."""
    from .fortigate_monitor import list_devices, FortigateError

    try:
        return list_devices(force_refresh=refresh)
    except FortigateError as e:
        _logger.error("get_fortigate_devices: %s", e)
        raise HTTPException(502, _fortigate_error_detail(e, "Could not fetch Fortigate devices"))
    except Exception:
        _logger.exception("get_fortigate_devices unexpected error")
        raise HTTPException(502, "Could not fetch Fortigate devices")


@app.get("/api/fortigate-monitor/devices/{device_name}/detail")
def get_fortigate_device_detail(device_name: str, refresh: bool = False, actor: str = Depends(get_actor)):
    """Live CPU/Memory usage, interface bandwidth, and VPN tunnel status
    for ONE FortiGate — fetched on demand only when a user expands that
    device's row in the frontend, NOT for every device on page load
    (see fortigate_monitor.py's "Per-device live detail" section
    docstring for why: fetching this for all ~27 devices per page load
    would mean ~81 proxy calls every time, which is slow and risks
    FortiManager Cloud's rate limiting)."""
    from .fortigate_monitor import get_device_detail, FortigateError

    try:
        return get_device_detail(device_name, force_refresh=refresh)
    except FortigateError as e:
        _logger.error("get_fortigate_device_detail: %s", e)
        raise HTTPException(502, _fortigate_error_detail(e, "Could not fetch Fortigate device detail"))
    except Exception:
        _logger.exception("get_fortigate_device_detail unexpected error")
        raise HTTPException(502, "Could not fetch Fortigate device detail")


# ── FortiSwitch sub-tab — a separate sub-tab from Fortigate Firewall
# (per the user's explicit choice), still under the 'meraki_monitor'
# tab flag. FortiSwitches are managed via FortiLink through a parent
# FortiGate rather than directly by FortiManager, so this proxies
# through to each FortiGate reporting fsw_cnt > 0 — see
# fortigate_monitor.py's "FortiSwitches" section docstring.

@app.get("/api/fortigate-monitor/switches/overview")
def get_fortigate_switch_overview(refresh: bool = False, actor: str = Depends(get_actor)):
    """Org-wide summary — switch counts and overall connected %."""
    from .fortigate_monitor import get_switch_overview, FortigateError

    try:
        return get_switch_overview(force_refresh=refresh)
    except FortigateError as e:
        _logger.error("get_fortigate_switch_overview: %s", e)
        raise HTTPException(502, _fortigate_error_detail(e, "Could not fetch FortiSwitch overview"))
    except Exception:
        _logger.exception("get_fortigate_switch_overview unexpected error")
        raise HTTPException(502, "Could not fetch FortiSwitch overview")


@app.get("/api/fortigate-monitor/switches")
def get_fortigate_switches(refresh: bool = False, actor: str = Depends(get_actor)):
    """Every FortiSwitch reachable via FortiLink through any managed
    FortiGate — powers the FortiSwitch sub-tab's switch table."""
    from .fortigate_monitor import list_switches, FortigateError

    try:
        return list_switches(force_refresh=refresh)
    except FortigateError as e:
        _logger.error("get_fortigate_switches: %s", e)
        raise HTTPException(502, _fortigate_error_detail(e, "Could not fetch FortiSwitches"))
    except Exception:
        _logger.exception("get_fortigate_switches unexpected error")
        raise HTTPException(502, "Could not fetch FortiSwitches")


# ── Website Monitoring (sidebar: Monitoring > Website Monitoring) ────────
# UP/DOWN, status code, response time and last successful screenshot of
# each monitored website from several AWS Regions. See
# app/website_monitor.py's module docstring: the probing is done by
# CloudWatch Synthetics canaries that write into a private S3 bucket;
# these routes only read that bucket and edit its site list.
#   READ  (status)        -> any logged-in user (get_actor). The
#                            'website_monitoring' tab is also granted to
#                            everyone by settings_store.EVERYONE_TAB_IDS.
#   WRITE (add / remove)  -> admins only (require_admin).

class WebsiteMonitorSiteBody(BaseModel):
    name: str
    url: str


def _website_monitor_http_error(e: Exception) -> HTTPException:
    """Map website_monitor's exceptions to HTTP errors (never leaks AWS detail)."""
    from . import website_monitor as wm

    if isinstance(e, wm.WebsiteMonitorNotConfigured):
        return HTTPException(503, "Website Monitoring is not configured for this environment")
    if isinstance(e, wm.SiteValidationError):
        return HTTPException(400, str(e))
    if isinstance(e, wm.SiteConflictError):
        return HTTPException(409, str(e))
    if isinstance(e, wm.SiteNotFoundError):
        return HTTPException(404, "Website not found")
    if isinstance(e, wm.WebsiteMonitorStorageError):
        _logger.error("website_monitor storage error: %s", e)
        return HTTPException(502, "Could not reach the website monitoring data store")
    _logger.exception("website_monitor unexpected error")
    return HTTPException(502, "Website monitoring request failed")


@app.get("/api/website-monitoring/status")
def get_website_monitoring_status(refresh: bool = False, actor: str = Depends(get_actor)):
    """Grid of every monitored site x probe Region. `can_manage` tells the
    UI whether to offer add/remove; the write routes below enforce it
    server-side regardless."""
    from . import website_monitor as wm

    try:
        data = wm.get_status(force_refresh=refresh)
    except Exception as e:  # noqa: BLE001 - mapped by _website_monitor_http_error
        raise _website_monitor_http_error(e)
    return {**data, "can_manage": user_store.is_admin(actor)}


@app.post("/api/website-monitoring/sites", status_code=201)
def add_website_monitoring_site(body: WebsiteMonitorSiteBody, actor: str = Depends(require_admin)):
    """Admin only: start monitoring a website."""
    from . import website_monitor as wm

    try:
        return wm.add_site(body.name, body.url, actor)
    except Exception as e:  # noqa: BLE001
        raise _website_monitor_http_error(e)


@app.delete("/api/website-monitoring/sites/{site_id}")
def remove_website_monitoring_site(site_id: str, actor: str = Depends(require_admin)):
    """Admin only: stop monitoring a website and discard its stored results."""
    from . import website_monitor as wm

    try:
        removed = wm.remove_site(site_id, actor)
    except Exception as e:  # noqa: BLE001
        raise _website_monitor_http_error(e)
    return {"removed": removed["id"]}


# ── Lifecycle > Patching > OS — patch scope & schedule planner ───────────
# See app/patching.py. READ: any logged-in user (the 'lifecycle_patching'
# tab is granted to everyone via settings_store.EVERYONE_TAB_IDS).
# WRITE (create / edit / delete a patching plan): admins, the Cloud Infra /
# Security / Management Teams, or users individually granted write on this
# tab by a portal authority - same shape as the DNS writer gate.

_PATCHING_PRIVILEGED_GROUPS = {"cloud_infra_team", "security_team", "management_team"}


def require_patching_writer(actor: str = Depends(get_actor)) -> str:
    """FastAPI dependency: 403 unless the caller may change OS patching plans."""
    if user_store.is_admin(actor):
        return actor
    if set(user_store.get_user_groups(actor)) & _PATCHING_PRIVILEGED_GROUPS:
        return actor
    if "lifecycle_patching" in user_store.get_user_tab_write(actor):
        return actor
    raise HTTPException(
        403,
        "Only admins and the Cloud Infra, Security and Management Teams (or users individually "
        "granted write access) can change patching schedules.",
    )


class PatchingInstanceRef(BaseModel):
    account_id: str
    instance_id: str


class PatchingPlanBody(BaseModel):
    title: str = ""
    schedule_date: str          # YYYY-MM-DD, UTC+8
    schedule_time: str          # HH:MM, UTC+8
    teams_meeting: Optional[str] = None   # id of a team-maintained Teams link
    group_id: Optional[str] = None        # id of a user-created group (None = ungrouped)
    freshservice_change: str = ""
    ams_sr: str = ""
    notes: str = ""
    status: str = "planned"
    instances: List[PatchingInstanceRef]


class PatchingGroupBody(BaseModel):
    name: str


class PatchingMeetingBody(BaseModel):
    name: str
    url: str


def _patching_http_error(e: Exception) -> HTTPException:
    from . import patching as p

    if isinstance(e, p.PatchingValidationError):
        return HTTPException(400, str(e))
    if isinstance(e, p.PatchingConflictError):
        return HTTPException(409, str(e))
    if isinstance(e, p.PatchingNotFoundError):
        return HTTPException(404, "Not found")
    if isinstance(e, p.PatchingInventoryError):
        return HTTPException(502, str(e))
    _logger.exception("patching unexpected error")
    return HTTPException(502, "Patching request failed")


@app.get("/api/patching/os/plans")
def list_patching_os_plans(actor: str = Depends(get_actor)):
    """Every OS patching plan (sorted by schedule), the user-created groups and the
    team-maintained Teams links, plus what the caller may do."""
    from . import patching as p

    try:
        overview = p.get_overview()
    except Exception as e:  # noqa: BLE001
        raise _patching_http_error(e)
    can_edit = True
    try:
        require_patching_writer(actor)
    except HTTPException:
        can_edit = False
    return {**overview, "can_edit": can_edit, "timezone": "UTC+8"}


@app.get("/api/patching/os/instances")
def list_patching_os_instances(refresh: bool = False, actor: str = Depends(get_actor)):
    """EC2 instances (from the AWS Resources inventory) to pick the scope from."""
    from . import patching as p

    try:
        return p.list_instances(force_refresh=refresh)
    except Exception as e:  # noqa: BLE001
        raise _patching_http_error(e)


@app.post("/api/patching/os/plans", status_code=201)
def create_patching_os_plan(body: PatchingPlanBody, actor: str = Depends(require_patching_writer)):
    from . import patching as p

    try:
        return p.create_plan(body.model_dump(), actor)
    except Exception as e:  # noqa: BLE001
        raise _patching_http_error(e)


@app.put("/api/patching/os/plans/{plan_id}")
def update_patching_os_plan(plan_id: str, body: PatchingPlanBody, actor: str = Depends(require_patching_writer)):
    from . import patching as p

    try:
        return p.update_plan(plan_id, body.model_dump(), actor)
    except Exception as e:  # noqa: BLE001
        raise _patching_http_error(e)


@app.delete("/api/patching/os/plans/{plan_id}")
def delete_patching_os_plan(plan_id: str, actor: str = Depends(require_patching_writer)):
    from . import patching as p

    try:
        removed = p.delete_plan(plan_id, actor)
    except Exception as e:  # noqa: BLE001
        raise _patching_http_error(e)
    return {"removed": removed["id"]}


# Groups: user-created "group / task name" that schedules are filed under.

@app.post("/api/patching/os/groups", status_code=201)
def create_patching_os_group(body: PatchingGroupBody, actor: str = Depends(require_patching_writer)):
    from . import patching as p

    try:
        return p.create_group(body.name, actor)
    except Exception as e:  # noqa: BLE001
        raise _patching_http_error(e)


@app.put("/api/patching/os/groups/{group_id}")
def rename_patching_os_group(group_id: str, body: PatchingGroupBody, actor: str = Depends(require_patching_writer)):
    from . import patching as p

    try:
        return p.rename_group(group_id, body.name, actor)
    except Exception as e:  # noqa: BLE001
        raise _patching_http_error(e)


@app.delete("/api/patching/os/groups/{group_id}")
def delete_patching_os_group(group_id: str, actor: str = Depends(require_patching_writer)):
    """Deletes the group only - its schedules are kept and become ungrouped."""
    from . import patching as p

    try:
        result = p.delete_group(group_id, actor)
    except Exception as e:  # noqa: BLE001
        raise _patching_http_error(e)
    return {"removed": result["group"]["id"], "ungrouped": result["ungrouped"]}


# MS Teams links: maintained by the teams (add / rename / change URL / delete).

@app.post("/api/patching/os/meetings", status_code=201)
def create_patching_os_meeting(body: PatchingMeetingBody, actor: str = Depends(require_patching_writer)):
    from . import patching as p

    try:
        return p.create_meeting(body.name, body.url, actor)
    except Exception as e:  # noqa: BLE001
        raise _patching_http_error(e)


@app.put("/api/patching/os/meetings/{meeting_id}")
def update_patching_os_meeting(meeting_id: str, body: PatchingMeetingBody, actor: str = Depends(require_patching_writer)):
    from . import patching as p

    try:
        return p.update_meeting(meeting_id, body.name, body.url, actor)
    except Exception as e:  # noqa: BLE001
        raise _patching_http_error(e)


@app.delete("/api/patching/os/meetings/{meeting_id}")
def delete_patching_os_meeting(meeting_id: str, actor: str = Depends(require_patching_writer)):
    """409 while any schedule still uses the link."""
    from . import patching as p

    try:
        removed = p.delete_meeting(meeting_id, actor)
    except Exception as e:  # noqa: BLE001
        raise _patching_http_error(e)
    return {"removed": removed["id"]}


# ── Azure Resources tab — a new, independent sidebar category (per
# user's explicit choice, NOT folded into "AWS Resources" above) that
# inventories a single Azure subscription via the Azure Resource
# Manager REST API. See app/azure_monitor.py's module docstring for the
# OAuth2 client-credentials auth flow and credential storage. v1 scope
# is deliberately READ-ONLY (Reader role only, per user's explicit
# "Reader only first" answer) — no write/control endpoints exist here.
#
# WAF and Cost were deliberately DROPPED from the v1 launch per user
# request, after live-testing during setup: WAF returned 0 policies
# (this org has none deployed at all — Application Gateway WAF nor
# Front Door WAF), and Cost's Microsoft.CostManagement/query call
# consistently returned a 400 ("Access to cost data has been disabled
# for account admins... 'AO View Charges' must be enabled") even after
# granting the Service Principal Cost Management Reader AND Billing
# Reader roles — this org's billing account is a Microsoft Customer
# Agreement (confirmed via Cost Management + Billing > Settings >
# Properties > Type), and the fix (Settings > Properties > Policies >
# Manage policies) was not pursued further per user's explicit "skip
# cost and WAF part, don't make it in the portal" instruction. Both
# list_waf_policies()/get_cost_summary() functions remain in
# azure_monitor.py (unused, but working code and useful analysis
# artifact) — only the ROUTES and frontend tabs were removed, not the
# module-level functions themselves.
#
# `resource_type` selects which azure_monitor.py list_*() function to
# call — mirrors the AWS Resources tab's own `?resource_type=...`-style
# single-endpoint-per-tab-type shape (see list_aws_resources_data below)
# rather than N separate route paths, so the frontend's tab-switching
# logic can stay identical to AwsResourcesDashboardPage.jsx's own
# pattern.
_AZURE_RESOURCE_TYPE_FUNCS = {
    "vm": ("list_vms", "🖥️", "VM"),
    "avd": ("list_avd_host_pools", "🖧", "AVD"),
    "resource_group": ("list_resource_groups", "📁", "Resource Group"),
    "nic": ("list_nics", "🔌", "NIC"),
    "nsg": ("list_nsgs", "🛡️", "NSG"),
}


@app.get("/api/azure-resources/resource-types")
def list_azure_resource_types(actor: str = Depends(get_actor)):
    """Static list of resource-type tabs (id/icon/label) for the Azure
    Resources page's tab bar — mirrors AwsResourcesDashboardPage's
    summary.resource_types shape exactly so the frontend can reuse the
    same tab-bar rendering code. Cost is intentionally NOT included here
    — it's rendered as its own dedicated sub-tab with its own layout
    (KPI + breakdown table), not the generic headers/rows table the
    other 6 types share."""
    return {
        "resource_types": [
            {"id": rt_id, "icon": icon, "label": label}
            for rt_id, (_, icon, label) in _AZURE_RESOURCE_TYPE_FUNCS.items()
        ]
    }


@app.get("/api/azure-resources/data")
def get_azure_resources_data(resource_type: str, refresh: bool = False, actor: str = Depends(get_actor)):
    """{headers, rows} for one Azure resource type — powers the Azure
    Resources page's data table for whichever tab is active (VM / AVD /
    Resource Group / NIC / NSG / WAF)."""
    from . import azure_monitor
    from .azure_monitor import AzureError

    entry = _AZURE_RESOURCE_TYPE_FUNCS.get(resource_type)
    if not entry:
        raise HTTPException(400, f"Unknown Azure resource_type: {resource_type}")
    func_name = entry[0]

    try:
        # Covers every subscription registered on the Cloud Accounts page; with just the
        # configured default subscription this is exactly the original single list call.
        return azure_monitor.list_connected(func_name, force_refresh=refresh)
    except AzureError as e:
        _logger.error("get_azure_resources_data(%s): %s", resource_type, e)
        raise HTTPException(502, f"Could not fetch Azure {resource_type} data: {e}")
    except Exception:
        _logger.exception("get_azure_resources_data(%s) unexpected error", resource_type)
        raise HTTPException(502, f"Could not fetch Azure {resource_type} data")


# ── AWS Resources tab — inventory sourced from the resource-inventory-
# scanner S3 bucket (account 100000000001 / Core account), replacing
# the previous static "external IP/URL dashboard" landing page. Read-only
# for everyone; this data source lives directly in Core account's own
# account, so no cross-account AssumeRole is involved at all.

@app.get("/api/aws-resources/accounts")
def list_aws_resources_accounts(actor: str = Depends(get_actor)):
    """Account picker data (id + alias) for the AWS Resources page — the
    actual account-id folders discovered live in the S3 bucket."""
    from .aws_resources_inventory import list_accounts, AwsResourcesInventoryError

    try:
        return {"accounts": list_accounts()}
    except AwsResourcesInventoryError as e:
        _logger.error("list_aws_resources_accounts: %s", e)
        raise HTTPException(502, "Could not list AWS Resources accounts")
    except Exception:
        _logger.exception("list_aws_resources_accounts unexpected error")
        raise HTTPException(502, "Could not list AWS Resources accounts")


@app.get("/api/aws-resources/summary")
def get_aws_resources_summary(
    accounts: Optional[str] = None,
    refresh: bool = False,
    actor: str = Depends(get_actor),
):
    """
    Which accounts have any non-empty data for which resource type
    (metadata only — S3 object sizes, no downloads) — used to build the
    account/resource-type picker on the AWS Resources page. Cached for
    10 minutes; pass refresh=true to force a fresh listing.
    """
    from .aws_resources_inventory import get_inventory_summary, AwsResourcesInventoryError

    parsed_accounts = None
    if accounts:
        parsed_accounts = [a.strip() for a in accounts.split(",") if a.strip()]

    try:
        return get_inventory_summary(parsed_accounts, force_refresh=refresh)
    except AwsResourcesInventoryError as e:
        _logger.error("get_aws_resources_summary: %s", e)
        raise HTTPException(502, "Could not fetch AWS Resources inventory summary")
    except Exception:
        _logger.exception("get_aws_resources_summary unexpected error")
        raise HTTPException(502, "Could not fetch AWS Resources inventory summary")


@app.get("/api/aws-resources/data")
def get_aws_resources_data(
    resource_type: str,
    accounts: Optional[str] = None,
    refresh: bool = False,
    actor: str = Depends(get_actor),
):
    """
    Actual inventory rows for one resource type (e.g. ec2, vpc, rds),
    across the requested accounts (or every discovered account if
    omitted). Cached for 5 minutes per exact (accounts, resource_type)
    selection; pass refresh=true to force a fresh fetch from S3.
    """
    from .aws_resources_inventory import (
        get_resource_data, list_accounts, AwsResourcesInventoryError, _VALID_RESOURCE_TYPE_IDS,
    )

    if resource_type not in _VALID_RESOURCE_TYPE_IDS:
        raise HTTPException(400, f"Unknown resource_type: {resource_type!r}")

    if accounts:
        parsed_accounts = [a.strip() for a in accounts.split(",") if a.strip()]
    else:
        try:
            parsed_accounts = [a["account_id"] for a in list_accounts()]
        except AwsResourcesInventoryError as e:
            _logger.error("get_aws_resources_data: could not list accounts: %s", e)
            raise HTTPException(502, "Could not fetch AWS Resources data")

    try:
        return get_resource_data(parsed_accounts, resource_type, force_refresh=refresh)
    except AwsResourcesInventoryError as e:
        _logger.error("get_aws_resources_data: %s", e)
        raise HTTPException(502, "Could not fetch AWS Resources data")
    except Exception:
        _logger.exception("get_aws_resources_data unexpected error")
        raise HTTPException(502, "Could not fetch AWS Resources data")


# ── RI & Savings Plan — Reseller On-Demand Cost comparison (manual input) ──
# Lets an admin type in the reseller-invoiced on-demand cost for one AWS
# account for a given month (per-account, since different accounts in
# this org can be on different reseller contracts), compared against that
# same account's own AWS On-Demand Equivalent figure to show the diff and
# effective discount percentage the reseller is passing on relative to
# AWS's own on-demand list price. See app/ri_sp_savings.py's "Reseller
# On-Demand Cost comparison" docstring section for the full explanation
# and formula. Read-only GET for everyone; the PUT/DELETE that actually
# change the number are admin-only, same convention as the Cost Analysis
# manual adjustments feature.

class SetResellerCostBody(BaseModel):
    amount: float
    currency: str = "USD"


def _validate_ri_sp_account_id(account_id: str) -> None:
    from .billing_by_service import ACCOUNT_ALIAS

    if account_id not in ACCOUNT_ALIAS:
        raise HTTPException(400, f"Unknown AWS account_id: {account_id}")


@app.get("/api/ri-sp-savings/reseller-cost")
def get_ri_sp_savings_reseller_cost(account_id: str, month: str, actor: str = Depends(get_actor)):
    """The reseller-invoiced on-demand cost an admin has entered for one
    AWS account, one calendar month ('YYYY-MM'), or amount=null if never
    set. Reseller cost is entered per account since different accounts
    in this org can be on different reseller contracts."""
    from .ri_sp_savings import get_reseller_ondemand_cost

    _validate_ri_sp_account_id(account_id)
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise HTTPException(400, "month must be in YYYY-MM format")
    return get_reseller_ondemand_cost(account_id, month)


@app.put("/api/ri-sp-savings/reseller-cost")
def set_ri_sp_savings_reseller_cost(account_id: str, month: str, body: SetResellerCostBody, actor: str = Depends(require_admin)):
    """Set/overwrite the reseller-invoiced on-demand cost for one AWS
    account, one month (admin-only — this drives a comparison figure
    everyone sees on the page, same restriction as Cost Analysis's manual
    adjustments)."""
    from .ri_sp_savings import set_reseller_ondemand_cost

    _validate_ri_sp_account_id(account_id)
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise HTTPException(400, "month must be in YYYY-MM format")
    currency = (body.currency or "USD").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise HTTPException(400, "currency must be a 3-letter code, e.g. USD")
    try:
        amount = float(body.amount)
    except (TypeError, ValueError):
        raise HTTPException(400, "amount must be a number")
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise HTTPException(400, "amount must be a finite number")

    actor_name = TEAM_MEMBERS.get(actor, actor)
    try:
        return set_reseller_ondemand_cost(account_id, month, amount, currency=currency, actor_email=actor, actor_name=actor_name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/ri-sp-savings/reseller-cost")
def delete_ri_sp_savings_reseller_cost(account_id: str, month: str, actor: str = Depends(require_admin)):
    """Clear a previously entered reseller cost for one account+month
    (admin-only)."""
    from .ri_sp_savings import delete_reseller_ondemand_cost

    _validate_ri_sp_account_id(account_id)
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise HTTPException(400, "month must be in YYYY-MM format")
    actor_name = TEAM_MEMBERS.get(actor, actor)
    delete_reseller_ondemand_cost(account_id, month, actor_email=actor, actor_name=actor_name)
    return {"result": True, "account_id": account_id, "month": month}


# ── CMDB — Configuration Management Database ────────────────────────────────
# ── Whitelist (LMS) — Service Desk portal ─────────────────────────────────
# Lets a user pick a fixed WAF IPSet / Security Group target + region,
# check whether an IP is already whitelisted, and (after supplying a
# Service Request number) add it if not. Every check/add attempt is
# recorded to an audit log visible ONLY to admins + Cloud Infra/Security/
# Management Team members (see require_whitelist_audit_viewer() above and
# app/whitelist_lms.py's module docstring for the full design, including
# the NOT-YET-PROVISIONED dedicated write-capable AWS credential this
# feature requires).

class WhitelistCheckBody(BaseModel):
    target_id: str
    ip_address: str


class WhitelistAddBody(BaseModel):
    target_id: str
    ip_address: str
    sr_number: str
    port: Optional[int] = None
    protocol: Optional[str] = None


@app.get("/api/whitelist/lms/targets")
def whitelist_lms_targets_endpoint(actor: str = Depends(get_actor)):
    """Target picker data only — no `regions` list, since the region is
    now auto-detected server-side per target (see
    app/whitelist_lms.py's "Region — AUTO-DETECTED" docstring section),
    never chosen by the user."""
    from .whitelist_lms import list_targets

    return {"targets": list_targets()}


@app.post("/api/whitelist/lms/check")
def whitelist_lms_check_endpoint(body: WhitelistCheckBody, actor: str = Depends(get_actor)):
    from .whitelist_lms import check_ip, WhitelistLmsError

    try:
        result = check_ip(body.target_id, body.ip_address)
    except WhitelistLmsError as e:
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("whitelist_lms_check_endpoint unexpected error")
        raise HTTPException(502, "Could not check this IP")

    whitelist_store.record(
        action="check", target_id=result["target_id"], target_label=result["target_label"],
        region=result["region"], ip_address=result["ip_address"],
        result="already_whitelisted" if result["already_whitelisted"] else "not_whitelisted",
        actor_email=actor, actor_name=user_store.get_display_name(actor),
    )
    return result


@app.post("/api/whitelist/lms/add")
def whitelist_lms_add_endpoint(body: WhitelistAddBody, actor: str = Depends(get_actor)):
    from .whitelist_lms import add_ip, WhitelistLmsError

    try:
        result = add_ip(
            body.target_id, body.ip_address, body.sr_number,
            port=body.port, protocol=body.protocol,
        )
    except WhitelistLmsError as e:
        whitelist_store.record(
            action="add_failed", target_id=body.target_id, target_label=body.target_id,
            region="unknown", ip_address=body.ip_address, sr_number=body.sr_number,
            result="error", detail=str(e), actor_email=actor, actor_name=user_store.get_display_name(actor),
        )
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("whitelist_lms_add_endpoint unexpected error")
        whitelist_store.record(
            action="add_failed", target_id=body.target_id, target_label=body.target_id,
            region="unknown", ip_address=body.ip_address, sr_number=body.sr_number,
            result="error", detail="Unexpected error", actor_email=actor, actor_name=user_store.get_display_name(actor),
        )
        raise HTTPException(502, "Could not add this IP")

    whitelist_store.record(
        action="add", target_id=result["target_id"], target_label=result["target_label"],
        region=result["region"], ip_address=result["ip_address"], sr_number=result["sr_number"],
        result="already_whitelisted" if result["already_whitelisted"] else "added",
        actor_email=actor, actor_name=user_store.get_display_name(actor),
    )
    return result


@app.get("/api/whitelist/lms/audit-log")
def whitelist_lms_audit_log_endpoint(limit: int = 500, actor: str = Depends(require_whitelist_audit_viewer)):
    """Restricted to admins + Cloud Infra/Security/Management Team
    members — see require_whitelist_audit_viewer()'s docstring above."""
    return {"records": whitelist_store.list_records(limit=limit)}


# ── Whitelist (LMS) — Admin target registry management ───────────────────
# Lets an admin add/edit/remove the WAF IPSet / Security Group targets
# users pick from in the Whitelist (LMS) tab, without a code deploy — see
# app/whitelist_lms.py's "Admin-editable target registry" docstring
# section for the full design (overlay on top of DEFAULT_TARGETS, WAF
# IPSet auto-resolve-by-name, linked_domain is display-only, etc.).
# Admin-only (require_admin) for every write; the resolve endpoints are
# also admin-only since they exist purely to support this management UI.

class ResolveWafIpsetBody(BaseModel):
    ip_set_name: str


class ResolveSecurityGroupBody(BaseModel):
    group_id: str


class AddWafTargetBody(BaseModel):
    label: str
    ip_set_name: str
    waf_scope: str          # "regional" | "cloudfront"
    linked_domain: Optional[str] = None


class AddSgTargetBody(BaseModel):
    label: str
    group_id: str
    linked_domain: Optional[str] = None
    port: Optional[int] = None
    protocol: Optional[str] = None


class UpdateWhitelistTargetBody(BaseModel):
    label: Optional[str] = None
    linked_domain: Optional[str] = None
    port: Optional[int] = None
    protocol: Optional[str] = None


@app.get("/api/whitelist/lms/targets/admin")
def whitelist_lms_targets_admin_endpoint(actor: str = Depends(require_admin)):
    """Full target detail (including waf_scope/group_id/port/protocol,
    is_custom) for the Admin management panel — the plain
    /api/whitelist/lms/targets endpoint above only returns the subset
    the picker UI needs."""
    from .whitelist_lms import list_targets_admin

    return {"targets": list_targets_admin()}


@app.post("/api/whitelist/lms/targets/resolve-waf-ipset")
def whitelist_lms_resolve_waf_ipset_endpoint(body: ResolveWafIpsetBody, actor: str = Depends(require_admin)):
    """Look up a WAFv2 IPSet by Name across every (scope, region) this
    module can reach, WITHOUT registering it — lets the admin form show
    "found under REGIONAL in ap-southeast-1" (etc.) and, if more than one
    match comes back (see whitelist_lms.py's "IMPORTANT AMBIGUITY"
    section), let the admin pick the right one before submitting the
    actual add."""
    from .whitelist_lms import resolve_waf_ipset_name, WhitelistLmsError

    try:
        matches = resolve_waf_ipset_name(body.ip_set_name)
    except WhitelistLmsError as e:
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("whitelist_lms_resolve_waf_ipset_endpoint unexpected error")
        raise HTTPException(502, "Could not look up this WAF IPSet")
    return {"matches": matches}


@app.post("/api/whitelist/lms/targets/resolve-security-group")
def whitelist_lms_resolve_security_group_endpoint(body: ResolveSecurityGroupBody, actor: str = Depends(require_admin)):
    """Confirm a Security Group ID exists (and which region it's in)
    before it's registered as a target."""
    from .whitelist_lms import resolve_security_group_id, WhitelistLmsError

    try:
        found = resolve_security_group_id(body.group_id)
    except WhitelistLmsError as e:
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("whitelist_lms_resolve_security_group_endpoint unexpected error")
        raise HTTPException(502, "Could not look up this security group")
    return {"found": found}


@app.post("/api/whitelist/lms/targets/waf", status_code=201)
def whitelist_lms_add_waf_target_endpoint(body: AddWafTargetBody, actor: str = Depends(require_admin)):
    from .whitelist_lms import add_custom_waf_target, WhitelistLmsError

    try:
        target = add_custom_waf_target(
            label=body.label, ip_set_name=body.ip_set_name, waf_scope=body.waf_scope,
            linked_domain=body.linked_domain,
            actor_email=actor, actor_name=user_store.get_display_name(actor),
        )
    except WhitelistLmsError as e:
        raise HTTPException(400, str(e))
    return target


@app.post("/api/whitelist/lms/targets/security-group", status_code=201)
def whitelist_lms_add_sg_target_endpoint(body: AddSgTargetBody, actor: str = Depends(require_admin)):
    from .whitelist_lms import add_custom_sg_target, WhitelistLmsError

    try:
        target = add_custom_sg_target(
            label=body.label, group_id=body.group_id, linked_domain=body.linked_domain,
            port=body.port, protocol=body.protocol,
            actor_email=actor, actor_name=user_store.get_display_name(actor),
        )
    except WhitelistLmsError as e:
        raise HTTPException(400, str(e))
    return target


@app.patch("/api/whitelist/lms/targets/{target_id}")
def whitelist_lms_update_target_endpoint(target_id: str, body: UpdateWhitelistTargetBody, actor: str = Depends(require_admin)):
    """Edit label / linked_domain / (SG only) port+protocol for any
    target — the underlying AWS resource reference itself is not
    editable (remove + re-add instead) — see whitelist_lms.py's
    update_target() docstring."""
    from .whitelist_lms import update_target, WhitelistLmsError

    try:
        target = update_target(
            target_id, label=body.label, linked_domain=body.linked_domain,
            port=body.port, protocol=body.protocol,
            actor_email=actor, actor_name=user_store.get_display_name(actor),
        )
    except WhitelistLmsError as e:
        raise HTTPException(400, str(e))
    return target


@app.delete("/api/whitelist/lms/targets/{target_id}")
def whitelist_lms_remove_target_endpoint(target_id: str, actor: str = Depends(require_admin)):
    """Remove a target — soft-delete + restorable for a built-in default,
    permanent for an admin-added custom target. Never touches AWS itself
    — see whitelist_lms.py's remove_target() docstring."""
    from .whitelist_lms import remove_target, WhitelistLmsError

    try:
        remove_target(target_id, actor_email=actor, actor_name=user_store.get_display_name(actor))
    except WhitelistLmsError as e:
        raise HTTPException(400, str(e))
    return {"result": True, "target_id": target_id}


@app.post("/api/whitelist/lms/targets/{target_id}/restore")
def whitelist_lms_restore_target_endpoint(target_id: str, actor: str = Depends(require_admin)):
    """Undo a soft-delete of a built-in default target."""
    from .whitelist_lms import restore_default_target, WhitelistLmsError

    try:
        target = restore_default_target(target_id, actor_email=actor, actor_name=user_store.get_display_name(actor))
    except WhitelistLmsError as e:
        raise HTTPException(400, str(e))
    return target


# ── DNS Record (Security) ─────────────────────────────────────────────────
# Browse every Route 53 hosted zone in the Core account (100000000001)
# and its records (type / value / TTL), and add, modify or delete them.
# See app/dns_records.py's module docstring for the credential path (the
# core account's own static keys — no AssumeRole, no new IAM), the NS/SOA
# protection, and why "add" uses Route 53 CREATE while "modify" uses
# UPSERT.
#
# THIS IS THE ONLY PART OF THE PORTAL THAT MUTATES PRODUCTION DNS, so the
# access split is deliberately asymmetric and differs from every other
# tab here:
#   * READS   — plain get_actor + the ordinary 'dns_records' tab flag, so
#               anyone who can see the tab can look a record up.
#   * WRITES  — require_dns_writer (admins + Cloud Infra/Security/
#               Management Team) AND a mandatory SR number.
#   * AUDIT   — require_dns_audit_viewer, same three groups.
# Every write attempt is recorded to dns_audit_store, including failures,
# so a rejected change is as visible in the trail as a successful one.

class DnsAliasTargetBody(BaseModel):
    dns_name: str
    hosted_zone_id: str
    evaluate_target_health: bool = False


class DnsRecordWriteBody(BaseModel):
    zone_id: str
    name: str
    record_type: str
    # ttl/values are for ordinary records; alias_target is for ALIAS
    # records, which have no TTL of their own. dns_records._validate_record()
    # rejects any request that supplies both or neither.
    ttl: Optional[int] = None
    values: Optional[list[str]] = None
    alias_target: Optional[DnsAliasTargetBody] = None
    # Identifies WHICH of several weighted/latency/failover siblings
    # sharing a name+type is meant. Only needed on modify.
    set_identifier: Optional[str] = None
    sr_number: str


class DnsRecordDeleteBody(BaseModel):
    """No ttl/values: a delete always re-reads the record's live contents
    server-side (see dns_records.delete_record), so nothing the browser
    holds can cause the wrong version to be removed."""
    zone_id: str
    name: str
    record_type: str
    set_identifier: Optional[str] = None
    sr_number: str


def _dns_audit_value(record: Optional[dict]) -> Optional[str]:
    """Flatten a record's payload into one human-readable audit string.

    ALIAS and ordinary records store their value in structurally
    different places, and the audit table has a single `value` column, so
    aliases are rendered in a form that still identifies the target.
    """
    if not record:
        return None
    if record.get("is_alias"):
        target = record.get("alias_target") or {}
        return f"ALIAS -> {target.get('dns_name')} (zone {target.get('hosted_zone_id')})"
    values = record.get("values") or []
    return ", ".join(values) if values else None


def _record_dns_failure(*, action: str, body, actor: str, detail: str) -> None:
    """Audit a write that never reached Route 53 (validation rejected it,
    the record didn't exist, Route 53 refused the change batch).

    zone_name falls back to the raw zone_id because the failure may have
    happened before or during get_zone(), so the friendly domain name
    isn't necessarily known — recording the id is still enough to trace
    what was attempted.
    """
    dns_audit_store.record(
        action=action,
        zone_id=body.zone_id,
        zone_name=body.zone_id,
        record_name=body.name,
        record_type=body.record_type,
        sr_number=body.sr_number,
        result="error",
        detail=detail,
        actor_email=actor,
        actor_name=user_store.get_display_name(actor),
    )


@app.get("/api/dns/permissions")
def dns_permissions_endpoint(actor: str = Depends(get_actor)):
    """What this caller is allowed to do in the DNS Record tab.

    Exists so the page can hide the add/edit/delete controls from
    read-only users rather than letting someone fill in a whole record
    plus an SR number and only then be told 403. A UI HINT ONLY — the
    real enforcement is require_dns_writer() / require_dns_audit_viewer()
    on the routes themselves, which never consult this endpoint. Tampering
    with the response changes nothing but the button visibility.
    """
    privileged = (
        user_store.is_admin(actor)
        or bool(set(user_store.get_user_groups(actor)) & _DNS_PRIVILEGED_GROUPS)
    )
    return {"can_write": privileged, "can_view_audit": privileged}


@app.get("/api/dns/zones")
def dns_zones_endpoint(actor: str = Depends(get_actor)):
    """Every hosted zone in the Core account — the domain picker's data."""
    from .dns_records import list_zones, DnsRecordError

    try:
        return {"zones": list_zones()}
    except DnsRecordError as e:
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("dns_zones_endpoint unexpected error")
        raise HTTPException(502, "Could not load the hosted zones")


@app.get("/api/dns/zones/{zone_id}/records")
def dns_zone_records_endpoint(zone_id: str, actor: str = Depends(get_actor)):
    """Every record in one zone. Read-only for anyone who can see the tab."""
    from .dns_records import list_records, DnsRecordError

    try:
        return list_records(zone_id)
    except DnsRecordError as e:
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("dns_zone_records_endpoint unexpected error")
        raise HTTPException(502, "Could not load the records for this zone")


@app.post("/api/dns/records", status_code=201)
def dns_create_record_endpoint(body: DnsRecordWriteBody, actor: str = Depends(require_dns_writer)):
    """Add a record. Fails if one already exists with the same name+type
    rather than overwriting it (Route 53 CREATE) — see dns_records.py."""
    from .dns_records import create_record, DnsRecordError

    try:
        result = create_record(
            zone_id=body.zone_id, name=body.name, record_type=body.record_type,
            ttl=body.ttl, values=body.values,
            alias_target=body.alias_target.model_dump() if body.alias_target else None,
            sr_number=body.sr_number, actor_email=actor,
        )
    except DnsRecordError as e:
        _record_dns_failure(action="create_failed", body=body, actor=actor, detail=str(e))
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("dns_create_record_endpoint unexpected error")
        _record_dns_failure(action="create_failed", body=body, actor=actor, detail="Unexpected error")
        raise HTTPException(502, "Could not create this DNS record")

    dns_audit_store.record(
        action="create", zone_id=result["zone"]["id"], zone_name=result["zone"]["display_name"],
        record_name=result["record"]["display_name"], record_type=result["record"]["type"],
        ttl=result["record"]["ttl"], value=_dns_audit_value(result["record"]),
        sr_number=result["sr_number"], change_id=result["change"]["change_id"],
        result="created", actor_email=actor, actor_name=user_store.get_display_name(actor),
    )
    return result


@app.put("/api/dns/records")
def dns_modify_record_endpoint(body: DnsRecordWriteBody, actor: str = Depends(require_dns_writer)):
    """Replace an existing record's TTL/values (or alias target). Fails if
    the record doesn't already exist, so a mistyped name can't silently
    create a new record and report it as a successful modify."""
    from .dns_records import modify_record, DnsRecordError

    try:
        result = modify_record(
            zone_id=body.zone_id, name=body.name, record_type=body.record_type,
            ttl=body.ttl, values=body.values,
            alias_target=body.alias_target.model_dump() if body.alias_target else None,
            set_identifier=body.set_identifier,
            sr_number=body.sr_number, actor_email=actor,
        )
    except DnsRecordError as e:
        _record_dns_failure(action="modify_failed", body=body, actor=actor, detail=str(e))
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("dns_modify_record_endpoint unexpected error")
        _record_dns_failure(action="modify_failed", body=body, actor=actor, detail="Unexpected error")
        raise HTTPException(502, "Could not modify this DNS record")

    dns_audit_store.record(
        action="modify", zone_id=result["zone"]["id"], zone_name=result["zone"]["display_name"],
        record_name=result["record"]["display_name"], record_type=result["record"]["type"],
        ttl=result["record"]["ttl"], value=_dns_audit_value(result["record"]),
        previous_value=_dns_audit_value(result.get("previous")),
        sr_number=result["sr_number"], change_id=result["change"]["change_id"],
        result="modified", actor_email=actor, actor_name=user_store.get_display_name(actor),
    )
    return result


@app.post("/api/dns/records/delete")
def dns_delete_record_endpoint(body: DnsRecordDeleteBody, actor: str = Depends(require_dns_writer)):
    """Remove a record.

    POST rather than HTTP DELETE because this needs a request BODY (the
    record identity plus the mandatory SR number) and a DELETE body is
    the one thing intermediaries are entitled to drop — putting the SR
    number in the query string instead would also leak it into ALB access
    logs. Same POST-for-an-action shape as /api/whitelist/lms/add.
    """
    from .dns_records import delete_record, DnsRecordError

    try:
        result = delete_record(
            zone_id=body.zone_id, name=body.name, record_type=body.record_type,
            set_identifier=body.set_identifier,
            sr_number=body.sr_number, actor_email=actor,
        )
    except DnsRecordError as e:
        _record_dns_failure(action="delete_failed", body=body, actor=actor, detail=str(e))
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("dns_delete_record_endpoint unexpected error")
        _record_dns_failure(action="delete_failed", body=body, actor=actor, detail="Unexpected error")
        raise HTTPException(502, "Could not delete this DNS record")

    # The deleted record's contents go in previous_value, not value —
    # after this call there is no current value, and an audit reader
    # scanning the `value` column should not see a deleted record's
    # payload as if it were still live.
    dns_audit_store.record(
        action="delete", zone_id=result["zone"]["id"], zone_name=result["zone"]["display_name"],
        record_name=result["record"]["display_name"], record_type=result["record"]["type"],
        ttl=result["record"]["ttl"], previous_value=_dns_audit_value(result["record"]),
        sr_number=result["sr_number"], change_id=result["change"]["change_id"],
        result="deleted", actor_email=actor, actor_name=user_store.get_display_name(actor),
    )
    return result


@app.get("/api/dns/audit-log")
def dns_audit_log_endpoint(limit: int = 500, actor: str = Depends(require_dns_audit_viewer)):
    """Restricted to admins + Cloud Infra/Security/Management Team
    members — see require_dns_audit_viewer()'s docstring above."""
    return {"records": dns_audit_store.list_records(limit=limit)}


# ── DNS bulk import (create/modify only, from an uploaded text file) ──────
# See dns_records.py's "Bulk import" section docstring for the full
# rationale: no delete, no alias, no routing-policy siblings, one
# ChangeResourceRecordSets call per row, and apply always re-validates
# against live Route 53 state rather than trusting a stale preview.
#
# The browser reads a CSV/TSV/zone-file with FileReader.readAsText() and
# sends the text as a plain JSON string field — deliberately NOT
# multipart/form-data. Every other file-upload route in this app
# (extractFromFile, kbUpload, ...) sits behind the ALB's CasesBypassRule
# because multipart POST doesn't survive the SameSite-cookie redirect in
# the normal OIDC path, and that bypass is authenticated only by a
# spoofable X-Actor-Email header. A route that can batch-write production
# DNS is not put behind that bypass: DNS bulk files are a few KB at most,
# so there is no need to trade the OIDC session for multipart support.
#
# .xlsx (Excel) is also accepted, but it's a binary ZIP-based format, so
# it cannot travel as plain text the way CSV/TSV/zone-file do — the
# browser instead reads an .xlsx pick with FileReader.readAsArrayBuffer(),
# base64-encodes it client-side, and sends it as the SEPARATE
# `file_content_b64` field below rather than `file_content`. Both fields
# are optional and mutually exclusive in practice — see
# dns_records._parse_bulk_input()'s docstring for exactly how the two are
# told apart server-side.

class DnsBulkPreviewBody(BaseModel):
    zone_id: str
    file_content: str = ""
    file_content_b64: Optional[str] = None


class DnsBulkApplyBody(BaseModel):
    zone_id: str
    file_content: str = ""
    file_content_b64: Optional[str] = None
    sr_number: str


@app.post("/api/dns/bulk/preview")
def dns_bulk_preview_endpoint(body: DnsBulkPreviewBody, actor: str = Depends(require_dns_writer)):
    """Parse + classify every record in the file against the zone's
    live state. Makes zero writes — gated the same as every DNS write
    (require_dns_writer) purely so a read-only user cannot probe which
    records exist elsewhere in the account via a crafted upload; no SR
    number is required at this stage since nothing is changed yet."""
    from .dns_records import preview_bulk_import, DnsRecordError

    try:
        return preview_bulk_import(
            zone_id=body.zone_id, file_content=body.file_content,
            file_content_b64=body.file_content_b64,
        )
    except DnsRecordError as e:
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("dns_bulk_preview_endpoint unexpected error")
        raise HTTPException(502, "Could not preview this file")


@app.post("/api/dns/bulk/apply")
def dns_bulk_apply_endpoint(body: DnsBulkApplyBody, actor: str = Depends(require_dns_writer)):
    """Re-validate the same file against Route 53's current state and
    write every CREATE/MODIFY row. One audit_store row per record,
    tagged with a shared batch_id so the whole upload's effect can be
    found later by filtering the audit log on that id."""
    from .dns_records import apply_bulk_import, DnsRecordError

    try:
        result = apply_bulk_import(
            zone_id=body.zone_id, file_content=body.file_content,
            file_content_b64=body.file_content_b64,
            sr_number=body.sr_number, actor_email=actor,
        )
    except DnsRecordError as e:
        dns_audit_store.record(
            action="bulk_import_failed", zone_id=body.zone_id, zone_name=body.zone_id,
            record_name="(bulk upload)", record_type="", sr_number=body.sr_number,
            result="error", detail=str(e), actor_email=actor,
            actor_name=user_store.get_display_name(actor),
        )
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("dns_bulk_apply_endpoint unexpected error")
        dns_audit_store.record(
            action="bulk_import_failed", zone_id=body.zone_id, zone_name=body.zone_id,
            record_name="(bulk upload)", record_type="", sr_number=body.sr_number,
            result="error", detail="Unexpected error", actor_email=actor,
            actor_name=user_store.get_display_name(actor),
        )
        raise HTTPException(502, "Could not apply this bulk import")

    zone = result["zone"]
    actor_name = user_store.get_display_name(actor)
    for row in result["rows"]:
        if row["result"] == "unchanged":
            # Explicitly listed to the user in the preview/apply table,
            # per the requirement that same-value rows be shown rather
            # than hidden — but not worth a row in the audit trail, since
            # nothing about the live record changed.
            continue
        dns_audit_store.record(
            action=("create" if row["result"] == "created" else
                    "modify" if row["result"] == "modified" else "bulk_import_row_failed"),
            zone_id=zone["id"], zone_name=zone["display_name"],
            record_name=row["name"], record_type=row["type"],
            ttl=row.get("ttl"),
            value=", ".join(row.get("values") or []) if row["result"] in ("created", "modified") else None,
            previous_value=", ".join(row.get("previous_values") or []) if row.get("previous_values") else None,
            sr_number=result["sr_number"], change_id=row.get("change_id"),
            result=row["result"], detail=row.get("error"),
            actor_email=actor, actor_name=actor_name, batch_id=result["batch_id"],
        )
    return result


# ── VPN Profile Lookup (Resources) ────────────────────────────────────────
# Given a site's WAN IP (its Customer Gateway device's public IP), scans
# every region of the network account (100000000003) for a matching AWS
# Site-to-Site VPN connection and returns its full tunnel/IKE/IPsec/DPD
# profile — see app/vpn_lookup.py's module docstring for the full
# rationale and the cross-account access pattern reused here (same
# AssumeRole(PortalReadOnlyRole) chain as CMDB/Cost Analysis). Single
# on-demand lookup, no cache. Read-only for everyone by default (like
# every other AWS data-visibility tab) — restrict via the Admin page's
# Tab Access Control if pre-shared keys shouldn't be visible to every
# portal user.

@app.get("/api/vpn-lookup/search")
def vpn_lookup_search_endpoint(wan_ip: str, account_id: Optional[str] = None, actor: str = Depends(get_actor)):
    from .vpn_lookup import search_vpn_by_wan_ip, VpnLookupError, DEFAULT_ACCOUNT_ID

    try:
        return search_vpn_by_wan_ip(wan_ip, account_id or DEFAULT_ACCOUNT_ID)
    except VpnLookupError as e:
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("vpn_lookup_search_endpoint unexpected error")
        raise HTTPException(502, "Could not complete the VPN lookup")


# ── S2S VPN Profile Creation (Security section) ─────────────────────────
# Wizard that CREATES real Site-to-Site VPN infrastructure — Customer
# Gateways, VPN connections (attached to one of four fixed Transit
# Gateways), and static routes on those Transit Gateways' route tables —
# in the network account (100000000003), across ap-southeast-1, us-east-1,
# eu-west-2 and ap-south-1 only. See app/s2s_vpn_creation.py for the full
# design rationale and its module docstring for why every write here is
# gated by require_vpn_writer() + a mandatory SR number + an audit
# trail in s2s_vpn_audit_store.py, mirroring the DNS Record tab exactly.
#
# Reuses app/vpn_lookup.py's cross-account AssumeRole plumbing
# (JobPortal-CrossAccount-InfraOps-<acct>, the ECS task's OWN identity —
# no static keys) as-is; see that module's docstring for the full
# cross-account rationale.


class S2sVpnCustomerGatewayCreateBody(BaseModel):
    region: str
    name_tag: str
    ip_address: str
    bgp_asn: int
    sr_number: str


class S2sVpnConnectionCreateBody(BaseModel):
    region: str
    name_tag: str
    customer_gateway_id: str
    target_gateway_type: str = "Transit gateway"
    transit_gateway_id: Optional[str] = None
    virtual_private_gateway_id: Optional[str] = None
    static_routes_only: bool = True
    sr_number: str


class S2sVpnRouteCreateBody(BaseModel):
    region: str
    cidr: str
    attachment_ref: str
    sr_number: str


def _s2s_vpn_audit_failure(
    *, action: str, region: str, resource_type: str, resource_name: Optional[str],
    sr_number: str, actor: str, detail: str,
) -> None:
    s2s_vpn_audit_store.record(
        action=action, region=region, resource_type=resource_type,
        resource_id=None, resource_name=resource_name, sr_number=sr_number,
        result="error", detail=detail, actor_email=actor,
        actor_name=user_store.get_display_name(actor),
    )


@app.get("/api/s2s-vpn/config")
def s2s_vpn_config_endpoint(actor: str = Depends(get_actor)):
    """Fixed mappings + tunnel standard the wizard renders — allowed
    regions, Transit Gateway per region, TGW route table per region,
    target-gateway-type choices, and the read-only Tunnel Configuration
    Standard. Read-only for anyone who can see the tab."""
    from .s2s_vpn_creation import get_config
    return get_config()


@app.get("/api/s2s-vpn/permissions")
def s2s_vpn_permissions_endpoint(actor: str = Depends(get_actor)):
    """What this caller is allowed to do in the S2S VPN Profile Creation
    tab — a UI HINT ONLY, mirroring dns_permissions_endpoint() exactly.
    Real enforcement is require_vpn_writer()/require_s2s_vpn_audit_viewer()
    on the routes themselves."""
    privileged = (
        user_store.is_admin(actor)
        or bool(set(user_store.get_user_groups(actor)) & _S2S_VPN_PRIVILEGED_GROUPS)
    )
    return {"can_write": privileged, "can_view_audit": privileged}


@app.get("/api/s2s-vpn/customer-gateways")
def s2s_vpn_list_customer_gateways_endpoint(region: str, actor: str = Depends(get_actor)):
    """Every non-deleted Customer Gateway in `region` — backs the
    wizard's "reuse an existing CGW?" popup. Read-only for anyone who can
    see the tab."""
    from .s2s_vpn_creation import list_customer_gateways, S2sVpnCreationError

    try:
        return {"region": region, "customer_gateways": list_customer_gateways(region)}
    except S2sVpnCreationError as e:
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("s2s_vpn_list_customer_gateways_endpoint unexpected error")
        raise HTTPException(502, "Could not list Customer Gateways")


@app.post("/api/s2s-vpn/customer-gateways", status_code=201)
def s2s_vpn_create_customer_gateway_endpoint(
    body: S2sVpnCustomerGatewayCreateBody, actor: str = Depends(require_vpn_writer)
):
    from .s2s_vpn_creation import create_customer_gateway, S2sVpnCreationError

    try:
        result = create_customer_gateway(
            body.region, name_tag=body.name_tag, ip_address=body.ip_address,
            bgp_asn=body.bgp_asn, account_id=os.environ.get("S2S_VPN_ACCOUNT_ID", ""),
        )
    except S2sVpnCreationError as e:
        _s2s_vpn_audit_failure(
            action="create_customer_gateway_failed", region=body.region,
            resource_type="customer_gateway", resource_name=body.name_tag,
            sr_number=body.sr_number, actor=actor, detail=str(e),
        )
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("s2s_vpn_create_customer_gateway_endpoint unexpected error")
        _s2s_vpn_audit_failure(
            action="create_customer_gateway_failed", region=body.region,
            resource_type="customer_gateway", resource_name=body.name_tag,
            sr_number=body.sr_number, actor=actor, detail="Unexpected error",
        )
        raise HTTPException(502, "Could not create this Customer Gateway")

    s2s_vpn_audit_store.record(
        action="create_customer_gateway", region=body.region, resource_type="customer_gateway",
        resource_id=result["customer_gateway_id"], resource_name=result["name"],
        sr_number=body.sr_number, result="created", actor_email=actor,
        actor_name=user_store.get_display_name(actor),
    )
    return result


@app.post("/api/s2s-vpn/vpn-connections", status_code=201)
def s2s_vpn_create_connection_endpoint(
    body: S2sVpnConnectionCreateBody, actor: str = Depends(require_vpn_writer)
):
    from .s2s_vpn_creation import create_vpn_connection, S2sVpnCreationError

    try:
        result = create_vpn_connection(
            body.region, name_tag=body.name_tag, customer_gateway_id=body.customer_gateway_id,
            target_gateway_type=body.target_gateway_type,
            transit_gateway_id=body.transit_gateway_id,
            virtual_private_gateway_id=body.virtual_private_gateway_id,
            static_routes_only=body.static_routes_only,
            account_id=os.environ.get("S2S_VPN_ACCOUNT_ID", ""),
        )
    except S2sVpnCreationError as e:
        _s2s_vpn_audit_failure(
            action="create_vpn_connection_failed", region=body.region,
            resource_type="vpn_connection", resource_name=body.name_tag,
            sr_number=body.sr_number, actor=actor, detail=str(e),
        )
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("s2s_vpn_create_connection_endpoint unexpected error")
        _s2s_vpn_audit_failure(
            action="create_vpn_connection_failed", region=body.region,
            resource_type="vpn_connection", resource_name=body.name_tag,
            sr_number=body.sr_number, actor=actor, detail="Unexpected error",
        )
        raise HTTPException(502, "Could not create this Site-to-Site VPN connection")

    s2s_vpn_audit_store.record(
        action="create_vpn_connection", region=body.region, resource_type="vpn_connection",
        resource_id=result["vpn_connection_id"], resource_name=result["name"],
        sr_number=body.sr_number, result="created", actor_email=actor,
        actor_name=user_store.get_display_name(actor),
    )
    return result


@app.post("/api/s2s-vpn/routes", status_code=201)
def s2s_vpn_create_route_endpoint(body: S2sVpnRouteCreateBody, actor: str = Depends(require_vpn_writer)):
    from .s2s_vpn_creation import add_static_route, S2sVpnCreationError

    try:
        result = add_static_route(
            body.region, cidr=body.cidr, attachment_ref=body.attachment_ref,
            account_id=os.environ.get("S2S_VPN_ACCOUNT_ID", ""),
        )
    except S2sVpnCreationError as e:
        _s2s_vpn_audit_failure(
            action="create_tgw_route_failed", region=body.region,
            resource_type="tgw_route", resource_name=body.cidr,
            sr_number=body.sr_number, actor=actor, detail=str(e),
        )
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("s2s_vpn_create_route_endpoint unexpected error")
        _s2s_vpn_audit_failure(
            action="create_tgw_route_failed", region=body.region,
            resource_type="tgw_route", resource_name=body.cidr,
            sr_number=body.sr_number, actor=actor, detail="Unexpected error",
        )
        raise HTTPException(502, "Could not add this route")

    s2s_vpn_audit_store.record(
        action="create_tgw_route", region=body.region, resource_type="tgw_route",
        resource_id=f"{result['route_table_id']}:{result['destination_cidr_block']}",
        resource_name=result["destination_cidr_block"], sr_number=body.sr_number,
        result="created", actor_email=actor, actor_name=user_store.get_display_name(actor),
    )
    return result


@app.get("/api/s2s-vpn/audit-log")
def s2s_vpn_audit_log_endpoint(limit: int = 500, actor: str = Depends(require_s2s_vpn_audit_viewer)):
    """Restricted to admins + Cloud Infra/Security Team members —
    require_s2s_vpn_audit_viewer() bypasses the 's2s_vpn_profile_creation'
    tab flag entirely, same convention as require_dns_audit_viewer()."""
    return {"records": s2s_vpn_audit_store.list_records(limit=limit)}


# Live inventory of AWS hardware/software assets (EC2/RDS/Lambda/S3/
# DynamoDB/Load Balancers) across the whole AWS Organization, reusing
# the same cross-account AssumeRole(PortalReadOnlyRole) pattern as Cost
# Analysis / Billing by Service. Read-only for everyone — no admin gating,
# matching this portal's existing convention for AWS data-visibility
# features. History (added/removed/status-changed) is recorded
# automatically on every successful fetch — see app/cmdb.py + cmdb_store.py.

@app.get("/api/cmdb/inventory")
def get_cmdb_inventory_endpoint(refresh: bool = False, actor: str = Depends(get_actor)):
    """Org-wide CMDB resource inventory, one row per account. `refresh`
    bypasses the 10-minute server-side cache."""
    from .cmdb import get_cmdb_inventory
    from .cost_explorer import CostAnalysisError

    try:
        return get_cmdb_inventory(force_refresh=refresh)
    except CostAnalysisError as e:
        _logger.error("get_cmdb_inventory_endpoint: CostAnalysisError: %s", e)
        raise HTTPException(502, "Could not fetch AWS CMDB inventory")
    except Exception:
        _logger.exception("get_cmdb_inventory_endpoint unexpected error")
        raise HTTPException(502, "Could not fetch AWS CMDB inventory")


@app.get("/api/cmdb/history")
def get_cmdb_history_endpoint(account: Optional[str] = None, limit: int = 200, actor: str = Depends(get_actor)):
    """Audit trail of resource added/removed/status-changed events,
    optionally filtered to one account. Newest first."""
    from . import cmdb_store

    limit = max(1, min(limit, 1000))
    try:
        return {"events": cmdb_store.list_history(account, limit=limit)}
    except Exception:
        _logger.exception("get_cmdb_history_endpoint unexpected error")
        raise HTTPException(502, "Could not fetch CMDB history")


class CmdbImpactAnalysisRequest(BaseModel):
    resource: dict
    account_id: str
    account_name: str


@app.post("/api/cmdb/analyze-impact")
def analyze_cmdb_resource_impact(payload: CmdbImpactAnalysisRequest, actor: str = Depends(get_actor)):
    """AI-generated impact assessment for one CMDB resource: what would
    likely break, who's affected, and what to check before touching it.
    Read-only/advisory — never modifies any AWS resource."""
    from .cmdb import analyse_resource_impact

    try:
        return analyse_resource_impact(payload.resource, payload.account_id, payload.account_name)
    except Exception:
        _logger.exception("analyze_cmdb_resource_impact unexpected error")
        raise HTTPException(502, "Could not generate AI impact analysis for this resource")


@app.get("/api/cmdb/ec2-topology")
def get_cmdb_ec2_topology_endpoint(account_id: str, instance_id: str, actor: str = Depends(get_actor)):
    """Illumio-style per-instance network topology for one EC2 instance:
    its VPC/subnet/security groups plus either real traffic peers (from
    VPC Flow Logs, when a CloudWatch-Logs-destined flow log is active on
    that VPC) or a generic activity indicator (from CloudWatch
    NetworkIn/NetworkOut metrics, when there's no usable flow log).
    Read-only — never modifies any AWS resource. Not cached server-side
    (unlike /cmdb/inventory) since this is a bounded, on-demand,
    single-instance lookup a user explicitly asked for by clicking one
    EC2 row."""
    from .cmdb import get_ec2_topology, CmdbTopologyError

    try:
        return get_ec2_topology(account_id, instance_id)
    except CmdbTopologyError as e:
        raise HTTPException(404, str(e))
    except Exception:
        _logger.exception("get_cmdb_ec2_topology_endpoint unexpected error")
        raise HTTPException(502, "Could not fetch EC2 network topology")


@app.get("/api/cmdb/rds-topology")
def get_cmdb_rds_topology_endpoint(account_id: str, db_instance_id: str, actor: str = Depends(get_actor)):
    """Illumio-style per-instance network topology for one RDS database
    instance: its VPC/subnets/security groups, real traffic peers (VPC
    Flow Logs) or a CloudWatch-metrics fallback indicator, and the same
    mandatory inbound "trigger" classification (User vs. resolved CMDB
    resource) as the EC2 topology. Read-only, on-demand, not cached."""
    from .cmdb import get_rds_topology, CmdbTopologyError

    try:
        return get_rds_topology(account_id, db_instance_id)
    except CmdbTopologyError as e:
        raise HTTPException(404, str(e))
    except Exception:
        _logger.exception("get_cmdb_rds_topology_endpoint unexpected error")
        raise HTTPException(502, "Could not fetch RDS network topology")


@app.get("/api/cmdb/lb-topology")
def get_cmdb_lb_topology_endpoint(account_id: str, lb_name: str, actor: str = Depends(get_actor)):
    """Illumio-style network topology for one Load Balancer: its VPC/
    subnets/security groups, its TargetGroup→Target service chain
    (yellow "will access" context edges), real traffic peers (VPC Flow
    Logs across every ENI the LB owns) or a CloudWatch ProcessedBytes-
    based fallback indicator, and the same mandatory inbound "trigger"
    classification as EC2/RDS. Read-only, on-demand, not cached."""
    from .cmdb import get_lb_topology, CmdbTopologyError

    try:
        return get_lb_topology(account_id, lb_name)
    except CmdbTopologyError as e:
        raise HTTPException(404, str(e))
    except Exception:
        _logger.exception("get_cmdb_lb_topology_endpoint unexpected error")
        raise HTTPException(502, "Could not fetch Load Balancer network topology")


# ── Helpdesk Team catalogue: S3-ScreenSaver ──────────────────────────────────
# Backend-proxy upload tool: user picks a file in the portal, the browser
# multipart-POSTs it to THIS backend (not directly to S3 — see
# app/screensaver.py's module docstring for why the original direct-
# browser-to-S3 presigned-PUT design was replaced: a real user's network/
# proxy/firewall was blocking outbound connections to
# `*.s3.ap-southeast-1.amazonaws.com`, which this team can't fix on the
# user's end), and the backend writes the bytes to the
# `screensaver-100000000001` bucket (account 100000000001, ap-southeast-1)
# and returns the final s3:// path + stable public URL to the user.

@app.post("/api/screensaver/upload")
async def upload_screensaver_file_endpoint(file: UploadFile = File(...), actor: str = Depends(get_actor)):
    """Single-step proxy upload: read the multipart file bytes here, then
    hand them to screensaver.upload_screensaver_file() which does the
    put_object() + publish-as-latest + CloudFront invalidation and
    returns the final metadata. This is a multipart POST and needs the
    same ALB OIDC bypass treatment as other upload routes (see
    compute_stack.py's UploadAndEscalateBypassRule — `/api/screensaver/*`
    is already in that bypass list and still matches this new path)."""
    from .screensaver import upload_screensaver_file, ScreenSaverError

    content = await file.read()
    try:
        return upload_screensaver_file(
            filename=file.filename or "file",
            content_type=file.content_type,
            content=content,
            uploaded_by_email=actor,
        )
    except ScreenSaverError as e:
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("upload_screensaver_file_endpoint unexpected error")
        raise HTTPException(502, "Could not upload file")


@app.get("/api/screensaver/files")
def list_screensaver_files_endpoint(actor: str = Depends(get_actor)):
    """List every historical ScreenSaver upload sitting in the bucket
    (see screensaver.list_uploaded_files()'s docstring for WHY this
    exists: every upload's dated key is kept forever as an audit trail
    and never auto-deleted, so old uploads accumulate — a stale/cached
    consumer pointed directly at an old dated key, rather than the fixed
    `latest/current` URL, is what actually causes the "link didn't
    update until I manually deleted the old file" symptom some users
    hit). The frontend shows this list with a delete button per row and
    a reminder banner to clean up old files after every new upload."""
    from .screensaver import list_uploaded_files, ScreenSaverError

    try:
        return {"files": list_uploaded_files()}
    except ScreenSaverError as e:
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("list_screensaver_files_endpoint unexpected error")
        raise HTTPException(502, "Could not list uploaded files")


@app.delete("/api/screensaver/files")
def delete_screensaver_file_endpoint(s3_key: str, actor: str = Depends(get_actor)):
    """Delete one historical ScreenSaver upload by its S3 key (query
    param, e.g. ?s3_key=screensaver/2026/08/27/<uuid>-file.jpg). Refuses
    to delete the live `latest/current` key — see
    screensaver.delete_uploaded_file()'s docstring."""
    from .screensaver import delete_uploaded_file, ScreenSaverError

    try:
        delete_uploaded_file(s3_key)
        return {"deleted": s3_key}
    except ScreenSaverError as e:
        raise HTTPException(400, str(e))
    except Exception:
        _logger.exception("delete_screensaver_file_endpoint unexpected error")
        raise HTTPException(502, "Could not delete file")


def _fetch_raw_health_events() -> list[dict]:
    """The slow part — paginating describe_events_for_organization() (or
    the local mock). Wrapped so it can go through _HEALTH_DASHBOARD_CACHE;
    deliberately does NOT include health_ack_store's checked/finished/
    remark state, since that's per-user-action data that must reflect
    immediately after a toggle, never served stale from this cache."""
    if ENV == "local":
        return _local_mock_health_events()
    from concurrent.futures import ThreadPoolExecutor
    from .escalation import list_ams_organization_health_events, EscalationError
    from . import health_member_accounts

    # The organization API does not return every event — some
    # ACCOUNT_SPECIFIC ones are visible only through the member account's
    # own Health API (see health_member_accounts.py). Query both at once so
    # the extra fan-out adds no wall-clock time; the organization result
    # stays authoritative and the member result only ADDS events whose ARN
    # it did not return. A member-side failure never breaks the dashboard.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        member_future = pool.submit(health_member_accounts.collect_member_events)
        try:
            org_events = list_ams_organization_health_events()
        except EscalationError as e:
            raise HTTPException(502, e.message)
        except Exception as e:
            _logger.error("get_org_health_dashboard unexpected: %s", e)
            raise HTTPException(502, "Could not fetch AWS Health events")
        try:
            member = member_future.result()
            extra = health_member_accounts.events_missing_from_org(member, {e.get("arn") for e in org_events})
        except Exception as e:
            _logger.error("member-account health events unavailable, showing organization view only: %s", e)
            extra = []
    finally:
        # Don't make a failed organization call wait for the member fan-out.
        pool.shutdown(wait=False)
    return org_events + extra


@app.get("/api/health-dashboard/organization")
def get_org_health_dashboard(refresh: bool = False, actor: str = Depends(get_actor)):
    """Full AWS Health Dashboard payload — all tabs + counts. The
    underlying org-wide event fetch is cached for 5 minutes (see
    cache_utils.py); pass refresh=true to bypass it. checked/finished/
    remark/AMS-SR/Job state is always merged in fresh (never cached) so a
    toggle is reflected immediately even on a cached events list."""
    cached = _HEALTH_DASHBOARD_CACHE.get_or_fetch(
        "org_health_events", lambda: {"events": _fetch_raw_health_events()}, force_refresh=refresh,
    )
    raw_events = cached["events"]
    cache_age_seconds = cached.get("cache_age_seconds", 0)

    checks_map = health_ack_store.list_checks()
    events = [_norm_health_event(e, checks_map) for e in raw_events]
    result = _group_health_events(events)
    result["cache_age_seconds"] = cache_age_seconds
    return result


def _merge_resource_checks(detail: dict, event_arn: str) -> dict:
    """Attach each affected resource's persisted tick state (if any) from
    health_ack_store, keyed the same way set_resource_check() stores it
    (entity_value + account). Lets an engineer tick individual resources
    inside the event-detail popup, independent of the popup's own
    "finished" checkbox and the row-level "checked" checkbox."""
    check = health_ack_store.get_check(event_arn) or {}
    resource_checks = check.get("resource_checks") or {}
    for ent in detail.get("affected_entities", []):
        rk = health_ack_store.resource_key(ent.get("entity_value"), ent.get("aws_account_id"))
        rc = resource_checks.get(rk) or {}
        ent["resource_key"] = rk
        ent["checked"] = bool(rc.get("checked"))
        ent["checked_by_name"] = rc.get("checked_by_name")
        ent["checked_at"] = rc.get("checked_at")
    return detail


@app.get("/api/health-dashboard/organization/event-detail")
def get_org_health_event_detail(event_arn: str, actor: str = Depends(get_actor)):
    """Drill-down for one event (Affected accounts / Affected resources) —
    shown in the modal when a user clicks the event name."""
    if ENV == "local":
        return _merge_resource_checks(_local_mock_health_event_detail(event_arn), event_arn)

    from .escalation import EscalationError
    try:
        detail = _org_or_member_event_detail(event_arn)
    except EscalationError as e:
        raise HTTPException(502, e.message)
    except Exception as e:
        _logger.error("get_org_health_event_detail unexpected for %s: %s", event_arn, e)
        raise HTTPException(502, "Could not fetch event detail")
    return _merge_resource_checks(detail, event_arn)


@app.get("/api/health-dashboard/organization/event-ai-suggestion")
def get_org_health_event_ai_suggestion(event_arn: str, actor: str = Depends(get_actor)):
    """
    On-demand AI suggestion for one Health event — "what will need to be
    done or be aware of", shown when the engineer clicks a button in the
    event detail popup. Not fetched automatically alongside event-detail
    to avoid an unnecessary Bedrock call on every popup open; the frontend
    only calls this when the engineer explicitly asks for it.
    """
    checks_map = health_ack_store.list_checks()

    if ENV == "local":
        raw_events = _local_mock_health_events()
        detail = _local_mock_health_event_detail(event_arn)
    else:
        from .escalation import EscalationError
        try:
            # Same cached list the dashboard shows (organization events PLUS
            # member-account-only events) so an account-specific event is
            # found here too, and the AI call doesn't re-page 3,000+ events.
            raw_events = _HEALTH_DASHBOARD_CACHE.get_or_fetch(
                "org_health_events", lambda: {"events": _fetch_raw_health_events()},
            )["events"]
            detail = _org_or_member_event_detail(event_arn)
        except EscalationError as e:
            raise HTTPException(502, e.message)
        except Exception as e:
            _logger.error("get_org_health_event_ai_suggestion fetch failed for %s: %s", event_arn, e)
            raise HTTPException(502, "Could not fetch event data for AI analysis")

    raw = next((e for e in raw_events if e.get("arn") == event_arn), None)
    event = _norm_health_event(raw, checks_map) if raw else {
        "event_type_code": "Unknown", "service": "Unknown", "region": "Unknown", "status_code": "Unknown",
    }

    try:
        suggestion = analyse_health_event(
            event_type_code=event.get("event_type_code") or "Unknown",
            service=event.get("service") or "Unknown",
            region=event.get("region") or "Unknown",
            status_code=event.get("status_code") or "Unknown",
            event_description=detail.get("event_description"),
            affected_accounts=detail.get("affected_accounts") or [],
            affected_entities=detail.get("affected_entities") or [],
        )
    except Exception as e:
        _logger.error("analyse_health_event failed for %s: %s", event_arn, e)
        raise HTTPException(502, "Could not generate AI suggestion")

    return suggestion


class SetHealthEventCheckBody(BaseModel):
    event_arn: str
    checked: bool


@app.post("/api/health-dashboard/organization/check")
def set_org_health_event_check(body: SetHealthEventCheckBody, actor: str = Depends(get_actor)):
    """Tick/untick the row-level checkbox for one Health event."""
    actor_name = TEAM_MEMBERS.get(actor, actor)
    return health_ack_store.set_check(body.event_arn, body.checked, actor, actor_name)


class SetHealthEventRemarkBody(BaseModel):
    event_arn: str
    remark: str


@app.post("/api/health-dashboard/organization/remark")
def set_org_health_event_remark(body: SetHealthEventRemarkBody, actor: str = Depends(get_actor)):
    """
    Save a free-text engineer remark for one Health event — rendered next
    to "Last Updated" in all 4 dashboard tabs (Task 12, sub-feature 1).
    """
    remark = body.remark.strip()[:2000]
    actor_name = TEAM_MEMBERS.get(actor, actor)
    return health_ack_store.set_remark(body.event_arn, remark, actor, actor_name)


class SetHealthEventFinishedBody(BaseModel):
    event_arn: str
    finished: bool


@app.post("/api/health-dashboard/organization/finish")
def set_org_health_event_finished(body: SetHealthEventFinishedBody, actor: str = Depends(get_actor)):
    """
    Tick/untick "Mark as finished" inside the event-detail popup — a
    separate flag from the row-level checkbox above, for an engineer to
    record that their remediation work on this event is done (Task 12,
    sub-feature 2).
    """
    actor_name = TEAM_MEMBERS.get(actor, actor)
    return health_ack_store.set_finished(body.event_arn, body.finished, actor, actor_name)


class SetHealthEventCategoryBody(BaseModel):
    event_arn: str
    # One of _OVERRIDABLE_TAB_KEYS (open_and_recent_issues /
    # scheduled_changes / retirements / lifecycle_events /
    # other_notifications), or None to clear the override and revert this
    # event to automatic classification.
    category: Optional[str] = None


@app.post("/api/health-dashboard/organization/category-override")
def set_org_health_event_category(body: SetHealthEventCategoryBody, actor: str = Depends(require_admin)):
    """
    Admin-only: manually move a Health event to a different dashboard tab
    than what AWS's own eventTypeCategory/eventTypeCode would normally
    place it in — the backend for the drag-and-drop feature (e.g. an
    admin dragging an AWS_SDK_PLANNED_LIFECYCLE_EVENT out of "Scheduled
    changes" into "Lifecycle event"). Persisted per-event (shared across
    the whole portal, same as checked/finished/remark), and always takes
    precedence in _group_health_events() until cleared (category=None).
    """
    if body.category is not None and body.category not in _OVERRIDABLE_TAB_KEYS:
        raise HTTPException(400, f"Unknown category '{body.category}'. Must be one of: {sorted(_OVERRIDABLE_TAB_KEYS)}")
    actor_name = TEAM_MEMBERS.get(actor, actor)
    return health_ack_store.set_category_override(body.event_arn, body.category, actor, actor_name)


class SetHealthEventResourceCheckBody(BaseModel):
    event_arn: str
    resource_key: str
    checked: bool


@app.post("/api/health-dashboard/organization/resource-check")
def set_org_health_event_resource_check(body: SetHealthEventResourceCheckBody, actor: str = Depends(get_actor)):
    """
    Tick/untick one row of the "Affected resources" table inside the
    event-detail popup — lets an engineer confirm resources one by one,
    separate from the popup's own "finished" checkbox and the row-level
    "checked" checkbox on the main dashboard table.
    """
    actor_name = TEAM_MEMBERS.get(actor, actor)
    check = health_ack_store.set_resource_check(body.event_arn, body.resource_key, body.checked, actor, actor_name)
    return {
        "event_arn": body.event_arn,
        "resource_key": body.resource_key,
        "checked": body.checked,
        "resource_checks": check.get("resource_checks") or {},
    }


# AWS Health service names -> portal category, for the one-click "Create
# Job" heuristic below. Anything not listed falls back to "Other" so the
# job is still created (never blocked) rather than requiring the engineer
# to pick first — category can always be corrected on the Job afterwards.
_HEALTH_SERVICE_TO_CATEGORY = {
    "EC2": "Compute", "LAMBDA": "Compute", "ECS": "Compute", "EKS": "Compute", "AUTOSCALING": "Compute",
    "RDS": "Storage", "S3": "Storage", "EBS": "Storage", "EFS": "Storage", "DYNAMODB": "Storage", "BACKUP": "Storage",
    "VPC": "Networking", "ROUTE53": "Networking", "CLOUDFRONT": "Networking", "DIRECTCONNECT": "Networking",
    "ELASTICLOADBALANCING": "Networking", "TRANSITGATEWAY": "Networking",
    "IAM": "Security", "GUARDDUTY": "Security", "KMS": "Security", "WAF": "Security", "SECURITYHUB": "Security",
    "DIRECTORYSERVICE": "AD/Identity", "SSO": "AD/Identity", "COGNITO": "AD/Identity",
}


def _health_event_to_job_category(service: Optional[str]) -> str:
    return _HEALTH_SERVICE_TO_CATEGORY.get((service or "").upper(), "Other")


def _health_event_to_job_priority(event_type_category: Optional[str], status_code: Optional[str]) -> str:
    """Open issues are the most urgent; scheduled changes get a working
    lead time (Medium); anything else/closed defaults to Low."""
    if event_type_category == "issue" and status_code == "open":
        return "High"
    if event_type_category == "scheduledChange":
        return "Medium"
    return "Low"


class CreateJobFromHealthEventBody(BaseModel):
    event_arn: str
    service: str
    event_type_code: str
    event_type_category: Optional[str] = None
    region: Optional[str] = None
    status_code: Optional[str] = None


@app.post("/api/health-dashboard/organization/create-job", status_code=201)
def create_job_from_health_event(body: CreateJobFromHealthEventBody, actor: str = Depends(require_tab_write("jobs"))):
    """
    One-click "Create Job" directly from a Health event row (any of the
    4 tabs) — for events that need engineer follow-up whether or not an
    AMS SR was also raised for the same event (the two are independent;
    ams_sr_case_id and job_id can both be set on the same event, or just
    one, or neither). The new Job is assigned to whoever clicked the
    button and pre-filled with the event's own details; category/priority
    are a best-effort heuristic the engineer can correct afterwards.
    """
    existing = health_ack_store.get_check(body.event_arn)
    if existing and existing.get("job_id"):
        raise HTTPException(400, f"A Job was already created for this event: {existing['job_id']}")

    actor_name = TEAM_MEMBERS.get(actor, actor)
    category = _health_event_to_job_category(body.service)
    priority = _health_event_to_job_priority(body.event_type_category, body.status_code)
    title = f"AWS Health: {body.service} — {body.event_type_code}"[:200]
    description = (
        f"Follow-up job auto-created from AWS Health Dashboard event.\n\n"
        f"Health Event ARN: {body.event_arn}\n"
        f"Service: {body.service}\n"
        f"Event type: {body.event_type_code}\n"
        f"Category: {body.event_type_category or 'unknown'}\n"
        f"Region: {body.region or 'unknown'}\n"
        f"AWS status: {body.status_code or 'unknown'}\n"
    )
    if existing and existing.get("ams_sr_case_id"):
        description += f"\nAn AMS Service Request was already raised for this event: {existing['ams_sr_case_id']}\n"
    if existing and existing.get("remark"):
        description += f"\nEngineer remark: {existing['remark']}\n"

    j = job_store.create_job(
        title=title,
        description=description[:5000],
        category=category,
        category_other=None,
        priority=priority,
        assignee_email=actor,
        assignee_name=actor_name,
        created_by_email=actor,
        created_by_name=actor_name,
        due_date=None,
        audit_log=[{
            "timestamp": datetime.utcnow().isoformat(),
            "actor": actor_name,
            "actor_email": actor,
            "event": f"Job created from AWS Health Dashboard event ({body.service} — {body.event_type_code})",
        }],
    )
    notify_job_assigned(j)

    updated = health_ack_store.set_job_created(
        body.event_arn, job_id=j["id"], ticket_number=j.get("ticket_number"),
        actor_email=actor, actor_name=actor_name,
    )
    return {"job": j, "check": updated}


class RaiseHealthEventSrBody(BaseModel):
    event_arn: str
    service: str
    event_type_code: str
    event_type_category: Optional[str] = None
    region: Optional[str] = None
    status_code: Optional[str] = None


@app.post("/api/health-dashboard/organization/raise-sr")
async def raise_sr_for_health_event(body: RaiseHealthEventSrBody, actor: str = Depends(get_actor)):
    """
    One-click "Raise SR to AMS" directly from a Health event row — no Job
    required (Task 12, sub-feature 3). Reuses the same AMS account
    (100000000001) as Job-based escalation, but builds the case body from
    the Health event's own fields. The resulting case_id is persisted onto
    this event's health_ack_store record so the row can show "already
    raised" (case number + icon) and the All Cases page can link back to
    this event (sub-feature 4).
    """
    import asyncio

    existing = health_ack_store.get_check(body.event_arn)
    if existing and existing.get("ams_sr_case_id"):
        raise HTTPException(400, f"An AMS Service Request was already raised for this event: {existing['ams_sr_case_id']}")

    actor_name = TEAM_MEMBERS.get(actor, actor)
    remark = (existing or {}).get("remark")

    if ENV == "local":
        case_id = f"case-ams-sr-health-{datetime.utcnow().strftime('%Y%m%d%H%M')}-{str(uuid.uuid4())[:6]}"
        case_status = "opened"
    else:
        from .escalation import raise_aws_ams_sr_for_health_event, EscalationError
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: raise_aws_ams_sr_for_health_event(
                    event_arn=body.event_arn,
                    service=body.service,
                    event_type_code=body.event_type_code,
                    event_type_category=body.event_type_category,
                    region=body.region,
                    status_code=body.status_code,
                    remark=remark,
                ),
            )
        except EscalationError as e:
            raise HTTPException(502, e.message)
        except Exception as e:
            _logger.error("raise_sr_for_health_event unexpected for %s: %s", body.event_arn, e)
            raise HTTPException(502, "Could not raise AMS Service Request")
        case_id = result["case_id"]
        case_status = result.get("status")

    updated = health_ack_store.set_ams_sr_case(
        body.event_arn,
        case_id=case_id,
        case_status=case_status,
        service=body.service,
        event_type_code=body.event_type_code,
        actor_email=actor,
        actor_name=actor_name,
    )
    # Same "who raised this" bookkeeping as the standalone Create-Case
    # button and Job escalation paths above.
    standalone_case_store.record_case(
        case_id, account="ams", case_type="aws_ams_sr",
        subject=f"AWS Health event: {body.service} — {body.event_type_code}",
        category=None, priority=None, display_id=None,
        actor_email=actor, actor_name=actor_name,
    )
    return {
        "case_id": case_id,
        "status": case_status,
        "url": f"https://support.console.aws.amazon.com/support/home#/case/?caseId={case_id}",
        "check": updated,
    }


# ── Job Notes ─────────────────────────────────────────────────────────────────

class AddNoteBody(BaseModel):
    text: str


@app.post("/api/jobs/{job_id}/notes", status_code=201)
def add_note(
    job_id: str,
    body: AddNoteBody,
    actor: str = Depends(get_actor),
):
    """Add an engineer note to a job. Notes are also indexed into the KB for AI retrieval."""
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")

    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Note text cannot be empty")
    if len(text) > 5000:
        raise HTTPException(400, "Note too long — maximum 5000 characters")

    actor_name = TEAM_MEMBERS.get(actor, actor)
    note_id = str(uuid.uuid4())
    note = {
        "id": note_id,
        "author": actor_name,
        "author_email": actor,
        "text": text,
        "created_at": datetime.utcnow().isoformat(),
        # Photo/file attachments (screenshots, logs, etc.) — added
        # separately via POST .../notes/{note_id}/attachments after the
        # note itself is created. Each entry stores its S3 key; a
        # presigned URL for viewing/downloading is only generated on
        # demand (see GET .../attachments/{attachment_id} below) so it
        # never goes stale sitting inside a cached job response.
        "attachments": [],
    }

    notes = list(j.get("notes") or [])
    notes.append(note)
    j = job_store.update_job_fields(job_id, {"notes": notes})

    # Also index this note as a KB chunk so AI can find it in future analyses
    kb_text = (
        f"Job Note — {j['title']} [{j['category']} / {j['priority']}]\n"
        f"Author: {actor_name}  Date: {note['created_at'][:10]}\n\n"
        f"{text}"
    )
    chunks_data = [{"chunk_id": 0, "text": kb_text}]
    job_store.create_kb_document(
        filename=f"Note: {j['title'][:60]}",
        file_type="note",
        description=f"Engineer note on job {job_id} by {actor_name}",
        char_count=len(kb_text),
        chunk_count=1,
        chunks=chunks_data,
        uploaded_by=actor,
    )

    j = add_audit(j, actor, f"Note added ({len(text)} chars)")
    return j


@app.delete("/api/jobs/{job_id}/notes/{note_id}", status_code=200)
def delete_note(
    job_id: str,
    note_id: str,
    actor: str = Depends(get_actor),
):
    """Delete a note from a job (only the author can delete their own note)."""
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")

    notes = list(j.get("notes") or [])
    target = next((n for n in notes if n["id"] == note_id), None)
    if not target:
        raise HTTPException(404, "Note not found")
    if target["author_email"] != actor:
        raise HTTPException(403, "You can only delete your own notes")

    # Best-effort cleanup of any S3 objects this note owned — never blocks
    # the note deletion itself if it fails (see delete_attachment()'s
    # own docstring).
    from . import note_attachments
    for att in (target.get("attachments") or []):
        note_attachments.delete_attachment(att["s3_key"])

    remaining = [n for n in notes if n["id"] != note_id]
    j = job_store.update_job_fields(job_id, {"notes": remaining})
    j = add_audit(j, actor, "Note deleted")
    return j


@app.post("/api/jobs/{job_id}/notes/{note_id}/attachments", status_code=201)
async def add_note_attachments(
    job_id: str,
    note_id: str,
    files: List[UploadFile] = File(...),
    actor: str = Depends(get_actor),
):
    """
    Upload one or more photo/file attachments to an existing note. Any
    logged-in team member can attach to any note (not author-restricted,
    unlike delete — mirrors how anyone can read all notes today).

    This is a multipart POST and needs the same ALB OIDC bypass treatment
    as other upload routes (see compute_stack.py's UploadAndEscalateBypassRule/
    CasesBypassRule — this path is added there too) because of the
    SameSite=None cross-site cookie issue.
    """
    from . import note_attachments

    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")

    notes = list(j.get("notes") or [])
    target = next((n for n in notes if n["id"] == note_id), None)
    if not target:
        raise HTTPException(404, "Note not found")

    if len(files) > note_attachments.MAX_ATTACHMENTS_PER_UPLOAD:
        raise HTTPException(
            400,
            f"Too many files — maximum {note_attachments.MAX_ATTACHMENTS_PER_UPLOAD} per upload",
        )

    actor_name = TEAM_MEMBERS.get(actor, actor)
    new_attachments = []
    for f in files:
        content = await f.read()
        try:
            att = note_attachments.upload_attachment(
                job_id=job_id,
                note_id=note_id,
                filename=f.filename or "file",
                content_type=f.content_type,
                content=content,
                uploaded_by_email=actor,
                uploaded_by_name=actor_name,
            )
        except note_attachments.NoteAttachmentError as e:
            raise HTTPException(400, str(e))
        new_attachments.append(att)

    existing_attachments = list(target.get("attachments") or [])
    target["attachments"] = existing_attachments + new_attachments
    j = job_store.update_job_fields(job_id, {"notes": notes})
    j = add_audit(j, actor, f"{len(new_attachments)} attachment(s) added to a note")
    return j


@app.delete("/api/jobs/{job_id}/notes/{note_id}/attachments/{attachment_id}", status_code=200)
def delete_note_attachment(
    job_id: str,
    note_id: str,
    attachment_id: str,
    actor: str = Depends(get_actor),
):
    """Remove one attachment from a note (only the uploader can delete
    their own attachment, or the note's author — mirrors delete_note's
    author-only rule but also allows the person who actually uploaded the
    file, since one note can carry attachments from multiple people)."""
    from . import note_attachments

    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")

    notes = list(j.get("notes") or [])
    note = next((n for n in notes if n["id"] == note_id), None)
    if not note:
        raise HTTPException(404, "Note not found")

    attachments = list(note.get("attachments") or [])
    att = next((a for a in attachments if a["id"] == attachment_id), None)
    if not att:
        raise HTTPException(404, "Attachment not found")
    if att["uploaded_by_email"] != actor and note["author_email"] != actor:
        raise HTTPException(403, "You can only delete your own attachments")

    note_attachments.delete_attachment(att["s3_key"])
    note["attachments"] = [a for a in attachments if a["id"] != attachment_id]
    j = job_store.update_job_fields(job_id, {"notes": notes})
    j = add_audit(j, actor, "Note attachment deleted")
    return j


@app.get("/api/jobs/{job_id}/notes/{note_id}/attachments/{attachment_id}/url")
def get_note_attachment_url(
    job_id: str,
    note_id: str,
    attachment_id: str,
    actor: str = Depends(get_actor),
):
    """Return a fresh short-lived presigned S3 URL for viewing (inline
    <img>) or downloading (<a download>) one attachment. Generated on
    demand rather than baked into the job payload so it never goes
    stale/expired while a job is displayed on screen for a while."""
    from . import note_attachments

    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")

    notes = j.get("notes") or []
    note = next((n for n in notes if n["id"] == note_id), None)
    if not note:
        raise HTTPException(404, "Note not found")
    att = next((a for a in (note.get("attachments") or []) if a["id"] == attachment_id), None)
    if not att:
        raise HTTPException(404, "Attachment not found")

    try:
        url = note_attachments.generate_presigned_get_url(att["s3_key"])
    except note_attachments.NoteAttachmentError as e:
        raise HTTPException(500, str(e))
    return {"url": url, "filename": att["filename"], "content_type": att["content_type"], "is_image": att["is_image"]}


# ── Chatbot ───────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str   # "user" | "assistant"
    content: str


class ChatBody(BaseModel):
    message: str
    history: Optional[List[ChatMessage]] = []


@app.post("/api/chat")
def chat(
    body: ChatBody,
    actor: str = Depends(get_actor),
):
    """
    Chatbot endpoint — powered by GPT-6.1 Terra (Bedrock mantle) with:
      • Reasoning          : model reasons step-by-step before answering
      • Web Search         : model autonomously searches the Bedrock web index when
                             needed, returns real-time answers with source citations
      • RAG (KB + jobs)    : internal runbooks, SOPs, and past job history
    """
    user_msg = body.message.strip()
    if not user_msg:
        raise HTTPException(400, "message cannot be empty")

    # 1. Retrieve relevant KB chunks
    kb_docs = job_store.list_kb_documents()
    kb_chunks = retrieve_relevant_chunks(user_msg, kb_docs, top_k=6)
    kb_context = format_kb_context(kb_chunks) if kb_chunks else ""

    # 2. Pull relevant past jobs
    jobs = job_store.list_jobs(page=1, per_page=100)["jobs"]
    job_summaries = []
    query_lower = user_msg.lower()
    for j in jobs:
        text = f"{j['title']} {j['description']} {j['category']}".lower()
        words = [w for w in query_lower.split() if len(w) > 3]
        if words and any(w in text for w in words):
            notes_summary = ""
            if j.get("notes"):
                notes_summary = " | Notes: " + "; ".join(n["text"][:100] for n in j["notes"][:3])
            job_summaries.append(
                f"- [{j['status']}] {j['title']} ({j['category']}, {j['priority']})"
                f" — {j['description'][:150]}{notes_summary}"
            )
    past_jobs_context = (
        "=== Relevant Past Jobs ===\n" +
        "\n".join(job_summaries[:10]) +
        "\n=== End Past Jobs ==="
    ) if job_summaries else ""

    # 3. Build Converse API message list (multi-turn)
    #    Converse API requires: messages must start with a "user" turn.
    #    The frontend history may include the initial greeting (assistant role)
    #    so strip any leading assistant messages before passing to the API.
    converse_messages = []
    raw_history = (body.history or [])[-6:]   # last 3 turns
    # Drop leading assistant messages
    while raw_history and raw_history[0].role == "assistant":
        raw_history = raw_history[1:]
    for msg in raw_history:
        converse_messages.append({
            "role": msg.role,
            "content": [{"text": msg.content[:800]}],
        })
    # Append current user message (with KB + job context injected inline)
    user_turn_text = user_msg
    if kb_context or past_jobs_context:
        context_block = "\n\n".join(filter(None, [kb_context, past_jobs_context]))
        user_turn_text = (
            f"[Internal context — use this when relevant]\n{context_block}\n\n"
            f"Question: {user_msg}"
        )
    converse_messages.append({
        "role": "user",
        "content": [{"text": user_turn_text}],
    })

    # 4. System prompt (identity + instructions — separate from messages)
    actor_name = TEAM_MEMBERS.get(actor, actor)
    system_prompt = f"""You are an expert IT infrastructure assistant for {org_config.ORG_NAME}'s Cloud Infra team.
You are talking to {actor_name}.

You have three layers of knowledge:
1. Internal KB: runbooks, SOPs, and technical guides (provided inline when relevant)
2. Past job history: resolved incidents and engineer notes (provided inline when relevant)
3. Real-time web search: use the built-in web grounding tool to look up current information when the topic is not covered by the internal knowledge above, or when the user explicitly asks about latest news, current versions, or recent events.

Behaviour:
- Think step-by-step before answering complex questions.
- Always prefer internal KB/job data when it applies — reference it explicitly ("Based on the runbook..." or "A similar past job showed...").
- If you search the web, say so and include the source in your answer.
- For step-by-step procedures, use numbered lists.
- Be concise and practical — this is an ops team, not an academic audience.
- If you genuinely don't know and cannot find the answer, say so honestly.
- Specialise in: AWS, Microsoft Azure, Alibaba Cloud, Active Directory, networking, compute, storage, security."""

    # 5. Call GPT-6.1 Terra with reasoning + web search
    client = _get_client()
    try:
        answer, citations = chat_with_grounding(
            client,
            messages=converse_messages,
            system_prompt=system_prompt,
            max_tokens=1200,
            enable_thinking=True,
        )
    except Exception as e:
        _logger.error(f"chat_with_grounding failed: {e}")
        raise HTTPException(500, f"AI service unavailable: {str(e)}")

    # Append citation list to answer if web sources were used
    if citations:
        unique_urls = []
        seen = set()
        for c in citations:
            if c["url"] not in seen:
                seen.add(c["url"])
                unique_urls.append(c["url"])
        sources_text = "\n\n**Sources:**\n" + "\n".join(f"- {u}" for u in unique_urls[:5])
        answer = answer + sources_text

    return {
        "answer": answer,
        "kb_chunks_used": len(kb_chunks),
        "past_jobs_matched": len(job_summaries),
        "web_citations": len(citations),
        "model": MODEL_CHAT,
    }


# ── AI Assistant (standalone sidebar tab) ─────────────────────────────────────
# A separate, deliberately simpler chatbot from the floating widget above:
# no Knowledge Base retrieval, no past-job lookups — it never touches any
# other portal data or AWS resource, it only calls Bedrock (GPT-6.1 Terra)
# directly for a general-purpose Q&A reply. Reuses ChatMessage/ChatBody
# (identical shape) and chat_with_grounding() so this stays functionally
# consistent with the widget rather than duplicating the Bedrock call
# logic. Gated by the "ai_assistant" tab_id (settings_store.TAB_REGISTRY).

@app.post("/api/ai-assistant/chat")
def ai_assistant_chat(
    body: ChatBody,
    actor: str = Depends(get_actor),
):
    """
    Standalone AI Assistant tab — general-purpose Q&A, no RAG context
    (no Knowledge Base, no past-job history). Uses the same GPT-6.1 Terra
    model + Web Search + reasoning as the floating widget, so
    it can still answer questions about current events / documentation
    it wasn't trained on, without pulling in any internal portal data.
    """
    user_msg = body.message.strip()
    if not user_msg:
        raise HTTPException(400, "message cannot be empty")

    # Converse API requires messages to start with a "user" turn — same
    # trimming as the /api/chat widget above.
    converse_messages = []
    raw_history = (body.history or [])[-6:]
    while raw_history and raw_history[0].role == "assistant":
        raw_history = raw_history[1:]
    for msg in raw_history:
        converse_messages.append({
            "role": msg.role,
            "content": [{"text": msg.content[:800]}],
        })
    converse_messages.append({
        "role": "user",
        "content": [{"text": user_msg}],
    })

    actor_name = TEAM_MEMBERS.get(actor, actor)
    system_prompt = f"""You are a helpful, knowledgeable general-purpose AI assistant for {org_config.ORG_NAME} staff.
You are talking to {actor_name}.

Behaviour:
- Answer clearly and directly. Think step-by-step before answering complex questions.
- If the question needs current information (recent events, latest versions, etc.), use the built-in web search tool and say so, including the source.
- For step-by-step procedures, use numbered lists.
- Be concise and practical.
- If you genuinely don't know and cannot find the answer, say so honestly rather than guessing.
- You have no access to this portal's internal tickets, knowledge base, or AWS account data — if the user asks about those, tell them to use the relevant portal tab instead."""

    client = _get_client()
    try:
        answer, citations = chat_with_grounding(
            client,
            messages=converse_messages,
            system_prompt=system_prompt,
            max_tokens=1200,
            enable_thinking=True,
        )
    except Exception as e:
        _logger.error(f"ai_assistant_chat failed: {e}")
        raise HTTPException(500, f"AI service unavailable: {str(e)}")

    if citations:
        unique_urls = []
        seen = set()
        for c in citations:
            if c["url"] not in seen:
                seen.add(c["url"])
                unique_urls.append(c["url"])
        sources_text = "\n\n**Sources:**\n" + "\n".join(f"- {u}" for u in unique_urls[:5])
        answer = answer + sources_text

    return {
        "answer": answer,
        "web_citations": len(citations),
        "model": MODEL_CHAT,
    }


# ── AWS DevOps Agent (A2A integration) ────────────────────────────────────────
# Forwards questions to a dedicated AWS DevOps Agent Agent Space
# (100000000001, us-east-1: "testing-pj-devops-agent") via the A2A v1.0
# remote server. See app/devops_agent.py for the full protocol details and
# app/devops_agent.py module docstring for the setup / token prerequisites.

class DevOpsAgentChatBody(BaseModel):
    message: str


@app.post("/api/devops-agent/chat")
def devops_agent_chat(
    body: DevOpsAgentChatBody,
    actor: str = Depends(get_actor),
):
    """
    Forward a question to the AWS DevOps Agent (A2A `message:send`).

    This call is always fast (single-digit to double-digit seconds) — it
    never blocks for the full duration of a slow "chat" answer or a
    multi-minute "investigate" run. See app/devops_agent.py's module
    docstring ("Async task model") for why.

    Response shape (always all three keys):
        {"done": bool, "task_id": str, "answer": str | None}

    If done=True, `answer` has the final text and the frontend should
    display it immediately. If done=False, the frontend must poll
    GET /api/devops-agent/tasks/{task_id} until it returns done=True.

    Requires DEVOPS_AGENT_TOKEN_SECRET_NAME (and optionally
    DEVOPS_AGENT_REGION) to be configured — see app/devops_agent.py for
    setup instructions. Access-token creation is console-only (no AWS
    API), so this endpoint returns a clear 503 until that manual setup
    step is completed.
    """
    from . import devops_agent

    question = body.message.strip()
    if not question:
        raise HTTPException(400, "message cannot be empty")

    try:
        result = devops_agent.send_message(question)
    except devops_agent.DevOpsAgentError as e:
        _logger.error(f"devops_agent_chat failed for actor={actor}: {e}")
        raise HTTPException(503, e.message)

    return result


@app.get("/api/devops-agent/tasks/{task_id}")
def devops_agent_task_status(
    task_id: str,
    actor: str = Depends(get_actor),
):
    """
    Poll the status of a previously started DevOps Agent task (A2A
    `GetTask`). Used by the frontend to follow up on a
    /api/devops-agent/chat response that came back with done=False
    (e.g. an "investigate"-skill question still running).

    Response shape (same as /chat):
        {"done": bool, "task_id": str, "answer": str | None}
    """
    from . import devops_agent

    try:
        result = devops_agent.get_task_status(task_id)
    except devops_agent.DevOpsAgentError as e:
        _logger.error(f"devops_agent_task_status failed for actor={actor}, task_id={task_id}: {e}")
        raise HTTPException(503, e.message)

    return result


# ── Knowledge Base (RAG) ──────────────────────────────────────────────────────

class KBUploadResponse(BaseModel):
    id: str
    filename: str
    description: Optional[str]
    char_count: int
    chunk_count: int
    uploaded_by: str
    uploaded_at: str


@app.post("/api/kb/upload", status_code=201)
async def kb_upload(
    file: UploadFile = File(...),
    description: Optional[str] = Form(default=None),
    actor: str = Depends(get_actor),
):
    """
    Upload a document to the Knowledge Base.
    Supported: PDF, DOCX, DOC, XLSX, XLS, PPTX, PPT,
               TXT, CSV, LOG, MD, HTML, EML,
               JPG, JPEG, PNG, GIF, BMP, WEBP (max 20 MB).
    The file is chunked and stored for RAG retrieval during AI analysis.
    """
    MAX_SIZE = 20 * 1024 * 1024  # 20 MB
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File too large — maximum 20 MB")
    if len(content) == 0:
        raise HTTPException(400, "Uploaded file is empty")

    try:
        raw_text = extract_text_from_file(file.filename or "upload.bin", content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to parse file: {str(e)}")

    if not raw_text.strip():
        raise HTTPException(422, "Could not extract any text from the file")

    chunks_text = chunk_text(raw_text)
    chunks_data = [{"chunk_id": i, "text": c} for i, c in enumerate(chunks_text)]

    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "unknown"

    doc = job_store.create_kb_document(
        filename=file.filename or "unknown",
        file_type=ext,
        description=(description or "").strip()[:300] or None,
        char_count=len(raw_text),
        chunk_count=len(chunks_data),
        chunks=chunks_data,
        uploaded_by=actor,
    )

    return {
        "id": doc["id"],
        "filename": doc["filename"],
        "file_type": doc["file_type"],
        "description": doc["description"],
        "char_count": doc["char_count"],
        "chunk_count": doc["chunk_count"],
        "uploaded_by": doc["uploaded_by"],
        "uploaded_at": doc["uploaded_at"],
    }


@app.get("/api/kb/documents")
def kb_list(
    actor: str = Depends(get_actor),
):
    """List all KB documents."""
    docs = job_store.list_kb_documents()
    return [
        {
            "id": d["id"],
            "filename": d["filename"],
            "file_type": d["file_type"],
            "description": d["description"],
            "char_count": d["char_count"],
            "chunk_count": d["chunk_count"],
            "uploaded_by": d["uploaded_by"],
            "uploaded_at": d["uploaded_at"],
        }
        for d in docs
    ]


@app.delete("/api/kb/documents/{doc_id}", status_code=204)
def kb_delete(
    doc_id: str,
    actor: str = Depends(get_actor),
):
    """Delete a KB document and its chunks."""
    deleted = job_store.delete_kb_document(doc_id)
    if not deleted:
        raise HTTPException(404, "KB document not found")
    return None


# ── Feedback & Bug Report (sidebar tab) ──────────────────────────────────────
# Any logged-in user can SUBMIT either report_type. Visibility/reply
# permission then differs by report_type:
#   - "feedback": a private thread between the submitter and admins only —
#     other regular members cannot see or reply to someone else's feedback.
#   - "bug": visible to admins only — even the person who reported the bug
#     loses visibility on it after submitting (they can't see admin
#     discussion/triage of bugs), only admins can see and comment.
# Status changes remain admin-only for both types.

class CreateFeedbackBody(BaseModel):
    report_type: str            # "feedback" | "bug"
    title: str
    description: str
    severity: Optional[str] = None   # Low/Medium/High/Critical — bug reports only
    page_url: Optional[str] = None


class UpdateStatusBody(BaseModel):
    status: str        # Open/In Review/Resolved/Closed


class AddReplyBody(BaseModel):
    text: str


def _feedback_for_viewer(report: dict, is_admin: bool) -> dict:
    """The AI triage note is for reviewing engineers only — never include
    it in what a non-admin receives."""
    if is_admin or "ai_analysis" not in report:
        return report
    return {k: v for k, v in report.items() if k != "ai_analysis"}


def _ai_model_options() -> list[dict]:
    return [{"key": k, "label": v["label"]} for k, v in bug_analysis.MODELS.items()]


# An AI analysis "pending" longer than this is assumed lost (e.g. the ECS
# task running it was replaced mid-call) and may be re-requested.
_AI_PENDING_STALE_SECONDS = 180


def _run_bug_analysis(report_id: str, model_key: Optional[str]) -> None:
    """Background job: run the AI triage for one bug report and store the
    outcome (done, or failed with a safe message) on the report. Never
    raises — it runs after the HTTP response has gone out."""
    try:
        report = feedback_store.get_report(report_id)
        if not report or report.get("report_type") != "bug":
            return
        try:
            result = bug_analysis.analyse_bug_report(report, model_key)
        except bug_analysis.AnalysisError as e:
            result = bug_analysis.failed_marker(e.message, model_key)
        except Exception:
            _logger.exception("bug AI analysis crashed for report %s", report_id)
            result = bug_analysis.failed_marker("AI analysis failed unexpectedly. Please try again.", model_key)
        feedback_store.set_ai_analysis(report_id, result)
    except Exception:
        _logger.exception("could not store bug AI analysis for report %s", report_id)


def _ai_pending_is_fresh(ai: Optional[dict]) -> bool:
    if not ai or ai.get("status") != "pending":
        return False
    try:
        requested = datetime.fromisoformat(ai["requested_at"])
    except Exception:
        return False
    if requested.tzinfo is None:
        requested = requested.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - requested).total_seconds() < _AI_PENDING_STALE_SECONDS


def _feedback_visible_to(report: dict, actor: str, is_admin: bool) -> bool:
    """Visibility rule shared by list/get/reply/status endpoints."""
    if is_admin:
        return True
    if report.get("report_type") == "feedback":
        return report.get("submitted_by_email") == actor
    # "bug" — admins only, not even the original reporter.
    return False


@app.get("/api/feedback")
def list_feedback(actor: str = Depends(get_actor)):
    """List feedback/bug reports visible to the caller:
    - Admins see everything.
    - Regular users see only their own "feedback" reports (private
      submitter<->admin thread) — never other users' feedback, and never
      any "bug" reports (bug reports are admin-only, even the reporter's
      own)."""
    is_admin = user_store.is_admin(actor)
    all_reports = feedback_store.list_reports()
    visible = [_feedback_for_viewer(r, is_admin) for r in all_reports if _feedback_visible_to(r, actor, is_admin)]
    return {"reports": visible, "ai_models": _ai_model_options() if is_admin else []}


@app.post("/api/feedback", status_code=201)
def create_feedback(body: CreateFeedbackBody, background_tasks: BackgroundTasks, actor: str = Depends(get_actor)):
    """Submit a new feedback item or bug report. Any logged-in user may
    submit either type — visibility afterwards depends on report_type
    (see _feedback_visible_to). A new bug report is also queued for an AI
    triage note (bug_analysis.py), generated after the response goes out so
    submitting stays instant; only admins can see the result."""
    report_type = (body.report_type or "").strip().lower()
    if report_type not in ("feedback", "bug"):
        raise HTTPException(400, "report_type must be 'feedback' or 'bug'")
    title = body.title.strip()
    description = body.description.strip()
    if not title:
        raise HTTPException(400, "Title is required")
    if not description:
        raise HTTPException(400, "Description is required")
    if len(title) > 200:
        raise HTTPException(400, "Title must be 200 characters or fewer")
    if len(description) > 5000:
        raise HTTPException(400, "Description must be 5000 characters or fewer")
    severity = body.severity if body.severity in ("Low", "Medium", "High", "Critical") else None

    # Local dev has no Bedrock access to rely on, so only queue the AI note
    # in a real environment.
    queue_ai = report_type == "bug" and ENV != "local"
    report = feedback_store.create_report(
        report_type=report_type,
        title=title,
        description=description,
        severity=severity,
        page_url=(body.page_url or "").strip()[:300] or None,
        submitted_by_email=actor,
        submitted_by_name=TEAM_MEMBERS.get(actor, actor),
        ai_analysis=bug_analysis.pending_marker() if queue_ai else None,
    )
    if queue_ai:
        background_tasks.add_task(_run_bug_analysis, report["id"], None)
    # The submitter must not receive the AI note (bug reports are admin-only).
    return _feedback_for_viewer(report, user_store.is_admin(actor))


class AiAnalysisBody(BaseModel):
    model: Optional[str] = None   # bug_analysis.MODELS key; None = default (Claude Sonnet 5.5, retried once)


def _get_bug_report_or_404(report_id: str) -> dict:
    report = feedback_store.get_report(report_id)
    if not report:
        raise HTTPException(404, "Feedback report not found")
    if report.get("report_type") != "bug":
        raise HTTPException(400, "AI analysis is only available for bug reports")
    return report


@app.get("/api/feedback/{report_id}/ai-analysis")
def get_feedback_ai_analysis(report_id: str, actor: str = Depends(require_admin)):
    """Admin-only: the current AI triage note for a bug report (also polled
    by the UI while one is being generated)."""
    report = _get_bug_report_or_404(report_id)
    return {"ai_analysis": report.get("ai_analysis"), "ai_models": _ai_model_options()}


@app.post("/api/feedback/{report_id}/ai-analysis", status_code=202)
def request_feedback_ai_analysis(
    report_id: str,
    body: AiAnalysisBody,
    background_tasks: BackgroundTasks,
    actor: str = Depends(require_admin),
):
    """Admin-only: (re)generate the AI triage note, optionally with a chosen
    model. Runs in the background; poll GET for the result."""
    if ENV == "local":
        raise HTTPException(503, "AI analysis is not available in local mode")
    report = _get_bug_report_or_404(report_id)
    if body.model is not None and body.model not in bug_analysis.MODELS:
        raise HTTPException(400, "Unknown AI model")
    if _ai_pending_is_fresh(report.get("ai_analysis")):
        raise HTTPException(409, "An AI analysis is already running for this report")
    pending = bug_analysis.pending_marker(body.model)
    updated = feedback_store.set_ai_analysis(report_id, pending)
    if not updated:
        raise HTTPException(404, "Feedback report not found")
    background_tasks.add_task(_run_bug_analysis, report_id, body.model)
    _logger.info("bug AI analysis requested for %s by %s (model=%s)", report_id, actor, body.model or "default")
    return {"ai_analysis": pending}


@app.patch("/api/feedback/{report_id}/status")
def update_feedback_status(report_id: str, body: UpdateStatusBody, actor: str = Depends(require_admin)):
    """Admin-only: change status of a feedback/bug report."""
    if body.status not in ("Open", "In Review", "Resolved", "Closed"):
        raise HTTPException(400, "Invalid status")
    report = feedback_store.update_status(report_id, body.status)
    if not report:
        raise HTTPException(404, "Feedback report not found")
    return report


@app.post("/api/feedback/{report_id}/replies", status_code=201)
def add_feedback_reply(report_id: str, body: AddReplyBody, actor: str = Depends(get_actor)):
    """Add a reply to a report's thread.
    - "feedback": the original submitter or any admin may reply (private
      two-way conversation).
    - "bug": only admins may reply — the reporter cannot comment further
      once submitted (matches their read visibility)."""
    existing = feedback_store.get_report(report_id)
    if not existing:
        raise HTTPException(404, "Feedback report not found")
    is_admin = user_store.is_admin(actor)
    if existing.get("report_type") == "feedback":
        allowed = is_admin or existing.get("submitted_by_email") == actor
    else:  # "bug"
        allowed = is_admin
    if not allowed:
        raise HTTPException(403, "You do not have permission to reply to this report")

    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Reply text is required")
    if len(text) > 3000:
        raise HTTPException(400, "Reply must be 3000 characters or fewer")

    report = feedback_store.add_reply(
        report_id,
        author_email=actor,
        author_name=TEAM_MEMBERS.get(actor, actor),
        is_admin=is_admin,
        text=text,
    )
    if not report:
        raise HTTPException(404, "Feedback report not found")
    return report


@app.delete("/api/feedback/{report_id}", status_code=204)
def delete_feedback(report_id: str, actor: str = Depends(require_admin)):
    """Admin-only: remove a feedback/bug report (e.g. spam or duplicate)."""
    deleted = feedback_store.delete_report(report_id)
    if not deleted:
        raise HTTPException(404, "Feedback report not found")
    return None


# ── Meta ──────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Cloud Infra Job Portal"}


@app.get("/api/org-config")
def get_org_config(actor: str = Depends(get_actor)):
    """Non-secret, organisation-level settings (name, domains, key AWS account
    IDs) and which optional integrations are configured. The frontend uses this
    instead of hardcoding anything about the deploying organisation, and hides
    the menu entries whose integration is switched off (see org_config.py)."""
    return org_config.public_config(include_admin_info=user_store.is_admin(actor))


@app.get("/api/meta/options")
def meta_options():
    # team_members includes photo_data_url (straight from user_store.list_users(),
    # which already carries it) so any page can render a user's real avatar
    # next to their name wherever it shows a comment/note/reply/audit entry
    # they authored, without a separate per-user lookup call.
    return {
        "categories": VALID_CATEGORIES,
        "priorities": VALID_PRIORITIES,
        "statuses": VALID_STATUSES,
        "status_transitions": STATUS_TRANSITIONS,
        # "Sort By" filter options for the All Tickets tab — list of
        # {value, label} pairs so the frontend dropdown doesn't need its
        # own hardcoded copy of JOB_SORT_FIELDS.
        "sort_fields": [{"value": k, "label": v} for k, v in JOB_SORT_FIELDS.items()],
        "team_members": [
            {"email": u["email"], "name": u["name"], "photo_data_url": u.get("photo_data_url")}
            for u in user_store.list_users()
        ],
    }


# Everything except "Closed" counts as open/in-flight — Resolved and
# Pending Review are no longer terminal in the new flow (Closed is the
# only end state), so they must still count as open work here.
OPEN_STATUSES = [s for s in VALID_STATUSES if s != "Closed"]


@app.get("/api/dashboard/stats")
def dashboard_stats(actor: str = Depends(get_actor)):
    """
    ITSM-style KPI summary for the Dashboard landing page: open/closed
    counts, overdue-by-due-date count, breakdown by status/category, and
    the most recently updated tickets for an activity feed. All computed
    on-demand from job_store (small dataset — internal tool, not high volume)
    rather than maintained as a running aggregate. (SLA tracking was removed
    per user request — not needed for this portal right now.)
    """
    all_jobs = job_store.list_jobs(page=1, per_page=1_000_000)["jobs"]
    now = datetime.utcnow()
    today_ymd = now.date().isoformat()

    open_jobs = [j for j in all_jobs if j["status"] in OPEN_STATUSES]
    closed_jobs = [j for j in all_jobs if j["status"] not in OPEN_STATUSES]

    def _count_by(jobs: list[dict], field: str) -> dict:
        counts: dict = {}
        for j in jobs:
            key = j.get(field) or "Unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts

    overdue_due_date = [j for j in open_jobs if j.get("due_date") and j["due_date"] < today_ymd]

    recent = sorted(all_jobs, key=lambda j: j.get("updated_at") or "", reverse=True)[:8]

    resolved_this_month = [
        j for j in closed_jobs
        if j.get("updated_at") and j["updated_at"][:7] == now.strftime("%Y-%m")
    ]
    created_this_month = [
        j for j in all_jobs
        if j.get("created_at") and j["created_at"][:7] == now.strftime("%Y-%m")
    ]

    return {
        "generated_at": now.isoformat(),
        "totals": {
            "total": len(all_jobs),
            "open": len(open_jobs),
            "closed": len(closed_jobs),
            "overdue_due_date": len(overdue_due_date),
            "created_this_month": len(created_this_month),
            "resolved_this_month": len(resolved_this_month),
        },
        "by_status": _count_by(all_jobs, "status"),
        "by_priority_open": _count_by(open_jobs, "priority"),
        "by_category": _count_by(all_jobs, "category"),
        "recent_activity": [
            {"id": j["id"], "ticket_number": j.get("ticket_number"), "title": j["title"],
             "status": j["status"], "priority": j["priority"], "updated_at": j["updated_at"],
             "assignee_name": j["assignee_name"]}
            for j in recent
        ],
    }


# ── AI Preview Analysis (pre-submit) ─────────────────────────────────────────

class PreviewAnalyseBody(BaseModel):
    title: str
    description: str
    category: str
    priority: str


@app.post("/api/analyse/preview")
def analyse_preview(
    body: PreviewAnalyseBody,
    actor: str = Depends(get_actor),
):
    """
    Run AI analysis on draft job fields before the job is saved.
    Returns the same structured analysis as /api/jobs/{id}/analyse
    but does not persist anything to the database.
    """
    if not body.title.strip():
        raise HTTPException(400, "title is required")
    if not body.description.strip():
        raise HTTPException(400, "description is required")

    # Retrieve relevant KB chunks for RAG enrichment
    query = f"{body.title} {body.description} {body.category}"
    kb_docs = job_store.list_kb_documents()
    kb_chunks = retrieve_relevant_chunks(query, kb_docs, top_k=5)
    kb_context = format_kb_context(kb_chunks)

    draft = analyse_job(
        title=body.title.strip(),
        description=body.description.strip(),
        category=body.category,
        priority=body.priority,
        audit_log=[],
        kb_context=kb_context,
    )
    draft["kb_chunks_used"] = len(kb_chunks)
    return draft


# ── AI Extract from File or Email Text ───────────────────────────────────────

@app.post("/api/extract/file")
async def extract_from_file(
    file: UploadFile = File(...),
    actor: str = Depends(get_actor),
):
    """
    Upload a file to extract job fields via AI.
    Supported: PDF, DOCX, DOC, XLSX, XLS, PPTX, PPT,
               TXT, CSV, LOG, MD, HTML, EML, MSG,
               JPG, JPEG, PNG, GIF, BMP, WEBP (max 20 MB).
    Returns AI-extracted job fields: title, description, category, priority.
    """
    MAX_SIZE = 20 * 1024 * 1024  # 20 MB
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File too large — maximum 20 MB")
    if len(content) == 0:
        raise HTTPException(400, "Uploaded file is empty")

    try:
        raw_text = extract_text_from_file(file.filename or "upload.bin", content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to parse file: {str(e)}")

    if not raw_text.strip():
        raise HTTPException(422, "Could not extract any text from the file")

    # Try Bedrock-powered extraction first, fall back to local rule-based parser
    try:
        result = ai_extract_job_fields_bedrock(raw_text)
    except Exception:
        result = ai_extract_job_fields(raw_text)

    return {
        "source": "file",
        "filename": file.filename,
        **result,
    }


@app.post("/api/extract/email")
async def extract_from_email_text(
    body: dict,
    actor: str = Depends(get_actor),
):
    """
    Accept pasted email body text.
    Returns AI-extracted job fields: title, description, category, priority.
    """
    email_text = body.get("text", "").strip()
    if not email_text:
        raise HTTPException(400, "email text is required")
    if len(email_text) > 50000:
        raise HTTPException(400, "Email text too long — maximum 50,000 characters")

    try:
        result = ai_extract_job_fields_bedrock(email_text)
    except Exception:
        result = ai_extract_job_fields(email_text)

    return {
        "source": "email_text",
        **result,
    }


# ── Config Mappings (category/status <-> Freshservice) ──────────────────────
# Task 5.13: CRUD endpoints reading/writing the DynamoDB Mappings table.
# Only reachable in production (ENV != local) where DynamoDB tables exist;
# local dev continues to use SQLite and has no Mappings table to manage.

class MappingBody(BaseModel):
    mapping_type: str      # e.g. "category_to_freshservice", "priority_to_freshservice",
                            # "freshservice_to_status"
    portal_value: str
    external_value: str


@app.get("/api/config/category-mapping")
def get_category_mapping(actor: str = Depends(get_actor)):
    if ENV == "local":
        raise HTTPException(501, "Category mapping requires the production DynamoDB backend")
    from . import dynamo_repository as repo
    return repo.list_mappings("category_to_freshservice")


@app.put("/api/config/category-mapping")
def put_category_mapping(body: MappingBody, actor: str = Depends(get_actor)):
    if ENV == "local":
        raise HTTPException(501, "Category mapping requires the production DynamoDB backend")
    from . import dynamo_repository as repo
    return repo.put_mapping("category_to_freshservice", body.portal_value, body.external_value)


@app.delete("/api/config/category-mapping/{portal_value}")
def delete_category_mapping(portal_value: str, actor: str = Depends(get_actor)):
    if ENV == "local":
        raise HTTPException(501, "Category mapping requires the production DynamoDB backend")
    from . import dynamo_repository as repo
    repo.delete_mapping("category_to_freshservice", portal_value)
    return {"deleted": portal_value}


@app.get("/api/config/status-mapping")
def get_status_mapping(actor: str = Depends(get_actor)):
    if ENV == "local":
        raise HTTPException(501, "Status mapping requires the production DynamoDB backend")
    from . import dynamo_repository as repo
    return repo.list_mappings("freshservice_to_status")


@app.put("/api/config/status-mapping")
def put_status_mapping(body: MappingBody, actor: str = Depends(get_actor)):
    if ENV == "local":
        raise HTTPException(501, "Status mapping requires the production DynamoDB backend")
    from . import dynamo_repository as repo
    return repo.put_mapping("freshservice_to_status", body.portal_value, body.external_value)


@app.delete("/api/config/status-mapping/{portal_value}")
def delete_status_mapping(portal_value: str, actor: str = Depends(get_actor)):
    if ENV == "local":
        raise HTTPException(501, "Status mapping requires the production DynamoDB backend")
    from . import dynamo_repository as repo
    repo.delete_mapping("freshservice_to_status", portal_value)
    return {"deleted": portal_value}


# ── Freshservice Webhook (inbound) ───────────────────────────────────────────
# Task 5.9 / 7.7: this route is reached via the ALB listener rule for
# /api/webhooks/freshservice* which has NO authenticate-oidc action —
# authenticity comes from a static shared API key header (see
# freshservice_sync.py module docstring for why this isn't a real HMAC
# signature), not ALB/Azure AD auth. Deliberately does NOT depend on
# get_actor().
#
# Configure this exact header name + the Secrets Manager value
# (job-portal/freshservice-webhook-hmac-secret) as the "API key"
# authentication in Freshservice's Workflow Automator "Trigger Webhook"
# action for this workflow.

@app.post("/api/webhooks/freshservice", status_code=200)
async def freshservice_webhook(request: Request):
    if ENV == "local":
        raise HTTPException(501, "Freshservice webhook requires the production DynamoDB backend")

    from . import freshservice_sync

    raw_body = await request.body()
    signature = request.headers.get("x-freshservice-signature", "")

    try:
        result = freshservice_sync.verify_and_handle_webhook(raw_body, signature)
    except freshservice_sync.WebhookSignatureError:
        raise HTTPException(401, "Invalid webhook signature")
    except freshservice_sync.WebhookPayloadError as e:
        raise HTTPException(400, str(e))
    except freshservice_sync.WebhookMappingError as e:
        raise HTTPException(422, str(e))

    return result


# ── Freshservice Change Calendar (external tool) — proxy routes ─────────────
# These forward to the separate Freshservice Change Calendar app's API
# (see app/fs_calendar.py). The portal's own Azure-AD-authenticated users
# call these two routes; the actual Freshservice Calendar API key only
# ever exists server-side (env var locally, Secrets Manager in
# production) — never sent to the browser or committed to source control.

@app.get("/api/fs-calendar/dashboard")
def fs_calendar_dashboard(
    year: int,
    month: int,
    refresh: bool = False,
    actor: str = Depends(get_actor),
):
    try:
        return fs_calendar.get_dashboard(year, month, force_refresh=refresh)
    except fs_calendar.FsCalendarError as e:
        raise HTTPException(502, e.message)


@app.get("/api/fs-calendar/changes")
def fs_calendar_changes(
    year: int,
    month: int,
    refresh: bool = False,
    actor: str = Depends(get_actor),
):
    try:
        return fs_calendar.get_changes(year, month, force_refresh=refresh)
    except fs_calendar.FsCalendarError as e:
        raise HTTPException(502, e.message)


# ── Security Team Portal (external tool) — proxy route ──────────────────────
# Forwards to the Security Team's read-only /external-api/* (see
# app/security_portal.py). The data is security-sensitive (findings, host
# inventory, installed software), so unlike the calendar proxy above this
# is NOT open to every logged-in user: server-side it mirrors who can SEE
# the "security_team_portal" sidebar tab — admins, plus members of any User
# Group granted that tab (settings_store.tabs_granted_by_groups) — so a
# user who types the URL directly gets the same answer as the sidebar.

def require_security_portal_viewer(actor: str = Depends(get_actor)) -> str:
    if user_store.is_admin(actor):
        return actor
    granted = settings_store.tabs_granted_by_groups(user_store.get_user_groups(actor))
    if "security_team_portal" not in (granted | settings_store.EVERYONE_TAB_IDS):
        raise HTTPException(403, "Not authorized to view the Security Team Portal")
    return actor


@app.get("/api/security-portal/{resource:path}")
def security_portal_resource(
    resource: str,
    refresh: bool = False,
    actor: str = Depends(require_security_portal_viewer),
):
    try:
        return security_portal.get_resource(resource, force_refresh=refresh)
    except security_portal.SecurityPortalError as e:
        raise HTTPException(e.status_code, e.message)


# ── Worldwide Public Holidays (Calendars > Worldwide Public Holidays) ───────
# Reads the public holidays-calendar.net pages server-side (see
# app/world_holidays.py). No credentials involved; results are cached for
# 24 hours per region/year so page loads never fan out to the source site.

@app.get("/api/world-holidays/regions")
def world_holidays_regions(actor: str = Depends(get_actor)):
    return {"regions": world_holidays.list_regions()}


@app.get("/api/world-holidays")
def world_holidays_list(
    year: int,
    regions: str,
    refresh: bool = False,
    actor: str = Depends(get_actor),
):
    keys = [r.strip() for r in regions.split(",") if r.strip()]
    try:
        return world_holidays.get_holidays(year, keys, force_refresh=refresh)
    except world_holidays.WorldHolidaysError as e:
        raise HTTPException(e.status, e.message)


# ── SR Ticket Information (external tool) — proxy route ─────────────────────
# Forwards to the separate SR Ticket Information system's API. The
# portal's own Azure-AD-authenticated users call this route; the actual
# system's username/password only ever exist server-side (env var
# locally, Secrets Manager in production) — never sent to the browser or
# committed to source control. Mirrors the Freshservice Change Calendar
# proxy pattern above (see app/sr_ticket.py).

@app.get("/api/sr-ticket/tickets")
def sr_ticket_tickets(
    refresh: bool = False,
    start: Optional[str] = None,
    end: Optional[str] = None,
    service_item: Optional[str] = None,
    workspace: Optional[str] = None,
    requester: Optional[str] = None,
    approval_status: Optional[str] = None,
    request_status: Optional[str] = None,
    outstanding_only: bool = False,
    actor: str = Depends(get_actor),
):
    """Proxies the ITSM Support Portal's "Service Item Report" (SR
    tickets). Defaults to the trailing ~32 days with no filters when no
    query params are given, matching that app's own default view."""
    filters = {
        "start": start,
        "end": end,
        "serviceItem": service_item,
        "workspace": workspace,
        "requester": requester,
        "approvalStatus": approval_status,
        "requestStatus": request_status,
        "outstandingOnly": outstanding_only,
    }
    try:
        return sr_ticket.get_tickets(force_refresh=refresh, filters=filters)
    except sr_ticket.SrTicketError as e:
        raise HTTPException(502, e.message)


# ── Chat/Call (job-portal-chat-voice spec) ───────────────────────────────────
# All routes below are gated only by Depends(get_actor) (any authenticated
# Team_Member) — Conversation/Call-specific access is enforced per-route via
# an explicit participant check against chat_store.get_conversation(),
# matching every other module's "look up the parent, then check membership"
# convention (see e.g. delete_note_attachment above for the note/job analog).

from . import chat_store
from . import call_store
from . import chat_attachments
from . import chime_client


def _require_conversation_participant(conversation_id: str, actor: str) -> dict:
    """Shared helper: 404 if the Conversation doesn't exist, 403 if the
    caller is not a current participant. Returns the conversation dict
    on success — every Chat/Call route below that operates on an
    existing conversation_id starts with this call."""
    conversation = chat_store.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    if actor.lower().strip() not in {p.lower().strip() for p in (conversation.get("participants") or [])}:
        raise HTTPException(403, "You are not a participant of this conversation")
    return conversation


class CreateConversationBody(BaseModel):
    type: str                              # "direct" | "group"
    participant_email: Optional[str] = None      # required for type="direct"
    name: Optional[str] = None                    # required for type="group" (1-100 chars)
    participant_emails: Optional[list[str]] = None  # required for type="group"


@app.get("/api/chat/conversations")
def list_chat_conversations(actor: str = Depends(get_actor)):
    """Requirement 1.5: only Conversations the caller currently
    participates in, most-recent-activity-first (chat_store.py's own
    sort order)."""
    return {"conversations": chat_store.list_conversations_for_user(actor)}


@app.post("/api/chat/conversations", status_code=201)
def create_chat_conversation(body: CreateConversationBody, actor: str = Depends(get_actor)):
    """Requirement 1.1/1.2/1.3: direct conversations dedup automatically
    (chat_store.create_direct_conversation() reopens an existing one
    instead of duplicating); group conversations require a 1-100
    character name and at least two OTHER participants besides the
    creating owner (three-or-more total, per the Group_Conversation
    glossary definition)."""
    if body.type == "direct":
        if not body.participant_email:
            raise HTTPException(400, "participant_email is required for a direct conversation")
        other = body.participant_email.lower().strip()
        if not other or "@" not in other:
            raise HTTPException(400, "A valid participant_email is required")
        if other == actor.lower().strip():
            raise HTTPException(400, "Cannot start a direct conversation with yourself")
        if not user_store.is_allowed(other):
            raise HTTPException(404, "That user was not found")
        return chat_store.create_direct_conversation(actor, other)

    if body.type == "group":
        name = (body.name or "").strip()
        if not (1 <= len(name) <= 100):
            raise HTTPException(400, "Group name must be 1-100 characters")
        others = sorted({e.lower().strip() for e in (body.participant_emails or []) if e.strip()})
        others = [e for e in others if e != actor.lower().strip()]
        if len(others) < 2:
            raise HTTPException(400, "A group conversation requires at least two other participants")
        unknown = [e for e in others if not user_store.is_allowed(e)]
        if unknown:
            raise HTTPException(404, f"Unknown user(s): {', '.join(unknown)}")
        return chat_store.create_group_conversation(name=name, owner_email=actor, participant_emails=others)

    raise HTTPException(400, "type must be 'direct' or 'group'")


class UpdateConversationBody(BaseModel):
    name: Optional[str] = None
    add_participant_emails: Optional[list[str]] = None
    remove_participant_emails: Optional[list[str]] = None


@app.patch("/api/chat/conversations/{conversation_id}")
def update_chat_conversation(conversation_id: str, body: UpdateConversationBody, actor: str = Depends(get_actor)):
    """Requirement 1.4: rename/add/remove participants — group
    conversations only, owner only."""
    conversation = _require_conversation_participant(conversation_id, actor)
    if conversation["type"] != "group":
        raise HTTPException(400, "Only group conversations can be renamed or have participants managed")
    if conversation["owner_email"] != actor.lower().strip():
        raise HTTPException(403, "Only the group owner can rename or manage participants")

    updated = conversation
    if body.name is not None:
        name = body.name.strip()
        if not (1 <= len(name) <= 100):
            raise HTTPException(400, "Group name must be 1-100 characters")
        updated = chat_store.rename_group(conversation_id, name) or updated
    for email in (body.add_participant_emails or []):
        email = email.lower().strip()
        if not user_store.is_allowed(email):
            raise HTTPException(404, f"Unknown user: {email}")
        updated = chat_store.add_participant(conversation_id, email) or updated
    for email in (body.remove_participant_emails or []):
        updated = chat_store.remove_participant(conversation_id, email.lower().strip()) or updated
    return updated


@app.get("/api/chat/conversations/{conversation_id}/messages")
def list_chat_messages(
    conversation_id: str,
    limit: int = 100,
    before_message_id: Optional[str] = None,
    actor: str = Depends(get_actor),
):
    """Requirement 2.4: ascending chronological order, paginated
    backwards from before_message_id if given."""
    _require_conversation_participant(conversation_id, actor)
    return {"messages": chat_store.list_messages(conversation_id, limit=limit, before_message_id=before_message_id)}


class MarkMessagesReadBody(BaseModel):
    message_ids: list[str]


@app.post("/api/chat/conversations/{conversation_id}/messages/read")
def mark_chat_messages_read(conversation_id: str, body: MarkMessagesReadBody, actor: str = Depends(get_actor)):
    """Requirement 2.5: mark the given Messages (in this Conversation)
    as read by the caller, updating each Message's `read_by` map —
    real-time delivery of the resulting Read_Receipt state to the
    sender reuses the exact same DynamoDB Streams fan-out as a new/
    edited Message (chat_store.mark_read() writes to the same
    ChatMessagesTable rows, so the Streams poller picks up the change
    and tags it "message.edited" — see ws_manager.py's own comment on
    why any non-INSERT stream event on this table is classified that
    way; the pushed payload's `data.read_by` is what the frontend
    actually cares about, not the event_type label itself).

    No-op (not an error) for a message_id that doesn't exist or isn't
    in this Conversation — chat_store.mark_read() silently skips rows
    it can't find, matching the "opening a Conversation" UX this exists
    for: the caller (ChatContext.jsx) doesn't need to first verify which
    of the visible messages are already read before calling this."""
    _require_conversation_participant(conversation_id, actor)
    if not body.message_ids:
        return {"status": "ok"}
    chat_store.mark_read(conversation_id, body.message_ids, reader_email=actor)
    return {"status": "ok"}


class SendMessageBody(BaseModel):
    content: Optional[str] = None
    attachment: Optional[dict] = None


@app.post("/api/chat/conversations/{conversation_id}/messages", status_code=201)
def send_chat_message(conversation_id: str, body: SendMessageBody, actor: str = Depends(get_actor)):
    """Requirement 2.1/2.7: send a text and/or attachment-referencing
    Message. New/edited-message real-time delivery is handled entirely
    by ws_manager.py's DynamoDB Streams fan-out (Task 5/10) — this route
    only needs to write the row."""
    _require_conversation_participant(conversation_id, actor)
    try:
        return chat_store.send_message(conversation_id, sender_email=actor, content=body.content, attachment=body.attachment)
    except chat_store.ChatStoreError as e:
        raise HTTPException(400, str(e))


class EditMessageBody(BaseModel):
    content: str


@app.patch("/api/chat/messages/{message_id}")
def edit_chat_message(message_id: str, body: EditMessageBody, conversation_id: str, actor: str = Depends(get_actor)):
    """Requirement 3.1/3.4-3.7: edit within the server-enforced
    10-minute Edit_Window, sender only. conversation_id is required as
    a query param — the production DynamoDB branch's edit_message()
    needs it as the table's partition key (see chat_store.py); the
    caller is expected to already have it from the message list."""
    _require_conversation_participant(conversation_id, actor)
    try:
        if ENV == "local":
            return chat_store.edit_message(message_id, editor_email=actor, new_content=body.content)
        return chat_store.edit_message(message_id, editor_email=actor, new_content=body.content, conversation_id=conversation_id)
    except chat_store.NotMessageSenderError as e:
        raise HTTPException(403, str(e))
    except chat_store.EditWindowExpiredError as e:
        raise HTTPException(400, str(e))
    except chat_store.ChatStoreError as e:
        raise HTTPException(400, str(e))


class PresignAttachmentBody(BaseModel):
    message_id: str
    filename: str
    content_type: Optional[str] = None
    size_bytes: int


@app.post("/api/chat/conversations/{conversation_id}/attachments/presign")
def presign_chat_attachment(conversation_id: str, body: PresignAttachmentBody, actor: str = Depends(get_actor)):
    """Requirement 4.1-4.3/9.5: presigned PUT for a file/photo attachment
    — participant check happens HERE (chat_attachments.py's
    generate_presigned_put_url() deliberately has no request context of
    its own, see that module's docstring)."""
    _require_conversation_participant(conversation_id, actor)
    try:
        return chat_attachments.generate_presigned_put_url(
            conversation_id=conversation_id,
            message_id=body.message_id,
            filename=body.filename,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
        )
    except chat_attachments.ChatAttachmentError as e:
        raise HTTPException(400, str(e))


@app.get("/api/chat/attachments/{s3_key:path}/url")
def get_chat_attachment_url(s3_key: str, conversation_id: str, actor: str = Depends(get_actor)):
    """Requirement 4.7/9.5: fresh presigned GET URL, regenerated on
    every call. conversation_id (query param) is required because
    chat_attachments.generate_presigned_get_url() needs it to look up
    the owning Conversation's participant list — s3_key alone (path
    param, using the `:path` converter so it can contain `/`) is not
    enough to derive that without an extra round-trip."""
    try:
        url = chat_attachments.generate_presigned_get_url(s3_key=s3_key, conversation_id=conversation_id, requester_email=actor)
    except chat_attachments.ChatAttachmentError as e:
        raise HTTPException(403, str(e))
    return {"url": url}


class CreateCallBody(BaseModel):
    conversation_id: str
    callee_emails: Optional[list[str]] = None   # omit to ring every other current participant


@app.post("/api/calls", status_code=201)
def create_call_route(body: CreateCallBody, actor: str = Depends(get_actor)):
    """Requirement 5.1/6.1: initiate a Direct_Call or Group_Call.
    call_type is derived from the parent conversation's type — a
    Direct_Conversation always produces a "direct" call to its one
    other participant; a Group_Conversation produces a "group" call
    (defaulting to ringing every OTHER current participant if
    callee_emails is omitted, per the "Start Group Call" button's
    intended one-click behaviour — Task 15.5)."""
    conversation = _require_conversation_participant(body.conversation_id, actor)
    other_participants = [p for p in conversation["participants"] if p != actor.lower().strip()]
    if conversation["type"] == "direct":
        call_type = "direct"
        callees = other_participants
    else:
        call_type = "group"
        if body.callee_emails:
            requested = {e.lower().strip() for e in body.callee_emails}
            invalid = requested - set(other_participants)
            if invalid:
                raise HTTPException(400, f"Not a participant of this conversation: {', '.join(sorted(invalid))}")
            callees = sorted(requested)
        else:
            callees = other_participants
    try:
        return call_store.create_call(conversation_id=body.conversation_id, caller_email=actor, callee_emails=callees, call_type=call_type)
    except call_store.CallStoreError as e:
        raise HTTPException(400, str(e))
    except chime_client.ChimeClientError as e:
        raise HTTPException(502, e.message)


def _require_call(call_id: str) -> dict:
    call = call_store.get_call(call_id)
    if not call:
        raise HTTPException(404, "Call not found")
    return call


@app.post("/api/calls/{call_id}/accept")
def accept_call_route(call_id: str, actor: str = Depends(get_actor)):
    """Requirement 5.2: joins only the accepting Callee's own
    audio/video — the response's chime fields are exactly what the
    frontend's amazon-chime-sdk-js MeetingSession is constructed from
    (Task 15.3)."""
    _require_call(call_id)
    try:
        return call_store.accept_call(call_id, callee_email=actor)
    except call_store.CallNotActiveError as e:
        raise HTTPException(400, str(e))
    except call_store.CallStoreError as e:
        raise HTTPException(400, str(e))
    except chime_client.ChimeClientError as e:
        raise HTTPException(502, e.message)


@app.post("/api/calls/{call_id}/decline")
def decline_call_route(call_id: str, actor: str = Depends(get_actor)):
    """Requirement 6.3: one Callee declining never affects any other
    Callee's ringing/joined state."""
    _require_call(call_id)
    try:
        return call_store.decline_call(call_id, callee_email=actor)
    except call_store.CallStoreError as e:
        raise HTTPException(400, str(e))
    except chime_client.ChimeClientError as e:
        raise HTTPException(502, e.message)


@app.post("/api/calls/{call_id}/cancel")
def cancel_call_route(call_id: str, actor: str = Depends(get_actor)):
    """Caller cancels while still ringing (nobody has accepted yet)."""
    _require_call(call_id)
    try:
        return call_store.cancel_call(call_id, caller_email=actor)
    except call_store.CallStoreError as e:
        raise HTTPException(400, str(e))
    except chime_client.ChimeClientError as e:
        raise HTTPException(502, e.message)


@app.post("/api/calls/{call_id}/leave")
def leave_call_route(call_id: str, actor: str = Depends(get_actor)):
    """Requirement 6.5/6.6: any joined participant may leave without
    ending the call for others; the last leaver triggers DeleteMeeting
    (handled entirely inside call_store.leave_call())."""
    _require_call(call_id)
    try:
        return call_store.leave_call(call_id, participant_email=actor)
    except call_store.CallStoreError as e:
        raise HTTPException(400, str(e))
    except chime_client.ChimeClientError as e:
        raise HTTPException(502, e.message)


class InviteToCallBody(BaseModel):
    email: str


@app.post("/api/calls/{call_id}/invite")
def invite_to_call_route(call_id: str, body: InviteToCallBody, actor: str = Depends(get_actor)):
    """Requirement 6.7: any current participant of an active
    Group_Call may invite additional Group_Conversation members, who
    then go through the same Ringing_State/Accept/Decline flow as an
    initial invitation."""
    call = _require_call(call_id)
    caller_norm = actor.lower().strip()
    if caller_norm != call["caller_email"] and caller_norm not in (call.get("participants") or {}):
        raise HTTPException(403, "You must be a current participant of this call to invite others")
    conversation = chat_store.get_conversation(call["conversation_id"])
    if not conversation or body.email.lower().strip() not in {p.lower().strip() for p in (conversation.get("participants") or [])}:
        raise HTTPException(400, "That user is not a participant of the parent conversation")
    try:
        return call_store.invite_to_active_call(call_id, new_callee_email=body.email)
    except call_store.CallStoreError as e:
        raise HTTPException(400, str(e))
    except chime_client.ChimeClientError as e:
        raise HTTPException(502, e.message)


@app.get("/api/chat/conversations/{conversation_id}/calls")
def list_conversation_call_history(conversation_id: str, actor: str = Depends(get_actor)):
    """Requirement 9.3/11.3/11.4: caller must be a current Conversation
    participant (checked here) — call_store.list_call_history() then
    additionally scopes results to calls the requester was actually
    part of (see that function's docstring)."""
    _require_conversation_participant(conversation_id, actor)
    return {"calls": call_store.list_call_history(conversation_id, requester_email=actor)}


# ── WebSocket route (job-portal-chat-voice Task 10) ──────────────────────────
# Single multiplexed connection per Team_Member client carrying every
# real-time event (new/edited Message, read receipt, Presence change,
# Call ring/roster/accept/decline/timeout), each JSON payload tagged
# with an "event_type" field — see design.md's WebSocket route section.
#
# Outbound push is handled entirely by ws_manager.py's DynamoDB Streams
# poller/dispatcher (started from startup() above); THIS route's own
# job is: authenticate the handshake, register/heartbeat/unregister the
# connection, and hold the actual WebSocket object open so
# connection_manager.send_to_local_connections() has something to push
# to. It does not need to manually forward anything on writes it
# receives from ITS OWN client — any REST route the client also calls
# (send message, accept call, etc.) already triggers a DynamoDB write
# that the Streams poller will fan out, including back to this same
# connection if it's a listed recipient.
#
# Task 10.4 (CRITICAL, unresolved until manually verified in a deployed
# environment): design.md's decision is to route /ws/* through the
# ALB's DEFAULT authenticate-oidc listener action (compute_stack.py's
# new priority-11 rule, Task 2.5 — already deployed) and expect the ALB
# to forward x-amzn-oidc-data on the WebSocket upgrade handshake itself,
# reusing auth_alb.verify_alb_oidc_token()/_extract_email() UNMODIFIED
# by reading the header from websocket.headers directly (Starlette
# exposes it the same way as a normal HTTP request's headers). This is
# NOT yet empirically confirmed to work for a real ALB — Task 10.4
# requires a manual test against a real browser session with an
# already-established OIDC cookie before this can be considered
# validated. If that test shows the ALB does NOT forward the header on
# the WS upgrade, the documented fallback (client re-fetches
# /api/auth/whoami over plain HTTPS first to refresh the OIDC cookie,
# then retries the WS connection once) is NOT YET implemented here —
# deferred until Task 10.4's real-world result is known, per that
# task's own wording ("if this does not hold, implement the documented
# fallback").


def _authenticate_ws(websocket: WebSocket) -> Optional[str]:
    """Returns the authenticated Team_Member's email, or None if
    authentication fails (caller closes the connection with WS_1008
    POLICY_VIOLATION — never accepts first and rejects after, so no
    unauthenticated client ever receives a successful handshake).

    ENV=local: reads an `x_actor_email` QUERY PARAMETER (not a header —
    the browser WebSocket() constructor cannot set custom request
    headers, unlike a normal fetch()/XHR call, so this cannot mirror
    get_actor_local()'s X-Actor-Email header exactly; a query param is
    the closest local-dev equivalent and is validated against the same
    user_store allowlist).

    ENV!=local: reads x-amzn-oidc-data from websocket.headers and
    verifies it via auth_alb.verify_alb_oidc_token()/_extract_email()
    UNMODIFIED (Task 10.1's literal instruction) — see the module-level
    comment above this route for the Task 10.4 caveat this depends on.
    """
    if ENV == "local":
        email = (websocket.query_params.get("x_actor_email") or "").lower().strip()
        if email and user_store.is_allowed(email):
            return email
        return None

    from . import auth_alb

    token = websocket.headers.get("x-amzn-oidc-data")
    if not token:
        return None
    try:
        claims = auth_alb.verify_alb_oidc_token(token)
    except auth_alb.AuthAlbError as e:
        auth_alb.logger.warning("ws: token verification failed: %s", e)
        return None
    email = auth_alb._extract_email(claims)
    if not email or not user_store.is_allowed(email):
        return None
    return email


@app.websocket("/ws")
async def websocket_route(websocket: WebSocket):
    from . import ws_manager, ws_connections

    email = _authenticate_ws(websocket)
    if not email:
        # Reject BEFORE accept() — the handshake itself fails, matching
        # get_actor's "never accept, then reject" 401 semantics for the
        # REST routes. 1008 = Policy Violation, the closest standard WS
        # close code to "authentication/authorisation failed".
        await websocket.close(code=1008)
        return

    await websocket.accept()
    connection = ws_connections.register_connection(user_email=email)
    connection_id = connection["connection_id"]
    ws_manager.connection_manager.register(connection_id, websocket)

    # Requirement 8.1/8.4: tell every OTHER client this task locally
    # holds that `email` just came online, without waiting for them to
    # notice via their own next directory re-fetch. Deliberately
    # LOCAL-ONLY (send_to_local_connections against
    # connection_manager.local_connection_ids(), never a cross-task
    # broadcast) — there is no cross-task pub/sub broker for this event
    # type (design.md's fan-out design only covers the `messages` and
    # `call_sessions` DynamoDB Streams; adding a third table+stream
    # purely for presence would be a materially larger infra change
    # than this task's frontend-context scope). A user on a different
    # ECS task's connection still converges on the correct state, just
    # via list_users_directory()'s own live get_presence_state() lookup
    # on that task's next periodic re-fetch (see ChatContext.jsx),
    # rather than an instant push — an acceptable trade-off given
    # Requirement 8.1's 5-second budget is itself just a UX target, not
    # a hard real-time guarantee, and this LOCAL broadcast still covers
    # the (in practice, likely majority) case of two Team_Members whose
    # connections happen to land on the same task.
    await ws_manager.connection_manager.send_to_local_connections(
        ws_manager.connection_manager.local_connection_ids() - {connection_id},
        {"event_type": "presence.update", "data": {"email": email, "presence": _get_presence_for_email(email)}},
    )

    try:
        while True:
            # Task 10.3: the only INBOUND message this route expects
            # from the client is a lightweight 15-second heartbeat ping
            # (design.md / ws_connections.HEARTBEAT_INTERVAL_SECONDS) —
            # any JSON body is accepted and its content is ignored
            # beyond refreshing the connection's staleness clock; all
            # actual chat/call ACTIONS go through the REST routes above,
            # never through this socket, per design.md's WS route being
            # multiplex-OUTBOUND-only.
            await websocket.receive_text()
            ws_connections.heartbeat(connection_id)
    except WebSocketDisconnect:
        pass
    finally:
        ws_connections.unregister_connection(connection_id)
        ws_manager.connection_manager.unregister(connection_id)
        # Requirement 8.2: mirror of the online broadcast above — tell
        # this task's other locally-held connections that `email` just
        # went offline. Uses _get_presence_for_email() (not a hardcoded
        # "offline") because unregister_connection() above already
        # removed this row, so if `email` still has another fresh
        # connection open (e.g. a second browser tab) the correct
        # broadcast value is still "online"/"in_call", not "offline".
        try:
            await ws_manager.connection_manager.send_to_local_connections(
                ws_manager.connection_manager.local_connection_ids(),
                {"event_type": "presence.update", "data": {"email": email, "presence": _get_presence_for_email(email)}},
            )
        except Exception:
            pass


# ── Global Exception Handler ─────────────────────────────────────────────────
# Task 5.11: log full detail server-side, return a generic body to the client
# (Requirement 9.6/9.8: never leak stack traces or internal names to callers).

from starlette.requests import Request as _StarletteRequest
from starlette.responses import JSONResponse as _JSONResponse
import logging as _logging

_logger = _logging.getLogger("job_portal.unhandled")


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: _StarletteRequest, exc: Exception):
    _logger.exception(
        "Unhandled exception on %s %s: %s", request.method, request.url.path, exc
    )
    return _JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
