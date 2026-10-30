"""
Bookkeeping for AWS cases raised directly from the AWS Cases page's
"Create AWS Case" button — i.e. NOT via a Job's Escalation Actions.

The real case lives in AWS Support (own account or the AMS support
account 100000000001) the moment escalation.py's raise_aws_support_case()/
raise_aws_ams_case() succeeds; /api/cases already lists every case in
both accounts regardless of how it was created. This store exists only
so:
  - ENV=local dev (no real AWS Support account to query) has a case to
    show on the AWS Cases page after "creating" one, same convention as
    Job-linked cases' _local_mock_cases() in main.py.
  - the portal can show "raised via portal by <name>" attribution
    without depending on AWS Support's own free-text fields.

ENV=local: SQLite via SQLAlchemy (StandaloneCase model in database.py).
ENV!=local: DynamoDB table job-portal-standalone-cases, partition key
`case_id` (small, low-volume table — a portal-created case only, not a
mirror of every AWS Support case).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

ENV = os.environ.get("ENV", "local")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if ENV == "local":
    from .database import SessionLocal, StandaloneCase as _CaseModel

    def _to_dict(c) -> dict:
        return {
            "case_id": c.case_id, "account": c.account, "case_type": c.case_type,
            "subject": c.subject, "category": c.category, "priority": c.priority,
            "display_id": getattr(c, "display_id", None),
            "raised_by_email": c.raised_by_email, "raised_by_name": c.raised_by_name,
            "created_at": c.created_at,
        }

    def record_case(
        case_id: str, *, account: str, case_type: str, subject: str,
        category: Optional[str], priority: Optional[str],
        actor_email: str, actor_name: str, display_id: Optional[str] = None,
    ) -> dict:
        db = SessionLocal()
        try:
            row = _CaseModel(
                case_id=case_id, account=account, case_type=case_type, subject=subject,
                category=category, priority=priority, display_id=display_id,
                raised_by_email=actor_email, raised_by_name=actor_name,
                created_at=_now_iso(),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _to_dict(row)
        finally:
            db.close()

    def list_cases() -> list[dict]:
        db = SessionLocal()
        try:
            rows = db.query(_CaseModel).all()
            return [_to_dict(r) for r in rows]
        finally:
            db.close()

    def get_case(case_id: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            row = db.query(_CaseModel).filter(_CaseModel.case_id == case_id).first()
            return _to_dict(row) if row else None
        finally:
            db.close()

else:
    import boto3

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    TABLE_NAME = os.environ.get("STANDALONE_CASES_TABLE_NAME", "job-portal-standalone-cases")

    def _table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)

    def record_case(
        case_id: str, *, account: str, case_type: str, subject: str,
        category: Optional[str], priority: Optional[str],
        actor_email: str, actor_name: str, display_id: Optional[str] = None,
    ) -> dict:
        item = {
            "case_id": case_id, "account": account, "case_type": case_type, "subject": subject,
            "category": category, "priority": priority, "display_id": display_id,
            "raised_by_email": actor_email, "raised_by_name": actor_name,
            "created_at": _now_iso(),
        }
        _table().put_item(Item=item)
        return item

    def list_cases() -> list[dict]:
        table = _table()
        resp = table.scan()
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        return [dict(i) for i in items]

    def get_case(case_id: str) -> Optional[dict]:
        resp = _table().get_item(Key={"case_id": case_id})
        item = resp.get("Item")
        return dict(item) if item else None
