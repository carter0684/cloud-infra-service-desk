"""
Cloud account use-cases for the Administration page
(.kiro/specs/cloud-account-onboarding, task 4).

Routes (main.py) stay thin and call these functions. Every function takes the
acting Admin's email, validates its input, writes through cloud_account_store,
and appends an audit entry. Failures the Admin should see are raised as
ServiceError(message, status) — the message is written here and is safe to
show (no raw AWS/Azure text, credentials or External_Ids).
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from . import cloud_account_access as access
from . import cloud_account_seed as seed
from . import cloud_account_store as store

logger = logging.getLogger(__name__)

# The phrase an Admin must type to offboard the Portal's own host account or
# the Core account. Compared exactly (case-sensitive, final full stop).
RISK_PHRASE = "I understand the risk and agree to offboard."

MAX_NAME_LENGTH = 100
TEST_RATE_LIMIT_PER_MINUTE = 30
TEST_ALL_CONCURRENCY = 8

_AWS_ID_RE = re.compile(r"^\d{12}$")
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Fields an Admin may change with update(); scope flags are AWS only.
_EDITABLE = ("display_name", "business_unit", "scope_core", "scope_billing")
# What audit entries record about an account (never the External_Id).
_AUDIT_FIELDS = ("display_name", "business_unit", "status", "access_mode", "scope_core", "scope_billing")


class ServiceError(Exception):
    """An error with a message safe to show the Admin and an HTTP status hint."""

    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status
        super().__init__(message)


# ── Validation ───────────────────────────────────────────────────────────

def validate_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p == "aliyun":
        raise ServiceError("Alibaba Cloud (Aliyun) onboarding is not available yet.", 400)
    if p not in store.PROVIDERS:
        raise ServiceError("Unknown provider. Use 'aws' or 'azure'.", 400)
    return p


def validate_identifier(provider: str, identifier: str) -> str:
    ident = (identifier or "").strip()
    if provider == "aws":
        if not _AWS_ID_RE.match(ident):
            raise ServiceError("An AWS account ID is exactly 12 digits.", 400)
    elif not _GUID_RE.match(ident):
        raise ServiceError("An Azure subscription ID is a GUID like 00000000-0000-0000-0000-000000000000.", 400)
    return ident.lower() if provider == "azure" else ident


def validate_display_name(name: str) -> str:
    n = (name or "").strip()
    if not n:
        raise ServiceError("Enter a display name.", 400)
    if len(n) > MAX_NAME_LENGTH:
        raise ServiceError(f"The display name can be at most {MAX_NAME_LENGTH} characters.", 400)
    if _CONTROL_RE.search(n):
        raise ServiceError("The display name contains characters that are not allowed.", 400)
    return n


def known_business_units() -> list[str]:
    used = {e.get("business_unit") for e in store.snapshot() if e.get("business_unit")}
    return sorted(set(seed.KNOWN_BUSINESS_UNITS) | used)


def validate_business_unit(bu: Optional[str], confirm_new: bool = False) -> str:
    b = (bu or "").strip() or seed.DEFAULT_BUSINESS_UNIT
    if len(b) > 30 or _CONTROL_RE.search(b):
        raise ServiceError("The business unit name is not valid.", 400)
    if b not in known_business_units() and not confirm_new:
        raise ServiceError(
            f"'{b}' is a new business unit. Confirm that you want to create it.", 400
        )
    return b


def _duplicate_name_warning(provider: str, name: str, exclude_identifier: str) -> Optional[str]:
    low = name.lower()
    for e in store.snapshot():
        if (e.get("provider") == provider and e.get("identifier") != exclude_identifier
                and (e.get("display_name") or "").lower() == low):
            return f"Another {provider.upper()} account is already called '{name}'."
    return None


# ── Rate limit ───────────────────────────────────────────────────────────

_rate_lock = threading.Lock()
_rate_calls: dict[str, deque] = {}


def check_test_rate(actor: str) -> None:
    """At most TEST_RATE_LIMIT_PER_MINUTE test requests per Admin per minute (per
    task; one 'Re-test all' request counts once). Raises ServiceError(429)."""
    now = time.monotonic()
    with _rate_lock:
        q = _rate_calls.setdefault(actor, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= TEST_RATE_LIMIT_PER_MINUTE:
            raise ServiceError("Too many connection tests. Wait a minute and try again.", 429)
        q.append(now)


# ── Presentation ─────────────────────────────────────────────────────────

def public_entry(e: dict) -> dict:
    """An entry safe to send to the browser: never includes the External_Id."""
    out = {k: v for k, v in e.items() if k != "external_id"}
    out["external_id_set"] = bool(e.get("external_id"))
    out["visible"] = store.is_visible(e)
    if e.get("provider") == "aws" and e.get("access_mode") == "infraops":
        out["role_arn"] = access.role_arn_for(e["identifier"], e.get("role_name"))
    return out


def _audit_view(e: Optional[dict]) -> Optional[dict]:
    if e is None:
        return None
    return {k: e.get(k) for k in _AUDIT_FIELDS if k in e}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_or_404(provider: str, identifier: str) -> dict:
    entry = store.get(provider, identifier)
    if not entry:
        raise ServiceError("That account is not in the registry.", 404)
    return entry


# ── Listing ──────────────────────────────────────────────────────────────

def list_with_counts() -> dict:
    """Every entry (any status) with counts per provider and status. Reloads the
    registry first so the Admin always sees current data."""
    store.invalidate()
    entries = store.snapshot()
    counts: dict[str, dict] = {p: {s: 0 for s in store.STATUSES} | {"total": 0} for p in store.PROVIDERS}
    for e in entries:
        c = counts.setdefault(e.get("provider"), {s: 0 for s in store.STATUSES} | {"total": 0})
        c[e.get("status")] = c.get(e.get("status"), 0) + 1
        c["total"] += 1
    return {
        "accounts": [public_entry(e) for e in entries],
        "counts": counts,
        "business_units": known_business_units(),
    }


def detail(provider: str, identifier: str) -> dict:
    provider = validate_provider(provider)
    identifier = validate_identifier(provider, identifier)
    entry = _get_or_404(provider, identifier)
    return {"account": public_entry(entry), "audit": store.list_audit(provider, identifier, 50)}


# ── AWS onboarding ───────────────────────────────────────────────────────

def _setup_payload(entry: dict) -> dict:
    """What the setup screen needs. The ONLY place the External_Id leaves the
    service, and only for Admins."""
    try:
        trusted = access.task_role_arn()
    except access.AccessError as e:
        raise ServiceError(e.message, 503)
    return {
        "account_id": entry["identifier"],
        "role_name": entry.get("role_name") or access.role_name_for(entry["identifier"]),
        "role_arn": access.role_arn_for(entry["identifier"], entry.get("role_name")),
        "external_id": entry.get("external_id"),
        "portal_role_arn": trusted,
        "status": entry.get("status"),
        "template_filename": f"JobPortal-InfraOps-Role-{entry['identifier']}.yaml",
    }


def prepare_aws(account_id: str, actor: str) -> dict:
    """Start adding an AWS account: create (or reuse) a draft with its own
    External_Id and return the setup data (role name, External_Id, Portal role
    ARN). Idempotent — starting again for the same draft returns the same
    External_Id, so a template that was already downloaded stays valid."""
    account_id = validate_identifier("aws", account_id)
    existing = store.get("aws", account_id)
    if existing:
        if existing.get("access_mode") == "infraops" and existing.get("status") != "offboarded" \
                and existing.get("external_id"):
            if existing.get("status") == "connected":
                raise ServiceError("That account is already connected.", 409)
            return _setup_payload(existing)
        if existing.get("status") == "offboarded":
            raise ServiceError("That account is offboarded. Use 'Onboard again' on its row.", 409)
        raise ServiceError("That account is already registered.", 409)

    entry = store.new_entry(
        "aws", account_id, f"AWS {account_id}", actor=actor, status="pending",
        access_mode="infraops", never_connected=True,
        external_id=secrets.token_urlsafe(32), role_name=access.role_name_for(account_id),
    )
    if not store.put_new(entry):  # lost a race with another request: reuse the winner
        return _setup_payload(_get_or_404("aws", account_id))
    store.append_audit(entry["account_key"], actor=actor, action="prepare", after=_audit_view(entry))
    return _setup_payload(entry)


def get_setup(account_id: str) -> dict:
    account_id = validate_identifier("aws", account_id)
    entry = _get_or_404("aws", account_id)
    if entry.get("access_mode") != "infraops" or not entry.get("external_id"):
        raise ServiceError("That account does not use the InfraOps role.", 409)
    return _setup_payload(entry)


def template_for(account_id: str, actor: str) -> tuple[str, str]:
    """(filename, CloudFormation YAML) for a draft/failed/connected infraops account."""
    setup = get_setup(account_id)
    entry = _get_or_404("aws", setup["account_id"])
    if entry.get("status") == "offboarded":
        raise ServiceError("That account is offboarded.", 409)
    try:
        text = access.render_template(entry, task_role=setup["portal_role_arn"])
    except ValueError:
        logger.error("cloud_account_service: template render refused for %s", account_id)
        raise ServiceError("The template could not be generated for this account.", 500)
    store.append_audit(entry["account_key"], actor=actor, action="template_download")
    return setup["template_filename"], text


def _run_test(entry: dict) -> dict:
    if entry.get("provider") == "aws":
        return access.test_aws_connection(entry)
    from . import azure_monitor

    fn = getattr(azure_monitor, "test_azure_subscription", None)
    if fn is None:
        return {"ok": False, "code": "not_available", "message": "Azure connection test is not available in this version."}
    return fn(entry["identifier"])


def _apply_test_result(entry: dict, result: dict, actor: str, *, keep_status: bool = False) -> dict:
    """Store a test outcome. `keep_status` leaves the status alone (used when an
    offboarded account is re-tested and fails). Returns the updated entry."""
    old_status = entry.get("status")
    fields = {"last_verified_at": _now_iso()}
    if result["ok"]:
        fields.update(last_error=None, last_error_code=None)
        if not keep_status:
            fields.update(status="connected", never_connected=False)
    else:
        fields.update(last_error=result["message"], last_error_code=result["code"])
        if not keep_status:
            fields.update(status="failed")
    updated = store.update_fields(entry["provider"], entry["identifier"], fields, actor=actor) or {**entry, **fields}
    if updated.get("status") != old_status:
        store.append_audit(
            entry["account_key"], actor=actor, action="status_change",
            before={"status": old_status}, after={"status": updated.get("status")}, note=result["code"],
        )
    if entry.get("provider") == "aws":
        access.invalidate(entry["identifier"])
    return updated


def connect_aws(account_id: str, display_name: str, actor: str, *,
                business_unit: Optional[str] = None, confirm_new_bu: bool = False) -> dict:
    """Save the name / BU of a prepared account and run the connection test."""
    account_id = validate_identifier("aws", account_id)
    entry = _get_or_404("aws", account_id)
    if entry.get("access_mode") != "infraops":
        raise ServiceError("That account does not use the InfraOps role.", 409)
    if entry.get("status") == "offboarded":
        raise ServiceError("That account is offboarded. Use 'Onboard again' on its row.", 409)
    name = validate_display_name(display_name)
    bu = validate_business_unit(business_unit, confirm_new_bu)
    check_test_rate(actor)

    warning = _duplicate_name_warning("aws", name, account_id)
    changed = store.update_fields("aws", account_id, {"display_name": name, "business_unit": bu}, actor=actor)
    entry = changed or entry
    if entry.get("display_name") != name:  # update failed to apply
        raise ServiceError("The account could not be saved. Try again.", 503)

    result = _run_test(entry)
    updated = _apply_test_result(entry, result, actor)
    store.append_audit(
        entry["account_key"], actor=actor, action="connect",
        after=_audit_view(updated), note=result["code"],
    )
    return {"account": public_entry(updated), "result": result, "warning": warning}


# ── Azure onboarding ─────────────────────────────────────────────────────

def add_azure(subscription_id: str, display_name: str, actor: str, *,
              business_unit: Optional[str] = None, confirm_new_bu: bool = False) -> dict:
    sub = validate_identifier("azure", subscription_id)
    name = validate_display_name(display_name)
    bu = validate_business_unit(business_unit, confirm_new_bu)
    if store.get("azure", sub):
        raise ServiceError("That subscription is already registered.", 409)
    check_test_rate(actor)

    entry = store.new_entry(
        "azure", sub, name, actor=actor, business_unit=bu, status="pending",
        access_mode="arm", never_connected=True,
    )
    if not store.put_new(entry):
        raise ServiceError("That subscription is already registered.", 409)
    store.append_audit(entry["account_key"], actor=actor, action="create", after=_audit_view(entry))

    result = _run_test(entry)
    updated = _apply_test_result(entry, result, actor)
    return {
        "account": public_entry(updated), "result": result,
        "warning": _duplicate_name_warning("azure", name, sub),
    }


# ── Edit ─────────────────────────────────────────────────────────────────

def update(provider: str, identifier: str, changes: dict, actor: str, *, confirm_new_bu: bool = False) -> dict:
    provider = validate_provider(provider)
    identifier = validate_identifier(provider, identifier)
    entry = _get_or_404(provider, identifier)

    fields: dict = {}
    if "display_name" in changes:
        fields["display_name"] = validate_display_name(changes["display_name"])
    if "business_unit" in changes:
        fields["business_unit"] = validate_business_unit(changes["business_unit"], confirm_new_bu)
    for flag in ("scope_core", "scope_billing"):
        if flag in changes:
            if provider != "aws":
                raise ServiceError("Scopes only apply to AWS accounts.", 400)
            if not isinstance(changes[flag], bool):
                raise ServiceError(f"{flag} must be true or false.", 400)
            fields[flag] = changes[flag]
    unknown = set(changes) - set(_EDITABLE)
    if unknown:
        raise ServiceError("Only the display name, business unit and scopes can be edited.", 400)
    if not fields:
        raise ServiceError("Nothing to change.", 400)

    before = {k: entry.get(k) for k in fields}
    if all(entry.get(k) == v for k, v in fields.items()):
        return {"account": public_entry(entry), "warning": None}
    updated = store.update_fields(provider, identifier, fields, actor=actor)
    if updated is None:
        raise ServiceError("The account could not be saved. Try again.", 503)
    store.append_audit(entry["account_key"], actor=actor, action="update", before=before, after=fields)
    warning = _duplicate_name_warning(provider, fields["display_name"], identifier) if "display_name" in fields else None
    return {"account": public_entry(updated), "warning": warning}


# ── Testing ──────────────────────────────────────────────────────────────

def test_one(provider: str, identifier: str, actor: str) -> dict:
    provider = validate_provider(provider)
    identifier = validate_identifier(provider, identifier)
    entry = _get_or_404(provider, identifier)
    if entry.get("status") == "offboarded":
        raise ServiceError("That account is offboarded. Onboard it again first.", 409)
    check_test_rate(actor)
    result = _run_test(entry)
    updated = _apply_test_result(entry, result, actor)
    return {"account": public_entry(updated), "result": result}


def test_all(actor: str) -> dict:
    """Re-test every non-offboarded account in parallel (bounded). One failing or
    slow account never blocks the others."""
    check_test_rate(actor)
    store.invalidate()
    targets = [e for e in store.snapshot() if e.get("status") != "offboarded"]

    def one(entry: dict) -> dict:
        try:
            result = _run_test(entry)
            updated = _apply_test_result(entry, result, actor)
            return {"account": public_entry(updated), "result": result}
        except Exception:  # pragma: no cover - _run_test never raises; belt and braces
            logger.exception("cloud_account_service: test_all failed for %s", entry.get("account_key"))
            return {"account": public_entry(entry),
                    "result": {"ok": False, "code": "unexpected", "message": "Unexpected error while testing."}}

    with ThreadPoolExecutor(max_workers=TEST_ALL_CONCURRENCY) as pool:
        results = list(pool.map(one, targets))
    ok = sum(1 for r in results if r["result"]["ok"])
    return {"results": results, "tested": len(results), "connected": ok, "failed": len(results) - ok}


# ── Offboard / onboard / discard ─────────────────────────────────────────

def offboard(provider: str, identifier: str, actor: str, *, risk_confirmation: Optional[str] = None) -> dict:
    """Remove an account from all Portal reporting; history is kept and nothing is
    touched in the cloud account. The Portal's own host account and the Core account
    account need the exact RISK_PHRASE, checked here (not only in the browser)."""
    provider = validate_provider(provider)
    identifier = validate_identifier(provider, identifier)
    entry = _get_or_404(provider, identifier)
    if entry.get("status") == "offboarded":
        raise ServiceError("That account is already offboarded.", 409)

    protected = bool(entry.get("protected"))
    if protected and risk_confirmation != RISK_PHRASE:
        raise ServiceError(
            "Other Portal functions depend on this account. To offboard it, type exactly: " + RISK_PHRASE, 400
        )

    fields = {
        "status": "offboarded", "offboarded_at": _now_iso(), "offboarded_by": actor,
        "status_before_offboard": entry.get("status"),
    }
    updated = store.update_fields(provider, identifier, fields, actor=actor)
    if updated is None:
        raise ServiceError("The account could not be saved. Try again.", 503)
    if provider == "aws":
        access.invalidate(identifier)
    store.append_audit(
        entry["account_key"], actor=actor, action="offboard",
        before={"status": entry.get("status")}, after={"status": "offboarded"},
        note="risk confirmation given" if protected else None,
    )
    return {"account": public_entry(updated)}


def onboard(provider: str, identifier: str, actor: str) -> dict:
    """Bring an offboarded account back. It is tested first and only restored when
    the test succeeds; otherwise it stays offboarded with the reason recorded."""
    provider = validate_provider(provider)
    identifier = validate_identifier(provider, identifier)
    entry = _get_or_404(provider, identifier)
    if entry.get("status") != "offboarded":
        raise ServiceError("That account is not offboarded.", 409)
    check_test_rate(actor)

    result = _run_test(entry)
    updated = _apply_test_result(entry, result, actor, keep_status=not result["ok"])
    store.append_audit(
        entry["account_key"], actor=actor, action="onboard",
        before={"status": "offboarded"}, after={"status": updated.get("status")}, note=result["code"],
    )
    return {"account": public_entry(updated), "result": result}


def discard_draft(provider: str, identifier: str, actor: str) -> dict:
    """Delete an entry that never connected (and was not part of the built-in
    list). Anything that has ever been connected can only be offboarded."""
    provider = validate_provider(provider)
    identifier = validate_identifier(provider, identifier)
    entry = _get_or_404(provider, identifier)
    if not store.delete_draft(provider, identifier):
        raise ServiceError(
            "Only an account that has never connected can be discarded. Offboard it instead.", 409
        )
    if provider == "aws":
        access.invalidate(identifier)
    store.append_audit(entry["account_key"], actor=actor, action="discard", before=_audit_view(entry))
    return {"discarded": True}


# ── Startup ──────────────────────────────────────────────────────────────

def verify_pending_established(concurrency: int = 8) -> int:
    """Test the established (seeded / legacy) accounts that have not been checked
    yet, in the background after the first seed. Each result is written only if
    the entry is still `pending`, so running on several ECS tasks at once, or
    after an Admin already tested or changed an account, never overwrites
    anything. The outcome is informational for legacy accounts (they stay
    visible either way). Returns how many entries were updated. Never raises."""
    try:
        targets = [
            e for e in store.list_all()
            if e.get("status") == "pending" and (e.get("seeded") or e.get("access_mode") == "legacy")
        ]
    except Exception as exc:
        logger.error("cloud_account_service: could not list accounts to verify: %s", exc)
        return 0

    def one(entry: dict) -> bool:
        try:
            result = _run_test(entry)
            fields = {
                "last_verified_at": _now_iso(),
                "last_error": None if result["ok"] else result["message"],
                "last_error_code": None if result["ok"] else result["code"],
                "status": "connected" if result["ok"] else "failed",
            }
            updated = store.update_fields(
                entry["provider"], entry["identifier"], fields, actor="system:startup",
                expect={"status": "pending"},
            )
            if updated is not None:
                store.append_audit(
                    entry["account_key"], actor="system:startup", action="status_change",
                    before={"status": "pending"}, after={"status": fields["status"]}, note=result["code"],
                )
            return updated is not None
        except Exception:
            logger.exception("cloud_account_service: startup verification failed for %s", entry.get("account_key"))
            return False

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        return sum(1 for ok in pool.map(one, targets) if ok)


def bootstrap() -> None:
    """Seed the registry and verify the established accounts. Run from a background
    thread at startup (main.py decides whether to start it: skipped for local
    development unless CLOUD_ACCOUNTS_BOOTSTRAP=1, since it calls AWS and Azure);
    never raises."""
    try:
        store.seed_if_needed()
        verify_pending_established()
    except Exception:
        logger.exception("cloud_account_service: bootstrap failed")


# ── Role permissions view ────────────────────────────────────────────────

def role_permissions(provider: str, identifier: str, actor: str) -> dict:
    """What the role the Portal assumes in an AWS account can do. Only meaningful when
    the Portal can assume the role: if it cannot, the result has ok=False and the same
    explanation a connection test would give. Shares the connection-test rate limit."""
    provider = validate_provider(provider)
    if provider != "aws":
        raise ServiceError("Permission details are available for AWS accounts only.", 400)
    identifier = validate_identifier(provider, identifier)
    entry = _get_or_404(provider, identifier)
    if entry.get("status") == "offboarded":
        raise ServiceError("That account is offboarded. Onboard it again first.", 409)
    if entry.get("access_mode") == "infraops" and not entry.get("external_id"):
        raise ServiceError("This account has not been set up yet.", 409)
    check_test_rate(actor)
    result = access.describe_role_permissions(entry)

    # An account can be reached through more than one role. `permissions` stays the role
    # the registry says the Portal uses (PortalReadOnlyRole, or the InfraOps role onboarded on
    # this page); `roles` lists every role found, so an account that ALSO has the
    # hand-made JobPortal-CrossAccount-InfraOps-XXXX role shows both.
    if entry.get("access_mode") == "infraops":
        primary_name = entry.get("role_name") or access.role_name_for(identifier)
        primary_label, primary_purpose = "InfraOps role", "Everything the Portal reads from this account"
    elif identifier == seed.CORE_ACCOUNT_ID:
        primary_name, primary_label, primary_purpose = None, "IAM user access key", access.READONLY_ROLE_PURPOSE
    else:
        from . import cost_explorer

        primary_name = cost_explorer.READONLY_ROLE_NAME
        primary_label, primary_purpose = "Read-only role (legacy)", access.READONLY_ROLE_PURPOSE
    roles = [{
        "key": "primary", "label": primary_label, "role_name": primary_name,
        "purpose": primary_purpose, "permissions": result,
    }]
    extra_note = ""
    if entry.get("access_mode") != "infraops":
        shared = access.describe_shared_infraops_role(entry)
        roles.append({
            "key": "infraops", "label": "InfraOps role", "role_name": access.role_name_for(identifier),
            "purpose": access.SHARED_INFRAOPS_PURPOSE, "permissions": shared,
        })
        extra_note = f"; infraops={'ok' if shared.get('ok') else shared.get('code')}"

    store.append_audit(
        entry["account_key"], actor=actor, action="permissions_view",
        note=("ok" if result.get("ok") else str(result.get("code"))) + extra_note,
    )
    return {"account": public_entry(entry), "permissions": result, "roles": roles}
