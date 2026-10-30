"""
Tests for app/patching.py and its /api/patching/os/* routes
(sidebar: Lifecycle > Patching, OS tab).

No real AWS / DB access: the settings store is replaced by an in-memory dict
and the EC2 inventory by a fixed fake.

Access rules covered:
  * every logged-in user may read plans and the instance list;
  * only admins, Cloud Infra / Security / Management Team members, or users
    individually granted write on 'lifecycle_patching' may create / edit /
    delete.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

ADMIN = "admin@example.com"
MEMBER = "member@example.com"          # no groups
CLOUD = "cloud@example.com"            # cloud_infra_team
SEC = "sec@example.com"                # security_team
MGMT = "mgmt@example.com"              # management_team
BILLING = "billing@example.com"        # billing_team (not privileged)
GRANTED = "granted@example.com"        # individually granted write

INV = [
    {"account_id": "111111111111", "account_name": "core", "region": "ap-southeast-1",
     "instance_id": "i-aaa", "name": "web-01", "instance_type": "t3.medium", "state": "running", "private_ip": "10.0.0.1"},
    {"account_id": "111111111111", "account_name": "core", "region": "ap-southeast-1",
     "instance_id": "i-bbb", "name": "web-02", "instance_type": "t3.medium", "state": "running", "private_ip": "10.0.0.2"},
    {"account_id": "222222222222", "account_name": "network", "region": "us-east-1",
     "instance_id": "i-ccc", "name": "api-01", "instance_type": "m5.large", "state": "stopped", "private_ip": "10.1.0.3"},
]


@pytest.fixture
def pt(monkeypatch):
    """patching module wired to an in-memory settings store and fake inventory."""
    from app import patching, settings_store

    store: dict = {}

    def fake_get(key, default=None):
        return dict(store[key]) if key in store else (dict(default) if default is not None else {})

    def fake_set(key, value, *, actor_email, actor_name):
        store[key] = {"value": value, "updated_by_email": actor_email, "updated_by_name": actor_name, "updated_at": "now"}
        return store[key]

    monkeypatch.setattr(settings_store, "get_setting", fake_get)
    monkeypatch.setattr(settings_store, "set_setting", fake_set)
    monkeypatch.setattr(
        patching, "list_instances",
        lambda force_refresh=False: {"instances": [dict(i) for i in INV], "as_of": "2026-10-22T00:00:00Z", "is_mock": False},
    )
    patching._store = store  # exposed for assertions
    return patching


def body(**over):
    base = {
        "title": "Nov patching",
        "schedule_date": "2026-11-14",
        "schedule_time": "22:00",
        "teams_meeting": "webapp",
        "freshservice_change": "CHG-1234",
        "ams_sr": "98765",
        "notes": "",
        "status": "planned",
        "instances": [{"account_id": "111111111111", "instance_id": "i-aaa"}],
    }
    base.update(over)
    return base


class TestCreate:
    def test_happy_path_snapshots_inventory_and_converts_to_utc(self, pt):
        plan = pt.create_plan(body(instances=[
            {"account_id": "111111111111", "instance_id": "i-aaa"},
            {"account_id": "222222222222", "instance_id": "i-ccc"},
        ]), CLOUD)
        assert plan["scheduled_at_utc"] == "2026-11-14T14:00:00Z"  # 22:00 UTC+8
        assert plan["created_by"] == CLOUD
        assert [i["name"] for i in plan["instances"]] == ["web-01", "api-01"]
        assert plan["instances"][1]["account_name"] == "network"
        assert plan["instances"][1]["private_ip"] == "10.1.0.3"
        assert plan["teams_meeting_label"] == "WebApp"
        assert plan["freshservice_change"] == "CHG-1234"
        assert plan["ams_sr"] == "98765"

    def test_client_cannot_spoof_instance_details(self, pt):
        plan = pt.create_plan(body(instances=[
            {"account_id": "111111111111", "instance_id": "i-aaa", "name": "EVIL", "private_ip": "1.2.3.4"},
        ]), CLOUD)
        assert plan["instances"][0]["name"] == "web-01"
        assert plan["instances"][0]["private_ip"] == "10.0.0.1"

    def test_duplicates_are_collapsed(self, pt):
        ref = {"account_id": "111111111111", "instance_id": "i-aaa"}
        plan = pt.create_plan(body(instances=[ref, ref]), CLOUD)
        assert len(plan["instances"]) == 1

    @pytest.mark.parametrize("over", [
        {"schedule_date": "2026-13-45"},
        {"schedule_date": ""},
        {"schedule_time": "25:00"},
        {"schedule_time": "10pm"},
        {"schedule_date": "1999-01-01"},
        {"instances": []},
        {"instances": [{"account_id": "111111111111", "instance_id": "i-nope"}]},
        {"instances": [{"account_id": "999999999999", "instance_id": "i-aaa"}]},  # right id, wrong account
        {"teams_meeting": "https://evil.example.com"},
        {"teams_meeting": "not-a-link-id"},
        {"freshservice_change": "<script>"},
        {"ams_sr": "x" * 41},
        {"status": "weird"},
        {"title": "t" * 81},
    ])
    def test_rejects_bad_input(self, pt, over):
        with pytest.raises(pt.PatchingValidationError):
            pt.create_plan(body(**over), CLOUD)

    def test_references_and_meeting_are_optional(self, pt):
        plan = pt.create_plan(body(teams_meeting=None, freshservice_change="", ams_sr=""), CLOUD)
        assert plan["teams_meeting"] is None and plan["teams_meeting_url"] is None
        assert plan["freshservice_change"] == "" and plan["ams_sr"] == ""

    def test_notes_control_characters_are_stripped(self, pt):
        plan = pt.create_plan(body(notes="a\x00b\x07c"), CLOUD)
        assert plan["notes"] == "abc"


TEAMS_URL = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_NEWLINK%40thread.v2/0?context=%7b%22Tid%22%3a%22x%22%7d"


class TestSeedTeamsLinks:
    def test_three_links_are_seeded_when_nothing_was_ever_saved(self, pt):
        meetings = {m["id"]: m for m in pt.get_overview()["teams_meetings"]}
        assert set(meetings) == {"webapp", "lms", "ibe"}
        assert meetings["webapp"]["name"] == "WebApp"
        assert "SEEDLINKONE" in meetings["webapp"]["url"]
        assert "SEEDLINKTWO" in meetings["lms"]["url"]
        assert "SEEDLINKTHREE" in meetings["ibe"]["url"]
        for m in meetings.values():
            assert m["url"].startswith("https://teams.microsoft.com/l/meetup-join/19%3ameeting_")
            assert "tenant-id" in m["url"]  # tenant context

    @pytest.mark.parametrize("key", ["webapp", "lms", "ibe"])
    def test_each_seed_link_can_be_bound_to_a_plan(self, pt, key):
        plan = pt.create_plan(body(teams_meeting=key), CLOUD)
        assert plan["teams_meeting"] == key
        assert plan["teams_meeting_url"] == {m["id"]: m for m in pt.get_overview()["teams_meetings"]}[key]["url"]

    def test_plans_saved_before_links_were_editable_keep_working(self, pt):
        # Old document shape: plans only, referencing the hard-coded keys.
        pt._store[pt.PLANS_KEY] = {"value": {"version": 5, "plans": [
            {"id": "old1", "schedule_date": "2026-11-01", "schedule_time": "22:00", "status": "planned",
             "teams_meeting": "lms", "instances": [], "title": "", "created_at": "t"},
        ]}}
        plan = pt.list_plans()[0]
        assert plan["teams_meeting_label"] == "LMS" and "SEEDLINKTWO" in plan["teams_meeting_url"]
        assert plan["group_id"] is None


class TestTeamsLinksMaintainedByTeam:
    def test_add_link(self, pt):
        m = pt.create_meeting("  Project Falcon ", TEAMS_URL, CLOUD)
        assert m["name"] == "Project Falcon" and m["url"] == TEAMS_URL and m["created_by"] == CLOUD
        names = [x["name"] for x in pt.get_overview()["teams_meetings"]]
        assert names == sorted(names, key=str.lower) and "Project Falcon" in names
        assert len(names) == 4  # 3 seeds + new one (seeds are persisted on first write)
        assert pt._load_doc()["version"] == 1

    def test_new_link_can_be_used_by_a_plan(self, pt):
        m = pt.create_meeting("Falcon", TEAMS_URL, CLOUD)
        plan = pt.create_plan(body(teams_meeting=m["id"]), CLOUD)
        assert plan["teams_meeting_label"] == "Falcon" and plan["teams_meeting_url"] == TEAMS_URL

    def test_editing_name_and_url_flows_through_to_existing_plans(self, pt):
        plan = pt.create_plan(body(teams_meeting="lms"), CLOUD)
        pt.update_meeting("lms", "LMS (new bridge)", TEAMS_URL, SEC)
        again = pt.list_plans()[0]
        assert again["id"] == plan["id"]
        assert again["teams_meeting_label"] == "LMS (new bridge)" and again["teams_meeting_url"] == TEAMS_URL
        meeting = {m["id"]: m for m in pt.get_overview()["teams_meetings"]}["lms"]
        assert meeting["updated_by"] == SEC

    @pytest.mark.parametrize("url", [
        "", "http://teams.microsoft.com/l/meetup-join/x", "https://evil.example.com/l/meetup-join/x",
        "https://teams.microsoft.com.evil.example.com/x", "javascript:alert(1)", "ftp://teams.microsoft.com/x",
        "https://user:pw@teams.microsoft.com/x", "https://teams.microsoft.com/a b", "teams.microsoft.com/x",
        "https://teams.microsoft.com/" + "a" * 2000,
    ])
    def test_rejects_non_teams_or_unsafe_urls(self, pt, url):
        with pytest.raises(pt.PatchingValidationError):
            pt.create_meeting("Bad", url, CLOUD)
        with pytest.raises(pt.PatchingValidationError):
            pt.update_meeting("lms", "LMS", url, CLOUD)

    def test_accepts_other_microsoft_teams_hosts(self, pt):
        pt.create_meeting("Live", "https://teams.live.com/meet/9876", CLOUD)
        pt.create_meeting("Cloud", "https://teams.cloud.microsoft/l/meetup-join/x", CLOUD)

    def test_name_rules(self, pt):
        with pytest.raises(pt.PatchingValidationError):
            pt.create_meeting("   ", TEAMS_URL, CLOUD)
        with pytest.raises(pt.PatchingValidationError):
            pt.create_meeting("n" * 61, TEAMS_URL, CLOUD)
        with pytest.raises(pt.PatchingConflictError):
            pt.create_meeting("lms", TEAMS_URL, CLOUD)          # clashes with seed "LMS", case-insensitive
        pt.create_meeting("Other", TEAMS_URL, CLOUD)
        with pytest.raises(pt.PatchingConflictError):
            pt.update_meeting("ibe", "other", TEAMS_URL, CLOUD)  # rename onto another link
        pt.update_meeting("ibe", "IBE", TEAMS_URL, CLOUD)         # keeping its own name is fine

    def test_unused_link_can_be_deleted_used_link_cannot(self, pt):
        pt.create_plan(body(teams_meeting="webapp"), CLOUD)
        with pytest.raises(pt.PatchingConflictError) as e:
            pt.delete_meeting("webapp", CLOUD)
        assert "used by 1 patching schedule" in str(e.value)
        pt.delete_meeting("ibe", CLOUD)
        assert {m["id"] for m in pt.get_overview()["teams_meetings"]} == {"webapp", "lms"}
        # and a deleted link can no longer be picked
        with pytest.raises(pt.PatchingValidationError):
            pt.create_plan(body(schedule_date="2026-12-01", teams_meeting="ibe"), CLOUD)

    def test_link_becomes_deletable_once_the_plan_uses_another(self, pt):
        plan = pt.create_plan(body(teams_meeting="webapp"), CLOUD)
        pt.update_plan(plan["id"], body(teams_meeting="lms"), CLOUD)
        pt.delete_meeting("webapp", CLOUD)

    def test_not_found(self, pt):
        with pytest.raises(pt.PatchingNotFoundError):
            pt.update_meeting("nope", "x", TEAMS_URL, CLOUD)
        with pytest.raises(pt.PatchingNotFoundError):
            pt.delete_meeting("nope", CLOUD)

    def test_deleting_every_link_does_not_bring_the_seeds_back(self, pt):
        for mid in ("webapp", "lms", "ibe"):
            pt.delete_meeting(mid, CLOUD)
        assert pt.get_overview()["teams_meetings"] == []


class TestGroups:
    def test_nothing_is_grouped_automatically(self, pt):
        # Same date AND time, no group chosen -> both stay ungrouped.
        a = pt.create_plan(body(), CLOUD)
        b = pt.create_plan(body(instances=[{"account_id": "111111111111", "instance_id": "i-bbb"}]), CLOUD)
        assert a["group_id"] is None and b["group_id"] is None
        assert pt.get_overview()["groups"] == []

    def test_create_group_and_file_plans_under_it(self, pt):
        g = pt.create_group("  Nov   2026 monthly patching ", CLOUD)
        assert g["name"] == "Nov 2026 monthly patching" and g["created_by"] == CLOUD
        a = pt.create_plan(body(group_id=g["id"]), CLOUD)
        b = pt.create_plan(body(schedule_date="2026-11-15", group_id=g["id"]), CLOUD)
        c = pt.create_plan(body(schedule_date="2026-11-16"), CLOUD)
        assert a["group_id"] == b["group_id"] == g["id"] and c["group_id"] is None

    def test_a_plan_can_be_moved_between_groups(self, pt):
        g1, g2 = pt.create_group("One", CLOUD), pt.create_group("Two", CLOUD)
        plan = pt.create_plan(body(group_id=g1["id"]), CLOUD)
        assert pt.update_plan(plan["id"], body(group_id=g2["id"]), CLOUD)["group_id"] == g2["id"]
        assert pt.update_plan(plan["id"], body(group_id=None), CLOUD)["group_id"] is None

    def test_unknown_group_is_rejected(self, pt):
        with pytest.raises(pt.PatchingValidationError):
            pt.create_plan(body(group_id="nope"), CLOUD)

    def test_group_name_rules(self, pt):
        with pytest.raises(pt.PatchingValidationError):
            pt.create_group("   ", CLOUD)
        with pytest.raises(pt.PatchingValidationError):
            pt.create_group("g" * 61, CLOUD)
        pt.create_group("Alpha", CLOUD)
        with pytest.raises(pt.PatchingConflictError):
            pt.create_group("alpha", CLOUD)

    def test_rename(self, pt):
        g = pt.create_group("Alpha", CLOUD)
        other = pt.create_group("Beta", CLOUD)
        assert pt.rename_group(g["id"], "Alpha v2", SEC)["updated_by"] == SEC
        with pytest.raises(pt.PatchingConflictError):
            pt.rename_group(g["id"], "beta", CLOUD)
        pt.rename_group(other["id"], "Beta", CLOUD)  # same name as itself is fine
        with pytest.raises(pt.PatchingNotFoundError):
            pt.rename_group("nope", "x", CLOUD)

    def test_delete_group_keeps_its_schedules_as_ungrouped(self, pt):
        g = pt.create_group("Temp", CLOUD)
        keep = pt.create_group("Keep", CLOUD)
        a = pt.create_plan(body(group_id=g["id"]), CLOUD)
        b = pt.create_plan(body(schedule_date="2026-11-20", group_id=keep["id"]), CLOUD)
        result = pt.delete_group(g["id"], CLOUD)
        assert result["ungrouped"] == 1
        plans = {p["id"]: p for p in pt.list_plans()}
        assert plans[a["id"]]["group_id"] is None and plans[b["id"]]["group_id"] == keep["id"]
        assert [x["name"] for x in pt.get_overview()["groups"]] == ["Keep"]
        with pytest.raises(pt.PatchingNotFoundError):
            pt.delete_group(g["id"], CLOUD)

    def test_groups_are_listed_alphabetically(self, pt):
        for n in ("charlie", "Alpha", "bravo"):
            pt.create_group(n, CLOUD)
        assert [g["name"] for g in pt.get_overview()["groups"]] == ["Alpha", "bravo", "charlie"]


class TestOverlap:
    def test_same_instance_same_day_is_rejected(self, pt):
        pt.create_plan(body(), CLOUD)
        with pytest.raises(pt.PatchingConflictError):
            pt.create_plan(body(schedule_time="23:00"), CLOUD)

    def test_different_day_or_instance_is_fine(self, pt):
        pt.create_plan(body(), CLOUD)
        pt.create_plan(body(schedule_date="2026-11-15"), CLOUD)
        pt.create_plan(body(instances=[{"account_id": "111111111111", "instance_id": "i-bbb"}]), CLOUD)

    def test_completed_or_cancelled_plans_do_not_block(self, pt):
        first = pt.create_plan(body(), CLOUD)
        pt.update_plan(first["id"], body(status="cancelled"), CLOUD)
        pt.create_plan(body(), CLOUD)

    def test_editing_a_plan_does_not_clash_with_itself(self, pt):
        plan = pt.create_plan(body(), CLOUD)
        pt.update_plan(plan["id"], body(schedule_time="23:30"), CLOUD)


class TestUpdateDelete:
    def test_update_changes_fields_and_keeps_creator(self, pt):
        plan = pt.create_plan(body(), CLOUD)
        updated = pt.update_plan(plan["id"], body(
            schedule_date="2026-12-01", freshservice_change="CHG-9", ams_sr="55", teams_meeting="lms", status="completed",
            instances=[{"account_id": "111111111111", "instance_id": "i-bbb"}],
        ), SEC)
        assert updated["created_by"] == CLOUD and updated["updated_by"] == SEC
        assert updated["schedule_date"] == "2026-12-01"
        assert updated["freshservice_change"] == "CHG-9" and updated["ams_sr"] == "55"
        assert updated["teams_meeting"] == "lms" and updated["status"] == "completed"
        assert [i["instance_id"] for i in updated["instances"]] == ["i-bbb"]
        assert len(pt.list_plans()) == 1

    def test_delete(self, pt):
        a = pt.create_plan(body(), CLOUD)
        b = pt.create_plan(body(schedule_date="2026-11-20"), CLOUD)
        pt.delete_plan(a["id"], CLOUD)
        assert [p["id"] for p in pt.list_plans()] == [b["id"]]

    def test_not_found(self, pt):
        with pytest.raises(pt.PatchingNotFoundError):
            pt.update_plan("nope", body(), CLOUD)
        with pytest.raises(pt.PatchingNotFoundError):
            pt.delete_plan("nope", CLOUD)

    def test_plans_are_sorted_by_schedule(self, pt):
        pt.create_plan(body(schedule_date="2026-12-01"), CLOUD)
        pt.create_plan(body(schedule_date="2026-11-01"), CLOUD)
        pt.create_plan(body(schedule_date="2026-11-01", schedule_time="08:00",
                            instances=[{"account_id": "111111111111", "instance_id": "i-bbb"}]), CLOUD)
        assert [(p["schedule_date"], p["schedule_time"]) for p in pt.list_plans()] == [
            ("2026-11-01", "08:00"), ("2026-11-01", "22:00"), ("2026-12-01", "22:00"),
        ]

    def test_stale_version_write_is_refused(self, pt):
        pt.create_plan(body(), CLOUD)
        doc = pt._load_doc()
        with pytest.raises(pt.PatchingConflictError):
            pt._save_doc(doc, doc["version"] - 1, CLOUD)

    def test_dynamodb_decimal_numbers_are_tolerated(self, pt):
        from decimal import Decimal
        pt._store[pt.PLANS_KEY] = {"value": {"version": Decimal("3"), "plans": []}}
        assert pt._load_doc()["version"] == 3
        pt.create_plan(body(), CLOUD)
        assert pt._load_doc()["version"] == 4


# ── HTTP routes + access control ─────────────────────────────────────────

@pytest.fixture
def client(pt, monkeypatch):
    monkeypatch.setenv("ENV", "local")
    from app import main, user_store

    importlib.reload(main)
    people = {ADMIN, MEMBER, CLOUD, SEC, MGMT, BILLING, GRANTED}
    groups = {CLOUD: ["cloud_infra_team"], SEC: ["security_team"], MGMT: ["management_team"], BILLING: ["billing_team"]}
    monkeypatch.setattr(user_store, "is_allowed", lambda email: email in people)
    monkeypatch.setattr(user_store, "is_admin", lambda email: email == ADMIN)
    monkeypatch.setattr(user_store, "get_user_groups", lambda email: groups.get(email, []))
    monkeypatch.setattr(user_store, "get_user_tab_write",
                        lambda email: ["lifecycle_patching"] if email == GRANTED else [])
    return TestClient(main.app)


def _h(email):
    return {"X-Actor-Email": email}


class TestRoutesAccess:
    def test_unauthenticated_is_rejected(self, client):
        assert client.get("/api/patching/os/plans").status_code == 401
        assert client.get("/api/patching/os/instances").status_code == 401
        assert client.post("/api/patching/os/plans", json=body()).status_code == 401

    @pytest.mark.parametrize("who", [MEMBER, BILLING, CLOUD, SEC, MGMT, ADMIN, GRANTED])
    def test_everyone_can_read_plans_and_instances(self, client, who):
        plans = client.get("/api/patching/os/plans", headers=_h(who))
        assert plans.status_code == 200
        assert plans.json()["timezone"] == "UTC+8"
        assert {m["id"] for m in plans.json()["teams_meetings"]} == {"webapp", "lms", "ibe"}
        assert plans.json()["groups"] == []
        inst = client.get("/api/patching/os/instances", headers=_h(who))
        assert inst.status_code == 200
        assert len(inst.json()["instances"]) == 3

    @pytest.mark.parametrize("who,expected", [
        (MEMBER, False), (BILLING, False), (CLOUD, True), (SEC, True), (MGMT, True), (ADMIN, True), (GRANTED, True),
    ])
    def test_can_edit_flag(self, client, who, expected):
        assert client.get("/api/patching/os/plans", headers=_h(who)).json()["can_edit"] is expected

    @pytest.mark.parametrize("who", [MEMBER, BILLING])
    def test_unprivileged_users_cannot_write(self, client, pt, who):
        existing = pt.create_plan(body(), ADMIN)
        assert client.post("/api/patching/os/plans", headers=_h(who), json=body(schedule_date="2026-12-12")).status_code == 403
        assert client.put(f"/api/patching/os/plans/{existing['id']}", headers=_h(who), json=body(notes="x")).status_code == 403
        assert client.delete(f"/api/patching/os/plans/{existing['id']}", headers=_h(who)).status_code == 403
        assert len(pt.list_plans()) == 1 and pt.list_plans()[0]["notes"] == ""

    @pytest.mark.parametrize("who", [CLOUD, SEC, MGMT, ADMIN, GRANTED])
    def test_privileged_users_can_create_edit_delete(self, client, who):
        created = client.post("/api/patching/os/plans", headers=_h(who), json=body())
        assert created.status_code == 201
        plan = created.json()
        assert plan["created_by"] == who
        upd = client.put(f"/api/patching/os/plans/{plan['id']}", headers=_h(who),
                         json=body(freshservice_change="CHG-77", teams_meeting="ibe"))
        assert upd.status_code == 200 and upd.json()["freshservice_change"] == "CHG-77"
        assert upd.json()["teams_meeting_label"] == "IBE"
        assert client.delete(f"/api/patching/os/plans/{plan['id']}", headers=_h(who)).json() == {"removed": plan["id"]}
        assert client.get("/api/patching/os/plans", headers=_h(who)).json()["plans"] == []


class TestRoutesGroupsAndLinks:
    @pytest.mark.parametrize("who", [MEMBER, BILLING])
    def test_unprivileged_users_cannot_change_groups_or_links(self, client, pt, who):
        g = pt.create_group("Existing", ADMIN)
        assert client.post("/api/patching/os/groups", headers=_h(who), json={"name": "x"}).status_code == 403
        assert client.put(f"/api/patching/os/groups/{g['id']}", headers=_h(who), json={"name": "y"}).status_code == 403
        assert client.delete(f"/api/patching/os/groups/{g['id']}", headers=_h(who)).status_code == 403
        assert client.post("/api/patching/os/meetings", headers=_h(who), json={"name": "x", "url": TEAMS_URL}).status_code == 403
        assert client.put("/api/patching/os/meetings/lms", headers=_h(who), json={"name": "x", "url": TEAMS_URL}).status_code == 403
        assert client.delete("/api/patching/os/meetings/ibe", headers=_h(who)).status_code == 403
        ov = pt.get_overview()
        assert [x["name"] for x in ov["groups"]] == ["Existing"]
        assert len(ov["teams_meetings"]) == 3

    @pytest.mark.parametrize("who", [CLOUD, SEC, MGMT, ADMIN, GRANTED])
    def test_privileged_users_manage_groups_and_links(self, client, who):
        g = client.post("/api/patching/os/groups", headers=_h(who), json={"name": "Nov patching"})
        assert g.status_code == 201
        gid = g.json()["id"]
        assert client.put(f"/api/patching/os/groups/{gid}", headers=_h(who), json={"name": "Nov patching v2"}).json()["name"] == "Nov patching v2"

        m = client.post("/api/patching/os/meetings", headers=_h(who), json={"name": "Falcon", "url": TEAMS_URL})
        assert m.status_code == 201
        mid = m.json()["id"]
        assert client.put(f"/api/patching/os/meetings/{mid}", headers=_h(who),
                          json={"name": "Falcon 2", "url": TEAMS_URL}).json()["name"] == "Falcon 2"

        plan = client.post("/api/patching/os/plans", headers=_h(who), json=body(group_id=gid, teams_meeting=mid))
        assert plan.status_code == 201
        assert plan.json()["group_id"] == gid and plan.json()["teams_meeting_label"] == "Falcon 2"

        listed = client.get("/api/patching/os/plans", headers=_h(MEMBER)).json()
        assert [x["name"] for x in listed["groups"]] == ["Nov patching v2"]
        assert "Falcon 2" in [x["name"] for x in listed["teams_meetings"]]

        # the link is in use -> 409; deleting the group keeps the schedule
        assert client.delete(f"/api/patching/os/meetings/{mid}", headers=_h(who)).status_code == 409
        deleted = client.delete(f"/api/patching/os/groups/{gid}", headers=_h(who))
        assert deleted.status_code == 200 and deleted.json() == {"removed": gid, "ungrouped": 1}
        assert client.get("/api/patching/os/plans", headers=_h(MEMBER)).json()["plans"][0]["group_id"] is None

    def test_error_mapping(self, client):
        assert client.post("/api/patching/os/groups", headers=_h(CLOUD), json={"name": " "}).status_code == 400
        assert client.post("/api/patching/os/groups", headers=_h(CLOUD), json={"name": "A"}).status_code == 201
        assert client.post("/api/patching/os/groups", headers=_h(CLOUD), json={"name": "a"}).status_code == 409
        assert client.put("/api/patching/os/groups/nope", headers=_h(CLOUD), json={"name": "x"}).status_code == 404
        assert client.delete("/api/patching/os/groups/nope", headers=_h(CLOUD)).status_code == 404
        bad = client.post("/api/patching/os/meetings", headers=_h(CLOUD), json={"name": "x", "url": "https://evil.example.com/"})
        assert bad.status_code == 400
        assert client.post("/api/patching/os/meetings", headers=_h(CLOUD), json={"name": "x"}).status_code == 422
        assert client.delete("/api/patching/os/meetings/nope", headers=_h(CLOUD)).status_code == 404


class TestRoutesErrors:
    def test_error_mapping(self, client):
        assert client.post("/api/patching/os/plans", headers=_h(CLOUD), json=body(schedule_time="99:99")).status_code == 400
        assert client.post("/api/patching/os/plans", headers=_h(CLOUD), json={"title": "x"}).status_code == 422
        assert client.post("/api/patching/os/plans", headers=_h(CLOUD), json=body()).status_code == 201
        assert client.post("/api/patching/os/plans", headers=_h(CLOUD), json=body(schedule_time="23:00")).status_code == 409
        assert client.put("/api/patching/os/plans/unknown", headers=_h(CLOUD), json=body()).status_code == 404
        assert client.delete("/api/patching/os/plans/unknown", headers=_h(CLOUD)).status_code == 404

    def test_inventory_failure_is_502(self, client, pt, monkeypatch):
        def boom(force_refresh=False):
            raise pt.PatchingInventoryError("Could not load the EC2 inventory")
        monkeypatch.setattr(pt, "list_instances", boom)
        assert client.get("/api/patching/os/instances", headers=_h(MEMBER)).status_code == 502
        assert client.post("/api/patching/os/plans", headers=_h(CLOUD), json=body()).status_code == 502


class TestTabVisibility:
    def test_member_without_groups_sees_the_patching_tab(self, client):
        access = client.get("/api/settings/my-tab-access", headers=_h(MEMBER)).json()
        assert access["lifecycle_patching"] is True
        assert access["waf_monitoring"] is False


class TestInventoryAdapter:
    def test_list_instances_maps_ec2_rows(self, monkeypatch):
        from app import patching, aws_resources_inventory as inv
        headers = ["Account ID", "Region", "Instance ID", "Instance Type", "State", "VPC ID", "Private IP", "EBS Info", "Name", "Tags"]
        rows = [
            ["111111111111", "ap-southeast-1", "i-zzz", "t3.small", "running", "vpc-1", "10.0.0.9", "", "zeta", ""],
            ["111111111111", "ap-southeast-1", "i-aaa", "t3.small", "stopped", "vpc-1", "10.0.0.8", "", "alpha", ""],
            ["111111111111", "ap-southeast-1", "", "t3.small", "running", "vpc-1", "", "", "blank-id", ""],
        ]
        monkeypatch.setattr(inv, "list_accounts", lambda: [{"account_id": "111111111111", "alias": "core"}])
        monkeypatch.setattr(inv, "get_resource_data",
                            lambda ids, rtype, force_refresh=False: {"headers": headers, "rows": rows, "as_of": "t", "is_mock": False})
        out = patching.list_instances()
        assert [i["name"] for i in out["instances"]] == ["alpha", "zeta"]
        assert out["instances"][0]["account_name"] == "core"
        assert out["instances"][0]["state"] == "stopped"
