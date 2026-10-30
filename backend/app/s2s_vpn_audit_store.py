"""
Append-only audit trail for the "S2S VPN Profile Creation" tool —
records every Customer Gateway / VPN connection / Transit Gateway static
route create attempt against the network account (see
app/s2s_vpn_creation.py). Mirrors app/dns_audit_store.py exactly (same
ENV=local SQLite / ENV!=local DynamoDB split, same "every field a
string" DynamoDB-Decimal-safety rule), for the same reason: this tool
creates real, billable, hard-to-fully-undo AWS network infrastructure,
so every write attempt — success AND failure — needs to be traceable
back to who did it, when, and under which approved change ticket.

VISIBILITY of these records is enforced entirely by the route handler in
main.py (require_s2s_vpn_audit_viewer()), not by this module — every
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
    *, action: str, region: str, resource_type: str, resource_id: Optional[str],
    resource_name: Optional[str], result: str, actor_email: str, actor_name: str,
    detail: Optional[str] = None, sr_number: Optional[str] = None,
) -> dict:
    """Single source of truth for a row's fields, keyword-only so a
    caller can never silently transpose two same-typed arguments — the
    same discipline app/dns_audit_store.py's _record_shape() follows."""
    return {
        "action": action, "region": region,
        "resource_type": resource_type,       # "customer_gateway" | "vpn_connection" | "tgw_route"
        "resource_id": resource_id,            # cgw-.../vpn-.../"<route_table_id>:<cidr>"
        "resource_name": resource_name,        # Name tag, or CIDR for a route
        "sr_number": sr_number,
        "result": result,                      # "created" | "error"
        "detail": detail,
        "actor_email": actor_email, "actor_name": actor_name,
    }


if ENV == "local":
    from .database import SessionLocal, S2sVpnAuditLog as _Model

    def _row_to_dict(r) -> dict:
        return {
            "id": r.id, "action": r.action, "region": r.region,
            "resource_type": r.resource_type, "resource_id": r.resource_id,
            "resource_name": r.resource_name, "sr_number": r.sr_number,
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
    TABLE_NAME = os.environ.get("S2S_VPN_AUDIT_TABLE_NAME", "job-portal-s2s-vpn-audit-log")

    def _table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)

    def record(**kwargs) -> dict:
        item = {"id": str(uuid.uuid4()), "created_at": _now_iso(), **_record_shape(**kwargs)}
        _table().put_item(Item=item)
        return item

    def list_records(limit: int = 500) -> list[dict]:
        # Low-volume audit table — bounded by how often S2S VPN
        # infrastructure actually changes, not by request rate — so a
        # scan is acceptable here, same tradeoff dns_audit_store.py makes.
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
