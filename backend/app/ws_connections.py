"""
Chat/Call feature (job-portal-chat-voice spec) — WebSocket connection
registry: which connection_id belongs to which Team_Member, refreshed
by a 15-second client heartbeat.

ENV=local: SQLite via SQLAlchemy (WsConnection model in database.py).
ENV!=local: DynamoDB — job-portal-ws-connections (PK connection_id, GSI
UserEmailIndex on user_email, TTL attribute "ttl" — see data_stack.py).

This module is the cross-task connection *registry* only — it never
holds an actual WebSocket object (that in-process map lives in
ws_manager.py, per ECS task). A task uses
list_connections_for_user(email) to discover which connection_id(s)
belong to a Team_Member and then checks its OWN in-process map to see
if it personally holds any of them.

Requirement 8.2 (30-second offline threshold): a connection is
considered stale — and therefore excluded from
list_connections_for_user()'s results — once
STALE_CONNECTION_SECONDS have passed since its last heartbeat. Per
design.md, this is set to 45 seconds (not exactly 30) to give
Requirement 8.2's 30-second window some margin against a heartbeat that
was merely slightly delayed rather than truly disconnected — a
connection is only ever falsely reported "online" for at most this
margin, never falsely reported "offline" while genuinely still alive.

Requirement 8.7 (multiple simultaneous connections per Team_Member):
this module has no notion of "the" connection for a user — every
function that reads by user_email always returns/considers ALL of that
user's currently-fresh connections, by design, so a caller (ws_manager.py's
fan-out dispatcher) can push to every one of them.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

ENV = os.environ.get("ENV", "local")

HEARTBEAT_INTERVAL_SECONDS = 15
STALE_CONNECTION_SECONDS = 45


def _new_id() -> str:
    return str(uuid.uuid4())


def _norm(email: str) -> str:
    return email.lower().strip()


if ENV == "local":
    from .database import SessionLocal, WsConnection as _ConnModel

    def _conn_to_dict(c) -> dict:
        return {
            "connection_id": c.connection_id,
            "user_email": c.user_email,
            "connected_at": c.connected_at.isoformat() if c.connected_at else None,
            "last_heartbeat_at": c.last_heartbeat_at.isoformat() if c.last_heartbeat_at else None,
        }

    def register_connection(*, user_email: str, connection_id: Optional[str] = None) -> dict:
        """Requirement 9.1: called immediately after a WS handshake has
        been authenticated (main.py's @app.websocket("/ws") route) —
        this function itself does not authenticate anything, it only
        records that an already-authenticated connection now exists."""
        now = datetime.utcnow()
        cid = connection_id or _new_id()
        db = SessionLocal()
        try:
            c = _ConnModel(
                connection_id=cid,
                user_email=_norm(user_email),
                connected_at=now,
                last_heartbeat_at=now,
            )
            db.add(c)
            db.commit()
            db.refresh(c)
            return _conn_to_dict(c)
        finally:
            db.close()

    def heartbeat(connection_id: str) -> bool:
        """Refresh last_heartbeat_at for an existing connection. Returns
        False if the connection_id is unknown (e.g. it was already
        unregistered, or never existed — the caller's WS loop should
        treat this as a signal to close and re-register)."""
        db = SessionLocal()
        try:
            c = db.query(_ConnModel).filter(_ConnModel.connection_id == connection_id).first()
            if not c:
                return False
            c.last_heartbeat_at = datetime.utcnow()
            db.commit()
            return True
        finally:
            db.close()

    def unregister_connection(connection_id: str) -> None:
        """Requirement 9.6 / a clean WS disconnect: remove the
        connection row immediately rather than waiting for staleness to
        age it out — this is what makes a CLEAN disconnect (logout, tab
        close where the WS close frame is delivered) reflect as
        "offline" faster than the 30-second Requirement 8.2 window,
        which exists to cover UNCLEAN disconnects (network loss) where
        no close frame ever arrives."""
        db = SessionLocal()
        try:
            c = db.query(_ConnModel).filter(_ConnModel.connection_id == connection_id).first()
            if c:
                db.delete(c)
                db.commit()
        finally:
            db.close()

    def list_connections_for_user(email: str) -> list[dict]:
        """Requirement 8.7: return every currently-fresh (non-stale)
        connection for this Team_Member — never just one. "Fresh" means
        last_heartbeat_at is within STALE_CONNECTION_SECONDS of now (see
        module docstring on Requirement 8.2's margin)."""
        want = _norm(email)
        cutoff = datetime.utcnow() - timedelta(seconds=STALE_CONNECTION_SECONDS)
        db = SessionLocal()
        try:
            rows = (
                db.query(_ConnModel)
                .filter(_ConnModel.user_email == want, _ConnModel.last_heartbeat_at >= cutoff)
                .all()
            )
            return [_conn_to_dict(r) for r in rows]
        finally:
            db.close()

    def is_user_online(email: str) -> bool:
        """Requirement 8.1: convenience wrapper — a Team_Member is
        "online" iff they have at least one fresh connection."""
        return len(list_connections_for_user(email)) > 0

    def unregister_all_connections_for_user(email: str) -> list[str]:
        """Requirement 9.6 (Task 11.1): bulk cross-task-safe cleanup for
        a session-expiry/forced-logout event — removes EVERY registry
        row for this Team_Member (not just the connection this specific
        HTTP request happens to be able to see), regardless of which
        ECS task actually holds each one's live WebSocket object.
        Returns the list of connection_id values that were removed, so
        the caller (main.py's /api/auth/logout) can also ask THIS
        task's own ws_manager.connection_manager to close any of them
        it happens to hold locally — see that route's docstring on why
        a connection living on a *different* task cannot be forcibly
        closed from here (no cross-task pub/sub broker exists per
        design.md's fan-out design; that task's own heartbeat timeout
        eventually reclaims it, same as any other unclean disconnect)."""
        want = _norm(email)
        db = SessionLocal()
        try:
            rows = db.query(_ConnModel).filter(_ConnModel.user_email == want).all()
            removed_ids = [r.connection_id for r in rows]
            for r in rows:
                db.delete(r)
            db.commit()
            return removed_ids
        finally:
            db.close()

    def get_presence_state(email: str, *, in_active_call: bool = False) -> str:
        """
        Requirement 8.1/8.2/8.3: combine "has a fresh WS_Connection" with
        an externally-supplied "is this Team_Member joined to an active
        Call right now" flag into the single Presence value the Portal
        displays. Deliberately takes in_active_call as a parameter
        rather than importing call_store.py directly — this module and
        call_store.py have no dependency on each other; the caller
        (a route handler, or a small presence-composition helper) is
        responsible for calling call_store.is_user_in_any_active_call()
        itself and passing the result in here, avoiding a circular
        import between the two modules.

        Returns "in_call" | "online" | "offline". "in_call" takes
        priority over "online" whenever both would otherwise apply
        (Requirement 8.3 — being joined to a Call is a more specific,
        higher-priority Presence state than merely being connected).
        """
        if in_active_call:
            return "in_call"
        if is_user_online(email):
            return "online"
        return "offline"

else:
    import boto3
    from boto3.dynamodb.conditions import Key

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    CONN_TABLE_NAME = os.environ.get("WS_CONNECTIONS_TABLE_NAME", "job-portal-ws-connections")

    def _table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(CONN_TABLE_NAME)

    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _ttl_epoch() -> int:
        return int((datetime.now(timezone.utc) + timedelta(seconds=STALE_CONNECTION_SECONDS)).timestamp())

    def register_connection(*, user_email: str, connection_id: Optional[str] = None) -> dict:
        cid = connection_id or _new_id()
        now = _now_iso()
        item = {
            "connection_id": cid,
            "user_email": _norm(user_email),
            "connected_at": now,
            "last_heartbeat_at": now,
            "ttl": _ttl_epoch(),
        }
        _table().put_item(Item=item)
        return item

    def heartbeat(connection_id: str) -> bool:
        """Refreshes last_heartbeat_at AND ttl — this is what keeps a
        genuinely-alive connection from being aged out by DynamoDB's own
        TTL-based deletion, which is the production-mode equivalent of
        local mode's staleness cutoff filter."""
        resp = _table().get_item(Key={"connection_id": connection_id})
        item = resp.get("Item")
        if not item:
            return False
        item["last_heartbeat_at"] = _now_iso()
        item["ttl"] = _ttl_epoch()
        _table().put_item(Item=item)
        return True

    def unregister_connection(connection_id: str) -> None:
        _table().delete_item(Key={"connection_id": connection_id})

    def list_connections_for_user(email: str) -> list[dict]:
        """Requirement 8.7: Query UserEmailIndex (NOT a scan) for every
        connection row belonging to this Team_Member. DynamoDB's own TTL
        deletion is asynchronous (can lag real-world expiry by up to
        ~48 hours in the worst case per AWS documentation), so this ALSO
        explicitly filters out any row whose stored ttl has technically
        already passed but has not yet been physically deleted — the
        Query result is filtered defensively rather than trusting TTL
        deletion to have already happened."""
        want = _norm(email)
        resp = _table().query(IndexName="UserEmailIndex", KeyConditionExpression=Key("user_email").eq(want))
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        return [item for item in resp.get("Items", []) if int(item.get("ttl", 0)) > now_epoch]

    def is_user_online(email: str) -> bool:
        return len(list_connections_for_user(email)) > 0

    def unregister_all_connections_for_user(email: str) -> list[str]:
        """See the local-mode branch's docstring above — identical
        bulk-cleanup contract. Deliberately does NOT rely on the ttl
        filter list_connections_for_user() applies (this must remove a
        row even if it happens to be right at its staleness boundary),
        so it queries UserEmailIndex directly rather than delegating."""
        want = _norm(email)
        resp = _table().query(IndexName="UserEmailIndex", KeyConditionExpression=Key("user_email").eq(want))
        items = resp.get("Items", [])
        removed_ids = [item["connection_id"] for item in items]
        for item in items:
            _table().delete_item(Key={"connection_id": item["connection_id"]})
        return removed_ids

    def get_presence_state(email: str, *, in_active_call: bool = False) -> str:
        """See the local-mode branch's docstring above — identical
        combination logic, no DynamoDB calls of its own."""
        if in_active_call:
            return "in_call"
        if is_user_online(email):
            return "online"
        return "offline"
