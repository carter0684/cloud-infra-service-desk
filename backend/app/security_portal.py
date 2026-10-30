"""
Server-side proxy client for the Security Team's "the organization Security Monitoring
Dashboard" (a separate internal app on AWS App Runner, not part of this
portal's infrastructure).

That app has two surfaces on the SAME host:
  - /api/*           browser session login (username/password + cookie).
                     Does NOT accept an API key.
  - /external-api/*  read-only, GET-only, authenticated with the header
                     `X-API-Key: <key>` (no Bearer / query-string / cookie).
This module only ever talks to /external-api/*.

Why a backend proxy instead of the frontend calling it directly:
  - The API key is a secret. Putting it in the frontend bundle would serve
    it plaintext to every browser and commit it into build artifacts.
  - Instead the key lives ONLY here: the SECURITY_PORTAL_API_KEY env var
    (local dev, gitignored .env) or the "api_key" field of the AWS Secrets
    Manager secret (production — see compute_stack.py), which an admin can
    replace from the Admin page without a redeploy
    (app/connected_portal_credentials.py). main.py exposes one thin
    passthrough route, /api/security-portal/{resource}, gated to admins and
    members of the groups that can see the Security Team Portal tab.

Every upstream response has the shape {ok, data, cachedAt, fromCache}; the
upstream caches for ~10 minutes and honours `?refresh=1` to bypass that.
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

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Public config — not secret.
# Base URL of the external security dashboard's read-only API. Empty = the
# integration is not configured (enable it with ENABLED_FEATURES=security_portal).
SECURITY_PORTAL_API_BASE = os.environ.get(
    "SECURITY_PORTAL_API_BASE", "https://security-portal.example.com/external-api"
).rstrip("/")

# Public (browser-facing) URL of the same app, for the "Open Security Scan
# Portal" link on the tab.
SECURITY_PORTAL_PUBLIC_URL = os.environ.get(
    "SECURITY_PORTAL_PUBLIC_URL", "https://security-portal.example.com"
)

# Allow-list of upstream resources this proxy will forward. Anything else
# is rejected before any network call, so the route can never be used to
# reach other paths on the upstream host (no path traversal / SSRF).
RESOURCES: frozenset[str] = frozenset({
    "aws/alarms",
    "aws/backups",
    "aws/backup-images",
    "aws/certificates",
    "aws/ec2",
    "aws/inventory",
    "aws/utilization",
    "securityhub/report",
    "securityscorecard/report",
})

# Matches the upstream's own 10-minute cache; "Refresh now" bypasses both.
_CACHE = TTLCache(ttl_seconds=10 * 60)

# API key source, in priority order:
#   1. SECURITY_PORTAL_API_KEY env var (local dev via a gitignored .env).
#   2. The "api_key" field of the SECURITY_PORTAL_CREDENTIALS_SECRET_NAME
#      secret in AWS Secrets Manager (production).
_ENV_API_KEY = os.environ.get("SECURITY_PORTAL_API_KEY")
_SECRET_NAME = os.environ.get(
    "SECURITY_PORTAL_CREDENTIALS_SECRET_NAME", "job-portal/security-portal-credentials"
)

# The secret is re-read at most this often, so a key replaced from the
# Admin page (or on another ECS task) is picked up without a redeploy.
_API_KEY_REFRESH_SECONDS = 3600

# Upstream may do a live AWS fetch on a cache miss (inventory is ~0.5 MB).
_REQUEST_TIMEOUT_SECONDS = 60.0

_lock = threading.Lock()
_cached_key: Optional[str] = None
_cached_key_expiry: float = 0.0  # unix timestamp


class SecurityPortalError(Exception):
    """Raised when the Security Team API can't be reached or returns an
    error. `message` is safe to show to end users; `status_code` is the
    HTTP status main.py should answer with."""
    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _load_api_key_from_secret() -> str:
    try:
        client = boto3.client("secretsmanager", region_name=AWS_REGION)
        resp = client.get_secret_value(SecretId=_SECRET_NAME)
        secret = json.loads(resp["SecretString"])
        api_key = (secret.get("api_key") or "").strip()
    except Exception as e:
        logger.error("security_portal: failed to load API key: %s", e)
        raise SecurityPortalError("Security Team Portal API key is not configured") from e
    if not api_key:
        logger.error("security_portal: credentials secret has no api_key")
        raise SecurityPortalError("Security Team Portal API key is not configured")
    return api_key


def _get_key() -> str:
    global _cached_key, _cached_key_expiry
    with _lock:
        if _cached_key and time.time() < _cached_key_expiry - 300:
            return _cached_key
        api_key = (_ENV_API_KEY or "").strip() or _load_api_key_from_secret()
        _cached_key = api_key
        _cached_key_expiry = time.time() + _API_KEY_REFRESH_SECONDS
        return _cached_key


def _get(path: str, key: str, refresh: bool) -> httpx.Response:
    try:
        return httpx.get(
            f"{SECURITY_PORTAL_API_BASE}/{path}",
            params={"refresh": 1} if refresh else None,
            headers={"X-API-Key": key},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        logger.error("security_portal: request to %s failed: %s", path, e)
        raise SecurityPortalError("Could not reach the Security Team Portal") from e


def _request(path: str, refresh: bool) -> dict:
    global _cached_key
    resp = _get(path, _get_key(), refresh)

    if resp.status_code == 401:
        # The key may have just been replaced (e.g. from the Admin page on
        # another ECS task) — drop the cached copy, re-read it from its
        # source, and retry once.
        with _lock:
            _cached_key = None
        resp = _get(path, _get_key(), refresh)

    if resp.status_code != 200:
        logger.error("security_portal: %s returned %s: %s", path, resp.status_code, resp.text[:300])
        raise SecurityPortalError(f"Security Team Portal API error (HTTP {resp.status_code})")

    try:
        body = resp.json()
    except ValueError as e:
        logger.error("security_portal: %s returned non-JSON", path)
        raise SecurityPortalError("Security Team Portal returned an unreadable response") from e

    if not isinstance(body, dict) or not body.get("ok"):
        err = body.get("error") if isinstance(body, dict) else None
        logger.error("security_portal: %s returned ok=false: %s", path, err)
        raise SecurityPortalError("Security Team Portal could not provide this data")

    return body


def get_resource(resource: str, force_refresh: bool = False) -> dict:
    """Returns the upstream envelope {ok, data, cachedAt, fromCache} for an
    allow-listed resource, plus this proxy's own `cache_age_seconds`."""
    if resource not in RESOURCES:
        raise SecurityPortalError("Unknown Security Team Portal resource", status_code=404)
    return _CACHE.get_or_fetch(
        resource, lambda: _request(resource, force_refresh), force_refresh=force_refresh,
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
    global _cached_key, _cached_key_expiry
    with _lock:
        _cached_key = None
        _cached_key_expiry = 0.0
