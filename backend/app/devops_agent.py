"""
AWS DevOps Agent A2A (Agent-to-Agent) integration.

Forwards chat messages from the Job Portal to a dedicated AWS DevOps Agent
Agent Space (region set by DEVOPS_AGENT_REGION) via the official A2A v1.0
remote server, using Bearer access-token auth (simplest for a cross-account
integration — the portal's backend may run in a different account from the
Agent Space).

Auth model
----------
The Agent Space issues a scoped access token (scope="operate",
client type="agent") from its web app — Settings > Access Tokens. AWS does
not expose an API/CLI to create or list these tokens (by design: the token
value is shown only once), so it must be generated manually in the console
and stored in Secrets Manager. See:
https://docs.aws.amazon.com/devopsagent/latest/userguide/accessing-devops-agent-connect-to-devops-agent-remote-servers.html

Protocol
--------
A2A v1.0, HTTP+JSON REST binding (Google API "colon-action" style — NOT
plain JSON-RPC 2.0, despite what the AWS docs page describes; confirmed
empirically 2026-09-11/14 against the live endpoint):

    POST https://connect.aidevops.{region}.api.aws/a2a/message:send
    Headers:
        A2A-Version: 1.0
        Authorization: Bearer <access-token>
        Content-Type: application/json
    Body (no jsonrpc/method/params wrapper — the message object directly):
        {
          "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "<question>"}],
            "messageId": "<uuid>"
          }
        }

Async task model (CRITICAL — confirmed empirically 2026-09-14)
----------------------------------------------------------------
message:send does NOT block until the agent finishes. It always returns
promptly with a task in one of two states:

* "chat"-skill questions usually come back already COMPLETED, with the
  real answer in `artifacts` (still often 10-50+ seconds for a
  moderately complex question — this endpoint's own timeout still needs
  to comfortably cover that).
* "investigate"-skill questions (deep root-cause analysis, documented as
  5-8 minutes) come back near-instantly in TASK_STATE_WORKING, with a
  single JSON-encoded-as-text artifact part whose payload looks like:
      {"type": "investigation_started", "taskId": "<task-id>",
       "executionId": "exe-...", "message": "Investigation started. Use
       GetTask or SubscribeToTask for progress."}
  The actual investigation result only shows up later via GetTask.

    Response (application/a2a+json) — completed case:
        {
          "task": {
            "id": "<task-id>",
            "contextId": "<context-id>",
            "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "..."},
            "artifacts": [
              {"artifactId": "...", "parts": [{"text": "<answer>"}]}
            ]
          }
        }

    Response — still working:
        {
          "task": {
            "id": "<task-id>",
            "contextId": "...",
            "status": {"state": "TASK_STATE_WORKING", "timestamp": "..."},
            "artifacts": [{"artifactId": "...", "parts": [{"text":
                "{\"type\": \"investigation_started\", ...}"}]}]
          }
        }

GetTask (poll for progress/completion — confirmed empirically 2026-09-14):

    GET https://connect.aidevops.{region}.api.aws/a2a/tasks/{task_id}
    Headers: A2A-Version: 1.0, Authorization: Bearer <access-token>

    Response while running (note: NOT wrapped in a "task" key here,
    unlike message:send's response — the task fields are top-level):
        {
          "id": "<task-id>", "contextId": "<context-id>",
          "status": {"state": "TASK_STATE_WORKING", "timestamp": "..."},
          "metadata": {"executionId": "exe-...", "backendStatus": "IN_PROGRESS"}
        }

    Response once complete is expected to add an "artifacts" field with
    the final text answer, matching message:send's completed shape
    (not exhaustively confirmed for a full 5-8 minute investigation
    run — treat any non-WORKING/non-SUBMITTED state as terminal and
    look for `artifacts` defensively either way).

    Error response body shape (both endpoints):
        {"error": {"code": ..., "status": "...", "message": "..."}}

With Bearer-token auth, the token itself identifies the target Agent
Space — X-Agent-Space-Id is only needed for SigV4 auth, so it is
intentionally omitted here.

Design note — why polling, not one long blocking call
-------------------------------------------------------
An earlier version of this module just used a long httpx read timeout
(180s) and hoped every response would be a completed "chat" answer. That
both (a) couldn't support "investigate"-skill questions at all (5-8 min
>> any timeout it's reasonable to hold a browser/ALB connection open for)
and (b) caused a production "Network Error" for a merely slow "chat"
answer, because the ALB's default 60s idle_timeout closed the connection
before the single long-lived request finished (2026-09-14 incident).

Since message:send itself already returns almost immediately either way
(fast path: COMPLETED with the answer; slow path: WORKING with a task id
to poll), the fix is to always treat it as async: return the task id to
the caller right away, and let the frontend poll get_task() on a timer
until it's done. Every individual HTTP call in this module is now fast
(single-digit to double-digit seconds), so none of the timeouts in this
chain (httpx here, the ALB, the frontend's axios calls) need to be long
at all — see app/main.py's devops-agent routes and
frontend/src/pages/DevOpsAgentPage.jsx for the polling loop.
"""

from __future__ import annotations

import logging
import os
import uuid

import boto3
import httpx

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
# Secrets Manager region — this portal's own account/region (100000000002),
# NOT the Agent Space's account (100000000001). The token itself is a bearer
# credential; it works regardless of which account's Secrets Manager stores it.
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Region the Agent Space lives in — drives the connect.aidevops.{region}
# endpoint host. Independent of AWS_REGION above.
DEVOPS_AGENT_REGION = os.environ.get("DEVOPS_AGENT_REGION", "us-east-1")

DEVOPS_AGENT_TOKEN_SECRET_NAME = os.environ.get(
    "DEVOPS_AGENT_TOKEN_SECRET_NAME", "job-portal/devops-agent-access-token"
)

_SEND_URL_TEMPLATE = "https://connect.aidevops.{region}.api.aws/a2a/message:send"
_GET_TASK_URL_TEMPLATE = "https://connect.aidevops.{region}.api.aws/a2a/tasks/{task_id}"

# Every individual call in this module is expected to be fast (message:send
# and GetTask both return promptly — see the module docstring's "Async task
# model" section), so a generous-but-bounded 45s is plenty. This is
# comfortably under the ALB's idle_timeout and the frontend's axios timeout
# (see compute_stack.py / frontend/src/api.js) — there is no long-lived
# single request left in this design to race against those anymore.
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=10.0)

# A2A task states observed/documented. Anything not in _WORKING_STATES is
# treated as terminal (completed, failed, or otherwise done) when deciding
# whether the frontend should keep polling.
_WORKING_STATES = {"TASK_STATE_WORKING", "TASK_STATE_SUBMITTED"}


class DevOpsAgentError(Exception):
    """Typed error for DevOps Agent A2A failures. `message` is safe to show
    to the end user (no raw upstream response bodies or stack traces)."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _get_secret(secret_name: str) -> str:
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]


def _get_access_token() -> str:
    try:
        return _get_secret(DEVOPS_AGENT_TOKEN_SECRET_NAME).strip()
    except Exception as e:
        logger.error("devops_agent: failed to load access token: %s", e)
        raise DevOpsAgentError("DevOps Agent is not configured") from e


def _headers(token: str) -> dict:
    return {
        "A2A-Version": "1.0",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _extract_answer_text(task: dict) -> str | None:
    """Pull concatenated text out of a task's artifacts, if any are
    present and contain real text parts. Returns None if there's nothing
    usable yet (e.g. a bare "still working" status with no artifacts)."""
    answer_parts = []
    for artifact in task.get("artifacts", []) or []:
        for part in artifact.get("parts", []) or []:
            # Text parts come back without an explicit "kind" field in
            # practice (confirmed empirically) — just check for "text".
            if part.get("text"):
                answer_parts.append(part["text"])
    return "\n".join(answer_parts) if answer_parts else None


def _request(method: str, url: str, token: str, *, json_body: dict | None = None) -> dict:
    try:
        resp = httpx.request(
            method, url, json=json_body, headers=_headers(token), timeout=_REQUEST_TIMEOUT
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(
            "devops_agent: A2A %s %s failed with status %s: %s",
            method, url, e.response.status_code, e.response.text[:500],
        )
        raise DevOpsAgentError(
            f"DevOps Agent returned an error (HTTP {e.response.status_code})"
        ) from e
    except httpx.HTTPError as e:
        logger.error("devops_agent: A2A %s %s failed: %s", method, url, e)
        raise DevOpsAgentError("DevOps Agent is unreachable") from e

    try:
        payload = resp.json()
    except ValueError as e:
        logger.error("devops_agent: non-JSON response from A2A endpoint (%s %s)", method, url)
        raise DevOpsAgentError("DevOps Agent returned an invalid response") from e

    if "error" in payload:
        err = payload["error"]
        logger.error("devops_agent: A2A error (%s %s): %s", method, url, err)
        raise DevOpsAgentError(err.get("message", "DevOps Agent returned an error"))

    return payload


def send_message(question: str) -> dict:
    """
    Send a new message to the DevOps Agent via A2A `message:send`. Always
    returns promptly (see module docstring) — never blocks for the full
    duration of a slow "chat" answer or an "investigate" run.

    Returns a dict with:
        - "done": bool — True if the task already completed with a
          usable answer, False if the caller must poll get_task_status().
        - "task_id": str — the A2A task id, always present. Pass this to
          get_task_status() to poll for completion.
        - "answer": str | None — populated only when done=True.

    Raises:
        DevOpsAgentError on any failure (missing token, HTTP error,
        malformed response, or an A2A error envelope).
    """
    question = (question or "").strip()
    if not question:
        raise DevOpsAgentError("question cannot be empty")

    token = _get_access_token()
    url = _SEND_URL_TEMPLATE.format(region=DEVOPS_AGENT_REGION)
    body = {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": question}],
            "messageId": str(uuid.uuid4()),
        }
    }

    payload = _request("POST", url, token, json_body=body)
    task = payload.get("task", {})
    task_id = task.get("id")
    if not task_id:
        logger.warning("devops_agent: message:send response had no task id: %s", task)
        raise DevOpsAgentError("DevOps Agent did not return a task id")

    state = task.get("status", {}).get("state")
    if state in _WORKING_STATES:
        return {"done": False, "task_id": task_id, "answer": None}

    answer = _extract_answer_text(task)
    if answer is None:
        # Terminal state but no usable text — e.g. an unexpected shape,
        # or a "working"-style placeholder artifact that slipped through
        # under a state we didn't classify as working. Treat as still
        # in progress rather than erroring, so the frontend just polls.
        logger.warning(
            "devops_agent: task %s in state %s but no answer text yet: %s",
            task_id, state, task,
        )
        return {"done": False, "task_id": task_id, "answer": None}

    return {"done": True, "task_id": task_id, "answer": answer}


def get_task_status(task_id: str) -> dict:
    """
    Poll an existing task's status via A2A `GetTask`.

    Returns a dict with:
        - "done": bool — True once the task has reached a terminal state
          with a usable answer.
        - "task_id": str — echoes the input task_id.
        - "answer": str | None — populated only when done=True.

    Raises:
        DevOpsAgentError on any failure, including a 404 (task not found
        / expired) from the upstream A2A server.
    """
    task_id = (task_id or "").strip()
    if not task_id:
        raise DevOpsAgentError("task_id cannot be empty")

    token = _get_access_token()
    url = _GET_TASK_URL_TEMPLATE.format(region=DEVOPS_AGENT_REGION, task_id=task_id)

    # GetTask's response is the task object directly (no "task" wrapper
    # key) — confirmed empirically, unlike message:send's response shape.
    task = _request("GET", url, token)

    state = task.get("status", {}).get("state")
    if state in _WORKING_STATES:
        return {"done": False, "task_id": task_id, "answer": None}

    answer = _extract_answer_text(task)
    if answer is None:
        logger.warning(
            "devops_agent: GetTask %s in state %s but no answer text yet: %s",
            task_id, state, task,
        )
        return {"done": False, "task_id": task_id, "answer": None}

    return {"done": True, "task_id": task_id, "answer": answer}
