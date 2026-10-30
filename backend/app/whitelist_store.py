"""
Append-only audit trail store for the "Whitelist (LMS)" tool — records
every IP check/add attempt against the fixed WAF IPSet / Security Group
targets in app/whitelist_lms.py. Mirrors cmdb_store.py's ENV=local
(SQLite via SQLAlchemy) / ENV!=local (DynamoDB) split exactly.

VISIBILITY of these records is enforced entirely by the route handler in
main.py (require_whitelist_audit_viewer()), not by this module — every
function here is a plain read/write with no access check of its own.
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
    *, action: str, target_id: str, target_label: str, region: str, ip_address: str,
    result: str, actor_email: str, actor_name: str,
    sr_number: Optional[str] = None, detail: Optional[str] = None,
) -> dict:
    return {
        "action": action, "target_id": target_id, "target_label": target_label,
        "region": region, "ip_address": ip_address, "sr_number": sr_number,
        "result": result, "detail": detail,
        "actor_email": actor_email, "actor_name": actor_name,
    }


if ENV == "local":
    from .database import SessionLocal, WhitelistAuditLog as _Model

    def record(**kwargs) -> dict:
        db = SessionLocal()
        try:
            row = _Model(id=str(uuid.uuid4()), **_record_shape(**kwargs))
            db.add(row)
            db.commit()
            db.refresh(row)
            return {
                "id": row.id, "action": row.action, "target_id": row.target_id,
                "target_label": row.target_label, "region": row.region,
                "ip_address": row.ip_address, "sr_number": row.sr_number,
                "result": row.result, "detail": row.detail,
                "actor_email": row.actor_email, "actor_name": row.actor_name,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        finally:
            db.close()

    def list_records(limit: int = 500) -> list[dict]:
        db = SessionLocal()
        try:
            rows = db.query(_Model).order_by(_Model.created_at.desc()).limit(limit).all()
            return [
                {
                    "id": r.id, "action": r.action, "target_id": r.target_id,
                    "target_label": r.target_label, "region": r.region,
                    "ip_address": r.ip_address, "sr_number": r.sr_number,
                    "result": r.result, "detail": r.detail,
                    "actor_email": r.actor_email, "actor_name": r.actor_name,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
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
    TABLE_NAME = os.environ.get("WHITELIST_AUDIT_TABLE_NAME", "job-portal-whitelist-audit-log")

    def _table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)

    def record(**kwargs) -> dict:
        item = {"id": str(uuid.uuid4()), "created_at": _now_iso(), **_record_shape(**kwargs)}
        _table().put_item(Item=item)
        return item

    def list_records(limit: int = 500) -> list[dict]:
        # Low-volume audit table (bounded by how often people actually
        # request whitelisting, not by request rate) — a scan is
        # acceptable here, same tradeoff cmdb_store.py/cost_adjustment_store.py
        # make for their own "list everything" paths.
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
