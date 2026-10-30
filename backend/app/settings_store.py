"""
Generic single-row-per-key portal settings, managed via the Admin page.

Currently backs exactly one setting — the site-wide "Production Freeze"
yellow banner (key FREEZE_NOTICE_KEY) — so an admin can edit its message
and toggle it visible/hidden without a code deploy. Designed as a small
generic key/value store (not a dedicated "freeze notice" table) so future
admin-editable site settings can reuse the same get/set functions instead
of each needing their own table + store module.

ENV=local: SQLite via SQLAlchemy (PortalSetting model in database.py).
ENV!=local: DynamoDB table job-portal-settings, partition key
`setting_key` (tiny, low-volume, admin-curated table — a handful of rows
at most).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

ENV = os.environ.get("ENV", "local")

FREEZE_NOTICE_KEY = "freeze_notice"

# Fallback shown if no admin has ever configured this setting yet (first
# boot / fresh table) — preserves the original hardcoded banner content
# so nothing regresses for existing users until an admin edits it.
FREEZE_NOTICE_DEFAULT = {
    "enabled": False,
    "message": (
        "\u2744\ufe0f **Upcoming Production Freeze Periods** \u2014 please plan deployments/releases outside these windows. "
        "**Example freeze:** 18 Dec \u2013 10 Jan\n"
        "Scope: list the affected systems here. Exception requests (e.g. production hotfixes) must follow your "
        "Emergency Change Procedure and obtain approval before implementation."
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if ENV == "local":
    from .database import SessionLocal, PortalSetting as _SettingModel

    def get_setting(key: str, default: Optional[dict] = None) -> dict:
        db = SessionLocal()
        try:
            row = db.query(_SettingModel).filter(_SettingModel.setting_key == key).first()
            if not row:
                return dict(default) if default is not None else {}
            return {
                "value": row.value,
                "updated_by_email": row.updated_by_email,
                "updated_by_name": row.updated_by_name,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        finally:
            db.close()

    def set_setting(key: str, value: dict, *, actor_email: str, actor_name: str) -> dict:
        db = SessionLocal()
        try:
            row = db.query(_SettingModel).filter(_SettingModel.setting_key == key).first()
            if row:
                row.value = value
            else:
                row = _SettingModel(setting_key=key, value=value)
                db.add(row)
            row.updated_by_email = actor_email
            row.updated_by_name = actor_name
            db.commit()
            db.refresh(row)
            return {
                "value": row.value,
                "updated_by_email": row.updated_by_email,
                "updated_by_name": row.updated_by_name,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        finally:
            db.close()

else:
    import boto3

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    TABLE_NAME = os.environ.get("SETTINGS_TABLE_NAME", "job-portal-settings")

    def _table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)

    def get_setting(key: str, default: Optional[dict] = None) -> dict:
        resp = _table().get_item(Key={"setting_key": key})
        item = resp.get("Item")
        if not item:
            return dict(default) if default is not None else {}
        return {
            "value": item.get("value", {}),
            "updated_by_email": item.get("updated_by_email"),
            "updated_by_name": item.get("updated_by_name"),
            "updated_at": item.get("updated_at"),
        }

    def set_setting(key: str, value: dict, *, actor_email: str, actor_name: str) -> dict:
        item = {
            "setting_key": key,
            "value": value,
            "updated_by_email": actor_email,
            "updated_by_name": actor_name,
            "updated_at": _now_iso(),
        }
        _table().put_item(Item=item)
        return item


def get_freeze_notice() -> dict:
    """Public-facing shape consumed by the frontend banner — always
    returns {enabled, message, updated_by_name, updated_at}, falling back
    to FREEZE_NOTICE_DEFAULT if no admin has configured it yet."""
    stored = get_setting(FREEZE_NOTICE_KEY)
    value = stored.get("value") if stored else None
    if not value:
        value = FREEZE_NOTICE_DEFAULT
    return {
        "enabled": bool(value.get("enabled", True)),
        "message": value.get("message", FREEZE_NOTICE_DEFAULT["message"]),
        "updated_by_name": stored.get("updated_by_name") if stored else None,
        "updated_at": stored.get("updated_at") if stored else None,
    }


def set_freeze_notice(*, enabled: bool, message: str, actor_email: str, actor_name: str) -> dict:
    set_setting(
        FREEZE_NOTICE_KEY,
        {"enabled": enabled, "message": message},
        actor_email=actor_email,
        actor_name=actor_name,
    )
    return get_freeze_notice()


# ── Sidebar Tab Registry ──────────────────────────────────────────────────
# Canonical registry of every sidebar tab/page whose visibility is gated
# by User Group membership (see "User Groups & Category Access" below —
# this is now the ONLY access-control layer; the previous per-role Tab
# Access Control matrix has been removed entirely per user request). The
# Admin page/settings section itself is deliberately NOT included here —
# it is already gated separately via user_store.is_admin(), and keeping
# it out of this registry guarantees an admin can never lock themselves
# (or every admin) out of the one page that could fix a bad group
# configuration.
TAB_REGISTRY = [
    {"id": "dashboard", "label": "Dashboard"},
    {"id": "jobs", "label": "All Tickets"},
    {"id": "jobs_new", "label": "New Ticket"},
    {"id": "kb", "label": "Knowledge Base"},
    {"id": "calendars", "label": "Calendars"},
    {"id": "aws_resources", "label": "AWS Resources"},
    {"id": "cases", "label": "All Cases"},
    {"id": "health_dashboard", "label": "AWS Health Dashboard"},
    {"id": "cost_analysis", "label": "Cost Analysis"},
    {"id": "billing_by_service", "label": "Billing by Service"},
    {"id": "ri_sp_savings", "label": "RI & Savings Plan Saved Cost"},
    {"id": "cmdb", "label": "CMDB"},
    # VPN Profile Lookup (see app/vpn_lookup.py) — surfaces Site-to-Site
    # VPN tunnel details INCLUDING plaintext pre-shared keys for the
    # network account (100000000003), given a site's WAN IP. Defaults to
    # visible for everyone like every other Resources tab, but this is
    # the one tab in that section whose data is genuinely sensitive
    # (IPsec PSKs) — restrict this specific tab_id via the Admin page if
    # PSKs should only be visible to the network/infra team.
    {"id": "vpn_lookup", "label": "VPN Profile Lookup"},
    {"id": "feedback", "label": "Feedback & Bug Report"},
    {"id": "security_team_portal", "label": "Security Team Portal"},
    {"id": "security_hub", "label": "AWS Security Hub"},
    {"id": "waf_monitoring", "label": "WAF Rule Monitoring"},
    # SR Ticket Information (see app/sr_ticket.py) — proxies a separate
    # internal SR ticketing system. Its username/password are embedded
    # server-side only (AWS Secrets Manager "job-portal/sr-ticket-credentials"),
    # so portal users never see a second login prompt, mirroring the
    # Freshservice Change Calendar tab's proxy pattern.
    {"id": "sr_ticket_information", "label": "SR Ticket Information"},
    # Licensing (see frontend/src/pages/LicensingPage.jsx) — an
    # "open in new tab" link to the separate Azure License Dashboard.
    # Uses the SAME Azure AD tenant as this portal's own SSO, so users
    # aren't prompted to sign in again, but is NOT embedded via <iframe>
    # (Azure AD's sign-in flow refuses to run inside a frame — same
    # class of issue documented in SecurityTeamPortalPage.jsx for a
    # different external portal).
    {"id": "licensing", "label": "Licensing"},
    {"id": "helpdesk_team_s3_screensaver", "label": "Helpdesk Team - S3-ScreenSaver"},
    # Standalone general-purpose AI chatbot tab (POST /api/ai-assistant/chat
    # in main.py) — deliberately separate from the floating ChatBot widget's
    # /api/chat: no Knowledge Base retrieval, no past-job lookups, i.e. it
    # never touches any other portal/AWS resource, it only calls Bedrock
    # (GPT-6.1 Terra) directly for a plain Q&A reply (optionally with web
    # grounding). Restrict via the Admin page's User Groups if this
    # should only be available to selected users.
    {"id": "ai_assistant", "label": "AI Assistant"},
    # Whitelist (LMS) — see app/whitelist_lms.py. This tab_id gates only
    # the CHECK/ADD page itself; the separate audit-log record VIEW is
    # gated independently by main.py's require_whitelist_audit_viewer()
    # (admins + Cloud Infra/Security/Management Team only), which
    # bypasses this tab-level flag entirely — restricting this tab_id
    # here does NOT restrict who can view the audit log, and vice versa.
    {"id": "whitelist_lms", "label": "Whitelist - LMS"},
    # AWS DevOps Agent (A2A integration) — forwards questions to a
    # dedicated AWS DevOps Agent Agent Space (region set by
    # DEVOPS_AGENT_REGION) via POST /api/devops-agent/chat (see
    # app/devops_agent.py). Separate from the AI Assistant tab above —
    # this one CAN access real AWS resource state (investigations, cost,
    # topology) in the Agent Space's associated accounts, so keep it
    # restricted via the Admin page's User Groups if it should only be
    # available to the Cloud Infra team.
    {"id": "devops_agent", "label": "AWS DevOps Agent"},
    # DNS Record (Security section) — see app/dns_records.py. This tab_id
    # gates only VISIBILITY of the page, i.e. who can LOOK UP records.
    # It does NOT control who can change DNS: writes are gated
    # independently by main.py's require_dns_writer() (admins + Cloud
    # Infra/Security/Management Team) and the audit-log view by
    # require_dns_audit_viewer(), both of which bypass this flag
    # entirely. So granting this tab widely is safe — it hands out
    # read-only DNS lookup, not the ability to edit a zone.
    {"id": "dns_records", "label": "DNS Record"},
    # S2S VPN Profile Creation (Security section) — see
    # app/s2s_vpn_creation.py. WRITES real AWS network infrastructure
    # (Customer Gateways, Site-to-Site VPN connections, Transit Gateway
    # static routes) in the network account (100000000003), via
    # sts:AssumeRole(JobPortal-CrossAccount-InfraOps-1000) using the ECS
    # task's own identity — same cross-account pattern as vpn_lookup.py.
    # This tab_id gates only VISIBILITY of the wizard; it does NOT
    # control who can actually create infrastructure. Creating is gated
    # independently by main.py's require_vpn_writer() (admins + Cloud
    # Infra/Security Team only) and the audit-log view by
    # require_s2s_vpn_audit_viewer(), both of which bypass this flag
    # entirely — so granting this tab widely only exposes the "list
    # existing Customer Gateways" read, not the ability to create real
    # VPN infrastructure.
    {"id": "s2s_vpn_profile_creation", "label": "S2S VPN Profile Creation"},
    # Local Infra Hardware Monitor — see app/meraki_monitor.py. A
    # read-only Cisco Meraki Access Point monitoring dashboard (device
    # inventory, channel utilization, client counts, latency, packet
    # loss, event log). No write path at all (never modifies anything in
    # Meraki), so — mirroring waf_monitoring's precedent exactly — this
    # tab_id is the ONLY access control: every backend endpoint uses
    # plain get_actor (any logged-in user), no dedicated require_* group
    # gate.
    {"id": "meraki_monitor", "label": "Local Infra Hardware Monitor"},
    # Azure Resources — see app/azure_monitor.py. A read-only inventory
    # dashboard for this org's Azure subscription (VM, AVD, Resource
    # Group, NIC, NSG sub-tabs). Originally launched as its own
    # independent sidebar category, then folded into the existing
    # "resources" category (see CATEGORY_REGISTRY below) per user's
    # explicit follow-up request. v1 is Reader-only — the Service
    # Principal backing this has no write/control permissions in Azure,
    # so this tab_id is the ONLY access control needed; no dedicated
    # require_* group gate exists.
    {"id": "azure_resources", "label": "Azure Resources"},
    # Chat + Voice/Video Calls (job-portal-chat-voice spec) — see
    # app/chat_store.py / app/call_store.py / app/ws_manager.py and
    # frontend/src/chat/ChatPage.jsx (Task 14). A company-wide
    # communication tool (Direct/Group text chat, calls, screen share,
    # file/photo sharing) intended for every Team_Member by default —
    # like every other tab_id here, an admin can still restrict it via
    # the Admin page's per-role/User-Group visibility controls if ever
    # needed, but there's no dedicated require_* group gate on the
    # underlying chat/call routes themselves (participation in a
    # specific Conversation/Call is what's actually enforced server-side
    # — see main.py's _require_conversation_participant()).
    {"id": "chat", "label": "Chat"},
    # Website Monitoring (Monitoring section) — see app/website_monitor.py.
    # Up/down, status code, response time and last successful screenshot
    # of the company websites from US/UK/Singapore/Mumbai/Tokyo probes.
    # Visible to EVERY logged-in user regardless of group (listed in
    # EVERYONE_TAB_IDS below, so it deliberately has no CATEGORY_REGISTRY
    # entry and nothing to grant on the Admin page). Reading is open to
    # everyone; ADDING/REMOVING a monitored website is admin-only and is
    # enforced server-side by main.py's require_admin, not by this flag.
    {"id": "website_monitoring", "label": "Website Monitoring"},
    # Lifecycle > Patching > OS — see app/patching.py. Which EC2 instances
    # are in scope for OS patching and when (UTC+8), with the Freshservice
    # change / AMS SR numbers and the Teams bridge. READ is open to every
    # logged-in user (EVERYONE_TAB_IDS below). WRITE is enforced server-side
    # by main.py's require_patching_writer(): admins, Cloud Infra / Security /
    # Management Team members, or users individually granted write on THIS
    # tab_id via the Admin page's per-user "Can Do" list.
    {"id": "lifecycle_patching", "label": "Lifecycle - Patching (OS)"},
]
TAB_IDS = {t["id"] for t in TAB_REGISTRY}

# Tabs every logged-in user can see, independent of User Group membership
# (see main.py's get_my_tab_access()). Kept tiny and explicit: only
# read-only, company-wide pages belong here.
EVERYONE_TAB_IDS = frozenset({"website_monitoring", "lifecycle_patching"})


# ── User Groups & Category Access ─────────────────────────────────────────
# The SOLE access-control layer for sidebar tab visibility (the previous
# per-role Tab Access Control matrix has been removed entirely). Lets an
# admin organize portal users into fixed teams
# (Security Team, Infra Team, Cloud Infra Team, Management Team, Billing
# Team, Helpdesk Team) and grant each team access to whole sidebar
# CATEGORIES (Cost, Security, etc.) rather than having to tick individual
# tabs one by one.
#
# Semantics: a tab is visible to a user ONLY if the tab's category is
# granted to at least one group the user belongs to (see
# tabs_granted_by_groups() below). Admins (user_store.is_admin()) are the
# one exception and always see every tab regardless of group membership
# — same "admin can never lock themselves out" precedent used everywhere
# else in this app. A user with no group memberships (and who is not an
# admin) sees NO tabs at all until an admin assigns them to a group.
#
# Both the group registry and the category->tabs mapping are fixed code
# constants (same pattern as TAB_REGISTRY above) — not admin-creatable —
# since the specific teams and categories were a deliberate, named
# request rather than a generic "let admins invent arbitrary groups"
# feature. Extending either list is a code change (new constant entry),
# same tradeoff TAB_REGISTRY already makes.

GROUP_REGISTRY = [
    {"id": "security_team", "label": "Security Team"},
    {"id": "infra_team", "label": "Infra Team"},
    {"id": "cloud_infra_team", "label": "Cloud Infra Team"},
    {"id": "management_team", "label": "Management Team"},
    {"id": "billing_team", "label": "Billing Team"},
    # New group so the Helpdesk Team can be granted the "helpdesk"
    # category (see CATEGORY_REGISTRY below) without also being an
    # existing team (e.g. Infra Team) whose members shouldn't
    # necessarily all get Helpdesk Catalog tools by association.
    {"id": "helpdesk_team", "label": "Helpdesk Team"},
]
GROUP_IDS = {g["id"] for g in GROUP_REGISTRY}

# Category -> tab_ids, mirroring the sidebar section groupings in
# frontend/src/App.jsx exactly (Sidebar()'s SIDEBAR_SECTION_LABEL blocks).
# "Administration" (the Admin page itself) is deliberately excluded here
# too, for the same reason it's excluded from TAB_REGISTRY — group-based
# access is only ever an ADDITIONAL grant on top of the is_admin() check,
# which this cannot bypass.
CATEGORY_REGISTRY = [
    {"id": "overview", "label": "Overview", "tab_ids": ["dashboard"]},
    {"id": "tickets", "label": "Tickets", "tab_ids": ["jobs", "jobs_new", "cases"]},
    {"id": "resources", "label": "Resources", "tab_ids": ["aws_resources", "health_dashboard", "cmdb", "vpn_lookup", "azure_resources"]},
    {"id": "cost", "label": "Cost", "tab_ids": ["cost_analysis", "billing_by_service", "ri_sp_savings"]},
    {"id": "calendars", "label": "Calendars", "tab_ids": ["calendars"]},
    {"id": "others", "label": "Others", "tab_ids": ["feedback", "kb"]},
    # "dns_records" belongs here (rather than getting its own category
    # like "whitelist" did) because the user asked for it as a tab UNDER
    # Security. Granting a group this category therefore also grants DNS
    # lookup — which is fine, since the category only ever controls
    # read-only visibility; changing DNS needs require_dns_writer()
    # regardless of any category grant.
    {"id": "security", "label": "Security", "tab_ids": ["security_team_portal", "security_hub", "waf_monitoring", "dns_records", "s2s_vpn_profile_creation", "sr_ticket_information", "licensing"]},
    # Helpdesk Team catalogue (see frontend/src/pages/HelpdeskTeamPage.jsx)
    # — added so this section's tools can be granted to a whole team via
    # the group-based additive layer below, same as every other sidebar
    # section. Was previously only in TAB_REGISTRY (role-based member/
    # admin toggle) but missing here, so no group could be granted access
    # to it — this closes that gap. New Helpdesk Team tools should be
    # added to this category's tab_ids alongside a new TAB_REGISTRY entry.
    {"id": "helpdesk", "label": "Helpdesk Team", "tab_ids": ["helpdesk_team_s3_screensaver"]},
    # AI Assistant sidebar tab (see TAB_REGISTRY entry above) — its own
    # dedicated category (not folded into "others") so an admin can grant
    # it to a specific group of "selected users" via the group-based
    # additive layer below without also having to expose Feedback/KB.
    {"id": "ai_assistant", "label": "AI Assistant", "tab_ids": ["ai_assistant"]},
    # Whitelist (LMS) sidebar section (see frontend/src/App.jsx's
    # Sidebar()) — its own top-level category, not folded into
    # "helpdesk", since Cloud Infra/Security/Management Team access to
    # the underlying audit records is separately hardcoded (not
    # group-category based — see main.py's require_whitelist_audit_viewer())
    # and this category only ever controls visibility of the CHECK/ADD
    # tab itself, not the audit log.
    {"id": "whitelist", "label": "Whitelist", "tab_ids": ["whitelist_lms"]},
    # AWS DevOps Agent sidebar tab (see TAB_REGISTRY entry above) — its
    # own dedicated category, same reasoning as "ai_assistant" above, so
    # it can be granted to (e.g.) just the Cloud Infra team without also
    # exposing every other tab in "others".
    {"id": "devops_agent", "label": "AWS DevOps Agent", "tab_ids": ["devops_agent"]},
    # Local Infra Hardware Monitor sidebar tab (see TAB_REGISTRY entry
    # above) — its own dedicated category, same reasoning as
    # "ai_assistant"/"devops_agent" above, so it can be granted to (e.g.)
    # just the Cloud Infra/Infra team without also exposing every other
    # tab in "others".
    {"id": "meraki_monitor", "label": "Local Infra Hardware Monitor", "tab_ids": ["meraki_monitor"]},
    # Chat + Voice/Video Calls sidebar tab (see TAB_REGISTRY entry
    # above) — its own dedicated category, same reasoning as
    # "ai_assistant"/"devops_agent"/"meraki_monitor" above.
    {"id": "chat", "label": "Chat", "tab_ids": ["chat"]},
]
CATEGORY_IDS = {c["id"] for c in CATEGORY_REGISTRY}

GROUP_CATEGORY_ACCESS_KEY = "group_category_access"


def get_group_category_access() -> dict:
    """{group_id: [category_id, ...]} for every group in GROUP_REGISTRY.
    Defaults to an empty list (no extra category grants) for any group
    never explicitly configured yet — a brand-new group starts with NO
    access. This is deliberate: creating a group should never silently
    grant anyone anything until an admin explicitly assigns categories
    to it."""
    stored = get_setting(GROUP_CATEGORY_ACCESS_KEY)
    overrides = (stored.get("value") if stored else None) or {}
    access = {}
    for group in GROUP_REGISTRY:
        raw = overrides.get(group["id"]) or []
        access[group["id"]] = [c for c in raw if c in CATEGORY_IDS]
    return access


def set_group_category_access(access: dict, *, actor_email: str, actor_name: str) -> dict:
    """Persist which categories each group is granted. Silently drops any
    group_id not in GROUP_REGISTRY, any category_id not in
    CATEGORY_REGISTRY, and de-duplicates — defensive against a stale
    frontend build."""
    cleaned = {}
    for group_id, category_ids in (access or {}).items():
        if group_id not in GROUP_IDS or not isinstance(category_ids, list):
            continue
        cleaned[group_id] = sorted({c for c in category_ids if c in CATEGORY_IDS})
    set_setting(GROUP_CATEGORY_ACCESS_KEY, cleaned, actor_email=actor_email, actor_name=actor_name)
    return get_group_category_access()


def tabs_granted_by_groups(group_ids: list[str]) -> set[str]:
    """Resolve a user's group memberships down to the set of tab_ids
    granted by ANY of those groups' category access — used by
    /api/settings/my-tab-access, which is now the sole source of tab
    visibility for non-admin users (see the section docstring above)."""
    if not group_ids:
        return set()
    access = get_group_category_access()
    granted_categories: set[str] = set()
    for gid in group_ids:
        granted_categories.update(access.get(gid, []))
    if not granted_categories:
        return set()
    tab_ids: set[str] = set()
    for category in CATEGORY_REGISTRY:
        if category["id"] in granted_categories:
            tab_ids.update(category["tab_ids"])
    return tab_ids
