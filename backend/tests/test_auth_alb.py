"""
Tests for app/auth_alb.py — verifying the ALB-injected Azure AD OIDC JWT.

All tests use locally-generated fixture JWTs signed with a throwaway EC
keypair; no real ALB or Azure AD call is made. The module's
_fetch_alb_public_key() is monkeypatched to return our fixture public key
instead of hitting the real public-keys.auth.elb.<region>.amazonaws.com
endpoint.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from jose import jwt as jose_jwt

from app import auth_alb


@pytest.fixture(scope="module")
def keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return {"private_pem": private_pem, "public_pem": public_pem}


def _make_token(keypair, claims: dict, kid: str = "test-kid-1") -> str:
    headers = {"kid": kid, "alg": "ES256"}
    return jose_jwt.encode(claims, keypair["private_pem"], algorithm="ES256", headers=headers)


@pytest.fixture(autouse=True)
def clear_key_cache():
    """auth_alb caches ALB public keys by kid across calls — clear between
    tests so one test's monkeypatched key doesn't leak into another."""
    auth_alb._KEY_CACHE.clear()
    yield
    auth_alb._KEY_CACHE.clear()


@pytest.fixture
def mock_alb_public_key(monkeypatch, keypair):
    """Patch the network fetch so verify_alb_oidc_token() uses our fixture
    public key instead of calling the real ALB endpoint."""
    def _fake_fetch(kid: str) -> str:
        return keypair["public_pem"]
    monkeypatch.setattr(auth_alb, "_fetch_alb_public_key", _fake_fetch)


class TestValidToken:
    def test_valid_token_with_allowlisted_email_returns_email(self, keypair, mock_alb_public_key):
        token = _make_token(keypair, {
            "email": "admin.one@example.com",
            "exp": int(time.time()) + 3600,
        })
        result = auth_alb.get_actor(x_amzn_oidc_data=token)
        assert result == "admin.one@example.com"

    def test_email_extraction_falls_back_to_upn(self, keypair, mock_alb_public_key):
        token = _make_token(keypair, {
            "upn": "admin.two@example.com",
            "exp": int(time.time()) + 3600,
        })
        result = auth_alb.get_actor(x_amzn_oidc_data=token)
        assert result == "admin.two@example.com"

    def test_email_extraction_falls_back_to_preferred_username(self, keypair, mock_alb_public_key):
        token = _make_token(keypair, {
            "preferred_username": "admin.three@example.com",  # case should be normalised
            "exp": int(time.time()) + 3600,
        })
        result = auth_alb.get_actor(x_amzn_oidc_data=token)
        assert result == "admin.three@example.com"


class TestExpiredToken:
    def test_expired_token_raises_401(self, keypair, mock_alb_public_key):
        token = _make_token(keypair, {
            "email": "admin.one@example.com",
            "exp": int(time.time()) - 3600,  # expired 1 hour ago
        })
        with pytest.raises(Exception) as exc_info:
            auth_alb.get_actor(x_amzn_oidc_data=token)
        assert getattr(exc_info.value, "status_code", None) == 401

    def test_expired_token_error_has_no_internal_detail(self, keypair, mock_alb_public_key):
        token = _make_token(keypair, {
            "email": "admin.one@example.com",
            "exp": int(time.time()) - 3600,
        })
        with pytest.raises(Exception) as exc_info:
            auth_alb.get_actor(x_amzn_oidc_data=token)
        # Requirement 9.6/9.8: no leaking of internal error detail (e.g. the
        # word "expired" or "signature" or any exception internals) to the client.
        detail = str(getattr(exc_info.value, "detail", ""))
        assert detail == "Unauthorised"


class TestWrongOrMissingClaims:
    def test_missing_header_raises_401(self):
        with pytest.raises(Exception) as exc_info:
            auth_alb.get_actor(x_amzn_oidc_data=None)
        assert getattr(exc_info.value, "status_code", None) == 401

    def test_malformed_token_raises_401(self, mock_alb_public_key):
        with pytest.raises(Exception) as exc_info:
            auth_alb.get_actor(x_amzn_oidc_data="not-a-real-jwt-at-all")
        assert getattr(exc_info.value, "status_code", None) == 401

    def test_token_missing_kid_raises_401(self, keypair, mock_alb_public_key):
        # Encode a token with no 'kid' in the header at all.
        token = jose_jwt.encode(
            {"email": "admin.one@example.com", "exp": int(time.time()) + 3600},
            keypair["private_pem"],
            algorithm="ES256",
            headers={"alg": "ES256"},
        )
        with pytest.raises(Exception) as exc_info:
            auth_alb.get_actor(x_amzn_oidc_data=token)
        assert getattr(exc_info.value, "status_code", None) == 401

    def test_token_signed_with_wrong_key_raises_401(self, keypair, mock_alb_public_key):
        # Sign with a *different* keypair than the one _fetch_alb_public_key
        # returns — signature verification must fail.
        other_private_key = ec.generate_private_key(ec.SECP256R1())
        other_private_pem = other_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        token = jose_jwt.encode(
            {"email": "admin.one@example.com", "exp": int(time.time()) + 3600},
            other_private_pem,
            algorithm="ES256",
            headers={"kid": "test-kid-1", "alg": "ES256"},
        )
        with pytest.raises(Exception) as exc_info:
            auth_alb.get_actor(x_amzn_oidc_data=token)
        assert getattr(exc_info.value, "status_code", None) == 401


class TestNonAllowlistedEmail:
    def test_non_allowlisted_email_raises_401(self, keypair, mock_alb_public_key):
        token = _make_token(keypair, {
            "email": "someone.else@example.com",
            "exp": int(time.time()) + 3600,
        })
        with pytest.raises(Exception) as exc_info:
            auth_alb.get_actor(x_amzn_oidc_data=token)
        assert getattr(exc_info.value, "status_code", None) == 401

    def test_missing_email_claim_entirely_raises_401(self, keypair, mock_alb_public_key):
        token = _make_token(keypair, {"exp": int(time.time()) + 3600})
        with pytest.raises(Exception) as exc_info:
            auth_alb.get_actor(x_amzn_oidc_data=token)
        assert getattr(exc_info.value, "status_code", None) == 401

    def test_non_allowlisted_email_error_has_no_internal_detail(self, keypair, mock_alb_public_key):
        token = _make_token(keypair, {
            "email": "someone.else@example.com",
            "exp": int(time.time()) + 3600,
        })
        with pytest.raises(Exception) as exc_info:
            auth_alb.get_actor(x_amzn_oidc_data=token)
        detail = str(getattr(exc_info.value, "detail", ""))
        assert detail == "Unauthorised"
