"""
Tests for app/cost_explorer.py — AWS Cost Explorer org-wide summary logic.

None of these tests make real AWS/network calls. `_core_session()` and
`_client_for_account()` are monkeypatched so `get_organization_cost_summary()`
never tries to reach Secrets Manager, STS, or Cost Explorer — instead each
"account" is served by a `FakeCostExplorerClient` we fully control, letting
us assert exact accounting behaviour (pagination, RECORD_TYPE splitting,
BILLING_ENTITY-based Marketplace classification, reconciliation, etc.)
without depending on moto's (nonexistent) Cost Explorer support.
"""

from __future__ import annotations

import threading
import time

import pytest

from app import cost_explorer as ce_module


def _metric(amount, unit="USD"):
    return {"UnblendedCost": {"Amount": str(amount), "Unit": unit}}


def _bucket(start, groups=None, total=None):
    b = {"TimePeriod": {"Start": start}, "Groups": []}
    if groups:
        for keys, amount in groups:
            b["Groups"].append({"Keys": list(keys), "Metrics": _metric(amount)})
    if total is not None:
        b["Total"] = _metric(total)
    return b


class FakeCostExplorerClient:
    """Minimal stand-in for a boto3 Cost Explorer client.

    `script` maps a query "kind" (one of "normal", "adjustment", "totals")
    to a list of *pages*, where each page is a list of ResultsByTime
    buckets (as returned by boto3). The kind is inferred from the
    GroupBy/Filter shape of each get_cost_and_usage call, mirroring how
    cost_explorer.py actually issues its three disjoint queries.
    """

    def __init__(self, script: dict[str, list[list[dict]]], raise_on_call: bool = False):
        self.script = script
        self.raise_on_call = raise_on_call
        self.calls: list[dict] = []

    @staticmethod
    def _kind_for(kwargs: dict) -> str:
        group_by = kwargs.get("GroupBy") or []
        group_keys = {g.get("Key") for g in group_by}
        if group_keys == {"SERVICE", "BILLING_ENTITY"}:
            return "normal"
        filt = kwargs.get("Filter") or {}
        if "Dimensions" in filt and filt["Dimensions"].get("Key") == "RECORD_TYPE":
            return "adjustment"
        return "totals"

    def get_cost_and_usage(self, **kwargs):
        if self.raise_on_call:
            raise RuntimeError("simulated Cost Explorer failure")
        self.calls.append(kwargs)
        kind = self._kind_for(kwargs)
        pages = self.script.get(kind, [[]])
        token = kwargs.get("NextPageToken")
        page_index = 0 if token is None else int(token)
        page_index = min(page_index, len(pages) - 1) if pages else 0
        results = pages[page_index] if pages else []
        resp = {"ResultsByTime": results}
        if page_index + 1 < len(pages):
            resp["NextPageToken"] = str(page_index + 1)
        return resp


@pytest.fixture
def patch_accounts(monkeypatch):
    """Reduce ACCOUNTS to a small, deterministic set for these tests, and
    stub out _core_session()/_client_for_account() so no real AWS
    session/STS/Cost Explorer call is ever attempted."""
    test_accounts = {"111111111111": "TestAccountOne", "222222222222": "TestAccountTwo"}
    monkeypatch.setattr(ce_module, "ACCOUNTS", test_accounts)
    monkeypatch.setattr(ce_module, "ACCOUNT_BU", {"111111111111": "BU-A", "222222222222": "BU-B"})
    monkeypatch.setattr(ce_module, "_core_session", lambda: object())

    def _client_for_account(account_id, service, core_session, _clients=[]):
        raise AssertionError("test must monkeypatch _client_for_account per-test")

    monkeypatch.setattr(ce_module, "_client_for_account", _client_for_account)
    return test_accounts


def _install_clients(monkeypatch, clients_by_account: dict[str, FakeCostExplorerClient]):
    def _client_for_account(account_id, service, core_session):
        assert service == "ce"
        return clients_by_account[account_id]

    monkeypatch.setattr(ce_module, "_client_for_account", _client_for_account)


def _bounds():
    return ce_module._month_bounds()


# ── Pagination ────────────────────────────────────────────────────────────

def test_paginated_retrieval_merges_pages_and_stops_on_repeated_token(monkeypatch):
    ce = FakeCostExplorerClient(script={
        "normal": [
            [_bucket("2024-01-01", groups=[(("Amazon EC2", "AWS"), "10.00")])],
            [_bucket("2024-01-01", groups=[(("Amazon S3", "AWS"), "5.00")])],
        ],
    })
    results = ce_module._get_cost_and_usage_paginated(ce, TimePeriod={}, Granularity="MONTHLY", Metrics=["UnblendedCost"], GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "BILLING_ENTITY"}])
    assert len(results) == 2
    merged = ce_module._merge_results_by_time(results)
    assert set(merged["2024-01-01"]["groups"].keys()) == {("Amazon EC2", "AWS"), ("Amazon S3", "AWS")}


def test_paginated_retrieval_stops_on_repeated_token(monkeypatch):
    class LoopingClient:
        def __init__(self):
            self.call_count = 0

        def get_cost_and_usage(self, **kwargs):
            self.call_count += 1
            # Always return the same NextPageToken — a buggy/looping client.
            return {"ResultsByTime": [_bucket("2024-01-01", groups=[(("Amazon EC2", "AWS"), "1.00")])], "NextPageToken": "same-token"}

    client = LoopingClient()
    results = ce_module._get_cost_and_usage_paginated(client, TimePeriod={}, Granularity="MONTHLY", Metrics=["UnblendedCost"])
    # Should stop well before an infinite loop — at most a couple of pages.
    assert client.call_count <= 3
    assert len(results) == client.call_count


# ── Accounting / classification ──────────────────────────────────────────

def test_fetch_account_cost_accounting_and_marketplace_classification(patch_accounts, monkeypatch):
    two_ago, prev, mtd, _tomorrow = _bounds()

    normal_page = [
        _bucket(mtd, groups=[
            (("Amazon EC2", "AWS"), "100.00"),
            (("Some Vendor Product", "AWS Marketplace"), "20.00"),  # BILLING_ENTITY authoritative
            (("AWS Shield", "AWS"), "3.00"),
            (("AWS Enterprise Support", "AWS"), "5.00"),
        ]),
        _bucket(prev, groups=[(("Amazon EC2", "AWS"), "80.00")]),
        _bucket(two_ago, groups=[(("Amazon EC2", "AWS"), "70.00")]),
    ]
    adjustment_page = [
        _bucket(mtd, total="-10.00"),
        _bucket(prev, total="-8.00"),
        _bucket(two_ago, total="-7.00"),
    ]
    # Authoritative total matches classified sum exactly here (no residual).
    totals_page = [
        _bucket(mtd, total="118.00"),   # 100+20+3+5-10
        _bucket(prev, total="72.00"),   # 80-8
        _bucket(two_ago, total="63.00"),  # 70-7
    ]

    fake = FakeCostExplorerClient(script={"normal": [normal_page], "adjustment": [adjustment_page], "totals": [totals_page]})
    _install_clients(monkeypatch, {"111111111111": fake, "222222222222": fake})

    row = ce_module._fetch_account_cost("111111111111", "TestAccountOne", object())

    assert row["error"] is None
    assert row["mtd"]["usage"] == 100.00
    assert row["mtd"]["marketplace"] == 20.00
    assert row["mtd"]["shield"] == 3.00
    assert row["mtd"]["support"] == 5.00
    assert row["mtd"]["credits"] == -10.00
    assert row["mtd"]["subtotal"] == 118.00
    assert row["last_month"]["subtotal"] == 72.00
    assert row["two_months_ago"]["subtotal"] == 63.00
    # Full per-service maps kept for org aggregation, stripped later.
    assert "Some Vendor Product" in row["_full_services_mtd"]
    assert row["_full_services_mtd"]["Some Vendor Product"] == 20.00


def test_reconciliation_residual_folds_into_usage_not_credits(patch_accounts, monkeypatch):
    two_ago, prev, mtd, _tomorrow = _bounds()
    normal_page = [_bucket(mtd, groups=[(("Amazon EC2", "AWS"), "100.00")]), _bucket(prev, groups=[]), _bucket(two_ago, groups=[])]
    adjustment_page = [_bucket(mtd, total="-5.00"), _bucket(prev, total="0.00"), _bucket(two_ago, total="0.00")]
    # Authoritative total is 97.00 but classified sum is 95.00 -> residual +2.00 must land in usage.
    totals_page = [_bucket(mtd, total="97.00"), _bucket(prev, total="0.00"), _bucket(two_ago, total="0.00")]

    fake = FakeCostExplorerClient(script={"normal": [normal_page], "adjustment": [adjustment_page], "totals": [totals_page]})
    _install_clients(monkeypatch, {"111111111111": fake, "222222222222": fake})

    row = ce_module._fetch_account_cost("111111111111", "TestAccountOne", object())
    assert row["mtd"]["usage"] == 102.00  # 100 + 2 residual
    assert row["mtd"]["credits"] == -5.00  # untouched by reconciliation
    assert row["mtd"]["subtotal"] == 97.00


def test_billing_entity_marketplace_overrides_service_name_heuristic(patch_accounts, monkeypatch):
    """A service that doesn't start with 'AWS Marketplace' but has
    BILLING_ENTITY == 'AWS Marketplace' must still classify as marketplace,
    and a service that DOES start with the prefix but has a different
    BILLING_ENTITY must NOT be misclassified — BILLING_ENTITY wins."""
    assert ce_module._classify_service("Some Vendor Product", "AWS Marketplace") == "marketplace"
    assert ce_module._classify_service("AWS Marketplace Consulting", "AWS") == "usage"
    # No BILLING_ENTITY info available at all -> fall back to name heuristic.
    assert ce_module._classify_service("AWS Marketplace Consulting", None) == "marketplace"


def test_malformed_amounts_are_treated_as_zero_not_crash(patch_accounts, monkeypatch):
    two_ago, prev, mtd, _tomorrow = _bounds()
    normal_page = [
        _bucket(mtd, groups=[(("Amazon EC2", "AWS"), "not-a-number"), (("Amazon S3", "AWS"), "NaN")]),
        _bucket(prev, groups=[]),
        _bucket(two_ago, groups=[]),
    ]
    adjustment_page = [_bucket(mtd, total="Infinity"), _bucket(prev, total="0"), _bucket(two_ago, total="0")]
    totals_page = [_bucket(mtd, total="0"), _bucket(prev, total="0"), _bucket(two_ago, total="0")]

    fake = FakeCostExplorerClient(script={"normal": [normal_page], "adjustment": [adjustment_page], "totals": [totals_page]})
    _install_clients(monkeypatch, {"111111111111": fake, "222222222222": fake})

    row = ce_module._fetch_account_cost("111111111111", "TestAccountOne", object())
    assert row["error"] is None
    assert row["mtd"]["usage"] == 0.0
    assert row["mtd"]["credits"] == 0.0
    assert row["mtd"]["subtotal"] == 0.0


def test_account_error_row_has_generic_message_and_complete_shape(patch_accounts, monkeypatch):
    failing = FakeCostExplorerClient(script={}, raise_on_call=True)
    _install_clients(monkeypatch, {"111111111111": failing, "222222222222": failing})

    row = ce_module._fetch_account_cost("111111111111", "TestAccountOne", object())
    assert row["error"] == "Could not fetch cost data for this account"
    assert "simulated" not in row["error"]  # raw exception text must not leak
    for period_key in ("mtd", "last_month", "two_months_ago"):
        period = row[period_key]
        for k in ("usage", "marketplace", "shield", "support", "credits", "subtotal"):
            assert k in period
    assert row["mtd_cost"] == 0.0
    assert row["last_month_cost"] == 0.0
    assert row["services_mtd"] == []


# ── Long-tail / top-services aggregation ─────────────────────────────────

def test_top_services_folds_long_tail_into_other_services():
    amounts = {f"Service{i}": float(20 - i) for i in range(15)}
    top = ce_module._top_services(amounts, limit=10)
    assert len(top) == 11  # 10 individual + "Other services"
    assert top[-1]["service"] == "Other services"
    kept_total = sum(t["amount"] for t in top if t["service"] != "Other services")
    other_total = top[-1]["amount"]
    assert round(kept_total + other_total, 2) == round(sum(amounts.values()), 2)


def test_top_services_ignores_negative_and_nonfinite_amounts():
    amounts = {"Good": 10.0, "Negative": -5.0, "NotANumber": float("nan")}
    top = ce_module._top_services(amounts)
    names = {t["service"] for t in top}
    assert "Good" in names
    assert "Negative" not in names
    assert "NotANumber" not in names


# ── Org-wide aggregation / top_services_last_month ───────────────────────

def test_build_summary_includes_top_services_last_month_from_full_maps():
    accounts_result = [
        {
            "account_id": "111111111111", "account_name": "A", "bu": "BU-A",
            "mtd": ce_module._empty_breakdown(), "last_month": ce_module._empty_breakdown(),
            "two_months_ago": ce_module._empty_breakdown(),
            "services_mtd": [], "services_last_month": [],
            "mtd_vs_last_month_pct": None, "last_month_vs_prior_pct": None,
            "mtd_cost": 50.0, "last_month_cost": 40.0, "currency": "USD", "error": None,
            "_full_services_mtd": {"Amazon EC2": 50.0},
            "_full_services_last_month": {"Amazon EC2": 30.0, "Amazon S3": 10.0},
        },
    ]
    summary = ce_module._build_summary(accounts_result)
    assert "top_services_last_month" in summary
    names = {s["service"] for s in summary["top_services_last_month"]}
    assert "Amazon EC2" in names and "Amazon S3" in names
    # Internal keys must never leak into the public account rows.
    assert all(not k.startswith("_full_") for k in summary["accounts"][0].keys())


# ── pct_change semantics ──────────────────────────────────────────────────

@pytest.mark.parametrize("new,old,expected", [
    (110.0, 100.0, 10.0),
    (90.0, 100.0, -10.0),
    (10.0, 0.0, None),
    (10.0, -5.0, None),
    (10.0, None, None),
    (None, 100.0, None),
    (float("nan"), 100.0, None),
])
def test_pct_change_semantics(new, old, expected):
    assert ce_module._pct_change(new, old) == expected


# ── Cache: TTL, isolation, single-flight, last-known-good fallback ──────

def test_cache_returns_same_summary_within_ttl(patch_accounts, monkeypatch):
    two_ago, prev, mtd, _tomorrow = _bounds()
    normal_page = [_bucket(mtd, groups=[(("Amazon EC2", "AWS"), "10.00")]), _bucket(prev, groups=[]), _bucket(two_ago, groups=[])]
    fake = FakeCostExplorerClient(script={"normal": [normal_page], "adjustment": [[]], "totals": [[]]})
    _install_clients(monkeypatch, {"111111111111": fake, "222222222222": fake})

    first = ce_module.get_organization_cost_summary()
    calls_after_first = len(fake.calls)
    second = ce_module.get_organization_cost_summary()
    assert len(fake.calls) == calls_after_first  # no new Cost Explorer calls
    assert second["total_mtd_cost"] == first["total_mtd_cost"]
    # Returned summaries must be independent copies, not shared references.
    second["accounts"][0]["mtd_cost"] = -999
    third = ce_module.get_organization_cost_summary()
    assert third["accounts"][0]["mtd_cost"] != -999


def test_force_refresh_bypasses_cache(patch_accounts, monkeypatch):
    two_ago, prev, mtd, _tomorrow = _bounds()
    normal_page = [_bucket(mtd, groups=[(("Amazon EC2", "AWS"), "10.00")]), _bucket(prev, groups=[]), _bucket(two_ago, groups=[])]
    fake = FakeCostExplorerClient(script={"normal": [normal_page], "adjustment": [[]], "totals": [[]]})
    _install_clients(monkeypatch, {"111111111111": fake, "222222222222": fake})

    ce_module.get_organization_cost_summary()
    calls_after_first = len(fake.calls)
    ce_module.get_organization_cost_summary(force_refresh=True)
    assert len(fake.calls) > calls_after_first


def test_last_known_good_fallback_when_all_accounts_fail(patch_accounts, monkeypatch):
    two_ago, prev, mtd, _tomorrow = _bounds()
    normal_page = [_bucket(mtd, groups=[(("Amazon EC2", "AWS"), "10.00")]), _bucket(prev, groups=[]), _bucket(two_ago, groups=[])]
    good_client = FakeCostExplorerClient(script={"normal": [normal_page], "adjustment": [[]], "totals": [[]]})
    _install_clients(monkeypatch, {"111111111111": good_client, "222222222222": good_client})

    good_summary = ce_module.get_organization_cost_summary()
    assert good_summary["error_count"] == 0

    failing_client = FakeCostExplorerClient(script={}, raise_on_call=True)
    _install_clients(monkeypatch, {"111111111111": failing_client, "222222222222": failing_client})

    fallback_summary = ce_module.get_organization_cost_summary(force_refresh=True)
    assert fallback_summary.get("stale_fallback") is True
    assert fallback_summary["total_mtd_cost"] == good_summary["total_mtd_cost"]


def test_single_flight_avoids_duplicate_concurrent_fetches(patch_accounts, monkeypatch):
    two_ago, prev, mtd, _tomorrow = _bounds()
    normal_page = [_bucket(mtd, groups=[(("Amazon EC2", "AWS"), "10.00")]), _bucket(prev, groups=[]), _bucket(two_ago, groups=[])]

    call_count = {"n": 0}
    lock = threading.Lock()

    class SlowClient(FakeCostExplorerClient):
        def get_cost_and_usage(self, **kwargs):
            with lock:
                call_count["n"] += 1
            time.sleep(0.05)
            return super().get_cost_and_usage(**kwargs)

    fake = SlowClient(script={"normal": [normal_page], "adjustment": [[]], "totals": [[]]})
    _install_clients(monkeypatch, {"111111111111": fake, "222222222222": fake})

    results = []

    def worker():
        results.append(ce_module.get_organization_cost_summary(force_refresh=True))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 5
    for r in results:
        assert r["total_mtd_cost"] == results[0]["total_mtd_cost"]
    # Single-flight should mean far fewer than 5x(2 accounts x 3 queries)
    # calls were made — some concurrent callers waited instead of firing
    # their own duplicate fetch.
    assert call_count["n"] < 5 * 2 * 3


# ── Manual cost adjustments (period_key_for / apply_manual_adjustments) ──

def test_period_key_for_maps_names_to_current_calendar_months():
    two_ago, prev, mtd, _tomorrow = _bounds()
    assert ce_module.period_key_for("mtd") == mtd[:7]
    assert ce_module.period_key_for("last_month") == prev[:7]
    assert ce_module.period_key_for("two_months_ago") == two_ago[:7]
    # Unknown period name falls back to the current MTD month rather than
    # raising, so a caller passing a typo'd name degrades safely.
    assert ce_module.period_key_for("bogus") == mtd[:7]


def _fake_account(account_id, bu, mtd_usage, mtd_credits=0.0):
    breakdown_mtd = ce_module._empty_breakdown()
    breakdown_mtd["usage"] = mtd_usage
    breakdown_mtd["credits"] = mtd_credits
    breakdown_mtd["subtotal"] = mtd_usage + mtd_credits
    breakdown_last = ce_module._empty_breakdown()
    breakdown_last["subtotal"] = 0.0
    breakdown_two_ago = ce_module._empty_breakdown()
    breakdown_two_ago["subtotal"] = 0.0
    return {
        "account_id": account_id, "account_name": f"Account-{account_id}", "bu": bu,
        "mtd": breakdown_mtd, "last_month": breakdown_last, "two_months_ago": breakdown_two_ago,
        "services_mtd": [], "services_last_month": [],
        "mtd_vs_last_month_pct": None, "last_month_vs_prior_pct": None,
        "mtd_cost": breakdown_mtd["subtotal"], "last_month_cost": 0.0,
        "currency": "USD", "error": None,
        "_full_services_mtd": {}, "_full_services_last_month": {},
    }


def _fake_org_summary(*accounts):
    summary = ce_module._build_summary(list(accounts))
    return summary


def test_apply_manual_adjustments_adds_to_matching_account_and_period():
    mtd_month = ce_module.period_key_for("mtd")
    acct = _fake_account("111111111111", "BU-A", mtd_usage=100.0)
    summary = _fake_org_summary(acct)
    assert summary["accounts"][0]["mtd"]["adjustments"] == 0.0
    assert summary["accounts"][0]["mtd"]["subtotal"] == 100.0
    assert summary["total_mtd_cost"] == 100.0

    adjustments = [{
        "id": "adj-1", "account_id": "111111111111", "period": mtd_month,
        "label": "Partner invoice", "amount": 25.0, "currency": "USD",
    }]
    adjusted = ce_module.apply_manual_adjustments(summary, adjustments)

    account = adjusted["accounts"][0]
    assert account["mtd"]["adjustments"] == 25.0
    assert account["mtd"]["subtotal"] == 125.0
    assert account["mtd"]["usage"] == 100.0  # untouched AWS-reported number
    assert account["mtd_cost"] == 125.0
    assert adjusted["total_mtd_cost"] == 125.0
    # Original summary must never be mutated in place.
    assert summary["accounts"][0]["mtd"]["adjustments"] == 0.0
    assert summary["total_mtd_cost"] == 100.0


def test_apply_manual_adjustments_multiple_entries_accumulate():
    mtd_month = ce_module.period_key_for("mtd")
    acct = _fake_account("111111111111", "BU-A", mtd_usage=100.0)
    summary = _fake_org_summary(acct)

    adjustments = [
        {"id": "adj-1", "account_id": "111111111111", "period": mtd_month, "amount": 10.0},
        {"id": "adj-2", "account_id": "111111111111", "period": mtd_month, "amount": -3.5},
    ]
    adjusted = ce_module.apply_manual_adjustments(summary, adjustments)
    account = adjusted["accounts"][0]
    assert account["mtd"]["adjustments"] == 6.5
    assert account["mtd"]["subtotal"] == 106.5


def test_apply_manual_adjustments_ignores_unknown_account():
    mtd_month = ce_module.period_key_for("mtd")
    acct = _fake_account("111111111111", "BU-A", mtd_usage=100.0)
    summary = _fake_org_summary(acct)

    adjustments = [{"id": "adj-1", "account_id": "999999999999", "period": mtd_month, "amount": 50.0}]
    adjusted = ce_module.apply_manual_adjustments(summary, adjustments)
    assert adjusted["accounts"][0]["mtd"]["adjustments"] == 0.0
    assert adjusted["total_mtd_cost"] == 100.0


def test_apply_manual_adjustments_ignores_period_outside_current_window():
    acct = _fake_account("111111111111", "BU-A", mtd_usage=100.0)
    summary = _fake_org_summary(acct)

    adjustments = [{"id": "adj-1", "account_id": "111111111111", "period": "2000-01", "amount": 999.0}]
    adjusted = ce_module.apply_manual_adjustments(summary, adjustments)
    assert adjusted["accounts"][0]["mtd"]["adjustments"] == 0.0
    assert adjusted["total_mtd_cost"] == 100.0


def test_apply_manual_adjustments_preserves_top_services_and_metadata():
    mtd_month = ce_module.period_key_for("mtd")
    acct = _fake_account("111111111111", "BU-A", mtd_usage=100.0)
    acct["_full_services_mtd"] = {"Amazon EC2": 100.0}
    summary = _fake_org_summary(acct)
    summary["is_mock"] = True
    summary["as_of"] = "2026-08-04T00:00:00Z"

    adjustments = [{"id": "adj-1", "account_id": "111111111111", "period": mtd_month, "amount": 10.0}]
    adjusted = ce_module.apply_manual_adjustments(summary, adjustments)

    assert adjusted["is_mock"] is True
    assert adjusted["as_of"] == "2026-08-04T00:00:00Z"
    names = {s["service"] for s in adjusted["top_services_mtd"]}
    assert "Amazon EC2" in names
    # Internal keys never leak, even after the rebuild.
    assert all(not k.startswith("_full_") for k in adjusted["accounts"][0].keys())


def test_apply_manual_adjustments_recomputes_business_units():
    mtd_month = ce_module.period_key_for("mtd")
    acct_a = _fake_account("111111111111", "BU-A", mtd_usage=100.0)
    acct_b = _fake_account("222222222222", "BU-B", mtd_usage=50.0)
    summary = _fake_org_summary(acct_a, acct_b)

    adjustments = [{"id": "adj-1", "account_id": "111111111111", "period": mtd_month, "amount": 20.0}]
    adjusted = ce_module.apply_manual_adjustments(summary, adjustments)

    bu_by_name = {b["bu"]: b for b in adjusted["business_units"]}
    assert bu_by_name["BU-A"]["mtd_cost"] == 120.0
    assert bu_by_name["BU-B"]["mtd_cost"] == 50.0
    assert adjusted["total_mtd_cost"] == 170.0


def test_apply_manual_adjustments_empty_list_is_noop():
    acct = _fake_account("111111111111", "BU-A", mtd_usage=100.0)
    summary = _fake_org_summary(acct)
    adjusted = ce_module.apply_manual_adjustments(summary, [])
    assert adjusted["total_mtd_cost"] == summary["total_mtd_cost"]
    assert adjusted["accounts"][0]["mtd"]["adjustments"] == 0.0
