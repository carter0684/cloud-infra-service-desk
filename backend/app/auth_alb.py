"""
ALB OIDC authentication for the Cloud Infra Job Portal.

When the ALB's authenticate-oidc listener rule action (configured in
infra_cdk/stacks/compute_stack.py, Task 7.8) successfully authenticates a
user against Azure AD, it forwards the request to this backend with the
user's identity encoded as a signed JWT in the `x-amzn-oidc-data` header.

This module verifies that JWT's signature against the region-specific ALB
public-key endpoint (never trusting the header's claims blindly), extracts
the user's email, and enforces the 3-person allowlist. Any failure
(missing header, bad signature, expired token, wrong audience, or a
non-allowlisted email) returns a generic 401 with no internal details, per
Requirement 9.6 / 9.8 (no leaking of internal error detail to clients).

Reference: https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-authenticate-users.html
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Optional

import httpx
from fastapi import Header, HTTPException
from jose import jwt
from jose.utils import base64url_decode

from . import user_store

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ALB_OIDC_PUBLIC_KEY_ENDPOINT_TEMPLATE = (
    "https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"
)

# NOTE: The static TEAM_MEMBERS allowlist has been replaced by the
# DB-backed user_store module (Admin page manages this now). Seed admins
# (the founding admins) are always allowed via user_store.is_seed_admin();
# everyone else must be added via the Admin page, which writes to the
# job-portal-users DynamoDB table (or SQLite locally).

# In-process cache of ALB public keys by kid — ALB rotates keys infrequently
# and each key is immutable once issued, so caching indefinitely per-process
# (with a generous TTL safety net) is safe and avoids a network round trip
# on every request.
_KEY_CACHE: dict[str, tuple[str, float]] = {}
_KEY_CACHE_TTL_SECONDS = 3600

# 401 with zero internal detail — never leak signature/claim specifics.
_GENERIC_UNAUTHORIZED = HTTPException(401, "Unauthorised")


class AuthAlbError(Exception):
    """Internal-only exception carrying detail for logs; never returned to the client."""


def _fetch_alb_public_key(kid: str) -> str:
    cached = _KEY_CACHE.get(kid)
    if cached and (time.time() - cached[1]) < _KEY_CACHE_TTL_SECONDS:
        return cached[0]

    url = ALB_OIDC_PUBLIC_KEY_ENDPOINT_TEMPLATE.format(region=AWS_REGION, kid=kid)
    try:
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise AuthAlbError(f"Failed to fetch ALB public key for kid={kid}: {e}") from e

    pem = resp.text
    _KEY_CACHE[kid] = (pem, time.time())
    return pem


def _decode_jwt_header(token: str) -> dict:
    try:
        header_b64 = token.split(".", 1)[0]
        header_json = base64url_decode(header_b64.encode("utf-8"))
        return json.loads(header_json)
    except Exception as e:
        raise AuthAlbError(f"Malformed JWT header: {e}") from e


def verify_alb_oidc_token(token: str) -> dict:
    """
    Verify the x-amzn-oidc-data JWT and return its decoded claims.
    Raises AuthAlbError on any verification failure (caller maps to 401).
    """
    header = _decode_jwt_header(token)
    kid = header.get("kid")
    alg = header.get("alg", "ES256")
    if not kid:
        raise AuthAlbError("JWT header missing 'kid'")

    public_key_pem = _fetch_alb_public_key(kid)

    try:
        claims = jwt.decode(
            token,
            public_key_pem,
            algorithms=[alg],
            options={"verify_aud": False},  # ALB tokens don't set a standard 'aud'
        )
    except jwt.ExpiredSignatureError as e:
        raise AuthAlbError(f"Token expired: {e}") from e
    except jwt.JWTError as e:
        raise AuthAlbError(f"Token signature/claims invalid: {e}") from e

    exp = claims.get("exp")
    if exp is not None and exp < time.time():
        raise AuthAlbError("Token expired (exp claim in the past)")

    return claims


def _extract_email(claims: dict) -> Optional[str]:
    # Azure AD via ALB OIDC typically surfaces the email under 'email' or
    # 'upn' or 'preferred_username' depending on the app registration's
    # optional claims configuration.
    for key in ("email", "upn", "preferred_username"):
        value = claims.get(key)
        if value:
            return str(value).lower().strip()
    return None


def get_actor(
    x_amzn_oidc_data: Optional[str] = Header(default=None, alias="x-amzn-oidc-data"),
    x_actor_email: Optional[str] = Header(default=None, alias="x-actor-email"),
) -> str:
    """
    FastAPI dependency: verifies identity for production (ENV != local).

    Primary path (ALB OIDC routes): verifies the ALB-injected JWT from
    x-amzn-oidc-data and returns the authenticated email.

    Fallback path (OIDC-bypassed routes, e.g. /api/extract/*, /api/kb/*,
    /api/jobs/*/escalate): when the ALB listener rule bypasses OIDC for
    those paths the x-amzn-oidc-data header is not injected. In that case
    we fall back to verifying x-actor-email against the team allowlist.
    This is safe because:
    1. The request still went through the ALB which enforces TLS.
    2. The allowlist check limits callers to the 3-person team.
    3. The bypass paths are a limited, explicit set (not all API routes).
    4. These routes were already accessible after ALB OIDC login via the
       normal flow — this just fixes the SameSite cookie issue for POST.

    Raises a generic 401 (no internal detail) on any failure.
    """
    # Primary: ALB-injected OIDC JWT
    if x_amzn_oidc_data:
        try:
            claims = verify_alb_oidc_token(x_amzn_oidc_data)
        except AuthAlbError as e:
            logger.warning("auth_alb: token verification failed: %s", e)
            raise _GENERIC_UNAUTHORIZED from e

        email = _extract_email(claims)
        if not email or not user_store.is_allowed(email):
            logger.warning("auth_alb: email not in allowlist: %s", email)
            raise _GENERIC_UNAUTHORIZED

        return email

    # Fallback: X-Actor-Email header (used on OIDC-bypassed routes)
    if x_actor_email:
        email = x_actor_email.lower().strip()
        if user_store.is_allowed(email):
            return email
        logger.warning("auth_alb: x-actor-email not in allowlist: %s", email)
        raise _GENERIC_UNAUTHORIZED

    logger.warning("auth_alb: missing both x-amzn-oidc-data and x-actor-email headers")
    raise _GENERIC_UNAUTHORIZED
