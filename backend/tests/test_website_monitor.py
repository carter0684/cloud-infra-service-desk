"""
Tests for app/website_monitor.py and its /api/website-monitoring/* routes
(sidebar: Monitoring > Website Monitoring).

S3 is mocked with moto - no real AWS access. The central bucket lives in
ap-southeast-1 like the real one.

Access rules covered here:
  * every logged-in user may read the status grid (and sees the tab even with
    no User Group memberships);
  * only admins may add or remove a monitored website.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

BUCKET = "webmon-central-test"
REGION = "ap-southeast-1"

ADMIN = "admin@example.com"
MEMBER = "member@example.com"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def s3(aws_credentials, monkeypatch):
    monkeypatch.setenv("WEBSITE_MONITOR_BUCKET", BUCKET)
    monkeypatch.setenv("WEBSITE_MONITOR_REGION", REGION)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        from app import website_monitor

        website_monitor.reset_for_tests()
        yield client
        website_monitor.reset_for_tests()


@pytest.fixture
def wm(s3):
    from app import website_monitor

    return website_monitor


def _put_json(s3, key, obj):
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(obj).encode())


def _read_config(s3):
    return json.loads(s3.get_object(Bucket=BUCKET, Key="config/sites.json")["Body"].read())


# ── URL validation ───────────────────────────────────────────────────────

class TestNormalizeUrl:
    def test_adds_https_and_root_path(self, wm):
        assert wm.normalize_url("WWW.Example.com") == "https://www.example.com/"

    def test_keeps_path_and_query_and_drops_fragment_and_default_port(self, wm):
        assert wm.normalize_url("https://example.com:443/a/b?x=1#frag") == "https://example.com/a/b?x=1"

    def test_keeps_non_default_port(self, wm):
        assert wm.normalize_url("http://example.com:8080/") == "http://example.com:8080/"

    @pytest.mark.parametrize("bad", [
        "", "   ", "ftp://example.com/", "javascript:alert(1)", "file:///etc/passwd",
        "https://user:pw@example.com/", "https://localhost/", "https://intranet/",
        "https://printer.local/", "https://127.0.0.1/", "https://10.0.0.5/",
        "https://169.254.169.254/latest/meta-data/", "https://[::1]/", "https://127.1/",
        "https://exa mple.com/", "https://example.com:99999/", "https://-bad-.com/",
    ])
    def test_rejects_unsafe_or_invalid(self, wm, bad):
        with pytest.raises(wm.SiteValidationError):
            wm.normalize_url(bad)

    def test_allows_public_ip(self, wm):
        assert wm.normalize_url("https://1.1.1.1/") == "https://1.1.1.1/"


# ── Site list editing ────────────────────────────────────────────────────

class TestAddRemove:
    def test_add_bootstraps_config_with_default_regions(self, wm, s3):
        site = wm.add_site("Example Site A", "https://www.example-site-a.com/", ADMIN)
        assert site["id"] == "example-site-a-com"
        assert site["addedBy"] == ADMIN
        cfg = _read_config(s3)
        assert [s["id"] for s in cfg["sites"]] == ["example-site-a-com"]
        assert [r["key"] for r in cfg["regions"]] == ["us", "uk", "sg", "in", "jp"]
        assert cfg["intervalMinutes"] == 5
        assert cfg["updatedBy"] == ADMIN

    def test_add_preserves_existing_regions_and_interval(self, wm, s3):
        _put_json(s3, "config/sites.json", {
            "intervalMinutes": 10,
            "regions": [{"key": "sg", "label": "Singapore"}],
            "sites": [],
        })
        wm.add_site("A", "https://a.example.com/", ADMIN)
        cfg = _read_config(s3)
        assert cfg["intervalMinutes"] == 10
        assert cfg["regions"] == [{"key": "sg", "label": "Singapore"}]

    def test_duplicate_url_is_rejected_even_if_written_differently(self, wm):
        wm.add_site("A", "https://example.com/", ADMIN)
        with pytest.raises(wm.SiteConflictError):
            wm.add_site("B", "EXAMPLE.com", ADMIN)

    def test_same_host_different_path_gets_unique_id(self, wm):
        a = wm.add_site("Home", "https://example.com/", ADMIN)
        b = wm.add_site("Shop", "https://example.com/shop", ADMIN)
        assert a["id"] != b["id"]

    def test_name_is_required_and_cleaned(self, wm):
        with pytest.raises(wm.SiteValidationError):
            wm.add_site("   ", "https://example.com/", ADMIN)
        site = wm.add_site("  My \x00  Site ", "https://example.com/", ADMIN)
        assert site["name"] == "My Site"

    def test_site_limit(self, wm, monkeypatch):
        monkeypatch.setattr(wm, "MAX_SITES", 2)
        wm.add_site("A", "https://a.example.com/", ADMIN)
        wm.add_site("B", "https://b.example.com/", ADMIN)
        with pytest.raises(wm.SiteValidationError):
            wm.add_site("C", "https://c.example.com/", ADMIN)

    def test_remove_deletes_config_entry_and_stored_objects(self, wm, s3):
        keep = wm.add_site("Keep", "https://keep.example.com/", ADMIN)
        gone = wm.add_site("Gone", "https://gone.example.com/", ADMIN)
        for sid in (keep["id"], gone["id"]):
            s3.put_object(Bucket=BUCKET, Key=f"results/{sid}/us.json", Body=b"{}")
            s3.put_object(Bucket=BUCKET, Key=f"screenshots/{sid}/us.jpg", Body=b"x")

        wm.remove_site(gone["id"], ADMIN)

        assert [s["id"] for s in _read_config(s3)["sites"]] == [keep["id"]]
        keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])}
        assert f"results/{keep['id']}/us.json" in keys
        assert f"screenshots/{keep['id']}/us.jpg" in keys
        assert f"results/{gone['id']}/us.json" not in keys
        assert f"screenshots/{gone['id']}/us.jpg" not in keys

    def test_remove_unknown_or_malformed_id(self, wm):
        with pytest.raises(wm.SiteNotFoundError):
            wm.remove_site("nope", ADMIN)
        with pytest.raises(wm.SiteNotFoundError):
            wm.remove_site("../config", ADMIN)

    def test_not_configured(self, wm, monkeypatch):
        monkeypatch.delenv("WEBSITE_MONITOR_BUCKET")
        with pytest.raises(wm.WebsiteMonitorNotConfigured):
            wm.get_status()


# ── Status grid ──────────────────────────────────────────────────────────

class TestStatus:
    def _setup(self, wm, s3):
        site = wm.add_site("Example", "https://example.com/", ADMIN)
        sid = site["id"]
        now = datetime.now(timezone.utc)
        base = {"siteId": sid, "consecutiveFailures": 0, "lastSuccessAt": _iso(now)}
        _put_json(s3, f"results/{sid}/us.json", {
            **base, "up": True, "statusCode": 200, "responseTimeMs": 345,
            "checkedAt": _iso(now - timedelta(minutes=1)), "screenshotAt": _iso(now),
        })
        _put_json(s3, f"results/{sid}/uk.json", {
            **base, "up": False, "statusCode": 503, "responseTimeMs": 900, "error": "HTTP 503",
            "consecutiveFailures": 3, "checkedAt": _iso(now - timedelta(minutes=2)),
        })
        _put_json(s3, f"results/{sid}/sg.json", {
            **base, "up": True, "statusCode": 200, "responseTimeMs": 100,
            "checkedAt": _iso(now - timedelta(minutes=60)),
        })
        # "in" and "jp" never reported
        return sid

    def test_states_and_fields(self, wm, s3):
        sid = self._setup(wm, s3)
        data = wm.get_status(force_refresh=True)
        assert data["interval_minutes"] == 5
        assert [r["key"] for r in data["regions"]] == ["us", "uk", "sg", "in", "jp"]
        res = data["sites"][0]["results"]

        assert res["us"]["state"] == "up"
        assert res["us"]["status_code"] == 200
        assert res["us"]["response_time_ms"] == 345
        assert f"screenshots/{sid}/us.jpg" in res["us"]["screenshot_url"]
        assert "X-Amz-Signature" in res["us"]["screenshot_url"]
        # Must be signed for the REGIONAL host; the global s3.amazonaws.com host
        # redirects (307) and the signature then breaks (broken image in the UI).
        host = res["us"]["screenshot_url"].split("/")[2]
        assert host == f"{BUCKET}.s3.{REGION}.amazonaws.com"

        assert res["uk"]["state"] == "down"
        assert res["uk"]["status_code"] == 503
        assert res["uk"]["error"] == "HTTP 503"
        assert res["uk"]["screenshot_url"] is None

        assert res["sg"]["state"] == "stale"
        assert res["in"] == {"state": "nodata"}
        assert res["jp"] == {"state": "nodata"}

    def test_slow_but_up_is_flagged_slow_and_never_down(self, wm, s3):
        site = wm.add_site("Example", "https://example.com/", ADMIN)
        sid = site["id"]
        now = datetime.now(timezone.utc)
        recent = _iso(now - timedelta(minutes=1))
        base = {"siteId": sid, "consecutiveFailures": 0, "statusCode": 200, "checkedAt": recent}
        _put_json(s3, f"results/{sid}/us.json", {**base, "up": True, "responseTimeMs": 21799})   # slow, no flag from canary
        _put_json(s3, f"results/{sid}/uk.json", {**base, "up": True, "responseTimeMs": 900, "slow": True})  # canary says slow
        _put_json(s3, f"results/{sid}/sg.json", {**base, "up": True, "responseTimeMs": 900})     # fast
        _put_json(s3, f"results/{sid}/in.json", {**base, "up": False, "statusCode": 503, "responseTimeMs": 30000})  # down, not "slow"
        res = wm.get_status(force_refresh=True)["sites"][0]["results"]
        assert res["us"]["state"] == "up" and res["us"]["slow"] is True
        assert res["uk"]["state"] == "up" and res["uk"]["slow"] is True
        assert res["sg"]["state"] == "up" and res["sg"]["slow"] is False
        assert res["in"]["state"] == "down" and res["in"]["slow"] is False

    def test_empty_list(self, wm):
        data = wm.get_status(force_refresh=True)
        assert data["sites"] == []
        assert len(data["regions"]) == 5

    def test_site_change_shows_up_immediately_despite_cache(self, wm):
        assert wm.get_status()["sites"] == []
        wm.add_site("Example", "https://example.com/", ADMIN)
        assert len(wm.get_status()["sites"]) == 1

    def test_garbage_result_object_is_treated_as_no_data(self, wm, s3):
        site = wm.add_site("Example", "https://example.com/", ADMIN)
        s3.put_object(Bucket=BUCKET, Key=f"results/{site['id']}/us.json", Body=b"not json")
        res = wm.get_status(force_refresh=True)["sites"][0]["results"]
        assert res["us"] == {"state": "nodata"}


# ── HTTP routes + access control ─────────────────────────────────────────

@pytest.fixture
def client(s3, monkeypatch):
    """TestClient against main.app in ENV=local (X-Actor-Email auth), with a
    fixed roster: one admin and one plain member with no User Groups."""
    monkeypatch.setenv("ENV", "local")
    from app import main, user_store

    importlib.reload(main)
    monkeypatch.setattr(user_store, "is_allowed", lambda email: email in {ADMIN, MEMBER})
    monkeypatch.setattr(user_store, "is_admin", lambda email: email == ADMIN)
    monkeypatch.setattr(user_store, "get_user_groups", lambda email: [])
    from app import website_monitor

    website_monitor.reset_for_tests()
    return TestClient(main.app)


def _h(email):
    return {"X-Actor-Email": email}


class TestRoutes:
    def test_unauthenticated_is_rejected(self, client):
        assert client.get("/api/website-monitoring/status").status_code == 401
        assert client.post("/api/website-monitoring/sites", json={"name": "A", "url": "https://a.example.com/"}).status_code == 401

    def test_member_can_read_but_cannot_manage(self, client):
        resp = client.get("/api/website-monitoring/status", headers=_h(MEMBER))
        assert resp.status_code == 200
        assert resp.json()["can_manage"] is False

    def test_admin_can_read_and_manage(self, client):
        resp = client.get("/api/website-monitoring/status", headers=_h(ADMIN))
        assert resp.status_code == 200
        assert resp.json()["can_manage"] is True

    def test_member_cannot_add_or_remove(self, client, wm):
        site = wm.add_site("A", "https://a.example.com/", ADMIN)
        add = client.post("/api/website-monitoring/sites", headers=_h(MEMBER), json={"name": "B", "url": "https://b.example.com/"})
        assert add.status_code == 403
        rem = client.delete(f"/api/website-monitoring/sites/{site['id']}", headers=_h(MEMBER))
        assert rem.status_code == 403
        assert len(wm.get_status(force_refresh=True)["sites"]) == 1

    def test_admin_add_then_remove(self, client):
        add = client.post("/api/website-monitoring/sites", headers=_h(ADMIN),
                          json={"name": "Example", "url": "example.com"})
        assert add.status_code == 201
        site = add.json()
        assert site["url"] == "https://example.com/"
        assert site["addedBy"] == ADMIN

        listed = client.get("/api/website-monitoring/status", headers=_h(MEMBER)).json()
        assert [s["id"] for s in listed["sites"]] == [site["id"]]

        rem = client.delete(f"/api/website-monitoring/sites/{site['id']}", headers=_h(ADMIN))
        assert rem.status_code == 200
        assert rem.json() == {"removed": site["id"]}
        assert client.get("/api/website-monitoring/status", headers=_h(MEMBER)).json()["sites"] == []

    def test_error_mapping(self, client):
        bad = client.post("/api/website-monitoring/sites", headers=_h(ADMIN),
                          json={"name": "X", "url": "http://169.254.169.254/"})
        assert bad.status_code == 400
        ok = client.post("/api/website-monitoring/sites", headers=_h(ADMIN), json={"name": "A", "url": "https://a.example.com/"})
        assert ok.status_code == 201
        dup = client.post("/api/website-monitoring/sites", headers=_h(ADMIN), json={"name": "A2", "url": "https://a.example.com"})
        assert dup.status_code == 409
        missing = client.delete("/api/website-monitoring/sites/unknown", headers=_h(ADMIN))
        assert missing.status_code == 404

    def test_not_configured_returns_503(self, client, monkeypatch):
        monkeypatch.delenv("WEBSITE_MONITOR_BUCKET")
        assert client.get("/api/website-monitoring/status", headers=_h(MEMBER)).status_code == 503


class TestTabVisibility:
    def test_everyone_sees_website_monitoring_but_nothing_else_without_groups(self, client):
        resp = client.get("/api/settings/my-tab-access", headers=_h(MEMBER))
        assert resp.status_code == 200
        access = resp.json()
        assert access["website_monitoring"] is True
        assert access["waf_monitoring"] is False
        assert access["dashboard"] is False

    def test_admin_sees_it_too(self, client):
        access = client.get("/api/settings/my-tab-access", headers=_h(ADMIN)).json()
        assert access["website_monitoring"] is True
