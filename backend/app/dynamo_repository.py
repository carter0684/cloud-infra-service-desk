"""
DynamoDB-backed repository for the Cloud Infra Job Portal.

Replaces database.py's SQLAlchemy Job/KBDocument models with direct
DynamoDB access (via boto3) against the tables created by
job-portal/infra-cdk (DataStack): job-portal-jobs, job-portal-kb-documents,
job-portal-mappings, job-portal-sync-log, job-portal-webhook-idempotency,
job-portal-notification-dedup.

Design notes:
- Table/attribute names match infra_cdk/stacks/data_stack.py exactly.
- All read/write functions return/accept plain dicts (no ORM objects) so
  main.py's job_to_dict()-shaped output stays unchanged for the frontend.
- Numeric round-trip fidelity (Requirement 12.4): DynamoDB stores numbers as
  Decimal internally. We convert Decimal -> int/float on read so JSON
  serialization never produces surprising string/Decimal artifacts, and we
  never let floats silently truncate (int stays int, float stays float).
- entity_type is a constant "job" partition key value for the AllJobsIndex
  GSI, letting us list all jobs sorted by created_at without a full scan.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

JOBS_TABLE_NAME = os.environ.get("JOBS_TABLE_NAME", "job-portal-jobs")
KB_DOCUMENTS_TABLE_NAME = os.environ.get("KB_DOCUMENTS_TABLE_NAME", "job-portal-kb-documents")
MAPPINGS_TABLE_NAME = os.environ.get("MAPPINGS_TABLE_NAME", "job-portal-mappings")
SYNC_LOG_TABLE_NAME = os.environ.get("SYNC_LOG_TABLE_NAME", "job-portal-sync-log")
WEBHOOK_IDEMPOTENCY_TABLE_NAME = os.environ.get(
    "WEBHOOK_IDEMPOTENCY_TABLE_NAME", "job-portal-webhook-idempotency"
)
NOTIFICATION_DEDUP_TABLE_NAME = os.environ.get(
    "NOTIFICATION_DEDUP_TABLE_NAME", "job-portal-notification-dedup"
)

ALL_JOBS_ENTITY_TYPE = "job"


def _resource():
    return boto3.resource("dynamodb", region_name=AWS_REGION)


def _jobs_table():
    return _resource().Table(JOBS_TABLE_NAME)


def _kb_documents_table():
    return _resource().Table(KB_DOCUMENTS_TABLE_NAME)


def _mappings_table():
    return _resource().Table(MAPPINGS_TABLE_NAME)


def _sync_log_table():
    return _resource().Table(SYNC_LOG_TABLE_NAME)


def _webhook_idempotency_table():
    return _resource().Table(WEBHOOK_IDEMPOTENCY_TABLE_NAME)


def _notification_dedup_table():
    return _resource().Table(NOTIFICATION_DEDUP_TABLE_NAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decimal_to_number(value: Decimal) -> Any:
    """Convert a Decimal read back from DynamoDB to int or float, preserving
    the original numeric type (no int/float coercion, no string leakage)."""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _from_dynamo(value: Any) -> Any:
    """Recursively convert DynamoDB Decimal values to native int/float."""
    if isinstance(value, Decimal):
        return _decimal_to_number(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


def _to_dynamo(value: Any) -> Any:
    """Recursively convert native floats to Decimal (via str to avoid binary
    float rounding) before writing to DynamoDB. Ints pass through untouched."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


# ── Jobs ──────────────────────────────────────────────────────────────────────

def job_to_dict(item: dict) -> dict:
    """Shape a raw DynamoDB job item into the same dict shape main.py expects
    from the old SQLAlchemy job_to_dict()."""
    item = _from_dynamo(item)
    return {
        "id": item.get("job_id"),
        "ticket_number": item.get("ticket_number"),
        "title": item.get("title"),
        "description": item.get("description"),
        "category": item.get("category"),
        "category_other": item.get("category_other"),
        "priority": item.get("priority"),
        "assignee_email": item.get("assignee_email"),
        "assignee_name": item.get("assignee_name"),
        "created_by_email": item.get("created_by_email"),
        "created_by_name": item.get("created_by_name"),
        "due_date": item.get("due_date"),
        "status": item.get("status"),
        "ai_draft": item.get("ai_draft"),
        "ai_analysed_at": item.get("ai_analysed_at"),
        "ai_analysed_by": item.get("ai_analysed_by"),
        "aws_support_case_id": item.get("aws_support_case_id"),
        "aws_support_case_display_id": item.get("aws_support_case_display_id"),
        "aws_ams_sr_case_id": item.get("aws_ams_sr_case_id"),
        "aws_ams_sr_case_display_id": item.get("aws_ams_sr_case_display_id"),
        "aws_ams_sr_case_status": item.get("aws_ams_sr_case_status"),
        "aws_ams_incident_case_id": item.get("aws_ams_incident_case_id"),
        "aws_ams_incident_case_display_id": item.get("aws_ams_incident_case_display_id"),
        "aws_ams_incident_case_status": item.get("aws_ams_incident_case_status"),
        "freshservice_ticket_id": item.get("freshservice_ticket_id"),
        "freshservice_ticket_url": item.get("freshservice_ticket_url"),
        "audit_log": item.get("audit_log") or [],
        "notes": item.get("notes") or [],
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _next_ticket_number() -> int:
    """
    Atomically allocate the next sequential ticket number using DynamoDB's
    UpdateItem ADD, which is atomic even with multiple ECS tasks writing
    concurrently (unlike a read-then-write from job_store.py's local/SQLite
    counterpart, which only needs to handle a single process). Stored as
    a dedicated row in the existing mappings table (mapping_type="counter",
    portal_value="ticket_number") rather than a new table, since this
    portal already has a general-purpose key-value table available and a
    single counter doesn't justify provisioning another one.
    """
    resp = _mappings_table().update_item(
        Key={"mapping_type": "counter", "portal_value": "ticket_number"},
        UpdateExpression="SET external_value = if_not_exists(external_value, :start) + :incr",
        ExpressionAttributeValues={":start": Decimal(1000), ":incr": Decimal(1)},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["external_value"])


def create_job(
    *,
    title: str,
    description: str,
    category: str,
    category_other: Optional[str],
    priority: str,
    assignee_email: str,
    assignee_name: str,
    created_by_email: str,
    created_by_name: str,
    due_date: Optional[str],
    audit_log: list,
) -> dict:
    now = _now_iso()
    job_id = str(uuid.uuid4())
    item = {
        "job_id": job_id,
        "entity_type": ALL_JOBS_ENTITY_TYPE,
        "ticket_number": _next_ticket_number(),
        "title": title,
        "description": description,
        "category": category,
        "category_other": category_other,
        "priority": priority,
        "assignee_email": assignee_email,
        "assignee_name": assignee_name,
        "created_by_email": created_by_email,
        "created_by_name": created_by_name,
        "due_date": due_date,
        "status": "Open",
        "ai_draft": None,
        "ai_analysed_at": None,
        "ai_analysed_by": None,
        "aws_support_case_id": None,
        "freshservice_ticket_id": None,
        "freshservice_ticket_url": None,
        "audit_log": audit_log,
        "notes": [],
        "created_at": now,
        "updated_at": now,
    }
    _jobs_table().put_item(Item=_to_dynamo(item))
    return job_to_dict(item)


def get_job(job_id: str) -> Optional[dict]:
    resp = _jobs_table().get_item(Key={"job_id": job_id})
    item = resp.get("Item")
    return job_to_dict(item) if item else None


def update_job_fields(job_id: str, fields: dict) -> Optional[dict]:
    """Update arbitrary top-level fields on a job item and bump updated_at.
    `fields` values may include None to explicitly clear an attribute."""
    if not fields:
        return get_job(job_id)

    fields = dict(fields)
    fields["updated_at"] = _now_iso()

    update_expr_parts = []
    expr_attr_names = {}
    expr_attr_values = {}
    for i, (key, value) in enumerate(fields.items()):
        name_ph = f"#f{i}"
        value_ph = f":v{i}"
        update_expr_parts.append(f"{name_ph} = {value_ph}")
        expr_attr_names[name_ph] = key
        expr_attr_values[value_ph] = _to_dynamo(value)

    update_expr = "SET " + ", ".join(update_expr_parts)
    resp = _jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_attr_names,
        ExpressionAttributeValues=expr_attr_values,
        ReturnValues="ALL_NEW",
    )
    item = resp.get("Attributes")
    return job_to_dict(item) if item else None


_PRIORITY_RANK = {"ASAP": 0, "High": 1, "Medium": 2, "Low": 3}


def list_jobs(
    *,
    status: Optional[list[str]] = None,
    priority: Optional[list[str]] = None,
    category: Optional[list[str]] = None,
    assignee: Optional[list[str]] = None,
    q: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> dict:
    """
    List jobs with optional filtering, backed by the AllJobsIndex GSI so we
    avoid a full table Scan for the common "list everything" case, then
    apply additional filters/pagination in-memory (dataset is small: a
    3-person internal tool, not a high-volume table).
    """
    table = _jobs_table()

    if assignee and len(assignee) == 1 and not (status or priority or category or q):
        # Single-assignee filter can use AssigneeIndex directly.
        resp = table.query(
            IndexName="AssigneeIndex",
            KeyConditionExpression=Key("assignee_email").eq(assignee[0]),
            ScanIndexForward=False,
        )
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.query(
                IndexName="AssigneeIndex",
                KeyConditionExpression=Key("assignee_email").eq(assignee[0]),
                ScanIndexForward=False,
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
    else:
        resp = table.query(
            IndexName="AllJobsIndex",
            KeyConditionExpression=Key("entity_type").eq(ALL_JOBS_ENTITY_TYPE),
            ScanIndexForward=False,
        )
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.query(
                IndexName="AllJobsIndex",
                KeyConditionExpression=Key("entity_type").eq(ALL_JOBS_ENTITY_TYPE),
                ScanIndexForward=False,
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))

    def matches(item: dict) -> bool:
        if status and item.get("status") not in status:
            return False
        if priority and item.get("priority") not in priority:
            return False
        if category and item.get("category") not in category:
            return False
        if assignee and item.get("assignee_email") not in assignee:
            return False
        if q:
            haystack = f"{item.get('title', '')} {item.get('description', '')}".lower()
            if q.lower() not in haystack:
                return False
        return True

    filtered = [item for item in items if matches(item)]
    total = len(filtered)

    # "Sort By" filter (All Tickets tab). "priority" sorts by urgency rank
    # (ASAP first when ascending) rather than alphabetically; every other
    # field sorts on its raw attribute value. Items missing the sort
    # field always sort to the end regardless of direction — split into
    # sortable/unsortable groups first so `reverse` doesn't flip a
    # "missing" marker to the front.
    reverse = sort_order == "desc"
    if sort_by == "priority":
        def _key(item):
            return _PRIORITY_RANK.get(item.get("priority"), 99)
        with_value = [i for i in filtered if i.get("priority") in _PRIORITY_RANK]
        without_value = [i for i in filtered if i.get("priority") not in _PRIORITY_RANK]
    else:
        def _key(item):
            return item.get(sort_by)
        with_value = [i for i in filtered if i.get(sort_by) is not None]
        without_value = [i for i in filtered if i.get(sort_by) is None]
    with_value.sort(key=_key, reverse=reverse)
    filtered = with_value + without_value

    start = (page - 1) * per_page
    end = start + per_page
    page_items = filtered[start:end]

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "jobs": [job_to_dict(item) for item in page_items],
    }


def delete_job(job_id: str) -> None:
    _jobs_table().delete_item(Key={"job_id": job_id})


def build_case_to_job_map() -> dict[str, dict]:
    """
    Scan every job (via the existing AllJobsIndex GSI, same query
    list_jobs() already uses — small dataset, internal 3-person tool) and
    build a {case_id: {job_id, ticket_number, title, status}} map covering
    all 3 case-id fields a job can carry (own AWS Support, AMS SR, AMS
    Incident). Used by the "All Cases" page to show which Job (if any)
    a given AWS Support/AMS case is linked to.
    """
    table = _jobs_table()
    resp = table.query(
        IndexName="AllJobsIndex",
        KeyConditionExpression=Key("entity_type").eq(ALL_JOBS_ENTITY_TYPE),
    )
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = table.query(
            IndexName="AllJobsIndex",
            KeyConditionExpression=Key("entity_type").eq(ALL_JOBS_ENTITY_TYPE),
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))

    case_map: dict[str, dict] = {}
    for item in items:
        item = _from_dynamo(item)
        job_ref = {
            "job_id": item.get("job_id"),
            "ticket_number": item.get("ticket_number"),
            "title": item.get("title"),
            "status": item.get("status"),
            # Included so /api/cases can fall back to "who created the
            # linked Job" as a best-effort "raised by" when the case
            # predates standalone_case_store tracking.
            "created_by_email": item.get("created_by_email"),
            "created_by_name": item.get("created_by_name"),
        }
        for field in ("aws_support_case_id", "aws_ams_sr_case_id", "aws_ams_incident_case_id"):
            case_id = item.get(field)
            if case_id:
                case_map[case_id] = job_ref
    return case_map


# ── KB Documents ──────────────────────────────────────────────────────────────

def kb_doc_to_dict(item: dict) -> dict:
    item = _from_dynamo(item)
    return {
        "id": item.get("doc_id"),
        "filename": item.get("filename"),
        "file_type": item.get("file_type"),
        "description": item.get("description"),
        "char_count": item.get("char_count", 0),
        "chunk_count": item.get("chunk_count", 0),
        "chunks": item.get("chunks") or [],
        "uploaded_by": item.get("uploaded_by"),
        "uploaded_at": item.get("uploaded_at"),
    }


def create_kb_document(
    *,
    filename: str,
    file_type: str,
    description: Optional[str],
    char_count: int,
    chunk_count: int,
    chunks: list,
    uploaded_by: str,
) -> dict:
    doc_id = str(uuid.uuid4())
    item = {
        "doc_id": doc_id,
        "filename": filename,
        "file_type": file_type,
        "description": description,
        "char_count": char_count,
        "chunk_count": chunk_count,
        "chunks": chunks,
        "uploaded_by": uploaded_by,
        "uploaded_at": _now_iso(),
    }
    _kb_documents_table().put_item(Item=_to_dynamo(item))
    return kb_doc_to_dict(item)


def get_kb_document(doc_id: str) -> Optional[dict]:
    resp = _kb_documents_table().get_item(Key={"doc_id": doc_id})
    item = resp.get("Item")
    return kb_doc_to_dict(item) if item else None


def list_kb_documents() -> list[dict]:
    table = _kb_documents_table()
    resp = table.scan()
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    items.sort(key=lambda i: i.get("uploaded_at") or "", reverse=True)
    return [kb_doc_to_dict(item) for item in items]


def delete_kb_document(doc_id: str) -> None:
    _kb_documents_table().delete_item(Key={"doc_id": doc_id})


# ── Mappings (category/status <-> Freshservice) ──────────────────────────────

def get_mapping(mapping_type: str, portal_value: str) -> Optional[dict]:
    resp = _mappings_table().get_item(
        Key={"mapping_type": mapping_type, "portal_value": portal_value}
    )
    item = resp.get("Item")
    return _from_dynamo(item) if item else None


def list_mappings(mapping_type: str) -> list[dict]:
    resp = _mappings_table().query(
        KeyConditionExpression=Key("mapping_type").eq(mapping_type)
    )
    return [_from_dynamo(item) for item in resp.get("Items", [])]


def put_mapping(mapping_type: str, portal_value: str, external_value: str, **extra) -> dict:
    item = {
        "mapping_type": mapping_type,
        "portal_value": portal_value,
        "external_value": external_value,
        **extra,
    }
    _mappings_table().put_item(Item=_to_dynamo(item))
    return item


def delete_mapping(mapping_type: str, portal_value: str) -> None:
    _mappings_table().delete_item(
        Key={"mapping_type": mapping_type, "portal_value": portal_value}
    )


# ── SyncLog (Freshservice sync events, 90-day TTL) ───────────────────────────

def put_sync_log(job_id: str, event_key: str, detail: dict, ttl_days: int = 90) -> None:
    ttl = int(datetime.now(timezone.utc).timestamp()) + ttl_days * 86400
    item = {
        "job_id": job_id,
        "event_key": event_key,
        "detail": detail,
        "created_at": _now_iso(),
        "ttl": ttl,
    }
    _sync_log_table().put_item(Item=_to_dynamo(item))


def list_sync_log(job_id: str) -> list[dict]:
    resp = _sync_log_table().query(KeyConditionExpression=Key("job_id").eq(job_id))
    return [_from_dynamo(item) for item in resp.get("Items", [])]


# ── WebhookIdempotency (short TTL dedup for inbound webhooks) ───────────────

def check_and_set_webhook_idempotency(dedup_key: str, ttl_seconds: int = 300) -> bool:
    """
    Returns True if this is a new (not-yet-seen) event and marks it seen.
    Returns False if the event was already processed (duplicate).
    Uses a conditional put to avoid a race between check and set.
    """
    ttl = int(datetime.now(timezone.utc).timestamp()) + ttl_seconds
    try:
        _webhook_idempotency_table().put_item(
            Item={"dedup_key": dedup_key, "ttl": ttl, "created_at": _now_iso()},
            ConditionExpression=Attr("dedup_key").not_exists(),
        )
        return True
    except _webhook_idempotency_table().meta.client.exceptions.ConditionalCheckFailedException:
        return False


# ── NotificationDedup (5-minute TTL dedup for notifications) ────────────────

def check_and_set_notification_dedup(dedup_key: str, ttl_seconds: int = 300) -> bool:
    """Same semantics as check_and_set_webhook_idempotency, for outbound
    notification dedup (e.g. avoid double-sending the same email within
    a 5-minute window if two ECS tasks race on the same event)."""
    ttl = int(datetime.now(timezone.utc).timestamp()) + ttl_seconds
    try:
        _notification_dedup_table().put_item(
            Item={"dedup_key": dedup_key, "ttl": ttl, "created_at": _now_iso()},
            ConditionExpression=Attr("dedup_key").not_exists(),
        )
        return True
    except _notification_dedup_table().meta.client.exceptions.ConditionalCheckFailedException:
        return False
