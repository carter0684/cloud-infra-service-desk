"""
Manual cost adjustment line items for the AWS Cost Analysis page.

Lets a Cloud Manager record an extra fee/cost (or a correction) per AWS
account + calendar month that Cost Explorer doesn't capture — e.g. a
partner-invoiced charge, a manual chargeback split, or a known billing
correction that hasn't reconciled through AWS yet.

These entries are stored ENTIRELY SEPARATELY from live Cost Explorer data
and are only ever layered on top of it at read time (see
cost_explorer.apply_manual_adjustments()) as their own "adjustments"
bucket — they never mutate or get mistaken for AWS's own numbers.

ENV=local: SQLite via SQLAlchemy (CostAdjustment model in database.py).
ENV!=local: DynamoDB table job-portal-cost-adjustments, partition key
`account_id`, sort key `id` (one account can have many adjustments across
different periods; querying by account_id then filtering by period in
Python keeps this simple and avoids a GSI for what's expected to be a
low-volume, admin-curated table).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

ENV = os.environ.get("ENV", "local")

_ALL_FIELDS = (
    "id", "account_id", "period", "label", "amount", "currency", "note",
    "created_by_email", "created_by_name", "created_at",
    "updated_by_email", "updated_by_name", "updated_at",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_amount(value) -> float:
    """Parse a user-supplied amount into a finite float, rejecting
    non-numeric/NaN/Infinity input rather than silently storing garbage
    that would later corrupt chart geometry or totals."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError("amount must be a number")
    if f != f or f in (float("inf"), float("-inf")):  # NaN / +-Infinity
        raise ValueError("amount must be a finite number")
    return round(f, 2)


if ENV == "local":
    from .database import SessionLocal, CostAdjustment as _AdjustmentModel

    def _to_dict(row) -> dict:
        d = {field: getattr(row, field, None) for field in _ALL_FIELDS}
        d["amount"] = _safe_amount(d["amount"])
        for k in ("created_at", "updated_at"):
            if d.get(k) is not None and not isinstance(d[k], str):
                d[k] = d[k].isoformat()
        return d

    def list_adjustments(account_id: Optional[str] = None) -> list[dict]:
        """All adjustments, optionally filtered to one account. Sorted by
        period descending then created_at descending (newest first)."""
        db = SessionLocal()
        try:
            q = db.query(_AdjustmentModel)
            if account_id:
                q = q.filter(_AdjustmentModel.account_id == account_id)
            rows = q.all()
            items = [_to_dict(r) for r in rows]
            items.sort(key=lambda a: (a["period"], a["created_at"] or ""), reverse=True)
            return items
        finally:
            db.close()

    def get_adjustment(adjustment_id: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            row = db.query(_AdjustmentModel).filter(_AdjustmentModel.id == adjustment_id).first()
            return _to_dict(row) if row else None
        finally:
            db.close()

    def create_adjustment(
        *, account_id: str, period: str, label: str, amount: float, currency: str,
        note: Optional[str], actor_email: str, actor_name: str,
    ) -> dict:
        safe_amount = _safe_amount(amount)
        db = SessionLocal()
        try:
            row = _AdjustmentModel(
                id=str(uuid.uuid4()), account_id=account_id, period=period, label=label,
                amount=str(safe_amount), currency=currency, note=note,
                created_by_email=actor_email, created_by_name=actor_name,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _to_dict(row)
        finally:
            db.close()

    def update_adjustment(
        adjustment_id: str, *, label: Optional[str] = None, amount: Optional[float] = None,
        currency: Optional[str] = None, note: Optional[str] = None,
        actor_email: str, actor_name: str,
    ) -> Optional[dict]:
        db = SessionLocal()
        try:
            row = db.query(_AdjustmentModel).filter(_AdjustmentModel.id == adjustment_id).first()
            if not row:
                return None
            if label is not None:
                row.label = label
            if amount is not None:
                row.amount = str(_safe_amount(amount))
            if currency is not None:
                row.currency = currency
            if note is not None:
                row.note = note
            row.updated_by_email = actor_email
            row.updated_by_name = actor_name
            db.commit()
            db.refresh(row)
            return _to_dict(row)
        finally:
            db.close()

    def delete_adjustment(adjustment_id: str) -> bool:
        db = SessionLocal()
        try:
            row = db.query(_AdjustmentModel).filter(_AdjustmentModel.id == adjustment_id).first()
            if not row:
                return False
            db.delete(row)
            db.commit()
            return True
        finally:
            db.close()

else:
    import boto3
    from boto3.dynamodb.conditions import Attr, Key

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    TABLE_NAME = os.environ.get("COST_ADJUSTMENTS_TABLE_NAME", "job-portal-cost-adjustments")

    def _table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)

    def _clean(item: dict) -> dict:
        item = dict(item)
        if "amount" in item:
            item["amount"] = _safe_amount(item["amount"])
        return item

    def list_adjustments(account_id: Optional[str] = None) -> list[dict]:
        table = _table()
        items: list[dict] = []
        if account_id:
            resp = table.query(KeyConditionExpression=Key("account_id").eq(account_id))
            items.extend(resp.get("Items", []))
            while "LastEvaluatedKey" in resp:
                resp = table.query(
                    KeyConditionExpression=Key("account_id").eq(account_id),
                    ExclusiveStartKey=resp["LastEvaluatedKey"],
                )
                items.extend(resp.get("Items", []))
        else:
            resp = table.scan()
            items.extend(resp.get("Items", []))
            while "LastEvaluatedKey" in resp:
                resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
                items.extend(resp.get("Items", []))
        cleaned = [_clean(i) for i in items]
        cleaned.sort(key=lambda a: (a.get("period", ""), a.get("created_at", "")), reverse=True)
        return cleaned

    def get_adjustment(adjustment_id: str) -> Optional[dict]:
        # id is not the partition key in the DynamoDB schema (account_id
        # is), so a full scan+filter is used here. Acceptable for this
        # low-volume, admin-curated table; revisit with a GSI on id if
        # adjustment volume ever grows significantly.
        table = _table()
        resp = table.scan(FilterExpression=Attr("id").eq(adjustment_id))
        items = resp.get("Items", [])
        return _clean(items[0]) if items else None

    def create_adjustment(
        *, account_id: str, period: str, label: str, amount: float, currency: str,
        note: Optional[str], actor_email: str, actor_name: str,
    ) -> dict:
        safe_amount = _safe_amount(amount)
        item = {
            "id": str(uuid.uuid4()),
            "account_id": account_id,
            "period": period,
            "label": label,
            "amount": str(safe_amount),
            "currency": currency,
            "note": note,
            "created_by_email": actor_email,
            "created_by_name": actor_name,
            "created_at": _now_iso(),
            "updated_by_email": None,
            "updated_by_name": None,
            "updated_at": _now_iso(),
        }
        _table().put_item(Item=item)
        return _clean(item)

    def update_adjustment(
        adjustment_id: str, *, label: Optional[str] = None, amount: Optional[float] = None,
        currency: Optional[str] = None, note: Optional[str] = None,
        actor_email: str, actor_name: str,
    ) -> Optional[dict]:
        existing = get_adjustment(adjustment_id)
        if not existing:
            return None
        table = _table()
        updates: dict = {
            "updated_by_email": actor_email,
            "updated_by_name": actor_name,
            "updated_at": _now_iso(),
        }
        if label is not None:
            updates["label"] = label
        if amount is not None:
            updates["amount"] = str(_safe_amount(amount))
        if currency is not None:
            updates["currency"] = currency
        if note is not None:
            updates["note"] = note

        update_expr_parts = []
        names = {}
        values = {}
        for i, (k, v) in enumerate(updates.items()):
            name_ph, value_ph = f"#f{i}", f":v{i}"
            update_expr_parts.append(f"{name_ph} = {value_ph}")
            names[name_ph] = k
            values[value_ph] = v
        resp = table.update_item(
            Key={"account_id": existing["account_id"], "id": adjustment_id},
            UpdateExpression="SET " + ", ".join(update_expr_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return _clean(dict(resp.get("Attributes", {})))

    def delete_adjustment(adjustment_id: str) -> bool:
        existing = get_adjustment(adjustment_id)
        if not existing:
            return False
        _table().delete_item(Key={"account_id": existing["account_id"], "id": adjustment_id})
        return True
