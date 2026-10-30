"""HTTP tests for /api/admin/cloud-accounts/* (.kiro/specs/cloud-account-onboarding, task 6).

TestClient is used as a plain call (not a context manager) so no startup hook
runs: no database file is touched and no bootstrap thread starts. The registry
lives on an in-memory SQLite database and the AWS / Azure connection tests are
replaced, so nothing here calls AWS or Azure.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

from app import cloud_account_access as access
from app import cloud_account_service as svc
from app import cloud_account_store as store
from app import main, user_store

ADMIN = "admin@example.com"          # founding admin: admin AND Cloud Accounts manager
MEMBER = "member@example.com"        # ordinary allowed user
PLAIN_ADMIN = "plainadmin@example.com"  # is_admin, but not founding / Management Team
SECURITY = "security@example.com"    # Security Team member (not Management Team)
MGMT = "mgmt@example.com"            # Management Team member (not an admin)
ACCT = "123456789012"
GUID = "11111111-2222-3333-4444-555555555555"
TASK_ROLE = "arn:aws:iam::100000000002:role/JobPortalComputeStack-JobPortalTaskRoleABC123"
PHRASE = "I understand the risk and agree to offboard."
BASE = "/api/admin/cloud-accounts"
OK = {"ok": True, "code": "ok", "message": "Connected."}
BAD = {"ok": False, "code": "access_denied", "message": "Role not found, or its trust policy or External ID does not match."}


class Env:
    outcome = None


@pytest.fixture
def api(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app import azure_monitor, database

    monkeypatch.setenv("ENV", "local")
    importlib.reload(main)  # other test modules may have left main reloaded under ENV=production

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))

    monkeypatch.setattr(user_store, "is_allowed", lambda email: email in {ADMIN, MEMBER, PLAIN_ADMIN, SECURITY, MGMT})
    monkeypatch.setattr(user_store, "is_admin", lambda email: email in {ADMIN, PLAIN_ADMIN})
    monkeypatch.setattr(user_store, "is_cloud_accounts_manager", lambda email: email in {ADMIN, MGMT})

    def _no_azure():
        raise azure_monitor.AzureError("no azure credentials in tests")

    monkeypatch.setattr(azure_monitor, "_get_credentials", _no_azure)

    env = Env()
    env.outcome = dict(OK)
    env.calls = []

    def fake_aws(entry):
        env.calls.append(entry["identifier"])
        return dict(env.outcome)

    def fake_permissions(entry):
        env.permission_calls.append(entry["identifier"])
        return dict(env.permissions)

    env.permission_calls = []
    env.permissions = {
        "ok": True,
        "identity": {"account": "100000000003", "kind": "role", "role_name": "r", "arn": "arn:aws:iam::100000000003:role/r"},
        "managed_policies": [{"name": "ReadOnlyAccess", "arn": "arn:aws:iam::aws:policy/ReadOnlyAccess", "aws_managed": True}],
        "inline_policies": [],
        "simulation": {"available": True, "write_checked": 29, "write_allowed": [], "data_read_checked": 9, "data_read_allowed": []},
        "summary": "Read-only: none of the 29 write actions checked are allowed.",
    }
    monkeypatch.setattr(access, "describe_role_permissions", fake_permissions)
    env.infraops = {"ok": False, "code": "not_provisioned", "message": "This account does not have the role."}
    monkeypatch.setattr(access, "describe_shared_infraops_role", lambda entry: dict(env.infraops))
    monkeypatch.setattr(access, "test_aws_connection", fake_aws)
    monkeypatch.setattr(access, "task_role_arn", lambda: TASK_ROLE)
    monkeypatch.setattr(azure_monitor, "test_azure_subscription", lambda sub: (env.calls.append(sub), dict(env.outcome))[1])
    monkeypatch.setattr(svc, "TEST_ALL_CONCURRENCY", 1)
    with store._lock:
        store._snap.update(entries=None, loaded_at=0.0, from_storage=False)
    svc._rate_calls.clear()
    store.seed_if_needed()

    env.client = TestClient(main.app)
    env.monkeypatch = monkeypatch
    yield env
    with store._lock:
        store._snap.update(entries=None, loaded_at=0.0, from_storage=False)
    svc._rate_calls.clear()


def _h(email=ADMIN):
    return {"X-Actor-Email": email}


def _get(api, path, who=ADMIN):
    return api.client.get(BASE + path, headers=_h(who))


def _post(api, path, body=None, who=ADMIN):
    return api.client.post(BASE + path, json=body or {}, headers=_h(who))


def _prepare(api, account=ACCT):
    r = _post(api, "/aws/prepare", {"account_id": account})
    assert r.status_code == 200, r.text
    return r.json()


# ── Access control ──────────────────────────────────────────────────────────

ROUTES = [
    ("GET", "", None),
    ("GET", "/azure/guide", None),
    ("POST", "/test-all", {}),
    ("POST", "/aws/prepare", {"account_id": ACCT}),
    ("GET", f"/aws/{ACCT}/setup", None),
    ("GET", f"/aws/{ACCT}/template", None),
    ("POST", f"/aws/{ACCT}/connect", {"display_name": "x"}),
    ("POST", "/azure", {"subscription_id": GUID, "display_name": "x"}),
    ("GET", "/aws/100000000003", None),
    ("PATCH", "/aws/100000000003", {"display_name": "x"}),
    ("POST", "/aws/100000000003/test", {}),
    ("GET", "/aws/100000000003/permissions", None),
    ("POST", "/aws/100000000003/offboard", {}),
    ("POST", "/aws/100000000003/onboard", {}),
    ("DELETE", f"/aws/{ACCT}", None),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_route_is_for_founding_admins_and_management_team_only(api, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    # An ordinary user, a plain admin and a Security Team member are all refused.
    for who in (MEMBER, PLAIN_ADMIN, SECURITY):
        r = api.client.request(method, BASE + path, headers=_h(who), **kwargs)
        assert r.status_code == 403, (who, method, path, r.text)
    anonymous = api.client.request(method, BASE + path, **kwargs)
    assert anonymous.status_code == 401, (method, path)
    stranger = api.client.request(method, BASE + path, headers=_h("stranger@evil.example"), **kwargs)
    assert stranger.status_code in (401, 403)
    assert api.calls == [] and api.permission_calls == []  # nothing ran on behalf of a refused user


@pytest.mark.parametrize("who", [ADMIN, MGMT])
def test_founding_admin_and_management_team_can_open_the_page_and_edit(api, who):
    assert api.client.get(BASE, headers=_h(who)).status_code == 200
    r = api.client.patch(f"{BASE}/aws/100000000003", json={"display_name": "renamed"}, headers=_h(who))
    assert r.status_code == 200 and r.json()["account"]["display_name"] == "renamed"


def test_a_management_team_member_who_is_not_an_admin_still_cannot_use_other_admin_routes(api):
    # the page access is separate from is_admin: it must not widen anything else
    assert api.client.get("/api/admin/connected-portals", headers=_h(MGMT)).status_code == 403


def test_whoami_tells_the_frontend_who_may_manage_cloud_accounts(api, monkeypatch):
    # the other lookups whoami makes read the user table; they are not under test here
    class _Team(dict):  # stands in for the lazy user-table view (TEAM_MEMBERS)
        def __contains__(self, email):
            return user_store.is_allowed(email)

        def get(self, email, default=None):
            return email

    monkeypatch.setattr(main, "TEAM_MEMBERS", _Team())
    for name in ("get_photo_url", "is_portal_founder", "is_user_admin", "is_portal_authority"):
        monkeypatch.setattr(user_store, name, lambda email: None)
    monkeypatch.setattr(user_store, "get_user_tab_write", lambda email: [])
    flags = {}
    for who in (ADMIN, MGMT, PLAIN_ADMIN, SECURITY, MEMBER):
        r = api.client.get("/api/auth/whoami", headers=_h(who))
        assert r.status_code == 200, (who, r.status_code, r.text)
        flags[who] = r.json()["can_manage_cloud_accounts"]
    assert flags == {ADMIN: True, MGMT: True, PLAIN_ADMIN: False, SECURITY: False, MEMBER: False}


def test_a_rejected_request_changes_nothing(api):
    api.client.patch(f"{BASE}/aws/100000000003", json={"display_name": "hacked"}, headers=_h(MEMBER))
    api.client.post(f"{BASE}/aws/100000000003/offboard", json={}, headers=_h(MEMBER))
    assert store.get("aws", "100000000003")["display_name"] == "network"
    assert store.get("aws", "100000000003")["status"] == "pending"


# ── Listing and secrecy ─────────────────────────────────────────────────────

def test_list_returns_the_seeded_accounts_counts_and_business_units(api):
    r = _get(api, "")
    assert r.status_code == 200
    body = r.json()
    assert len(body["accounts"]) == 30
    assert body["counts"]["aws"]["total"] == 30
    assert "BU-A" in body["business_units"]
    row = next(a for a in body["accounts"] if a["identifier"] == "100000000003")
    assert row["display_name"] == "network" and row["access_mode"] == "legacy" and row["visible"] is True
    assert row["protected"] is False
    assert next(a for a in body["accounts"] if a["identifier"] == "100000000001")["protected"] is True


def test_the_external_id_only_appears_in_the_setup_responses(api):
    secret = _prepare(api)["external_id"]
    others = [
        _get(api, ""), _get(api, f"/aws/{ACCT}"),
        _post(api, f"/aws/{ACCT}/connect", {"display_name": "Prod"}),
        api.client.patch(f"{BASE}/aws/{ACCT}", json={"display_name": "Prod2"}, headers=_h()),
        _post(api, f"/aws/{ACCT}/test"), _get(api, "/aws/100000000003"),
    ]
    for r in others:
        assert r.status_code == 200, r.text
        assert secret not in r.text
    assert _get(api, f"/aws/{ACCT}/setup").json()["external_id"] == secret
    assert secret in _get(api, f"/aws/{ACCT}/template").text


# ── AWS onboarding flow ─────────────────────────────────────────────────────

def test_full_aws_onboarding_flow_over_http(api):
    # 1. prepare: role name, External_Id, no-store, and stable on repeat
    setup = _prepare(api)
    assert setup["role_name"] == "JobPortal-CrossAccount-InfraOps-1234"
    assert setup["portal_role_arn"] == TASK_ROLE and len(setup["external_id"]) >= 32
    r = _post(api, "/aws/prepare", {"account_id": ACCT})
    assert r.json()["external_id"] == setup["external_id"]
    assert r.headers["cache-control"] == "no-store"

    # 2. template download
    t = _get(api, f"/aws/{ACCT}/template")
    assert t.status_code == 200
    assert t.headers["content-type"].startswith("application/x-yaml")
    assert t.headers["content-disposition"] == f'attachment; filename="JobPortal-InfraOps-Role-{ACCT}.yaml"'
    assert t.headers["cache-control"] == "no-store"
    assert setup["external_id"] in t.text and "ReadOnlyAccess" in t.text

    # 3. not visible on other pages until connected
    assert ACCT not in [a["account_id"] for a in api.client.get("/api/billing-by-service/accounts", headers=_h()).json()["accounts"]]

    # 4. connect
    c = _post(api, f"/aws/{ACCT}/connect", {"display_name": "Brand New Prod", "business_unit": "BU-B"})
    assert c.status_code == 200 and c.json()["result"]["ok"] is True
    assert c.json()["account"]["status"] == "connected"
    picker = {a["account_id"]: a["alias"] for a in api.client.get("/api/billing-by-service/accounts", headers=_h()).json()["accounts"]}
    assert picker[ACCT] == "Brand New Prod"

    # 5. rename shows up everywhere without a deployment
    assert api.client.patch(f"{BASE}/aws/{ACCT}", json={"display_name": "Renamed"}, headers=_h()).status_code == 200
    picker = {a["account_id"]: a["alias"] for a in api.client.get("/api/billing-by-service/accounts", headers=_h()).json()["accounts"]}
    assert picker[ACCT] == "Renamed"

    # 6. detail has the audit trail
    actions = [a["action"] for a in _get(api, f"/aws/{ACCT}").json()["audit"]]
    assert {"prepare", "template_download", "connect", "update"} <= set(actions)

    # 7. offboard removes it, onboard again brings it back, discard is refused once connected
    assert _post(api, f"/aws/{ACCT}/offboard").status_code == 200
    assert ACCT not in [a["account_id"] for a in api.client.get("/api/billing-by-service/accounts", headers=_h()).json()["accounts"]]
    assert _post(api, f"/aws/{ACCT}/onboard").json()["account"]["status"] == "connected"
    assert ACCT in [a["account_id"] for a in api.client.get("/api/billing-by-service/accounts", headers=_h()).json()["accounts"]]
    d = api.client.delete(f"{BASE}/aws/{ACCT}", headers=_h())
    assert d.status_code == 409 and "Offboard" in d.json()["detail"]


def test_failed_connect_is_explained_and_the_draft_can_be_discarded(api):
    _prepare(api)
    api.outcome = dict(BAD)
    c = _post(api, f"/aws/{ACCT}/connect", {"display_name": "Prod"})
    assert c.status_code == 200
    body = c.json()
    assert body["result"]["ok"] is False and "trust policy" in body["result"]["message"]
    assert body["account"]["status"] == "failed" and body["account"]["visible"] is False
    d = api.client.delete(f"{BASE}/aws/{ACCT}", headers=_h())
    assert d.status_code == 200 and d.json() == {"discarded": True}
    assert _get(api, f"/aws/{ACCT}").status_code == 404


def test_template_and_setup_are_refused_for_a_legacy_account(api):
    assert _get(api, "/aws/100000000003/template").status_code == 409
    assert _get(api, "/aws/100000000003/setup").status_code == 409
    assert _post(api, "/aws/prepare", {"account_id": "100000000003"}).status_code == 409


# ── Validation ──────────────────────────────────────────────────────────────

def test_validation_errors_are_400_with_a_readable_message(api):
    r = _post(api, "/aws/prepare", {"account_id": "12345"})
    assert r.status_code == 400 and "12 digits" in r.json()["detail"]
    assert _post(api, f"/aws/{ACCT}/connect", {"display_name": "x"}).status_code == 404  # not prepared yet
    _prepare(api)
    assert _post(api, f"/aws/{ACCT}/connect", {"display_name": ""}).status_code == 400
    assert _post(api, "/azure", {"subscription_id": "nope", "display_name": "x"}).status_code == 400
    assert _post(api, "/aws/prepare", {}).status_code == 422  # missing field (pydantic)


def test_aliyun_is_not_available_yet(api):
    for r in (_get(api, "/aliyun/1234567890"), _post(api, "/aliyun/1234567890/test"),
              _post(api, "/aliyun/1234567890/offboard")):
        assert r.status_code == 400 and "not available yet" in r.json()["detail"]


def test_unknown_account_is_404_and_bad_provider_is_400(api):
    assert _get(api, "/aws/000000000000").status_code == 404
    assert _post(api, "/aws/000000000000/test").status_code == 404
    assert _get(api, "/gcp/whatever").status_code == 400


def test_patch_rejects_unknown_fields_empty_bodies_and_bad_values(api):
    p = f"{BASE}/aws/100000000003"
    # fields that are not editable are ignored by the request model, leaving nothing to change
    assert api.client.patch(p, json={"status": "connected"}, headers=_h()).status_code == 400
    assert api.client.patch(p, json={"external_id": "x"}, headers=_h()).status_code == 400
    assert api.client.patch(p, json={}, headers=_h()).status_code == 400
    assert api.client.patch(p, json={"display_name": ""}, headers=_h()).status_code == 400
    assert api.client.patch(p, json={"business_unit": "Nope"}, headers=_h()).status_code == 400
    assert api.client.patch(p, json={"scope_core": None}, headers=_h()).status_code == 400
    assert store.get("aws", "100000000003")["status"] == "pending"
    r = api.client.patch(p, json={"business_unit": "Brand New", "confirm_new_business_unit": True}, headers=_h())
    assert r.status_code == 200 and r.json()["account"]["business_unit"] == "Brand New"


def test_patch_can_change_scopes(api):
    r = api.client.patch(f"{BASE}/aws/100000000004", json={"scope_core": False}, headers=_h())
    assert r.status_code == 200 and r.json()["account"]["scope_core"] is False
    assert store.get("aws", "100000000004")["scope_billing"] is True


# ── Protected offboarding over HTTP ─────────────────────────────────────────

@pytest.mark.parametrize("account", ["100000000002", "100000000001"])
def test_protected_accounts_need_the_exact_phrase_over_http(api, account):
    for body in ({}, {"risk_confirmation": None}, {"risk_confirmation": ""}, {"risk_confirmation": "yes"},
                 {"risk_confirmation": PHRASE.rstrip(".")}, {"risk_confirmation": PHRASE.lower()},
                 {"risk_confirmation": PHRASE + " "}):
        r = _post(api, f"/aws/{account}/offboard", body)
        assert r.status_code == 400, body
        assert PHRASE in r.json()["detail"]
        assert store.get("aws", account)["status"] == "pending"
    ok = _post(api, f"/aws/{account}/offboard", {"risk_confirmation": PHRASE})
    assert ok.status_code == 200 and ok.json()["account"]["status"] == "offboarded"
    notes = [a.get("note") for a in _get(api, f"/aws/{account}").json()["audit"] if a["action"] == "offboard"]
    assert notes == ["risk confirmation given"]


def test_ordinary_accounts_offboard_without_a_phrase_and_vanish_from_every_picker(api):
    acct = "100000000004"
    pickers = ["/api/billing-by-service/accounts", "/api/ri-sp-savings/accounts",
               "/api/security-hub/accounts", "/api/waf-monitoring/accounts"]
    for p in pickers:
        assert acct in [a["account_id"] for a in api.client.get(p, headers=_h()).json()["accounts"]]
    assert _post(api, f"/aws/{acct}/offboard").status_code == 200
    for p in pickers:
        assert acct not in [a["account_id"] for a in api.client.get(p, headers=_h()).json()["accounts"]], p


def test_an_offboarded_account_is_rejected_by_the_billing_report(api):
    acct = "100000000004"
    assert api.client.get(f"/api/billing-by-service/report?accounts={acct}&month=2026-01", headers=_h()).status_code != 400
    _post(api, f"/aws/{acct}/offboard")
    r = api.client.get(f"/api/billing-by-service/report?accounts={acct}&month=2026-01", headers=_h())
    assert r.status_code == 400 and acct in r.json()["detail"]
    assert api.calls == []  # offboarding and the rejection made no cloud calls


# ── Role permissions ────────────────────────────────────────────────────────

def test_permissions_route_returns_the_account_and_the_role_permissions(api):
    r = _get(api, "/aws/100000000003/permissions")
    assert r.status_code == 200
    body = r.json()
    assert body["account"]["identifier"] == "100000000003"
    assert body["permissions"]["ok"] is True
    assert body["permissions"]["managed_policies"][0]["name"] == "ReadOnlyAccess"
    assert api.permission_calls == ["100000000003"]
    assert "external_id" not in body["account"] and "external_id" not in body["permissions"]
    assert api.calls == []  # a permissions view is not a connection test


def test_permissions_route_lists_every_role_the_account_has(api):
    api.infraops = dict(api.permissions)  # this account also has the InfraOps role
    body = _get(api, "/aws/100000000003/permissions").json()
    roles = {r["key"]: r for r in body["roles"]}
    assert roles["primary"]["role_name"] == "PortalReadOnlyRole"
    assert roles["infraops"]["role_name"] == "JobPortal-CrossAccount-InfraOps-1000"
    assert roles["infraops"]["permissions"]["ok"] is True
    assert "'external_id'" not in str(body) and "jobportal-infra-ops" not in str(body)

    api.infraops = {"ok": False, "code": "not_provisioned", "message": "This account does not have the role."}
    other = _get(api, "/aws/100000000004/permissions").json()
    assert [r["permissions"].get("code") for r in other["roles"] if r["key"] == "infraops"] == ["not_provisioned"]


def test_permissions_route_works_for_the_management_team_and_is_audited(api):
    assert _get(api, "/aws/100000000003/permissions", who=MGMT).status_code == 200
    audit = _get(api, "/aws/100000000003").json()["audit"]
    assert [a for a in audit if a["action"] == "permissions_view" and a["actor"] == MGMT]


def test_permissions_route_passes_a_failed_assume_through_without_raw_aws_text(api):
    api.permissions = {"ok": False, "code": "access_denied", "message": BAD["message"]}
    r = _get(api, "/aws/100000000003/permissions")
    assert r.status_code == 200
    assert r.json()["permissions"] == api.permissions


def test_permissions_route_is_aws_only_and_refuses_offboarded_accounts(api):
    assert _get(api, f"/azure/{GUID}/permissions").status_code in (400, 404)
    _post(api, "/aws/100000000004/offboard")
    assert _get(api, "/aws/100000000004/permissions").status_code == 409
    assert _get(api, "/aws/000000000000/permissions").status_code == 404


def test_permissions_route_shares_the_connection_test_rate_limit(api):
    codes = [_get(api, "/aws/100000000003/permissions").status_code for _ in range(32)]
    assert codes[:30] == [200] * 30
    assert codes[30:] == [429, 429]


# ── Testing, Azure, guide ───────────────────────────────────────────────────

def test_single_test_and_test_all(api):
    r = _post(api, "/aws/100000000003/test")
    assert r.status_code == 200 and r.json()["result"]["ok"] is True
    assert store.get("aws", "100000000003")["status"] == "connected"
    _post(api, "/aws/100000000004/offboard")
    all_ = _post(api, "/test-all")
    assert all_.status_code == 200
    body = all_.json()
    assert body["tested"] == 29 and body["connected"] == 29 and body["failed"] == 0
    assert "100000000004" not in api.calls


def test_testing_an_offboarded_account_is_refused(api):
    _post(api, "/aws/100000000004/offboard")
    assert _post(api, "/aws/100000000004/test").status_code == 409


def test_test_requests_are_rate_limited_to_30_per_minute(api):
    codes = [_post(api, "/aws/100000000003/test").status_code for _ in range(32)]
    assert codes[:30] == [200] * 30
    assert codes[30:] == [429, 429]


def test_azure_subscription_can_be_added_and_connects(api):
    r = _post(api, "/azure", {"subscription_id": GUID.upper(), "display_name": "Azure Prod"})
    assert r.status_code == 200
    assert r.json()["account"]["status"] == "connected" and r.json()["account"]["identifier"] == GUID
    assert _post(api, "/azure", {"subscription_id": GUID, "display_name": "Again"}).status_code == 409
    api.outcome = dict(BAD)
    r2 = _post(api, "/azure", {"subscription_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "display_name": "Azure Dev"})
    assert r2.status_code == 200 and r2.json()["result"]["ok"] is False


def test_azure_guide_route_is_not_swallowed_by_the_generic_route(api):
    creds = {"tenant_id": "00000000-0000-0000-0000-000000000000", "client_id": "cccccccc-0000-1111-2222-333333333333",
             "client_secret": "SUPERSECRET-VALUE", "subscription_id": GUID}
    from app import azure_monitor

    api.monkeypatch.setattr(azure_monitor, "_get_credentials", lambda: dict(creds))
    r = api.client.get(f"{BASE}/azure/guide?subscription_id={GUID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["application_id"] == creds["client_id"] and body["command"].endswith(f"/subscriptions/{GUID}")
    assert "SUPERSECRET" not in r.text


def test_azure_guide_rejects_a_bad_subscription_and_reports_missing_credentials(api):
    r = api.client.get(f"{BASE}/azure/guide?subscription_id=x;rm -rf /", headers=_h())
    assert r.status_code == 400  # the format is checked before anything is read
    r = api.client.get(f"{BASE}/azure/guide", headers=_h())
    assert r.status_code == 503 and "not configured" in r.json()["detail"]


def test_azure_resources_route_still_uses_the_default_subscription_path(api):
    from app import azure_monitor

    sentinel = {"headers": ["Name"], "rows": [["vm1"]], "count": 1}
    api.monkeypatch.setattr(azure_monitor, "list_vms", lambda force_refresh=False: sentinel)
    r = api.client.get("/api/azure-resources/data?resource_type=vm", headers=_h())
    assert r.status_code == 200 and r.json() == sentinel
