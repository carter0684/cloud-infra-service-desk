"""Tests for multi-subscription Azure support, the subscription connection test
and the onboarding guide (.kiro/specs/cloud-account-onboarding, task 5)."""

import pytest

from app import azure_monitor as az
from app import cloud_account_store as store
from app.cache_utils import TTLCache

SUB1 = "11111111-2222-3333-4444-555555555555"
SUB2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SUB3 = "99999999-8888-7777-6666-555555555555"
CREDS = {
    "tenant_id": "00000000-0000-0000-0000-000000000000", "client_id": "cccccccc-0000-1111-2222-333333333333",
    "client_secret": "SUPERSECRET-VALUE", "subscription_id": SUB1,
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(az, "_CACHE", TTLCache(ttl_seconds=300))
    monkeypatch.setattr(az, "_get_credentials", lambda: dict(CREDS))
    # Never let a test sign in to Azure AD for real.
    monkeypatch.setattr(az, "_get_token", lambda force_refresh=False: "test-token")


# ── test_azure_subscription ─────────────────────────────────────────────────

def _arm_returns(monkeypatch, body=None, error=None):
    calls = []

    def fake(path, api_version, extra_params=None):
        calls.append((path, api_version))
        if error:
            raise error
        return body

    monkeypatch.setattr(az, "_arm_get", fake)
    return calls


def test_enabled_subscription_connects(monkeypatch):
    calls = _arm_returns(monkeypatch, {"subscriptionId": SUB2, "state": "Enabled"})
    assert az.test_azure_subscription(SUB2) == {"ok": True, "code": "ok", "message": "Connected."}
    assert calls == [(f"/subscriptions/{SUB2}", "2022-12-01")]


def test_subscription_id_comparison_ignores_case(monkeypatch):
    _arm_returns(monkeypatch, {"subscriptionId": SUB2.upper(), "state": "Enabled"})
    assert az.test_azure_subscription(SUB2)["ok"] is True


@pytest.mark.parametrize("state", ["Disabled", "Warned", "PastDue", "Deleted", ""])
def test_a_subscription_that_is_not_enabled_is_rejected(monkeypatch, state):
    _arm_returns(monkeypatch, {"subscriptionId": SUB2, "state": state})
    r = az.test_azure_subscription(SUB2)
    assert (r["ok"], r["code"]) == (False, "disabled")


def test_a_different_subscription_in_the_answer_is_rejected(monkeypatch):
    _arm_returns(monkeypatch, {"subscriptionId": SUB3, "state": "Enabled"})
    assert az.test_azure_subscription(SUB2)["code"] == "invalid_id"


@pytest.mark.parametrize("status", [403, 404])
def test_not_granted_is_explained_without_raw_azure_text(monkeypatch, status):
    _arm_returns(monkeypatch, error=az.AzureError(
        f"Azure Resource Manager request failed for /subscriptions/{SUB2} ({status}): raw-azure-detail-123"))
    r = az.test_azure_subscription(SUB2)
    assert (r["ok"], r["code"]) == (False, "not_granted")
    assert "Reader" in r["message"] and "raw-azure-detail" not in r["message"]


def test_token_failures_are_reported_as_portal_credential_problems(monkeypatch):
    for text in ("Azure AD token request failed (401): AADSTS7000215: Invalid client secret SUPERSECRET-VALUE",
                 "Could not load Azure API credentials: boom", "Azure API credentials secret is missing one of x"):
        _arm_returns(monkeypatch, error=az.AzureError(text))
        r = az.test_azure_subscription(SUB2)
        assert r["code"] == "portal_credentials"
        assert "SUPERSECRET" not in r["message"] and "AADSTS" not in r["message"]


def test_network_errors(monkeypatch):
    _arm_returns(monkeypatch, error=az.AzureError("Could not reach Azure Resource Manager (/x): timed out"))
    assert az.test_azure_subscription(SUB2)["code"] == "network"


def test_other_azure_errors_show_only_the_status(monkeypatch):
    _arm_returns(monkeypatch, error=az.AzureError("Azure Resource Manager request failed for /x (500): internal detail"))
    r = az.test_azure_subscription(SUB2)
    assert r["code"] == "azure_error" and "500" in r["message"] and "internal detail" not in r["message"]


def test_unexpected_errors_never_raise(monkeypatch):
    _arm_returns(monkeypatch, error=RuntimeError("kaboom"))
    r = az.test_azure_subscription(SUB2)
    assert (r["ok"], r["code"]) == (False, "unexpected") and "kaboom" not in r["message"]


@pytest.mark.parametrize("bad", ["", "nope", "../../etc", SUB2 + "x", None])
def test_invalid_ids_are_rejected_without_calling_azure(monkeypatch, bad):
    calls = _arm_returns(monkeypatch, {"state": "Enabled"})
    assert az.test_azure_subscription(bad)["code"] == "invalid_id"
    assert calls == []


# ── guide ───────────────────────────────────────────────────────────────────

def test_guide_gives_the_application_id_and_a_reader_command_but_never_the_secret():
    g = az.azure_guide(SUB2)
    assert g["application_id"] == CREDS["client_id"] and g["role"] == "Reader"
    assert g["command"] == (
        f"az role assignment create --assignee {CREDS['client_id']} --role Reader --scope /subscriptions/{SUB2}"
    )
    assert "SUPERSECRET" not in repr(g)


def test_guide_without_a_subscription_shows_a_placeholder():
    assert "/subscriptions/<SUBSCRIPTION_ID>" in az.azure_guide()["command"]


@pytest.mark.parametrize("bad", ["x; rm -rf /", "a b", SUB2 + " --role Owner", "../x"])
def test_guide_refuses_anything_that_is_not_a_guid(bad):
    with pytest.raises(ValueError):
        az.azure_guide(bad)


# ── subscription override ───────────────────────────────────────────────────

def test_current_subscription_defaults_to_the_configured_one():
    assert az._current_subscription_id() == SUB1


def test_override_applies_inside_and_is_restored_after_even_on_error():
    seen = []

    def inside():
        seen.append(az._current_subscription_id())
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        az._with_subscription(SUB2, inside)
    assert seen == [SUB2]
    assert az._current_subscription_id() == SUB1


def test_arm_list_calls_use_the_overridden_subscription(monkeypatch):
    paths = []

    def fake_get(path, api_version, extra_params=None):
        paths.append(path)
        return {"value": [{"name": "x"}]}

    monkeypatch.setattr(az, "_arm_get", fake_get)
    az._arm_list_all_pages("/providers/Microsoft.Compute/virtualMachines", "2024-07-01")
    az._with_subscription(SUB2, lambda: az._arm_list_all_pages("/providers/Microsoft.Compute/virtualMachines", "2024-07-01"))
    az._with_subscription(SUB2, lambda: az._arm_list_all_pages_with_filter("/x", "1", "f"))
    assert paths == [
        f"/subscriptions/{SUB1}/providers/Microsoft.Compute/virtualMachines",
        f"/subscriptions/{SUB2}/providers/Microsoft.Compute/virtualMachines",
        f"/subscriptions/{SUB2}/x",
    ]


# ── list_connected ──────────────────────────────────────────────────────────

class Fetch:
    """Stand-in for a _fetch_* function: returns rows that name the subscription it ran for."""

    def __init__(self, rows_by_sub, headers=("Name", "Location"), details=False, fail=()):
        self.rows_by_sub = rows_by_sub
        self.headers = list(headers)
        self.details = details
        self.fail = set(fail)
        self.calls = []

    def __call__(self):
        sub = az._current_subscription_id()
        self.calls.append(sub)
        if sub in self.fail:
            raise az.AzureError(f"Azure Resource Manager request failed for /subscriptions/{sub} (500): raw-detail")
        rows = [list(r) for r in self.rows_by_sub[sub]]
        out = {"headers": self.headers, "rows": rows, "count": len(rows)}
        if self.details:
            out["row_details"] = [{"of": r[0], "sub": sub} for r in rows]
        return out


def _subs(monkeypatch, subs):
    monkeypatch.setattr(az, "_connected_subscriptions", lambda: subs)


def test_no_registry_subscriptions_runs_the_original_list_function(monkeypatch):
    sentinel = {"headers": ["Name"], "rows": [["vm1"]], "count": 1}
    monkeypatch.setattr(az, "list_vms", lambda force_refresh=False: sentinel)
    _subs(monkeypatch, [])
    assert az.list_connected("list_vms") is sentinel


def test_only_the_default_subscription_runs_the_original_function_unchanged(monkeypatch):
    sentinel = {"headers": ["Name"], "rows": [["vm1"]], "count": 1}
    seen = []
    monkeypatch.setattr(az, "list_vms", lambda force_refresh=False: (seen.append(force_refresh), sentinel)[1])
    _subs(monkeypatch, [(SUB1.upper(), "Azure (default subscription)")])
    out = az.list_connected("list_vms", force_refresh=True)
    assert out is sentinel and "Subscription" not in out["headers"]  # byte-identical to today
    assert seen == [True]


def test_registry_read_failure_falls_back_to_the_original_function(monkeypatch):
    sentinel = {"headers": ["Name"], "rows": [], "count": 0}
    monkeypatch.setattr(az, "list_vms", lambda force_refresh=False: sentinel)

    def boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(az, "_connected_subscriptions", boom)
    assert az.list_connected("list_vms") is sentinel


def test_a_single_non_default_subscription_is_queried_without_the_extra_column(monkeypatch):
    f = Fetch({SUB2: [["vm-b", "eastus"]]})
    monkeypatch.setattr(az, "_fetch_vms", f)
    _subs(monkeypatch, [(SUB2, "Azure Dev")])
    out = az.list_connected("list_vms")
    assert out["headers"] == ["Name", "Location"] and out["rows"] == [["vm-b", "eastus"]]
    assert f.calls == [SUB2]  # never touched the default subscription


def test_two_subscriptions_are_merged_with_a_subscription_column(monkeypatch):
    f = Fetch({SUB1: [["vm-z", "eastus"], ["vm-a", "westus"]], SUB2: [["vm-m", "eastus"]]})
    monkeypatch.setattr(az, "_fetch_vms", f)
    _subs(monkeypatch, [(SUB1, "Azure Prod"), (SUB2, "Azure Dev")])
    out = az.list_connected("list_vms")
    assert out["headers"] == ["Name", "Location", "Subscription"]
    assert out["rows"] == [["vm-a", "westus", "Azure Prod"], ["vm-m", "eastus", "Azure Dev"], ["vm-z", "eastus", "Azure Prod"]]
    assert out["count"] == 3 and "errors" not in out
    assert sorted(f.calls) == sorted([SUB1, SUB2])  # each ran under its own subscription


def test_row_details_stay_aligned_with_their_rows_after_merging(monkeypatch):
    f = Fetch({SUB1: [["nsg-b", "x"]], SUB2: [["nsg-a", "y"]]}, details=True)
    monkeypatch.setattr(az, "_fetch_nsgs", f)
    _subs(monkeypatch, [(SUB1, "Prod"), (SUB2, "Dev")])
    out = az.list_connected("list_nsgs")
    assert [r[0] for r in out["rows"]] == ["nsg-a", "nsg-b"]
    assert out["row_details"] == [{"of": "nsg-a", "sub": SUB2}, {"of": "nsg-b", "sub": SUB1}]


def test_results_are_cached_per_subscription_and_force_refresh_bypasses(monkeypatch):
    f = Fetch({SUB1: [["a", "x"]], SUB2: [["b", "y"]]})
    monkeypatch.setattr(az, "_fetch_vms", f)
    _subs(monkeypatch, [(SUB1, "Prod"), (SUB2, "Dev")])
    az.list_connected("list_vms")
    az.list_connected("list_vms")
    assert len(f.calls) == 2  # second call served from the cache
    az.list_connected("list_vms", force_refresh=True)
    assert len(f.calls) == 4


def test_one_failing_subscription_does_not_hide_the_other(monkeypatch):
    f = Fetch({SUB1: [["a", "x"]], SUB2: [["b", "y"]]}, fail=[SUB2])
    monkeypatch.setattr(az, "_fetch_vms", f)
    _subs(monkeypatch, [(SUB1, "Prod"), (SUB2, "Dev")])
    out = az.list_connected("list_vms")
    assert [r[0] for r in out["rows"]] == ["a"]
    assert out["errors"] == [{"subscription": "Dev", "message": "Could not read this subscription."}]
    assert "raw-detail" not in repr(out)


def test_all_subscriptions_failing_raises(monkeypatch):
    f = Fetch({SUB1: [], SUB2: []}, fail=[SUB1, SUB2])
    monkeypatch.setattr(az, "_fetch_vms", f)
    _subs(monkeypatch, [(SUB1, "Prod"), (SUB2, "Dev")])
    with pytest.raises(az.AzureError):
        az.list_connected("list_vms")


def test_unknown_list_function_is_refused(monkeypatch):
    _subs(monkeypatch, [(SUB1, "Prod"), (SUB2, "Dev")])
    with pytest.raises(az.AzureError):
        az.list_connected("list_everything")


def test_connected_subscriptions_come_from_the_registry_visibility_rule(monkeypatch):
    entries = [
        {"identifier": SUB1, "display_name": "Prod", "provider": "azure"},
        {"identifier": SUB2, "display_name": "Dev", "provider": "azure"},
    ]
    monkeypatch.setattr(store, "active_accounts", lambda provider="aws", scope=None: entries if provider == "azure" else [])
    assert az._connected_subscriptions() == [(SUB1, "Prod"), (SUB2, "Dev")]
