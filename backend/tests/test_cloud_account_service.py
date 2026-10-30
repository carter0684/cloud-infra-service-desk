"""Tests for the cloud account use-cases (.kiro/specs/cloud-account-onboarding, task 4)."""

import pytest

from app import cloud_account_access as access
from app import cloud_account_service as svc
from app import cloud_account_store as store

ACCT = "123456789012"
GUID = "11111111-2222-3333-4444-555555555555"
GUID2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TASK_ROLE = "arn:aws:iam::100000000002:role/JobPortalComputeStack-JobPortalTaskRoleABC123"
ADMIN = "admin@example.com"
OK = {"ok": True, "code": "ok", "message": "Connected."}
BAD = {"ok": False, "code": "access_denied", "message": "Role not found, or its trust policy or External ID does not match."}


@pytest.fixture
def world(monkeypatch):
    """Registry on an in-memory SQLite database, seeded, with the AWS/Azure tests under test control."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app import azure_monitor, database

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setenv("ENV", "local")

    def _no_azure():
        raise azure_monitor.AzureError("no azure credentials in tests")

    monkeypatch.setattr(azure_monitor, "_get_credentials", _no_azure)
    with store._lock:
        store._snap.update(entries=None, loaded_at=0.0, from_storage=False)
    svc._rate_calls.clear()

    class World:
        outcome = dict(OK)
        calls: list = []

    w = World()
    w.calls = []

    def fake_aws_test(entry):
        w.calls.append(entry["identifier"])
        if isinstance(w.outcome, Exception):
            raise w.outcome
        return dict(w.outcome)

    monkeypatch.setattr(access, "test_aws_connection", fake_aws_test)
    monkeypatch.setattr(access, "task_role_arn", lambda: TASK_ROLE)
    monkeypatch.setattr(azure_monitor, "test_azure_subscription", lambda sub: (w.calls.append(sub), dict(w.outcome))[1], raising=False)
    monkeypatch.setattr(svc, "TEST_ALL_CONCURRENCY", 1)  # one shared in-memory SQLite connection
    store.seed_if_needed()
    yield w
    with store._lock:
        store._snap.update(entries=None, loaded_at=0.0, from_storage=False)
    svc._rate_calls.clear()


def _prepare(account=ACCT):
    return svc.prepare_aws(account, ADMIN)


def _connect(account=ACCT, name="My Account", **kw):
    return svc.connect_aws(account, name, ADMIN, **kw)


def _status(provider, ident):
    return store.get(provider, ident)["status"]


def _err(fn, *a, **kw):
    with pytest.raises(svc.ServiceError) as exc:
        fn(*a, **kw)
    return exc.value


# ── Validation ──────────────────────────────────────────────────────────────

def test_aliyun_is_rejected_with_a_clear_message(world):
    e = _err(svc.validate_provider, "aliyun")
    assert e.status == 400 and "not available yet" in e.message
    assert svc.validate_provider("AWS") == "aws"
    assert _err(svc.validate_provider, "gcp").status == 400


@pytest.mark.parametrize("bad", ["", "12345", "1234567890123", "12345678901a", " 12 "])
def test_aws_identifier_must_be_12_digits(world, bad):
    assert _err(svc.validate_identifier, "aws", bad).status == 400


def test_azure_identifier_must_be_a_guid_and_is_lowercased(world):
    assert svc.validate_identifier("azure", GUID.upper()) == GUID
    assert _err(svc.validate_identifier, "azure", "not-a-guid").status == 400


@pytest.mark.parametrize("bad", ["", "   ", "x" * 101, "bad\x00name", "line\nbreak", "tab\tname"])
def test_display_name_rules(world, bad):
    assert _err(svc.validate_display_name, bad).status == 400


def test_display_name_is_trimmed_and_100_chars_is_allowed(world):
    assert svc.validate_display_name("  Prod API  ") == "Prod API"
    assert len(svc.validate_display_name("x" * 100)) == 100


def test_business_unit_must_exist_or_be_confirmed(world):
    assert svc.validate_business_unit(None) == "Other"
    assert svc.validate_business_unit("BU-B") == "BU-B"
    e = _err(svc.validate_business_unit, "Brand New")
    assert "new business unit" in e.message
    assert svc.validate_business_unit("Brand New", confirm_new=True) == "Brand New"


# ── prepare / setup ─────────────────────────────────────────────────────────

def test_prepare_creates_a_draft_with_its_own_external_id(world):
    setup = _prepare()
    assert setup["role_name"] == "JobPortal-CrossAccount-InfraOps-1234"
    assert setup["role_arn"] == f"arn:aws:iam::{ACCT}:role/JobPortal-CrossAccount-InfraOps-1234"
    assert setup["portal_role_arn"] == TASK_ROLE
    assert len(setup["external_id"]) >= 32
    e = store.get("aws", ACCT)
    assert (e["status"], e["access_mode"], e["never_connected"], e["seeded"]) == ("pending", "infraops", True, False)
    assert e["external_id"] == setup["external_id"]


def test_prepare_twice_reuses_the_same_external_id(world):
    first = _prepare()
    second = _prepare()
    assert first["external_id"] == second["external_id"]


def test_two_accounts_get_different_external_ids(world):
    assert _prepare(ACCT)["external_id"] != _prepare("999988887777")["external_id"]


def test_prepare_refuses_existing_offboarded_and_invalid_accounts(world):
    assert _err(svc.prepare_aws, "100000000003", ADMIN).status == 409  # seeded legacy account
    _prepare()
    world.outcome = dict(OK)
    _connect()
    assert _err(svc.prepare_aws, ACCT, ADMIN).status == 409  # already connected
    svc.offboard("aws", ACCT, ADMIN)
    e = _err(svc.prepare_aws, ACCT, ADMIN)
    assert e.status == 409 and "Onboard again" in e.message
    assert _err(svc.prepare_aws, "123", ADMIN).status == 400


def test_a_new_draft_is_invisible_to_the_other_pages(world):
    from app.billing_by_service import ACCOUNT_ALIAS

    _prepare()
    assert ACCT not in ACCOUNT_ALIAS


# ── template ────────────────────────────────────────────────────────────────

def test_template_download_returns_yaml_for_the_account_and_is_audited(world):
    setup = _prepare()
    filename, text = svc.template_for(ACCT, ADMIN)
    assert filename == f"JobPortal-InfraOps-Role-{ACCT}.yaml"
    assert setup["external_id"] in text and TASK_ROLE in text and "JobPortal-CrossAccount-InfraOps-1234" in text
    assert "template_download" in [a["action"] for a in store.list_audit("aws", ACCT)]


def test_template_is_refused_for_legacy_and_offboarded_accounts(world):
    assert _err(svc.template_for, "100000000003", ADMIN).status == 409
    _prepare()
    _connect()
    svc.offboard("aws", ACCT, ADMIN)
    assert _err(svc.template_for, ACCT, ADMIN).status == 409
    assert _err(svc.template_for, "000000000000", ADMIN).status == 404


# ── connect ─────────────────────────────────────────────────────────────────

def test_connect_success_marks_connected_and_shows_the_account_everywhere(world):
    from app.billing_by_service import ACCOUNT_ALIAS
    from app.cost_explorer import ACCOUNTS, ACCOUNT_BU

    _prepare()
    out = _connect(name="Brand New Prod", business_unit="BU-C")
    assert out["result"]["ok"] is True
    e = store.get("aws", ACCT)
    assert (e["status"], e["never_connected"], e["last_error"]) == ("connected", False, None)
    assert e["display_name"] == "Brand New Prod" and e["last_verified_at"]
    assert ACCOUNT_ALIAS[ACCT] == "Brand New Prod" and ACCOUNTS[ACCT] == "Brand New Prod"
    assert ACCOUNT_BU[ACCT] == "BU-C"
    assert "external_id" not in out["account"]


def test_connect_failure_keeps_the_entry_failed_hidden_and_explained(world):
    from app.billing_by_service import ACCOUNT_ALIAS

    _prepare()
    world.outcome = dict(BAD)
    out = _connect()
    assert out["result"]["ok"] is False
    e = store.get("aws", ACCT)
    assert (e["status"], e["never_connected"]) == ("failed", True)
    assert e["last_error"] == BAD["message"] and e["last_error_code"] == "access_denied"
    assert ACCT not in ACCOUNT_ALIAS
    # fixing the role and pressing Connect again works
    world.outcome = dict(OK)
    assert _connect()["result"]["ok"] is True
    assert ACCT in ACCOUNT_ALIAS


def test_connect_status_changes_are_audited_once_each(world):
    _prepare()
    world.outcome = dict(BAD)
    _connect()
    world.outcome = dict(OK)
    _connect()
    changes = [(a["before"]["status"], a["after"]["status"]) for a in store.list_audit("aws", ACCT) if a["action"] == "status_change"]
    assert sorted(changes) == [("failed", "connected"), ("pending", "failed")]


def test_connect_validates_before_testing(world):
    _prepare()
    assert _err(_connect, name="").status == 400
    assert _err(_connect, name="ok", business_unit="Unknown BU").status == 400
    assert _err(svc.connect_aws, "bad", "name", ADMIN).status == 400
    assert world.calls == []  # no AWS call was made for invalid input


def test_connect_requires_a_prepared_infraops_account(world):
    assert _err(svc.connect_aws, "000000000000", "n", ADMIN).status == 404
    assert _err(svc.connect_aws, "100000000003", "n", ADMIN).status == 409  # legacy
    _prepare()
    _connect()
    svc.offboard("aws", ACCT, ADMIN)
    assert _err(_connect).status == 409


def test_connect_warns_about_a_duplicate_name_but_saves(world):
    _prepare()
    out = _connect(name="network")
    assert "already called" in out["warning"]
    assert store.get("aws", ACCT)["display_name"] == "network"


def test_test_requests_are_rate_limited_per_admin(world):
    _prepare()
    for _ in range(svc.TEST_RATE_LIMIT_PER_MINUTE):
        svc.check_test_rate(ADMIN)
    e = _err(_connect)
    assert e.status == 429
    svc.check_test_rate("someone.else@example.com")  # another admin is not affected


def test_rate_limit_window_slides(world, monkeypatch):
    now = {"t": 100.0}
    monkeypatch.setattr(svc, "time", type("T", (), {"monotonic": staticmethod(lambda: now["t"])}))
    for _ in range(svc.TEST_RATE_LIMIT_PER_MINUTE):
        svc.check_test_rate(ADMIN)
    assert _err(svc.check_test_rate, ADMIN).status == 429
    now["t"] += 61
    svc.check_test_rate(ADMIN)


# ── Azure ───────────────────────────────────────────────────────────────────

def test_add_azure_success_and_failure(world):
    out = svc.add_azure(GUID.upper(), "Azure Prod", ADMIN)
    e = store.get("azure", GUID)
    assert out["result"]["ok"] and e["status"] == "connected" and e["access_mode"] == "arm"
    assert e["display_name"] == "Azure Prod"

    world.outcome = dict(BAD)
    out2 = svc.add_azure(GUID2, "Azure Dev", ADMIN)
    assert out2["result"]["ok"] is False
    assert store.get("azure", GUID2)["status"] == "failed"


def test_add_azure_rejects_duplicates_and_bad_input(world):
    svc.add_azure(GUID, "Azure Prod", ADMIN)
    assert _err(svc.add_azure, GUID, "Again", ADMIN).status == 409
    assert _err(svc.add_azure, "nope", "x", ADMIN).status == 400
    assert _err(svc.add_azure, GUID2, "", ADMIN).status == 400


def test_azure_test_missing_in_this_build_fails_cleanly(world, monkeypatch):
    from app import azure_monitor

    monkeypatch.delattr(azure_monitor, "test_azure_subscription", raising=False)
    out = svc.add_azure(GUID, "Azure Prod", ADMIN)
    assert out["result"]["ok"] is False and out["result"]["code"] == "not_available"


# ── update ──────────────────────────────────────────────────────────────────

def test_update_renames_and_changes_bu_and_audits_before_and_after(world):
    out = svc.update("aws", "100000000003", {"display_name": "network-main", "business_unit": "BU-B"}, ADMIN)
    assert out["account"]["display_name"] == "network-main"
    row = [a for a in store.list_audit("aws", "100000000003") if a["action"] == "update"][0]
    assert row["before"] == {"display_name": "network", "business_unit": "BU-A"}
    assert row["after"] == {"display_name": "network-main", "business_unit": "BU-B"}


def test_renaming_does_not_touch_the_cloud_or_the_access_path(world):
    svc.update("aws", "100000000003", {"display_name": "renamed"}, ADMIN)
    assert world.calls == []
    e = store.get("aws", "100000000003")
    assert e["access_mode"] == "legacy" and e["status"] == "pending"


def test_update_scope_flags(world):
    from app.cost_explorer import ACCOUNTS

    svc.update("aws", "100000000004", {"scope_core": False}, ADMIN)
    assert "100000000004" not in ACCOUNTS
    assert _err(svc.update, "aws", "100000000004", {"scope_core": "no"}, ADMIN).status == 400
    svc.update("aws", "100000000004", {"scope_core": True}, ADMIN)
    assert "100000000004" in ACCOUNTS


def test_scopes_are_not_allowed_on_azure(world):
    svc.add_azure(GUID, "Azure Prod", ADMIN)
    assert _err(svc.update, "azure", GUID, {"scope_core": True}, ADMIN).status == 400


def test_update_rejects_unknown_fields_empty_changes_and_bad_values(world):
    assert _err(svc.update, "aws", "100000000003", {"status": "connected"}, ADMIN).status == 400
    assert _err(svc.update, "aws", "100000000003", {"external_id": "x"}, ADMIN).status == 400
    assert _err(svc.update, "aws", "100000000003", {}, ADMIN).status == 400
    assert _err(svc.update, "aws", "100000000003", {"display_name": ""}, ADMIN).status == 400
    assert _err(svc.update, "aws", "100000000003", {"business_unit": "Nope"}, ADMIN).status == 400
    assert _err(svc.update, "aws", "000000000000", {"display_name": "x"}, ADMIN).status == 404
    assert store.get("aws", "100000000003")["status"] == "pending"


def test_update_with_no_actual_change_writes_no_audit_entry(world):
    svc.update("aws", "100000000003", {"display_name": "network"}, ADMIN)
    assert [a for a in store.list_audit("aws", "100000000003") if a["action"] == "update"] == []


def test_update_confirms_a_new_business_unit(world):
    assert _err(svc.update, "aws", "100000000003", {"business_unit": "BU-E"}, ADMIN).status == 400
    svc.update("aws", "100000000003", {"business_unit": "BU-E"}, ADMIN, confirm_new_bu=True)
    assert store.get("aws", "100000000003")["business_unit"] == "BU-E"
    assert "BU-E" in svc.known_business_units()


# ── test one / all ──────────────────────────────────────────────────────────

def test_test_one_updates_the_verification_time_and_status(world):
    out = svc.test_one("aws", "100000000003", ADMIN)
    assert out["result"]["ok"] and store.get("aws", "100000000003")["status"] == "connected"
    assert store.get("aws", "100000000003")["last_verified_at"]


def test_a_failing_legacy_account_stays_visible_as_before(world):
    from app.billing_by_service import ACCOUNT_ALIAS

    world.outcome = dict(BAD)
    svc.test_one("aws", "100000000003", ADMIN)
    assert store.get("aws", "100000000003")["status"] == "failed"
    assert "100000000003" in ACCOUNT_ALIAS  # established account: shown whatever the last test said


def test_test_one_refuses_offboarded_and_missing_accounts(world):
    svc.offboard("aws", "100000000003", ADMIN)
    assert _err(svc.test_one, "aws", "100000000003", ADMIN).status == 409
    assert _err(svc.test_one, "aws", "000000000000", ADMIN).status == 404


def test_test_all_covers_every_non_offboarded_account(world):
    svc.offboard("aws", "100000000003", ADMIN)
    out = svc.test_all(ADMIN)
    assert out["tested"] == 29 and out["connected"] == 29 and out["failed"] == 0
    assert "100000000003" not in world.calls


def test_test_all_one_crash_does_not_block_the_others(world, monkeypatch):
    def flaky(entry):
        if entry["identifier"] == "100000000003":
            raise RuntimeError("boom")
        return dict(OK)

    monkeypatch.setattr(access, "test_aws_connection", flaky)
    out = svc.test_all(ADMIN)
    assert out["tested"] == 30 and out["failed"] == 1 and out["connected"] == 29
    bad = [r for r in out["results"] if not r["result"]["ok"]][0]
    assert bad["result"]["code"] == "unexpected" and "boom" not in str(bad)


def test_test_all_counts_as_one_rate_limited_request(world):
    svc.test_all(ADMIN)
    assert len(svc._rate_calls[ADMIN]) == 1


# ── offboard / onboard ──────────────────────────────────────────────────────

def test_offboard_hides_the_account_everywhere_keeps_history_and_audits(world):
    from app.billing_by_service import ACCOUNT_ALIAS
    from app.cost_explorer import ACCOUNTS, ACCOUNT_BU

    out = svc.offboard("aws", "100000000004", ADMIN)
    assert out["account"]["status"] == "offboarded"
    for m in (ACCOUNT_ALIAS, ACCOUNTS, ACCOUNT_BU):
        assert "100000000004" not in m
    assert store.get("aws", "100000000004") is not None  # kept, not deleted
    row = [a for a in store.list_audit("aws", "100000000004") if a["action"] == "offboard"][0]
    assert row["actor"] == ADMIN and row.get("note") is None


def test_offboard_twice_and_missing_account(world):
    svc.offboard("aws", "100000000004", ADMIN)
    assert _err(svc.offboard, "aws", "100000000004", ADMIN).status == 409
    assert _err(svc.offboard, "aws", "000000000000", ADMIN).status == 404


def test_offboard_makes_no_cloud_calls(world):
    svc.offboard("aws", "100000000004", ADMIN)
    assert world.calls == []


@pytest.mark.parametrize("protected_id", ["100000000002", "100000000001"])
@pytest.mark.parametrize("attempt", [
    None, "", "I understand the risk and agree to offboard",            # missing full stop
    "i understand the risk and agree to offboard.",                      # wrong case
    "I understand the risk and agree to offboard. ",                     # trailing space
    " I understand the risk and agree to offboard.",                     # leading space
    "I understand the risk and agree to  offboard.",                     # double space
    "yes", "I understand the risk and agree to offboard.\n",
])
def test_protected_accounts_need_the_exact_phrase(world, protected_id, attempt):
    e = _err(svc.offboard, "aws", protected_id, ADMIN, risk_confirmation=attempt)
    assert e.status == 400 and svc.RISK_PHRASE in e.message
    assert store.get("aws", protected_id)["status"] == "pending"  # unchanged


@pytest.mark.parametrize("protected_id", ["100000000002", "100000000001"])
def test_protected_accounts_can_be_offboarded_with_the_exact_phrase_and_it_is_audited(world, protected_id):
    assert svc.RISK_PHRASE == "I understand the risk and agree to offboard."
    out = svc.offboard("aws", protected_id, ADMIN, risk_confirmation=svc.RISK_PHRASE)
    assert out["account"]["status"] == "offboarded"
    row = [a for a in store.list_audit("aws", protected_id) if a["action"] == "offboard"][0]
    assert row["note"] == "risk confirmation given"


def test_normal_accounts_do_not_need_the_phrase_and_a_phrase_is_ignored(world):
    svc.offboard("aws", "100000000004", ADMIN, risk_confirmation="anything")
    assert store.get("aws", "100000000004")["status"] == "offboarded"


def test_onboard_again_restores_only_when_the_test_passes(world):
    from app.billing_by_service import ACCOUNT_ALIAS

    svc.offboard("aws", "100000000004", ADMIN)
    world.outcome = dict(BAD)
    out = svc.onboard("aws", "100000000004", ADMIN)
    assert out["result"]["ok"] is False
    e = store.get("aws", "100000000004")
    assert e["status"] == "offboarded" and e["last_error"] == BAD["message"]
    assert "100000000004" not in ACCOUNT_ALIAS

    world.outcome = dict(OK)
    out = svc.onboard("aws", "100000000004", ADMIN)
    assert out["account"]["status"] == "connected"
    assert "100000000004" in ACCOUNT_ALIAS


def test_onboard_requires_an_offboarded_account(world):
    assert _err(svc.onboard, "aws", "100000000004", ADMIN).status == 409
    assert _err(svc.onboard, "aws", "000000000000", ADMIN).status == 404


def test_offboard_drops_the_cached_infraops_session(world, monkeypatch):
    dropped = []
    monkeypatch.setattr(access, "invalidate", lambda account_id=None: dropped.append(account_id))
    _prepare()
    _connect()
    dropped.clear()
    svc.offboard("aws", ACCT, ADMIN)
    assert ACCT in dropped


# ── discard ─────────────────────────────────────────────────────────────────

def test_discard_removes_a_never_connected_draft(world):
    _prepare()
    assert svc.discard_draft("aws", ACCT, ADMIN) == {"discarded": True}
    assert store.get("aws", ACCT) is None
    assert "discard" in [a["action"] for a in store.list_audit("aws", ACCT)]


def test_discard_is_refused_for_connected_failed_after_connect_seeded_and_missing(world):
    _prepare()
    _connect()
    e = _err(svc.discard_draft, "aws", ACCT, ADMIN)
    assert e.status == 409 and "Offboard" in e.message
    assert _err(svc.discard_draft, "aws", "100000000003", ADMIN).status == 409
    assert _err(svc.discard_draft, "aws", "000000000000", ADMIN).status == 404


def test_a_failed_draft_that_never_connected_can_still_be_discarded(world):
    _prepare()
    world.outcome = dict(BAD)
    _connect()
    assert svc.discard_draft("aws", ACCT, ADMIN)["discarded"] is True


# ── listing and secrecy ─────────────────────────────────────────────────────

def test_list_counts_per_provider_and_status(world):
    svc.add_azure(GUID, "Azure Prod", ADMIN)
    svc.offboard("aws", "100000000004", ADMIN)
    out = svc.list_with_counts()
    assert out["counts"]["aws"]["total"] == 30 and out["counts"]["aws"]["offboarded"] == 1
    assert out["counts"]["aws"]["pending"] == 29
    assert out["counts"]["azure"]["connected"] == 1 and out["counts"]["azure"]["total"] == 1
    assert "BU-A" in out["business_units"]


def test_external_id_never_appears_in_list_detail_connect_or_audit(world):
    secret = _prepare()["external_id"]
    out = _connect()
    svc.update("aws", ACCT, {"display_name": "Renamed"}, ADMIN)
    svc.test_one("aws", ACCT, ADMIN)
    svc.template_for(ACCT, ADMIN)
    svc.offboard("aws", ACCT, ADMIN)
    everything = repr(out) + repr(svc.list_with_counts()) + repr(svc.detail("aws", ACCT)) \
        + repr(store.list_audit("aws", ACCT, 100))
    assert secret not in everything
    assert svc.list_with_counts()["accounts"]  # non-trivial
    assert svc.detail("aws", ACCT)["account"]["external_id_set"] is True


def test_detail_includes_the_audit_trail_newest_first(world):
    _prepare()
    _connect()
    d = svc.detail("aws", ACCT)
    actions = [a["action"] for a in d["audit"]]
    assert actions[0] == "connect" and actions[-1] == "prepare"


def test_public_entry_for_infraops_includes_the_role_arn_but_not_the_secret(world):
    _prepare()
    pub = svc.public_entry(store.get("aws", ACCT))
    assert pub["role_arn"] == f"arn:aws:iam::{ACCT}:role/JobPortal-CrossAccount-InfraOps-1234"
    assert "external_id" not in pub and pub["external_id_set"] is True


# ── Role permissions view ───────────────────────────────────────────────────

PERMS_OK = {
    "ok": True,
    "identity": {"account": "100000000003", "kind": "role", "role_name": "r", "arn": "arn:aws:iam::100000000003:role/r"},
    "managed_policies": [], "inline_policies": [],
    "simulation": {"available": True, "write_checked": 29, "write_allowed": [], "data_read_checked": 9, "data_read_allowed": []},
    "summary": "Read-only: none of the 29 write actions checked are allowed.",
}


@pytest.fixture
def perms(world, monkeypatch):
    seen = []

    def fake(entry):
        seen.append(entry["identifier"])
        return dict(PERMS_OK)

    monkeypatch.setattr(access, "describe_role_permissions", fake)
    # The second role (the hand-made InfraOps role) is probed separately; by default the
    # account does not have it.
    monkeypatch.setattr(access, "describe_shared_infraops_role", lambda entry: dict(NOT_PROVISIONED))
    return seen


NOT_PROVISIONED = {"ok": False, "code": "not_provisioned", "message": "This account does not have the role."}


def test_a_legacy_account_lists_its_read_only_role_and_reports_the_missing_infraops_role(world, perms):
    out = svc.role_permissions("aws", "100000000004", ADMIN)
    primary, infraops = out["roles"]
    assert primary["key"] == "primary" and primary["role_name"] == "PortalReadOnlyRole"
    assert primary["permissions"]["ok"] is True
    assert infraops["key"] == "infraops" and infraops["role_name"] == "JobPortal-CrossAccount-InfraOps-1000"
    assert infraops["permissions"]["code"] == "not_provisioned"
    assert out["permissions"] == primary["permissions"]  # unchanged field for older callers


def test_an_account_that_also_has_the_infraops_role_shows_both(world, perms, monkeypatch):
    monkeypatch.setattr(access, "describe_shared_infraops_role", lambda entry: dict(PERMS_OK))
    out = svc.role_permissions("aws", "100000000003", ADMIN)
    assert [r["key"] for r in out["roles"]] == ["primary", "infraops"]
    assert out["roles"][0]["role_name"] == "PortalReadOnlyRole"
    assert out["roles"][1]["role_name"] == "JobPortal-CrossAccount-InfraOps-1000"
    assert out["roles"][1]["permissions"]["ok"] is True
    audit = [a for a in store.list_audit("aws", "100000000003") if a["action"] == "permissions_view"]
    assert audit[-1]["note"] == "ok; infraops=ok"


def test_the_audit_note_records_when_the_infraops_role_is_absent(world, perms):
    svc.role_permissions("aws", "100000000004", ADMIN)
    audit = [a for a in store.list_audit("aws", "100000000004") if a["action"] == "permissions_view"]
    assert audit[-1]["note"] == "ok; infraops=not_provisioned"


def test_the_core_account_is_described_as_an_access_key_with_no_role_name(world, perms):
    out = svc.role_permissions("aws", "100000000001", ADMIN)
    assert out["roles"][0]["label"] == "IAM user access key" and out["roles"][0]["role_name"] is None


def test_an_onboarded_infraops_account_lists_only_its_own_role(world, perms, monkeypatch):
    probed = []
    monkeypatch.setattr(access, "describe_shared_infraops_role", lambda entry: probed.append(1) or dict(PERMS_OK))
    _prepare()
    out = svc.role_permissions("aws", ACCT, ADMIN)
    assert [r["key"] for r in out["roles"]] == ["primary"]
    assert out["roles"][0]["role_name"] == access.role_name_for(ACCT)
    assert probed == []  # that role IS the primary one; nothing extra to probe


def test_role_permissions_returns_the_account_and_audits_the_view(world, perms):
    out = svc.role_permissions("aws", "100000000003", ADMIN)
    assert out["account"]["identifier"] == "100000000003"
    assert out["permissions"]["ok"] is True
    assert perms == ["100000000003"]
    assert "external_id" not in out["account"] and "external_id" not in out["permissions"]
    audit = [a for a in store.list_audit("aws", "100000000003") if a["action"] == "permissions_view"]
    assert len(audit) == 1 and audit[0]["actor"] == ADMIN
    assert world.calls == []  # not a connection test: status and last_verified_at stay as they were


def test_role_permissions_does_not_change_the_account(world, perms):
    before = dict(store.get("aws", "100000000003"))
    svc.role_permissions("aws", "100000000003", ADMIN)
    assert store.get("aws", "100000000003") == before


def test_role_permissions_reports_a_failed_assume_and_audits_the_code(world, monkeypatch):
    monkeypatch.setattr(access, "describe_role_permissions", lambda entry: dict(BAD))
    monkeypatch.setattr(access, "describe_shared_infraops_role", lambda entry: dict(NOT_PROVISIONED))
    out = svc.role_permissions("aws", "100000000003", ADMIN)
    assert out["permissions"] == BAD
    audit = [a for a in store.list_audit("aws", "100000000003") if a["action"] == "permissions_view"]
    assert audit[0]["note"] == "access_denied; infraops=not_provisioned"


def test_role_permissions_is_aws_only(world, perms):
    assert _err(svc.role_permissions, "azure", GUID, ADMIN).status == 400
    assert _err(svc.role_permissions, "aliyun", "1234567890", ADMIN).status == 400
    assert perms == []


def test_role_permissions_validates_and_looks_up_the_account(world, perms):
    assert _err(svc.role_permissions, "aws", "12345", ADMIN).status == 400
    assert _err(svc.role_permissions, "aws", "000000000000", ADMIN).status == 404
    assert perms == []


def test_role_permissions_refuses_offboarded_accounts(world, perms):
    svc.offboard("aws", "100000000004", ADMIN)
    assert _err(svc.role_permissions, "aws", "100000000004", ADMIN).status == 409
    assert perms == []


def test_role_permissions_can_be_tried_on_a_draft_that_has_an_external_id(world, perms):
    _prepare()
    out = svc.role_permissions("aws", ACCT, ADMIN)  # the role may already exist before Connect is pressed
    assert out["permissions"]["ok"] is True and perms == [ACCT]


def test_role_permissions_shares_the_connection_test_rate_limit(world, perms):
    for _ in range(svc.TEST_RATE_LIMIT_PER_MINUTE):
        svc.role_permissions("aws", "100000000003", ADMIN)
    assert _err(svc.role_permissions, "aws", "100000000003", ADMIN).status == 429
    assert _err(svc.test_one, "aws", "100000000003", ADMIN).status == 429  # same bucket
    svc.role_permissions("aws", "100000000003", "other-admin@example.com")  # per user
