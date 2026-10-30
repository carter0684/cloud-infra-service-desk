"""
"SonicWall" sub-tab of the "Local Infra Hardware Monitor" page — a
simulated health dashboard built on top of the MySonicWall MSSP API
(https://api.mysonicwall.com/mssp.html), added per user request as a
second hardware-monitoring source alongside Cisco Meraki
(meraki_monitor.py).

## Important scope note — read before extending this module

Unlike Meraki's Dashboard API, the public MySonicWall API
(api.mysonicwall.com) is primarily an MSSP billing/provisioning surface
— tenant list, licensed product counts, cloud-service enablement ratios
— NOT a real-time per-firewall telemetry API (no uptime/CPU/interface
status endpoint is publicly documented). There is therefore no literal
"device is up/down" signal available here the way there is for Meraki
APs. This module surfaces the best available proxy instead: each MSSP
tenant's cloud-service *provisioning ratio* (e.g. "6 of 6" firewalls
provisioned under FW Management/NGMGMT), presented as a "simulated"
health view (per the user's own framing: "simulate health dashboard
link"). Field names below are deliberately "provisioned"/"licensed"
rather than "online"/"offline", to avoid implying real-time device
health this API does not actually provide.

## Endpoint used

GET https://api.mysonicwall.com/api/hgms/get-cloud-tenants — returns
every MSSP tenant (customer) under this MySonicWall account, each with
a `cloudServices` array (one entry per licensed cloud service — FW
Management/NGMGMT, Reports, Wireless, etc.) carrying `isEnabled` and an
`avaiableRatio` string like "6 of 6" (provisioned-of-licensed).
Confirmed live against the real API key provided by the user.

## Credentials

Same pattern as meraki_monitor.py's Cisco Meraki API key handling — a
single MySonicWall API key (bearer-style, sent as the X-API-KEY header
per MySonicWall's own documented convention, see
https://www.sonicwall.com/support/knowledge-base/nsm-cloud-getting-
token-for-msw-and-nsm-apis). Local dev can set SONICWALL_API_KEY
directly to skip Secrets Manager; production loads Secrets Manager
secret `job-portal/sonicwall-api-key` (a single plain string).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from .cache_utils import TTLCache

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")  # region for Secrets Manager (portal's own account)

SONICWALL_BASE_URL = "https://api.mysonicwall.com"
SONICWALL_API_KEY_SECRET_NAME = os.environ.get("SONICWALL_API_KEY_SECRET_NAME", "job-portal/sonicwall-api-key")

_REQUEST_TIMEOUT_SECONDS = 20.0

# Single cache — this module makes one call (get-cloud-tenants) and
# derives everything else from it, unlike meraki_monitor.py's several
# distinct endpoints, so one TTLCache entry is enough. 10 minutes: MSSP
# tenant/provisioning data changes far less often than Meraki's live AP
# status, so a slightly longer TTL than meraki_monitor.py's _CACHE_SHORT
# (5 min) is appropriate here.
_CACHE = TTLCache(ttl_seconds=10 * 60)


class SonicWallError(ValueError):
    """Raised for invalid input or a total inability to reach the
    MySonicWall API. Safe to show the user as-is."""


# ── Credentials ─────────────────────────────────────────────────────────

def _get_secret(secret_name: str) -> str:
    import boto3
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]


def _get_api_key() -> str:
    """Local-dev-env-var-override convenience, same pattern as every
    other cross-service credential in this codebase (see
    meraki_monitor.py's _get_api_key())."""
    env_key = os.environ.get("SONICWALL_API_KEY")
    if env_key:
        return env_key
    try:
        secret_str = _get_secret(SONICWALL_API_KEY_SECRET_NAME)
    except Exception as e:
        raise SonicWallError(f"Could not load SonicWall API key: {e}") from e
    # Tolerate the secret being stored as either a plain string or JSON
    # ({"api_key": "..."}) — same defensive parsing as meraki_monitor.py.
    secret_str = (secret_str or "").strip()
    if secret_str.startswith("{"):
        import json
        try:
            parsed = json.loads(secret_str)
            key = parsed.get("api_key") or parsed.get("SONICWALL_API_KEY")
            if key:
                return key
        except (ValueError, AttributeError):
            pass
    if not secret_str:
        raise SonicWallError("SonicWall API key secret is empty.")
    return secret_str


def _headers() -> dict:
    # MySonicWall's own documented header convention for API-key auth
    # (see the module docstring's "Credentials" section for the source).
    return {"X-API-KEY": _get_api_key(), "Content-Type": "application/json"}


# ── HTTP ────────────────────────────────────────────────────────────────

def _get_json(path: str) -> dict:
    url = f"{SONICWALL_BASE_URL}{path}"
    try:
        resp = httpx.get(url, headers=_headers(), timeout=_REQUEST_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        raise SonicWallError(f"Could not reach MySonicWall API ({url}): {e}") from e
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise SonicWallError(f"MySonicWall API error for {path}: {e}") from e
    try:
        body = resp.json()
    except ValueError as e:
        raise SonicWallError(f"MySonicWall API returned invalid JSON for {path}: {e}") from e
    if not isinstance(body, dict):
        raise SonicWallError(f"MySonicWall API returned an unexpected response shape for {path}")
    return body


def _parse_ratio(ratio: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """Parses MySonicWall's `avaiableRatio` strings like "6 of 6" into
    (6, 6). Returns (None, None) for "-1", empty, or any unparsable
    value — MySonicWall uses "-1" to mean "not applicable to this
    service" (e.g. a disabled/unlicensed service), which is distinct
    from "0 of 0" (licensed but zero seats provisioned)."""
    if not ratio or not isinstance(ratio, str):
        return None, None
    parts = ratio.split(" of ")
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        return None, None


# ── Tenants (the one real data fetch) ─────────────────────────────────────

def _fetch_tenants() -> dict:
    body = _get_json("/api/hgms/get-cloud-tenants")
    if body.get("operationStatus") != "OK":
        raise SonicWallError(body.get("msgDesc") or "MySonicWall API returned a non-OK status for get-cloud-tenants")
    content = body.get("content") or {}
    raw_tenants = content.get("arrTenants") or []

    tenants = []
    for t in raw_tenants:
        services = []
        for svc in (t.get("cloudServices") or []):
            provisioned, licensed = _parse_ratio(svc.get("avaiableRatio"))
            services.append({
                "service_name": svc.get("serviceNameDisp") or svc.get("serviceName"),
                "is_enabled": bool(svc.get("isEnabled")),
                "provisioned": provisioned,
                "licensed": licensed,
                # Present only for the firewall-management service
                # (NGMGMT) — the serial number of the actual SonicWall
                # appliance(s) under this tenant.
                "tenant_serial": svc.get("tenantSerial"),
            })
        tenants.append({
            "tenant_id": t.get("productGroupID"),
            "name": t.get("name") or "—",
            "product_count": int(t.get("productCount") or 0),
            "services": services,
        })
    tenants.sort(key=lambda t: (t.get("name") or "").lower())
    return {"tenants": tenants, "count": len(tenants)}


def list_tenants(force_refresh: bool = False) -> dict:
    """Every MSSP tenant under this MySonicWall account, each annotated
    with its licensed cloud services and provisioning ratios. Powers the
    SonicWall sub-tab's tenant table."""
    return _CACHE.get_or_fetch("tenants", _fetch_tenants, force_refresh)


def get_overview(force_refresh: bool = False) -> dict:
    """Org-wide summary derived from list_tenants() — total tenant/
    product counts, plus a "simulated health" provisioning percentage
    (see module docstring's scope note: this reflects licensed-vs-
    provisioned service seats, NOT real-time device up/down state,
    since the MySonicWall API does not expose the latter)."""
    data = list_tenants(force_refresh)
    tenants = data["tenants"]

    total_products = sum(t["product_count"] for t in tenants)
    total_provisioned = 0
    total_licensed = 0
    enabled_service_count = 0
    for t in tenants:
        for svc in t["services"]:
            if not svc["is_enabled"]:
                continue
            enabled_service_count += 1
            if svc["provisioned"] is None or svc["licensed"] is None or svc["licensed"] == 0:
                continue
            total_provisioned += svc["provisioned"]
            total_licensed += svc["licensed"]

    provisioning_pct = (total_provisioned / total_licensed * 100) if total_licensed > 0 else None

    return {
        "tenant_count": len(tenants),
        "total_products": total_products,
        "enabled_service_count": enabled_service_count,
        "total_provisioned": total_provisioned,
        "total_licensed": total_licensed,
        "provisioning_pct": provisioning_pct,
    }
