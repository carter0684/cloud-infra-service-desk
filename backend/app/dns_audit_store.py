"""
Append-only audit trail for the "DNS Record" tool — records every
create/modify/delete attempt against the Route 53 hosted zones in
app/dns_records.py. Mirrors whitelist_store.py's ENV=local (SQLite via
SQLAlchemy) / ENV!=local (DynamoDB) split exactly, since the Security
Audit requirement for this tab is the same one Whitelist (LMS) already
satisfies.

VISIBILITY of these records is enforced entirely by the route handler in
main.py (require_dns_audit_viewer()), not by this module — every
function here is a plain read/write with no access check of its own.

WHY THIS MATTERS MORE THAN THE WHITELIST EQUIVALENT: a whitelist entry
adds one IP to an allowlist, whereas a row here can correspond to a
change that took a public domain's mail or web traffic down. So unlike
whitelist_store.py this store also keeps `previous_value` — the record's
contents *before* a modify/delete — because "what did it used to be?" is
the first question asked during a DNS incident, and Route 53 itself
keeps no such history.

EVERY FIELD IS STORED AS A STRING, including TTL. DynamoDB hands numeric
attributes back as decimal.Decimal, which FastAPI cannot serialise to
JSON — a numeric ttl column would make GET /api/dns/audit-log return 500
in production while passing every local test (where SQLite returns a
plain int). whitelist_store.py never hit this only because it happens to
have no numeric field.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

ENV = os.environ.get("ENV", "local")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_shape(
    *, action: str, zone_id: str, zone_name: str, record_name: str, record_type: str,
    result: str, actor_email: str, actor_name: str,
    ttl: Optional[str] = None, value: Optional[str] = None,
    previous_value: Optional[str] = None, sr_number: Optional[str] = None,
    change_id: Optional[str] = None, batch_id: Optional[str] = None,
    detail: Optional[str] = None,
) -> dict:
    """Single source of truth for a row's fields, keyword-only so a
    caller can never silently transpose two same-typed arguments (e.g.
    record_name/record_type or value/previous_value) — the exact mistake
    that would make an audit trail quietly wrong rather than obviously
    broken."""
    return {
        "action": action, "zone_id": zone_id, "zone_name": zone_name,
        "record_name": record_name, "record_type": record_type,
        "ttl": str(ttl) if ttl is not None else None,
        "value": value, "previous_value": previous_value,
        "sr_number": sr_number, "change_id": change_id, "batch_id": batch_id,
        "result": result, "detail": detail,
        "actor_email": actor_email, "actor_name": actor_name,
    }


if ENV == "local":
    from .database import SessionLocal, DnsAuditLog as _Model

    def _row_to_dict(r) -> dict:
        return {
            "id": r.id, "action": r.action, "zone_id": r.zone_id,
            "zone_name": r.zone_name, "record_name": r.record_name,
            "record_type": r.record_type, "ttl": r.ttl, "value": r.value,
            "previous_value": r.previous_value, "sr_number": r.sr_number,
            "change_id": r.change_id, "batch_id": r.batch_id,
            "result": r.result, "detail": r.detail,
            "actor_email": r.actor_email, "actor_name": r.actor_name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }

    def record(**kwargs) -> dict:
        db = SessionLocal()
        try:
            row = _Model(id=str(uuid.uuid4()), **_record_shape(**kwargs))
            db.add(row)
            db.commit()
            db.refresh(row)
            return _row_to_dict(row)
        finally:
            db.close()

    def list_records(limit: int = 500) -> list[dict]:
        db = SessionLocal()
        try:
            rows = db.query(_Model).order_by(_Model.created_at.desc()).limit(limit).all()
            return [_row_to_dict(r) for r in rows]
        finally:
            db.close()

    def reset_for_tests() -> None:
        """Test-only helper: wipe the table so each test starts clean."""
        db = SessionLocal()
        try:
            db.query(_Model).delete()
            db.commit()
        finally:
            db.close()

else:
    import boto3

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    TABLE_NAME = os.environ.get("DNS_AUDIT_TABLE_NAME", "job-portal-dns-audit-log")

    def _table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)

    def record(**kwargs) -> dict:
        item = {"id": str(uuid.uuid4()), "created_at": _now_iso(), **_record_shape(**kwargs)}
        _table().put_item(Item=item)
        return item

    def list_records(limit: int = 500) -> list[dict]:
        # Low-volume audit table — bounded by how often DNS actually
        # changes (a handful of records a week at most), not by request
        # rate — so a scan is acceptable here, the same tradeoff
        # whitelist_store.py/cmdb_store.py make for their own "list
        # everything" paths.
        table = _table()
        items: list[dict] = []
        resp = table.scan()
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp and len(items) < limit:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        items.sort(key=lambda i: i.get("created_at", ""), reverse=True)
        return items[:limit]

    def reset_for_tests() -> None:  # pragma: no cover - DynamoDB path has no test double here
        raise NotImplementedError("reset_for_tests() is only available in ENV=local")
