"""
Tests for app/meraki_monitor.py — "Local Infra Hardware Monitor" (Cisco
Meraki AP monitoring). No real Meraki API calls: httpx.request is
monkeypatched to a fake router that returns canned JSON per path,
mirroring test_escalation.py/test_freshservice_sync.py's httpx-mocking
style. All caches (module-level TTLCache instances) are reset before
every test so no test observes another test's cached data.
"""

from __future__ import annotations

import importlib

import httpx
import pytest


@pytest.fixture
def meraki_module(monkeypatch):
    monkeypatch.setenv("MERAKI_API_KEY", "test-meraki-key")
    monkeypatch.setenv("MERAKI_ORG_ID", "123456")
    from app import meraki_monitor as m
    importlib.reload(m)
    # Fresh caches per test.
    m._CACHE_SHORT.reset_for_tests()
    m._CACHE_LONG.reset_for_tests()
    m._CACHE_FANOUT.reset_for_tests()
    return m


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else []
        self.headers = headers or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self,  # type: ignore[arg-type]
            )


def _is_retry_sequence(value) -> bool:
    """True only for the explicit 'sequence of (status, body, headers)
    responses for successive calls' shape — NOT for an ordinary JSON
    list response body (devices/networks/history rows are themselves
    lists, so `isinstance(value, list)` alone would wrongly match those
    too)."""
    return (
        isinstance(value, list) and len(value) > 0
        and all(isinstance(v, tuple) and len(v) == 3 for v in value)
    )


def _router(routes: dict):
    """routes: {path_substring: json_body} — json_body can itself be a
    list or dict (an ordinary Meraki response shape), OR a list of
    (status, json_body, headers) 3-tuples for simulating successive
    calls to the SAME route returning different responses. The longest
    matching path_substring wins, so e.g. '/networks/N_1/events' is
    preferred over the more generic '/networks' key when both are
    registered. Returns a fake httpx.request(method, url, **kwargs)
    callable."""
    call_log = []

    def _fake_request(method, url, headers=None, params=None, timeout=None):
        call_log.append(url)
        matches = [key for key in routes if key in url]
        if not matches:
            raise AssertionError(f"Unexpected URL in test: {url}")
        key = max(matches, key=len)
        value = routes[key]
        if _is_retry_sequence(value):
            idx = min(len([c for c in call_log if key in c]) - 1, len(value) - 1)
            status, body, hdrs = value[idx]
            return _FakeResponse(status, body, hdrs)
        return _FakeResponse(200, value, {})

    _fake_request.call_log = call_log
    return _fake_request


NETWORKS_BODY = [
    {"id": "N_1", "name": "HKPPLE"},
    {"id": "N_2", "name": "DENC1"},
]

DEVICES_BODY = [
    {
        "serial": "Q2LD-AAAA-BBBB", "name": "HKPPLE-AP001", "networkId": "N_1",
        "tags": ["HKPPLE-AP01"], "model": "MR52", "lanIp": "10.0.0.1", "firmware": "wireless-30-6",
    },
    {
        "serial": "Q2LD-CCCC-DDDD", "name": "DENC1-AP001", "networkId": "N_2",
        "tags": ["DENC1-AP01", "recently-added"], "model": "MR28", "lanIp": "10.0.0.2", "firmware": "wireless-30-6",
    },
]

STATUSES_BODY = [
    {"serial": "Q2LD-AAAA-BBBB", "status": "online"},
    {"serial": "Q2LD-CCCC-DDDD", "status": "offline"},
]


class TestApiKeyResolution:
    def test_env_var_override_used_directly(self, meraki_module, monkeypatch):
        monkeypatch.setenv("MERAKI_API_KEY", "abc123")
        assert meraki_module._get_api_key() == "abc123"

    def test_falls_back_to_secrets_manager_when_env_var_missing(self, meraki_module, monkeypatch):
        monkeypatch.delenv("MERAKI_API_KEY", raising=False)
        monkeypatch.setattr(meraki_module, "_get_secret", lambda name: "secret-key-value")
        assert meraki_module._get_api_key() == "secret-key-value"

    def test_secrets_manager_failure_raises_meraki_error(self, meraki_module, monkeypatch):
        monkeypatch.delenv("MERAKI_API_KEY", raising=False)

        def _boom(name):
            raise RuntimeError("no access")

        monkeypatch.setattr(meraki_module, "_get_secret", _boom)
        with pytest.raises(meraki_module.MerakiError):
            meraki_module._get_api_key()

    def test_json_wrapped_secret_extracts_api_key_field(self, meraki_module, monkeypatch):
        monkeypatch.delenv("MERAKI_API_KEY", raising=False)
        monkeypatch.setattr(meraki_module, "_get_secret", lambda name: '{"api_key": "from-json"}')
        assert meraki_module._get_api_key() == "from-json"


class TestPagination:
    def test_parse_next_link_extracts_url(self, meraki_module):
        header = '<https://api.meraki.com/api/v1/foo?startingAfter=X>; rel=next, <https://x>; rel=last'
        assert meraki_module._parse_next_link(header) == "https://api.meraki.com/api/v1/foo?startingAfter=X"

    def test_parse_next_link_returns_none_when_no_next(self, meraki_module):
        assert meraki_module._parse_next_link('<https://x>; rel=last') is None
        assert meraki_module._parse_next_link(None) is None

    def test_get_paginated_follows_link_header_until_exhausted(self, meraki_module, monkeypatch):
        page1 = _FakeResponse(200, [{"id": 1}], {"Link": "<https://api.meraki.com/api/v1/next>; rel=next"})
        page2 = _FakeResponse(200, [{"id": 2}], {})
        responses = [page1, page2]

        def _fake_request(method, url, headers=None, params=None, timeout=None):
            return responses.pop(0)

        monkeypatch.setattr(httpx, "request", _fake_request)
        items = meraki_module._get_paginated("/organizations/123456/devices", {"perPage": 1000})
        assert items == [{"id": 1}, {"id": 2}]


class TestRetryBackoff:
    def test_429_is_retried_then_succeeds(self, meraki_module, monkeypatch):
        responses = [
            _FakeResponse(429, {}, {"Retry-After": "0"}),
            _FakeResponse(200, {"ok": True}, {}),
        ]

        def _fake_request(method, url, headers=None, params=None, timeout=None):
            return responses.pop(0)

        monkeypatch.setattr(httpx, "request", _fake_request)
        monkeypatch.setattr(meraki_module.time, "sleep", lambda s: None)
        body, _ = meraki_module._get_json("/organizations/123456/devices/statuses/overview")
        assert body == {"ok": True}

    def test_exhausting_retries_raises_meraki_error(self, meraki_module, monkeypatch):
        monkeypatch.setattr(
            httpx, "request", lambda *a, **k: _FakeResponse(500, {}, {}),
        )
        monkeypatch.setattr(meraki_module.time, "sleep", lambda s: None)
        with pytest.raises(meraki_module.MerakiError):
            meraki_module._get_json("/organizations/123456/devices/statuses/overview")

    def test_network_error_is_retried_then_raises_meraki_error_after_max_attempts(self, meraki_module, monkeypatch):
        def _raise_transport_error(*a, **k):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "request", _raise_transport_error)
        monkeypatch.setattr(meraki_module.time, "sleep", lambda s: None)
        with pytest.raises(meraki_module.MerakiError):
            meraki_module._get_json("/organizations/123456/devices/statuses/overview")


class TestAccessPoints:
    def test_list_access_points_joins_devices_statuses_and_site_names(self, meraki_module, monkeypatch):
        fake = _router({
            "/networks": NETWORKS_BODY,
            "/devices/statuses": STATUSES_BODY,
            "/devices": DEVICES_BODY,
        })
        monkeypatch.setattr(httpx, "request", fake)

        result = meraki_module.list_access_points()
        aps = {ap["serial"]: ap for ap in result["access_points"]}

        assert result["count"] == 2
        assert aps["Q2LD-AAAA-BBBB"]["site"] == "HKPPLE"
        assert aps["Q2LD-AAAA-BBBB"]["status"] == "online"
        assert aps["Q2LD-AAAA-BBBB"]["tags"] == ["HKPPLE-AP01"]
        assert aps["Q2LD-CCCC-DDDD"]["site"] == "DENC1"
        assert aps["Q2LD-CCCC-DDDD"]["status"] == "offline"

    def test_unknown_status_defaults_when_device_missing_from_statuses_list(self, meraki_module, monkeypatch):
        fake = _router({
            "/networks": NETWORKS_BODY,
            "/devices/statuses": [],  # no status rows at all
            "/devices": DEVICES_BODY,
        })
        monkeypatch.setattr(httpx, "request", fake)
        result = meraki_module.list_access_points()
        for ap in result["access_points"]:
            assert ap["status"] == "unknown"


class TestDevicesOverview:
    def test_maps_by_status_counts(self, meraki_module, monkeypatch):
        body = {"counts": {"byStatus": {"online": 382, "alerting": 19, "offline": 2, "dormant": 97}}}
        monkeypatch.setattr(httpx, "request", _router({"/devices/statuses/overview": body}))
        overview = meraki_module.get_devices_overview()
        assert overview["online"] == 382
        assert overview["alerting"] == 19
        assert overview["offline"] == 2
        assert overview["dormant"] == 97

    def test_missing_fields_default_to_zero(self, meraki_module, monkeypatch):
        monkeypatch.setattr(httpx, "request", _router({"/devices/statuses/overview": {"counts": {}}}))
        overview = meraki_module.get_devices_overview()
        assert overview["online"] == 0
        assert overview["alerting"] == 0
        assert overview["offline"] == 0
        assert overview["dormant"] == 0


class TestChannelUtilizationByNetwork:
    def test_averages_percentage_across_bands(self, meraki_module, monkeypatch):
        body = [
            {
                "network": {"id": "N_1", "name": "HKPPLE"},
                "byBand": [
                    {"band": "2.4", "wifi": {"percentage": 20.0}, "nonWifi": {"percentage": 10.0}, "total": {"percentage": 30.0}},
                    {"band": "5", "wifi": {"percentage": 40.0}, "nonWifi": {"percentage": 0.0}, "total": {"percentage": 40.0}},
                ],
            },
        ]
        monkeypatch.setattr(httpx, "request", _router({"/channelUtilization/byNetwork": body, "/networks": NETWORKS_BODY}))
        result = meraki_module.get_channel_utilization_by_network()
        row = result["networks"][0]
        assert row["wifi_pct"] == pytest.approx(30.0)
        assert row["non_wifi_pct"] == pytest.approx(5.0)
        assert row["total_pct"] == pytest.approx(35.0)

    def test_empty_by_band_returns_none_not_zero(self, meraki_module, monkeypatch):
        body = [{"network": {"id": "N_1", "name": "HKPPLE"}, "byBand": []}]
        monkeypatch.setattr(httpx, "request", _router({"/channelUtilization/byNetwork": body, "/networks": NETWORKS_BODY}))
        result = meraki_module.get_channel_utilization_by_network()
        row = result["networks"][0]
        assert row["wifi_pct"] is None
        assert row["total_pct"] is None


class TestChannelUtilizationByDevice:
    def test_joins_device_name_and_site_from_ap_inventory(self, meraki_module, monkeypatch):
        cu_body = [
            {
                "serial": "Q2LD-AAAA-BBBB", "network": {"id": "N_1"},
                "byBand": [{"band": "2.4", "wifi": {"percentage": 50.0}, "nonWifi": {"percentage": 5.0}, "total": {"percentage": 55.0}}],
            },
        ]
        fake = _router({
            "/networks": NETWORKS_BODY,
            "/devices/statuses": STATUSES_BODY,
            "/devices": DEVICES_BODY,
            "/channelUtilization/byDevice": cu_body,
        })
        monkeypatch.setattr(httpx, "request", fake)
        result = meraki_module.get_channel_utilization_by_device()
        row = result["devices"][0]
        assert row["name"] == "HKPPLE-AP001"
        assert row["site"] == "HKPPLE"
        assert row["wifi_pct"] == pytest.approx(50.0)


class TestChannelUtilizationChange:
    def test_by_network_computes_delta_between_first_and_last_bucket(self, meraki_module, monkeypatch):
        history_body = [
            {"startTs": "2026-01-01T00:00:00Z", "network": {"id": "N_1"}, "byBand": [{"band": "2.4", "total": {"percentage": 20.0}}]},
            {"startTs": "2026-01-01T01:00:00Z", "network": {"id": "N_1"}, "byBand": [{"band": "2.4", "total": {"percentage": 50.0}}]},
        ]
        fake = _router({
            "/networks": NETWORKS_BODY,
            "/channelUtilization/history/byNetwork/byInterval": history_body,
        })
        monkeypatch.setattr(httpx, "request", fake)
        result = meraki_module.get_channel_utilization_change_by_network()
        row = result["networks"][0]
        assert row["from_pct"] == pytest.approx(20.0)
        assert row["to_pct"] == pytest.approx(50.0)
        assert row["change_pct"] == pytest.approx(30.0)

    def test_single_data_point_is_excluded_from_results(self, meraki_module, monkeypatch):
        history_body = [
            {"startTs": "2026-01-01T00:00:00Z", "network": {"id": "N_1"}, "byBand": [{"band": "2.4", "total": {"percentage": 20.0}}]},
        ]
        fake = _router({
            "/networks": NETWORKS_BODY,
            "/channelUtilization/history/byNetwork/byInterval": history_body,
        })
        monkeypatch.setattr(httpx, "request", fake)
        result = meraki_module.get_channel_utilization_change_by_network()
        assert result["networks"] == []

    def test_by_device_computes_delta_and_sorts_by_absolute_change_desc(self, meraki_module, monkeypatch):
        history_body = [
            {"startTs": "t0", "serial": "SER-1", "byBand": [{"band": "2.4", "total": {"percentage": 10.0}}]},
            {"startTs": "t1", "serial": "SER-1", "byBand": [{"band": "2.4", "total": {"percentage": 15.0}}]},
            {"startTs": "t0", "serial": "SER-2", "byBand": [{"band": "2.4", "total": {"percentage": 90.0}}]},
            {"startTs": "t1", "serial": "SER-2", "byBand": [{"band": "2.4", "total": {"percentage": 10.0}}]},
        ]
        fake = _router({
            "/networks": NETWORKS_BODY,
            "/devices/statuses": STATUSES_BODY,
            "/devices": DEVICES_BODY,
            "/channelUtilization/history/byDevice/byInterval": history_body,
        })
        monkeypatch.setattr(httpx, "request", fake)
        result = meraki_module.get_channel_utilization_change_by_device()
        # SER-2 dropped 80 points (biggest absolute swing) -> sorted first.
        assert result["devices"][0]["serial"] == "SER-2"
        assert result["devices"][0]["change_pct"] == pytest.approx(-80.0)


class TestPacketLossByNetwork:
    def test_computes_combined_loss_percentage_and_sorts_desc(self, meraki_module, monkeypatch):
        body = [
            {
                "network": {"id": "N_1", "name": "HKPPLE"},
                "downstream": {"total": 1000, "lost": 10, "lossPercentage": 1.0},
                "upstream": {"total": 1000, "lost": 10, "lossPercentage": 1.0},
            },
            {
                "network": {"id": "N_2", "name": "DENC1"},
                "downstream": {"total": 1000, "lost": 200, "lossPercentage": 20.0},
                "upstream": {"total": 1000, "lost": 200, "lossPercentage": 20.0},
            },
        ]
        monkeypatch.setattr(httpx, "request", _router({"/packetLoss/byNetwork": body, "/networks": NETWORKS_BODY}))
        result = meraki_module.get_packet_loss_by_network()
        assert result["networks"][0]["network_id"] == "N_2"
        assert result["networks"][0]["loss_pct"] == pytest.approx(20.0)
        assert result["networks"][1]["loss_pct"] == pytest.approx(1.0)

    def test_zero_total_packets_reports_zero_loss_not_division_error(self, meraki_module, monkeypatch):
        body = [{"network": {"id": "N_1", "name": "HKPPLE"}, "downstream": {"total": 0, "lost": 0}, "upstream": {"total": 0, "lost": 0}}]
        monkeypatch.setattr(httpx, "request", _router({"/packetLoss/byNetwork": body, "/networks": NETWORKS_BODY}))
        result = meraki_module.get_packet_loss_by_network()
        assert result["networks"][0]["loss_pct"] == 0.0


class TestTopNetworksAndDevicesByClients:
    def test_top_networks_by_clients_sorted_desc(self, meraki_module, monkeypatch):
        body = [
            {"networkId": "N_1", "name": "HKPPLE", "clients": {"counts": {"total": 50}}, "statuses": {"overall": "online"}},
            {"networkId": "N_2", "name": "DENC1", "clients": {"counts": {"total": 500}}, "statuses": {"overall": "online"}},
        ]
        monkeypatch.setattr(httpx, "request", _router({"/summary/top/networks/byStatus": body}))
        result = meraki_module.get_top_networks_by_clients()
        assert result["networks"][0]["network_id"] == "N_2"
        assert result["networks"][0]["client_count"] == 500

    def test_top_devices_by_clients_sorted_desc(self, meraki_module, monkeypatch):
        body = [
            {"serial": "S1", "name": "AP1", "network": {"name": "HKPPLE", "id": "N_1"}, "clients": {"counts": {"total": 10}}},
            {"serial": "S2", "name": "AP2", "network": {"name": "DENC1", "id": "N_2"}, "clients": {"counts": {"total": 900}}},
        ]
        monkeypatch.setattr(httpx, "request", _router({"/summary/top/devices/byUsage": body}))
        result = meraki_module.get_top_devices_by_clients()
        assert result["devices"][0]["serial"] == "S2"
        assert result["devices"][0]["client_count"] == 900


class TestTopLatencyNetworks:
    def test_fans_out_over_top_networks_only_and_averages_traffic_classes(self, meraki_module, monkeypatch):
        top_networks_body = [
            {"networkId": "N_1", "name": "HKPPLE", "clients": {"counts": {"total": 500}}, "statuses": {"overall": "online"}},
            {"networkId": "N_2", "name": "DENC1", "clients": {"counts": {"total": 10}}, "statuses": {"overall": "online"}},
        ]
        latency_body = {
            "backgroundTraffic": {"avg": 10.0},
            "bestEffortTraffic": {"avg": 20.0},
            "videoTraffic": {"avg": None},
            "voiceTraffic": {"avg": None},
        }

        def _fake_request(method, url, headers=None, params=None, timeout=None):
            if "summary/top/networks/byStatus" in url:
                return _FakeResponse(200, top_networks_body, {})
            if "/wireless/latencyStats" in url:
                return _FakeResponse(200, latency_body, {})
            raise AssertionError(f"unexpected url {url}")

        monkeypatch.setattr(httpx, "request", _fake_request)
        result = meraki_module.get_top_latency_networks()
        assert len(result["networks"]) == 2
        for row in result["networks"]:
            assert row["avg_latency_ms"] == pytest.approx(15.0)
        assert "note" in result

    def test_network_that_fails_latency_fetch_is_excluded_not_crashing(self, meraki_module, monkeypatch):
        top_networks_body = [
            {"networkId": "N_1", "name": "HKPPLE", "clients": {"counts": {"total": 500}}, "statuses": {"overall": "online"}},
        ]

        def _fake_request(method, url, headers=None, params=None, timeout=None):
            if "summary/top/networks/byStatus" in url:
                return _FakeResponse(200, top_networks_body, {})
            if "/wireless/latencyStats" in url:
                return _FakeResponse(500, {}, {})
            raise AssertionError(f"unexpected url {url}")

        monkeypatch.setattr(httpx, "request", _fake_request)
        monkeypatch.setattr(meraki_module.time, "sleep", lambda s: None)
        result = meraki_module.get_top_latency_networks()
        assert result["networks"] == []


class TestMerakiEvents:
    def test_scoped_to_one_network_returns_mapped_rows(self, meraki_module, monkeypatch):
        events_body = {
            "events": [
                {
                    "occurredAt": "2026-09-16T04:00:00Z", "deviceName": "HKPPLE-AP001", "category": "80211",
                    "ssidName": "PPL-Guest", "description": "802.11 deauth", "clientMac": "aa:bb:cc:dd:ee:ff",
                },
            ],
        }
        fake = _router({
            "/networks": NETWORKS_BODY,
            "/networks/N_1/events": events_body,
        })
        monkeypatch.setattr(httpx, "request", fake)
        result = meraki_module.get_meraki_events(network_id="N_1")
        row = result["events"][0]
        assert row["ap_name"] == "HKPPLE-AP001"
        assert row["category"] == "80211"
        assert row["ssid"] == "PPL-Guest"
        assert row["description"] == "802.11 deauth"

    def test_no_network_id_fans_out_over_top_networks_and_merges_sorted(self, meraki_module, monkeypatch):
        top_networks_body = [
            {"networkId": "N_1", "name": "HKPPLE", "clients": {"counts": {"total": 500}}, "statuses": {"overall": "online"}},
            {"networkId": "N_2", "name": "DENC1", "clients": {"counts": {"total": 100}}, "statuses": {"overall": "online"}},
        ]
        events_n1 = {"events": [{"occurredAt": "2026-09-16T03:00:00Z", "deviceName": "AP1", "category": "80211", "description": "old"}]}
        events_n2 = {"events": [{"occurredAt": "2026-09-16T05:00:00Z", "deviceName": "AP2", "category": "wpa_auth", "description": "new"}]}

        def _fake_request(method, url, headers=None, params=None, timeout=None):
            if "summary/top/networks/byStatus" in url:
                return _FakeResponse(200, top_networks_body, {})
            if "/networks/N_1/events" in url:
                return _FakeResponse(200, events_n1, {})
            if "/networks/N_2/events" in url:
                return _FakeResponse(200, events_n2, {})
            if url.endswith("/networks") or "/networks?" in url:
                return _FakeResponse(200, NETWORKS_BODY, {})
            raise AssertionError(f"unexpected url {url}")

        monkeypatch.setattr(httpx, "request", _fake_request)
        result = meraki_module.get_meraki_events()
        # Most-recent-first across both networks.
        assert result["events"][0]["description"] == "new"
        assert result["events"][1]["description"] == "old"
        assert "note" in result


class TestNetworks:
    def test_list_networks_returns_id_name_pairs(self, meraki_module, monkeypatch):
        monkeypatch.setattr(httpx, "request", _router({"/networks": NETWORKS_BODY}))
        result = meraki_module.list_networks()
        assert {"id": "N_1", "name": "HKPPLE"} in result["networks"]
        assert {"id": "N_2", "name": "DENC1"} in result["networks"]
