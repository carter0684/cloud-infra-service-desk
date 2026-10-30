"""Who may open and edit Admin > Cloud Accounts: founding admins and the Management
Team only (user_store.is_cloud_accounts_manager). Plain admins and the Security
Team are refused."""

import pytest

from app import user_store

FOUNDER = "founder@example.com"
OTHER = "someone@example.com"


@pytest.fixture
def groups(monkeypatch):
    table: dict[str, list[str]] = {}
    monkeypatch.setattr(user_store, "SEED_ADMIN_EMAILS", {FOUNDER})
    monkeypatch.setattr(user_store, "get_user_groups", lambda email: table.get(email, []))
    return table


def test_a_founding_admin_is_a_manager(groups):
    assert user_store.is_cloud_accounts_manager(FOUNDER) is True
    assert user_store.is_cloud_accounts_manager(FOUNDER.upper()) is True
    assert user_store.is_cloud_accounts_manager(f"  {FOUNDER} ") is True


def test_a_management_team_member_is_a_manager(groups):
    groups[OTHER] = ["management_team"]
    assert user_store.is_cloud_accounts_manager(OTHER) is True


@pytest.mark.parametrize("member_of", [[], ["security_team"], ["infra_team"], ["security_team", "infra_team"]])
def test_everyone_else_is_refused(groups, member_of):
    groups[OTHER] = member_of
    assert user_store.is_cloud_accounts_manager(OTHER) is False


def test_admin_role_alone_is_not_enough(groups, monkeypatch):
    monkeypatch.setattr(user_store, "is_admin", lambda email: True)
    assert user_store.is_cloud_accounts_manager(OTHER) is False


@pytest.mark.parametrize("email", ["", None, "   "])
def test_blank_email_is_refused(groups, email):
    assert user_store.is_cloud_accounts_manager(email) is False
