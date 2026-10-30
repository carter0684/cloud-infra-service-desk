"""
Portal user allowlist + role management.

ENV=local: SQLite via SQLAlchemy (PortalUser model in database.py).
ENV!=local: DynamoDB table job-portal-users.

Seed admins (SEED_ADMIN_EMAILS env var, comma-separated) are ALWAYS treated
as allowlisted admins regardless of what's in the store — this guarantees
the founding admins can never lock themselves out,
even on a fresh deploy before the table has any rows, or if someone
accidentally removes themselves via the Admin page.

All other users must exist in the store (added via the Admin page) to be
allowed through get_actor() in auth_alb.py / main.py.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

ENV = os.environ.get("ENV", "local")

# REQUIRED for a first deployment: without at least one seed admin nobody can
# sign in to an empty portal. The default is a placeholder; set the real
# founding admins' e-mails (comma-separated) in the SEED_ADMIN_EMAILS env var.
SEED_ADMIN_EMAILS = set(
    e.strip().lower()
    for e in os.environ.get("SEED_ADMIN_EMAILS", "admin@example.com").split(",")
    if e.strip()
)

# Members seeded into an EMPTY user store on first boot only (so existing
# access granted before the Admin page existed isn't lost). Once the store
# has any rows, this list is ignored — manage users via the Admin page
# from then on.
SEED_MEMBER_EMAILS = set(
    e.strip().lower()
    for e in os.environ.get("SEED_MEMBER_EMAILS", "").split(",")
    if e.strip()
)

# Display names for the seed admins, used until/unless overridden by a
# store row with the same email (store row always wins if present).
#
# Optional: SEED_USER_NAMES="a@x.com=Alice A,b@x.com=Bob B" gives seed users a
# friendly name; otherwise the e-mail itself is shown until they are renamed.
def _parse_name_pairs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            email, name = pair.split("=", 1)
            if email.strip() and name.strip():
                out[email.strip().lower()] = name.strip()
    return out


SEED_ADMIN_NAMES = _parse_name_pairs(os.environ.get("SEED_USER_NAMES", ""))
SEED_MEMBER_NAMES = SEED_ADMIN_NAMES


def is_seed_admin(email: str) -> bool:
    return (email or "").lower().strip() in SEED_ADMIN_EMAILS


# "Portal founder" — a distinct, singular designation from "founding
# admin" above: every founding admin can never be locked out, but one of
# them can additionally be recognised as the portal's founder, purely for
# display purposes (e.g. an extra badge in the Admin page's user table).
# Optional; same env-var pattern as SEED_ADMIN_EMAILS, empty by default.
PORTAL_FOUNDER_EMAILS = set(
    e.strip().lower()
    for e in os.environ.get("PORTAL_FOUNDER_EMAILS", "").split(",")
    if e.strip()
)


def is_portal_founder(email: str) -> bool:
    return (email or "").lower().strip() in PORTAL_FOUNDER_EMAILS


if ENV == "local":
    from .database import SessionLocal, PortalUser as _UserModel

    def _seed_defaults_if_empty():
        db = SessionLocal()
        try:
            if db.query(_UserModel).count() == 0:
                for email in SEED_ADMIN_EMAILS:
                    db.add(_UserModel(
                        email=email,
                        name=SEED_ADMIN_NAMES.get(email, email),
                        role="admin",
                        added_by="system",
                        created_at=datetime.utcnow(),
                    ))
                for email in SEED_MEMBER_EMAILS:
                    if email in SEED_ADMIN_EMAILS:
                        continue
                    db.add(_UserModel(
                        email=email,
                        name=SEED_MEMBER_NAMES.get(email, email),
                        role="member",
                        added_by="system",
                        created_at=datetime.utcnow(),
                    ))
                db.commit()
        finally:
            db.close()

    def _to_dict(u) -> dict:
        return {
            "email": u.email,
            "name": u.name,
            "role": u.role,
            "added_by": u.added_by,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "photo_data_url": u.photo_data_url,
            "groups": u.groups or [],
            "last_login": u.last_login.isoformat() if u.last_login else None,
            "tab_write": u.tab_write or [],
        }

    def list_users() -> list[dict]:
        _seed_defaults_if_empty()
        db = SessionLocal()
        try:
            users = db.query(_UserModel).order_by(_UserModel.created_at.asc()).all()
            return [_to_dict(u) for u in users]
        finally:
            db.close()

    def get_user(email: str) -> Optional[dict]:
        db = SessionLocal()
        try:
            u = db.query(_UserModel).filter(_UserModel.email == email.lower().strip()).first()
            return _to_dict(u) if u else None
        finally:
            db.close()

    def upsert_user(email: str, name: str, role: str, added_by: str) -> dict:
        email = email.lower().strip()
        db = SessionLocal()
        try:
            u = db.query(_UserModel).filter(_UserModel.email == email).first()
            if u:
                u.name = name
                u.role = role
                # photo_data_url and groups deliberately untouched here —
                # this function only manages name/role (Admin page's add/
                # edit-role form); the photo is set/cleared exclusively
                # via set_photo() below, and groups via set_user_groups().
            else:
                u = _UserModel(
                    email=email, name=name, role=role,
                    added_by=added_by, created_at=datetime.utcnow(), groups=[],
                )
                db.add(u)
            db.commit()
            db.refresh(u)
            return _to_dict(u)
        finally:
            db.close()

    def set_groups(email: str, group_ids: list[str]) -> Optional[dict]:
        """Set (fully replace) the group memberships for an existing
        user. Returns None if the user doesn't exist yet — callers
        should upsert_user() first, same convention as set_photo()."""
        email = email.lower().strip()
        db = SessionLocal()
        try:
            u = db.query(_UserModel).filter(_UserModel.email == email).first()
            if not u:
                return None
            u.groups = list(group_ids)
            db.commit()
            db.refresh(u)
            return _to_dict(u)
        finally:
            db.close()

    def set_tab_write(email: str, tab_ids: list[str]) -> Optional[dict]:
        """Set (fully replace) the per-user WRITE grant list for an
        existing user. Returns None if the user doesn't exist yet —
        callers should upsert_user() first, same convention as
        set_groups()/set_photo() above."""
        email = email.lower().strip()
        db = SessionLocal()
        try:
            u = db.query(_UserModel).filter(_UserModel.email == email).first()
            if not u:
                return None
            u.tab_write = list(tab_ids)
            db.commit()
            db.refresh(u)
            return _to_dict(u)
        finally:
            db.close()

    def set_photo(email: str, photo_data_url: Optional[str]) -> Optional[dict]:
        """Set (or clear, if photo_data_url is None) the profile photo for
        an existing user. Returns None if the user doesn't exist yet —
        callers should upsert_user() first (e.g. for a founding admin who
        has never appeared in the store as a real row)."""
        email = email.lower().strip()
        db = SessionLocal()
        try:
            u = db.query(_UserModel).filter(_UserModel.email == email).first()
            if not u:
                return None
            u.photo_data_url = photo_data_url
            db.commit()
            db.refresh(u)
            return _to_dict(u)
        finally:
            db.close()

    def delete_user(email: str) -> bool:
        db = SessionLocal()
        try:
            u = db.query(_UserModel).filter(_UserModel.email == email.lower().strip()).first()
            if not u:
                return False
            db.delete(u)
            db.commit()
            return True
        finally:
            db.close()

    def update_last_login(email: str) -> None:
        """Stamp `last_login` (UTC) for a real login event — see
        database.py's PortalUser.last_login docstring for exactly when
        this should (and should NOT) be called. Silently no-ops if the
        user has no store row yet (e.g. a founding admin who has never
        appeared as a row) — same tolerance as set_photo()/set_groups()
        — since there's no meaningful "last login" to record for a user
        this store doesn't know about, and callers here (mock_login,
        login_landing) shouldn't fail a login over this being missing."""
        db = SessionLocal()
        try:
            u = db.query(_UserModel).filter(_UserModel.email == email.lower().strip()).first()
            if not u:
                return
            u.last_login = datetime.utcnow()
            db.commit()
        finally:
            db.close()

else:
    import boto3
    from datetime import timezone

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    TABLE_NAME = os.environ.get("USERS_TABLE_NAME", "job-portal-users")

    def _table():
        return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)

    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _seed_defaults_if_empty():
        table = _table()
        resp = table.scan(Limit=1)
        if resp.get("Count", 0) == 0 and not resp.get("Items"):
            for email in SEED_ADMIN_EMAILS:
                table.put_item(Item={
                    "email": email,
                    "name": SEED_ADMIN_NAMES.get(email, email),
                    "role": "admin",
                    "added_by": "system",
                    "created_at": _now_iso(),
                })
            for email in SEED_MEMBER_EMAILS:
                if email in SEED_ADMIN_EMAILS:
                    continue
                table.put_item(Item={
                    "email": email,
                    "name": SEED_MEMBER_NAMES.get(email, email),
                    "role": "member",
                    "added_by": "system",
                    "created_at": _now_iso(),
                })

    def list_users() -> list[dict]:
        _seed_defaults_if_empty()
        table = _table()
        resp = table.scan()
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        items.sort(key=lambda i: i.get("created_at") or "")
        return items

    def get_user(email: str) -> Optional[dict]:
        resp = _table().get_item(Key={"email": email.lower().strip()})
        return resp.get("Item")

    def upsert_user(email: str, name: str, role: str, added_by: str) -> dict:
        email = email.lower().strip()
        existing = get_user(email)
        item = {
            "email": email,
            "name": name,
            "role": role,
            "added_by": added_by,
            "created_at": existing["created_at"] if existing else _now_iso(),
        }
        # Preserve any existing photo/groups/tab_write — this function
        # only manages name/role (Admin page's add/edit-role form); the
        # photo is set/cleared exclusively via set_photo() below, groups
        # via set_groups() below, and tab_write via set_tab_write() below.
        if existing and existing.get("photo_data_url"):
            item["photo_data_url"] = existing["photo_data_url"]
        if existing and existing.get("groups"):
            item["groups"] = existing["groups"]
        if existing and existing.get("tab_write"):
            item["tab_write"] = existing["tab_write"]
        _table().put_item(Item=item)
        return item

    def set_groups(email: str, group_ids: list[str]) -> Optional[dict]:
        """Set (fully replace) the group memberships for an existing
        user. Returns None if the user doesn't exist yet — callers
        should upsert_user() first, same convention as set_photo()."""
        email = email.lower().strip()
        existing = get_user(email)
        if not existing:
            return None
        existing["groups"] = list(group_ids)
        _table().put_item(Item=existing)
        return existing

    def set_tab_write(email: str, tab_ids: list[str]) -> Optional[dict]:
        """Set (fully replace) the per-user WRITE grant list for an
        existing user. Returns None if the user doesn't exist yet —
        callers should upsert_user() first, same convention as
        set_groups()/set_photo() above."""
        email = email.lower().strip()
        existing = get_user(email)
        if not existing:
            return None
        existing["tab_write"] = list(tab_ids)
        _table().put_item(Item=existing)
        return existing

    def set_photo(email: str, photo_data_url: Optional[str]) -> Optional[dict]:
        """Set (or clear, if photo_data_url is None) the profile photo for
        an existing user. Returns None if the user doesn't exist yet —
        callers should upsert_user() first (e.g. for a founding admin who
        has never appeared in the store as a real row)."""
        email = email.lower().strip()
        existing = get_user(email)
        if not existing:
            return None
        if photo_data_url is None:
            existing.pop("photo_data_url", None)
            # DynamoDB put_item overwrites the whole item — omitting the
            # key here is sufficient to clear it (no explicit REMOVE
            # needed since this is a full put, not an UpdateItem).
        else:
            existing["photo_data_url"] = photo_data_url
        _table().put_item(Item=existing)
        return existing

    def delete_user(email: str) -> bool:
        email = email.lower().strip()
        existing = get_user(email)
        if not existing:
            return False
        _table().delete_item(Key={"email": email})
        return True

    def update_last_login(email: str) -> None:
        """Stamp `last_login` (UTC) for a real login event — see
        database.py's PortalUser.last_login docstring for exactly when
        this should (and should NOT) be called. Uses UpdateItem (not a
        full-item put_item overwrite like most of this DynamoDB branch)
        specifically so a login stamp can never race with/clobber a
        concurrent admin edit (e.g. set_groups()/set_photo() reading-then-
        overwriting the whole item at the same time) — only the single
        last_login attribute is touched. Silently no-ops if the user has
        no store row yet, same tolerance as the local/SQLite branch
        above."""
        email = email.lower().strip()
        if get_user(email) is None:
            return
        _table().update_item(
            Key={"email": email},
            UpdateExpression="SET last_login = :v",
            ExpressionAttributeValues={":v": _now_iso()},
        )


def is_allowed(email: str) -> bool:
    """True if this email is allowed to use the portal at all
    (seed admin OR present in the user store)."""
    email = (email or "").lower().strip()
    if not email:
        return False
    if is_seed_admin(email):
        return True
    return get_user(email) is not None


def is_admin(email: str) -> bool:
    """True if this email has FULL admin role (founding admin, or a
    store row with role == "admin"). Does NOT include "user_admin" —
    that is a deliberately narrower role, see is_user_admin() below, and
    every existing require_admin()-gated route (exports, connected-
    portal credentials, freeze notice, cost adjustments, DNS, etc.)
    stays restricted to this full-admin set only."""
    email = (email or "").lower().strip()
    if not email:
        return False
    if is_seed_admin(email):
        return True
    u = get_user(email)
    return bool(u and u.get("role") == "admin")


def is_user_admin(email: str) -> bool:
    """True if this email has the narrower "User Admin" role (store row
    with role == "user_admin") OR full admin. User Admins may add new
    portal users and see the users they themselves added, but nothing
    else that require_admin() gates — see require_user_admin() in
    main.py for the FastAPI dependency built on this."""
    email = (email or "").lower().strip()
    if not email:
        return False
    if is_admin(email):
        return True
    u = get_user(email)
    return bool(u and u.get("role") == "user_admin")


# The three User Groups treated as "portal authorities" — trusted with
# every authorization/delegation decision in this portal: who belongs to
# which User Group, which categories each group is granted, per-user
# tab WRITE grants (see PortalUser.tab_write), and promoting/demoting
# someone to "admin" or "user_admin". Deliberately a SUPERSET of
# founding admins, but NOT every plain "admin" — a full admin who is
# themselves neither a founding admin nor a member of one of these three
# groups does NOT qualify, per the user's explicit, repeated choice
# through this feature's design. Named once here (rather than inlined in
# is_portal_authority() below) so it's trivially greppable if the exact
# set of trusted groups ever needs to change.
_PORTAL_AUTHORITY_GROUPS = {"management_team", "security_team"}


def is_portal_authority(email: str) -> bool:
    """True if this email may make authorization/delegation decisions
    for OTHER users — restricted to founding admins (SEED_ADMIN_EMAILS)
    or members of the Management Team / Security Team User Groups
    (_PORTAL_AUTHORITY_GROUPS), per the user's explicit choice. Gates:
    - Group membership changes (who belongs to which User Group)
    - Group->category access changes (what each group can see)
    - Per-user tab WRITE grants (PortalUser.tab_write)
    - Promoting/demoting a user to/from "admin" or "user_admin"
    A plain "admin" store row (added by a founding admin, but not itself
    a founding admin or on the Management/Security Team) does NOT
    qualify — this is deliberately narrower than is_admin(). Named
    "is_user_admin_manager" in an earlier iteration of this feature when
    it only gated Admin/User Admin role changes; kept as an importable
    alias below for any code that still references the old name."""
    email = (email or "").lower().strip()
    if not email:
        return False
    if is_seed_admin(email):
        return True
    return bool(set(get_user_groups(email)) & _PORTAL_AUTHORITY_GROUPS)


# Backward-compatible alias — see is_portal_authority()'s docstring.
is_user_admin_manager = is_portal_authority


# Who may VIEW and CHANGE the Cloud Accounts page (Admin > Cloud Accounts): the
# founding admins and the Management Team ONLY. Narrower than
# is_portal_authority() (which also admits the Security Team) and than
# is_admin(): a plain admin, and a Security Team member who is not also on the
# Management Team, are refused. Changing this set is a one-line edit here.
_CLOUD_ACCOUNTS_GROUPS = {"management_team"}


def is_cloud_accounts_manager(email: str) -> bool:
    email = (email or "").lower().strip()
    if not email:
        return False
    if is_seed_admin(email):
        return True
    return bool(set(get_user_groups(email)) & _CLOUD_ACCOUNTS_GROUPS)


def get_display_name(email: str) -> str:
    email = (email or "").lower().strip()
    u = get_user(email)
    if u and u.get("name"):
        return u["name"]
    if email in SEED_ADMIN_NAMES:
        return SEED_ADMIN_NAMES[email]
    return email


def get_photo_url(email: str) -> Optional[str]:
    """Convenience accessor used by whoami()/the AI chat etc. — returns
    None if the user has no store row yet or no photo set."""
    u = get_user((email or "").lower().strip())
    return u.get("photo_data_url") if u else None


def get_user_tab_write(email: str) -> list[str]:
    """Convenience accessor — the list of tab_ids this user has been
    individually granted WRITE access to (see PortalUser.tab_write's
    docstring in database.py), or an empty list if the user has no
    store row yet or no grants set. Used by main.py's require_tab_write()
    and by the DNS/S2S VPN writer dependencies (OR'd with their existing
    group-based check)."""
    u = get_user((email or "").lower().strip())
    return list(u.get("tab_write") or []) if u else []


def get_user_groups(email: str) -> list[str]:
    """Convenience accessor — the list of group_ids this user belongs to
    (Security Team / Infra Team / etc., see settings_store.GROUP_REGISTRY),
    or an empty list if the user has no store row yet or no groups set.
    Used by /api/settings/my-tab-access, which resolves this down to the
    set of sidebar tabs this user can see — group membership is now the
    SOLE basis for tab visibility (besides being an admin)."""
    u = get_user((email or "").lower().strip())
    return list(u.get("groups") or []) if u else []


def set_user_photo(email: str, photo_data_url: Optional[str], actor: str) -> Optional[dict]:
    """Set/clear a user's profile photo, tolerating the case where a
    founding admin (the founding admins) has never actually appeared as a
    row in the store yet (e.g. a brand-new deploy before list_users()
    has run its seeding, or a store that was reset) — upserts a row for
    them first so set_photo() below always has something to update.
    Returns None only if `email` is neither a seed admin nor an existing
    store row (i.e. genuinely not a portal user) — callers should map
    that to a 404."""
    email = (email or "").lower().strip()
    if get_user(email) is None:
        if not is_seed_admin(email):
            return None
        upsert_user(
            email=email, name=SEED_ADMIN_NAMES.get(email, email),
            role="admin", added_by=actor,
        )
    return set_photo(email, photo_data_url)


def set_user_groups(email: str, group_ids: list[str], actor: str) -> Optional[dict]:
    """Set a user's group memberships, tolerating the case where a
    founding admin has never actually appeared as a row in the store yet
    (same tolerance as set_user_photo() above) — upserts a row for them
    first so set_groups() below always has something to update. Returns
    None only if `email` is neither a seed admin nor an existing store
    row — callers should map that to a 404."""
    email = (email or "").lower().strip()
    if get_user(email) is None:
        if not is_seed_admin(email):
            return None
        upsert_user(
            email=email, name=SEED_ADMIN_NAMES.get(email, email),
            role="admin", added_by=actor,
        )
    return set_groups(email, group_ids)


def set_user_tab_write(email: str, tab_ids: list[str], actor: str) -> Optional[dict]:
    """Set a user's per-tab WRITE grants, tolerating the case where a
    founding admin has never actually appeared as a row in the store yet
    (same tolerance as set_user_groups()/set_user_photo() above) —
    upserts a row for them first so set_tab_write() below always has
    something to update. Returns None only if `email` is neither a seed
    admin nor an existing store row — callers should map that to a 404.
    (In practice a founding admin never needs this — they already pass
    every require_tab_write() check via is_admin() — but kept symmetric
    with set_user_groups() for any other caller.)"""
    email = (email or "").lower().strip()
    if get_user(email) is None:
        if not is_seed_admin(email):
            return None
        upsert_user(
            email=email, name=SEED_ADMIN_NAMES.get(email, email),
            role="admin", added_by=actor,
        )
    return set_tab_write(email, tab_ids)
