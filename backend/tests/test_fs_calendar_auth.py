"""Auth tests for the Freshservice Change Calendar proxy — it signs in with
a long-lived service API key sent directly as the Bearer token. There is no
username/password login any more (app/fs_calendar.py), and the Admin page's
credential update (app/connected_portal_credentials.py) is API-key-only for
this system."""
import json
from unittest.mock import MagicMock, patch

import pytest

from app import fs_calendar, connected_portal_credentials

FAKE_KEY = "fake.api.key"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(fs_calendar, "_ENV_API_KEY", None)
    fs_calendar.force_relogin()
    fs_calendar.reset_cache_for_tests()
    yield
    fs_calendar.force_relogin()
    fs_calendar.reset_cache_for_tests()


def _ok(payload=None):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload or {"ok": True}
    return r


def _secret_client(secret: dict) -> MagicMock:
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": json.dumps(secret)}
    return client


# ── fs_calendar.py: API key used directly ────────────────────────────────────

def test_env_api_key_is_used_as_bearer_and_never_touches_secrets_manager(monkeypatch):
    monkeypatch.setattr(fs_calendar, "_ENV_API_KEY", FAKE_KEY)
    with patch.object(fs_calendar.boto3, "client") as boto, \
         patch.object(fs_calendar.httpx, "get", return_value=_ok()) as get, \
         patch.object(fs_calendar.httpx, "post") as post:
        fs_calendar.get_dashboard(2026, 9)
    boto.assert_not_called()
    post.assert_not_called()
    assert get.call_args.kwargs["headers"] == {"Authorization": f"Bearer {FAKE_KEY}"}


def test_secret_api_key_is_used_as_bearer_without_any_login_call():
    with patch.object(fs_calendar.boto3, "client", return_value=_secret_client({"api_key": FAKE_KEY})), \
         patch.object(fs_calendar.httpx, "get", return_value=_ok()) as get, \
         patch.object(fs_calendar.httpx, "post") as post:
        fs_calendar.get_changes(2026, 9)
    post.assert_not_called()
    assert get.call_args.kwargs["headers"] == {"Authorization": f"Bearer {FAKE_KEY}"}


def test_legacy_username_password_in_secret_is_ignored_not_used_to_login():
    secret = {"username": "u", "password": "p"}
    with patch.object(fs_calendar.boto3, "client", return_value=_secret_client(secret)), \
         patch.object(fs_calendar.httpx, "post") as post, \
         patch.object(fs_calendar.httpx, "get") as get:
        with pytest.raises(fs_calendar.FsCalendarError):
            fs_calendar.get_dashboard(2026, 8)
    post.assert_not_called()
    get.assert_not_called()


@pytest.mark.parametrize("secret", [{}, {"api_key": ""}, {"api_key": "   "}])
def test_missing_or_blank_api_key_raises_clean_error(secret):
    with patch.object(fs_calendar.boto3, "client", return_value=_secret_client(secret)):
        with pytest.raises(fs_calendar.FsCalendarError) as exc:
            fs_calendar.get_dashboard(2026, 7)
    assert "not configured" in exc.value.message


def test_unreadable_secret_raises_clean_error_without_leaking_details():
    boto_client = MagicMock()
    boto_client.get_secret_value.side_effect = RuntimeError("AccessDenied arn:aws:secret:xyz")
    with patch.object(fs_calendar.boto3, "client", return_value=boto_client):
        with pytest.raises(fs_calendar.FsCalendarError) as exc:
            fs_calendar.get_dashboard(2026, 7)
    assert "AccessDenied" not in exc.value.message


def test_401_rereads_secret_so_a_rotated_key_is_picked_up():
    unauthorized = MagicMock()
    unauthorized.status_code = 401
    clients = [_secret_client({"api_key": "old-key"}), _secret_client({"api_key": "new-key"})]
    with patch.object(fs_calendar.boto3, "client", side_effect=clients), \
         patch.object(fs_calendar.httpx, "get", side_effect=[unauthorized, _ok()]) as get:
        fs_calendar.get_dashboard(2026, 6)
    sent = [c.kwargs["headers"]["Authorization"] for c in get.call_args_list]
    assert sent == ["Bearer old-key", "Bearer new-key"]


def test_api_key_is_cached_between_requests():
    with patch.object(fs_calendar.boto3, "client", return_value=_secret_client({"api_key": FAKE_KEY})) as boto, \
         patch.object(fs_calendar.httpx, "get", return_value=_ok()):
        fs_calendar.get_dashboard(2026, 5)
        fs_calendar.get_changes(2026, 5)
    assert boto.call_count == 1


# ── connected_portal_credentials.py: Admin page ──────────────────────────────

def test_admin_sets_calendar_api_key_and_secret_contains_only_api_key():
    client = _secret_client({"username": "old-u", "password": "old-p", "api_key": "old"})
    with patch.object(connected_portal_credentials, "_secrets_client", return_value=client):
        result = connected_portal_credentials.update_credentials("fs_calendar", api_key=f"  {FAKE_KEY}  ")
    written = json.loads(client.put_secret_value.call_args.kwargs["SecretString"])
    assert written == {"api_key": FAKE_KEY}  # stale username/password are NOT kept
    assert result["api_key_configured"] is True
    assert result["auth_type"] == "api_key"
    assert FAKE_KEY not in json.dumps(result)


def test_saving_api_key_drops_cached_key_so_it_takes_effect_immediately(monkeypatch):
    monkeypatch.setattr(fs_calendar, "_cached_token", "stale")
    monkeypatch.setattr(fs_calendar, "_cached_token_expiry", 9e12)
    with patch.object(connected_portal_credentials, "_secrets_client", return_value=_secret_client({})):
        connected_portal_credentials.update_credentials("fs_calendar", api_key=FAKE_KEY)
    assert fs_calendar._cached_token is None


@pytest.mark.parametrize("kwargs", [
    {},                                              # nothing supplied
    {"api_key": "   "},                              # blank key
    {"username": "u", "password": "p"},              # password login not allowed
    {"api_key": FAKE_KEY, "username": "u"},          # key plus a stray username
    {"api_key": FAKE_KEY, "password": "p"},          # key plus a stray password
])
def test_calendar_rejects_anything_but_a_lone_api_key(kwargs):
    client = _secret_client({"api_key": "existing"})
    with patch.object(connected_portal_credentials, "_secrets_client", return_value=client):
        with pytest.raises(connected_portal_credentials.ConnectedPortalCredentialsError):
            connected_portal_credentials.update_credentials("fs_calendar", **kwargs)
    client.put_secret_value.assert_not_called()


def test_sr_ticket_still_uses_username_and_password():
    client = _secret_client({"username": "old", "password": "old"})
    with patch.object(connected_portal_credentials, "_secrets_client", return_value=client):
        result = connected_portal_credentials.update_credentials("sr_ticket", "newuser", "newpass")
    assert json.loads(client.put_secret_value.call_args.kwargs["SecretString"]) == {
        "username": "newuser", "password": "newpass"
    }
    assert result["auth_type"] == "password"
    assert result["username"] == "newuser"
    assert "newpass" not in json.dumps(result)


@pytest.mark.parametrize("kwargs", [
    {"api_key": FAKE_KEY},                 # SR Ticket has no API key
    {"username": "u"},                     # username without password
    {"password": "p"},                     # password without username
    {},
])
def test_sr_ticket_rejects_invalid_combinations_without_writing(kwargs):
    client = _secret_client({"username": "u", "password": "p"})
    with patch.object(connected_portal_credentials, "_secrets_client", return_value=client):
        with pytest.raises(connected_portal_credentials.ConnectedPortalCredentialsError):
            connected_portal_credentials.update_credentials("sr_ticket", **kwargs)
    client.put_secret_value.assert_not_called()


def test_list_systems_reports_state_but_never_a_key_or_password():
    def fake_client():
        c = MagicMock()
        def get_secret_value(SecretId):
            if "fs-calendar" in SecretId:
                return {"SecretString": json.dumps({"api_key": FAKE_KEY})}
            return {"SecretString": json.dumps({"username": "srv-user", "password": "s3cret-pw"})}
        c.get_secret_value.side_effect = get_secret_value
        return c
    with patch.object(connected_portal_credentials, "_secrets_client", side_effect=fake_client):
        systems = {s["id"]: s for s in connected_portal_credentials.list_systems()}
    assert systems["fs_calendar"]["auth_type"] == "api_key"
    assert systems["fs_calendar"]["api_key_configured"] is True
    assert systems["fs_calendar"]["username"] is None
    assert systems["sr_ticket"]["auth_type"] == "password"
    assert systems["sr_ticket"]["username"] == "srv-user"
    assert systems["sr_ticket"]["api_key_configured"] is False
    blob = json.dumps(systems)
    assert FAKE_KEY not in blob and "s3cret-pw" not in blob
