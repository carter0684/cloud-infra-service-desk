"""
Admin-managed credential rotation for the portal's "connected" external
systems — currently the Freshservice Change Calendar (app/fs_calendar.py),
the SR Ticket Information / ITSM Support Portal (app/sr_ticket.py), and the
Security Team Portal (app/security_portal.py).

Each of those systems has its own authentication, completely separate
from this portal's Azure AD SSO, and both occasionally need their
credential replaced on the far end (a security rotation, an account
policy, key expiry, etc.). Before this module existed, that meant asking
someone to run `aws secretsmanager put-secret-value` by hand every time —
this module lets an admin do it directly from the Admin page instead.

Each system has an `auth_type`:
  - "api_key":  Freshservice Change Calendar. Secret JSON is exactly
    {"api_key": ...}; there is no username/password for this system.
  - "password": SR Ticket Information. Secret JSON is
    {"username": ..., "password": ...}.

Design:
  - Each connected system's credentials live in their own existing AWS
    Secrets Manager secret (job-portal/fs-calendar-credentials,
    job-portal/sr-ticket-credentials).
  - This module ONLY writes (secretsmanager:PutSecretValue, scoped to
    just these two secrets in compute_stack.py — deliberately NOT
    granted for any other secret) and reads back non-secret facts for the
    Admin UI (the username for a "password" system; a yes/no "is an API
    key configured" for an "api_key" system — per Requirement 9.6/9.8
    "never leak secrets"). Passwords and API keys are write-only from
    this module's perspective: once saved, no route returns them.
  - Saving replaces the secret wholesale with only the fields that
    system's auth_type uses, so e.g. a Calendar secret never keeps a
    stale username/password around.
  - After a successful write, the corresponding module's in-process
    cached credential state is dropped (force_relogin()) so the NEW value
    takes effect on this ECS task immediately, without waiting for its
    cache to expire or for a redeploy. Other running ECS tasks pick it up
    on their own next refresh / 401 (see each module), so no coordination
    across tasks is needed here.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Literal

import boto3

from . import fs_calendar
from . import security_portal
from . import sr_ticket

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

SystemId = Literal["fs_calendar", "sr_ticket", "security_portal"]

_SYSTEMS: dict[str, dict] = {
    "fs_calendar": {
        "label": "Freshservice Change Calendar",
        "secret_name": fs_calendar._SECRET_NAME,
        "force_relogin": fs_calendar.force_relogin,
        "auth_type": "api_key",
    },
    "sr_ticket": {
        "label": "SR Ticket Information",
        "secret_name": sr_ticket._SECRET_NAME,
        "force_relogin": sr_ticket.force_relogin,
        "auth_type": "password",
    },
    "security_portal": {
        "label": "Security Team Portal",
        "secret_name": security_portal._SECRET_NAME,
        "force_relogin": security_portal.force_relogin,
        "auth_type": "api_key",
    },
}


class ConnectedPortalCredentialsError(Exception):
    """Raised on any failure reading/writing a connected system's
    credentials. `message` is safe to show to end users."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _secrets_client():
    return boto3.client("secretsmanager", region_name=AWS_REGION)


def list_systems() -> list[dict]:
    """Returns [{id, label, auth_type, username, api_key_configured,
    configured}] for every connected system. Passwords and API keys are
    NEVER included: `username` is only populated for "password" systems,
    `api_key_configured` is just a boolean. `configured` is False if the
    secret couldn't be read at all (e.g. it doesn't exist yet in this
    environment)."""
    out = []
    for system_id, meta in _SYSTEMS.items():
        auth_type = meta["auth_type"]
        username = None
        api_key_configured = False
        configured = True
        try:
            resp = _secrets_client().get_secret_value(SecretId=meta["secret_name"])
            secret = json.loads(resp["SecretString"])
            if auth_type == "api_key":
                api_key_configured = bool((secret.get("api_key") or "").strip())
            else:
                username = secret.get("username")
        except Exception as e:
            logger.warning(
                "connected_portal_credentials: could not read %s: %s", meta["secret_name"], e
            )
            configured = False
        out.append({
            "id": system_id,
            "label": meta["label"],
            "auth_type": auth_type,
            "username": username,
            "api_key_configured": api_key_configured,
            "configured": configured,
        })
    return out


def update_credentials(
    system_id: str,
    username: str | None = None,
    password: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Overwrites the given system's Secrets Manager secret, then drops
    that system's in-process cached credential so the change takes effect
    immediately on this ECS task.

    What must be supplied depends on the system's auth_type:
      - "api_key"  -> `api_key` only; username/password are rejected.
      - "password" -> `username` and `password` together; api_key is
        rejected.
    Returns {id, label, auth_type, username, api_key_configured} (never
    the password or key) on success."""
    meta = _SYSTEMS.get(system_id)
    if meta is None:
        raise ConnectedPortalCredentialsError(f"Unknown connected system: {system_id}")

    auth_type = meta["auth_type"]
    username = (username or "").strip()
    password = password or ""
    api_key = (api_key or "").strip()

    if auth_type == "api_key":
        if username or password:
            raise ConnectedPortalCredentialsError(
                f"{meta['label']} signs in with an API key, not a username and password"
            )
        if not api_key:
            raise ConnectedPortalCredentialsError("API key must not be empty")
        secret = {"api_key": api_key}
    else:
        if api_key:
            raise ConnectedPortalCredentialsError(f"{meta['label']} does not use an API key")
        if not username:
            raise ConnectedPortalCredentialsError("Username must not be empty")
        if not password:
            raise ConnectedPortalCredentialsError("Password must not be empty")
        secret = {"username": username, "password": password}

    try:
        _secrets_client().put_secret_value(
            SecretId=meta["secret_name"], SecretString=json.dumps(secret)
        )
    except Exception as e:
        logger.error(
            "connected_portal_credentials: failed to update %s: %s", meta["secret_name"], e
        )
        raise ConnectedPortalCredentialsError(
            f"Failed to save credentials for {meta['label']}"
        ) from e

    try:
        meta["force_relogin"]()
    except Exception as e:
        # The secret WAS saved successfully — a failure here just means
        # this one ECS task keeps using its old cached credential a little
        # longer (until its own refresh / 401), not that the update failed.
        logger.warning(
            "connected_portal_credentials: force_relogin failed for %s: %s", system_id, e
        )

    return {
        "id": system_id,
        "label": meta["label"],
        "auth_type": auth_type,
        "username": secret.get("username"),
        "api_key_configured": bool(secret.get("api_key")),
    }
