"""
Chat/Call feature (job-portal-chat-voice spec) — in-process WebSocket
connection map (per ECS task) + DynamoDB Streams poller/dispatcher that
implements the cross-task fan-out mechanism described in design.md's
"Key design decision: cross-task message and signaling fan-out" section.

Why this exists: the ECS service runs 2-6 Fargate tasks behind the ALB
with no target-group stickiness, so two Team_Members' WebSocket
connections can land on different tasks. Rather than adding a new
message broker (SNS/SQS/ElastiCache) for a 3-10 user internal tool,
DynamoDB itself is the fan-out mechanism: every task's Streams poller
sees every new/edited Message and every Call signaling event, but each
task only ever pushes to the WebSocket connections it PERSONALLY holds
in its own in-process ConnectionManager — recipients connected to a
different task are reached because THAT task's own poller also saw the
same stream record and pushes to its own local connections.

Two separable concerns, deliberately kept in separate classes/functions
so each is independently testable without a real DynamoDB Streams
connection:
  - ConnectionManager: the in-process connection_id -> WebSocket map
    (register/unregister/send_to_connection/send_to_user). Pure,
    synchronous-registration + async-send, no AWS calls.
  - resolve_recipients_for_message()/resolve_recipients_for_call():
    pure functions that decide WHO should receive a given stream
    record's event — takes the record's "NewImage" dict (already
    dict-of-python-values, not raw DynamoDB AttributeValue-typed JSON)
    and returns the set of recipient emails. Kept pure and separate
    from any AWS/WebSocket I/O so they are trivially unit-testable.
  - dispatch_stream_record(): glues the two together — resolves
    recipients, looks up their connections via
    ws_connections.list_connections_for_user(), and pushes to any of
    those connection_ids this process's ConnectionManager holds
    locally. This is the function every test in test_ws_manager.py
    exercises directly, without needing a real DynamoDB Stream.
  - The actual boto3 dynamodbstreams polling loop
    (_poll_stream_forever()/start_stream_pollers()) is a thin outer
    layer on top of dispatch_stream_record() — started once from
    main.py's FastAPI startup event, one poller task per table stream.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from . import ws_connections

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
STREAM_POLL_INTERVAL_SECONDS = 2


class ConnectionManager:
    """
    Per-ECS-task, in-process registry of connection_id -> the actual
    WebSocket object main.py's @app.websocket("/ws") handler is holding
    for that connection. This is DELIBERATELY separate from
    ws_connections.py's cross-task DynamoDB-backed registry — that
    module answers "which connection_id(s) belong to this user, across
    ALL tasks"; this class answers "do I, this specific process,
    personally hold a live WebSocket for this connection_id right now".

    Not thread-safe by design — FastAPI/Starlette's WebSocket handling
    runs on a single asyncio event loop per process, so no locking is
    needed for a plain dict here.
    """

    def __init__(self) -> None:
        self._connections: dict[str, object] = {}

    def register(self, connection_id: str, websocket) -> None:
        self._connections[connection_id] = websocket

    def unregister(self, connection_id: str) -> None:
        self._connections.pop(connection_id, None)

    async def close_connection(self, connection_id: str, *, code: int = 1000) -> bool:
        """Task 11.1 (session-expiry cleanup): actively close a locally-
        held WebSocket (sending a real WS close frame), THEN forget it
        locally — unlike unregister() alone, which only forgets the
        Python-side reference without telling the client anything, this
        is what main.py's /api/auth/logout cleanup path needs so a
        Team_Member whose session just expired/signed-out actually sees
        their WS connection close rather than it silently going stale.
        Returns False (no-op) if this process doesn't locally hold the
        given connection_id — the caller (e.g. a different ECS task
        holding this same user's OTHER connection) is expected to reach
        this same code path on its own next opportunity instead; see
        module docstring on why no cross-task close exists."""
        ws = self._connections.get(connection_id)
        if ws is None:
            return False
        try:
            await ws.close(code=code)
        except Exception:
            logger.warning("ws_manager: close_connection failed for %s — forgetting it locally anyway", connection_id)
        finally:
            self.unregister(connection_id)
        return True

    def has_connection(self, connection_id: str) -> bool:
        return connection_id in self._connections

    def local_connection_ids(self) -> set[str]:
        return set(self._connections.keys())

    async def send_to_connection(self, connection_id: str, payload: dict) -> bool:
        """Push a JSON-able payload to one locally-held connection.
        Returns False (and cleans up the dead entry) if the send fails —
        the WebSocket is presumed to have died without a clean
        unregister() call, which the route handler's own disconnect
        path will also eventually reach, but this proactively avoids
        repeatedly trying to send to a known-dead socket in the
        meantime."""
        ws = self._connections.get(connection_id)
        if ws is None:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            logger.warning("ws_manager: send failed for connection %s — dropping it locally", connection_id)
            self.unregister(connection_id)
            return False

    async def send_to_local_connections(self, connection_ids: set[str], payload: dict) -> int:
        """Push to every one of the given connection_ids that this
        process personally holds locally (silently skips any that
        aren't — they belong to a different ECS task, which will
        deliver via its own copy of this same dispatch logic). Returns
        the number of connections actually pushed to."""
        sent = 0
        for cid in connection_ids & self.local_connection_ids():
            if await self.send_to_connection(cid, payload):
                sent += 1
        return sent


# One ConnectionManager per ECS task process — module-level singleton,
# imported by main.py's WebSocket route handler.
connection_manager = ConnectionManager()


def resolve_recipients_for_message(message_item: dict, *, conversation_participants: list[str]) -> set[str]:
    """
    Requirement 2.2/2.3: who should receive a new/edited Message
    real-time push. Everyone currently a participant of the
    conversation EXCEPT the sender themselves (a sender already has
    their own optimistic local copy from the REST response that created
    the Message; re-pushing it to them over the WS is harmless but
    unnecessary — excluded here to keep the fan-out minimal).

    conversation_participants must be looked up by the caller (from
    chat_store.get_conversation()) — this function stays pure and takes
    it as an argument rather than importing chat_store itself, so it has
    zero AWS/DB dependencies and is trivially unit-testable.
    """
    sender = (message_item.get("sender_email") or "").lower().strip()
    return {p.lower().strip() for p in conversation_participants if p.lower().strip() != sender}


def resolve_recipients_for_call(call_item: dict) -> set[str]:
    """
    Requirement 5.1/6.1/6.4/8.6: who should receive a Call signaling
    event (ring/roster/accept/decline/timeout) push — every invited
    participant (regardless of their current state — a roster update
    is relevant to ringing/joined/declined/timed_out/left participants
    alike, e.g. so a declined participant's client still gets the final
    "someone else joined" roster update if it stays open) plus the
    caller themselves (the caller needs live roster updates too, even
    though they are not in the participants map).

    Per design.md: stream records for the terminal "ended" write are
    NOT forwarded — nothing to push, since every relevant client either
    already knows the call ended (they triggered the transition
    themselves) or was already told via the specific accept/decline/
    leave response that ended it. dispatch_stream_record() is
    responsible for checking status=="ended" and skipping the call
    entirely BEFORE calling this function; this function itself does
    not re-check status, so it must not be called directly for an
    "ended" record — see dispatch_stream_record()'s docstring.
    """
    caller = (call_item.get("caller_email") or "").lower().strip()
    participants = call_item.get("participants") or {}
    recipients = {caller} if caller else set()
    recipients |= {e.lower().strip() for e in participants.keys()}
    return recipients


async def dispatch_stream_record(
    *,
    table: str,
    event_type: str,
    new_image: dict,
    old_image: Optional[dict] = None,
    conversation_participants: Optional[list[str]] = None,
) -> int:
    """
    The core fan-out entry point every DynamoDB Stream record is routed
    through, for BOTH "this task saw its own write" and "this task saw
    another task's write" — the dispatch logic is identical either way,
    which is the whole point of using Streams as the fan-out mechanism
    (see module docstring).

    table: "messages" or "call_sessions" — selects which resolver to use.
    event_type: an arbitrary tag forwarded verbatim in the pushed
        payload's "event_type" field (e.g. "message.new",
        "message.edited", "call.ring", "call.roster_update",
        "call.ended") — main.py's caller decides the exact tag based on
        INSERT vs MODIFY and the old/new image diff; this function does
        not need to know the full taxonomy, it only needs to know
        whether to skip dispatch entirely (see below).
    conversation_participants: required (and only meaningful) for
        table="messages" — see resolve_recipients_for_message()'s
        docstring on why this is passed in rather than looked up here.

    Returns the number of local WebSocket pushes actually made (for
    logging/testing) — note this is PER-TASK; the true total delivery
    count across the whole ECS service is the sum of every task's
    return value for the same stream record, which no single call site
    ever needs to know.
    """
    if table == "call_sessions" and (new_image.get("status") == "ended"):
        # Per design.md: nothing to push for the terminal ended
        # transition — every relevant client already knows via the
        # specific action's own REST response.
        return 0

    if table == "messages":
        recipients = resolve_recipients_for_message(new_image, conversation_participants=conversation_participants or [])
    elif table == "call_sessions":
        recipients = resolve_recipients_for_call(new_image)
    else:
        logger.warning("ws_manager: dispatch_stream_record called with unknown table %r — ignoring", table)
        return 0

    if not recipients:
        return 0

    all_connection_ids: set[str] = set()
    for email in recipients:
        for conn in ws_connections.list_connections_for_user(email):
            all_connection_ids.add(conn["connection_id"])

    if not all_connection_ids:
        return 0

    data = new_image
    if table == "call_sessions":
        # Security fix (Task 15.0): `new_image` is the raw call row,
        # which includes `caller_attendee_id`/`caller_join_token` — the
        # Caller's own Chime SDK join credential for this meeting (see
        # database.py's CallSession model comment on why these are
        # top-level columns rather than a `participants[caller]` entry).
        # This broadcast fans out to EVERY recipient returned by
        # resolve_recipients_for_call() — i.e. every callee too, not
        # just the caller — so forwarding the token here would leak the
        # Caller's own meeting credential to every Callee's browser,
        # letting any of them impersonate the Caller's Chime attendee.
        # The Caller already receives their own join_token/attendee_id
        # directly from the POST /api/calls response; every OTHER
        # recipient only ever needs it back at accept-time via their
        # OWN POST /api/calls/{id}/accept response. Strip both fields
        # from the broadcast payload unconditionally.
        data = {k: v for k, v in new_image.items() if k not in ("caller_attendee_id", "caller_join_token")}

    payload = {"event_type": event_type, "table": table, "data": data}
    return await connection_manager.send_to_local_connections(all_connection_ids, payload)


# ── Real DynamoDB Streams polling loop ───────────────────────────────
# Thin outer layer on top of dispatch_stream_record() above — not
# exercised by test_ws_manager.py's unit tests (which call
# dispatch_stream_record() directly), since moto's DynamoDB Streams
# support does not model shard/iterator behaviour realistically enough
# to be a meaningful test of THIS logic specifically; the dispatch
# decision logic above is what actually needs unit coverage.

_poller_tasks: list["asyncio.Task"] = []


async def _poll_one_shard(streams_client, table: str, stream_arn: str, shard_id: str) -> None:
    """Poll a single shard forever using TRIM_HORIZON on first read,
    then chain shard iterators — standard DynamoDB Streams consumer
    pattern. Runs until cancelled (main.py's shutdown handler cancels
    every task in _poller_tasks)."""
    from . import chat_store, call_store

    iterator_resp = streams_client.get_shard_iterator(
        StreamArn=stream_arn, ShardId=shard_id, ShardIteratorType="LATEST"
    )
    shard_iterator = iterator_resp.get("ShardIterator")

    while shard_iterator:
        resp = streams_client.get_records(ShardIterator=shard_iterator, Limit=100)
        for record in resp.get("Records", []):
            try:
                dynamodb_record = record.get("dynamodb", {})
                new_image_raw = dynamodb_record.get("NewImage")
                if not new_image_raw:
                    continue
                new_image = _deserialize_stream_image(new_image_raw)

                if table == "messages":
                    event_name = record.get("eventName")
                    event_type = "message.new" if event_name == "INSERT" else "message.edited"
                    conv = chat_store.get_conversation(new_image.get("conversation_id", ""))
                    participants = (conv or {}).get("participants") or []
                    await dispatch_stream_record(
                        table="messages",
                        event_type=event_type,
                        new_image=new_image,
                        conversation_participants=participants,
                    )
                elif table == "call_sessions":
                    await dispatch_stream_record(table="call_sessions", event_type="call.update", new_image=new_image)
            except Exception:
                logger.exception("ws_manager: failed to process a stream record from %s", table)

        shard_iterator = resp.get("NextShardIterator")
        await asyncio.sleep(STREAM_POLL_INTERVAL_SECONDS)


def _deserialize_stream_image(image: dict) -> dict:
    """DynamoDB Streams records images in the raw AttributeValue-typed
    JSON shape ({"S": "..."}, {"N": "..."}, {"M": {...}}, etc.) — convert
    to plain Python values using boto3's own TypeDeserializer so
    downstream code (resolve_recipients_for_*, chat_store, call_store)
    only ever deals with plain dicts, matching what they receive
    everywhere else in this codebase (via boto3.resource("dynamodb"),
    which already auto-deserializes)."""
    from boto3.dynamodb.types import TypeDeserializer

    deserializer = TypeDeserializer()
    return {k: deserializer.deserialize(v) for k, v in image.items()}


def _discover_shard_ids(streams_client, stream_arn: str) -> list[str]:
    resp = streams_client.describe_stream(StreamArn=stream_arn)
    return [s["ShardId"] for s in resp.get("StreamDescription", {}).get("Shards", [])]


async def _poll_table_stream(table: str, table_name_env_var: str, default_table_name: str) -> None:
    """Discover the given table's stream ARN via the plain DynamoDB
    client (DescribeTable), then start one _poll_one_shard() task per
    shard. New shards appearing later (from a table resize — not
    expected at this scale with PAY_PER_REQUEST billing, but possible)
    are not automatically picked up by this simple implementation; a
    task restart (normal ECS deploys already happen regularly) would
    pick up any new shards on its next startup."""
    import boto3

    table_name = os.environ.get(table_name_env_var, default_table_name)
    ddb_client = boto3.client("dynamodb", region_name=AWS_REGION)
    streams_client = boto3.client("dynamodbstreams", region_name=AWS_REGION)

    try:
        desc = ddb_client.describe_table(TableName=table_name)
        stream_arn = desc.get("Table", {}).get("LatestStreamArn")
    except Exception:
        logger.exception("ws_manager: could not describe table %s to find its stream", table_name)
        return

    if not stream_arn:
        logger.warning("ws_manager: table %s has no active stream — fan-out for it is disabled", table_name)
        return

    shard_ids = _discover_shard_ids(streams_client, stream_arn)
    for shard_id in shard_ids:
        task = asyncio.create_task(_poll_one_shard(streams_client, table, stream_arn, shard_id))
        _poller_tasks.append(task)


def start_stream_pollers() -> None:
    """Called once from main.py's FastAPI startup event — starts the
    background poller tasks for both the Messages and CallSessions
    streams. Safe to call in local/ENV=local mode too: chat_store.py/
    call_store.py's local SQLite branch has no DynamoDB Streams at all,
    so this is a no-op there in practice (describe_table would fail
    against a non-existent DynamoDB table, which is caught and logged,
    not raised, in _poll_table_stream() above) — main.py only calls this
    when ENV != "local" to avoid the noisy log line in local dev."""
    asyncio.create_task(
        _poll_table_stream("messages", "CHAT_MESSAGES_TABLE_NAME", "job-portal-chat-messages")
    )
    asyncio.create_task(
        _poll_table_stream("call_sessions", "CALL_SESSIONS_TABLE_NAME", "job-portal-call-sessions")
    )


def stop_stream_pollers() -> None:
    """Cancel every poller task started by start_stream_pollers() — for
    graceful shutdown. Safe to call even if none were started."""
    for task in _poller_tasks:
        task.cancel()
    _poller_tasks.clear()
