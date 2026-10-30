"""
"Fortigate Firewall" sub-tab of the "Local Infra Hardware Monitor" page —
a health dashboard built on top of the FortiManager Cloud JSON-RPC API
(https://<instance>.fortimanager.forticloud.com), added per user request
as a third hardware-monitoring source alongside Cisco Meraki
(meraki_monitor.py) and MySonicWall (sonicwall_monitor.py).

## Endpoint / instance

This portal talks to a single FortiManager Cloud instance:
https://your-instance.region.fortimanager.forticloud.com
(FORTIGATE_INSTANCE_URL below — overridable via env var for local dev
or if the instance URL ever changes).

## Authentication — 3-step session flow (deliberately NOT a single
## static bearer key, unlike Meraki/SonicWall above)

FortiManager Cloud does not accept a single static API key the way
Meraki/SonicWall do. Per Fortinet's own documented flow (see
https://docs.fortinet.com/document/fortimanager-cloud/7.2.1/cloud-
deployment/510042 and the community "Set up an API call to FortiManager
or FortiAnalyzer Cloud" tip), reaching real device data takes three
chained calls:

1. POST https://customerapiauth.fortinet.com/api/v1/oauth/token/
   with {"username": <API user ID>, "password": <API user password>,
   "client_id": "FortiManager", "grant_type": "password"}
   -> {"access_token": "..."}  (short-lived OAuth2 token from FortiCare)

2. POST https://<instance>/p/forticloud_jsonrpc_login/
   with {"access_token": "<from step 1>"}
   -> {"session": "..."}  (a FortiManager JSON-RPC session id)

3. POST https://<instance>/jsonrpc
   with {"method": "get", "params": [{"url": "/dvmdb/device"}],
         "session": "<from step 2>", "id": 1}
   -> the actual device list/status data

The "username"/"password" here are a FortiCloud IAM API user's
credentials (a UUID-shaped API ID + a generated password) — NOT a
device-local admin account. This module caches the resulting session id
for _SESSION_TTL_SECONDS and transparently re-authenticates (redoing all
3 steps) if a request comes back with a session-expired error code, so
callers never see the multi-step handshake.

## Credentials

Local dev can set FORTIGATE_API_ID / FORTIGATE_API_PASSWORD directly to
skip Secrets Manager; production loads Secrets Manager secret
`job-portal/fortigate-api-credentials` (a JSON object:
{"api_id": "...", "password": "..."}), same "read server-side only,
never exposed to the frontend bundle or Git" convention as
meraki_monitor.py / sonicwall_monitor.py.

## Data surfaced

GET /dvmdb/device (across all ADOMs) returns every FortiGate managed by
this FortiManager instance, with fields including hostname, ip,
conn_status (1 = up, 0 = down — the real-time connectivity signal,
unlike SonicWall's provisioning-ratio proxy above), conf_status (config
sync state), os_ver/os_type, platform_str (model), and ha_mode. This is
genuine real-time device health (unlike sonicwall_monitor.py's
provisioning-ratio simulation), so labelled "Up"/"Down" directly rather
than "simulated".
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx

from .cache_utils import TTLCache

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")  # region for Secrets Manager (portal's own account)

# FortiCloud's shared OAuth2 token endpoint (same for every customer/
# instance — this is FortiCare's own auth server, not the FortiManager
# instance itself).
FORTICLOUD_OAUTH_URL = "https://customerapiauth.fortinet.com/api/v1/oauth/token/"

# This portal's single FortiManager Cloud instance.
FORTIGATE_INSTANCE_URL = os.environ.get(
    "FORTIGATE_INSTANCE_URL", "https://your-instance.region.fortimanager.forticloud.com",
)
FORTIGATE_CREDENTIALS_SECRET_NAME = os.environ.get(
    "FORTIGATE_CREDENTIALS_SECRET_NAME", "job-portal/fortigate-api-credentials",
)

_REQUEST_TIMEOUT_SECONDS = 20.0

# Device-list cache — mirrors sonicwall_monitor.py's single-entry
# TTLCache convention. 5 minutes: device conn_status is a genuinely
# real-time signal (unlike SonicWall's provisioning ratios), so a
# shorter TTL than SonicWall's 10 minutes is appropriate, matching
# meraki_monitor.py's _CACHE_SHORT for similarly live data.
_CACHE = TTLCache(ttl_seconds=5 * 60)

# Cached FortiManager JSON-RPC session id + when it was obtained.
# FortiManager's documented default ADMIN IDLE TIMEOUT is 900 seconds
# (15 minutes) — see https://docs.fortinet.com/document/fortimanager/
# 7.4.2/administration-guide/151959 — and this API session appears to
# be subject to the same idle limit in practice (observed -11 errors on
# requests made well after the previous successful call). Kept well
# under that at 10 minutes so a request is very unlikely to ever hit an
# idle-expired session in normal use; _jsonrpc_call's retry-once-on-
# error-11-or-similar logic below is the real safety net regardless.
_SESSION_TTL_SECONDS = 10 * 60
_session_cache: dict = {"session": None, "obtained_at": 0.0}

# FortiManager JSON-RPC error codes that mean "treat this as a possibly-
# stale session and retry once with a fresh login" — observed values
# from Fortinet's own API error table, PLUS -11 ("No permission for the
# resource"). -11 was originally assumed to mean only "the API user's
# permission profile is too narrow" (e.g. lacking ADOM access), but in
# production this code was also observed on a request that had
# succeeded moments earlier with the exact same credentials/permissions
# — i.e. FortiManager Cloud can return -11 for a genuinely expired/
# invalid session too, not just a real permission gap. Retrying once
# with force_refresh=True is safe either way: if it's actually a
# permission problem, the retry fails with the same -11 and that's
# reported to the caller as-is; if it was a stale session, the retry
# succeeds and the caller never sees an error.
_SESSION_INVALID_CODES = {-11009, -6, -22, -11}


class FortigateError(ValueError):
    """Raised for invalid input or a total inability to reach the
    FortiManager Cloud API. Safe to show the user as-is."""


# ── Credentials ─────────────────────────────────────────────────────────

def _get_secret_json(secret_name: str) -> dict:
    import boto3
    import json
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = client.get_secret_value(SecretId=secret_name)
    raw = (resp.get("SecretString") or "").strip()
    if not raw:
        raise FortigateError(f"Secret {secret_name} is empty.")
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise FortigateError(f"Secret {secret_name} is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise FortigateError(f"Secret {secret_name} must be a JSON object with api_id/password.")
    return parsed


def _get_credentials() -> tuple[str, str]:
    """(api_id, password) — local-dev-env-var-override convenience, same
    pattern as every other cross-service credential in this codebase
    (see meraki_monitor.py's _get_api_key())."""
    env_id = os.environ.get("FORTIGATE_API_ID")
    env_pw = os.environ.get("FORTIGATE_API_PASSWORD")
    if env_id and env_pw:
        return env_id, env_pw
    try:
        parsed = _get_secret_json(FORTIGATE_CREDENTIALS_SECRET_NAME)
    except FortigateError:
        raise
    except Exception as e:
        raise FortigateError(f"Could not load FortiGate API credentials: {e}") from e
    api_id = parsed.get("api_id") or parsed.get("FORTIGATE_API_ID")
    password = parsed.get("password") or parsed.get("FORTIGATE_API_PASSWORD")
    if not api_id or not password:
        raise FortigateError("FortiGate API credentials secret is missing api_id/password.")
    return api_id, password


# ── 3-step session handshake ──────────────────────────────────────────────

def _fetch_access_token() -> str:
    api_id, password = _get_credentials()
    try:
        resp = httpx.post(
            FORTICLOUD_OAUTH_URL,
            json={
                "username": api_id, "password": password,
                "client_id": "FortiManager", "grant_type": "password",
            },
            headers={"Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise FortigateError(f"Could not reach FortiCloud OAuth endpoint: {e}") from e
    if resp.status_code >= 400:
        # Same rationale as _fetch_session()'s error handling below —
        # surface FortiCloud's actual response body (e.g. "invalid
        # credentials", "account disabled") instead of httpx's generic
        # raise_for_status() message.
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise FortigateError(f"FortiCloud OAuth login failed ({resp.status_code}): {detail}")
    try:
        body = resp.json()
    except ValueError as e:
        raise FortigateError(f"FortiCloud OAuth endpoint returned invalid JSON: {e}") from e
    token = (body or {}).get("access_token")
    if not token:
        raise FortigateError("FortiCloud OAuth login did not return an access_token.")
    return token


def _fetch_session(access_token: str) -> str:
    url = f"{FORTIGATE_INSTANCE_URL}/p/forticloud_jsonrpc_login/"
    try:
        resp = httpx.post(
            url, json={"access_token": access_token},
            headers={"Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise FortigateError(f"Could not reach FortiManager Cloud login endpoint ({url}): {e}") from e
    if resp.status_code >= 400:
        # Surface the ACTUAL response body (e.g. FortiManager's own
        # {"message": "Too many login sessions. You need to disconnect
        # some to continue."}) rather than httpx's generic
        # raise_for_status() message, which discards the body — the
        # caller-facing error message below specifically pattern-
        # matches on this exact body text.
        try:
            detail = resp.json().get("message") or resp.text
        except ValueError:
            detail = resp.text
        raise FortigateError(f"FortiManager Cloud session login failed ({resp.status_code}): {detail}")
    try:
        body = resp.json()
    except ValueError as e:
        raise FortigateError(f"FortiManager Cloud login endpoint returned invalid JSON: {e}") from e
    session = (body or {}).get("session")
    if not session:
        raise FortigateError("FortiManager Cloud login did not return a session id.")
    return session


def _logout_session(session: str) -> None:
    """Best-effort JSON-RPC logout ("exec" /sys/logout) for a session
    this module is about to discard — releases the API user's login
    slot on the FortiManager/FortiCloud side. Never raises: if a
    session is already stale/invalid, logging it out will itself fail
    with some error, which is fine to ignore since the goal (freeing
    that slot) is either already satisfied or unattainable either way.
    Without this, every _get_session(force_refresh=True) call leaks
    one login session that is never released until FortiManager's own
    idle timeout eventually reclaims it — which is exactly how this API
    user's login-session cap got exhausted in production (see the
    "Too many login sessions" FortigateError message below), since
    force_refresh happens automatically on every _SESSION_INVALID_CODES
    retry."""
    if not session:
        return
    try:
        httpx.post(
            f"{FORTIGATE_INSTANCE_URL}/jsonrpc",
            json={"method": "exec", "params": [{"url": "/sys/logout"}], "session": session, "id": 1},
            headers={"Content-Type": "application/json"},
            timeout=5.0,
        )
    except httpx.HTTPError:
        pass


def _get_session(force_refresh: bool = False) -> str:
    now = time.monotonic()
    cached = _session_cache.get("session")
    age = now - (_session_cache.get("obtained_at") or 0.0)
    if cached and not force_refresh and age < _SESSION_TTL_SECONDS:
        return cached
    if force_refresh and cached:
        # Release the old session's login slot BEFORE requesting a new
        # one, rather than just abandoning it — see _logout_session()'s
        # docstring for why this matters.
        _logout_session(cached)
    try:
        access_token = _fetch_access_token()
        session = _fetch_session(access_token)
    except FortigateError as e:
        if "Too many login sessions" in str(e):
            # This API user has genuinely exhausted its FortiCloud login-
            # session cap (observed after repeated force_refresh cycles
            # without a logout step existing yet — now fixed above, but
            # kept as a clear, actionable error for any sessions that
            # were already leaked before this fix, or if the cap is
            # simply set too low for this workload). Not retried here:
            # retrying immediately would not help, since the cap is a
            # server-side count that only drains via logout or idle
            # timeout, neither of which this immediate retry causes.
            raise FortigateError(
                "FortiManager Cloud rejected login: too many active sessions for this API user. "
                "Ask a FortiManager admin to disconnect stale sessions for this API user, or "
                "increase its login-max setting, then try again in a few minutes."
            ) from e
        raise
    _session_cache["session"] = session
    _session_cache["obtained_at"] = now
    return session


# ── JSON-RPC calls ──────────────────────────────────────────────────────

def _jsonrpc_call(url_path: str, fields: Optional[list[str]] = None) -> list:
    """POSTs a single JSON-RPC "get" request to /jsonrpc, transparently
    obtaining a session first (or reusing the cached one). If the
    response indicates the session is no longer valid, redoes the full
    3-step handshake exactly once and retries — callers never see the
    session-management plumbing."""
    rpc_url = f"{FORTIGATE_INSTANCE_URL}/jsonrpc"
    param: dict = {"url": url_path}
    if fields:
        param["fields"] = fields

    def _post(session: str):
        return httpx.post(
            rpc_url,
            json={"method": "get", "params": [param], "session": session, "id": 1, "verbose": 1},
            headers={"Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    session = _get_session()
    try:
        resp = _post(session)
    except httpx.HTTPError as e:
        raise FortigateError(f"Could not reach FortiManager Cloud JSON-RPC endpoint: {e}") from e

    try:
        body = resp.json()
    except ValueError as e:
        raise FortigateError(f"FortiManager Cloud returned invalid JSON for {url_path}: {e}") from e

    results = (body or {}).get("result") or []
    top_status = results[0].get("status") if results else None
    code = (top_status or {}).get("code")

    if code in _SESSION_INVALID_CODES:
        # Session expired/invalid — redo the full handshake once and retry.
        session = _get_session(force_refresh=True)
        try:
            resp = _post(session)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise FortigateError(f"FortiManager Cloud retry after session refresh failed: {e}") from e
        results = (body or {}).get("result") or []
        top_status = results[0].get("status") if results else None
        code = (top_status or {}).get("code")

    if code not in (0, None):
        message = (top_status or {}).get("message") or "Unknown error"
        raise FortigateError(f"FortiManager Cloud API error for {url_path}: [{code}] {message}")

    if not results:
        return []
    data = results[0].get("data")
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


# ── Devices (the one real data fetch) ────────────────────────────────────

def _conn_status_label(conn_status) -> str:
    # FortiManager returns conn_status as either an int (1 = up, 0 = down)
    # or occasionally a string ("up"/"down") depending on API version —
    # tolerate both rather than assuming one shape.
    if isinstance(conn_status, str):
        return "up" if conn_status.lower() == "up" else "down"
    return "up" if conn_status == 1 else "down"


def _fetch_devices() -> dict:
    raw_devices = _jsonrpc_call("/dvmdb/device")
    devices = []
    for d in raw_devices:
        if not isinstance(d, dict):
            continue
        devices.append({
            "name": d.get("name") or d.get("hostname") or "—",
            "hostname": d.get("hostname"),
            "ip": d.get("ip"),
            "serial": d.get("sn"),
            "platform": d.get("platform_str") or d.get("dev_type"),
            "os_version": d.get("os_ver"),
            "conn_status": _conn_status_label(d.get("conn_status")),
            "conf_status": d.get("conf_status") or "unknown",
            "ha_mode": d.get("ha_mode"),
            "adom": d.get("adom_name") or d.get("adom"),
            "last_checked": d.get("last_checked"),
            # Location — reported by the device itself via FortiManager's
            # geolocation feature (Device Manager map view); None for
            # devices that never reported coordinates. Frontend renders
            # these as a "view on map" link rather than trying to embed
            # a map component.
            "latitude": d.get("latitude"),
            "longitude": d.get("longitude"),
            # Unix timestamp of when this device's management tunnel to
            # FortiManager FIRST came up — used as a "Connected since"
            # proxy for uptime. Deliberately NOT labelled "device uptime"
            # in the API/UI: FortiOS's monitor API has no such field (a
            # FortiGate's OS uptime and its FortiManager tunnel uptime
            # are two different things, and only the latter is available
            # here) — see get_device_detail()'s docstring for the fuller
            # explanation surfaced to the frontend.
            "first_tunnel_up": d.get("first_tunnel_up"),
            # Number of FortiSwitches this FortiGate manages via
            # FortiLink — used by list_switches() below to skip proxy
            # calls to devices with none, rather than proxy-calling
            # every device "just in case".
            "fsw_cnt": d.get("fsw_cnt") or 0,
        })
    devices.sort(key=lambda d: (d.get("name") or "").lower())
    up_count = sum(1 for d in devices if d["conn_status"] == "up")
    return {
        "devices": devices,
        "count": len(devices),
        "up_count": up_count,
        "down_count": len(devices) - up_count,
    }


def list_devices(force_refresh: bool = False) -> dict:
    """Every FortiGate managed by this FortiManager Cloud instance, with
    real-time connection/config status. Powers the Fortigate Firewall
    sub-tab's device table."""
    return _CACHE.get_or_fetch("devices", _fetch_devices, force_refresh)


def get_overview(force_refresh: bool = False) -> dict:
    """Org-wide summary derived from list_devices() — device counts and
    an overall "up %" figure. Unlike sonicwall_monitor.py's provisioning-
    ratio proxy, this IS a genuine real-time health signal (conn_status
    reflects whether FortiManager currently has a live management
    connection to the device)."""
    data = list_devices(force_refresh)
    total = data["count"]
    up_pct = (data["up_count"] / total * 100) if total > 0 else None
    return {
        "device_count": total,
        "up_count": data["up_count"],
        "down_count": data["down_count"],
        "up_pct": up_pct,
    }


# ── Per-device live detail (CPU/Memory, Interface Bandwidth, VPN Tunnel
# status) — a FOURTH, more expensive data source, deliberately NOT
# fetched for every device on page load. FortiManager itself does not
# store this data (dvmdb only has inventory/connection-status fields,
# as used by list_devices() above) — it has to be proxied through to
# each individual FortiGate's own FortiOS REST API over the FGFM tunnel
# ("sys/proxy/json" exec calls, one HTTP round-trip per device per
# resource). Fetching this for all ~27 devices on every page load would
# mean 27 x 3 = ~81 proxy calls per load, which is both slow and risks
# tripping FortiManager Cloud's rate limiting — so this is deliberately
# a per-device, on-demand fetch, triggered only when a user expands one
# device's detail row in the frontend. See main.py's
# get_fortigate_device_detail for the route.

# Separate, shorter-TTL cache for per-device detail — 1 minute, since
# CPU/interface/VPN figures are genuinely live and a stale 5-10 minute
# cache (like the device list above) would defeat the point of a
# dashboard someone just clicked to check right now.
_DETAIL_CACHE = TTLCache(ttl_seconds=60)


def _exec_proxy_call(device_name: str, resource: str) -> dict:
    """Runs one FortiManager "sys/proxy/json" exec call, which forwards
    a FortiOS REST API GET (over the existing FGFM management tunnel)
    to a single managed FortiGate and returns its response — see
    https://community.fortinet.com/t5/FortiManager/Technical-Tip-How-to-
    run-a-proxy-API-call-from-FortiManager-to-a/ta-p/282784. This is a
    DIFFERENT JSON-RPC shape from _jsonrpc_call() above (method "exec",
    not "get", and the interesting payload is nested under
    result[0].data[0].response), so it is not built on top of that
    function. Session handling (obtain/reuse/retry-once-on-error) is
    duplicated here rather than shared with _jsonrpc_call(), since the
    response-parsing logic genuinely differs; if this pattern needs a
    third exec-style call in future, consider factoring the session
    handling into a shared helper then. Returns the FortiGate's own
    "response" object (which has its own "status"/"results" keys,
    distinct from FortiManager's proxy-call status), or raises
    FortigateError if the proxy call itself failed (e.g. device
    offline, resource path doesn't exist on that FortiOS version)."""
    rpc_url = f"{FORTIGATE_INSTANCE_URL}/jsonrpc"
    proxy_body = {"target": [f"device/{device_name}"], "action": "get", "resource": resource}

    def _post(session: str):
        return httpx.post(
            rpc_url,
            json={
                "id": 1, "method": "exec",
                "params": [{"url": "sys/proxy/json", "data": proxy_body}],
                "session": session,
            },
            headers={"Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    session = _get_session()
    try:
        resp = _post(session)
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise FortigateError(f"Could not reach FortiManager Cloud for proxy call to {device_name}: {e}") from e

    results = (body or {}).get("result") or []
    top_status = results[0].get("status") if results else None
    code = (top_status or {}).get("code")

    if code in _SESSION_INVALID_CODES:
        session = _get_session(force_refresh=True)
        try:
            resp = _post(session)
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise FortigateError(f"FortiManager Cloud retry after session refresh failed: {e}") from e
        results = (body or {}).get("result") or []
        top_status = results[0].get("status") if results else None
        code = (top_status or {}).get("code")

    if code not in (0, None):
        message = (top_status or {}).get("message") or "Unknown error"
        raise FortigateError(f"FortiManager Cloud proxy call error for {device_name} {resource}: [{code}] {message}")

    proxy_data = results[0].get("data") if results else None
    proxy_entries = proxy_data if isinstance(proxy_data, list) else ([proxy_data] if proxy_data else [])
    if not proxy_entries:
        raise FortigateError(f"FortiManager Cloud returned no proxy data for {device_name} {resource}")

    entry = proxy_entries[0]
    entry_status = (entry.get("status") or {}).get("code")
    if entry_status not in (0, None):
        entry_message = (entry.get("status") or {}).get("message") or "Unknown error"
        raise FortigateError(f"Device {device_name} rejected proxy call {resource}: [{entry_status}] {entry_message}")

    response = entry.get("response") or {}
    if response.get("status") != "success":
        raise FortigateError(
            f"FortiOS returned an error for {device_name} {resource}: {response.get('status') or 'unknown'}"
        )
    return response.get("results") or {}


def _fetch_device_detail(device_name: str) -> dict:
    """Combines 3 proxy calls (resource usage, interfaces, VPN tunnels)
    into one detail payload for a single device. Any one of the 3 calls
    failing independently (e.g. a device with no VPN tunnels configured
    may error rather than return an empty list on some FortiOS
    versions) does not fail the whole detail fetch — that section is
    simply omitted with its own error note, so a working CPU/Memory
    section is still shown even if the VPN section can't be reached."""
    detail: dict = {"device_name": device_name}

    try:
        usage = _exec_proxy_call(device_name, "/api/v2/monitor/system/performance/status")
        cpu = usage.get("cpu") or {}
        mem = usage.get("mem") or {}
        cpu_pct = 100 - cpu.get("idle", 0) if "idle" in cpu else None
        mem_total = mem.get("total")
        mem_used = mem.get("used")
        detail["cpu_pct"] = cpu_pct
        detail["mem_pct"] = (mem_used / mem_total * 100) if mem_total else None
        detail["mem_used_bytes"] = mem_used
        detail["mem_total_bytes"] = mem_total
        detail["cpu_error"] = None
    except FortigateError as e:
        logger.warning("fortigate detail: CPU/memory fetch failed for %s: %s", device_name, e)
        detail["cpu_pct"] = None
        detail["mem_pct"] = None
        detail["mem_used_bytes"] = None
        detail["mem_total_bytes"] = None
        detail["cpu_error"] = str(e)

    try:
        raw_interfaces = _exec_proxy_call(device_name, "/api/v2/monitor/system/interface")
        interfaces = []
        for if_name, if_data in (raw_interfaces or {}).items():
            if not isinstance(if_data, dict):
                continue
            # Skip interfaces that are down with no real traffic —
            # unused physical ports report link:false with a tiny
            # (observed: 856-byte) constant tx_bytes even when nothing
            # has ever been plugged in (FortiOS's own internal keepalive/
            # accounting overhead, not real traffic), so a plain
            # "tx_bytes truthy" check does not filter them out; a
            # >1 KB threshold does, while still keeping any interface
            # that has ever actually carried meaningful traffic.
            if not if_data.get("link") and (if_data.get("tx_bytes") or 0) < 1024 and (if_data.get("rx_bytes") or 0) < 1024:
                continue
            interfaces.append({
                "name": if_data.get("name") or if_name,
                "ip": if_data.get("ip"),
                "link": bool(if_data.get("link")),
                "speed_mbps": if_data.get("speed"),
                "tx_bytes": if_data.get("tx_bytes"),
                "rx_bytes": if_data.get("rx_bytes"),
                "tx_packets": if_data.get("tx_packets"),
                "rx_packets": if_data.get("rx_packets"),
            })
        interfaces.sort(key=lambda i: (i["name"] or ""))
        detail["interfaces"] = interfaces
        detail["interfaces_error"] = None
    except FortigateError as e:
        logger.warning("fortigate detail: interface fetch failed for %s: %s", device_name, e)
        detail["interfaces"] = []
        detail["interfaces_error"] = str(e)

    try:
        raw_vpn = _exec_proxy_call(device_name, "/api/v2/monitor/vpn/ipsec")
        tunnels = []
        vpn_list = raw_vpn if isinstance(raw_vpn, list) else []
        for t in vpn_list:
            if not isinstance(t, dict):
                continue
            # A tunnel can have multiple phase2 selectors ("proxyid"); a
            # tunnel counts as "up" if ANY of its phase2 selectors are up.
            proxyid = t.get("proxyid") or []
            is_up = any((p or {}).get("status") == "up" for p in proxyid) if proxyid else (t.get("status") == "up")
            incoming = sum((p or {}).get("incoming_bytes") or 0 for p in proxyid) or t.get("incoming_bytes") or 0
            outgoing = sum((p or {}).get("outgoing_bytes") or 0 for p in proxyid) or t.get("outgoing_bytes") or 0
            tunnels.append({
                "name": t.get("name"),
                "remote_gateway": t.get("rgwy"),
                "status": "up" if is_up else "down",
                "incoming_bytes": incoming,
                "outgoing_bytes": outgoing,
            })
        detail["vpn_tunnels"] = tunnels
        detail["vpn_error"] = None
    except FortigateError as e:
        logger.warning("fortigate detail: VPN fetch failed for %s: %s", device_name, e)
        detail["vpn_tunnels"] = []
        detail["vpn_error"] = str(e)

    return detail


def get_device_detail(device_name: str, force_refresh: bool = False) -> dict:
    """Live CPU/Memory usage, interface bandwidth, and VPN tunnel status
    for ONE FortiGate — fetched on demand (see the section docstring
    above for why this is not part of list_devices()/get_overview()).
    "Uptime" is deliberately not included here: FortiOS's monitor API
    has no such field, and the closest available proxy — FortiManager's
    own "first_tunnel_up" timestamp on the device record (see
    list_devices()) — reflects when the management TUNNEL first came
    up, not the FortiGate's own OS uptime, so it is surfaced by the
    frontend as "Connected since" rather than conflated with "Uptime"."""
    if not device_name or not device_name.strip():
        raise FortigateError("device_name is required")
    return _DETAIL_CACHE.get_or_fetch(device_name, lambda: _fetch_device_detail(device_name), force_refresh)


# ── FortiSwitches (managed via FortiLink through a parent FortiGate) ────
# FortiManager does not manage FortiSwitches directly the way it manages
# FortiGates via dvmdb above — each FortiSwitch is attached to, and
# reported through, one specific FortiGate over FortiLink. There is no
# single dvmdb-style "list every switch across the whole instance" call;
# per-switch status has to be proxied through to EACH FortiGate that
# actually has switches attached (see fortigate_monitor.py's
# _exec_proxy_call() docstring for the general proxy mechanism), using
# /api/v2/monitor/switch-controller/managed-switch/status. To avoid
# proxy-calling every one of the ~27 FortiGates "just in case", this
# only calls devices whose dvmdb record reports fsw_cnt > 0 (see
# list_devices() above) — confirmed live to correctly skip devices with
# no switches and return real per-port status for devices that have
# them. Powers the FortiSwitch sub-tab, kept as its own sub-tab
# (alongside SonicWall / Fortigate Firewall) per the user's explicit
# choice, rather than folding it into the Fortigate Firewall device
# detail panel.

_SWITCH_CACHE = TTLCache(ttl_seconds=5 * 60)


def _fetch_switches_for_device(device_name: str) -> list[dict]:
    """One proxy call's worth of FortiSwitches for a single parent
    FortiGate. Returns an empty list (rather than raising) if the proxy
    call itself fails for this one device — a single unreachable
    FortiGate should not prevent every OTHER FortiGate's switches from
    showing up in list_switches() below."""
    try:
        raw_switches = _exec_proxy_call(device_name, "/api/v2/monitor/switch-controller/managed-switch/status")
    except FortigateError as e:
        logger.warning("fortigate switches: fetch failed for parent device %s: %s", device_name, e)
        return []
    raw_list = raw_switches if isinstance(raw_switches, list) else ([raw_switches] if raw_switches else [])
    switches = []
    for sw in raw_list:
        if not isinstance(sw, dict):
            continue
        ports = sw.get("ports") or []
        up_ports = sum(1 for p in ports if (p or {}).get("status") == "up")
        switches.append({
            # Field name varies by FortiOS version — observed both
            # "switch-id" and "name"/"serial" as the identifying field
            # across different switch models/firmware in this fleet.
            "switch_id": sw.get("switch-id") or sw.get("name") or sw.get("serial") or "—",
            "parent_device": device_name,
            "status": sw.get("state") or sw.get("status"),
            "connected": (sw.get("status") or "").lower() == "connected",
            "os_version": sw.get("os_version"),
            "serial": sw.get("serial"),
            "join_time": sw.get("join_time"),
            "port_count": len(ports),
            "port_up_count": up_ports,
            "port_down_count": len(ports) - up_ports,
            # Per-port detail (VLAN, link status, speed/duplex, PoE) —
            # the frontend only renders this when a user expands a
            # switch's row, same "don't clutter the list view" UX as the
            # Fortigate Firewall tab's device detail panel, but unlike
            # that tab this needs NO separate on-demand proxy call: the
            # ports array is already present in this same
            # managed-switch/status response, so it's included directly
            # in list_switches() rather than requiring its own endpoint.
            "ports": [
                {
                    "name": p.get("interface"),
                    "vlan": p.get("vlan"),
                    "status": p.get("status"),
                    "speed_mbps": p.get("speed"),
                    "duplex": p.get("duplex"),
                    "poe_status": p.get("poe_status") if p.get("poe_capable") else None,
                }
                for p in ports if isinstance(p, dict)
            ],
        })
    return switches


def _fetch_switches() -> dict:
    devices_data = list_devices()
    parent_devices = [d["name"] for d in devices_data["devices"] if (d.get("fsw_cnt") or 0) > 0]
    switches: list[dict] = []
    for device_name in parent_devices:
        switches.extend(_fetch_switches_for_device(device_name))
    switches.sort(key=lambda s: (s.get("switch_id") or "").lower())
    connected_count = sum(1 for s in switches if s["connected"])
    return {
        "switches": switches,
        "count": len(switches),
        "connected_count": connected_count,
        "disconnected_count": len(switches) - connected_count,
        "parent_device_count": len(parent_devices),
    }


def list_switches(force_refresh: bool = False) -> dict:
    """Every FortiSwitch reachable via FortiLink through any managed
    FortiGate that reports fsw_cnt > 0 — one proxy call per such parent
    FortiGate, NOT per switch (a single call to a parent FortiGate's
    managed-switch/status endpoint returns ALL of its attached
    switches at once, so this scales with the number of FortiGates that
    HAVE switches, not the number of switches or the FortiGate fleet's
    full size). Powers the FortiSwitch sub-tab, including each switch's
    per-port detail (see _fetch_switches_for_device()'s "ports" field).

    CPU/Memory is deliberately NOT included here or anywhere else in
    this module — confirmed live against the real API that FortiOS has
    no monitor REST endpoint for FortiSwitch health/CPU/memory
    (/managed-switch/health and /managed-switch/detail both return
    "[-6] Invalid url"); that data is only reachable via CLI diagnostic
    commands (e.g. "diagnose switch-controller"), which this module
    does not attempt to shell out to."""
    return _SWITCH_CACHE.get_or_fetch("switches", _fetch_switches, force_refresh)


def get_switch_overview(force_refresh: bool = False) -> dict:
    """Org-wide summary derived from list_switches() — switch counts and
    an overall "connected %" figure, mirroring get_overview()'s shape
    for the Fortigate Firewall sub-tab."""
    data = list_switches(force_refresh)
    total = data["count"]
    connected_pct = (data["connected_count"] / total * 100) if total > 0 else None
    return {
        "switch_count": total,
        "connected_count": data["connected_count"],
        "disconnected_count": data["disconnected_count"],
        "connected_pct": connected_pct,
        "parent_device_count": data["parent_device_count"],
    }
