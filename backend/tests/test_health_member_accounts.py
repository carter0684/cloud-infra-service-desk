"""Member-account Health events: fills the gap left by the organization API,
which does not return every ACCOUNT_SPECIFIC event (verified live, 2026-10:
Gen-BI 100000000005 had 32 events of its own, the org API returned 13).

Everything is mocked — no AWS calls."""
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app import health_member_accounts as hma

GEN_BI = "100000000005"
OTHER = "111111111111"


def ev(arn, code="AWS_X", **kw):
    return {"arn": arn, "eventTypeCode": code, "eventTypeCategory": "scheduledChange",
            "statusCode": "upcoming", "region": "us-east-1", **kw}


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "arn:aws:iam::1:role/x denied"}}, "DescribeEvents")


@pytest.fixture(autouse=True)
def _fresh_cache():
    hma._INDEX_CACHE.reset_for_tests()
    yield
    hma._INDEX_CACHE.reset_for_tests()


def health_client(events_pages):
    c = MagicMock()
    c.describe_events.side_effect = events_pages
    return c


def fake_accounts(monkeypatch, ids):
    monkeypatch.setattr(hma.cost_explorer, "ACCOUNTS", {i: f"acct-{i}" for i in ids})


# ── _collect / collect_member_events ─────────────────────────────────────────

def test_collects_every_account_and_merges_accounts_per_arn(monkeypatch):
    fake_accounts(monkeypatch, [GEN_BI, OTHER])
    shared = ev("arn:shared")
    per_account = {
        GEN_BI: [shared, ev("arn:only-gen-bi")],
        OTHER: [shared],
    }
    monkeypatch.setattr(hma, "_health_client_for", lambda a: health_client([{"events": per_account[a]}]))
    out = hma._collect()
    assert set(out["by_arn"]) == {"arn:shared", "arn:only-gen-bi"}
    assert sorted(out["by_arn"]["arn:shared"]["accounts"]) == sorted([GEN_BI, OTHER])
    assert out["by_arn"]["arn:only-gen-bi"]["accounts"] == [GEN_BI]
    assert out["failures"] == {}


def test_pages_through_each_account(monkeypatch):
    fake_accounts(monkeypatch, [GEN_BI])
    c = health_client([{"events": [ev("a")], "nextToken": "t"}, {"events": [ev("b")]}])
    monkeypatch.setattr(hma, "_health_client_for", lambda a: c)
    out = hma._collect()
    assert set(out["by_arn"]) == {"a", "b"}
    assert c.describe_events.call_args_list[1].kwargs["nextToken"] == "t"


def test_one_failing_account_does_not_affect_the_others(monkeypatch):
    fake_accounts(monkeypatch, [GEN_BI, OTHER])

    def client_for(a):
        if a == OTHER:
            raise client_error("AccessDenied")
        return health_client([{"events": [ev("arn:ok")]}])

    monkeypatch.setattr(hma, "_health_client_for", client_for)
    out = hma._collect()
    assert set(out["by_arn"]) == {"arn:ok"}
    assert out["failures"] == {OTHER: "AccessDenied"}


def test_account_without_support_plan_is_recorded_as_an_expected_skip(monkeypatch):
    fake_accounts(monkeypatch, [GEN_BI])
    c = MagicMock()
    c.describe_events.side_effect = client_error("SubscriptionRequiredException")
    monkeypatch.setattr(hma, "_health_client_for", lambda a: c)
    out = hma._collect()
    assert out["by_arn"] == {}
    assert out["failures"] == {GEN_BI: "SubscriptionRequiredException"}
    assert "SubscriptionRequiredException" in hma._EXPECTED_SKIP_CODES


def test_collect_never_raises_even_if_everything_fails(monkeypatch):
    fake_accounts(monkeypatch, [GEN_BI, OTHER])
    monkeypatch.setattr(hma, "_health_client_for", MagicMock(side_effect=RuntimeError("boom")))
    out = hma.collect_member_events()
    assert out["by_arn"] == {}
    assert set(out["failures"]) == {GEN_BI, OTHER}


def test_core_account_uses_the_ams_health_client_without_assume_role(monkeypatch):
    ams = MagicMock(name="ams-health")
    monkeypatch.setattr(hma.escalation, "_get_ams_health_client", lambda: ams)
    monkeypatch.setattr(hma.cost_explorer, "_core_session",
                        MagicMock(side_effect=AssertionError("must not AssumeRole into itself")))
    assert hma._health_client_for(hma.CORE_ACCOUNT_ID) is ams


# ── events_missing_from_org ──────────────────────────────────────────────────

def test_only_events_absent_from_the_org_view_are_added_and_tagged():
    member = {"by_arn": {
        "arn:in-org": {"event": ev("arn:in-org"), "accounts": [GEN_BI]},
        "arn:kiro": {"event": ev("arn:kiro", "AWS_KIRO_PLANNED_LIFECYCLE_EVENT"), "accounts": [GEN_BI]},
        "arn:billing": {"event": ev("arn:billing", "AWS_BILLING_PLANNED_LIFECYCLE_EVENT"), "accounts": [OTHER, GEN_BI]},
    }, "failures": {}}
    extra = hma.events_missing_from_org(member, {"arn:in-org"})
    assert {e["arn"] for e in extra} == {"arn:kiro", "arn:billing"}  # no duplicate of the org event
    by = {e["arn"]: e for e in extra}
    assert by["arn:kiro"]["_member_account_ids"] == [GEN_BI]
    assert by["arn:billing"]["_member_account_ids"] == sorted([OTHER, GEN_BI])
    assert by["arn:kiro"]["eventTypeCode"] == "AWS_KIRO_PLANNED_LIFECYCLE_EVENT"


def test_each_missing_arn_is_kept_separately_no_cross_account_dedup_by_type():
    """Two same-type events with different ARNs (the shared one and an
    account's own) must both survive."""
    member = {"by_arn": {
        "arn:BILLING_1786511513": {"event": ev("arn:BILLING_1786511513", "AWS_BILLING_PLANNED_LIFECYCLE_EVENT"), "accounts": [OTHER]},
        "arn:BILLING_1786512164": {"event": ev("arn:BILLING_1786512164", "AWS_BILLING_PLANNED_LIFECYCLE_EVENT"), "accounts": [GEN_BI]},
    }, "failures": {}}
    extra = hma.events_missing_from_org(member, set())
    assert len(extra) == 2


def test_does_not_mutate_the_cached_event():
    original = ev("arn:x")
    member = {"by_arn": {"arn:x": {"event": original, "accounts": [GEN_BI]}}, "failures": {}}
    hma.events_missing_from_org(member, set())
    assert "_member_account_ids" not in original


def test_empty_member_result_adds_nothing():
    assert hma.events_missing_from_org({"by_arn": {}, "failures": {"x": "y"}}, {"a"}) == []
    assert hma.events_missing_from_org({}, set()) == []


# ── detail ───────────────────────────────────────────────────────────────────

def _prime(by_arn):
    hma._INDEX_CACHE.get_or_fetch(hma._INDEX_KEY, lambda: {"by_arn": by_arn, "failures": {}}, force_refresh=True)


def test_detail_returns_description_and_entities_in_the_standard_shape(monkeypatch):
    _prime({"arn:kiro": {"event": ev("arn:kiro"), "accounts": [GEN_BI]}})
    c = MagicMock()
    c.describe_event_details.return_value = {"successfulSet": [
        {"event": {"statusCode": "upcoming"}, "eventDescription": {"latestDescription": "Kiro EOS"}}]}
    c.describe_affected_entities.return_value = {"entities": [
        {"entityValue": "kiro-client", "awsAccountId": GEN_BI, "statusCode": "IMPAIRED", "lastUpdatedTime": "t"}]}
    monkeypatch.setattr(hma, "_health_client_for", lambda a: c)
    d = hma.get_member_event_detail("arn:kiro")
    assert d == {
        "event_arn": "arn:kiro", "event_description": "Kiro EOS", "event_status_code": "upcoming",
        "affected_accounts": [GEN_BI],
        "affected_entities": [{"entity_value": "kiro-client", "aws_account_id": GEN_BI,
                               "status_code": "IMPAIRED", "last_updated_time": "t"}],
    }
    assert c.describe_event_details.call_args.kwargs == {"eventArns": ["arn:kiro"]}


def test_detail_unknown_arn_returns_none_without_calling_aws(monkeypatch):
    _prime({})
    monkeypatch.setattr(hma, "_health_client_for", MagicMock(side_effect=AssertionError("no AWS call expected")))
    assert hma.get_member_event_detail("arn:nope") is None


def test_detail_survives_one_account_failing(monkeypatch):
    _prime({"arn:e": {"event": ev("arn:e"), "accounts": [GEN_BI, OTHER]}})
    good = MagicMock()
    good.describe_event_details.return_value = {"successfulSet": [{"eventDescription": {"latestDescription": "d"}, "event": {}}]}
    good.describe_affected_entities.return_value = {"entities": [{"entityValue": "r1"}]}

    def client_for(a):
        if a == GEN_BI:
            raise client_error("AccessDenied")
        return good

    monkeypatch.setattr(hma, "_health_client_for", client_for)
    d = hma.get_member_event_detail("arn:e")
    assert d["affected_accounts"] == [GEN_BI, OTHER]
    assert [e["entity_value"] for e in d["affected_entities"]] == ["r1"]
    assert d["affected_entities"][0]["aws_account_id"] == OTHER  # falls back to the account queried


def test_detail_rebuilds_the_index_when_it_has_expired(monkeypatch):
    """A detail click served by a different ECS task (empty index) must
    still work, by re-collecting."""
    fake_accounts(monkeypatch, [GEN_BI])
    c = health_client([{"events": [ev("arn:late")]}])
    c.describe_event_details.return_value = {"successfulSet": []}
    c.describe_affected_entities.return_value = {"entities": []}
    monkeypatch.setattr(hma, "_health_client_for", lambda a: c)
    assert hma.accounts_for_event("arn:late") == [GEN_BI]


# ── main.py integration ──────────────────────────────────────────────────────

@pytest.fixture
def main_prod(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "ENV", "production")
    return main


def test_dashboard_fetch_adds_member_only_events_after_the_org_events(main_prod):
    from app import escalation
    org = [ev("arn:org-1"), ev("arn:org-2")]
    member = {"by_arn": {
        "arn:org-1": {"event": ev("arn:org-1"), "accounts": [GEN_BI]},
        "arn:gen-bi-only": {"event": ev("arn:gen-bi-only", "AWS_KIRO_PLANNED_LIFECYCLE_EVENT"), "accounts": [GEN_BI]},
    }, "failures": {}}
    with patch.object(escalation, "list_ams_organization_health_events", return_value=org), \
         patch.object(hma, "collect_member_events", return_value=member):
        out = main_prod._fetch_raw_health_events()
    assert [e["arn"] for e in out] == ["arn:org-1", "arn:org-2", "arn:gen-bi-only"]
    assert out[-1]["_member_account_ids"] == [GEN_BI]


def test_dashboard_still_works_when_the_member_fan_out_fails(main_prod):
    from app import escalation
    org = [ev("arn:org-1")]
    with patch.object(escalation, "list_ams_organization_health_events", return_value=org), \
         patch.object(hma, "collect_member_events", side_effect=RuntimeError("boom")):
        out = main_prod._fetch_raw_health_events()
    assert out == org


def test_org_failure_still_returns_502_even_if_member_side_is_fine(main_prod):
    from fastapi import HTTPException
    from app import escalation
    with patch.object(escalation, "list_ams_organization_health_events",
                      side_effect=escalation.EscalationError("aws_ams", "org down")), \
         patch.object(hma, "collect_member_events", return_value={"by_arn": {}, "failures": {}}):
        with pytest.raises(HTTPException) as exc:
            main_prod._fetch_raw_health_events()
    assert exc.value.status_code == 502 and exc.value.detail == "org down"


def test_normalised_event_exposes_member_accounts_only_for_added_events(main_prod):
    plain = main_prod._norm_health_event(ev("arn:a"), {})
    extra = main_prod._norm_health_event({**ev("arn:b"), "_member_account_ids": [GEN_BI]}, {})
    assert plain["member_account_ids"] is None
    assert extra["member_account_ids"] == [GEN_BI]


def test_detail_prefers_org_data_and_skips_member_lookup(main_prod):
    from app import escalation
    org_detail = {"event_arn": "arn:o", "event_description": "d", "affected_accounts": ["1"], "affected_entities": []}
    with patch.object(escalation, "get_ams_organization_health_event_detail", return_value=org_detail), \
         patch.object(hma, "get_member_event_detail", side_effect=AssertionError("must not be called")):
        assert main_prod._org_or_member_event_detail("arn:o") == org_detail


def test_detail_falls_back_to_member_accounts_when_org_knows_nothing(main_prod):
    from app import escalation
    empty = {"event_arn": "arn:k", "event_description": None, "event_status_code": None,
             "affected_accounts": [], "affected_entities": []}
    member_detail = {"event_arn": "arn:k", "event_description": "Kiro EOS", "affected_accounts": [GEN_BI], "affected_entities": []}
    with patch.object(escalation, "get_ams_organization_health_event_detail", return_value=empty), \
         patch.object(hma, "get_member_event_detail", return_value=member_detail):
        assert main_prod._org_or_member_event_detail("arn:k") == member_detail


def test_detail_returns_the_empty_org_result_if_member_lookup_also_fails(main_prod):
    from app import escalation
    empty = {"event_arn": "arn:k", "event_description": None, "affected_accounts": [], "affected_entities": []}
    with patch.object(escalation, "get_ams_organization_health_event_detail", return_value=empty), \
         patch.object(hma, "get_member_event_detail", side_effect=RuntimeError("boom")):
        assert main_prod._org_or_member_event_detail("arn:k") == empty
