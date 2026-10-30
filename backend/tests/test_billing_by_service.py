"""
Tests for app/billing_by_service.py — Console-matching per-service billing
classification (EC2 merge, Data Transfer folding, Marketplace detection),
row-building, and caching. None of these tests make real AWS/network
calls: `_client_for_account` is monkeypatched with a fully controlled fake
Cost Explorer client, the same approach test_cost_explorer.py uses.
"""

from __future__ import annotations

import pytest

from app import billing_by_service as bbs_module


def _group(service, usage_type, amount):
    return {"Keys": [service, usage_type], "Metrics": {"UnblendedCost": {"Amount": str(amount), "Unit": "USD"}}}


def _bucket(start, groups):
    return {"TimePeriod": {"Start": start}, "Groups": groups}


class FakeCeClient:
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


# ── Pure classification helpers ──────────────────────────────────────────

def test_is_marketplace_detects_known_keywords():
    assert bbs_module.is_marketplace("Datadog Pro")
    assert bbs_module.is_marketplace("Some Product (sold by Vendor Inc)")
    assert not bbs_module.is_marketplace("Amazon Elastic Compute Cloud - Compute")


def test_short_name_strips_prefix_for_web_services():
    assert bbs_module.short_name("Amazon Elastic Compute Cloud") == "Elastic Compute Cloud"
    assert bbs_module.short_name("AWS Shield") == "Shield"
    assert bbs_module.short_name("Something Else") == "Something Else"


def test_short_name_adds_known_sold_by_suffix_for_marketplace():
    result = bbs_module.short_name("Datadog Pro")
    assert result == "Datadog Pro (sold by Datadog Inc.)"


def test_short_name_marketplace_without_known_mapping_unchanged():
    result = bbs_module.short_name("Random Marketplace Product sold by Someone")
    assert result == "Random Marketplace Product sold by Someone"


def test_is_data_transfer_usage_matches_expected_patterns():
    assert bbs_module.is_data_transfer_usage("USE1-AWS-Out-Bytes")
    assert bbs_module.is_data_transfer_usage("USE1-AWS-In-Bytes")
    assert bbs_module.is_data_transfer_usage("USE1-DataTransfer-Regional-Bytes")
    assert not bbs_module.is_data_transfer_usage("BoxUsage:t3.micro")


def test_month_label_formats_correctly():
    assert bbs_module.month_label("2026-03") == "Mar-26"
    assert bbs_module.month_label("2025-12") == "Dec-25"


def test_list_accounts_returns_id_and_alias_pairs():
    accounts = bbs_module.list_accounts()
    assert len(accounts) == len(bbs_module.ACCOUNT_ALIAS)
    first = accounts[0]
    assert set(first.keys()) == {"account_id", "alias"}
    assert first["account_id"] == "100000000001"
    assert first["alias"] == "core"


# ── _fetch_month_service_costs: EC2 merge / Data Transfer / VPC peering ──

def test_fetch_month_service_costs_merges_ec2_compute_and_other(monkeypatch):
    fake = FakeCeClient({
        "2026-03": [[_bucket("2026-03-01", [
            _group("Amazon Elastic Compute Cloud - Compute", "BoxUsage:t3.micro", "100.00"),
            _group("EC2 - Other", "EBS:VolumeUsage", "20.00"),
        ])]],
    })
    monkeypatch.setattr(bbs_module, "_client_for_account", lambda *a, **k: fake)
    costs = bbs_module._fetch_month_service_costs("111111111111", "2026-03", object())
    assert costs == {"Amazon Elastic Compute Cloud": 120.00}


def test_fetch_month_service_costs_folds_data_transfer_except_shield_and_cloudfront(monkeypatch):
    fake = FakeCeClient({
        "2026-03": [[_bucket("2026-03-01", [
            _group("Amazon Elastic Compute Cloud - Compute", "USE1-AWS-Out-Bytes", "5.00"),
            _group("AWS Shield", "USE1-AWS-Out-Bytes", "1.00"),
            _group("Amazon CloudFront", "USE1-DataTransfer-Out-Bytes", "2.00"),
            _group("Amazon Simple Storage Service", "TimedStorage-ByteHrs", "50.00"),
        ])]],
    })
    monkeypatch.setattr(bbs_module, "_client_for_account", lambda *a, **k: fake)
    costs = bbs_module._fetch_month_service_costs("111111111111", "2026-03", object())
    assert costs["Data Transfer"] == 5.00
    assert costs["AWS Shield"] == 1.00
    assert costs["Amazon CloudFront"] == 2.00
    assert costs["Amazon Simple Storage Service"] == 50.00
    # EC2 merge still happens even with zero non-DT EC2 usage this month.
    assert "Amazon Elastic Compute Cloud" not in costs


def test_fetch_month_service_costs_routes_vpc_peering_to_vpc_service(monkeypatch):
    fake = FakeCeClient({
        "2026-03": [[_bucket("2026-03-01", [
            _group("Amazon Elastic Compute Cloud - Compute", "VpcPeering-In-Bytes", "3.00"),
        ])]],
    })
    monkeypatch.setattr(bbs_module, "_client_for_account", lambda *a, **k: fake)
    costs = bbs_module._fetch_month_service_costs("111111111111", "2026-03", object())
    assert costs == {"Amazon Virtual Private Cloud": 3.00}


def test_fetch_month_service_costs_paginates(monkeypatch):
    fake = FakeCeClient({
        "2026-03": [
            [_bucket("2026-03-01", [_group("Amazon Simple Storage Service", "TimedStorage-ByteHrs", "10.00")])],
            [_bucket("2026-03-01", [_group("Amazon Simple Storage Service", "Requests-Tier1", "5.00")])],
        ],
    })
    monkeypatch.setattr(bbs_module, "_client_for_account", lambda *a, **k: fake)
    costs = bbs_module._fetch_month_service_costs("111111111111", "2026-03", object())
    assert costs["Amazon Simple Storage Service"] == 15.00


# ── _build_account_rows: category ordering + per-month amounts ──────────

def test_build_account_rows_orders_web_services_before_marketplace():
    all_costs = {
        "2026-03": {
            "Amazon Simple Storage Service": 10.0,
            "Datadog Pro": 5.0,
            "Amazon Elastic Compute Cloud": 20.0,
        },
    }
    rows = bbs_module._build_account_rows(all_costs, ["2026-03"])
    categories = [r["category"] for r in rows]
    # All "Web Services" rows come first (alphabetical), then Marketplace.
    assert categories == ["Web Services", "Web Services", "Marketplace"]
    services = [r["service"] for r in rows]
    assert services == ["Elastic Compute Cloud", "Simple Storage Service", "Datadog Pro (sold by Datadog Inc.)"]


def test_build_account_rows_fills_zero_for_missing_month():
    all_costs = {
        "2026-02": {"Amazon Simple Storage Service": 10.0},
        "2026-03": {},  # service absent this month
    }
    rows = bbs_module._build_account_rows(all_costs, ["2026-02", "2026-03"])
    s3_row = next(r for r in rows if r["service"] == "Simple Storage Service")
    assert s3_row["amounts"] == {"2026-02": 10.0, "2026-03": 0.0}


# ── _fetch_account_report: error isolation ───────────────────────────────

def test_fetch_account_report_error_row_on_failure(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(bbs_module, "_client_for_account", _raise)
    row = bbs_module._fetch_account_report("111111111111", ["2026-03"], object())
    assert row["error"] == "Could not fetch billing data for this account"
    assert row["rows"] == []
    assert "simulated" not in row["error"]


# ── get_billing_report: validation + local mock path ─────────────────────

def test_get_billing_report_requires_account_ids():
    from app.cost_explorer import CostAnalysisError
    with pytest.raises(CostAnalysisError):
        bbs_module.get_billing_report([], ["2026-03"])


def test_get_billing_report_requires_months():
    from app.cost_explorer import CostAnalysisError
    with pytest.raises(CostAnalysisError):
        bbs_module.get_billing_report(["111111111111"], [])


def test_get_billing_report_local_mock_shape(monkeypatch):
    monkeypatch.setattr(bbs_module, "ENV", "local")
    report = bbs_module.get_billing_report(["100000000001", "100000000003"], ["2026-01", "2026-02"])
    assert report["is_mock"] is True
    assert report["months"] == ["2026-01", "2026-02"]
    assert report["month_labels"] == ["Jan-26", "Feb-26"]
    assert len(report["accounts"]) == 2
    for account in report["accounts"]:
        assert account["error"] is None
        assert set(account["totals"].keys()) == {"2026-01", "2026-02"}


def test_get_billing_report_local_mock_is_deterministic(monkeypatch):
    monkeypatch.setattr(bbs_module, "ENV", "local")
    report1 = bbs_module.get_billing_report(["100000000001"], ["2026-01"])
    report2 = bbs_module.get_billing_report(["100000000001"], ["2026-01"])
    assert report1["accounts"][0]["totals"] == report2["accounts"][0]["totals"]


# ── Caching (non-mock path) ───────────────────────────────────────────────

def test_get_billing_report_caches_within_ttl(monkeypatch):
    monkeypatch.setattr(bbs_module, "ENV", "production")
    bbs_module.reset_cache_for_tests()
    monkeypatch.setattr(bbs_module, "_core_session", lambda: object())

    fake = FakeCeClient({"2026-03": [[_bucket("2026-03-01", [_group("Amazon Simple Storage Service", "TimedStorage-ByteHrs", "10.00")])]]})
    monkeypatch.setattr(bbs_module, "_client_for_account", lambda *a, **k: fake)

    first = bbs_module.get_billing_report(["111111111111"], ["2026-03"])
    calls_after_first = len(fake.calls)
    second = bbs_module.get_billing_report(["111111111111"], ["2026-03"])
    assert len(fake.calls) == calls_after_first  # cached, no new CE calls
    assert second["accounts"][0]["totals"] == first["accounts"][0]["totals"]
    bbs_module.reset_cache_for_tests()


def test_get_billing_report_force_refresh_bypasses_cache(monkeypatch):
    monkeypatch.setattr(bbs_module, "ENV", "production")
    bbs_module.reset_cache_for_tests()
    monkeypatch.setattr(bbs_module, "_core_session", lambda: object())

    fake = FakeCeClient({"2026-03": [[_bucket("2026-03-01", [_group("Amazon Simple Storage Service", "TimedStorage-ByteHrs", "10.00")])]]})
    monkeypatch.setattr(bbs_module, "_client_for_account", lambda *a, **k: fake)

    bbs_module.get_billing_report(["111111111111"], ["2026-03"])
    calls_after_first = len(fake.calls)
    bbs_module.get_billing_report(["111111111111"], ["2026-03"], force_refresh=True)
    assert len(fake.calls) > calls_after_first
    bbs_module.reset_cache_for_tests()


def test_get_billing_report_different_month_sets_are_cached_separately(monkeypatch):
    monkeypatch.setattr(bbs_module, "ENV", "production")
    bbs_module.reset_cache_for_tests()
    monkeypatch.setattr(bbs_module, "_core_session", lambda: object())

    fake = FakeCeClient({
        "2026-03": [[_bucket("2026-03-01", [_group("Amazon Simple Storage Service", "TimedStorage-ByteHrs", "10.00")])]],
        "2026-04": [[_bucket("2026-04-01", [_group("Amazon Simple Storage Service", "TimedStorage-ByteHrs", "99.00")])]],
    })
    monkeypatch.setattr(bbs_module, "_client_for_account", lambda *a, **k: fake)

    report_march = bbs_module.get_billing_report(["111111111111"], ["2026-03"])
    report_april = bbs_module.get_billing_report(["111111111111"], ["2026-04"])
    assert report_march["accounts"][0]["totals"]["2026-03"] == 10.00
    assert report_april["accounts"][0]["totals"]["2026-04"] == 99.00
    bbs_module.reset_cache_for_tests()


# ── Excel export (build_workbook) ─────────────────────────────────────────

def test_build_workbook_creates_one_sheet_per_account_with_expected_layout():
    report = {
        "months": ["2026-03"],
        "month_labels": ["Mar-26"],
        "accounts": [
            {
                "account_id": "111111111111", "alias": "test-alias", "error": None,
                "rows": [
                    {"category": "Web Services", "service": "Simple Storage Service", "amounts": {"2026-03": 10.0}},
                ],
                "totals": {"2026-03": 10.0},
            },
        ],
        "is_mock": False,
    }
    wb = bbs_module.build_workbook(report)
    assert wb.sheetnames == ["test-alias"]
    ws = wb["test-alias"]
    assert [c.value for c in ws[1]] == ["test-alias", "item", "Mar-26"]
    assert [c.value for c in ws[2]] == ["Web Services", "Simple Storage Service", 10.0]
    # Grand Total is a live =SUM() formula (auto-sums if edited), not a
    # pre-computed static number.
    assert [c.value for c in ws[3]] == ["", "Grand total", "=SUM(C2:C2)"]


def test_build_workbook_handles_error_accounts_gracefully():
    report = {
        "months": ["2026-03"], "month_labels": ["Mar-26"],
        "accounts": [{"account_id": "1", "alias": "broken", "error": "Could not fetch billing data for this account", "rows": [], "totals": {}}],
        "is_mock": False,
    }
    wb = bbs_module.build_workbook(report)
    ws = wb["broken"]
    assert ws[2][1].value.startswith("ERROR:")


def test_workbook_to_bytes_returns_nonempty_xlsx_bytes():
    report = {
        "months": ["2026-03"], "month_labels": ["Mar-26"],
        "accounts": [{"account_id": "1", "alias": "a", "error": None, "rows": [], "totals": {"2026-03": 0.0}}],
        "is_mock": False,
    }
    wb = bbs_module.build_workbook(report)
    data = bbs_module.workbook_to_bytes(wb)
    assert isinstance(data, bytes)
    assert len(data) > 0
    # XLSX files are zip archives — magic bytes 'PK'.
    assert data[:2] == b"PK"


# ── merge_report_into_template ───────────────────────────────────────────
# Builds a small synthetic template in the exact shape reverse-engineered
# from the real master workbook (real datetime header cells, a Grand
# Total row with SUM() formulas, and the manual helper-column area to the
# right: blank spacer, two comparison-formula columns, a note, then a
# small VLOOKUP staging block) — deliberately NOT the user's real file,
# so these tests never depend on or touch real account data.

def _make_synthetic_template():
    from openpyxl import Workbook
    from datetime import datetime as dt

    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(title="111111111111-testacct")

    header = ["testacct", "item", dt(2026, 1, 1), dt(2026, 2, 1)]
    ws.append(header)
    for col in (3, 4):
        ws.cell(row=1, column=col).number_format = "[$-409]mmm\\-yy;@"

    ws.append(["Web Services", "Elastic Compute Cloud", 100.0, 110.0])
    ws.append(["Web Services", "Simple Storage Service", 20.0, 22.0])
    ws.append(["", "Grand total", "=SUM(C2:C3)", "=SUM(D2:D3)"])

    grand_row = 4
    ws.cell(row=grand_row, column=13).value = "=H4-F4"  # harmless stray formula, mimics real sheet quirks

    # Manual helper-column area to the right of the month columns.
    ws.cell(row=1, column=6).value = "Jan vs Feb"
    ws.cell(row=2, column=6).value = "=D2-C2"
    ws.cell(row=3, column=6).value = "=D3-C3"
    ws.cell(row=4, column=6).value = "=D4-C4"
    ws.cell(row=1, column=8).value = "a manual note, must survive untouched"

    ws.cell(row=1, column=10).value = "Function\u2193"
    ws.cell(row=2, column=10).value = "=IFERROR(VLOOKUP($B2, $M:$O, 2, FALSE), 0)"
    ws.cell(row=1, column=11).value = "Paste New Data"
    ws.cell(row=2, column=11).value = "=IFERROR(VLOOKUP($B2, $M:$O, 3, FALSE), 0)"
    ws.cell(row=1, column=13).value = "testacct"
    ws.cell(row=1, column=14).value = "item"
    ws.cell(row=1, column=15).value = "New Month-1"
    ws.cell(row=2, column=13).value = "Web Services"
    ws.cell(row=2, column=14).value = "Elastic Compute Cloud"
    ws.cell(row=2, column=15).value = 999

    buf = bbs_module.io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _synthetic_report(months, accounts_rows):
    """accounts_rows: {account_id: (alias, rows, totals)}"""
    return {
        "months": months,
        "month_labels": [bbs_module.month_label(m) for m in months],
        "accounts": [
            {"account_id": acct_id, "alias": alias, "error": None, "rows": rows, "totals": totals}
            for acct_id, (alias, rows, totals) in accounts_rows.items()
        ],
        "is_mock": False,
    }


def test_merge_updates_existing_row_in_place():
    template_bytes = _make_synthetic_template()
    report = _synthetic_report(
        ["2026-01", "2026-02"],
        {"111111111111": ("testacct", [
            {"category": "Web Services", "service": "Elastic Compute Cloud", "amounts": {"2026-01": 150.0, "2026-02": 160.0}},
            {"category": "Web Services", "service": "Simple Storage Service", "amounts": {"2026-01": 20.0, "2026-02": 22.0}},
        ], {"2026-01": 170.0, "2026-02": 182.0})},
    )
    out_bytes = bbs_module.merge_report_into_template(report, template_bytes)

    from openpyxl import load_workbook
    wb = load_workbook(bbs_module.io.BytesIO(out_bytes), data_only=False)
    ws = wb["111111111111-testacct"]
    assert ws.cell(row=2, column=3).value == 150.0
    assert ws.cell(row=2, column=4).value == 160.0
    assert ws.cell(row=3, column=3).value == 20.0


def test_merge_inserts_new_item_row_and_fixes_grand_total_sum():
    template_bytes = _make_synthetic_template()
    report = _synthetic_report(
        ["2026-01", "2026-02"],
        {"111111111111": ("testacct", [
            {"category": "Web Services", "service": "Elastic Compute Cloud", "amounts": {"2026-01": 100.0, "2026-02": 110.0}},
            {"category": "Web Services", "service": "Simple Storage Service", "amounts": {"2026-01": 20.0, "2026-02": 22.0}},
            {"category": "Web Services", "service": "AWS Lambda", "amounts": {"2026-01": 5.0, "2026-02": 6.0}},
        ], {"2026-01": 125.0, "2026-02": 138.0})},
    )
    out_bytes = bbs_module.merge_report_into_template(report, template_bytes)

    from openpyxl import load_workbook
    wb = load_workbook(bbs_module.io.BytesIO(out_bytes), data_only=False)
    ws = wb["111111111111-testacct"]

    grand_row = bbs_module._find_grand_total_row(ws)
    assert grand_row == 5  # was 4, shifted down by the one new row

    assert ws.cell(row=4, column=1).value == "Web Services"
    assert ws.cell(row=4, column=2).value == "AWS Lambda"
    assert ws.cell(row=4, column=3).value == 5.0

    assert ws.cell(row=grand_row, column=3).value == "=SUM(C2:C4)"
    assert ws.cell(row=grand_row, column=4).value == "=SUM(D2:D4)"


def test_merge_leaves_helper_columns_untouched():
    template_bytes = _make_synthetic_template()
    report = _synthetic_report(
        ["2026-01", "2026-02"],
        {"111111111111": ("testacct", [
            {"category": "Web Services", "service": "Elastic Compute Cloud", "amounts": {"2026-01": 100.0, "2026-02": 110.0}},
            {"category": "Web Services", "service": "Simple Storage Service", "amounts": {"2026-01": 20.0, "2026-02": 22.0}},
        ], {"2026-01": 120.0, "2026-02": 132.0})},
    )
    out_bytes = bbs_module.merge_report_into_template(report, template_bytes)

    from openpyxl import load_workbook
    wb = load_workbook(bbs_module.io.BytesIO(out_bytes), data_only=False)
    ws = wb["111111111111-testacct"]

    assert ws.cell(row=1, column=6).value == "Jan vs Feb"
    assert ws.cell(row=2, column=6).value == "=D2-C2"
    assert ws.cell(row=1, column=8).value == "a manual note, must survive untouched"
    assert ws.cell(row=1, column=10).value == "Function\u2193"
    assert ws.cell(row=2, column=13).value == "Web Services"
    assert ws.cell(row=2, column=15).value == 999


def test_merge_adds_new_month_column_when_report_has_unseen_month():
    template_bytes = _make_synthetic_template()
    report = _synthetic_report(
        ["2026-01", "2026-02", "2026-03"],
        {"111111111111": ("testacct", [
            {"category": "Web Services", "service": "Elastic Compute Cloud", "amounts": {"2026-01": 100.0, "2026-02": 110.0, "2026-03": 120.0}},
            {"category": "Web Services", "service": "Simple Storage Service", "amounts": {"2026-01": 20.0, "2026-02": 22.0, "2026-03": 24.0}},
        ], {"2026-01": 120.0, "2026-02": 132.0, "2026-03": 144.0})},
    )
    out_bytes = bbs_module.merge_report_into_template(report, template_bytes)

    from openpyxl import load_workbook
    from datetime import datetime as dt
    wb = load_workbook(bbs_module.io.BytesIO(out_bytes), data_only=False)
    ws = wb["111111111111-testacct"]

    # New month column inserted right after the last existing month
    # column (col 4 = Feb), i.e. at col 5, before the old col 5 ("Jan vs
    # Feb" helper header) which should have shifted right to col 7.
    assert ws.cell(row=1, column=5).value == dt(2026, 3, 1)
    assert ws.cell(row=2, column=5).value == 120.0
    assert ws.cell(row=1, column=7).value == "Jan vs Feb"


def test_merge_creates_new_sheet_for_account_not_in_template():
    template_bytes = _make_synthetic_template()
    report = _synthetic_report(
        ["2026-01"],
        {
            "111111111111": ("testacct", [
                {"category": "Web Services", "service": "Elastic Compute Cloud", "amounts": {"2026-01": 100.0}},
            ], {"2026-01": 100.0}),
            "222222222222": ("brandnew", [
                {"category": "Web Services", "service": "AWS Lambda", "amounts": {"2026-01": 7.5}},
            ], {"2026-01": 7.5}),
        },
    )
    out_bytes = bbs_module.merge_report_into_template(report, template_bytes)

    from openpyxl import load_workbook
    wb = load_workbook(bbs_module.io.BytesIO(out_bytes), data_only=False)
    assert "222222222222-brandnew" in wb.sheetnames
    ws = wb["222222222222-brandnew"]
    assert [c.value for c in ws[1]] == ["brandnew", "item", "Jan-26"]
    assert [c.value for c in ws[2]] == ["Web Services", "AWS Lambda", 7.5]
    assert ws.cell(row=3, column=3).value == "=SUM(C2:C2)"


def test_merge_skips_error_accounts_without_touching_their_sheet():
    template_bytes = _make_synthetic_template()
    report = {
        "months": ["2026-01"], "month_labels": ["Jan-26"],
        "accounts": [
            {"account_id": "111111111111", "alias": "testacct", "error": "Could not fetch billing data for this account", "rows": [], "totals": {}},
        ],
        "is_mock": False,
    }
    out_bytes = bbs_module.merge_report_into_template(report, template_bytes)

    from openpyxl import load_workbook
    wb = load_workbook(bbs_module.io.BytesIO(out_bytes), data_only=False)
    ws = wb["111111111111-testacct"]
    # Original rows untouched since the account errored this fetch.
    assert ws.cell(row=2, column=2).value == "Elastic Compute Cloud"
    assert ws.cell(row=2, column=3).value == 100.0


def test_sheet_account_id_parses_prefix_ignoring_alias_text():
    assert bbs_module._sheet_account_id("100000000001-core") == "100000000001"
    assert bbs_module._sheet_account_id("100000000030-app-c-workloa") == "100000000030"
    assert bbs_module._sheet_account_id("not-an-account-sheet") is None


def test_load_template_bytes_returns_nonempty_xlsx(tmp_path, monkeypatch):
    template_bytes = _make_synthetic_template()
    fake_path = tmp_path / "template.xlsx"
    fake_path.write_bytes(template_bytes)
    monkeypatch.setattr(bbs_module, "TEMPLATE_PATH", str(fake_path))
    loaded = bbs_module.load_template_bytes()
    assert loaded == template_bytes
