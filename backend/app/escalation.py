"""
Escalation integrations for the Cloud Infra Job Portal: Freshservice and
AWS Support.

Both entry points map the Portal's category/priority through the
`Mappings` DynamoDB table (populated via /api/config/category-mapping and
/api/config/status-mapping, Task 5.13) and raise a typed EscalationError on
any failure so main.py's route handler can return a clean error to the
client without leaking upstream response bodies (Requirement 9.6).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
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

# AWS Managed Services (AMS) support account — cases raised here go
# directly against account 100000000001 using its own raw IAM access
# keys (stored in Secrets Manager in THIS account), not this account's
# AWS Support. Discovered via support:DescribeServices against that
# account (run support:DescribeServices once against that account): this account exposes two AMS-specific Support services:
#   service-ams-operations-service-request  -> Service Request (SR)
#   service-ams-operations-report-incident  -> Incident
# AWS Support API is us-east-1 only regardless of the account's default region.
AMS_SUPPORT_REGION = "us-east-1"
AMS_CREDENTIALS_SECRET_NAME = os.environ.get(
    "AMS_CREDENTIALS_SECRET_NAME", "job-portal/aws-ams-support-credentials"
)
AMS_SERVICE_CODE_SR = "service-ams-operations-service-request"
AMS_SERVICE_CODE_INCIDENT = "service-ams-operations-report-incident"
# AWS Security Incident Response ("SIR") cases — opened by/with the AWS
# Security Incident Response service (observed on the AMS account as
# service code "service-security-incident-response"). The "All Cases" UI
# gives these their own "SIR" tab (frontend CasesPage.jsx).
SIR_SERVICE_CODE = "service-security-incident-response"

# The Security Incident Response service also has its OWN native case
# system (separate from Support Center / the Support API, with 10-digit
# case IDs), which the SIR console shows and the Support API does not
# return. These are read through the `security-ir` API, in the single
# region the service is active in for the AMS account (verified
# 2026-10: ap-southeast-1; every other region returns
# SecurityIncidentResponseNotActiveException). Read-only — the Portal's
# IAM user only has AWSSecurityIncidentResponseReadOnlyAccess.
SECURITY_IR_REGION = os.environ.get("SECURITY_IR_REGION", "ap-southeast-1")
SECURITY_IR_SOURCE = "security_ir"
SECURITY_IR_CASE_TYPE = "AWS Security Incident Response"
# ListCases/ListComments reject maxResults > 25.
_SECURITY_IR_PAGE_SIZE = 25
_SECURITY_IR_MAX_PAGES = 40  # hard stop so a bad nextToken can never loop forever
# Default category per case type — "general-guidance"/"other" are the
# safest generic picks; a future UI can let the user pick a more specific
# category from the full discovery output if needed.
AMS_DEFAULT_CATEGORY_SR = "general-guidance"
AMS_DEFAULT_CATEGORY_INCIDENT = "other"

# AMS account exposes 5 severities (adds "critical" above "urgent") vs the
# standard 4 on a normal Support account.
_DEFAULT_AMS_SEVERITY = {
    "ASAP": "critical",
    "High": "urgent",
    "Medium": "normal",
    "Low": "low",
}

# Freshservice ticket priority mapping when no explicit Mappings-table entry
# exists yet — used as a sane default so escalation still works before an
# admin has configured custom mappings via /api/config/*.
_DEFAULT_FRESHSERVICE_PRIORITY = {
    "ASAP": 4,     # Urgent
    "High": 3,     # High
    "Medium": 2,   # Medium
    "Low": 1,      # Low
}

# Freshservice severity field (1-4) — this account has it as required_for_agents.
# Separate from priority; 1 = most severe.
_DEFAULT_FRESHSERVICE_SEVERITY = {
    "ASAP": 1,
    "High": 2,
    "Medium": 3,
    "Low": 4,
}

# Default resolution_code for portal-created tickets (required_for_agents on
# this account). "Others" (display_id=44) is the safest generic value.
_DEFAULT_RESOLUTION_CODE = "Others"


class EscalationError(Exception):
    """Typed error for escalation failures. `system` is 'freshservice' or
    'aws_support'; `message` is safe to show to the end user (no raw
    upstream response bodies or stack traces)."""

    def __init__(self, system: str, message: str):
        self.system = system
        self.message = message
        super().__init__(f"[{system}] {message}")


def _get_secret(secret_name: str) -> str:
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]


def _freshservice_priority(portal_priority: str) -> int:
    mapping = repo.get_mapping("priority_to_freshservice", portal_priority)
    if mapping and mapping.get("external_value") is not None:
        try:
            return int(mapping["external_value"])
        except (TypeError, ValueError):
            pass
    return _DEFAULT_FRESHSERVICE_PRIORITY.get(portal_priority, 2)


def _freshservice_category(portal_category: str) -> Optional[str]:
    mapping = repo.get_mapping("category_to_freshservice", portal_category)
    if mapping and mapping.get("external_value"):
        return mapping["external_value"]
    return None


def raise_freshservice_ticket(
    *,
    job_id: str,
    title: str,
    description: str,
    category: str,
    priority: str,
    requester_email: str,
) -> dict:
    """
    Create a Freshservice ticket via the v2 Tickets API.
    Returns {"ticket_id": str, "ticket_url": str} on success.
    Raises EscalationError('freshservice', ...) on any failure.
    """
    if not FRESHSERVICE_DOMAIN:
        raise EscalationError("freshservice", "Freshservice is not configured (set FRESHSERVICE_DOMAIN)")
    try:
        api_key = _get_secret(FRESHSERVICE_API_KEY_SECRET_NAME)
    except Exception as e:
        logger.error("escalation: failed to load Freshservice API key: %s", e)
        raise EscalationError("freshservice", "Could not authenticate with Freshservice") from e

    payload = {
        "subject": title[:255],
        "description": description,
        "email": requester_email,
        "priority": _freshservice_priority(priority),
        "status": 2,  # 2 = Open (required; valid: 2,3,4,5,6,8)
        # severity and resolution_code are custom_fields on this account's
        # ITOM workspace. They must be nested under "custom_fields" and
        # severity must be a STRING "1"-"4", not an integer.
        "custom_fields": {
            "severity": str(_DEFAULT_FRESHSERVICE_SEVERITY.get(priority, 3)),
            "resolution_code": _DEFAULT_RESOLUTION_CODE,
        },
    }
    fs_category = _freshservice_category(category)
    if fs_category:
        payload["category"] = fs_category

    url = f"https://{FRESHSERVICE_DOMAIN}/api/v2/tickets"

    try:
        resp = httpx.post(
            url,
            json=payload,
            auth=(api_key, "X"),  # Freshservice: API key as basic-auth username
            timeout=15.0,
        )
    except httpx.TimeoutException as e:
        logger.error("escalation: Freshservice request timed out for job %s: %s", job_id, e)
        raise EscalationError("freshservice", "Freshservice did not respond in time") from e
    except httpx.HTTPError as e:
        logger.error("escalation: Freshservice request failed for job %s: %s", job_id, e)
        raise EscalationError("freshservice", "Could not reach Freshservice") from e

    if resp.status_code not in (200, 201):
        logger.error(
            "escalation: Freshservice returned %s for job %s: %s",
            resp.status_code, job_id, resp.text[:500],
        )
        raise EscalationError(
            "freshservice",
            f"Freshservice rejected the ticket (HTTP {resp.status_code}): {resp.text[:300]}"
        )

    try:
        data = resp.json()
        ticket = data["ticket"]
        ticket_id = str(ticket["id"])
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.error("escalation: unexpected Freshservice response shape: %s", e)
        raise EscalationError("freshservice", "Freshservice returned an unexpected response") from e

    ticket_url = f"https://{FRESHSERVICE_DOMAIN}/helpdesk/tickets/{ticket_id}"
    repo.put_sync_log(
        job_id,
        f"freshservice_created_{ticket_id}",
        {"ticket_id": ticket_id, "ticket_url": ticket_url, "at": datetime.now(timezone.utc).isoformat()},
    )
    return {"ticket_id": ticket_id, "ticket_url": ticket_url}


def raise_aws_support_case(
    *,
    job_id: Optional[str] = None,
    title: str,
    description: str,
    category: str,
    priority: str,
    attachment_set_id: Optional[str] = None,
) -> dict:
    """
    Create an AWS Support case via boto3 support:CreateCase.
    Returns {"case_id": str} on success.
    Raises EscalationError('aws_support', ...) on any failure.

    job_id is optional — pass None for a case raised directly from the
    AWS Cases page (no Job involved), which skips the sync-log entry
    (that log is keyed by job_id and only meaningful for Job-linked
    cases) and adjusts the communication body's header line accordingly.

    Confirmed valid combination for Business/Enterprise Support accounts:
      issueType     = "technical"
      serviceCode   = "amazon-elastic-compute-cloud-linux"  (General cloud infra)
      categoryCode  = "general-guidance"
    """
    severity_map = {
        "ASAP": "urgent",
        "High": "high",
        "Medium": "normal",
        "Low": "low",
    }
    severity_code = severity_map.get(priority, "normal")

    client = boto3.client("support", region_name="us-east-1")

    origin_line = f"Job ID: {job_id}" if job_id else "Raised directly from the AWS Cases page (no linked Job)."

    create_kwargs = dict(
        subject=title[:255],
        serviceCode="amazon-elastic-compute-cloud-linux",
        categoryCode="general-guidance",
        severityCode=severity_code,
        communicationBody=(
            f"Cloud Infra Job Portal — automated escalation\n\n"
            f"{origin_line}\n"
            f"Category: {category}\n"
            f"Priority: {priority}\n\n"
            f"{description}"
        ),
        issueType="technical",
    )
    if attachment_set_id:
        create_kwargs["attachmentSetId"] = attachment_set_id

    try:
        resp = client.create_case(**create_kwargs)
    except Exception as e:
        error_msg = str(e)
        logger.error("escalation: AWS Support create_case failed for job %s: %s", job_id, e)
        # Surface the real AWS error message so it's actionable
        raise EscalationError("aws_support", f"AWS Support API error: {error_msg[:200]}") from e

    case_id = resp.get("caseId")
    if not case_id:
        raise EscalationError("aws_support", "AWS Support returned an unexpected response")

    # create_case only ever returns the long internal caseId
    # ("case-12345678910-2013-c4c1d2bf33c5cf47") — the short numeric
    # "Display ID" shown in the AWS Support Center UI (and what users
    # actually recognise as "the case number") only comes back from a
    # separate describe_cases call. Best-effort: a failure here shouldn't
    # fail the whole escalation since the case was already created.
    display_id = None
    try:
        describe_resp = client.describe_cases(caseIdList=[case_id], includeResolvedCases=True)
        cases = describe_resp.get("cases", [])
        if cases:
            display_id = cases[0].get("displayId")
    except Exception as e:
        logger.warning("escalation: could not fetch displayId for AWS Support case %s: %s", case_id, e)

    if job_id:
        repo.put_sync_log(
            job_id,
            f"aws_support_created_{case_id}",
            {"case_id": case_id, "at": datetime.now(timezone.utc).isoformat()},
        )
    return {"case_id": case_id, "display_id": display_id}


def _get_ams_credentials() -> dict:
    try:
        secret_str = _get_secret(AMS_CREDENTIALS_SECRET_NAME)
        return json.loads(secret_str)
    except Exception as e:
        logger.error("escalation: failed to load AMS support credentials: %s", e)
        raise EscalationError("aws_ams", "Could not authenticate with the AWS AMS support account") from e





def raise_aws_ams_case(
    *,
    job_id: Optional[str] = None,
    title: str,
    description: str,
    category: str,
    priority: str,
    case_type: str,  # "sr" (Service Request) or "incident"
    attachment_set_id: Optional[str] = None,
) -> dict:
    """
    Create an AWS Support case directly against the AWS Managed Services
    (AMS) support account (100000000001) using that account's own raw IAM
    access keys — a genuinely different AWS account from the one this
    portal otherwise runs in, not just a different Support plan.

    case_type="sr" uses AMS's "Service Request" service catalog entry;
    case_type="incident" uses its "Report Incident" entry. Both discovered
    via support:DescribeServices against that account (raw catalog codes
    are account-specific and not documented publicly for AMS).

    job_id is optional — pass None for a case raised directly from the
    AWS Cases page (no Job involved), which skips the sync-log entry and
    adjusts the communication body's header line accordingly.

    Returns {"case_id": str} on success.
    Raises EscalationError('aws_ams', ...) on any failure.
    """
    if case_type not in ("sr", "incident"):
        raise EscalationError("aws_ams", "case_type must be 'sr' or 'incident'")

    creds = _get_ams_credentials()

    if case_type == "sr":
        service_code = AMS_SERVICE_CODE_SR
        category_code = AMS_DEFAULT_CATEGORY_SR
        label = "AMS Service Request"
    else:
        service_code = AMS_SERVICE_CODE_INCIDENT
        category_code = AMS_DEFAULT_CATEGORY_INCIDENT
        label = "AMS Incident"

    severity_code = _DEFAULT_AMS_SEVERITY.get(priority, "normal")

    origin_line = f"Job ID: {job_id}" if job_id else "Raised directly from the AWS Cases page (no linked Job)."

    try:
        client = boto3.client(
            "support",
            region_name=AMS_SUPPORT_REGION,
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
        )
    except KeyError as e:
        raise EscalationError("aws_ams", "AMS credentials secret is missing required keys") from e

    create_kwargs = dict(
        subject=title[:255],
        serviceCode=service_code,
        categoryCode=category_code,
        severityCode=severity_code,
        communicationBody=(
            f"Cloud Infra Job Portal — {label} escalation\n\n"
            f"{origin_line}\n"
            f"Category: {category}\n"
            f"Priority: {priority}\n\n"
            f"{description}"
        ),
        issueType="technical",
    )
    if attachment_set_id:
        create_kwargs["attachmentSetId"] = attachment_set_id

    try:
        resp = client.create_case(**create_kwargs)
    except Exception as e:
        error_msg = str(e)
        logger.error("escalation: AMS create_case (%s) failed for job %s: %s", case_type, job_id, e)
        raise EscalationError("aws_ams", f"AWS AMS Support API error: {error_msg[:200]}") from e

    case_id = resp.get("caseId")
    if not case_id:
        raise EscalationError("aws_ams", "AWS AMS Support returned an unexpected response")

    # Immediately fetch the case's initial status so the portal has
    # something to show right away rather than waiting for the first
    # manual refresh. Best-effort — a failure here shouldn't fail the
    # whole escalation since the case was already created successfully.
    # Also grabs displayId — create_case only ever returns the long
    # internal caseId ("case-100000000001-muen-2026-..."), NOT the short
    # numeric "Display ID" shown in the AWS Support Center UI (what users
    # actually recognise as "the case number") — that only comes back
    # from describe_cases.
    initial_status = None
    display_id = None
    try:
        describe_resp = client.describe_cases(caseIdList=[case_id], includeResolvedCases=True)
        cases = describe_resp.get("cases", [])
        if cases:
            initial_status = cases[0].get("status")
            display_id = cases[0].get("displayId")
    except Exception as e:
        logger.warning("escalation: could not fetch initial AMS case status for %s: %s", case_id, e)

    if job_id:
        repo.put_sync_log(
            job_id,
            f"aws_ams_{case_type}_created_{case_id}",
            {"case_id": case_id, "case_type": case_type, "at": datetime.now(timezone.utc).isoformat()},
        )
    return {"case_id": case_id, "case_type": case_type, "status": initial_status, "display_id": display_id}


def raise_aws_ams_sr_for_health_event(
    *,
    event_arn: str,
    service: str,
    event_type_code: str,
    event_type_category: Optional[str],
    region: Optional[str],
    status_code: Optional[str],
    remark: Optional[str] = None,
) -> dict:
    """
    Create an AMS Service Request directly from an AWS Health Dashboard
    event, with no Job involved (Task 12, sub-feature 3). Same AMS account
    (100000000001) and credentials as raise_aws_ams_case(), but the
    case body is built from the Health event's own fields instead of a
    Job's title/description/category/priority, and there is no job_id to
    log a sync entry against.

    Severity is derived from the event's own status/category rather than
    a Job priority (which doesn't exist here): an open "issue" maps to
    "urgent", everything else (scheduled changes, notifications, already
    closed issues) maps to "normal" — a reasonable default the user can
    still see the option to adjust in the AWS console once raised.

    Returns {"case_id": str, "status": str|None}.
    Raises EscalationError('aws_ams', ...) on any failure.
    """
    creds = _get_ams_credentials()

    severity_code = "urgent" if (event_type_category == "issue" and status_code == "open") else "normal"

    body_lines = [
        "Cloud Infra Job Portal — AMS Service Request raised from AWS Health Dashboard event",
        "",
        f"Health Event ARN: {event_arn}",
        f"Service: {service}",
        f"Event type: {event_type_code}",
        f"Category: {event_type_category or 'unknown'}",
        f"Region: {region or 'unknown'}",
        f"AWS status: {status_code or 'unknown'}",
    ]
    if remark:
        body_lines += ["", f"Engineer remark: {remark}"]
    communication_body = "\n".join(body_lines)

    try:
        client = boto3.client(
            "support",
            region_name=AMS_SUPPORT_REGION,
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
        )
    except KeyError as e:
        raise EscalationError("aws_ams", "AMS credentials secret is missing required keys") from e

    try:
        resp = client.create_case(
            subject=f"AWS Health event: {service} — {event_type_code}"[:255],
            serviceCode=AMS_SERVICE_CODE_SR,
            categoryCode=AMS_DEFAULT_CATEGORY_SR,
            severityCode=severity_code,
            communicationBody=communication_body,
            issueType="technical",
        )
    except Exception as e:
        error_msg = str(e)
        logger.error("escalation: AMS create_case (health-event SR) failed for %s: %s", event_arn, e)
        raise EscalationError("aws_ams", f"AWS AMS Support API error: {error_msg[:200]}") from e

    case_id = resp.get("caseId")
    if not case_id:
        raise EscalationError("aws_ams", "AWS AMS Support returned an unexpected response")

    initial_status = None
    display_id = None
    try:
        describe_resp = client.describe_cases(caseIdList=[case_id], includeResolvedCases=True)
        cases = describe_resp.get("cases", [])
        if cases:
            initial_status = cases[0].get("status")
            display_id = cases[0].get("displayId")
    except Exception as e:
        logger.warning("escalation: could not fetch initial AMS case status for %s: %s", case_id, e)

    return {"case_id": case_id, "status": initial_status, "display_id": display_id}


def _client_for_account(account: str):
    """Return (boto3 support client, EscalationError system tag) for
    account="own" (this portal's own AWS account, default credential
    chain) or account="ams" (AMS support account 100000000001, raw
    cross-account keys from Secrets Manager)."""
    if account == "own":
        return boto3.client("support", region_name="us-east-1"), "aws_support"
    if account == "ams":
        creds = _get_ams_credentials()
        client = boto3.client(
            "support",
            region_name=AMS_SUPPORT_REGION,
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
        )
        return client, "aws_ams"
    raise EscalationError("aws_support", "account must be 'own' or 'ams'")


def _get_ams_health_client():
    """boto3 'health' client using the AMS account's cross-account raw
    IAM keys — confirmed (via manual testing, 2026-07-30) that only the
    AMS support account (100000000001) has permission to call
    describe_events_for_organization() for this Organization; this
    portal's own account is a regular member account and gets
    AccessDeniedException ("did not come from the management account or
    the administrator of the organization")."""
    creds = _get_ams_credentials()
    try:
        return boto3.client(
            "health",
            region_name=AMS_SUPPORT_REGION,
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
        )
    except KeyError as e:
        raise EscalationError("aws_ams", "AMS credentials secret is missing required keys") from e


# Hard safety ceiling on how many events list_ams_organization_health_events()
# will ever page through — NOT meant to be hit in normal operation. It
# exists purely to guarantee the pagination loop below can't spin forever
# if AWS Health ever returns a broken/looping nextToken. Bumped way past
# any realistic org-wide event volume (2026-08-05 measured ~3,000 events
# org-wide) after discovering the old maxResults=500 cap silently dropped
# real "Action Required" events (e.g. an AWS_LAMBDA_PLANNED_LIFECYCLE_EVENT
# for account 100000000028 was sitting at fetch-order index ~1,350 — past
# the old 500 cutoff — because plenty of high-churn accountNotification
# events (e.g. AWS_VPN_REDUNDANCY_LOSS) keep bumping their lastUpdatedTime
# and pushing everything else down in the "sorted by lastModifiedTime,
# most recent first" result order).
_MAX_ORG_HEALTH_EVENTS = 10000


def list_ams_organization_health_events() -> list[dict]:
    """
    List AWS Health events for the AMS account's Organization — this is
    the data behind AWS Health Dashboard > "Your organization health" >
    Open and recent issues / Scheduled changes / Other notifications /
    Event log.

    Pages through describe_events_for_organization() to completion (see
    _MAX_ORG_HEALTH_EVENTS for the safety ceiling — not a real limit in
    practice). If the org-wide call fails outright before any events have
    been fetched, falls back to the plain account-level describe_events()
    so the page still shows the AMS account's own Health events rather
    than a hard failure. If it fails PARTWAY through pagination (after
    some pages already succeeded), the already-fetched events are
    returned as-is rather than being discarded and swapped for the
    completely different (and much smaller) account-level-only dataset —
    a partial org-wide result is more useful and less surprising than
    silently switching to a different scope.

    Returns raw boto3 event dicts (not re-shaped) — main.py normalizes
    field names and groups by eventTypeCategory for the API response.
    Raises EscalationError('aws_ams', ...) only if BOTH the org-wide and
    account-level calls fail with zero events fetched.
    """
    client = _get_ams_health_client()
    events: list[dict] = []
    next_token: Optional[str] = None
    try:
        while True:
            kwargs = {"maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = client.describe_events_for_organization(**kwargs)
            events.extend(resp.get("events", []))
            next_token = resp.get("nextToken")
            if not next_token:
                break
            if len(events) >= _MAX_ORG_HEALTH_EVENTS:
                logger.warning(
                    "escalation: describe_events_for_organization hit the "
                    "safety ceiling of %d events with more still available "
                    "(nextToken present) — returning what was fetched so far.",
                    _MAX_ORG_HEALTH_EVENTS,
                )
                break
        return events
    except Exception as e:
        if events:
            # Partial success — keep what we already have instead of
            # discarding it for a much smaller, differently-scoped dataset.
            logger.warning(
                "escalation: describe_events_for_organization failed mid-pagination "
                "after fetching %d events (%s) — returning the partial result "
                "rather than falling back to account-level describe_events.",
                len(events), e,
            )
            return events
        logger.warning(
            "escalation: describe_events_for_organization failed before fetching "
            "any events, falling back to account-level describe_events: %s", e,
        )

    try:
        events = []
        next_token = None
        while True:
            kwargs = {"maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = client.describe_events(**kwargs)
            events.extend(resp.get("events", []))
            next_token = resp.get("nextToken")
            if not next_token or len(events) >= _MAX_ORG_HEALTH_EVENTS:
                break
        return events
    except Exception as e2:
        logger.error("escalation: AMS Health describe_events also failed: %s", e2)
        raise EscalationError("aws_ams", f"Could not fetch AWS Health events: {str(e2)[:200]}") from e2


def get_ams_organization_health_event_detail(event_arn: str) -> dict:
    """
    Fetch "Affected accounts" and "Affected resources" for a single AWS
    Health event — the drill-down shown when a user clicks an event's
    name in the AWS Health Dashboard page (event detail modal). Mirrors
    what the AWS Health console shows when you open an event's detail
    pane.

    Tries the organization-wide APIs first (same account/credentials as
    list_ams_organization_health_events()'s primary path); falls back to
    the plain account-level describe_affected_entities() (no account
    filter) if no affected accounts come back, so this still works for
    events returned by that function's own account-level fallback path.
    """
    client = _get_ams_health_client()

    # Event description text — AWS's own narrative of what's happening,
    # shown in the console's event detail pane above "Affected accounts".
    # Tries the org-wide API first (needs at least one affected account to
    # scope the lookup, since org-wide description text is account-
    # specific); falls back to the plain account-level API if that fails
    # or no accounts are known yet.
    event_description: Optional[str] = None
    event_status_code: Optional[str] = None

    affected_accounts: list[str] = []
    try:
        next_token: Optional[str] = None
        while True:
            kwargs = {"eventArn": event_arn}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = client.describe_affected_accounts_for_organization(**kwargs)
            affected_accounts.extend(resp.get("affectedAccounts", []))
            next_token = resp.get("nextToken")
            if not next_token:
                break
    except Exception as e:
        logger.warning(
            "escalation: describe_affected_accounts_for_organization failed for %s: %s",
            event_arn, e,
        )

    try:
        if affected_accounts:
            resp = client.describe_event_details_for_organization(
                organizationEventDetailFilters=[
                    {"eventArn": event_arn, "awsAccountId": affected_accounts[0]}
                ]
            )
            successful = resp.get("successfulSet") or []
            if successful:
                item = successful[0]
                event_description = (item.get("eventDescription") or {}).get("latestDescription")
                event_status_code = (item.get("event") or {}).get("statusCode")
        if event_description is None:
            resp = client.describe_event_details(eventArns=[event_arn])
            successful = resp.get("successfulSet") or []
            if successful:
                item = successful[0]
                event_description = (item.get("eventDescription") or {}).get("latestDescription")
                event_status_code = (item.get("event") or {}).get("statusCode")
    except Exception as e:
        logger.warning(
            "escalation: describe_event_details(_for_organization) failed for %s: %s",
            event_arn, e,
        )

    entities: list[dict] = []
    try:
        if affected_accounts:
            # organizationEntityFilters is capped at 10 filters per call —
            # chunk the (eventArn, account) pairs to stay under that limit.
            for i in range(0, len(affected_accounts), 10):
                chunk = affected_accounts[i:i + 10]
                filters = [{"eventArn": event_arn, "awsAccountId": acct} for acct in chunk]
                next_token = None
                while True:
                    kwargs = {"organizationEntityFilters": filters}
                    if next_token:
                        kwargs["nextToken"] = next_token
                    resp = client.describe_affected_entities_for_organization(**kwargs)
                    entities.extend(resp.get("entities", []))
                    next_token = resp.get("nextToken")
                    if not next_token:
                        break
        else:
            # No affected accounts found via the org API (or it failed) —
            # fall back to the plain account-level call, same pattern as
            # list_ams_organization_health_events()'s own fallback.
            next_token = None
            while True:
                kwargs = {"filter": {"eventArns": [event_arn]}}
                if next_token:
                    kwargs["nextToken"] = next_token
                resp = client.describe_affected_entities(**kwargs)
                entities.extend(resp.get("entities", []))
                next_token = resp.get("nextToken")
                if not next_token:
                    break
    except Exception as e:
        logger.error(
            "escalation: describe_affected_entities(_for_organization) failed for %s: %s",
            event_arn, e,
        )
        # Don't raise — affected_accounts (if any) is still worth showing
        # even if the entities call failed.

    return {
        "event_arn": event_arn,
        "event_description": event_description,
        "event_status_code": event_status_code,
        "affected_accounts": affected_accounts,
        "affected_entities": [
            {
                "entity_value": e.get("entityValue"),
                "aws_account_id": e.get("awsAccountId"),
                "status_code": e.get("statusCode"),
                "last_updated_time": e.get("lastUpdatedTime"),
            }
            for e in entities
        ],
    }


def describe_case_type(account: str, service_code: str) -> str:
    """Human-readable case type label for the "All Cases" list UI."""
    if service_code == SIR_SERVICE_CODE:
        return "AWS Security Incident Response"
    if account == "ams":
        if service_code == AMS_SERVICE_CODE_SR:
            return "AMS Service Request"
        if service_code == AMS_SERVICE_CODE_INCIDENT:
            return "AMS Incident"
        return "AMS Case"
    return "AWS Support"


def _list_cases_via_client(client, err_system: str) -> list[dict]:
    """Page through support:DescribeCases (no caseIdList = every case) for
    the last 365 days — the max window AWS Support keeps communications
    for. Returns raw case dicts as returned by boto3 (not re-shaped)."""
    from datetime import timedelta

    after_time = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cases: list[dict] = []
    next_token: Optional[str] = None
    try:
        while True:
            kwargs = {
                "includeResolvedCases": True,
                "includeCommunications": False,
                "afterTime": after_time,
                "maxResults": 100,
            }
            if next_token:
                kwargs["nextToken"] = next_token
            resp = client.describe_cases(**kwargs)
            cases.extend(resp.get("cases", []))
            next_token = resp.get("nextToken")
            if not next_token:
                break
    except Exception as e:
        logger.error("escalation: list cases (%s) failed: %s", err_system, e)
        raise EscalationError(err_system, f"Could not list cases: {str(e)[:200]}") from e
    return cases


def _security_ir_client():
    """boto3 `security-ir` client using the AMS account's IAM keys (the
    SIR cases live in that account), pinned to SECURITY_IR_REGION."""
    from botocore.config import Config

    creds = _get_ams_credentials()
    return boto3.client(
        "security-ir",
        region_name=SECURITY_IR_REGION,
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}),
    )


def _security_ir_error(action: str, exc: Exception) -> EscalationError:
    """Map a botocore error to a short, user-safe EscalationError. Never
    includes raw AWS response bodies, only a known reason."""
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = resp.get("Error", {}).get("Code", "") or ""
    logger.error("escalation: security-ir %s failed: %s", action, exc)
    if code == "AccessDeniedException" and action == "create_case_comment":
        msg = (
            "Security Incident Response: the Portal's AMS IAM user is not allowed to post "
            "comments (needs security-ir:CreateCaseComment)"
        )
    elif code == "AccessDeniedException":
        msg = (
            "Security Incident Response: the Portal's AMS IAM user has no permission "
            "(needs AWSSecurityIncidentResponseReadOnlyAccess)"
        )
    elif code == "SecurityIncidentResponseNotActiveException":
        msg = f"Security Incident Response is not active in {SECURITY_IR_REGION}"
    elif code in ("ResourceNotFoundException", "ValidationException") and action == "get_case":
        msg = "Security Incident Response case was not found"
    elif action == "create_case_comment" and code in ("ResourceNotFoundException", "ValidationException", "ConflictException"):
        msg = "Security Incident Response could not accept this comment (the case may be closed or no longer exist)"
    else:
        msg = "Could not reach Security Incident Response"
    return EscalationError("aws_ams", msg)


def _friendly_creator(creator) -> Optional[str]:
    """SIR comments identify their author as a principal ARN for customer
    users (e.g. 'arn:aws:iam::123:user/jane') and a plain label such as
    'AWS Responder' for AWS staff. Show just the user/role name for ARNs."""
    if not creator:
        return creator
    s = str(creator)
    if s.startswith("arn:") and "/" in s:
        return s.rsplit("/", 1)[-1]
    return s


def _iso_utc(dt) -> Optional[str]:
    """datetime -> 'YYYY-MM-DDTHH:MM:SS.mmmZ' (same shape the Support API
    returns for timeCreated, so the existing sort/format code works)."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def list_security_ir_cases() -> list[dict]:
    """Every native Security Incident Response case for the AMS account,
    shaped like the rows GET /api/cases returns (plus `source`, and a few
    SIR-only fields). Raises EscalationError on failure."""
    client = _security_ir_client()
    items: list[dict] = []
    token: Optional[str] = None
    try:
        for _ in range(_SECURITY_IR_MAX_PAGES):
            kwargs = {"maxResults": _SECURITY_IR_PAGE_SIZE}
            if token:
                kwargs["nextToken"] = token
            resp = client.list_cases(**kwargs)
            items.extend(resp.get("items", []))
            token = resp.get("nextToken")
            if not token:
                break
    except Exception as e:
        raise _security_ir_error("list_cases", e) from e

    return [
        {
            "case_id": i.get("caseId"),
            "account": "ams",
            "source": SECURITY_IR_SOURCE,
            "case_type": SECURITY_IR_CASE_TYPE,
            "subject": i.get("title"),
            "status": i.get("caseStatus"),
            "service_code": None,
            "display_id": i.get("caseId"),
            "time_created": _iso_utc(i.get("createdDate")),
            "engagement_type": i.get("engagementType"),
            "pending_action": i.get("pendingAction"),
            "resolver_type": i.get("resolverType"),
            "last_updated": _iso_utc(i.get("lastUpdatedDate")),
        }
        for i in items
    ]


SECURITY_IR_COMMENT_MAX = 12000  # CreateCaseComment's body limit


def add_security_ir_comment(case_id: str, body: str, posted_by: str) -> dict:
    """Post a comment on a native SIR case. Every comment made through the
    Portal is authored by the single shared IAM user (infra_portal), so the
    real Portal user is prefixed into the text — otherwise AWS responders
    could not tell who on the team actually wrote it. A fresh clientToken
    makes boto's automatic retry idempotent (no duplicate comment on a
    timeout). Returns {"comment_id": ...}. Raises ValueError for an empty /
    over-long comment (nothing is sent), EscalationError for an AWS failure."""
    import uuid

    text = (body or "").strip()
    if not text:
        raise ValueError("Comment cannot be empty")
    full = f"[Posted via Cloud Infra Portal by {posted_by}]\n\n{text}"
    if len(full) > SECURITY_IR_COMMENT_MAX:
        raise ValueError(f"Comment is too long (max {SECURITY_IR_COMMENT_MAX} characters including the Portal author line)")

    client = _security_ir_client()
    try:
        resp = client.create_case_comment(caseId=case_id, body=full, clientToken=str(uuid.uuid4()))
    except Exception as e:
        raise _security_ir_error("create_case_comment", e) from e
    return {"comment_id": resp.get("commentId")}


def get_security_ir_case_detail(case_id: str) -> dict:
    """Full detail for one native SIR case, shaped like GET
    /api/cases/{id}'s response (so the existing detail page renders it)
    plus SIR-only fields. Read-only. Raises EscalationError on failure."""
    client = _security_ir_client()
    try:
        case = client.get_case(caseId=case_id)
        comments: list[dict] = []
        token: Optional[str] = None
        for _ in range(_SECURITY_IR_MAX_PAGES):
            kwargs = {"caseId": case_id, "maxResults": _SECURITY_IR_PAGE_SIZE}
            if token:
                kwargs["nextToken"] = token
            resp = client.list_comments(**kwargs)
            comments.extend(resp.get("items", []))
            token = resp.get("nextToken")
            if not token:
                break
    except Exception as e:
        raise _security_ir_error("get_case", e) from e

    # Newest last, matching how the Support-API communications render.
    comments.sort(key=lambda c: c.get("createdDate") or datetime.min.replace(tzinfo=timezone.utc))
    return {
        "case_id": case_id,
        "display_id": case_id,
        "account": "ams",
        "source": SECURITY_IR_SOURCE,
        "case_type": SECURITY_IR_CASE_TYPE,
        "subject": case.get("title"),
        "status": case.get("caseStatus"),
        "service_code": None,
        "category_code": case.get("engagementType"),
        "severity_code": None,
        "time_created": _iso_utc(case.get("createdDate")),
        "communications": [
            {
                "body": c.get("body"),
                "submitted_by": _friendly_creator(c.get("creator")),
                "time_created": _iso_utc(c.get("createdDate")),
            }
            for c in comments
        ],
        # SIR-only fields
        "description": case.get("description"),
        "engagement_type": case.get("engagementType"),
        "pending_action": case.get("pendingAction"),
        "resolver_type": case.get("resolverType"),
        "closure_code": case.get("closureCode"),
        "reported_incident_start": _iso_utc(case.get("reportedIncidentStartDate")),
        "actual_incident_start": _iso_utc(case.get("actualIncidentStartDate")),
        "last_updated": _iso_utc(case.get("lastUpdatedDate")),
        "closed_date": _iso_utc(case.get("closedDate")),
        "impacted_accounts": list(case.get("impactedAccounts") or []),
        "impacted_services": list(case.get("impactedServices") or []),
        "impacted_regions": [r.get("region") for r in (case.get("impactedAwsRegions") or []) if r.get("region")],
        "attachment_count": len(case.get("caseAttachments") or []),
        "console_url": f"https://{SECURITY_IR_REGION}.console.aws.amazon.com/security-ir/home?region={SECURITY_IR_REGION}",
    }


def list_aws_support_cases() -> list[dict]:
    """List every case in this portal's own AWS Support account (not just
    ones the portal itself raised — includes anything raised manually via
    the AWS console too)."""
    client, err_system = _client_for_account("own")
    return _list_cases_via_client(client, err_system)


def list_aws_ams_cases() -> list[dict]:
    """List every case in the AWS AMS support account (100000000001),
    same caveat as list_aws_support_cases() re: manually-raised cases."""
    client, err_system = _client_for_account("ams")
    return _list_cases_via_client(client, err_system)


def get_case_detail(case_id: str, account: str) -> dict:
    """Full case detail including recent communications, for the case
    detail/reply view. `account` is "own" or "ams"."""
    client, err_system = _client_for_account(account)
    try:
        resp = client.describe_cases(
            caseIdList=[case_id], includeResolvedCases=True, includeCommunications=True
        )
    except Exception as e:
        logger.error("escalation: get_case_detail(%s, %s) failed: %s", case_id, account, e)
        raise EscalationError(err_system, f"Could not fetch case {case_id}: {str(e)[:200]}") from e
    cases = resp.get("cases", [])
    if not cases:
        raise EscalationError(err_system, f"Case {case_id} was not found")
    return cases[0]


def add_case_communication(
    case_id: str, account: str, body: str, attachment_set_id: Optional[str] = None
) -> dict:
    """Add a reply (communication) to an existing case. `account` is
    "own" or "ams". Returns the raw boto3 add_communication_to_case()
    response (result is a boolean + attachment set, not the new comm
    itself — the caller should re-fetch case detail to see it)."""
    client, err_system = _client_for_account(account)
    kwargs = {"caseId": case_id, "communicationBody": body}
    if attachment_set_id:
        kwargs["attachmentSetId"] = attachment_set_id
    try:
        resp = client.add_communication_to_case(**kwargs)
    except Exception as e:
        logger.error("escalation: add_case_communication(%s, %s) failed: %s", case_id, account, e)
        raise EscalationError(err_system, f"Could not send reply to case {case_id}: {str(e)[:200]}") from e
    return resp


# AWS Support's AddAttachmentsToSet hard limits (AttachmentSetSizeLimitExceeded):
# max 3 attachments per set, max 5 MB per attachment. Enforced client-side
# here (before calling AWS) so the user gets a clear message rather than a
# raw boto3 ClientError.
MAX_ATTACHMENTS_PER_SET = 3
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5 MB


def add_attachments_to_set(
    account: str,
    files: list[tuple[str, bytes]],
    attachment_set_id: Optional[str] = None,
) -> str:
    """
    Upload up to 3 files (AWS Support's own per-call limit) to a new or
    existing attachment set via support:AddAttachmentsToSet. `files` is a
    list of (filename, content_bytes) tuples. `account` is "own" or "ams"
    — the same AWS Support account the resulting case/communication will
    be created against, since an attachment set is only valid within the
    account it was created in.

    An attachment set expires 1 hour after creation (AWS-enforced) — the
    caller should upload attachments and immediately use the returned
    attachmentSetId in the same request (create_case / add_communication).

    Returns the (possibly new) attachmentSetId.
    Raises EscalationError on any failure, including exceeding AWS's
    3-attachments / 5MB-per-attachment limits.
    """
    if not files:
        raise EscalationError("aws_support", "No files provided to attach")
    if len(files) > MAX_ATTACHMENTS_PER_SET:
        raise EscalationError(
            "aws_support",
            f"Too many files ({len(files)}) — AWS Support allows a maximum of "
            f"{MAX_ATTACHMENTS_PER_SET} attachments per upload.",
        )
    for filename, content in files:
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise EscalationError(
                "aws_support",
                f"'{filename}' is too large ({len(content) // 1024} KB) — AWS Support "
                f"allows a maximum of 5 MB per attachment.",
            )

    client, err_system = _client_for_account(account)
    kwargs = {
        "attachments": [{"fileName": name, "data": content} for name, content in files]
    }
    if attachment_set_id:
        kwargs["attachmentSetId"] = attachment_set_id

    try:
        resp = client.add_attachments_to_set(**kwargs)
    except Exception as e:
        logger.error("escalation: add_attachments_to_set(%s) failed: %s", account, e)
        raise EscalationError(err_system, f"Could not upload attachment(s): {str(e)[:200]}") from e

    new_set_id = resp.get("attachmentSetId")
    if not new_set_id:
        raise EscalationError(err_system, "AWS Support returned an unexpected response for the attachment upload")
    return new_set_id


def get_aws_ams_case_status(case_id: str) -> dict:
    """
    Fetch the current status of a previously-raised AWS AMS support case
    by calling support:DescribeCases against the AMS account (100000000001)
    using the same cross-account credentials as raise_aws_ams_case().

    Returns {"case_id": str, "status": str|None, "subject": str|None,
    "recent_communications": [str, ...]} — status is AWS's raw status
    string (e.g. "opened", "pending-customer-action", "resolved").
    Raises EscalationError('aws_ams', ...) on failure.
    """
    creds = _get_ams_credentials()

    try:
        client = boto3.client(
            "support",
            region_name=AMS_SUPPORT_REGION,
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
        )
    except KeyError as e:
        raise EscalationError("aws_ams", "AMS credentials secret is missing required keys") from e

    try:
        resp = client.describe_cases(
            caseIdList=[case_id],
            includeResolvedCases=True,
            includeCommunications=True,
        )
    except Exception as e:
        error_msg = str(e)
        logger.error("escalation: AMS describe_cases failed for case %s: %s", case_id, e)
        raise EscalationError("aws_ams", f"Could not fetch AMS case status: {error_msg[:200]}") from e

    cases = resp.get("cases", [])
    if not cases:
        raise EscalationError("aws_ams", f"Case {case_id} was not found in the AMS support account")

    case = cases[0]
    communications = case.get("recentCommunications", {}).get("communications", [])
    recent_bodies = [
        c.get("body", "")[:300] for c in communications[:3]
        if c.get("body")
    ]

    return {
        "case_id": case_id,
        "status": case.get("status"),
        "subject": case.get("subject"),
        "display_id": case.get("displayId"),
        "recent_communications": recent_bodies,
    }
