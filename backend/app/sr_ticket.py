"""
Server-side proxy client for the "SR Ticket Information" system — an
internal "ITSM Support Portal" web app (title observed via its /home
page), hosted at its own API Gateway + Express server, not part of this
portal's infrastructure.

Confirmed via a captured HAR of an authenticated browser session:
  - The app is a small server-rendered dashboard with a session-cookie
    login (Username/Password form on GET /home when unauthenticated),
    NOT a bearer-token REST API.
  - The one real feature is the "Service Item Report" page
    (GET /sr-report), which POSTs its filters as JSON to
    POST /api/sr-report/run and renders the JSON response client-side.
  - Request body shape: {start, end, serviceItem, workspace, requester,
    approvalStatus, requestStatus, outstandingOnly} — start/end are
    "YYYY-MM-DD"; the rest are exact-match strings from the `options`
    lists in the previous response, or "" for "all". outstandingOnly is
    a bool.
  - Response body shape: {ok, view, period, totals, summary,
    details: {columns, rows}, options, scanMode, meta}. `details.rows[]`
    is the actual ticket list — each row has `id`, `url` (link to the
    real Freshservice ticket), `bucket`, and `values` (a dict keyed by
    the column names in `details.columns`, e.g. "Service Item",
    "Ticket ID" (like "SR-59722"), "Requestor", "Request Date",
    "Fulfilment Group", "Approval Status", "Fulfilment Status").

The exact login POST endpoint/body shape was NOT captured (the HAR was
taken from an already-authenticated session) — `_login()` tries the most
likely shapes (JSON POST to a few common paths, form-urlencoded as a
fallback) and keeps whichever succeeds. If the real login path turns out
to be something else, only `_LOGIN_PATHS`/`_login()` need updating — the
report-fetching logic (`get_tickets()`) is confirmed correct and should
not need to change.

Why a backend proxy instead of the frontend calling it directly: this
system has its own login, completely separate from this portal's Azure
AD SSO, and its credentials must never reach the browser/frontend bundle
or Git — see AWS Secrets Manager "job-portal/sr-ticket-credentials",
loaded server-side only. Mirrors the Freshservice Change Calendar proxy
pattern (see app/fs_calendar.py), but uses a cookie jar (httpx.Client)
instead of a bearer token, since this system's auth is session-cookie
based.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

import boto3
import httpx

from .cache_utils import TTLCache

logger = logging.getLogger(__name__)

# 5-minute server-side cache (see cache_utils.py) — mirrors the
# Freshservice Change Calendar proxy's cache window so a tab left open
# doesn't hammer the separate SR Ticket system on every visit, without
# letting data go stale for more than 5 minutes. Keyed by the exact
# filter combination, same idea as billing_by_service.py.
_CACHE = TTLCache(ttl_seconds=5 * 60)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

SR_TICKET_API_BASE = os.environ.get(
    "SR_TICKET_API_BASE",
    "https://sr-ticket.example.com",
).rstrip("/")

REPORT_PATH = "/api/sr-report/run"

# Credential source, in priority order:
#   1. SR_TICKET_USERNAME / SR_TICKET_PASSWORD env vars directly (local dev).
#   2. SR_TICKET_CREDENTIALS_SECRET_NAME in AWS Secrets Manager (production
#      — see compute_stack.py). Created directly via `aws secretsmanager
#      create-secret`, not modeled as a CDK resource (the JSON payload
#      contains the raw password).
_ENV_USERNAME = os.environ.get("SR_TICKET_USERNAME")
_ENV_PASSWORD = os.environ.get("SR_TICKET_PASSWORD")
_SECRET_NAME = os.environ.get(
    "SR_TICKET_CREDENTIALS_SECRET_NAME", "job-portal/sr-ticket-credentials"
)


class SrTicketError(Exception):
    """Raised when the SR Ticket Information API can't be reached or
    returns an error. `message` is safe to show to end users."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _get_credentials() -> tuple[str, str]:
    if _ENV_USERNAME and _ENV_PASSWORD:
        return _ENV_USERNAME, _ENV_PASSWORD
    try:
        client = boto3.client("secretsmanager", region_name=AWS_REGION)
        resp = client.get_secret_value(SecretId=_SECRET_NAME)
        secret = json.loads(resp["SecretString"])
        return secret["username"], secret["password"]
    except Exception as e:
        logger.error("sr_ticket: failed to load credentials: %s", e)
        raise SrTicketError("SR Ticket Information credentials are not configured") from e


# This is a session-cookie app (Express + server-rendered login form), not
# a bearer-token API — a single httpx.Client with its cookie jar is kept
# alive per process and reused across requests, re-logging-in on demand
# when the session has expired/is missing.
_client_lock = threading.Lock()
_client: Optional[httpx.Client] = None
_logged_in_at: float = 0.0
# Re-login proactively after this long even if we haven't seen a 401 —
# session lifetime wasn't documented, so this is a conservative guess
# (shorter than fs_calendar.py's 24h bearer token, since cookie-based
# ITSM-style sessions are often shorter-lived).
_SESSION_MAX_AGE_SECONDS = 4 * 3600

# Common login-form POST shapes to try, in order, until one leaves us
# with a session that can load /sr-report without hitting the login page
# again. Cached in _login_path/_login_kind once one succeeds.
_LOGIN_CANDIDATES = [
    ("/login", "json"),
    ("/login", "form"),
    ("/api/login", "json"),
    ("/auth/login", "json"),
]
_login_path: Optional[str] = None
_login_kind: Optional[str] = None  # "json" | "form"


def _looks_like_login_page(resp: httpx.Response) -> bool:
    """True if `resp` is the "ITSM Support Portal — Sign in" HTML page
    rather than real app content — used both to verify login succeeded
    and to detect a session that has silently expired."""
    if resp.status_code in (401, 403):
        return True
    ctype = resp.headers.get("content-type", "")
    if "text/html" in ctype and "Sign in" in resp.text[:2000]:
        return True
    return False


def _login(client: httpx.Client) -> None:
    """Logs `client` in (mutating its cookie jar) by POSTing credentials
    to whichever candidate path/shape works. Raises SrTicketError if none
    of them do."""
    global _login_path, _login_kind
    username, password = _get_credentials()

    candidates = [(_login_path, _login_kind)] if _login_path else _LOGIN_CANDIDATES
    last_status = None
    for path, kind in candidates:
        try:
            if kind == "json":
                resp = client.post(
                    f"{SR_TICKET_API_BASE}{path}",
                    json={"username": username, "password": password},
                    timeout=15.0,
                )
            else:
                resp = client.post(
                    f"{SR_TICKET_API_BASE}{path}",
                    data={"username": username, "password": password},
                    timeout=15.0,
                )
        except httpx.HTTPError as e:
            last_status = f"error: {e}"
            continue

        last_status = resp.status_code
        if resp.status_code not in (200, 302, 303):
            continue

        # Verify the login actually worked by loading a real page and
        # checking it's NOT the sign-in form again.
        try:
            verify = client.get(f"{SR_TICKET_API_BASE}/home", timeout=15.0)
        except httpx.HTTPError:
            continue
        if not _looks_like_login_page(verify):
            _login_path, _login_kind = path, kind
            return

    raise SrTicketError(
        f"SR Ticket Information login failed (last status: {last_status})"
    )


def _get_client() -> httpx.Client:
    """Returns a logged-in httpx.Client, logging in (or re-logging-in if
    the session looks stale/expired) as needed. Thread-safe."""
    global _client, _logged_in_at
    with _client_lock:
        needs_login = (
            _client is None
            or (time.time() - _logged_in_at) > _SESSION_MAX_AGE_SECONDS
        )
        if needs_login:
            if _client is not None:
                _client.close()
            _client = httpx.Client()
            _login(_client)
            _logged_in_at = time.time()
        return _client


def _post_report(payload: dict) -> dict:
    """POSTs `payload` to /api/sr-report/run using the shared logged-in
    client, re-logging-in once and retrying if the session had expired."""
    global _client, _logged_in_at
    client = _get_client()
    try:
        resp = client.post(
            f"{SR_TICKET_API_BASE}{REPORT_PATH}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        logger.error("sr_ticket: report request failed: %s", e)
        raise SrTicketError("Could not reach the SR Ticket Information service") from e

    if _looks_like_login_page(resp):
        # Session expired server-side before our local max-age expected
        # it — force one re-login and retry once.
        with _client_lock:
            if _client is not None:
                _client.close()
            _client = httpx.Client()
            _login(_client)
            _logged_in_at = time.time()
            client = _client
        try:
            resp = client.post(
                f"{SR_TICKET_API_BASE}{REPORT_PATH}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=20.0,
            )
        except httpx.HTTPError as e:
            raise SrTicketError("Could not reach the SR Ticket Information service") from e

    if resp.status_code != 200:
        logger.error(
            "sr_ticket: %s returned %s: %s", REPORT_PATH, resp.status_code, resp.text[:300]
        )
        raise SrTicketError(f"SR Ticket Information API error (HTTP {resp.status_code})")

    try:
        body = resp.json()
    except ValueError as e:
        raise SrTicketError("SR Ticket Information API returned an unexpected response") from e

    if not body.get("ok", True):
        err = (body.get("error") or {}).get("message") or "Report failed"
        raise SrTicketError(err)

    return body


def _default_payload() -> dict:
    """Matches the frontend's default filters observed in the HAR: the
    trailing 32 days through today, no filters, not outstanding-only."""
    import datetime
    today = datetime.date.today()
    start = today - datetime.timedelta(days=32)
    return {
        "start": start.isoformat(),
        "end": today.isoformat(),
        "serviceItem": "",
        "workspace": "",
        "requester": "",
        "approvalStatus": "",
        "requestStatus": "",
        "outstandingOnly": False,
    }


def get_tickets(force_refresh: bool = False, filters: Optional[dict] = None) -> dict:
    """Returns the SR Ticket Information "Service Item Report" for the
    given filters (or the default ~32-day window if none given), cached
    for 5 minutes per exact filter combination.

    `filters` may include any of: start, end (both "YYYY-MM-DD"),
    serviceItem, workspace, requester, approvalStatus, requestStatus
    (exact-match strings, "" or omitted = "all"), outstandingOnly (bool).
    """
    payload = _default_payload()
    if filters:
        payload.update({k: v for k, v in filters.items() if v is not None})

    cache_key = ("report", tuple(sorted(payload.items())))

    def _fetch() -> dict:
        return _post_report(payload)

    return _CACHE.get_or_fetch(cache_key, _fetch, force_refresh=force_refresh)


def reset_cache_for_tests() -> None:
    """Test-only helper: clear the process-local cache and cached
    session/auth state so each test starts from a clean slate. Not used
    by production code paths."""
    global _client, _logged_in_at, _login_path, _login_kind
    _CACHE.reset_for_tests()
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None
    _logged_in_at = 0.0
    _login_path = None
    _login_kind = None


def force_relogin() -> None:
    """Drops the cached session (cookie jar) so the NEXT request logs in
    from scratch. Called by app/connected_portal_credentials.py right
    after an admin updates this system's password via the Admin page, so
    the new credentials take effect immediately in this process."""
    global _client, _logged_in_at
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None
        _logged_in_at = 0.0
