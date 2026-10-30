"""
"Azure Resources" sidebar category — a read-only inventory + cost
dashboard for this org's Azure subscription, added per user request
alongside the existing "AWS Resources" tab (see
app/aws_resources_inventory.py) as an independent, parallel resource
family rather than folded into it (different cloud, different auth,
different API shapes).

## Tenant / Subscription

Same Azure AD tenant as this portal's own SSO (example.com) — the
user confirmed this explicitly, so there is no separate "Azure
environment" to configure. The specific SUBSCRIPTION to inventory is
set via AZURE_SUBSCRIPTION_ID (env var / Secrets Manager JSON field,
see _get_credentials() below) — a tenant can contain many subscriptions,
and ARM's resource-listing APIs are always scoped to one subscription
at a time.

## Authentication — OAuth2 client-credentials flow (Service Principal)

Unlike this portal's own SSO (which uses the Azure AD app's
CLIENT_SECRET only to redeem a user's interactive OIDC login), this
module authenticates itself directly against Azure AD with client-
credentials grant — no user interaction, no user context. This
requires:

  1. A dedicated App Registration ("job-portal-azure-reader",
     single-tenant) created directly by an admin in the Azure Portal —
     the app_registration steps this project's agent cannot automate,
     mirroring the FortiManager Cloud API user precedent
     (fortigate_monitor.py's module docstring: created by a human
     admin, never by this codebase).
  2. That app assigned the "Reader" role (read-only — NO write access)
     at the target subscription's IAM/Access control, so the token this
     module obtains can only ever list/read ARM resources, never modify
     or delete anything. This is a deliberate v1 scope decision per the
     user's explicit "Reader only first" answer — a future phase may
     widen this to Contributor for actual VM start/stop control, at
     which point this module's error handling and the frontend would
     both need new write-capable endpoints, NOT a silent scope
     expansion of the existing read endpoints.

Token endpoint (tenant-specific, NOT the shared FortiCloud-style single
endpoint):

  POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
  Content-Type: application/x-www-form-urlencoded
  grant_type=client_credentials
  &client_id={client_id}
  &client_secret={client_secret}
  &scope=https://management.azure.com/.default

  -> {"access_token": "...", "expires_in": 3599, "token_type": "Bearer"}

The returned bearer token is valid for ~1 hour (expires_in, typically
3599 seconds) and is cached in-process (mirrors fortigate_monitor.py's
_session_cache pattern) with a safety margin so a request is never sent
with a token that's about to expire mid-flight.

## Credentials

Local dev can set AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET
/ AZURE_SUBSCRIPTION_ID directly to skip Secrets Manager; production
loads Secrets Manager secret `job-portal/azure-reader-credentials` (a
JSON object: {"tenant_id": "...", "client_id": "...",
"client_secret": "...", "subscription_id": "..."}), same "read
server-side only, never exposed to the frontend bundle or Git"
convention as every other cross-service credential in this codebase.

Deliberately a SEPARATE secret from `job-portal/azure-ad-client-secret`
(the existing SSO login app's client secret, read directly by the ALB
listener per compute_stack.py) — these are two entirely different Azure
AD App Registrations serving two entirely different purposes (one lets
humans sign in via OIDC; this one lets the backend read ARM resources
via client-credentials), and mixing them would mean a rotation of
either one's secret silently breaking the other's unrelated flow.

## Data surfaced — Azure Resource Manager (ARM) REST API

All resource-listing calls are plain ARM "list" GETs, scoped to the one
configured subscription, e.g.:

  GET https://management.azure.com/subscriptions/{sub}/providers/
      Microsoft.Compute/virtualMachines?api-version=2024-07-01
  GET https://management.azure.com/subscriptions/{sub}/providers/
      Microsoft.DesktopVirtualization/hostPools?api-version=2024-04-03
  GET https://management.azure.com/subscriptions/{sub}/resources
      ?api-version=2021-04-01           (Resource Groups' resource
                                          counts derive from this)
  GET https://management.azure.com/subscriptions/{sub}/providers/
      Microsoft.Network/networkInterfaces?api-version=2023-11-01
  GET https://management.azure.com/subscriptions/{sub}/providers/
      Microsoft.Network/networkSecurityGroups?api-version=2023-11-01
  GET https://management.azure.com/subscriptions/{sub}/providers/
      Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies
      ?api-version=2023-11-01           (WAF — Application Gateway WAF
                                          policies; if this org instead
                                          uses Front Door's WAF, a
                                          separate provider path would
                                          be needed — confirmed via live
                                          testing once credentials land)

Cost uses the separate Azure Cost Management API (POST .../query, not a
simple GET list) — see get_cost_summary() below.

Every list_*() function returns the same {"headers": [...], "rows":
[[...], ...], ...} shape the frontend's AwsResourcesDashboardPage
already knows how to render generically (see
frontend/src/pages/AzureResourcesPage.jsx), rather than one bespoke
shape per resource type.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
import time
from typing import Optional

import httpx

from .cache_utils import TTLCache

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")  # region for Secrets Manager (portal's own account)

AZURE_LOGIN_BASE_URL = "https://login.microsoftonline.com"
ARM_BASE_URL = "https://management.azure.com"
ARM_DEFAULT_SCOPE = "https://management.azure.com/.default"

AZURE_CREDENTIALS_SECRET_NAME = os.environ.get(
    "AZURE_CREDENTIALS_SECRET_NAME", "job-portal/azure-reader-credentials",
)

_REQUEST_TIMEOUT_SECONDS = 20.0

# Resource-list cache — 5 minutes, same rationale as fortigate_monitor.py's
# _CACHE: short enough that "Refresh" feels meaningful, long enough that
# repeated page loads/tab switches don't hammer ARM.
_CACHE = TTLCache(ttl_seconds=5 * 60)
# Cost data changes at most once a day (Azure's own cost data has a
# reporting lag of several hours), so a longer TTL is appropriate here
# — mirrors cost_explorer.py's own multi-hour cache rationale for the
# AWS Cost Analysis tab.
_COST_CACHE = TTLCache(ttl_seconds=60 * 60)

# Cached ARM bearer token + when it was obtained. Tokens are valid for
# ~3599 seconds (expires_in) per Azure AD's client-credentials response;
# refreshed with a 5-minute safety margin so a long-running request
# never starts with a token that expires mid-flight.
_TOKEN_SAFETY_MARGIN_SECONDS = 5 * 60
_token_cache: dict = {"token": None, "expires_at": 0.0}


class AzureError(ValueError):
    """Raised for invalid input or a total inability to reach Azure AD /
    Azure Resource Manager. Safe to show the user as-is."""


# ── Credentials ─────────────────────────────────────────────────────────

def _get_secret_json(secret_name: str) -> dict:
    import boto3
    import json
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = client.get_secret_value(SecretId=secret_name)
    raw = (resp.get("SecretString") or "").strip()
    if not raw:
        raise AzureError(f"Secret {secret_name} is empty.")
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise AzureError(f"Secret {secret_name} is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise AzureError(f"Secret {secret_name} must be a JSON object.")
    return parsed


def _get_credentials() -> dict:
    """{tenant_id, client_id, client_secret, subscription_id} —
    local-dev-env-var-override convenience, same pattern as every other
    cross-service credential in this codebase (see
    fortigate_monitor.py's _get_credentials())."""
    env_tenant = os.environ.get("AZURE_TENANT_ID")
    env_client = os.environ.get("AZURE_CLIENT_ID")
    env_secret = os.environ.get("AZURE_CLIENT_SECRET")
    env_sub = os.environ.get("AZURE_SUBSCRIPTION_ID")
    if env_tenant and env_client and env_secret and env_sub:
        return {
            "tenant_id": env_tenant, "client_id": env_client,
            "client_secret": env_secret, "subscription_id": env_sub,
        }
    try:
        parsed = _get_secret_json(AZURE_CREDENTIALS_SECRET_NAME)
    except AzureError:
        raise
    except Exception as e:
        raise AzureError(f"Could not load Azure API credentials: {e}") from e
    tenant_id = parsed.get("tenant_id")
    client_id = parsed.get("client_id")
    client_secret = parsed.get("client_secret")
    subscription_id = parsed.get("subscription_id")
    if not all([tenant_id, client_id, client_secret, subscription_id]):
        raise AzureError(
            "Azure API credentials secret is missing one of "
            "tenant_id/client_id/client_secret/subscription_id."
        )
    return {
        "tenant_id": tenant_id, "client_id": client_id,
        "client_secret": client_secret, "subscription_id": subscription_id,
    }


# ── OAuth2 client-credentials token ──────────────────────────────────────

def _fetch_access_token(creds: dict) -> tuple[str, float]:
    """Returns (access_token, expires_in_seconds)."""
    url = f"{AZURE_LOGIN_BASE_URL}/{creds['tenant_id']}/oauth2/v2.0/token"
    try:
        resp = httpx.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "scope": ARM_DEFAULT_SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise AzureError(f"Could not reach Azure AD token endpoint: {e}") from e
    if resp.status_code >= 400:
        # Surface Azure AD's actual error body (e.g. "invalid_client",
        # "AADSTS7000215: Invalid client secret provided") rather than
        # httpx's generic raise_for_status() message, which discards it
        # — same rationale as fortigate_monitor.py's _fetch_session().
        try:
            body = resp.json()
            detail = body.get("error_description") or body.get("error") or body
        except ValueError:
            detail = resp.text
        raise AzureError(f"Azure AD token request failed ({resp.status_code}): {detail}")
    try:
        body = resp.json()
    except ValueError as e:
        raise AzureError(f"Azure AD token endpoint returned invalid JSON: {e}") from e
    token = (body or {}).get("access_token")
    if not token:
        raise AzureError("Azure AD token response did not include an access_token.")
    expires_in = (body or {}).get("expires_in") or 3599
    return token, float(expires_in)


def _get_token(force_refresh: bool = False) -> str:
    now = time.monotonic()
    cached = _token_cache.get("token")
    expires_at = _token_cache.get("expires_at") or 0.0
    if cached and not force_refresh and now < (expires_at - _TOKEN_SAFETY_MARGIN_SECONDS):
        return cached
    creds = _get_credentials()
    token, expires_in = _fetch_access_token(creds)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expires_in
    return token


# ── ARM REST calls ────────────────────────────────────────────────────────

def _arm_get(path: str, api_version: str, extra_params: Optional[dict] = None) -> dict:
    """GET https://management.azure.com{path}?api-version=... with the
    cached bearer token, transparently retrying once with a fresh token
    on a 401 (expired/invalid token) — callers never see the token
    plumbing, mirroring fortigate_monitor.py's _jsonrpc_call() retry
    pattern for its own session handshake."""
    url = f"{ARM_BASE_URL}{path}"
    params = {"api-version": api_version, **(extra_params or {})}

    def _do(token: str):
        return httpx.get(
            url, params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    token = _get_token()
    try:
        resp = _do(token)
    except httpx.HTTPError as e:
        raise AzureError(f"Could not reach Azure Resource Manager ({path}): {e}") from e

    if resp.status_code == 401:
        token = _get_token(force_refresh=True)
        try:
            resp = _do(token)
        except httpx.HTTPError as e:
            raise AzureError(f"Azure Resource Manager retry after token refresh failed: {e}") from e

    if resp.status_code >= 400:
        try:
            body = resp.json()
            detail = ((body or {}).get("error") or {}).get("message") or body
        except ValueError:
            detail = resp.text
        raise AzureError(f"Azure Resource Manager request failed for {path} ({resp.status_code}): {detail}")

    try:
        return resp.json() or {}
    except ValueError as e:
        raise AzureError(f"Azure Resource Manager returned invalid JSON for {path}: {e}") from e


def _arm_list_all_pages(path: str, api_version: str) -> list[dict]:
    """ARM list endpoints paginate via a full `nextLink` URL in the
    response body — follows it until exhausted. Most subscriptions in
    this org's size range will have well under one page's worth of any
    given resource type, but this is not assumed."""
    subscription_id = _current_subscription_id()
    full_path = f"/subscriptions/{subscription_id}{path}"
    body = _arm_get(full_path, api_version)
    items: list[dict] = list(body.get("value") or [])
    next_link = body.get("nextLink")
    token = _get_token()
    while next_link:
        try:
            resp = httpx.get(next_link, headers={"Authorization": f"Bearer {token}"}, timeout=_REQUEST_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            raise AzureError(f"Could not follow Azure Resource Manager pagination link: {e}") from e
        if resp.status_code == 401:
            token = _get_token(force_refresh=True)
            try:
                resp = httpx.get(next_link, headers={"Authorization": f"Bearer {token}"}, timeout=_REQUEST_TIMEOUT_SECONDS)
            except httpx.HTTPError as e:
                raise AzureError(f"Azure Resource Manager pagination retry failed: {e}") from e
        if resp.status_code >= 400:
            raise AzureError(f"Azure Resource Manager pagination request failed ({resp.status_code}): {resp.text}")
        try:
            page = resp.json() or {}
        except ValueError as e:
            raise AzureError(f"Azure Resource Manager pagination page returned invalid JSON: {e}") from e
        items.extend(page.get("value") or [])
        next_link = page.get("nextLink")
    return items


def _arm_list_all_pages_with_filter(path: str, api_version: str, odata_filter: str) -> list[dict]:
    """Same pagination behaviour as _arm_list_all_pages() above, but
    threads an OData `$filter` query param through to the first
    request (via _arm_get()'s existing extra_params argument) —
    subsequent pages are followed via the response's own nextLink,
    which already embeds the filter, so it does not need to be
    re-appended per page."""
    subscription_id = _current_subscription_id()
    full_path = f"/subscriptions/{subscription_id}{path}"
    body = _arm_get(full_path, api_version, extra_params={"$filter": odata_filter})
    items: list[dict] = list(body.get("value") or [])
    next_link = body.get("nextLink")
    token = _get_token()
    while next_link:
        try:
            resp = httpx.get(next_link, headers={"Authorization": f"Bearer {token}"}, timeout=_REQUEST_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            raise AzureError(f"Could not follow Azure Resource Manager pagination link: {e}") from e
        if resp.status_code == 401:
            token = _get_token(force_refresh=True)
            try:
                resp = httpx.get(next_link, headers={"Authorization": f"Bearer {token}"}, timeout=_REQUEST_TIMEOUT_SECONDS)
            except httpx.HTTPError as e:
                raise AzureError(f"Azure Resource Manager pagination retry failed: {e}") from e
        if resp.status_code >= 400:
            raise AzureError(f"Azure Resource Manager pagination request failed ({resp.status_code}): {resp.text}")
        try:
            page = resp.json() or {}
        except ValueError as e:
            raise AzureError(f"Azure Resource Manager pagination page returned invalid JSON: {e}") from e
        items.extend(page.get("value") or [])
        next_link = page.get("nextLink")
    return items


# ── Helpers shared by multiple resource types ─────────────────────────────

def _resource_group_from_id(resource_id: Optional[str]) -> str:
    """ARM resource ids are always
    /subscriptions/{sub}/resourceGroups/{rg}/providers/... — extracts
    {rg} without a full URL parser, tolerating case-insensitive
    "resourcegroups" (ARM ids are inconsistently cased across API
    versions/providers)."""
    if not resource_id:
        return "—"
    parts = resource_id.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return "—"


def _tags_to_string(tags: Optional[dict]) -> str:
    if not tags:
        return ""
    return ", ".join(f"{k}={v}" for k, v in sorted(tags.items()))


def _split_arm_resource_id(resource_id: Optional[str]) -> Optional[tuple[str, str]]:
    """(subscription_id, "/resourceGroups/{rg}/providers/...") from a
    full ARM resource id — used to build sub-resource paths (e.g.
    .../hostPools/{name}/sessionHosts) off an existing resource's id.
    Splits on "/subscriptions/{sub}" case-insensitively by segment
    (mirrors _resource_group_from_id()'s "resourcegroups" tolerance
    above), since ARM resource ids are inconsistently cased across API
    versions/providers (e.g. lowercase "/resourcegroups/" has been
    observed from Microsoft.DesktopVirtualization). Returns None if the
    id doesn't match the expected shape."""
    if not resource_id:
        return None
    parts = resource_id.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "subscriptions" and i + 1 < len(parts):
            subscription_id = parts[i + 1]
            relative_path = "/" + "/".join(parts[i + 2:])
            return subscription_id, relative_path
    return None


# ── VM size specs (vCPU / RAM) lookup ──────────────────────────────────────
#
# ARM's VM list/get responses only ever carry the VM SIZE NAME (e.g.
# "Standard_D2as_v5") in hardwareProfile.vmSize — never the actual
# vCPU count or memory. Those specs live in a completely separate API,
# Microsoft.Compute/skus (subscription-wide, not per-VM), which this
# module queries ONCE and caches for the same 5-minute TTL as every
# other resource type, then joins onto each VM/AVD session host row by
# vmSize name. This avoids a per-VM extra call (unlike
# _fetch_vm_power_state() above, which genuinely has no bulk
# alternative) — the /skus endpoint is a single subscription-wide call
# that covers every VM size in one shot.

_VM_SIZE_SPECS_CACHE = TTLCache(ttl_seconds=60 * 60)  # size specs are static, so a longer TTL than the 5-min resource cache is fine


def _fetch_vm_size_specs_for_location(location: str) -> dict:
    """{vm_size_name: {"vcpus": int, "memory_gb": float}} for VM sizes
    available in ONE Azure region, sourced from Microsoft.Compute/skus
    filtered to that location. CONFIRMED via Azure's own REST API
    reference (resource-skus/list): "Only location filter is supported
    currently" — and this filter matters a lot here, since the
    unfiltered /skus listing covers every VM size across every Azure
    region worldwide (tens of thousands of entries with heavy
    pagination), which in practice took long enough to time out a
    request. Scoping to just the location(s) this subscription's VMs
    actually use keeps this fast, since a subscription only ever has
    VMs in a handful of regions. Each sku's capabilities array carries
    vCPUs/MemoryGB as string values under name/value pairs — parsed
    defensively since a handful of legacy/preview sizes are documented
    to omit some capability entries."""
    raw = _arm_list_all_pages_with_filter(
        "/providers/Microsoft.Compute/skus", "2021-07-01", f"location eq '{location}'",
    )
    specs: dict = {}
    for sku in raw:
        if sku.get("resourceType") != "virtualMachines":
            continue
        name = sku.get("name")
        if not name or name in specs:
            continue
        vcpus = None
        memory_gb = None
        for cap in (sku.get("capabilities") or []):
            cap_name = cap.get("name")
            cap_value = cap.get("value")
            if cap_name == "vCPUs":
                try:
                    vcpus = int(cap_value)
                except (TypeError, ValueError):
                    vcpus = None
            elif cap_name == "MemoryGB":
                try:
                    memory_gb = float(cap_value)
                except (TypeError, ValueError):
                    memory_gb = None
        specs[name] = {"vcpus": vcpus, "memory_gb": memory_gb}
    return specs


def _get_vm_size_specs(locations: tuple) -> dict:
    """Merged {vm_size_name: {...}} across all given locations, each
    fetched/cached independently per-location (a subscription with VMs
    in 2 regions only ever pays for 2 small /skus calls, not one huge
    global one)."""
    merged: dict = {}
    for location in locations:
        if not location:
            continue
        result = _VM_SIZE_SPECS_CACHE.get_or_fetch(
            f"vm_size_specs:{location}", lambda loc=location: {"specs": _fetch_vm_size_specs_for_location(loc)},
        )
        merged.update(result.get("specs") or {})
    return merged


def _vm_size_spec_columns(vm_size: Optional[str], specs: dict) -> tuple:
    """(vcpus_display, memory_gb_display) for one VM's size name —
    "—" for either value this subscription's /skus listing didn't
    have a matching size for (e.g. a retired/regional-only size)."""
    spec = specs.get(vm_size) if vm_size else None
    if not spec:
        return "—", "—"
    vcpus = spec.get("vcpus")
    memory_gb = spec.get("memory_gb")
    return (
        vcpus if vcpus is not None else "—",
        memory_gb if memory_gb is not None else "—",
    )


def _vm_os_version(storage_profile: dict) -> str:
    """Human-readable Windows OS version for one VM, e.g. "2019-
    datacenter-gensecond (17763.4645.230707)" or "win11-25h2-avd-m365
    (26200.7623.260113)" — CONFIRMED via live testing that ARM's
    imageReference.sku is already a readable version string (no
    separate lookup/mapping table needed), and exactVersion carries the
    precise OS build number Azure actually provisioned (version itself
    is almost always the unhelpful literal "latest", since VMs are
    provisioned with version="latest" and Azure resolves it at deploy
    time — exactVersion is what was actually resolved to). A handful of
    VMs in this org have no imageReference at all (custom
    image/snapshot-based provisioning bypasses the marketplace image
    reference entirely) — falls back to just the OS type in that case,
    since there is no version info to show."""
    img_ref = storage_profile.get("imageReference") or {}
    sku = img_ref.get("sku")
    if sku:
        exact_version = img_ref.get("exactVersion")
        return f"{sku} ({exact_version})" if exact_version else sku
    os_disk = storage_profile.get("osDisk") or {}
    os_type = os_disk.get("osType")
    return f"{os_type} (version unknown)" if os_type else "—"


# ── Virtual Machines ──────────────────────────────────────────────────────

def _fetch_vms() -> dict:
    # CONFIRMED via live testing: the plain list endpoint does NOT
    # populate power state (instanceView is always absent from the bulk
    # list response, regardless of api-version) — Azure requires
    # instanceView per-VM, via either $expand=instanceView on a
    # single-VM GET or the dedicated .../instanceView sub-resource. A
    # separate per-VM call for every VM on every list load is avoided
    # (mirrors fortigate_monitor.py's "don't fetch detail for all ~27
    # devices on page load" rationale) — power state is fetched only
    # for VMs actually present in this listing, via one extra call each,
    # since ARM has no "list VMs with instanceView expanded" bulk
    # endpoint. Failures per-VM are swallowed (leaves "unknown" for that
    # VM) rather than failing the whole VM list over one VM's hiccup.
    #
    # vCPU/Memory (GB) come from the separate /skus lookup (see above);
    # Disk Size (GB) IS already present in the bulk list response
    # itself (storageProfile.osDisk.diskSizeGB) — no extra call needed
    # for that one.
    raw = _arm_list_all_pages("/providers/Microsoft.Compute/virtualMachines", "2024-07-01")
    # Only query /skus for the region(s) VMs in this subscription
    # actually live in — a global unfiltered fetch is far too large
    # (see _fetch_vm_size_specs_for_location()'s docstring).
    distinct_locations = tuple(sorted({vm.get("location") for vm in raw if vm.get("location")}))
    size_specs = _get_vm_size_specs(distinct_locations)
    headers = ["Name", "Resource Group", "Location", "VM Size", "vCPUs", "Memory (GB)", "OS Type", "OS Version", "Disk Size (GB)", "Power State", "Tags"]
    rows = []
    for vm in raw:
        props = vm.get("properties") or {}
        hw = props.get("hardwareProfile") or {}
        vm_size = hw.get("vmSize")
        storage_profile = props.get("storageProfile") or {}
        os_disk = storage_profile.get("osDisk") or {}
        vcpus, memory_gb = _vm_size_spec_columns(vm_size, size_specs)
        rows.append([
            vm.get("name") or "—",
            _resource_group_from_id(vm.get("id")),
            vm.get("location") or "—",
            vm_size or "—",
            vcpus,
            memory_gb,
            os_disk.get("osType") or "—",
            _vm_os_version(storage_profile),
            os_disk.get("diskSizeGB") if os_disk.get("diskSizeGB") is not None else "—",
            _fetch_vm_power_state(vm.get("id")),
            _tags_to_string(vm.get("tags")),
        ])
    rows.sort(key=lambda r: (r[0] or "").lower())
    return {"headers": headers, "rows": rows, "count": len(rows)}


def _fetch_vm_power_state(vm_resource_id: Optional[str]) -> str:
    """One extra ARM call per VM (.../instanceView) to get the live
    power state — see _fetch_vms()'s comment above for why this can't
    be folded into the bulk list call. Errors are swallowed (returns
    "unknown") so one VM's transient failure never breaks the whole VM
    table."""
    if not vm_resource_id:
        return "unknown"
    # vm_resource_id is a full ARM id
    # (/subscriptions/{sub}/resourceGroups/{rg}/providers/
    # Microsoft.Compute/virtualMachines/{name}) — strip the
    # "/subscriptions/{sub}" prefix since _arm_get() takes a path
    # relative to ARM_BASE_URL and re-adds nothing itself.
    marker = "/resourceGroups/"
    idx = vm_resource_id.find(marker)
    if idx < 0:
        return "unknown"
    relative_path = vm_resource_id[idx:]  # "/resourceGroups/{rg}/providers/.../{name}"
    sub_prefix = vm_resource_id[:idx]
    subscription_id = sub_prefix.rsplit("/", 1)[-1] if "/" in sub_prefix else None
    if not subscription_id:
        return "unknown"
    try:
        body = _arm_get(f"/subscriptions/{subscription_id}{relative_path}/instanceView", "2024-07-01")
    except AzureError:
        return "unknown"
    statuses = body.get("statuses") or []
    for s in statuses:
        code = s.get("code") or ""
        if code.startswith("PowerState/"):
            return code.split("/", 1)[1]
    return "unknown"


def list_vms(force_refresh: bool = False) -> dict:
    """Every Azure VM in the configured subscription, including live
    power state (fetched via one extra per-VM instanceView call each —
    see _fetch_vm_power_state()). Confirmed via live testing that the
    plain bulk list endpoint never returns power state populated."""
    return _CACHE.get_or_fetch("vms", _fetch_vms, force_refresh=force_refresh)


# ── Azure Virtual Desktop (AVD) ────────────────────────────────────────────

def _fetch_session_hosts_for_pool(host_pool_resource_id: str) -> list[dict]:
    """GET .../hostPools/{name}/sessionHosts — the AVD-native sub-
    resource for a pool's session hosts (VM name is embedded in
    sessionHostName, and the underlying VM's full ARM resourceId is on
    properties.resourceId). Preferred over inferring "which VMs belong
    to this pool" from list_vms()'s tags, since this is the API AVD
    itself uses to report pool membership — the "cm-resource-parent"
    tag observed on session-host VMs is Azure Migrate metadata, not a
    documented/guaranteed AVD contract. Errors are swallowed (returns
    []) so one pool's failure doesn't break the whole AVD table."""
    split = _split_arm_resource_id(host_pool_resource_id)
    if not split:
        return []
    subscription_id, relative_path = split
    try:
        body = _arm_get(f"/subscriptions/{subscription_id}{relative_path}/sessionHosts", "2024-04-03")
    except AzureError:
        return []
    return body.get("value") or []


def _fetch_avd_host_pools() -> dict:
    raw = _arm_list_all_pages("/providers/Microsoft.DesktopVirtualization/hostPools", "2024-04-03")
    # Same rationale as _fetch_vms() above — scope the /skus lookup to
    # only the region(s) these host pools actually live in.
    distinct_locations = tuple(sorted({hp.get("location") for hp in raw if hp.get("location")}))
    size_specs = _get_vm_size_specs(distinct_locations)
    headers = [
        "Host Pool Name", "Resource Group", "Location", "Host Pool Type",
        "Load Balancer Type", "Max Session Limit", "Session Host Count",
        "Session Host VM Size", "vCPUs", "Memory (GB)", "OS Version", "Tags",
    ]
    rows = []
    for hp in raw:
        props = hp.get("properties") or {}
        session_hosts = _fetch_session_hosts_for_pool(hp.get("id") or "")
        host_count = len(session_hosts)
        # A pool's session hosts are usually all provisioned from the
        # same VM size/image template — surface size and OS version
        # once each (with "Mixed" if this particular pool genuinely
        # has heterogeneous values) rather than listing every host's
        # values in this summary row (per-host detail belongs in a
        # future drill-down, not the top-level pool table).
        vm_sizes_seen = set()
        os_versions_seen = set()
        for sh in session_hosts:
            sh_props = sh.get("properties") or {}
            resource_id = sh_props.get("resourceId")
            if resource_id:
                # session host resourceId points at the VM — we don't
                # have the VM's hardwareProfile/storageProfile here
                # without an extra per-VM call, so this relies on
                # list_vms()'s own _fetch_vms() cache already having
                # this info; to avoid an extra dependency/ordering
                # issue, fetch this VM's size + OS version directly
                # (cheap: a single non-instanceView GET, no extra API
                # surface beyond what's already used).
                vm_size, os_version = _fetch_vm_size_and_os_version(resource_id)
                if vm_size:
                    vm_sizes_seen.add(vm_size)
                if os_version:
                    os_versions_seen.add(os_version)
        if len(vm_sizes_seen) == 1:
            session_host_vm_size = next(iter(vm_sizes_seen))
        elif len(vm_sizes_seen) > 1:
            session_host_vm_size = "Mixed"
        else:
            session_host_vm_size = "—"
        if len(os_versions_seen) == 1:
            session_host_os_version = next(iter(os_versions_seen))
        elif len(os_versions_seen) > 1:
            session_host_os_version = "Mixed"
        else:
            session_host_os_version = "—"
        vcpus, memory_gb = _vm_size_spec_columns(
            session_host_vm_size if session_host_vm_size not in ("—", "Mixed") else None,
            size_specs,
        )
        rows.append([
            hp.get("name") or "—",
            _resource_group_from_id(hp.get("id")),
            hp.get("location") or "—",
            props.get("hostPoolType") or "—",
            props.get("loadBalancerType") or "—",
            props.get("maxSessionLimit") if props.get("maxSessionLimit") is not None else "—",
            host_count,
            session_host_vm_size,
            vcpus,
            memory_gb,
            session_host_os_version,
            _tags_to_string(hp.get("tags")),
        ])
    rows.sort(key=lambda r: (r[0] or "").lower())
    return {"headers": headers, "rows": rows, "count": len(rows)}


def _fetch_vm_size_and_os_version(vm_resource_id: str) -> tuple:
    """(vm_size, os_version_display) for one VM, used only by
    _fetch_avd_host_pools() above to resolve a session host's VM size
    + Windows OS version without needing list_vms() to have already
    run. One lightweight GET (no instanceView expand — power state
    isn't needed here). Errors are swallowed (returns (None, None))."""
    split = _split_arm_resource_id(vm_resource_id)
    if not split:
        return None, None
    subscription_id, relative_path = split
    try:
        body = _arm_get(f"/subscriptions/{subscription_id}{relative_path}", "2024-07-01")
    except AzureError:
        return None, None
    props = body.get("properties") or {}
    vm_size = (props.get("hardwareProfile") or {}).get("vmSize")
    os_version = _vm_os_version(props.get("storageProfile") or {})
    return vm_size, os_version


def list_avd_host_pools(force_refresh: bool = False) -> dict:
    """Azure Virtual Desktop host pools, including session host count
    and (when uniform) their VM size/vCPU/Memory — the top-level AVD
    resource (each host pool contains session hosts, which are
    themselves VMs already covered by list_vms() above, but AVD
    exposes its own sessionHosts sub-resource which this uses directly
    rather than inferring pool membership from VM tags). AVD is a
    separate resource provider (Microsoft.DesktopVirtualization) from
    plain Compute, hence a dedicated fetch here."""
    return _CACHE.get_or_fetch("avd_host_pools", _fetch_avd_host_pools, force_refresh=force_refresh)


# ── Resource Groups ────────────────────────────────────────────────────────

def _fetch_resource_groups() -> dict:
    subscription_id = _current_subscription_id()
    raw = _arm_list_all_pages("/resourcegroups", "2021-04-01")
    headers = ["Name", "Location", "Provisioning State", "Tags"]
    rows = []
    for rg in raw:
        props = rg.get("properties") or {}
        rows.append([
            rg.get("name") or "—",
            rg.get("location") or "—",
            props.get("provisioningState") or "—",
            _tags_to_string(rg.get("tags")),
        ])
    rows.sort(key=lambda r: (r[0] or "").lower())
    return {"headers": headers, "rows": rows, "count": len(rows)}


def list_resource_groups(force_refresh: bool = False) -> dict:
    """Every Resource Group in the configured subscription."""
    return _CACHE.get_or_fetch("resource_groups", _fetch_resource_groups, force_refresh=force_refresh)


# ── Network Interfaces (NIC) ───────────────────────────────────────────────

def _fetch_nics() -> dict:
    raw = _arm_list_all_pages("/providers/Microsoft.Network/networkInterfaces", "2023-11-01")
    headers = ["Name", "Resource Group", "Location", "Private IP", "Public IP?", "Attached VM", "NSG", "Tags"]
    rows = []
    for nic in raw:
        props = nic.get("properties") or {}
        ip_configs = props.get("ipConfigurations") or []
        private_ip = "—"
        has_public_ip = "No"
        if ip_configs:
            first = ip_configs[0].get("properties") or {}
            private_ip = first.get("privateIPAddress") or "—"
            if first.get("publicIPAddress"):
                has_public_ip = "Yes"
        vm_ref = props.get("virtualMachine") or {}
        nsg_ref = props.get("networkSecurityGroup") or {}
        rows.append([
            nic.get("name") or "—",
            _resource_group_from_id(nic.get("id")),
            nic.get("location") or "—",
            private_ip,
            has_public_ip,
            (vm_ref.get("id") or "—").split("/")[-1] if vm_ref.get("id") else "—",
            (nsg_ref.get("id") or "—").split("/")[-1] if nsg_ref.get("id") else "—",
            _tags_to_string(nic.get("tags")),
        ])
    rows.sort(key=lambda r: (r[0] or "").lower())
    return {"headers": headers, "rows": rows, "count": len(rows)}


def list_nics(force_refresh: bool = False) -> dict:
    """Every Network Interface (NIC) in the configured subscription."""
    return _CACHE.get_or_fetch("nics", _fetch_nics, force_refresh=force_refresh)


# ── Network Security Groups (NSG) ──────────────────────────────────────────

def _nsg_rule_ip_list(rule_props: dict, field_prefix: str) -> str:
    """One NSG security rule carries EITHER a single
    "{prefix}AddressPrefix" string OR a list under
    "{prefix}AddressPrefixes" — ARM uses the singular field when the
    rule has one address/CIDR/tag ("Internet", "VirtualNetwork", a
    single IP, etc.) and the plural field once a rule has more than
    one. Joined into one comma-separated display string covering both
    shapes."""
    single = rule_props.get(f"{field_prefix}AddressPrefix")
    multiple = rule_props.get(f"{field_prefix}AddressPrefixes") or []
    values = ([single] if single else []) + list(multiple)
    return ", ".join(v for v in values if v) or "Any"


def _nic_id_to_vm_display(nic_id: str, nic_by_id: dict) -> Optional[str]:
    """"VM_NAME" or "VM_NAME (AVD: POOL_NAME)" for one NIC's attached
    VM, or None if the NIC has no VM attached (e.g. a NIC on a
    load balancer frontend / unattached NIC) or the NIC id doesn't
    match anything in this subscription's NIC list. AVD membership is
    inferred from the VM's own "cm-resource-parent" tag (already
    fetched as part of the bulk VM list — see list_vms()'s docstring
    for why this tag, rather than a fresh sessionHosts call per NSG,
    is used here: it's the same Azure Migrate metadata tag already
    proven present on every session-host VM in this org, and re-using
    it avoids N extra sessionHosts calls just to label an NSG's linked
    resources)."""
    nic = nic_by_id.get(nic_id.lower()) if nic_id else None
    if not nic:
        return None
    nic_props = nic.get("properties") or {}
    vm_ref = (nic_props.get("virtualMachine") or {}).get("id")
    if not vm_ref:
        return None
    vm_name = vm_ref.split("/")[-1]
    return vm_name


def _fetch_nsgs() -> dict:
    raw = _arm_list_all_pages("/providers/Microsoft.Network/networkSecurityGroups", "2023-11-01")
    # NICs are fetched once up-front (not per-NSG) so resolving "which
    # VM is this NSG's linked NIC attached to" is a local dict lookup,
    # not an extra ARM call per NSG — NIC ids are looked up
    # case-insensitively since ARM resource ids are inconsistently
    # cased across API versions/providers (same rationale as
    # _resource_group_from_id() above).
    nics_raw = _arm_list_all_pages("/providers/Microsoft.Network/networkInterfaces", "2023-11-01")
    nic_by_id = {nic.get("id", "").lower(): nic for nic in nics_raw if nic.get("id")}
    # VM tags are fetched once up-front too, so an NSG's linked VM can
    # be labelled "(AVD: pool-name)" when that VM is an AVD session
    # host, without a per-NSG or per-VM extra call.
    vms_raw = _arm_list_all_pages("/providers/Microsoft.Compute/virtualMachines", "2024-07-01")
    avd_pool_by_vm_name: dict = {}
    for vm in vms_raw:
        vm_name = vm.get("name")
        cm_parent = (vm.get("tags") or {}).get("cm-resource-parent")
        if vm_name and cm_parent and "Microsoft.DesktopVirtualization" in cm_parent:
            avd_pool_by_vm_name[vm_name] = cm_parent.split("/")[-1]

    headers = ["Name", "Resource Group", "Location", "Security Rules", "Attached NICs", "Attached Subnets", "Linked VM/AVD", "Tags"]
    rows = []
    # Parallel array to `rows` (same index), collapsed to `None` for any
    # NSG with zero rules — the frontend only renders an expand
    # control when this entry is non-None, so an NSG with no rules gets
    # no expand affordance rather than an empty expandable panel.
    row_details = []
    for nsg in raw:
        props = nsg.get("properties") or {}
        security_rules = props.get("securityRules") or []
        linked_nic_refs = props.get("networkInterfaces") or []
        linked_vm_names = []
        for nic_ref in linked_nic_refs:
            nic_id = nic_ref.get("id")
            vm_name = _nic_id_to_vm_display(nic_id, nic_by_id) if nic_id else None
            if not vm_name:
                continue
            avd_pool = avd_pool_by_vm_name.get(vm_name)
            linked_vm_names.append(f"{vm_name} (AVD: {avd_pool})" if avd_pool else vm_name)
        rows.append([
            nsg.get("name") or "—",
            _resource_group_from_id(nsg.get("id")),
            nsg.get("location") or "—",
            len(security_rules),
            len(linked_nic_refs),
            len(props.get("subnets") or []),
            ", ".join(linked_vm_names) if linked_vm_names else "—",
            _tags_to_string(nsg.get("tags")),
        ])
        if not security_rules:
            row_details.append(None)
            continue
        rule_rows = []
        for rule in security_rules:
            rule_props = rule.get("properties") or {}
            rule_rows.append({
                "name": rule.get("name") or "—",
                "priority": rule_props.get("priority"),
                "direction": rule_props.get("direction") or "—",
                "access": rule_props.get("access") or "—",
                "protocol": rule_props.get("protocol") or "—",
                "source": _nsg_rule_ip_list(rule_props, "source"),
                "source_port": rule_props.get("sourcePortRange") or ", ".join(rule_props.get("sourcePortRanges") or []) or "Any",
                "destination": _nsg_rule_ip_list(rule_props, "destination"),
                "destination_port": rule_props.get("destinationPortRange") or ", ".join(rule_props.get("destinationPortRanges") or []) or "Any",
            })
        # Sort by priority (Azure evaluates NSG rules in priority order,
        # lowest number first) so the expanded panel mirrors how the
        # rules actually take effect, not raw API return order.
        rule_rows.sort(key=lambda r: r["priority"] if r["priority"] is not None else 999999)
        row_details.append({"type": "nsg_rules", "rules": rule_rows})
    # Sort rows and row_details together (by NSG name) so their indices
    # stay aligned — row_details[i] must always describe rows[i].
    combined = list(zip(rows, row_details))
    combined.sort(key=lambda pair: (pair[0][0] or "").lower())
    rows = [pair[0] for pair in combined]
    row_details = [pair[1] for pair in combined]
    return {"headers": headers, "rows": rows, "row_details": row_details, "count": len(rows)}


def list_nsgs(force_refresh: bool = False) -> dict:
    """Every Network Security Group (NSG) in the configured
    subscription, plus each NSG's full security-rule list (source/
    destination IP or CIDR, ports, protocol, direction, priority) in
    `row_details` — the frontend renders this collapsed by default,
    expanding only on click (see AzureResourcesPage.jsx's
    NsgRuleDetailPanel) — and a "Linked VM/AVD" column naming which
    VM(s) the NSG's attached NIC(s) belong to, with an "(AVD: pool-
    name)" suffix for any linked VM that's an AVD session host."""
    return _CACHE.get_or_fetch("nsgs", _fetch_nsgs, force_refresh=force_refresh)


# ── WAF (Application Gateway WAF policies) ─────────────────────────────────
#
# NOTE: this org's WAF could be either Application Gateway's WAF (this
# provider path) or Azure Front Door's WAF (a different provider path,
# Microsoft.Network/frontDoorWebApplicationFirewallPolicies) — confirmed
# via live testing once credentials are available; if the org uses
# Front Door instead, this fetch needs a second provider path added (or
# swapped) rather than assuming Application Gateway.

def _fetch_waf_policies() -> dict:
    raw = _arm_list_all_pages(
        "/providers/Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies",
        "2023-11-01",
    )
    headers = ["Name", "Resource Group", "Location", "Policy State", "Mode", "Managed Rule Sets", "Custom Rules", "Tags"]
    rows = []
    for waf in raw:
        props = waf.get("properties") or {}
        policy_settings = props.get("policySettings") or {}
        managed_rules = props.get("managedRules") or {}
        rows.append([
            waf.get("name") or "—",
            _resource_group_from_id(waf.get("id")),
            waf.get("location") or "—",
            policy_settings.get("state") or "—",
            policy_settings.get("mode") or "—",
            len(managed_rules.get("managedRuleSets") or []),
            len(props.get("customRules") or []),
            _tags_to_string(waf.get("tags")),
        ])
    rows.sort(key=lambda r: (r[0] or "").lower())
    return {"headers": headers, "rows": rows, "count": len(rows)}


def list_waf_policies(force_refresh: bool = False) -> dict:
    """Every Application Gateway WAF policy in the configured
    subscription. See the module-level note above re: Front Door WAF as
    an alternative that may need its own fetch if this org uses that
    instead."""
    return _CACHE.get_or_fetch("waf_policies", _fetch_waf_policies, force_refresh=force_refresh)


# ── Cost (Azure Cost Management API) ───────────────────────────────────────
#
# Cost Management's query API is a POST (not a simple list GET) that
# needs an explicit time range + aggregation/grouping spec — shaped
# roughly like AWS Cost Explorer's GetCostAndUsage, but Azure's own
# request/response schema. Requests cost data grouped by service name
# for the last 30 days, mirroring cost_explorer.py's own "cost by
# service" breakdown for the AWS Cost Analysis tab.
#
# NOTE: Cost Management Reader role may be required in ADDITION to plain
# Reader for this specific call to succeed (see azure_monitor.py's
# module docstring / the user-facing setup instructions this project
# gave — Reader is usually sufficient in practice, but this is
# unconfirmed until live-tested).

COST_MANAGEMENT_API_VERSION = "2023-11-01"


def _fetch_cost_summary() -> dict:
    subscription_id = _current_subscription_id()
    path = f"/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/query"
    body = {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ServiceName"}],
        },
    }
    url = f"{ARM_BASE_URL}{path}"
    token = _get_token()

    def _post(tok: str):
        return httpx.post(
            url, params={"api-version": COST_MANAGEMENT_API_VERSION}, json=body,
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    try:
        resp = _post(token)
    except httpx.HTTPError as e:
        raise AzureError(f"Could not reach Azure Cost Management API: {e}") from e
    if resp.status_code == 401:
        token = _get_token(force_refresh=True)
        try:
            resp = _post(token)
        except httpx.HTTPError as e:
            raise AzureError(f"Azure Cost Management retry after token refresh failed: {e}") from e
    if resp.status_code >= 400:
        try:
            err_body = resp.json()
            detail = ((err_body or {}).get("error") or {}).get("message") or err_body
        except ValueError:
            detail = resp.text
        raise AzureError(f"Azure Cost Management query failed ({resp.status_code}): {detail}")
    try:
        result = resp.json() or {}
    except ValueError as e:
        raise AzureError(f"Azure Cost Management API returned invalid JSON: {e}") from e

    props = result.get("properties") or {}
    columns = [c.get("name") for c in (props.get("columns") or [])]
    rows_raw = props.get("rows") or []
    try:
        cost_idx = columns.index("Cost")
    except ValueError:
        cost_idx = 0
    try:
        service_idx = columns.index("ServiceName")
    except ValueError:
        service_idx = 1
    try:
        currency_idx = columns.index("Currency")
    except ValueError:
        currency_idx = None

    headers = ["Service", "Cost", "Currency"]
    rows = []
    total_cost = 0.0
    currency = "—"
    for r in rows_raw:
        cost = float(r[cost_idx]) if cost_idx < len(r) and r[cost_idx] is not None else 0.0
        service = r[service_idx] if service_idx < len(r) else "—"
        cur = (r[currency_idx] if currency_idx is not None and currency_idx < len(r) else None) or currency
        currency = cur
        total_cost += cost
        rows.append([service or "—", round(cost, 2), cur])
    rows.sort(key=lambda r: r[1], reverse=True)

    return {
        "headers": headers, "rows": rows, "count": len(rows),
        "total_cost": round(total_cost, 2), "currency": currency,
        "timeframe": "Month to date",
    }


def get_cost_summary(force_refresh: bool = False) -> dict:
    """Month-to-date Azure cost, broken down by service — powers the
    Cost sub-tab. Cached for 1 hour (see _COST_CACHE) since Cost
    Management's own data has a multi-hour reporting lag anyway, so
    refreshing more often than that would not surface newer data."""
    return _COST_CACHE.get_or_fetch("cost_summary", _fetch_cost_summary, force_refresh=force_refresh)


# ── Multiple subscriptions (Cloud Accounts registry) ──────────────────────
#
# Every list function above was written for ONE subscription (the one in the
# credentials secret). To cover the subscriptions registered on the Cloud
# Accounts page without threading a parameter through every helper, the
# subscription in effect is held in a ContextVar: _current_subscription_id()
# returns the override when set, otherwise the secret's subscription exactly as
# before. Nothing in this module starts threads, so the override is seen by all
# the calls made for one fetch.

_subscription_override: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "azure_subscription_override", default=None
)

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _current_subscription_id() -> str:
    return _subscription_override.get() or _get_credentials()["subscription_id"]


def _with_subscription(subscription_id: str, fn):
    """Run fn() with ARM calls scoped to `subscription_id`."""
    token = _subscription_override.set(subscription_id)
    try:
        return fn()
    finally:
        _subscription_override.reset(token)


# list function name -> (cache key, fetch function). Looked up lazily so test
# patches of the fetch functions are honoured.
def _fetcher_for(list_fn_name: str):
    table = {
        "list_vms": ("vms", "_fetch_vms"),
        "list_avd_host_pools": ("avd_host_pools", "_fetch_avd_host_pools"),
        "list_resource_groups": ("resource_groups", "_fetch_resource_groups"),
        "list_nics": ("nics", "_fetch_nics"),
        "list_nsgs": ("nsgs", "_fetch_nsgs"),
    }
    if list_fn_name not in table:
        raise AzureError(f"Unknown Azure list function: {list_fn_name}")
    cache_key, fetch_name = table[list_fn_name]
    return cache_key, globals()[fetch_name]


def _connected_subscriptions() -> list[tuple[str, str]]:
    """[(subscription_id, display_name)] for the Azure subscriptions the registry
    currently shows (seeded default or connected, not offboarded)."""
    from . import cloud_account_store

    return [(e["identifier"], e.get("display_name") or e["identifier"])
            for e in cloud_account_store.active_accounts("azure")]


def _default_subscription_id() -> Optional[str]:
    try:
        return (_get_credentials() or {}).get("subscription_id")
    except Exception:
        return None


def list_connected(list_fn_name: str, force_refresh: bool = False) -> dict:
    """Run one of the list_*() functions over every connected subscription.

    With zero or one connected subscription that is the configured default, this
    simply calls the original list function, so the output and its cache are
    exactly what they were before the registry existed. Otherwise rows from all
    subscriptions are merged, a "Subscription" column (the registry Display_Name)
    is added, and a subscription that fails is reported in "errors" without
    hiding the others (raises only if every subscription fails)."""
    try:
        subs = _connected_subscriptions()
    except Exception as e:
        logger.warning("azure_monitor: could not read the subscription registry (%s); using the default", e)
        subs = []
    default_id = (_default_subscription_id() or "").lower()
    if not subs or (len(subs) == 1 and subs[0][0].lower() == default_id):
        return globals()[list_fn_name](force_refresh=force_refresh)

    cache_key, fetch_fn = _fetcher_for(list_fn_name)
    show_column = len(subs) > 1
    merged_rows: list = []
    merged_details: list = []
    headers: Optional[list] = None
    has_details = False
    errors: list[dict] = []

    for sub_id, sub_name in subs:
        try:
            part = _CACHE.get_or_fetch(
                f"{cache_key}:{sub_id}", lambda s=sub_id: _with_subscription(s, fetch_fn), force_refresh=force_refresh
            )
        except AzureError as e:
            logger.warning("azure_monitor: %s failed for subscription %s: %s", list_fn_name, sub_id, e)
            errors.append({"subscription": sub_name, "message": "Could not read this subscription."})
            continue
        if headers is None:
            headers = list(part.get("headers") or [])
        details = part.get("row_details")
        has_details = has_details or details is not None
        for i, row in enumerate(part.get("rows") or []):
            merged_rows.append(list(row) + [sub_name] if show_column else list(row))
            merged_details.append(details[i] if details is not None and i < len(details) else None)

    if headers is None:
        raise AzureError("Could not read any of the connected Azure subscriptions.")
    if show_column:
        headers = headers + ["Subscription"]

    order = sorted(range(len(merged_rows)), key=lambda i: (str(merged_rows[i][0] or "").lower(), i))
    rows = [merged_rows[i] for i in order]
    out = {"headers": headers, "rows": rows, "count": len(rows)}
    if has_details:
        out["row_details"] = [merged_details[i] for i in order]
    if errors:
        out["errors"] = errors
    return out


# ── Connection test and onboarding guide ──────────────────────────────────

def test_azure_subscription(subscription_id: str) -> dict:
    """Can the Portal's Service Principal read this subscription? Never raises.
    Returns {"ok", "code", "message"}; messages are written here, never raw Azure text."""
    if not _GUID_RE.match(subscription_id or ""):
        return {"ok": False, "code": "invalid_id", "message": "That is not a valid Azure subscription ID."}
    try:
        body = _arm_get(f"/subscriptions/{subscription_id}", "2022-12-01")
    except AzureError as e:
        text = str(e)
        m = re.search(r"\((\d{3})\)", text)
        status = int(m.group(1)) if m else None
        if text.startswith(("Azure AD token", "Could not load Azure", "Secret ", "Azure API credentials")):
            return {"ok": False, "code": "portal_credentials",
                    "message": "The Portal could not sign in to Azure. Contact a Portal admin."}
        if text.startswith("Could not reach"):
            return {"ok": False, "code": "network", "message": "Could not reach Azure. Try again in a moment."}
        if status in (403, 404):
            return {"ok": False, "code": "not_granted", "message": (
                "The Portal's Service Principal cannot see this subscription yet. Grant it the Reader "
                "role on the subscription (see the guide) and try again, or check the subscription ID.")}
        return {"ok": False, "code": "azure_error",
                "message": f"Azure returned an error{f' ({status})' if status else ''}."}
    except Exception:
        logger.exception("azure_monitor: unexpected error testing subscription %s", subscription_id)
        return {"ok": False, "code": "unexpected", "message": "Unexpected error while testing the connection."}

    returned = str((body or {}).get("subscriptionId") or "").lower()
    if returned != subscription_id.lower():
        return {"ok": False, "code": "invalid_id", "message": "Azure did not return the subscription that was entered."}
    state = str((body or {}).get("state") or "")
    if state.lower() != "enabled":
        return {"ok": False, "code": "disabled",
                "message": f"The subscription is {state or 'not enabled'}. Only enabled subscriptions can be read."}
    return {"ok": True, "code": "ok", "message": "Connected."}


def azure_guide(subscription_id: Optional[str] = None) -> dict:
    """What a subscription owner needs to grant the Portal read access: the
    Service Principal's application (client) ID and the Azure CLI command. The
    client SECRET is never included."""
    if subscription_id is not None and not _GUID_RE.match(subscription_id):
        raise ValueError("invalid subscription ID")
    creds = _get_credentials()
    client_id = creds["client_id"]
    sub = subscription_id or "<SUBSCRIPTION_ID>"
    return {
        "application_id": client_id,
        "tenant_id": creds["tenant_id"],
        "role": "Reader",
        "command": f"az role assignment create --assignee {client_id} --role Reader --scope /subscriptions/{sub}",
    }
