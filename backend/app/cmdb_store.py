"""
CMDB history/snapshot store — records when resources are added, removed,
or change status, by diffing each fresh inventory fetch against the last
known snapshot for that account.

This is intentionally separate from cmdb.py's live-fetch path: cmdb.py
always returns whatever it fetched from AWS right now (or the cache),
regardless of whether the history write below succeeds — a DynamoDB/
SQLite hiccup here must never make the live inventory page fail to load.

ENV=local: SQLite via SQLAlchemy (CmdbSnapshot / CmdbHistoryEvent models
in database.py).
ENV!=local: DynamoDB, two tables:
  - job-portal-cmdb-snapshots   (partition key: account_id) — one row per
    account holding its current full resource list, overwritten on every
    successful fetch.
  - job-portal-cmdb-history     (partition key: account_id, sort key: id)
    — append-only audit trail of added/removed/status_changed events.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

ENV = os.environ.get("ENV", "local")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resource_key(resource: dict) -> tuple:
    """Identity key for diffing — a resource is "the same" across two
    fetches if its type + id match, regardless of region (a resource
    practically never moves region, but if it somehow did this treats it
    as removed+added rather than a false "status changed", which is the
    more useful signal for an audit trail)."""
    return (resource.get("resource_type"), resource.get("resource_id"))


def _diff_resources(previous: list[dict], current: list[dict]) -> list[dict]:
    """Compare two resource-row lists and return a list of change-event
    dicts ({event_type, resource_type, resource_id, name, region, status,
    previous_status}). Pure function, no I/O — kept separate from the
    store's persistence layer so it's independently unit-testable."""
    prev_by_key = {_resource_key(r): r for r in (previous or [])}
    curr_by_key = {_resource_key(r): r for r in (current or [])}

    events: list[dict] = []

    for key, curr in curr_by_key.items():
        prev = prev_by_key.get(key)
        if prev is None:
            events.append({
                "event_type": "added",
                "resource_type": curr.get("resource_type"),
                "resource_id": curr.get("resource_id"),
                "name": curr.get("name"),
                "region": curr.get("region"),
                "status": curr.get("status"),
                "previous_status": None,
            })
        elif prev.get("status") != curr.get("status"):
            events.append({
                "event_type": "status_changed",
                "resource_type": curr.get("resource_type"),
                "resource_id": curr.get("resource_id"),
                "name": curr.get("name"),
                "region": curr.get("region"),
                "status": curr.get("status"),
                "previous_status": prev.get("status"),
            })

    for key, prev in prev_by_key.items():
        if key not in curr_by_key:
            events.append({
                "event_type": "removed",
                "resource_type": prev.get("resource_type"),
                "resource_id": prev.get("resource_id"),
                "name": prev.get("name"),
                "region": prev.get("region"),
                "status": None,
                "previous_status": prev.get("status"),
            })

    return events


if ENV == "local":
    from .database import SessionLocal, CmdbSnapshot as _SnapshotModel, CmdbHistoryEvent as _HistoryModel

    def get_snapshot(account_id: str) -> list[dict]:
        db = SessionLocal()
        try:
            row = db.query(_SnapshotModel).filter(_SnapshotModel.account_id == account_id).first()
            return list(row.resources or []) if row else []
        finally:
            db.close()

    def get_all_snapshots() -> dict:
        """account_id -> resources list, for every account with a saved
        snapshot. Used by cmdb.py to hydrate a cold in-process cache from
        durable storage (see get_all_snapshots' production/DynamoDB
        counterpart's docstring below for why this matters)."""
        db = SessionLocal()
        try:
            rows = db.query(_SnapshotModel).all()
            return {r.account_id: list(r.resources or []) for r in rows}
        finally:
            db.close()

    def _save_snapshot(account_id: str, resources: list[dict]) -> None:
        db = SessionLocal()
        try:
            row = db.query(_SnapshotModel).filter(_SnapshotModel.account_id == account_id).first()
            if row:
                row.resources = resources
            else:
                row = _SnapshotModel(account_id=account_id, resources=resources)
                db.add(row)
            db.commit()
        finally:
            db.close()

    def _record_events(account_id: str, events: list[dict]) -> None:
        if not events:
            return
        db = SessionLocal()
        try:
            for e in events:
                db.add(_HistoryModel(
                    id=str(uuid.uuid4()), account_id=account_id,
                    event_type=e["event_type"], resource_type=e["resource_type"],
                    resource_id=e["resource_id"], name=e.get("name"), region=e.get("region"),
                    status=e.get("status"), previous_status=e.get("previous_status"),
                ))
            db.commit()
        finally:
            db.close()

    def list_history(account_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        db = SessionLocal()
        try:
            q = db.query(_HistoryModel)
            if account_id:
                q = q.filter(_HistoryModel.account_id == account_id)
            rows = q.order_by(_HistoryModel.timestamp.desc()).limit(limit).all()
            return [
                {
                    "id": r.id, "account_id": r.account_id, "event_type": r.event_type,
                    "resource_type": r.resource_type, "resource_id": r.resource_id,
                    "name": r.name, "region": r.region, "status": r.status,
                    "previous_status": r.previous_status,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                }
                for r in rows
            ]
        finally:
            db.close()

    def reset_for_tests() -> None:
        """Test-only helper: wipe both CMDB tables so each test starts
        clean. Not used by production code paths."""
        db = SessionLocal()
        try:
            db.query(_HistoryModel).delete()
            db.query(_SnapshotModel).delete()
            db.commit()
        finally:
            db.close()

else:
    import boto3
    from boto3.dynamodb.conditions import Key

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    SNAPSHOTS_TABLE_NAME = os.environ.get("CMDB_SNAPSHOTS_TABLE_NAME", "job-portal-cmdb-snapshots")
    HISTORY_TABLE_NAME = os.environ.get("CMDB_HISTORY_TABLE_NAME", "job-portal-cmdb-history")

    def _snapshots_table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(SNAPSHOTS_TABLE_NAME)

    def _history_table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(HISTORY_TABLE_NAME)

    def get_snapshot(account_id: str) -> list[dict]:
        resp = _snapshots_table().get_item(Key={"account_id": account_id})
        item = resp.get("Item")
        return list(item.get("resources") or []) if item else []

    def get_all_snapshots() -> dict:
        """account_id -> resources list, for every account with a saved
        snapshot in DynamoDB.

        Why this exists: cmdb.py's live-inventory cache is a plain
        in-process dict, but this backend runs as 2+ ECS tasks behind an
        ALB with no session stickiness — a request that lands on task A
        sees whatever task A has fetched, but a poll that lands on task B
        sees task B's own (possibly still-empty) cache, even after task
        A has already finished a full org-wide fetch. This surfaced as
        the CMDB page showing "no accounts / 0 resources" indefinitely
        while the History drawer (which reads this same DynamoDB table
        directly, not the in-process cache) correctly showed data — proof
        the fetch itself succeeded, just on a *different* task's memory.
        cmdb.py calls this to seed its own empty/stale in-process cache
        with the last known-good state that ANY task already
        successfully wrote to this shared table, so a cold/never-fetched
        task doesn't have to wait through its own ~15-minute scan (or
        keep returning "pending" forever) before it can serve something
        useful."""
        table = _snapshots_table()
        items: list[dict] = []
        resp = table.scan()
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        return {item["account_id"]: list(item.get("resources") or []) for item in items}

    def _save_snapshot(account_id: str, resources: list[dict]) -> None:
        _snapshots_table().put_item(Item={
            "account_id": account_id,
            "resources": resources,
            "updated_at": _now_iso(),
        })

    def _record_events(account_id: str, events: list[dict]) -> None:
        if not events:
            return
        table = _history_table()
        for e in events:
            table.put_item(Item={
                "account_id": account_id,
                "id": str(uuid.uuid4()),
                "event_type": e["event_type"],
                "resource_type": e["resource_type"],
                "resource_id": e["resource_id"],
                "name": e.get("name"),
                "region": e.get("region"),
                "status": e.get("status"),
                "previous_status": e.get("previous_status"),
                "timestamp": _now_iso(),
            })

    def list_history(account_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        table = _history_table()
        items: list[dict] = []
        if account_id:
            resp = table.query(
                KeyConditionExpression=Key("account_id").eq(account_id),
                ScanIndexForward=False,
                Limit=limit,
            )
            items.extend(resp.get("Items", []))
        else:
            # Low-volume audit table (bounded by resource churn, not by
            # request rate) — a scan is acceptable here, same tradeoff
            # cost_adjustment_store.py makes for its own admin-curated
            # "list across all accounts" path.
            resp = table.scan()
            items.extend(resp.get("Items", []))
            while "LastEvaluatedKey" in resp and len(items) < limit:
                resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
                items.extend(resp.get("Items", []))
        items.sort(key=lambda i: i.get("timestamp", ""), reverse=True)
        return items[:limit]

    def reset_for_tests() -> None:  # pragma: no cover - DynamoDB path has no test double here
        raise NotImplementedError("reset_for_tests() is only available in ENV=local")


def diff_and_record(account_id: str, current_resources: list[dict]) -> list[dict]:
    """Compare `current_resources` (this fetch's result for one account)
    against the last saved snapshot for that account, record any
    added/removed/status_changed events, then overwrite the snapshot with
    the current state. Returns the list of events recorded (possibly
    empty). Called once per account per successful fetch by
    cmdb.get_cmdb_inventory() — never called for accounts that errored
    this fetch, so a transient AWS failure never gets misread as "every
    resource in this account was removed"."""
    previous = get_snapshot(account_id)
    events = _diff_resources(previous, current_resources)
    _record_events(account_id, events)
    _save_snapshot(account_id, current_resources)
    return events
