"""
Backend-agnostic job / KB-document persistence layer.

ENV=local (default): backed by SQLite via SQLAlchemy (database.py) — the
original local-dev behavior, unchanged.
ENV != local (production): backed by DynamoDB (dynamo_repository.py),
against the tables created by job-portal/infra-cdk's DataStack.

main.py's route handlers call only the functions in this module — never
database.py or dynamo_repository.py directly — so the same route logic
(validation, audit-log construction, notification calls) works unmodified
against either backend. This is the single place that branches on ENV for
persistence; main.py stays backend-agnostic.

All functions here take/return plain dicts shaped like the original
job_to_dict()/kb_doc_to_dict() outputs, regardless of backend.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Optional

ENV = os.environ.get("ENV", "local")

# Priority urgency order (ASAP most urgent) — used so sorting by
# "priority" ranks by real urgency instead of alphabetically. Mirrors
# main.py's PRIORITY_RANK; kept as its own copy here to avoid a circular
# import (main.py imports this module, not the other way around).
_PRIORITY_RANK = {"ASAP": 0, "High": 1, "Medium": 2, "Low": 3}


if ENV == "local":
    # ── Local dev: SQLite via SQLAlchemy ────────────────────────────────────
    from .database import SessionLocal, Job as _JobModel, KBDocument as _KBModel, TicketCounter as _CounterModel

    def _next_ticket_number_local(db) -> int:
        """Allocate the next sequential ticket number, starting at 1001.
        Single-process SQLite, so a plain read-increment-write inside the
        caller's transaction is sufficient (no concurrent writers)."""
        counter = db.query(_CounterModel).filter(_CounterModel.id == 1).first()
        if not counter:
            counter = _CounterModel(id=1, value=1000)
            db.add(counter)
            db.flush()
        counter.value += 1
        db.flush()
        return counter.value

    def _job_to_dict(j) -> dict:
        return {
            "id": j.id,
            "ticket_number": j.ticket_number,
            "title": j.title,
            "description": j.description,
            "category": j.category,
            "category_other": j.category_other,
            "priority": j.priority,
            "assignee_email": j.assignee_email,
            "assignee_name": j.assignee_name,
            "created_by_email": j.created_by_email,
            "created_by_name": j.created_by_name,
            "due_date": j.due_date,
            "status": j.status,
            "ai_draft": j.ai_draft,
            "ai_analysed_at": j.ai_analysed_at,
            "ai_analysed_by": j.ai_analysed_by,
            "aws_support_case_id": j.aws_support_case_id,
            "aws_support_case_display_id": getattr(j, "aws_support_case_display_id", None),
            "aws_ams_sr_case_id": j.aws_ams_sr_case_id,
            "aws_ams_sr_case_display_id": getattr(j, "aws_ams_sr_case_display_id", None),
            "aws_ams_sr_case_status": j.aws_ams_sr_case_status,
            "aws_ams_incident_case_id": j.aws_ams_incident_case_id,
            "aws_ams_incident_case_display_id": getattr(j, "aws_ams_incident_case_display_id", None),
            "aws_ams_incident_case_status": j.aws_ams_incident_case_status,
            "freshservice_ticket_id": j.freshservice_ticket_id,
            "freshservice_ticket_url": j.freshservice_ticket_url,
            "audit_log": j.audit_log or [],
            "notes": j.notes or [],
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "updated_at": j.updated_at.isoformat() if j.updated_at else None,
        }

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
        db = SessionLocal()
        try:
            query = db.query(_JobModel)
            if status:
                query = query.filter(_JobModel.status.in_(status))
            if priority:
                query = query.filter(_JobModel.priority.in_(priority))
            if category:
                query = query.filter(_JobModel.category.in_(category))
            if assignee:
                query = query.filter(_JobModel.assignee_email.in_(assignee))
            if q:
                like = f"%{q}%"
                query = query.filter(_JobModel.title.ilike(like) | _JobModel.description.ilike(like))
            total = query.count()

            # "priority" sorts by urgency rank (ASAP first when ascending)
            # rather than alphabetically — everything else sorts on its
            # own column directly.
            if sort_by == "priority":
                from sqlalchemy import case
                sort_col = case(
                    *[(_JobModel.priority == p, rank) for p, rank in _PRIORITY_RANK.items()],
                    else_=99,
                )
            else:
                sort_col = getattr(_JobModel, sort_by, _JobModel.created_at)
            # nulls_last() so tickets missing the sort field (e.g. no
            # due_date) always sink to the bottom instead of jumping to
            # the top on ascending sorts — matches the Dynamo-backed
            # list_jobs()'s equivalent behavior below.
            sort_col = sort_col.asc().nulls_last() if sort_order == "asc" else sort_col.desc().nulls_last()

            jobs = (
                query.order_by(sort_col)
                .offset((page - 1) * per_page)
                .limit(per_page)
                .all()
            )
            return {
                "total": total,
                "page": page,
                "per_page": per_page,
                "jobs": [_job_to_dict(j) for j in jobs],
            }
        finally:
            db.close()

    def get_job(job_id: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            j = db.query(_JobModel).filter(_JobModel.id == job_id).first()
            return _job_to_dict(j) if j else None
        finally:
            db.close()

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
        db = SessionLocal()
        try:
            ticket_number = _next_ticket_number_local(db)
            j = _JobModel(
                id=str(uuid.uuid4()),
                ticket_number=ticket_number,
                title=title,
                description=description,
                category=category,
                category_other=category_other,
                priority=priority,
                assignee_email=assignee_email,
                assignee_name=assignee_name,
                created_by_email=created_by_email,
                created_by_name=created_by_name,
                due_date=due_date,
                status="Open",
                audit_log=audit_log,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(j)
            db.commit()
            db.refresh(j)
            return _job_to_dict(j)
        finally:
            db.close()

    def update_job_fields(job_id: str, fields: dict) -> Optional[dict]:
        db = SessionLocal()
        try:
            j = db.query(_JobModel).filter(_JobModel.id == job_id).first()
            if not j:
                return None
            for key, value in fields.items():
                setattr(j, key, value)
            j.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(j)
            return _job_to_dict(j)
        finally:
            db.close()

    def delete_job(job_id: str) -> None:
        db = SessionLocal()
        try:
            j = db.query(_JobModel).filter(_JobModel.id == job_id).first()
            if j:
                db.delete(j)
                db.commit()
        finally:
            db.close()

    def build_case_to_job_map() -> dict:
        """Local/SQLite equivalent of dynamo_repository.build_case_to_job_map()
        — scans every job and maps each of its 3 possible case-id fields to
        a lightweight job reference."""
        db = SessionLocal()
        try:
            jobs = db.query(_JobModel).all()
            case_map = {}
            for j in jobs:
                job_ref = {
                    "job_id": j.id,
                    "ticket_number": j.ticket_number,
                    "title": j.title,
                    "status": j.status,
                    # Included so /api/cases can fall back to "who created
                    # the linked Job" as a best-effort "raised by" when the
                    # case predates standalone_case_store tracking.
                    "created_by_email": j.created_by_email,
                    "created_by_name": j.created_by_name,
                }
                for field in ("aws_support_case_id", "aws_ams_sr_case_id", "aws_ams_incident_case_id"):
                    case_id = getattr(j, field, None)
                    if case_id:
                        case_map[case_id] = job_ref
            return case_map
        finally:
            db.close()

    def _kb_to_dict(d) -> dict:
        return {
            "id": d.id,
            "filename": d.filename,
            "file_type": d.file_type,
            "description": d.description,
            "char_count": d.char_count,
            "chunk_count": d.chunk_count,
            "chunks": d.chunks or [],
            "uploaded_by": d.uploaded_by,
            "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
        }

    def list_kb_documents() -> list[dict]:
        db = SessionLocal()
        try:
            docs = db.query(_KBModel).order_by(_KBModel.uploaded_at.desc()).all()
            return [_kb_to_dict(d) for d in docs]
        finally:
            db.close()

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
        db = SessionLocal()
        try:
            doc = _KBModel(
                filename=filename,
                file_type=file_type,
                description=description,
                char_count=char_count,
                chunk_count=chunk_count,
                chunks=chunks,
                uploaded_by=uploaded_by,
                uploaded_at=datetime.utcnow(),
            )
            db.add(doc)
            db.commit()
            db.refresh(doc)
            return _kb_to_dict(doc)
        finally:
            db.close()

    def get_kb_document(doc_id: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            d = db.query(_KBModel).filter(_KBModel.id == doc_id).first()
            return _kb_to_dict(d) if d else None
        finally:
            db.close()

    def delete_kb_document(doc_id: str) -> bool:
        db = SessionLocal()
        try:
            d = db.query(_KBModel).filter(_KBModel.id == doc_id).first()
            if not d:
                return False
            db.delete(d)
            db.commit()
            return True
        finally:
            db.close()

else:
    # ── Production: DynamoDB via dynamo_repository ──────────────────────────
    from . import dynamo_repository as _repo

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
        return _repo.list_jobs(
            status=status, priority=priority, category=category,
            assignee=assignee, q=q, page=page, per_page=per_page,
            sort_by=sort_by, sort_order=sort_order,
        )

    def get_job(job_id: str) -> Optional[dict]:
        return _repo.get_job(job_id)

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
        return _repo.create_job(
            title=title, description=description, category=category,
            category_other=category_other, priority=priority,
            assignee_email=assignee_email, assignee_name=assignee_name,
            created_by_email=created_by_email, created_by_name=created_by_name,
            due_date=due_date, audit_log=audit_log,
        )

    def update_job_fields(job_id: str, fields: dict) -> Optional[dict]:
        return _repo.update_job_fields(job_id, fields)

    def delete_job(job_id: str) -> None:
        _repo.delete_job(job_id)

    def build_case_to_job_map() -> dict:
        return _repo.build_case_to_job_map()

    def list_kb_documents() -> list[dict]:
        return _repo.list_kb_documents()

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
        return _repo.create_kb_document(
            filename=filename, file_type=file_type, description=description,
            char_count=char_count, chunk_count=chunk_count, chunks=chunks,
            uploaded_by=uploaded_by,
        )

    def get_kb_document(doc_id: str) -> Optional[dict]:
        return _repo.get_kb_document(doc_id)

    def delete_kb_document(doc_id: str) -> bool:
        existing = _repo.get_kb_document(doc_id)
        if not existing:
            return False
        _repo.delete_kb_document(doc_id)
        return True
