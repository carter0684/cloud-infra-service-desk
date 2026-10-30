"""
Per-event state for AWS Health Dashboard events: checkbox acknowledgement,
engineer remark, "finished" flag, AMS Service Request linkage, and a
manual admin category override (drag-and-drop between tabs).

Each row is keyed by the AWS Health event's ARN (shared across the whole
portal, not per-user) — see database.py's HealthEventCheck model for the
full field list and history (Task 10: checked*; Task 12: remark*,
finished*, ams_sr_*; category_override* added for the admin
drag-and-drop-between-tabs feature).

ENV=local: SQLite via SQLAlchemy (HealthEventCheck model in database.py).
ENV!=local: DynamoDB table job-portal-health-event-checks.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

ENV = os.environ.get("ENV", "local")

_ALL_FIELDS = (
    "event_arn", "checked", "checked_by_email", "checked_by_name", "checked_at",
    "remark", "remark_by_email", "remark_by_name", "remark_at",
    "finished", "finished_by_email", "finished_by_name", "finished_at",
    "ams_sr_case_id", "ams_sr_case_status", "ams_sr_service", "ams_sr_event_type_code",
    "ams_sr_raised_by_email", "ams_sr_raised_by_name", "ams_sr_raised_at",
    "resource_checks",
    "job_id", "job_ticket_number", "job_created_by_email", "job_created_by_name", "job_created_at",
    "category_override", "category_override_by_email", "category_override_by_name", "category_override_at",
)


def resource_key(entity_value: Optional[str], aws_account_id: Optional[str]) -> str:
    """Stable key identifying one row in an event's "Affected resources"
    table, for per-resource tick state. Combines entity_value + account
    since the same resource identifier (e.g. an instance ID) is only
    guaranteed unique within one account."""
    return f"{aws_account_id or '-'}::{entity_value or '-'}"


if ENV == "local":
    from .database import SessionLocal, HealthEventCheck as _CheckModel

    def _to_dict(c) -> dict:
        return {field: getattr(c, field, None) for field in _ALL_FIELDS}

    def _get_or_create(db, event_arn: str) -> _CheckModel:
        row = db.query(_CheckModel).filter(_CheckModel.event_arn == event_arn).first()
        if not row:
            row = _CheckModel(event_arn=event_arn, checked=False, finished=False)
            db.add(row)
            db.flush()
        return row

    def list_checks() -> dict[str, dict]:
        """Returns {event_arn: check_dict} for every event that has ever
        had any state set (checked/remark/finished/AMS SR) at least once."""
        db = SessionLocal()
        try:
            rows = db.query(_CheckModel).all()
            return {r.event_arn: _to_dict(r) for r in rows}
        finally:
            db.close()

    def set_check(event_arn: str, checked: bool, actor_email: str, actor_name: str) -> dict:
        db = SessionLocal()
        try:
            row = _get_or_create(db, event_arn)
            row.checked = checked
            row.checked_by_email = actor_email
            row.checked_by_name = actor_name
            row.checked_at = datetime.utcnow().isoformat()
            db.commit()
            db.refresh(row)
            return _to_dict(row)
        finally:
            db.close()

    def set_remark(event_arn: str, remark: str, actor_email: str, actor_name: str) -> dict:
        db = SessionLocal()
        try:
            row = _get_or_create(db, event_arn)
            row.remark = remark
            row.remark_by_email = actor_email
            row.remark_by_name = actor_name
            row.remark_at = datetime.utcnow().isoformat()
            db.commit()
            db.refresh(row)
            return _to_dict(row)
        finally:
            db.close()

    def set_finished(event_arn: str, finished: bool, actor_email: str, actor_name: str) -> dict:
        db = SessionLocal()
        try:
            row = _get_or_create(db, event_arn)
            row.finished = finished
            row.finished_by_email = actor_email
            row.finished_by_name = actor_name
            row.finished_at = datetime.utcnow().isoformat()
            db.commit()
            db.refresh(row)
            return _to_dict(row)
        finally:
            db.close()

    def set_category_override(
        event_arn: str, category: Optional[str], actor_email: str, actor_name: str,
    ) -> dict:
        """Admin drag-and-drop: force this event into `category` (one of
        the dashboard's real tab keys) regardless of its AWS-classified
        eventTypeCategory/eventTypeCode. Passing category=None clears the
        override, reverting the event to automatic classification."""
        db = SessionLocal()
        try:
            row = _get_or_create(db, event_arn)
            row.category_override = category
            row.category_override_by_email = actor_email
            row.category_override_by_name = actor_name
            row.category_override_at = datetime.utcnow().isoformat()
            db.commit()
            db.refresh(row)
            return _to_dict(row)
        finally:
            db.close()

    def set_ams_sr_case(
        event_arn: str, *, case_id: str, case_status: Optional[str],
        service: Optional[str], event_type_code: Optional[str],
        actor_email: str, actor_name: str,
    ) -> dict:
        db = SessionLocal()
        try:
            row = _get_or_create(db, event_arn)
            row.ams_sr_case_id = case_id
            row.ams_sr_case_status = case_status
            row.ams_sr_service = service
            row.ams_sr_event_type_code = event_type_code
            row.ams_sr_raised_by_email = actor_email
            row.ams_sr_raised_by_name = actor_name
            row.ams_sr_raised_at = datetime.utcnow().isoformat()
            db.commit()
            db.refresh(row)
            return _to_dict(row)
        finally:
            db.close()

    def get_check(event_arn: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            row = db.query(_CheckModel).filter(_CheckModel.event_arn == event_arn).first()
            return _to_dict(row) if row else None
        finally:
            db.close()

    def set_resource_check(
        event_arn: str, res_key: str, checked: bool, actor_email: str, actor_name: str,
    ) -> dict:
        """Tick/untick one row of the "Affected resources" table inside
        the event-detail popup — independent per resource, distinct from
        both the row-level `checked` and the popup's own `finished`
        checkbox."""
        db = SessionLocal()
        try:
            row = _get_or_create(db, event_arn)
            checks = dict(row.resource_checks or {})
            checks[res_key] = {
                "checked": checked,
                "checked_by_email": actor_email,
                "checked_by_name": actor_name,
                "checked_at": datetime.utcnow().isoformat(),
            }
            row.resource_checks = checks
            db.commit()
            db.refresh(row)
            return _to_dict(row)
        finally:
            db.close()

    def find_by_ams_sr_case_id(case_id: str) -> Optional[dict]:
        """Reverse lookup: given an AMS SR case_id, find the Health event
        (if any) it was raised from. Used by /api/cases to annotate a case
        with `linked_health_event` when it wasn't raised via a Job."""
        db = SessionLocal()
        try:
            row = db.query(_CheckModel).filter(_CheckModel.ams_sr_case_id == case_id).first()
            return _to_dict(row) if row else None
        finally:
            db.close()

    def set_job_created(
        event_arn: str, *, job_id: str, ticket_number: Optional[int],
        actor_email: str, actor_name: str,
    ) -> dict:
        """Record that a portal Job was created from this Health event
        (one-click "Create Job", independent of whether an AMS SR was
        also raised for the same event)."""
        db = SessionLocal()
        try:
            row = _get_or_create(db, event_arn)
            row.job_id = job_id
            row.job_ticket_number = ticket_number
            row.job_created_by_email = actor_email
            row.job_created_by_name = actor_name
            row.job_created_at = datetime.utcnow().isoformat()
            db.commit()
            db.refresh(row)
            return _to_dict(row)
        finally:
            db.close()

    def build_case_to_health_event_map() -> dict[str, dict]:
        """Bulk version of find_by_ams_sr_case_id() — one query instead of
        N, for /api/cases's list view which needs to annotate every case
        at once. Mirrors job_store.build_case_to_job_map()'s pattern."""
        db = SessionLocal()
        try:
            rows = db.query(_CheckModel).filter(_CheckModel.ams_sr_case_id.isnot(None)).all()
            return {
                r.ams_sr_case_id: {
                    "event_arn": r.event_arn,
                    "service": r.ams_sr_service,
                    "event_type_code": r.ams_sr_event_type_code,
                }
                for r in rows
            }
        finally:
            db.close()

else:
    import boto3
    from boto3.dynamodb.conditions import Attr

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    TABLE_NAME = os.environ.get("HEALTH_EVENT_CHECKS_TABLE_NAME", "job-portal-health-event-checks")

    def _table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)

    def _scan_all() -> list[dict]:
        table = _table()
        resp = table.scan()
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        return items

    def list_checks() -> dict[str, dict]:
        items = _scan_all()
        return {i["event_arn"]: dict(i) for i in items}

    def get_check(event_arn: str) -> Optional[dict]:
        resp = _table().get_item(Key={"event_arn": event_arn})
        item = resp.get("Item")
        return dict(item) if item else None

    def _update_item(event_arn: str, updates: dict) -> dict:
        """Partial update via UpdateItem so we never clobber fields set by
        a different sub-feature's endpoint (e.g. setting a remark must not
        wipe out an already-set ams_sr_case_id)."""
        table = _table()
        update_expr_parts = []
        names = {}
        values = {}
        for i, (k, v) in enumerate(updates.items()):
            name_ph, value_ph = f"#f{i}", f":v{i}"
            update_expr_parts.append(f"{name_ph} = {value_ph}")
            names[name_ph] = k
            values[value_ph] = v
        resp = table.update_item(
            Key={"event_arn": event_arn},
            UpdateExpression="SET " + ", ".join(update_expr_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return dict(resp.get("Attributes", {}))

    def set_check(event_arn: str, checked: bool, actor_email: str, actor_name: str) -> dict:
        return _update_item(event_arn, {
            "checked": checked,
            "checked_by_email": actor_email,
            "checked_by_name": actor_name,
            "checked_at": datetime.utcnow().isoformat(),
        })

    def set_remark(event_arn: str, remark: str, actor_email: str, actor_name: str) -> dict:
        return _update_item(event_arn, {
            "remark": remark,
            "remark_by_email": actor_email,
            "remark_by_name": actor_name,
            "remark_at": datetime.utcnow().isoformat(),
        })

    def set_finished(event_arn: str, finished: bool, actor_email: str, actor_name: str) -> dict:
        return _update_item(event_arn, {
            "finished": finished,
            "finished_by_email": actor_email,
            "finished_by_name": actor_name,
            "finished_at": datetime.utcnow().isoformat(),
        })

    def set_category_override(
        event_arn: str, category: Optional[str], actor_email: str, actor_name: str,
    ) -> dict:
        """Admin drag-and-drop: force this event into `category` (one of
        the dashboard's real tab keys) regardless of its AWS-classified
        eventTypeCategory/eventTypeCode. Passing category=None clears the
        override, reverting the event to automatic classification.

        DynamoDB's UpdateExpression can't SET an attribute to a Python
        None (it would try to write a NULL AttributeValue in a way boto3
        rejects for a plain SET on some client configs) — use REMOVE for
        the clear case instead, same trade-off _update_item's SET-only
        helper doesn't handle, so this is a bespoke update_item call."""
        table = _table()
        if category is None:
            resp = table.update_item(
                Key={"event_arn": event_arn},
                UpdateExpression=(
                    "REMOVE category_override SET "
                    "category_override_by_email = :e, "
                    "category_override_by_name = :n, "
                    "category_override_at = :t"
                ),
                ExpressionAttributeValues={
                    ":e": actor_email, ":n": actor_name, ":t": datetime.utcnow().isoformat(),
                },
                ReturnValues="ALL_NEW",
            )
            return dict(resp.get("Attributes", {}))
        return _update_item(event_arn, {
            "category_override": category,
            "category_override_by_email": actor_email,
            "category_override_by_name": actor_name,
            "category_override_at": datetime.utcnow().isoformat(),
        })

    def set_resource_check(
        event_arn: str, res_key: str, checked: bool, actor_email: str, actor_name: str,
    ) -> dict:
        """Tick/untick one row of the "Affected resources" table, stored
        as one entry in the resource_checks map on this event's item.

        DynamoDB rejects a single UpdateExpression that both initialises
        a top-level attribute AND sets a nested path under it in the same
        call ("document paths overlap"), so this is two update_item
        calls: first ensure resource_checks exists as an (empty) map,
        then SET the specific nested key. The first call is a no-op once
        the map already exists (idempotent, safe to repeat every time)."""
        table = _table()
        table.update_item(
            Key={"event_arn": event_arn},
            UpdateExpression="SET resource_checks = if_not_exists(resource_checks, :empty_map)",
            ExpressionAttributeValues={":empty_map": {}},
        )
        resp = table.update_item(
            Key={"event_arn": event_arn},
            UpdateExpression="SET resource_checks.#rk = :entry",
            ExpressionAttributeNames={"#rk": res_key},
            ExpressionAttributeValues={
                ":entry": {
                    "checked": checked,
                    "checked_by_email": actor_email,
                    "checked_by_name": actor_name,
                    "checked_at": datetime.utcnow().isoformat(),
                },
            },
            ReturnValues="ALL_NEW",
        )
        return dict(resp.get("Attributes", {}))

    def set_ams_sr_case(
        event_arn: str, *, case_id: str, case_status: Optional[str],
        service: Optional[str], event_type_code: Optional[str],
        actor_email: str, actor_name: str,
    ) -> dict:
        return _update_item(event_arn, {
            "ams_sr_case_id": case_id,
            "ams_sr_case_status": case_status,
            "ams_sr_service": service,
            "ams_sr_event_type_code": event_type_code,
            "ams_sr_raised_by_email": actor_email,
            "ams_sr_raised_by_name": actor_name,
            "ams_sr_raised_at": datetime.utcnow().isoformat(),
        })

    def find_by_ams_sr_case_id(case_id: str) -> Optional[dict]:
        """No GSI on ams_sr_case_id (small dataset — internal tool, not
        high volume) — a full scan with a filter is simplest and cheap
        enough here, same trade-off job_store/dynamo_repository already
        make elsewhere in this codebase (e.g. build_case_to_job_map)."""
        table = _table()
        resp = table.scan(FilterExpression=Attr("ams_sr_case_id").eq(case_id))
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp and not items:
            resp = table.scan(
                FilterExpression=Attr("ams_sr_case_id").eq(case_id),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
        return dict(items[0]) if items else None

    def build_case_to_health_event_map() -> dict[str, dict]:
        """Bulk version of find_by_ams_sr_case_id() for /api/cases's list
        view — one scan instead of N. Same small-dataset trade-off as the
        rest of this module's DynamoDB path."""
        items = _scan_all()
        return {
            i["ams_sr_case_id"]: {
                "event_arn": i.get("event_arn"),
                "service": i.get("ams_sr_service"),
                "event_type_code": i.get("ams_sr_event_type_code"),
            }
            for i in items
            if i.get("ams_sr_case_id")
        }

    def set_job_created(
        event_arn: str, *, job_id: str, ticket_number: Optional[int],
        actor_email: str, actor_name: str,
    ) -> dict:
        """Record that a portal Job was created from this Health event."""
        return _update_item(event_arn, {
            "job_id": job_id,
            "job_ticket_number": ticket_number,
            "job_created_by_email": actor_email,
            "job_created_by_name": actor_name,
            "job_created_at": datetime.utcnow().isoformat(),
        })
