"""
Tests for app/ri_sp_savings.py — per-account, per-service Savings Plan
realized savings, derived from GetCostAndUsage filtered to
RECORD_TYPE=SavingsPlanCoveredUsage (UnblendedCost - AmortizedCost),
matching the Billing Console's Bills > Savings breakdown.

Each account is queried independently (via _client_for_account, one
GetCostAndUsage call per account, in parallel) — NOT as a single shared
org-wide call grouped by LINKED_ACCOUNT. That single-call design was
tried first and found to be wrong (a real user manually found Savings
Plan coverage on an account, 100000000009, that the single-call design
was silently missing because Core account is not the org's payer/management
account and its own GetCostAndUsage call never sees other accounts'
usage). See the module docstring for the full explanation.

None of these tests make real AWS/network calls: `_client_for_account` is
monkeypatched with a fully controlled fake Cost Explorer client, the same
approach test_billing_by_service.py uses.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import ri_sp_savings as rss_module
from app import settings_store
from app.database import Base


@pytest.fixture(autouse=True)
def isolated_settings_db(monkeypatch):
    """Every test in this module may trigger _attach_reseller_comparisons()
    (called on every get_ri_sp_savings_report() path — mock, cached, and
    freshly-fetched), which reads settings_store.get_setting() for the
    reseller-cost key. Route settings_store through a fresh in-memory
    SQLite DB per test (same isolation pattern as
    test_cost_adjustment_store.py) so these tests never touch the real
    dev job_portal.db file and never leak state between tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(settings_store, "SessionLocal", TestSessionLocal)
    yield TestSessionLocal
    engine.dispose()


def _group(service, unblended, amortized):
    return {
        "Keys": [service],
        "Metrics": {
            "UnblendedCost": {"Amount": str(unblended), "Unit": "USD"},
            "AmortizedCost": {"Amount": str(amortized), "Unit": "USD"},
        },
    }


def _bucket(start, groups):
    return {"TimePeriod": {"Start": start}, "Groups": groups}


class FakeCeClient:
    """One fake client represents ONE account's Cost Explorer identity —
    mirrors _client_for_account() returning a distinct client per account
    (Core account direct, others via AssumeRole)."""

    def __init__(self, pages_by_month: dict[str, list[list[dict]]]):
        self.pages_by_month = pages_by_month
        self.calls: list[dict] = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        start = kwargs["TimePeriod"]["Start"]
        month = start[:7]
        pages = self.pages_by_month.get(month, [[]])
        token = kwargs.get("NextPageToken")
        page_index = 0 if token is None else int(token)
        page_index = min(page_index, len(pages) - 1) if pages else 0
        results = pages[page_index] if pages else []
        resp = {"ResultsByTime": results}
        if page_index + 1 < len(pages):
            resp["NextPageToken"] = str(page_index + 1)
        return resp


class FakeCeClientRegistry:
    """Multi-account fake: maps account_id -> FakeCeClient, and records
    which (account_id, service) pairs _client_for_account was called
    with, so tests can assert every account was queried independently."""

    def __init__(self, clients_by_account: dict[str, FakeCeClient]):
        self.clients_by_account = clients_by_account
        self.requested: list[tuple[str, str]] = []

    def client_for_account(self, account_id, service, core_session):
        self.requested.append((account_id, service))
        return self.clients_by_account.get(account_id, FakeCeClient({}))


# ── list_accounts ───────────────────────────────────────────────────────

def test_list_accounts_returns_id_and_alias_pairs():
    accounts = rss_module.list_accounts()
    assert len(accounts) == len(rss_module.ACCOUNT_ALIAS)
    first = accounts[0]
    assert set(first.keys()) == {"account_id", "alias"}
    assert first["account_id"] == "100000000001"
    assert first["alias"] == "core"


# ── _fetch_account_month_sp_savings: RECORD_TYPE filter + SERVICE grouping ──

def test_fetch_account_month_sp_savings_computes_net_savings_per_service(monkeypatch):
    fake = FakeCeClient({
        "2026-07": [[_bucket("2026-07-01", [
            _group("Amazon Elastic Compute Cloud - Compute", "20890.52", "15045.98"),
            _group("AWS Lambda", "48.59", "43.65"),
        ])]],
    })
    monkeypatch.setattr(rss_module, "_client_for_account", lambda *a, **k: fake)
    services = rss_module._fetch_account_month_sp_savings("100000000001", "2026-07", object())

    assert services["Amazon Elastic Compute Cloud - Compute"]["on_demand_equivalent"] == 20890.52
    assert services["Amazon Elastic Compute Cloud - Compute"]["amortized_cost"] == 15045.98
    assert services["Amazon Elastic Compute Cloud - Compute"]["net_savings"] == 5844.54
    assert services["AWS Lambda"]["net_savings"] == 4.94


def test_fetch_account_month_sp_savings_uses_record_type_filter_and_service_grouping(monkeypatch):
    fake = FakeCeClient({"2026-07": [[_bucket("2026-07-01", [])]]})
    monkeypatch.setattr(rss_module, "_client_for_account", lambda *a, **k: fake)
    rss_module._fetch_account_month_sp_savings("100000000001", "2026-07", object())

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["Filter"] == {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["SavingsPlanCoveredUsage"]}}
    group_keys = [g["Key"] for g in call["GroupBy"]]
    assert group_keys == ["SERVICE"]
    assert set(call["Metrics"]) == {"UnblendedCost", "AmortizedCost"}


def test_fetch_account_month_sp_savings_skips_near_zero_noise_rows(monkeypatch):
    fake = FakeCeClient({
        "2026-07": [[_bucket("2026-07-01", [
            _group("AWS Key Management Service", "0.001", "0.001"),
        ])]],
    })
    monkeypatch.setattr(rss_module, "_client_for_account", lambda *a, **k: fake)
    services = rss_module._fetch_account_month_sp_savings("100000000001", "2026-07", object())
    assert services == {}


def test_fetch_account_month_sp_savings_paginates(monkeypatch):
    fake = FakeCeClient({
        "2026-07": [
            [_bucket("2026-07-01", [_group("AWS Lambda", "10.00", "8.00")])],
            [_bucket("2026-07-01", [_group("Amazon Elastic Compute Cloud - Compute", "50.00", "40.00")])],
        ],
    })
    monkeypatch.setattr(rss_module, "_client_for_account", lambda *a, **k: fake)
    services = rss_module._fetch_account_month_sp_savings("100000000001", "2026-07", object())
    assert services["AWS Lambda"]["net_savings"] == 2.00
    assert services["Amazon Elastic Compute Cloud - Compute"]["net_savings"] == 10.00


def test_fetch_account_month_sp_savings_scopes_client_to_the_requested_account(monkeypatch):
    captured = {}

    def _capture(account_id, service, core_session):
        captured["account_id"] = account_id
        captured["service"] = service
        return FakeCeClient({"2026-07": [[_bucket("2026-07-01", [])]]})

    monkeypatch.setattr(rss_module, "_client_for_account", _capture)
    rss_module._fetch_account_month_sp_savings("100000000009", "2026-07", object())
    assert captured["account_id"] == "100000000009"
    assert captured["service"] == "ce"


# ── _build_row ────────────────────────────────────────────────────────────

def test_build_row_sums_services_and_sorts_by_net_savings_descending():
    services = {
        "AWS Lambda": {"on_demand_equivalent": 48.59, "amortized_cost": 43.65, "net_savings": 4.94},
        "Amazon Elastic Compute Cloud - Compute": {"on_demand_equivalent": 20890.52, "amortized_cost": 15045.98, "net_savings": 5844.54},
    }
    row = rss_module._build_row("100000000001", services)
    assert row["account_id"] == "100000000001"
    assert row["alias"] == "core"
    assert row["error"] is None
    assert [s["service"] for s in row["services"]] == ["Amazon Elastic Compute Cloud - Compute", "AWS Lambda"]
    assert row["sp_on_demand_equivalent"] == 20939.11
    assert row["sp_amortized_cost"] == 15089.63
    assert row["sp_net_savings"] == 5849.48
    assert row["total_net_savings"] == 5849.48


def test_build_row_empty_services_yields_zeroed_row_with_no_service_rows():
    row = rss_module._build_row("111111111111", {})
    assert row["services"] == []
    assert row["sp_on_demand_equivalent"] == 0.0
    assert row["sp_net_savings"] == 0.0
    assert row["total_net_savings"] == 0.0


# ── _fetch_month: per-account fan-out (independent calls) + totals ────────

def test_fetch_month_queries_every_requested_account_independently(monkeypatch):
    registry = FakeCeClientRegistry({
        "100000000001": FakeCeClient({"2026-07": [[_bucket("2026-07-01", [_group("AWS Lambda", "48.59", "43.65")])]]}),
        "100000000009": FakeCeClient({"2026-07": [[_bucket("2026-07-01", [_group("Amazon Elastic Compute Cloud - Compute", "2792.22", "2029.34")])]]}),
        "100000000002": FakeCeClient({"2026-07": [[_bucket("2026-07-01", [])]]}),
    })
    monkeypatch.setattr(rss_module, "_client_for_account", registry.client_for_account)
    account_ids = ["100000000001", "100000000009", "100000000002"]
    result = rss_module._fetch_month(account_ids, "2026-07", object())

    # Every account was queried through its own _client_for_account call —
    # this is the behavior that catches accounts like 100000000009 that a
    # single shared org-wide call would silently miss.
    assert sorted(a for a, s in registry.requested) == sorted(account_ids)
    assert all(s == "ce" for a, s in registry.requested)

    core_row = next(r for r in result["accounts"] if r["account_id"] == "100000000001")
    ibe_row = next(r for r in result["accounts"] if r["account_id"] == "100000000009")
    zero_row = next(r for r in result["accounts"] if r["account_id"] == "100000000002")
    assert core_row["sp_net_savings"] == 4.94
    assert ibe_row["sp_net_savings"] == 762.88
    assert zero_row["sp_net_savings"] == 0.0
    assert zero_row["services"] == []


def test_fetch_month_preserves_requested_account_order_in_output(monkeypatch):
    registry = FakeCeClientRegistry({
        "100000000003": FakeCeClient({"2026-07": [[_bucket("2026-07-01", [])]]}),
        "100000000001": FakeCeClient({"2026-07": [[_bucket("2026-07-01", [_group("AWS Lambda", "48.59", "43.65")])]]}),
        "100000000002": FakeCeClient({"2026-07": [[_bucket("2026-07-01", [])]]}),
    })
    monkeypatch.setattr(rss_module, "_client_for_account", registry.client_for_account)
    account_ids = ["100000000003", "100000000001", "100000000002"]
    result = rss_module._fetch_month(account_ids, "2026-07", object())
    assert [r["account_id"] for r in result["accounts"]] == account_ids


def test_fetch_month_aggregates_totals_across_accounts(monkeypatch):
    registry = FakeCeClientRegistry({
        "100000000001": FakeCeClient({"2026-07": [[_bucket("2026-07-01", [_group("AWS Lambda", "48.59", "43.65")])]]}),
        "100000000003": FakeCeClient({"2026-07": [[_bucket("2026-07-01", [_group("Amazon Elastic Compute Cloud - Compute", "100.00", "80.00")])]]}),
    })
    monkeypatch.setattr(rss_module, "_client_for_account", registry.client_for_account)
    result = rss_module._fetch_month(["100000000001", "100000000003"], "2026-07", object())
    assert result["totals"]["sp_net_savings"] == 24.94
    assert result["totals"]["total_net_savings"] == 24.94
    assert result["totals"]["error_count"] == 0


def test_fetch_month_isolates_one_account_failure_from_the_rest(monkeypatch):
    """One account's AssumeRole/API failure must not blank out every other
    account's real numbers — the design flaw this per-account rewrite
    specifically fixes vs. the old single-shared-call approach."""
    good_client = FakeCeClient({"2026-07": [[_bucket("2026-07-01", [_group("AWS Lambda", "48.59", "43.65")])]]})

    def _client_for_account(account_id, service, core_session):
        if account_id == "100000000019":
            raise RuntimeError("AccessDenied: simulated secret failure detail")
        return good_client

    monkeypatch.setattr(rss_module, "_client_for_account", _client_for_account)
    result = rss_module._fetch_month(["100000000001", "100000000019"], "2026-07", object())

    good_row = next(r for r in result["accounts"] if r["account_id"] == "100000000001")
    bad_row = next(r for r in result["accounts"] if r["account_id"] == "100000000019")
    assert good_row["error"] is None
    assert good_row["sp_net_savings"] == 4.94
    assert bad_row["error"] == "Could not fetch Savings Plan savings data for this account"
    assert "simulated" not in bad_row["error"]
    assert bad_row["sp_net_savings"] == 0.0
    assert result["totals"]["error_count"] == 1
    # Total only reflects the account that actually succeeded.
    assert result["totals"]["sp_net_savings"] == 4.94


# ── get_ri_sp_savings_report: validation ───────────────────────────────────

def test_get_ri_sp_savings_report_requires_account_ids():
    with pytest.raises(rss_module.CostAnalysisError):
        rss_module.get_ri_sp_savings_report([], ["2026-07"])


def test_get_ri_sp_savings_report_requires_months():
    with pytest.raises(rss_module.CostAnalysisError):
        rss_module.get_ri_sp_savings_report(["100000000001"], [])


# ── get_ri_sp_savings_report: local mock path ──────────────────────────────

def test_get_ri_sp_savings_report_local_mock_shape(monkeypatch):
    monkeypatch.setattr(rss_module, "ENV", "local")
    report = rss_module.get_ri_sp_savings_report(["100000000001", "100000000003"], ["2026-01", "2026-02"])
    assert report["is_mock"] is True
    assert len(report["months"]) == 2
    for month_slice in report["months"]:
        assert set(month_slice.keys()) == {"month", "month_label", "accounts", "totals"}
        assert len(month_slice["accounts"]) == 2
        for account in month_slice["accounts"]:
            assert account["error"] is None
            assert isinstance(account["services"], list)
            for svc in account["services"]:
                assert set(svc.keys()) == {"service", "on_demand_equivalent", "amortized_cost", "net_savings"}


def test_get_ri_sp_savings_report_local_mock_is_deterministic(monkeypatch):
    monkeypatch.setattr(rss_module, "ENV", "local")
    report1 = rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-01"])
    report2 = rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-01"])
    assert report1["months"][0]["totals"] == report2["months"][0]["totals"]


# ── Caching (non-mock path) — refreshed once/day at 08:00 HKT, not a TTL ───
#
# _current_refresh_epoch() is monkeypatched to a fixed string in each of
# these tests so cache-hit/miss behavior is deterministic and can never
# flake by happening to straddle a real 08:00 HKT boundary while the test
# runs.

def test_get_ri_sp_savings_report_caches_within_the_same_refresh_epoch(monkeypatch):
    monkeypatch.setattr(rss_module, "ENV", "production")
    rss_module.reset_cache_for_tests()
    monkeypatch.setattr(rss_module, "_core_session", lambda: object())
    monkeypatch.setattr(rss_module, "_current_refresh_epoch", lambda: "2026-07-15T08:00:00+08:00")

    fake = FakeCeClient({"2026-07": [[_bucket("2026-07-01", [_group("AWS Lambda", "10.00", "8.00")])]]})
    monkeypatch.setattr(rss_module, "_client_for_account", lambda *a, **k: fake)

    first = rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-07"])
    calls_after_first = len(fake.calls)
    second = rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-07"])
    assert len(fake.calls) == calls_after_first  # same epoch, served from cache
    assert second["months"][0]["totals"]["sp_net_savings"] == first["months"][0]["totals"]["sp_net_savings"]
    rss_module.reset_cache_for_tests()


def test_get_ri_sp_savings_report_refetches_once_the_refresh_epoch_advances(monkeypatch):
    """Crossing the next 08:00 HKT boundary must trigger exactly one fresh
    fetch, even though nothing else about the request changed."""
    monkeypatch.setattr(rss_module, "ENV", "production")
    rss_module.reset_cache_for_tests()
    monkeypatch.setattr(rss_module, "_core_session", lambda: object())

    fake = FakeCeClient({"2026-07": [[_bucket("2026-07-01", [_group("AWS Lambda", "10.00", "8.00")])]]})
    monkeypatch.setattr(rss_module, "_client_for_account", lambda *a, **k: fake)

    monkeypatch.setattr(rss_module, "_current_refresh_epoch", lambda: "2026-07-15T08:00:00+08:00")
    rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-07"])
    calls_after_first = len(fake.calls)

    monkeypatch.setattr(rss_module, "_current_refresh_epoch", lambda: "2026-07-16T08:00:00+08:00")
    rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-07"])
    assert len(fake.calls) > calls_after_first
    rss_module.reset_cache_for_tests()


def test_get_ri_sp_savings_report_force_refresh_bypasses_cache(monkeypatch):
    monkeypatch.setattr(rss_module, "ENV", "production")
    rss_module.reset_cache_for_tests()
    monkeypatch.setattr(rss_module, "_core_session", lambda: object())
    monkeypatch.setattr(rss_module, "_current_refresh_epoch", lambda: "2026-07-15T08:00:00+08:00")

    fake = FakeCeClient({"2026-07": [[_bucket("2026-07-01", [_group("AWS Lambda", "10.00", "8.00")])]]})
    monkeypatch.setattr(rss_module, "_client_for_account", lambda *a, **k: fake)

    rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-07"])
    calls_after_first = len(fake.calls)
    rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-07"], force_refresh=True)
    assert len(fake.calls) > calls_after_first
    rss_module.reset_cache_for_tests()


def test_get_ri_sp_savings_report_different_month_sets_cached_separately(monkeypatch):
    monkeypatch.setattr(rss_module, "ENV", "production")
    rss_module.reset_cache_for_tests()
    monkeypatch.setattr(rss_module, "_core_session", lambda: object())
    monkeypatch.setattr(rss_module, "_current_refresh_epoch", lambda: "2026-07-15T08:00:00+08:00")

    fake = FakeCeClient({
        "2026-07": [[_bucket("2026-07-01", [_group("AWS Lambda", "10.00", "8.00")])]],
        "2026-08": [[_bucket("2026-08-01", [_group("AWS Lambda", "20.00", "16.00")])]],
    })
    monkeypatch.setattr(rss_module, "_client_for_account", lambda *a, **k: fake)

    report_jul = rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-07"])
    report_aug = rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-08"])
    assert report_jul["months"][0]["totals"]["sp_net_savings"] == 2.00
    assert report_aug["months"][0]["totals"]["sp_net_savings"] == 4.00
    rss_module.reset_cache_for_tests()


def test_get_ri_sp_savings_report_cache_hit_still_reports_cache_age(monkeypatch):
    monkeypatch.setattr(rss_module, "ENV", "production")
    rss_module.reset_cache_for_tests()
    monkeypatch.setattr(rss_module, "_core_session", lambda: object())
    monkeypatch.setattr(rss_module, "_current_refresh_epoch", lambda: "2026-07-15T08:00:00+08:00")

    fake = FakeCeClient({"2026-07": [[_bucket("2026-07-01", [_group("AWS Lambda", "10.00", "8.00")])]]})
    monkeypatch.setattr(rss_module, "_client_for_account", lambda *a, **k: fake)

    rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-07"])
    cached = rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-07"])
    assert cached["is_mock"] is False
    assert cached["cache_age_seconds"] >= 0
    rss_module.reset_cache_for_tests()


# ── _current_refresh_epoch: 08:00 HKT boundary logic ───────────────────────
#
# Note: _current_refresh_epoch() computes now_hkt as
# datetime.now(timezone.utc) + timedelta(hours=8) — this shifts the
# wall-clock value to HKT but the resulting datetime's tzinfo is still
# `timezone.utc` (adding a timedelta never changes tzinfo), so
# .isoformat() renders a "+00:00" suffix even though the wall-clock digits
# represent HKT. That's fine for this function's actual purpose (a cache
# key that must change exactly once every 24h at the right moment), but
# these tests assert the real rendered string rather than an assumed
# "+08:00" suffix.

def test_current_refresh_epoch_before_0800_hkt_uses_previous_days_boundary(monkeypatch):
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 2026-07-14 23:00:00 UTC -> 2026-07-15 07:00:00 HKT wall-clock,
            # which is before that day's 08:00 HKT boundary.
            return datetime(2026, 7, 14, 23, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(rss_module, "datetime", _FixedDatetime)
    epoch = rss_module._current_refresh_epoch()
    assert epoch == "2026-07-14T08:00:00+00:00"


def test_current_refresh_epoch_after_0800_hkt_uses_todays_boundary(monkeypatch):
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 2026-07-15 01:00:00 UTC -> 2026-07-15 09:00:00 HKT wall-clock,
            # which is after that day's 08:00 boundary.
            return datetime(2026, 7, 15, 1, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(rss_module, "datetime", _FixedDatetime)
    epoch = rss_module._current_refresh_epoch()
    assert epoch == "2026-07-15T08:00:00+00:00"


def test_current_refresh_epoch_exactly_at_0800_hkt_uses_todays_boundary(monkeypatch):
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 2026-07-15 00:00:00 UTC -> exactly 2026-07-15 08:00:00 HKT
            # wall-clock.
            return datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(rss_module, "datetime", _FixedDatetime)
    epoch = rss_module._current_refresh_epoch()
    assert epoch == "2026-07-15T08:00:00+00:00"


def test_current_refresh_epoch_stable_within_the_same_day(monkeypatch):
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(rss_module, "datetime", _FixedDatetime)
    assert rss_module._current_refresh_epoch() == rss_module._current_refresh_epoch()


# ── Reseller On-Demand Cost comparison (manual admin input, PER ACCOUNT) ───

def test_get_reseller_ondemand_cost_never_set_returns_none_amount():
    entry = rss_module.get_reseller_ondemand_cost("100000000001", "2026-07")
    assert entry == {
        "account_id": "100000000001",
        "month": "2026-07",
        "amount": None,
        "currency": "USD",
        "updated_by_name": None,
        "updated_at": None,
    }


def test_set_and_get_reseller_ondemand_cost_round_trips():
    rss_module.set_reseller_ondemand_cost(
        "100000000001", "2026-07", 18000.00, currency="usd", actor_email="admin@example.com", actor_name="Admin User",
    )
    entry = rss_module.get_reseller_ondemand_cost("100000000001", "2026-07")
    assert entry["account_id"] == "100000000001"
    assert entry["month"] == "2026-07"
    assert entry["amount"] == 18000.00
    assert entry["currency"] == "USD"  # normalized to uppercase
    assert entry["updated_by_name"] == "Admin User"
    assert entry["updated_at"] is not None


def test_set_reseller_ondemand_cost_rejects_negative_amount():
    with pytest.raises(ValueError):
        rss_module.set_reseller_ondemand_cost(
            "100000000001", "2026-07", -5.0, actor_email="admin@example.com", actor_name="Admin",
        )
    # Rejected write must not have persisted anything.
    assert rss_module.get_reseller_ondemand_cost("100000000001", "2026-07")["amount"] is None


def test_set_reseller_ondemand_cost_allows_zero():
    rss_module.set_reseller_ondemand_cost(
        "100000000001", "2026-07", 0.0, actor_email="admin@example.com", actor_name="Admin",
    )
    entry = rss_module.get_reseller_ondemand_cost("100000000001", "2026-07")
    assert entry["amount"] == 0.0


def test_set_reseller_ondemand_cost_rounds_to_2_decimal_places():
    rss_module.set_reseller_ondemand_cost(
        "100000000001", "2026-07", 18000.999, actor_email="admin@example.com", actor_name="Admin",
    )
    assert rss_module.get_reseller_ondemand_cost("100000000001", "2026-07")["amount"] == 18001.0


def test_delete_reseller_ondemand_cost_clears_the_value():
    rss_module.set_reseller_ondemand_cost(
        "100000000001", "2026-07", 100.0, actor_email="admin@example.com", actor_name="Admin",
    )
    assert rss_module.get_reseller_ondemand_cost("100000000001", "2026-07")["amount"] == 100.0
    rss_module.delete_reseller_ondemand_cost("100000000001", "2026-07", actor_email="admin@example.com", actor_name="Admin")
    assert rss_module.get_reseller_ondemand_cost("100000000001", "2026-07")["amount"] is None


def test_reseller_ondemand_cost_is_scoped_per_month():
    rss_module.set_reseller_ondemand_cost(
        "100000000001", "2026-07", 100.0, actor_email="a@example.com", actor_name="A",
    )
    rss_module.set_reseller_ondemand_cost(
        "100000000001", "2026-08", 200.0, actor_email="a@example.com", actor_name="A",
    )
    assert rss_module.get_reseller_ondemand_cost("100000000001", "2026-07")["amount"] == 100.0
    assert rss_module.get_reseller_ondemand_cost("100000000001", "2026-08")["amount"] == 200.0


def test_reseller_ondemand_cost_is_scoped_per_account():
    """Different accounts must never see or overwrite each other's
    reseller cost entries for the same month — this is the whole point
    of keying by account_id."""
    rss_module.set_reseller_ondemand_cost(
        "100000000001", "2026-07", 100.0, actor_email="a@example.com", actor_name="A",
    )
    rss_module.set_reseller_ondemand_cost(
        "100000000009", "2026-07", 999.0, actor_email="a@example.com", actor_name="A",
    )
    assert rss_module.get_reseller_ondemand_cost("100000000001", "2026-07")["amount"] == 100.0
    assert rss_module.get_reseller_ondemand_cost("100000000009", "2026-07")["amount"] == 999.0
    # An account that was never touched must still read as "not entered".
    assert rss_module.get_reseller_ondemand_cost("100000000003", "2026-07")["amount"] is None


# ── _build_reseller_comparison ──────────────────────────────────────────────

def test_build_reseller_comparison_returns_none_when_amount_not_entered():
    assert rss_module._build_reseller_comparison(20000.0, {"amount": None}) is None


def test_build_reseller_comparison_computes_diff_and_discount_pct():
    # Reseller billed less than AWS's own on-demand equivalent -> positive
    # discount_pct (a real discount), negative diff.
    comparison = rss_module._build_reseller_comparison(
        20000.0, {"amount": 18000.0, "currency": "USD", "updated_by_name": "Admin", "updated_at": "2026-07-15T00:00:00Z"},
    )
    assert comparison["reseller_ondemand_cost"] == 18000.0
    assert comparison["aws_ondemand_equivalent"] == 20000.0
    assert comparison["diff"] == -2000.0
    assert comparison["discount_pct"] == 10.0
    assert comparison["currency"] == "USD"
    assert comparison["updated_by_name"] == "Admin"


def test_build_reseller_comparison_reseller_billed_more_gives_negative_discount():
    comparison = rss_module._build_reseller_comparison(20000.0, {"amount": 22000.0})
    assert comparison["diff"] == 2000.0
    assert comparison["discount_pct"] == -10.0


def test_build_reseller_comparison_zero_aws_cost_gives_none_discount_pct():
    comparison = rss_module._build_reseller_comparison(0.0, {"amount": 500.0})
    assert comparison["aws_ondemand_equivalent"] == 0.0
    assert comparison["reseller_ondemand_cost"] == 500.0
    assert comparison["diff"] == 500.0
    assert comparison["discount_pct"] is None


# ── _attach_reseller_comparisons: wired into every report path, per account ─

def test_local_mock_report_attaches_reseller_comparison_to_the_right_account_only(monkeypatch):
    monkeypatch.setattr(rss_module, "ENV", "local")
    rss_module.set_reseller_ondemand_cost(
        "100000000001", "2026-01", 500.0, actor_email="admin@example.com", actor_name="Admin",
    )
    report = rss_module.get_ri_sp_savings_report(["100000000001", "100000000003"], ["2026-01"])
    accounts = {a["account_id"]: a for a in report["months"][0]["accounts"]}

    core_comparison = accounts["100000000001"]["reseller_comparison"]
    assert core_comparison is not None
    assert core_comparison["reseller_ondemand_cost"] == 500.0
    assert core_comparison["aws_ondemand_equivalent"] == accounts["100000000001"]["sp_on_demand_equivalent"]
    # The other requested account has nothing entered -> None, not
    # accidentally inheriting the first account's value.
    assert accounts["100000000003"]["reseller_comparison"] is None


def test_local_mock_report_totals_reseller_comparison_none_when_no_account_has_one(monkeypatch):
    monkeypatch.setattr(rss_module, "ENV", "local")
    report = rss_module.get_ri_sp_savings_report(["100000000001"], ["2026-02"])
    assert report["months"][0]["totals"]["reseller_comparison"] is None
    assert report["months"][0]["accounts"][0]["reseller_comparison"] is None


def test_local_mock_report_totals_reseller_comparison_aggregates_only_entered_accounts(monkeypatch):
    monkeypatch.setattr(rss_module, "ENV", "local")
    rss_module.set_reseller_ondemand_cost(
        "100000000001", "2026-03", 100.0, actor_email="a@example.com", actor_name="A",
    )
    rss_module.set_reseller_ondemand_cost(
        "100000000003", "2026-03", 50.0, actor_email="a@example.com", actor_name="A",
    )
    report = rss_module.get_ri_sp_savings_report(["100000000001", "100000000003", "100000000002"], ["2026-03"])
    totals = report["months"][0]["totals"]
    assert totals["reseller_comparison"] is not None
    assert totals["reseller_comparison"]["accounts_with_entry"] == 2
    assert totals["reseller_comparison"]["reseller_ondemand_cost"] == 150.0
    # The untouched third account contributes nothing to the aggregate but
    # still gets its own reseller_comparison: None on its row.
    third_row = next(a for a in report["months"][0]["accounts"] if a["account_id"] == "100000000002")
    assert third_row["reseller_comparison"] is None


def test_cached_report_reflects_reseller_cost_edit_immediately_for_the_right_account(monkeypatch):
    """The reseller comparison must be re-read fresh per account on every
    call, even when the underlying AWS Cost Explorer data is served from
    the daily cache — an admin's edit should never wait for the next
    08:00 HKT refresh to show up, and must never leak into another
    account's row."""
    monkeypatch.setattr(rss_module, "ENV", "production")
    rss_module.reset_cache_for_tests()
    monkeypatch.setattr(rss_module, "_core_session", lambda: object())
    monkeypatch.setattr(rss_module, "_current_refresh_epoch", lambda: "2026-07-15T08:00:00+08:00")

    fake = FakeCeClient({"2026-07": [[_bucket("2026-07-01", [_group("AWS Lambda", "100.00", "80.00")])]]})
    monkeypatch.setattr(rss_module, "_client_for_account", lambda *a, **k: fake)

    first = rss_module.get_ri_sp_savings_report(["100000000001", "100000000003"], ["2026-07"])
    accounts_first = {a["account_id"]: a for a in first["months"][0]["accounts"]}
    assert accounts_first["100000000001"]["reseller_comparison"] is None
    assert accounts_first["100000000003"]["reseller_comparison"] is None

    rss_module.set_reseller_ondemand_cost(
        "100000000001", "2026-07", 90.0, actor_email="admin@example.com", actor_name="Admin",
    )
    calls_before_second = len(fake.calls)
    second = rss_module.get_ri_sp_savings_report(["100000000001", "100000000003"], ["2026-07"])
    assert len(fake.calls) == calls_before_second  # AWS data still served from cache
    accounts_second = {a["account_id"]: a for a in second["months"][0]["accounts"]}
    assert accounts_second["100000000001"]["reseller_comparison"]["reseller_ondemand_cost"] == 90.0
    # Only the edited account changed — the other account row is still None.
    assert accounts_second["100000000003"]["reseller_comparison"] is None
    rss_module.reset_cache_for_tests()
