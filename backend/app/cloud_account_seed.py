"""
Optional pre-load of the cloud account registry.

This module is DATA ONLY: it may import org_config (which itself imports
nothing from the app) but no other app module, because it is imported by
cloud_account_store (which cost_explorer / billing_by_service import in turn),
so importing cost_explorer from here would be circular.

A fresh deployment starts with an EMPTY registry: Admins add AWS accounts and
Azure subscriptions on the Cloud Accounts page. To pre-load a list of accounts
on first start instead, point CLOUD_ACCOUNT_SEED_FILE at a JSON file:

    [
      {"account_id": "111111111111", "name": "core",    "business_unit": "Platform", "core_scope": true},
      {"account_id": "222222222222", "name": "workload", "business_unit": "Platform", "core_scope": true},
      {"account_id": "333333333333", "name": "sandbox",  "business_unit": "Other",    "core_scope": false}
    ]

`core_scope` = the account is covered by Cost Analysis, CMDB, AWS Health member
accounts, VPN Lookup names and cost-adjustment validation. Every seeded account
is always in the "billing" scope (Billing by Service, RI/SP Savings, Security
Hub, WAF Monitoring, AWS Resources). The seeding is idempotent: an Admin's
later rename, business-unit change or scope change is never overwritten, and
the order of the file is the order pickers show the accounts in.

The seed also doubles as the fallback account list when the registry has never
been readable, so a registry outage can never blank every account-based page.
"""

from __future__ import annotations

import json
import logging
import os

from . import org_config

logger = logging.getLogger(__name__)

# Accounts the Portal itself depends on; offboarding them needs the typed
# risk confirmation (see cloud_account_service). Set via the
# PORTAL_HOST_ACCOUNT_ID and CORE_ACCOUNT_ID environment variables.
PORTAL_HOST_ACCOUNT_ID = org_config.PORTAL_HOST_ACCOUNT_ID
CORE_ACCOUNT_ID = org_config.CORE_ACCOUNT_ID
PROTECTED_ACCOUNT_IDS = frozenset(a for a in (PORTAL_HOST_ACCOUNT_ID, CORE_ACCOUNT_ID) if a)

DEFAULT_BUSINESS_UNIT = "Other"


def _load_seed() -> list[tuple[str, str, str, bool]]:
    """Rows of (account_id, display_name, business_unit, in_core_scope)."""
    path = os.environ.get("CLOUD_ACCOUNT_SEED_FILE", "").strip()
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        rows = []
        for item in raw:
            account_id = str(item["account_id"]).strip()
            rows.append((
                account_id,
                str(item.get("name") or account_id),
                str(item.get("business_unit") or DEFAULT_BUSINESS_UNIT),
                bool(item.get("core_scope", True)),
            ))
        return rows
    except Exception:
        logger.exception("cloud_account_seed: could not read CLOUD_ACCOUNT_SEED_FILE=%s", path)
        return []


AWS_SEED: list[tuple[str, str, str, bool]] = _load_seed()

# Business units offered on the Cloud Accounts page; a new one needs an
# explicit Admin confirmation. Set KNOWN_BUSINESS_UNITS (comma-separated).
KNOWN_BUSINESS_UNITS = tuple(org_config.KNOWN_BUSINESS_UNITS) + (DEFAULT_BUSINESS_UNIT,)
