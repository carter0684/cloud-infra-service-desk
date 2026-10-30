"""Tests for the Security Team Portal proxy (app/security_portal.py): it calls
the upstream's read-only /external-api/* with `X-API-Key`, never the
session-login /api/* surface, and the route is gated like the sidebar tab."""
import importlib
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import connected_portal_credentials, security_portal, settings_store, user_store

FAKE_KEY = "fake-security-key"
ENVELOPE = {"ok": True, "data": [{"id": "i-1"}], "cachedAt": 1, "fromCache": False}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(security_portal, "_ENV_API_KEY", None)
    security_portal.force_relogin()
    security_portal.reset_cache_for_tests()
    yield
    security_portal.force_relogin()
    security_portal.reset_cache_for_tests()


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.text = json.dumps(payload or {})
    r.json.return_value = payload if payload is not None else ENVELOPE
    return r


def _secret_client(secret: dict) -> MagicMock:
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": json.dumps(secret)}
    return client


# ── security_portal.py ───────────────────────────────────────────────────────

def test_uses_x_api_key_header_on_external_api_path():
    with patch.object(security_portal.boto3, "client", return_value=_secret_client({"api_key": FAKE_KEY})), \
         patch.object(security_portal.httpx, "get", return_value=_resp()) as get:
        out = security_portal.get_resource("aws/ec2")

    assert out["data"] == [{"id": "i-1"}]
    assert get.call_args.kwargs["headers"] == {"X-API-Key": FAKE_KEY}
    url = get.call_args.args[0]
    assert url.endswith("/external-api/aws/ec2")
    assert "/api/" not in url  # the session-login surface does not accept keys


def test_env_key_is_used_and_secrets_manager_untouched(monkeypatch):
    monkeypatch.setattr(security_portal, "_ENV_API_KEY", FAKE_KEY)
    with patch.object(security_portal.boto3, "client") as boto, \
         patch.object(security_portal.httpx, "get", return_value=_resp()) as get:
        security_portal.get_resource("aws/alarms")
    boto.assert_not_called()
    assert get.call_args.kwargs["headers"] == {"X-API-Key": FAKE_KEY}


@pytest.mark.parametrize("resource", ["", "aws/../../api/login", "api/login", "aws/unknown", "/aws/ec2"])
def test_unknown_resource_is_rejected_before_any_network_call(resource):
    with patch.object(security_portal.httpx, "get") as get, \
         patch.object(security_portal.boto3, "client") as boto:
        with pytest.raises(security_portal.SecurityPortalError) as exc:
            security_portal.get_resource(resource)
    assert exc.value.status_code == 404
    get.assert_not_called()
    boto.assert_not_called()


def test_refresh_flag_is_forwarded_and_bypasses_cache(monkeypatch):
    monkeypatch.setattr(security_portal, "_ENV_API_KEY", FAKE_KEY)
    with patch.object(security_portal.httpx, "get", return_value=_resp()) as get:
        security_portal.get_resource("aws/ec2")
        security_portal.get_resource("aws/ec2")            # served from cache
        assert get.call_count == 1
        assert get.call_args.kwargs["params"] is None
        security_portal.get_resource("aws/ec2", force_refresh=True)
    assert get.call_count == 2
    assert get.call_args.kwargs["params"] == {"refresh": 1}


def test_401_rereads_key_once_and_retries():
    clients = [_secret_client({"api_key": "old"}), _secret_client({"api_key": "new"})]
    with patch.object(security_portal.boto3, "client", side_effect=clients), \
         patch.object(security_portal.httpx, "get", side_effect=[_resp(401), _resp()]) as get:
        security_portal.get_resource("aws/certificates")
    assert [c.kwargs["headers"]["X-API-Key"] for c in get.call_args_list] == ["old", "new"]


def test_persistent_401_raises_clean_error_without_leaking_key():
    with patch.object(security_portal.boto3, "client", return_value=_secret_client({"api_key": FAKE_KEY})), \
         patch.object(security_portal.httpx, "get", return_value=_resp(401)):
        with pytest.raises(security_portal.SecurityPortalError) as exc:
            security_portal.get_resource("aws/ec2")
    assert FAKE_KEY not in exc.value.message
    assert "401" in exc.value.message


@pytest.mark.parametrize("secret", [{}, {"api_key": ""}, {"api_key": "   "}])
def test_missing_or_blank_key_raises_not_configured(secret):
    with patch.object(security_portal.boto3, "client", return_value=_secret_client(secret)), \
         patch.object(security_portal.httpx, "get") as get:
        with pytest.raises(security_portal.SecurityPortalError) as exc:
            security_portal.get_resource("aws/ec2")
    assert "not configured" in exc.value.message
    get.assert_not_called()


def test_secrets_manager_error_is_not_leaked():
    boto_client = MagicMock()
    boto_client.get_secret_value.side_effect = RuntimeError("AccessDenied arn:aws:secret:xyz")
    with patch.object(security_portal.boto3, "client", return_value=boto_client):
        with pytest.raises(security_portal.SecurityPortalError) as exc:
            security_portal.get_resource("aws/ec2")
    assert "AccessDenied" not in exc.value.message


def test_upstream_ok_false_and_network_errors_become_clean_errors(monkeypatch):
    monkeypatch.setattr(security_portal, "_ENV_API_KEY", FAKE_KEY)
    with patch.object(security_portal.httpx, "get", return_value=_resp(200, {"ok": False, "error": "boom internal"})):
        with pytest.raises(security_portal.SecurityPortalError) as exc:
            security_portal.get_resource("aws/ec2")
    assert "boom" not in exc.value.message

    security_portal.reset_cache_for_tests()
    with patch.object(security_portal.httpx, "get", side_effect=security_portal.httpx.ConnectError("down")):
        with pytest.raises(security_portal.SecurityPortalError) as exc:
            security_portal.get_resource("aws/ec2")
    assert "Could not reach" in exc.value.message


# ── connected_portal_credentials.py: Admin page ──────────────────────────────

def test_admin_sets_security_portal_key_and_drops_cached_key():
    security_portal._cached_key = "stale"
    security_portal._cached_key_expiry = 9e12
    client = _secret_client({})
    with patch.object(connected_portal_credentials, "_secrets_client", return_value=client):
        result = connected_portal_credentials.update_credentials("security_portal", api_key=f"  {FAKE_KEY}  ")

    assert json.loads(client.put_secret_value.call_args.kwargs["SecretString"]) == {"api_key": FAKE_KEY}
    assert client.put_secret_value.call_args.kwargs["SecretId"] == "job-portal/security-portal-credentials"
    assert result["api_key_configured"] is True
    assert FAKE_KEY not in json.dumps(result)
    assert security_portal._cached_key is None


@pytest.mark.parametrize("kwargs", [{}, {"api_key": "   "}, {"username": "u", "password": "p"}, {"api_key": "k", "password": "p"}])
def test_security_portal_rejects_anything_but_a_lone_api_key(kwargs):
    client = _secret_client({"api_key": "existing"})
    with patch.object(connected_portal_credentials, "_secrets_client", return_value=client):
        with pytest.raises(connected_portal_credentials.ConnectedPortalCredentialsError):
            connected_portal_credentials.update_credentials("security_portal", **kwargs)
    client.put_secret_value.assert_not_called()


def test_security_portal_listed_as_api_key_system_without_leaking_the_key():
    client = _secret_client({"api_key": FAKE_KEY})
    with patch.object(connected_portal_credentials, "_secrets_client", return_value=client):
        systems = {s["id"]: s for s in connected_portal_credentials.list_systems()}
    assert systems["security_portal"]["auth_type"] == "api_key"
    assert systems["security_portal"]["api_key_configured"] is True
    assert FAKE_KEY not in json.dumps(systems)


# ── route authorization: same people as the sidebar tab ──────────────────────

ADMIN = "admin@example.com"
SEC = "sec@example.com"
INFRA = "infra@example.com"
NOBODY = "nobody@example.com"
GROUPS = {SEC: ["security_team"], INFRA: ["infra_team"], NOBODY: []}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ENV", "local")
    from app import main
    importlib.reload(main)  # other test modules may have left main reloaded under ENV=production
    everyone = {ADMIN, SEC, INFRA, NOBODY}
    monkeypatch.setattr(user_store, "is_allowed", lambda email: email in everyone)
    monkeypatch.setattr(user_store, "is_admin", lambda email: email == ADMIN)
    monkeypatch.setattr(user_store, "get_user_groups", lambda email: GROUPS.get(email, []))
    # Group -> category grants are admin-configured at runtime; pin them:
    # only the Security Team group is granted the "security" category
    # (which contains the security_team_portal tab).
    monkeypatch.setattr(
        settings_store, "get_group_category_access",
        lambda: {"security_team": ["security"], "infra_team": []},
    )
    monkeypatch.setattr(security_portal, "_ENV_API_KEY", FAKE_KEY)
    return TestClient(main.app)


def _h(email):
    return {"X-Actor-Email": email}


@pytest.mark.parametrize("who", [NOBODY, INFRA])
def test_route_denies_users_without_the_tab_and_never_calls_upstream(client, who):
    with patch.object(security_portal.httpx, "get", return_value=_resp()) as get:
        r = client.get("/api/security-portal/aws/ec2", headers=_h(who))
    assert r.status_code == 403
    get.assert_not_called()


def test_route_allows_admin_and_tab_group_members(client):
    with patch.object(security_portal.httpx, "get", return_value=_resp()):
        assert client.get("/api/security-portal/aws/ec2", headers=_h(ADMIN)).status_code == 200
        security_portal.reset_cache_for_tests()
        r = client.get("/api/security-portal/aws/ec2", headers=_h(SEC))
    assert r.status_code == 200
    assert r.json()["data"] == [{"id": "i-1"}]


def test_route_maps_unknown_resource_to_404_and_upstream_failure_to_502(client):
    with patch.object(security_portal.httpx, "get") as get:
        assert client.get("/api/security-portal/nope", headers=_h(ADMIN)).status_code == 404
        get.assert_not_called()
    with patch.object(security_portal.httpx, "get", return_value=_resp(500)):
        r = client.get("/api/security-portal/aws/ec2", headers=_h(ADMIN))
    assert r.status_code == 502
    assert FAKE_KEY not in r.text
