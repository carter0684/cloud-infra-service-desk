"""
User feedback / bug report persistence layer for the portal's
"Feedback & Bug Report" page.

ENV=local: SQLite via SQLAlchemy (FeedbackReport model in database.py).
ENV!=local: DynamoDB table job-portal-feedback-reports.

Visibility/reply permissions are NOT enforced here (this module is a pure
data layer) — they're enforced in main.py via _feedback_visible_to() /
_feedback_can_reply(), based on report_type:
  - "feedback": submitter + admins only.
  - "bug": admins only (even the submitter loses visibility after
    submitting).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Optional

ENV = os.environ.get("ENV", "local")


if ENV == "local":
    from .database import SessionLocal, FeedbackReport as _ReportModel

    def _to_dict(r) -> dict:
        return {
            "id": r.id,
            "report_type": r.report_type,
            "title": r.title,
            "description": r.description,
            "severity": r.severity,
            "page_url": r.page_url,
            "status": r.status,
            "submitted_by_email": r.submitted_by_email,
            "submitted_by_name": r.submitted_by_name,
            "replies": r.replies or [],
            "ai_analysis": r.ai_analysis,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }

    def list_reports() -> list[dict]:
        db = SessionLocal()
        try:
            rows = db.query(_ReportModel).order_by(_ReportModel.created_at.desc()).all()
            return [_to_dict(r) for r in rows]
        finally:
            db.close()

    def get_report(report_id: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            r = db.query(_ReportModel).filter(_ReportModel.id == report_id).first()
            return _to_dict(r) if r else None
        finally:
            db.close()

    def create_report(
        *,
        report_type: str,
        title: str,
        description: str,
        severity: Optional[str],
        page_url: Optional[str],
        submitted_by_email: str,
        submitted_by_name: str,
        ai_analysis: Optional[dict] = None,
    ) -> dict:
        db = SessionLocal()
        try:
            r = _ReportModel(
                id=str(uuid.uuid4()),
                report_type=report_type,
                title=title,
                description=description,
                severity=severity,
                page_url=page_url,
                status="Open",
                submitted_by_email=submitted_by_email,
                submitted_by_name=submitted_by_name,
                replies=[],
                ai_analysis=ai_analysis,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(r)
            db.commit()
            db.refresh(r)
            return _to_dict(r)
        finally:
            db.close()

    def set_ai_analysis(report_id: str, ai_analysis: Optional[dict]) -> Optional[dict]:
        """Replace only the AI triage note; does not touch updated_at (the
        report itself hasn't changed) so it can't reorder the list."""
        db = SessionLocal()
        try:
            r = db.query(_ReportModel).filter(_ReportModel.id == report_id).first()
            if not r:
                return None
            from sqlalchemy.orm.attributes import flag_modified

            r.ai_analysis = ai_analysis
            # updated_at has onupdate=utcnow; explicitly include it with its
            # current value so this UPDATE doesn't bump it.
            flag_modified(r, "updated_at")
            db.commit()
            db.refresh(r)
            return _to_dict(r)
        finally:
            db.close()

    def update_status(report_id: str, status: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            r = db.query(_ReportModel).filter(_ReportModel.id == report_id).first()
            if not r:
                return None
            r.status = status
            r.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(r)
            return _to_dict(r)
        finally:
            db.close()

    def add_reply(
        report_id: str,
        *,
        author_email: str,
        author_name: str,
        is_admin: bool,
        text: str,
    ) -> Optional[dict]:
        db = SessionLocal()
        try:
            r = db.query(_ReportModel).filter(_ReportModel.id == report_id).first()
            if not r:
                return None
            replies = list(r.replies or [])
            replies.append({
                "id": str(uuid.uuid4()),
                "author_email": author_email,
                "author_name": author_name,
                "is_admin": is_admin,
                "text": text,
                "created_at": datetime.utcnow().isoformat(),
            })
            r.replies = replies
            r.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(r)
            return _to_dict(r)
        finally:
            db.close()

    def delete_report(report_id: str) -> bool:
        db = SessionLocal()
        try:
            r = db.query(_ReportModel).filter(_ReportModel.id == report_id).first()
            if not r:
                return False
            db.delete(r)
            db.commit()
            return True
        finally:
            db.close()

else:
    import boto3
    from datetime import timezone

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    TABLE_NAME = os.environ.get("FEEDBACK_REPORTS_TABLE_NAME", "job-portal-feedback-reports")

    def _table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)

    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def list_reports() -> list[dict]:
        table = _table()
        resp = table.scan()
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        items.sort(key=lambda i: i.get("created_at") or "", reverse=True)
        return items

    def get_report(report_id: str) -> Optional[dict]:
        resp = _table().get_item(Key={"id": report_id})
        return resp.get("Item")

    def create_report(
        *,
        report_type: str,
        title: str,
        description: str,
        severity: Optional[str],
        page_url: Optional[str],
        submitted_by_email: str,
        submitted_by_name: str,
        ai_analysis: Optional[dict] = None,
    ) -> dict:
        now = _now_iso()
        item = {
            "id": str(uuid.uuid4()),
            "report_type": report_type,
            "title": title,
            "description": description,
            "severity": severity,
            "page_url": page_url,
            "status": "Open",
            "submitted_by_email": submitted_by_email,
            "submitted_by_name": submitted_by_name,
            "replies": [],
            "created_at": now,
            "updated_at": now,
        }
        if ai_analysis is not None:
            item["ai_analysis"] = ai_analysis
        _table().put_item(Item=item)
        return item

    def set_ai_analysis(report_id: str, ai_analysis: Optional[dict]) -> Optional[dict]:
        """Replace only the AI triage note. A targeted UpdateItem (not a
        read-modify-write of the whole item) so a reply added while the
        model was running isn't overwritten, and updated_at is untouched.
        Returns None if the report doesn't exist."""
        from botocore.exceptions import ClientError

        try:
            resp = _table().update_item(
                Key={"id": report_id},
                UpdateExpression="SET ai_analysis = :a",
                ConditionExpression="attribute_exists(id)",
                ExpressionAttributeValues={":a": ai_analysis},
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        return resp.get("Attributes")

    def update_status(report_id: str, status: str) -> Optional[dict]:
        existing = get_report(report_id)
        if not existing:
            return None
        existing["status"] = status
        existing["updated_at"] = _now_iso()
        _table().put_item(Item=existing)
        return existing

    def add_reply(
        report_id: str,
        *,
        author_email: str,
        author_name: str,
        is_admin: bool,
        text: str,
    ) -> Optional[dict]:
        existing = get_report(report_id)
        if not existing:
            return None
        replies = list(existing.get("replies") or [])
        replies.append({
            "id": str(uuid.uuid4()),
            "author_email": author_email,
            "author_name": author_name,
            "is_admin": is_admin,
            "text": text,
            "created_at": _now_iso(),
        })
        existing["replies"] = replies
        existing["updated_at"] = _now_iso()
        _table().put_item(Item=existing)
        return existing

    def delete_report(report_id: str) -> bool:
        existing = get_report(report_id)
        if not existing:
            return False
        _table().delete_item(Key={"id": report_id})
        return True
