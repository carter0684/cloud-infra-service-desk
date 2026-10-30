"""
Lifecycle > Patching > OS - patch scope and schedule planner.

Lets the Security / Cloud Infra / Management teams record WHICH EC2 instances
are in scope for an OS patching run and WHEN it happens, with the related
Freshservice Change number, AMS Service Request number and the MS Teams
bridge for the patching window.

## Concepts

* plan      one patching schedule: date + time (UTC+8), a set of EC2 instances,
            Change / SR numbers, an optional Teams link, an optional group.
* group     a user-created "group / task name" that schedules are filed under
            (e.g. "Nov 2026 monthly patching"). Chosen by the user per plan -
            nothing is grouped automatically. Deleting a group keeps its
            schedules (they become ungrouped).
* meeting   a named MS Teams link maintained by the teams themselves (add /
            rename / change URL / delete). Plans reference a meeting by id, so
            editing a link updates every plan that uses it. A link that is still
            used by a plan cannot be deleted.

## Data

* The instance list comes from the same inventory as AWS Resources > EC2
  Instances (aws_resources_inventory.get_resource_data(..., "ec2")). The
  client only sends (account_id, instance_id) pairs; name / region / private
  IP are looked up server-side, so a plan can never reference an instance that
  is not in the inventory and the stored snapshot cannot be spoofed.
* Everything lives in one JSON document in the generic settings store
  (settings_store, key "patching_os_plans") - no new table / IAM needed:
  {"version", "plans", "groups", "meetings"}. The document is versioned and
  size-capped. Edits are rare and made by a handful of people, so a
  per-process lock plus a version check is used; if two people save in the same
  instant on different ECS tasks the last writer wins.
* "meetings" is seeded from PATCHING_DEFAULT_TEAMS_MEETINGS (empty by default) the first time the document is
  read without that field (so plans saved before the links became editable keep
  working: they used the same ids).
* The schedule is entered and shown in UTC+8 (Hong Kong). `scheduled_at_utc`
  is derived for sorting / downstream use.

## Who may do what

* Read: every logged-in user (main.py grants the tab to everyone).
* Create / edit / delete plans, groups and Teams links: admins, the Cloud Infra
  / Security / Management Teams, or users individually granted write on this
  tab (main.py's require_patching_writer) - enforced server-side.

## Teams links

A link must be https and on a Microsoft Teams host (see _TEAMS_HOSTS). Anyone
who can edit links can put a URL in front of every viewer, so arbitrary
destinations are not accepted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from urllib.parse import urlsplit

from . import settings_store

logger = logging.getLogger(__name__)

PLANS_KEY = "patching_os_plans"
HKT = timezone(timedelta(hours=8))  # UTC+8, no DST

MAX_INSTANCES_PER_PLAN = 300
MAX_PLANS = 500
MAX_GROUPS = 100
MAX_MEETINGS = 50
MAX_DOC_BYTES = 300_000  # DynamoDB items are limited to 400 KB
MAX_TITLE = 80
MAX_NOTES = 500
MAX_NAME = 60
MAX_URL = 2000

STATUSES = ("planned", "completed", "cancelled")

# Hosts a Teams link may point at.
_TEAMS_HOSTS = {
    "teams.microsoft.com",
    "teams.live.com",
    "teams.cloud.microsoft",
    "gov.teams.microsoft.us",
    "dod.teams.microsoft.us",
}

# Optional seed links (ids double as the keys plans store). Empty by default;
# to pre-load recurring Teams meeting links set PATCHING_DEFAULT_TEAMS_MEETINGS
# to a JSON list such as
#   [{"id": "web", "name": "Web servers", "url": "https://teams.microsoft.com/l/meetup-join/..."}]
# Links can always be added, edited and deleted on the Patching page.
def _load_default_meetings() -> list[tuple[str, str, str]]:
    raw = os.environ.get("PATCHING_DEFAULT_TEAMS_MEETINGS", "").strip()
    if not raw:
        return []
    try:
        return [(str(m["id"]), str(m["name"]), str(m["url"])) for m in json.loads(raw)]
    except Exception:
        return []


DEFAULT_TEAMS_MEETINGS: list[tuple[str, str, str]] = _load_default_meetings()

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _#/\-]{0,39}$")
_write_lock = threading.Lock()


class PatchingValidationError(ValueError):
    """Bad input (HTTP 400)."""


class PatchingConflictError(Exception):
    """Duplicate / in-use / concurrent edit (HTTP 409)."""


class PatchingNotFoundError(KeyError):
    """No such plan / group / link (HTTP 404)."""


class PatchingInventoryError(Exception):
    """The EC2 inventory could not be loaded (HTTP 502)."""


# ── helpers ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(value):
    """DynamoDB hands numbers back as Decimal; turn everything into plain
    JSON-friendly Python so documents can be compared / serialised."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _default_meetings() -> list[dict]:
    now = _now_iso()
    return [
        {"id": mid, "name": name, "url": url,
         "created_by": "system", "created_at": now, "updated_by": "system", "updated_at": now}
        for mid, name, url in DEFAULT_TEAMS_MEETINGS
    ]


def _load_doc() -> dict:
    stored = settings_store.get_setting(PLANS_KEY)
    value = _plain((stored or {}).get("value") or {})
    plans = value.get("plans")
    groups = value.get("groups")
    meetings = value.get("meetings")
    return {
        "version": int(value.get("version") or 0),
        "plans": plans if isinstance(plans, list) else [],
        "groups": groups if isinstance(groups, list) else [],
        # Missing (not merely empty) means "never edited": offer the seed links.
        "meetings": meetings if isinstance(meetings, list) else _default_meetings(),
    }


def _save_doc(doc: dict, expected_version: int, actor: str) -> None:
    current = _load_doc()
    if current["version"] != expected_version:
        raise PatchingConflictError("The patching data was changed by someone else - reload and try again")
    payload = {
        "version": expected_version + 1,
        "plans": doc["plans"],
        "groups": doc["groups"],
        "meetings": doc["meetings"],
    }
    if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > MAX_DOC_BYTES:
        raise PatchingValidationError("Too much patching data stored - delete some old schedules first")
    settings_store.set_setting(PLANS_KEY, payload, actor_email=actor, actor_name=actor)


def _schedule_utc(date_s: str, time_s: str) -> str:
    local = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=HKT)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _public_plan(plan: dict, meetings_by_id: dict[str, dict]) -> dict:
    out = dict(plan)
    out.setdefault("group_id", None)
    meeting = meetings_by_id.get(plan.get("teams_meeting") or "")
    out["teams_meeting_label"] = meeting["name"] if meeting else None
    out["teams_meeting_url"] = meeting["url"] if meeting else None
    return out


def _sort_key(plan: dict):
    return (plan.get("schedule_date", ""), plan.get("schedule_time", ""), plan.get("created_at", ""))


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _clean_text(value, label: str, limit: int) -> str:
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value or "") if isinstance(value, str) else ""
    value = re.sub(r"\s+", " ", value).strip() if limit <= MAX_NAME else value.strip()
    if len(value) > limit:
        raise PatchingValidationError(f"{label} must be at most {limit} characters")
    return value


def _clean_ref(value, label: str) -> str:
    value = "" if value is None else str(value).strip()
    if not value:
        return ""
    if not _REF_RE.match(value):
        raise PatchingValidationError(
            f"{label} may only contain letters, numbers, spaces and - _ # / (max 40 characters)"
        )
    return value


def _clean_name(value, label: str) -> str:
    name = _clean_text(value, label, MAX_NAME)
    if not name:
        raise PatchingValidationError(f"{label} is required")
    return name


# ── inventory ────────────────────────────────────────────────────────────

def list_instances(force_refresh: bool = False) -> dict:
    """Every EC2 instance in the AWS Resources inventory, as dicts."""
    from .aws_resources_inventory import get_resource_data, list_accounts, AwsResourcesInventoryError

    try:
        accounts = list_accounts()
        names = {a["account_id"]: a["alias"] for a in accounts}
        data = get_resource_data([a["account_id"] for a in accounts], "ec2", force_refresh=force_refresh)
    except AwsResourcesInventoryError as e:
        logger.error("patching: inventory load failed: %s", e)
        raise PatchingInventoryError("Could not load the EC2 inventory") from e

    headers = data.get("headers") or []
    idx = {h: i for i, h in enumerate(headers)}
    needed = ("Account ID", "Region", "Instance ID", "Instance Type", "State", "Private IP", "Name")
    if any(h not in idx for h in needed):
        raise PatchingInventoryError("Unexpected EC2 inventory format")

    def cell(row, header):
        i = idx[header]
        v = row[i] if i < len(row) else ""
        return "" if v is None else str(v)

    instances = []
    for row in data.get("rows") or []:
        iid = cell(row, "Instance ID")
        if not iid:
            continue
        acct = cell(row, "Account ID")
        instances.append({
            "account_id": acct,
            "account_name": names.get(acct, acct),
            "region": cell(row, "Region"),
            "instance_id": iid,
            "name": cell(row, "Name"),
            "instance_type": cell(row, "Instance Type"),
            "state": cell(row, "State"),
            "private_ip": cell(row, "Private IP"),
        })
    instances.sort(key=lambda r: (r["account_name"].lower(), r["region"], r["name"].lower(), r["instance_id"]))
    return {"instances": instances, "as_of": data.get("as_of"), "is_mock": bool(data.get("is_mock"))}


def _inventory_index() -> dict[tuple[str, str], dict]:
    data = list_instances()
    return {(i["account_id"], i["instance_id"]): i for i in data["instances"]}


# ── plans ────────────────────────────────────────────────────────────────

def _validate_plan(payload: dict, inventory: dict[tuple[str, str], dict], doc: dict) -> dict:
    date_s = (payload.get("schedule_date") or "").strip()
    time_s = (payload.get("schedule_time") or "").strip()
    try:
        datetime.strptime(date_s, "%Y-%m-%d")
        datetime.strptime(time_s, "%H:%M")
    except ValueError:
        raise PatchingValidationError("Schedule needs a valid date (YYYY-MM-DD) and time (HH:MM, UTC+8)")
    if not ("2020-01-01" <= date_s <= "2100-12-31"):
        raise PatchingValidationError("Schedule date is out of range")

    meeting = (payload.get("teams_meeting") or "").strip()
    if meeting and meeting not in {m["id"] for m in doc["meetings"]}:
        raise PatchingValidationError("Unknown Teams meeting link")

    group_id = (payload.get("group_id") or "").strip()
    if group_id and group_id not in {g["id"] for g in doc["groups"]}:
        raise PatchingValidationError("Unknown group")

    status = (payload.get("status") or "planned").strip().lower()
    if status not in STATUSES:
        raise PatchingValidationError("Status must be planned, completed or cancelled")

    requested = payload.get("instances")
    if not isinstance(requested, list) or not requested:
        raise PatchingValidationError("Select at least one EC2 instance")
    if len(requested) > MAX_INSTANCES_PER_PLAN:
        raise PatchingValidationError(f"At most {MAX_INSTANCES_PER_PLAN} instances per plan")

    instances, seen = [], set()
    for item in requested:
        if not isinstance(item, dict):
            raise PatchingValidationError("Invalid instance selection")
        key = (str(item.get("account_id") or ""), str(item.get("instance_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        found = inventory.get(key)
        if found is None:
            raise PatchingValidationError(f"Instance {key[1] or '?'} is not in the EC2 inventory")
        instances.append({
            "account_id": found["account_id"],
            "account_name": found["account_name"],
            "region": found["region"],
            "instance_id": found["instance_id"],
            "name": found["name"],
            "private_ip": found["private_ip"],
        })

    return {
        "title": _clean_text(payload.get("title"), "Title", MAX_TITLE),
        "group_id": group_id or None,
        "schedule_date": date_s,
        "schedule_time": time_s,
        "scheduled_at_utc": _schedule_utc(date_s, time_s),
        "teams_meeting": meeting or None,
        "freshservice_change": _clean_ref(payload.get("freshservice_change"), "Freshservice change number"),
        "ams_sr": _clean_ref(payload.get("ams_sr"), "AMS SR number"),
        "notes": _clean_text(payload.get("notes"), "Notes", MAX_NOTES),
        "status": status,
        "instances": instances,
    }


def _check_overlap(clean: dict, plans: list[dict], own_id: Optional[str]) -> None:
    if clean["status"] != "planned":
        return
    mine = {(i["account_id"], i["instance_id"]) for i in clean["instances"]}
    for other in plans:
        if other.get("id") == own_id or other.get("status") != "planned":
            continue
        if other.get("schedule_date") != clean["schedule_date"]:
            continue
        clash = [i for i in other.get("instances", []) if (i["account_id"], i["instance_id"]) in mine]
        if clash:
            label = clash[0].get("name") or clash[0]["instance_id"]
            raise PatchingConflictError(
                f"{label} is already in another planned patching run on {clean['schedule_date']}"
                f" ({other.get('title') or other.get('id')})"
            )


def get_overview() -> dict:
    """Everything the OS tab needs in one read."""
    doc = _load_doc()
    meetings_by_id = {m["id"]: m for m in doc["meetings"]}
    return {
        "plans": [_public_plan(p, meetings_by_id) for p in sorted(doc["plans"], key=_sort_key)],
        "groups": sorted(doc["groups"], key=lambda g: g["name"].lower()),
        "teams_meetings": sorted(doc["meetings"], key=lambda m: m["name"].lower()),
    }


def list_plans() -> list[dict]:
    return get_overview()["plans"]


def create_plan(payload: dict, actor: str) -> dict:
    inventory = _inventory_index()
    with _write_lock:
        doc = _load_doc()
        clean = _validate_plan(payload, inventory, doc)
        if len(doc["plans"]) >= MAX_PLANS:
            raise PatchingValidationError(f"At most {MAX_PLANS} plans can be stored - delete old ones first")
        _check_overlap(clean, doc["plans"], None)
        now = _now_iso()
        plan = {
            "id": _new_id(),
            **clean,
            "created_by": actor,
            "created_at": now,
            "updated_by": actor,
            "updated_at": now,
        }
        doc["plans"].append(plan)
        _save_doc(doc, doc["version"], actor)
        meetings_by_id = {m["id"]: m for m in doc["meetings"]}
    logger.info("patching: %s created plan %s (%d instances, %s %s UTC+8)",
                actor, plan["id"], len(plan["instances"]), plan["schedule_date"], plan["schedule_time"])
    return _public_plan(plan, meetings_by_id)


def update_plan(plan_id: str, payload: dict, actor: str) -> dict:
    inventory = _inventory_index()
    with _write_lock:
        doc = _load_doc()
        existing = next((p for p in doc["plans"] if p.get("id") == plan_id), None)
        if existing is None:
            raise PatchingNotFoundError(plan_id)
        clean = _validate_plan(payload, inventory, doc)
        _check_overlap(clean, doc["plans"], plan_id)
        existing.update(clean)
        existing["updated_by"] = actor
        existing["updated_at"] = _now_iso()
        _save_doc(doc, doc["version"], actor)
        meetings_by_id = {m["id"]: m for m in doc["meetings"]}
    logger.info("patching: %s updated plan %s", actor, plan_id)
    return _public_plan(existing, meetings_by_id)


def delete_plan(plan_id: str, actor: str) -> dict:
    with _write_lock:
        doc = _load_doc()
        existing = next((p for p in doc["plans"] if p.get("id") == plan_id), None)
        if existing is None:
            raise PatchingNotFoundError(plan_id)
        doc["plans"] = [p for p in doc["plans"] if p.get("id") != plan_id]
        _save_doc(doc, doc["version"], actor)
    logger.info("patching: %s deleted plan %s (%s %s)", actor, plan_id,
                existing.get("schedule_date"), existing.get("schedule_time"))
    return existing


# ── groups (user-created "group / task name") ────────────────────────────

def _check_unique(name: str, items: list[dict], own_id: Optional[str], what: str) -> None:
    if any(i["id"] != own_id and i["name"].casefold() == name.casefold() for i in items):
        raise PatchingConflictError(f"A {what} named \"{name}\" already exists")


def create_group(name: str, actor: str) -> dict:
    name = _clean_name(name, "Group name")
    with _write_lock:
        doc = _load_doc()
        if len(doc["groups"]) >= MAX_GROUPS:
            raise PatchingValidationError(f"At most {MAX_GROUPS} groups can be stored")
        _check_unique(name, doc["groups"], None, "group")
        now = _now_iso()
        group = {"id": _new_id(), "name": name, "created_by": actor, "created_at": now,
                 "updated_by": actor, "updated_at": now}
        doc["groups"].append(group)
        _save_doc(doc, doc["version"], actor)
    logger.info("patching: %s created group %s (%s)", actor, group["id"], name)
    return group


def rename_group(group_id: str, name: str, actor: str) -> dict:
    name = _clean_name(name, "Group name")
    with _write_lock:
        doc = _load_doc()
        group = next((g for g in doc["groups"] if g["id"] == group_id), None)
        if group is None:
            raise PatchingNotFoundError(group_id)
        _check_unique(name, doc["groups"], group_id, "group")
        group["name"] = name
        group["updated_by"] = actor
        group["updated_at"] = _now_iso()
        _save_doc(doc, doc["version"], actor)
    logger.info("patching: %s renamed group %s to %s", actor, group_id, name)
    return group


def delete_group(group_id: str, actor: str) -> dict:
    """Remove a group; its schedules are kept and become ungrouped."""
    with _write_lock:
        doc = _load_doc()
        group = next((g for g in doc["groups"] if g["id"] == group_id), None)
        if group is None:
            raise PatchingNotFoundError(group_id)
        moved = 0
        for p in doc["plans"]:
            if p.get("group_id") == group_id:
                p["group_id"] = None
                moved += 1
        doc["groups"] = [g for g in doc["groups"] if g["id"] != group_id]
        _save_doc(doc, doc["version"], actor)
    logger.info("patching: %s deleted group %s (%s), %d schedules ungrouped", actor, group_id, group["name"], moved)
    return {"group": group, "ungrouped": moved}


# ── Teams links (maintained by the teams) ────────────────────────────────

def _clean_teams_url(value) -> str:
    url = value.strip() if isinstance(value, str) else ""
    if not url or len(url) > MAX_URL:
        raise PatchingValidationError(f"URL is required and must be at most {MAX_URL} characters")
    if re.search(r"[\s\x00-\x1f\x7f]", url):
        raise PatchingValidationError("URL must not contain spaces or control characters")
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        raise PatchingValidationError("URL is not valid")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in _TEAMS_HOSTS or parts.username or parts.password:
        raise PatchingValidationError(
            "Only https MS Teams meeting links are accepted (https://teams.microsoft.com/...)"
        )
    return url


def create_meeting(name: str, url: str, actor: str) -> dict:
    name = _clean_name(name, "Link name")
    url = _clean_teams_url(url)
    with _write_lock:
        doc = _load_doc()
        if len(doc["meetings"]) >= MAX_MEETINGS:
            raise PatchingValidationError(f"At most {MAX_MEETINGS} Teams links can be stored")
        _check_unique(name, doc["meetings"], None, "Teams link")
        now = _now_iso()
        meeting = {"id": _new_id(), "name": name, "url": url, "created_by": actor, "created_at": now,
                   "updated_by": actor, "updated_at": now}
        doc["meetings"].append(meeting)
        _save_doc(doc, doc["version"], actor)
    logger.info("patching: %s added Teams link %s (%s)", actor, meeting["id"], name)
    return meeting


def update_meeting(meeting_id: str, name: str, url: str, actor: str) -> dict:
    name = _clean_name(name, "Link name")
    url = _clean_teams_url(url)
    with _write_lock:
        doc = _load_doc()
        meeting = next((m for m in doc["meetings"] if m["id"] == meeting_id), None)
        if meeting is None:
            raise PatchingNotFoundError(meeting_id)
        _check_unique(name, doc["meetings"], meeting_id, "Teams link")
        meeting.update({"name": name, "url": url, "updated_by": actor, "updated_at": _now_iso()})
        _save_doc(doc, doc["version"], actor)
    logger.info("patching: %s updated Teams link %s (%s)", actor, meeting_id, name)
    return meeting


def delete_meeting(meeting_id: str, actor: str) -> dict:
    with _write_lock:
        doc = _load_doc()
        meeting = next((m for m in doc["meetings"] if m["id"] == meeting_id), None)
        if meeting is None:
            raise PatchingNotFoundError(meeting_id)
        used = sum(1 for p in doc["plans"] if p.get("teams_meeting") == meeting_id)
        if used:
            raise PatchingConflictError(
                f"\"{meeting['name']}\" is used by {used} patching schedule{'s' if used != 1 else ''} - "
                "change those schedules to another link first"
            )
        doc["meetings"] = [m for m in doc["meetings"] if m["id"] != meeting_id]
        _save_doc(doc, doc["version"], actor)
    logger.info("patching: %s deleted Teams link %s (%s)", actor, meeting_id, meeting["name"])
    return meeting
