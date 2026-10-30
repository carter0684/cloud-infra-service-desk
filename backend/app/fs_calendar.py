"""
Server-side proxy client for the Freshservice Change Calendar app
(a separate internal tool, hosted at its own CloudFront + API Gateway,
not part of this portal's infrastructure).

Auth: the Calendar app issues a long-lived service API key (a JWT with
typ="service"), sent as the Bearer token on every request. There is NO
username/password login any more.

Why a backend proxy instead of the frontend calling it directly:
  - The API key is a secret. Putting it in the frontend JS bundle would
    serve it plaintext to every browser AND permanently commit it into
    the frontend/dist build artifacts / Git history.
  - Instead the key lives ONLY here: the FS_CALENDAR_API_KEY env var
    (local dev, gitignored .env) or the "api_key" field of the AWS
    Secrets Manager secret (production — see compute_stack.py), which an
    admin can replace from the Admin page without a redeploy. main.py
    exposes thin passthrough routes (/api/fs-calendar/dashboard,
    /api/fs-calendar/changes); the portal's own Azure-AD-authenticated
    users hit those routes, never the raw Calendar API or its key.
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

# 5-minute server-side cache (see cache_utils.py), keyed per (path, year,
# month, timezone) — most visits re-view the same month within a few
# minutes of each other (paging back/forth, switching Calendars tabs), so
# this avoids redundant upstream calls without letting data go stale for
# more than 5 minutes.
_CACHE = TTLCache(ttl_seconds=5 * 60)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Public config — not secret, safe to hardcode/env either way.
FS_CALENDAR_API_BASE = os.environ.get(
    "FS_CALENDAR_API_BASE",
    "https://fs-calendar.example.com/v1/api/v1",
)

# API key source, in priority order:
#   1. FS_CALENDAR_API_KEY env var (local dev via a gitignored .env file —
#      see backend/.env.example).
#   2. The "api_key" field of the FS_CALENDAR_CREDENTIALS_SECRET_NAME
#      secret in AWS Secrets Manager (production — see compute_stack.py).
_ENV_API_KEY = os.environ.get("FS_CALENDAR_API_KEY")
_SECRET_NAME = os.environ.get(
    "FS_CALENDAR_CREDENTIALS_SECRET_NAME", "job-portal/fs-calendar-credentials"
)

# The secret is re-read at most this often, so a key replaced from the
# Admin page (or on another ECS task) is picked up without a redeploy.
_API_KEY_REFRESH_SECONDS = 3600

_lock = threading.Lock()
_cached_token: Optional[str] = None
_cached_token_expiry: float = 0.0  # unix timestamp


class FsCalendarError(Exception):
    """Raised when the Freshservice Calendar API can't be reached or
    returns an error. `message` is safe to show to end users."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _load_api_key_from_secret() -> str:
    try:
        client = boto3.client("secretsmanager", region_name=AWS_REGION)
        resp = client.get_secret_value(SecretId=_SECRET_NAME)
        secret = json.loads(resp["SecretString"])
        api_key = (secret.get("api_key") or "").strip()
    except Exception as e:
        logger.error("fs_calendar: failed to load API key: %s", e)
        raise FsCalendarError("Freshservice Calendar API key is not configured") from e
    if not api_key:
        logger.error("fs_calendar: credentials secret has no api_key")
        raise FsCalendarError("Freshservice Calendar API key is not configured")
    return api_key


def _get_token() -> str:
    global _cached_token, _cached_token_expiry
    with _lock:
        if _cached_token and time.time() < _cached_token_expiry - 300:
            return _cached_token
        api_key = (_ENV_API_KEY or "").strip() or _load_api_key_from_secret()
        _cached_token = api_key
        _cached_token_expiry = time.time() + _API_KEY_REFRESH_SECONDS
        return _cached_token


def _request(path: str, params: Optional[dict] = None) -> dict:
    token = _get_token()
    try:
        resp = httpx.get(
            f"{FS_CALENDAR_API_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
    except httpx.HTTPError as e:
        logger.error("fs_calendar: request to %s failed: %s", path, e)
        raise FsCalendarError("Could not reach the Freshservice Calendar service") from e

    if resp.status_code == 401:
        # The key may have just been replaced (e.g. from the Admin page on
        # another ECS task) — drop the cached copy, re-read it from its
        # source, and retry once.
        global _cached_token
        with _lock:
            _cached_token = None
        token = _get_token()
        try:
            resp = httpx.get(
                f"{FS_CALENDAR_API_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
        except httpx.HTTPError as e:
            raise FsCalendarError("Could not reach the Freshservice Calendar service") from e

    if resp.status_code != 200:
        logger.error("fs_calendar: %s returned %s: %s", path, resp.status_code, resp.text[:300])
        raise FsCalendarError(f"Freshservice Calendar API error (HTTP {resp.status_code})")

    return resp.json()


def get_dashboard(year: int, month: int, timezone_str: str = "Asia/Hong_Kong", force_refresh: bool = False) -> dict:
    key = ("dashboard", year, month, timezone_str)
    return _CACHE.get_or_fetch(
        key, lambda: _request("/dashboard", {"year": year, "month": month, "timezone": timezone_str}),
        force_refresh=force_refresh,
    )


def get_changes(year: int, month: int, timezone_str: str = "Asia/Hong_Kong", force_refresh: bool = False) -> dict:
    key = ("changes", year, month, timezone_str)
    return _CACHE.get_or_fetch(
        key, lambda: _request("/changes", {"year": year, "month": month, "timezone": timezone_str}),
        force_refresh=force_refresh,
    )


def reset_cache_for_tests() -> None:
    """Test-only helper: clear the process-local cache so each test starts
    from a clean slate. Not used by production code paths."""
    _CACHE.reset_for_tests()


def force_relogin() -> None:
    """Drops the cached API key so the NEXT request re-reads it from its
    source. Called by app/connected_portal_credentials.py right after an
    admin saves a new API key via the Admin page, so it takes effect
    immediately in this process. (Other running ECS tasks pick it up
    within _API_KEY_REFRESH_SECONDS, or sooner on their next 401.)"""
    global _cached_token, _cached_token_expiry
    with _lock:
        _cached_token = None
        _cached_token_expiry = 0.0
