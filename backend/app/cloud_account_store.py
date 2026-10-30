"""
Cloud account registry — the single source of truth for which AWS accounts
(and Azure subscriptions) the Portal reports on.
(.kiro/specs/cloud-account-onboarding)

Storage: ENV=local -> SQLite (CloudAccountEntry / CloudAccountAuditEntry in
database.py); otherwise DynamoDB tables job-portal-cloud-accounts
(PK account_key) and job-portal-cloud-account-audit (PK account_key,
SK sk). The backend is chosen per call (not at import) so tests can switch
it without reloading the module.

An entry is a plain dict:
    account_key      "{provider}#{identifier}"
    provider         "aws" | "azure"            ("aliyun" is reserved)
    identifier       12-digit AWS account ID / Azure subscription GUID
    display_name     1-100 chars
    business_unit    BU-A / BU-B / BU-C / BU-D / Other / ...
    status           pending | connected | failed | offboarded
    access_mode      "legacy" (PortalReadOnlyRole via Core account keys, unchanged),
                     "infraops" (JobPortal-CrossAccount-InfraOps-XXXX), "arm" (Azure)
    scope_core / scope_billing   AWS only — which Consumer_Modules see it
    external_id / role_name      infraops only. external_id is never put in audit.
    seeded           True for entries created by the built-in seed
    never_connected  True until the first successful test (drafts only)
    protected        offboarding needs the typed risk confirmation
    last_verified_at / last_error / created_* / updated_*

Consumer modules do not call this directly for their account lists: they
keep using ACCOUNTS / ACCOUNT_BU / ACCOUNT_ALIAS, which are LiveAccountMap
views over snapshot() (see bottom of this file).

Failure policy: a registry that cannot be read or is still empty never
produces an empty account list — the last good snapshot is kept, otherwise
the built-in seed (cloud_account_seed) is used, so every account-based page
keeps today's behaviour.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Iterator, Optional

from . import cloud_account_seed as seed

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNTS_TABLE_NAME = os.environ.get("CLOUD_ACCOUNTS_TABLE_NAME", "job-portal-cloud-accounts")
AUDIT_TABLE_NAME = os.environ.get("CLOUD_ACCOUNT_AUDIT_TABLE_NAME", "job-portal-cloud-account-audit")

PROVIDERS = ("aws", "azure")
STATUSES = ("pending", "connected", "failed", "offboarded")

# A change made by this task is visible to it immediately; other ECS tasks
# pick it up within this many seconds (requirement: within 60 s).
SNAPSHOT_TTL_SECONDS = 30.0

# Keys that must never be copied into an audit entry.
_SECRET_KEYS = frozenset({"external_id"})


def _use_local() -> bool:
    return os.environ.get("ENV", "local") == "local"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_key(provider: str, identifier: str) -> str:
    return f"{provider}#{identifier}"


# ── Local (SQLite) backend ───────────────────────────────────────────────

def _local_session():
    from .database import SessionLocal

    return SessionLocal()


def _local_list() -> list[dict]:
    from .database import CloudAccountEntry

    db = _local_session()
    try:
        return [dict(r.data) for r in db.query(CloudAccountEntry).all()]
    finally:
        db.close()


def _local_get(key: str) -> Optional[dict]:
    from .database import CloudAccountEntry

    db = _local_session()
    try:
        r = db.query(CloudAccountEntry).filter(CloudAccountEntry.account_key == key).first()
        return dict(r.data) if r else None
    finally:
        db.close()


def _local_put_new(entry: dict) -> bool:
    from .database import CloudAccountEntry

    db = _local_session()
    try:
        if db.query(CloudAccountEntry).filter(CloudAccountEntry.account_key == entry["account_key"]).first():
            return False
        db.add(CloudAccountEntry(account_key=entry["account_key"], data=entry))
        db.commit()
        return True
    finally:
        db.close()


def _local_update(key: str, fields: dict, expect: Optional[dict]) -> Optional[dict]:
    from .database import CloudAccountEntry

    db = _local_session()
    try:
        r = db.query(CloudAccountEntry).filter(CloudAccountEntry.account_key == key).first()
        if not r:
            return None
        data = dict(r.data)
        if expect and any(data.get(k) != v for k, v in expect.items()):
            return None
        data.update(fields)
        r.data = data  # reassign so SQLAlchemy sees the JSON change
        db.commit()
        return data
    finally:
        db.close()


def _local_delete(key: str, require: Optional[dict]) -> bool:
    from .database import CloudAccountEntry

    db = _local_session()
    try:
        r = db.query(CloudAccountEntry).filter(CloudAccountEntry.account_key == key).first()
        if not r:
            return False
        if require and any(r.data.get(k) != v for k, v in require.items()):
            return False
        db.delete(r)
        db.commit()
        return True
    finally:
        db.close()


def _local_audit_add(item: dict) -> None:
    from .database import CloudAccountAuditEntry

    db = _local_session()
    try:
        db.add(CloudAccountAuditEntry(account_key=item["account_key"], sk=item["sk"], data=item))
        db.commit()
    finally:
        db.close()


def _local_audit_list(key: str, limit: int) -> list[dict]:
    from .database import CloudAccountAuditEntry

    db = _local_session()
    try:
        rows = (
            db.query(CloudAccountAuditEntry)
            .filter(CloudAccountAuditEntry.account_key == key)
            .order_by(CloudAccountAuditEntry.sk.desc())
            .limit(limit)
            .all()
        )
        return [dict(r.data) for r in rows]
    finally:
        db.close()


# ── DynamoDB backend ─────────────────────────────────────────────────────

def _table(name: str):
    import boto3

    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(name)


def _is_conditional_failure(e: Exception) -> bool:
    return getattr(e, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _ddb_list() -> list[dict]:
    table = _table(ACCOUNTS_TABLE_NAME)
    resp = table.scan()
    items = list(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return items


def _ddb_get(key: str) -> Optional[dict]:
    return _table(ACCOUNTS_TABLE_NAME).get_item(Key={"account_key": key}).get("Item")


def _ddb_put_new(entry: dict) -> bool:
    from botocore.exceptions import ClientError

    try:
        _table(ACCOUNTS_TABLE_NAME).put_item(
            Item=entry, ConditionExpression="attribute_not_exists(account_key)"
        )
        return True
    except ClientError as e:
        if _is_conditional_failure(e):
            return False
        raise


def _ddb_update(key: str, fields: dict, expect: Optional[dict]) -> Optional[dict]:
    from botocore.exceptions import ClientError

    names: dict[str, str] = {}
    values: dict[str, object] = {}
    sets = []
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"] = k
        values[f":f{i}"] = v
        sets.append(f"#f{i} = :f{i}")
    conds = ["attribute_exists(account_key)"]
    for i, (k, v) in enumerate((expect or {}).items()):
        names[f"#e{i}"] = k
        values[f":e{i}"] = v
        conds.append(f"#e{i} = :e{i}")
    try:
        resp = _table(ACCOUNTS_TABLE_NAME).update_item(
            Key={"account_key": key},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression=" AND ".join(conds),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if _is_conditional_failure(e):
            return None
        raise
    return resp.get("Attributes")


def _ddb_delete(key: str, require: Optional[dict]) -> bool:
    from botocore.exceptions import ClientError

    names = {"#k": "account_key"}
    values: dict[str, object] = {}
    conds = ["attribute_exists(#k)"]
    for i, (k, v) in enumerate((require or {}).items()):
        names[f"#r{i}"] = k
        values[f":r{i}"] = v
        conds.append(f"#r{i} = :r{i}")
    kwargs = {
        "Key": {"account_key": key},
        "ConditionExpression": " AND ".join(conds),
        "ExpressionAttributeNames": names,
    }
    if values:
        kwargs["ExpressionAttributeValues"] = values
    try:
        _table(ACCOUNTS_TABLE_NAME).delete_item(**kwargs)
        return True
    except ClientError as e:
        if _is_conditional_failure(e):
            return False
        raise


def _ddb_audit_add(item: dict) -> None:
    _table(AUDIT_TABLE_NAME).put_item(Item=item)


def _ddb_audit_list(key: str, limit: int) -> list[dict]:
    from boto3.dynamodb.conditions import Key

    resp = _table(AUDIT_TABLE_NAME).query(
        KeyConditionExpression=Key("account_key").eq(key),
        ScanIndexForward=False,
        Limit=limit,
    )
    return list(resp.get("Items", []))


# ── Public raw access (uncached) ─────────────────────────────────────────

def list_all() -> list[dict]:
    """Every registry entry straight from storage (no cache, no fallback)."""
    return _local_list() if _use_local() else _ddb_list()


def get(provider: str, identifier: str) -> Optional[dict]:
    key = make_key(provider, identifier)
    return _local_get(key) if _use_local() else _ddb_get(key)


def put_new(entry: dict) -> bool:
    """Insert only if (provider, identifier) does not exist yet. Returns False if it does."""
    ok = _local_put_new(entry) if _use_local() else _ddb_put_new(entry)
    if ok:
        invalidate()
    return ok


def update_fields(
    provider: str, identifier: str, fields: dict, *, actor: Optional[str] = None,
    expect: Optional[dict] = None,
) -> Optional[dict]:
    """Targeted update of the given fields only. `expect` is a dict of
    current-value conditions (e.g. {"status": "pending"}); if any differs, or
    the entry is missing, nothing is written and None is returned."""
    fields = dict(fields)
    fields["updated_at"] = _now_iso()
    if actor:
        fields["updated_by"] = actor
    key = make_key(provider, identifier)
    out = _local_update(key, fields, expect) if _use_local() else _ddb_update(key, fields, expect)
    if out is not None:
        invalidate()
    return out


def delete_draft(provider: str, identifier: str) -> bool:
    """Delete an entry only if it has never connected and was not seeded.
    An entry that was ever connected can only be offboarded."""
    key = make_key(provider, identifier)
    require = {"never_connected": True, "seeded": False}
    ok = _local_delete(key, require) if _use_local() else _ddb_delete(key, require)
    if ok:
        invalidate()
    return ok


# ── Audit ────────────────────────────────────────────────────────────────

def _scrub(value):
    """Copy of a dict without secret keys (External_Ids); other types unchanged."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in _SECRET_KEYS}
    return value


def append_audit(
    account_key: str, *, actor: str, action: str,
    before: Optional[dict] = None, after: Optional[dict] = None, note: Optional[str] = None,
) -> None:
    """Append one audit entry. Never raises: an audit write failure is logged
    and must not undo or block the change it describes."""
    try:
        item = {
            "account_key": account_key,
            "sk": f"{_now_iso()}#{uuid.uuid4().hex[:8]}",
            "actor": actor,
            "action": action,
        }
        if before is not None:
            item["before"] = _scrub(before)
        if after is not None:
            item["after"] = _scrub(after)
        if note:
            item["note"] = note[:500]
        _local_audit_add(item) if _use_local() else _ddb_audit_add(item)
    except Exception as e:
        logger.error("cloud_account_store: audit write failed for %s (%s): %s", account_key, action, e)


def list_audit(provider: str, identifier: str, limit: int = 50) -> list[dict]:
    key = make_key(provider, identifier)
    return _local_audit_list(key, limit) if _use_local() else _ddb_audit_list(key, limit)


# ── Entry construction ───────────────────────────────────────────────────

def new_entry(
    provider: str, identifier: str, display_name: str, *, actor: str,
    business_unit: str = seed.DEFAULT_BUSINESS_UNIT, status: str = "pending",
    access_mode: str = "infraops", scope_core: bool = True, scope_billing: bool = True,
    seeded: bool = False, never_connected: bool = True, external_id: Optional[str] = None,
    role_name: Optional[str] = None,
) -> dict:
    now = _now_iso()
    entry = {
        "account_key": make_key(provider, identifier),
        "provider": provider,
        "identifier": identifier,
        "display_name": display_name,
        "business_unit": business_unit,
        "status": status,
        "access_mode": access_mode,
        "seeded": seeded,
        "never_connected": never_connected,
        "protected": provider == "aws" and identifier in seed.PROTECTED_ACCOUNT_IDS,
        "last_verified_at": None,
        "last_error": None,
        "created_by": actor,
        "created_at": now,
        "updated_by": actor,
        "updated_at": now,
    }
    if provider == "aws":
        entry["scope_core"] = scope_core
        entry["scope_billing"] = scope_billing
    if external_id:
        entry["external_id"] = external_id
    if role_name:
        entry["role_name"] = role_name
    return entry


def _builtin_entries() -> list[dict]:
    """The built-in seed as registry entries (used for the fallback and the first-start seed)."""
    out = []
    for account_id, name, bu, in_core in seed.AWS_SEED:
        out.append(new_entry(
            "aws", account_id, name, actor="system:seed", business_unit=bu, status="pending",
            access_mode="legacy", scope_core=in_core, scope_billing=True,
            seeded=True, never_connected=False,
        ))
    return out


# ── Visibility ───────────────────────────────────────────────────────────

def is_visible(entry: dict) -> bool:
    """Whether Consumer_Modules may see this entry: not offboarded, and either
    established (seeded / legacy access — shown whatever its last test said,
    exactly as before the registry) or connected. A newly onboarded account is
    therefore invisible until its connection test succeeds."""
    if entry.get("status") == "offboarded":
        return False
    established = entry.get("access_mode") == "legacy" or bool(entry.get("seeded"))
    return established or entry.get("status") == "connected"


# ── Snapshot cache ───────────────────────────────────────────────────────

_ORDER = {account_id: i for i, (account_id, *_rest) in enumerate(seed.AWS_SEED)}

_lock = threading.Lock()
_snap: dict = {"entries": None, "loaded_at": 0.0, "version": 0, "from_storage": False}


def invalidate() -> None:
    """Drop the cached snapshot so the next read reloads (this task only)."""
    with _lock:
        _snap["loaded_at"] = 0.0


def _sort_key(e: dict):
    idx = _ORDER.get(e.get("identifier")) if e.get("provider") == "aws" else None
    return (0, idx, "") if idx is not None else (1, 0, e.get("created_at") or "")


def _load() -> tuple[list[dict], bool]:
    """(entries, from_storage). Falls back as described in the module docstring."""
    try:
        rows = list_all()
    except Exception as e:
        logger.error("cloud_account_store: registry unreadable (%s); using last good / built-in list", e)
        if _snap["entries"] is not None:
            return _snap["entries"], _snap["from_storage"]
        return _builtin_entries(), False
    if not rows:
        return _builtin_entries(), False
    return sorted(rows, key=_sort_key), True


def snapshot() -> list[dict]:
    """All entries (every status), cached for SNAPSHOT_TTL_SECONDS. Callers must not mutate them."""
    now = time.monotonic()
    with _lock:
        if _snap["entries"] is not None and now - _snap["loaded_at"] < SNAPSHOT_TTL_SECONDS:
            return _snap["entries"]
    entries, from_storage = _load()
    with _lock:
        _snap["entries"] = entries
        _snap["from_storage"] = from_storage
        _snap["loaded_at"] = time.monotonic()
        _snap["version"] += 1
        return entries


def snapshot_version() -> int:
    snapshot()
    return _snap["version"]


def active_accounts(provider: str = "aws", scope: Optional[str] = None) -> list[dict]:
    """Visible entries of a provider, optionally only those with the given
    scope flag set ("core" or "billing", AWS only)."""
    out = []
    for e in snapshot():
        if e.get("provider") != provider or not is_visible(e):
            continue
        if scope and not e.get(f"scope_{scope}", False):
            continue
        out.append(e)
    return out


def access_mode(account_id: str) -> str:
    """How an AWS account is reached. Unknown accounts report "legacy" so
    callers fall through to the existing code path unchanged."""
    for e in snapshot():
        if e.get("provider") == "aws" and e.get("identifier") == account_id:
            return e.get("access_mode") or "legacy"
    return "legacy"


def find(provider: str, identifier: str) -> Optional[dict]:
    """Entry from the cached snapshot (any status), or None."""
    key = make_key(provider, identifier)
    for e in snapshot():
        if e.get("account_key") == key:
            return e
    return None


# ── Seed ─────────────────────────────────────────────────────────────────

def _seed_azure_subscription() -> None:
    """Register the subscription already configured for the Azure Resources
    page so it keeps working without an Admin action. Any failure (no secret,
    local dev) is ignored."""
    try:
        from . import azure_monitor

        subscription_id = (azure_monitor._get_credentials() or {}).get("subscription_id")
    except Exception as e:
        logger.info("cloud_account_store: no Azure subscription to seed (%s)", type(e).__name__)
        return
    if not subscription_id:
        return
    entry = new_entry(
        "azure", subscription_id, "Azure (default subscription)", actor="system:seed",
        status="pending", access_mode="arm", seeded=True, never_connected=False,
    )
    put_new(entry)


def seed_if_needed() -> int:
    """Create any missing built-in entry (conditional put per entry, so an
    Admin's rename, BU or scope change is never overwritten), then the current
    Azure subscription. Returns the number of AWS entries created. Never raises."""
    created = 0
    try:
        for entry in _builtin_entries():
            if put_new(entry):
                created += 1
        _seed_azure_subscription()
    except Exception as e:
        logger.error("cloud_account_store: seeding failed: %s", e)
    if created:
        logger.info("cloud_account_store: seeded %d AWS account(s)", created)
    return created


# ── Live maps ────────────────────────────────────────────────────────────

class LiveAccountMap(Mapping):
    """Read-only dict-like view of the registry, keyed by AWS account ID.

    Replaces the hard-coded ACCOUNTS / ACCOUNT_ALIAS / ACCOUNT_BU dicts without
    touching the many modules that import and read them: every lookup sees the
    current (cached) registry, so no module can keep a stale copy.

      value="name"  -> display name        value="bu" -> business unit
      scope="core"  -> accounts with the core flag (old cost_explorer.ACCOUNTS)
      scope="billing" -> accounts with the billing flag (old ACCOUNT_ALIAS)
      scope=None    -> every visible AWS account
    """

    def __init__(self, value: str, scope: Optional[str]):
        if value not in ("name", "bu"):
            raise ValueError("value must be 'name' or 'bu'")
        self._value = value
        self._scope = scope
        self._cache: tuple[int, dict] = (-1, {})

    def _data(self) -> dict:
        version = snapshot_version()
        if self._cache[0] != version:
            field = "display_name" if self._value == "name" else "business_unit"
            data = {e["identifier"]: e.get(field) for e in active_accounts("aws", self._scope)}
            self._cache = (version, data)
        return self._cache[1]

    def __getitem__(self, key):
        return self._data()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())

    def __repr__(self) -> str:
        return f"LiveAccountMap({self._value!r}, scope={self._scope!r}, {self._data()!r})"
