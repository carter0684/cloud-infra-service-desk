"""
"Local Infra Hardware Monitor" — Cisco Meraki Access Point monitoring
dashboard for the Cloud Infra Job Portal.

## What this covers

A single Cisco Meraki organization ("Example Site A Management
Ltd", org id 175206) — every wireless AP (MR-series) across every
network/site in that org — surfaced read-only:
  - Full AP inventory (Site/Name/Tag/Status)
  - Devices Up/Down overview
  - WiFi channel utilization (2.4GHz/5GHz, Wi-Fi vs non-Wi-Fi) by
    network AND by device, both current snapshot and "top change" (a
    two-point delta over the last 2 hours)
  - Wireless client count and average latency by network
  - Packet loss % by network
  - Meraki-native event log (AP join/disconnect/deauth/roam etc.),
    which doubles as this feature's "Logs (source: meraki)" widget —
    everything this module surfaces originates from Meraki, so every
    row already satisfies a literal "source:meraki" filter.

## Credentials — mirrors whitelist_lms.py's direct-override convenience

A single Meraki Dashboard API key (bearer-style, sent as the
X-Cisco-Meraki-API-Key header) — not per-account AssumeRole like every
AWS-facing module in this app, since Meraki is a separate SaaS platform
entirely outside AWS IAM. Local dev can set MERAKI_API_KEY directly to
skip Secrets Manager; production loads Secrets Manager secret
`job-portal/meraki-api-key` (a single plain string, not JSON, since
there's only one credential value — unlike whitelist_lms's access-key/
secret-key pair).

## Rate limiting — Meraki enforces ~10 calls/sec per organization

No existing module in this codebase talks to a non-AWS, non-Freshservice
API with a hard per-second cap, so this module rolls its own retry/
backoff: any 429 (or 5xx) response is retried with exponential backoff
(honouring the `Retry-After` header when Meraki sends one), up to
_MAX_RETRIES times, before giving up and raising MerakiError.

## Pagination

Meraki's list endpoints paginate via an RFC 5988 `Link` response header
(`rel="next"`), not a token in the JSON body — `_get_paginated()` below
walks that header until exhausted or `max_pages` is hit (a safety cap;
this org's largest list — devices — is a few hundred rows, well under
any page cap used here).

## Fan-out cost control

This organization has 91 networks. Two widgets conceptually want
"every network's X" (client count, average latency) — but Meraki has no
single org-wide call for latency (the only such endpoint,
getOrganizationWirelessDevicesLatencyByNetwork, is Early-API-Access/beta
and returns 404 for this org, confirmed live), so per-network latency
requires one call per network. To stay well under the ~10 calls/sec
organization-wide rate limit and keep the dashboard responsive,
per-network latency is fetched for only the top _LATENCY_NETWORK_LIMIT
networks by current client count (a `ThreadPoolExecutor` with a small
worker cap), not all 91 — see get_top_latency_networks()'s docstring.
The Meraki event log (get_meraki_events) fans out the same way when no
specific network is requested.

## Caching

Every "cheap" (single-call, org-wide) function is wrapped in a shared
cache_utils.TTLCache instance (5-10 minute TTL depending on how often
the underlying data realistically changes) — same building block AWS
Health Dashboard / Freshservice Change Calendar already use. The
"expensive" fan-out functions (per-network latency, multi-network
events) use a shorter, dedicated TTL so a page full of widgets loading
together doesn't repeatedly refan-out on every render.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import httpx

from .cache_utils import TTLCache

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")  # region for Secrets Manager (portal's own account)

MERAKI_BASE_URL = "https://api.meraki.com/api/v1"
MERAKI_ORG_ID = os.environ.get("MERAKI_ORG_ID", "")
MERAKI_API_KEY_SECRET_NAME = os.environ.get("MERAKI_API_KEY_SECRET_NAME", "job-portal/meraki-api-key")

# See module docstring's "Fan-out cost control" section.
_LATENCY_NETWORK_LIMIT = 20
_EVENTS_NETWORK_LIMIT = 20
_FANOUT_MAX_WORKERS = 5

_MAX_RETRIES = 4
_INITIAL_BACKOFF_SECONDS = 1.0
_REQUEST_TIMEOUT_SECONDS = 20.0

_MAX_PAGES = 40  # safety cap for pagination — this org's lists are far smaller than 40 pages

_CACHE_SHORT = TTLCache(ttl_seconds=5 * 60)   # snapshot-ish data (channel util, packet loss, devices)
_CACHE_LONG = TTLCache(ttl_seconds=30 * 60)   # rarely-changing data (network list)
_CACHE_FANOUT = TTLCache(ttl_seconds=3 * 60)  # per-network fan-out results (latency, events)


class MerakiError(ValueError):
    """Raised for invalid input or a total inability to reach the Meraki
    Dashboard API. Safe to show the user as-is."""


# ── Credentials ─────────────────────────────────────────────────────────

def _get_secret(secret_name: str) -> str:
    import boto3
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]


def _get_api_key() -> str:
    """Local-dev-env-var-override convenience, same pattern as every
    other cross-service credential in this codebase (see
    whitelist_lms._get_whitelist_credentials())."""
    env_key = os.environ.get("MERAKI_API_KEY")
    if env_key:
        return env_key
    try:
        secret_str = _get_secret(MERAKI_API_KEY_SECRET_NAME)
    except Exception as e:
        raise MerakiError(f"Could not load Meraki API key: {e}") from e
    # The secret may have been stored as a plain string or as JSON
    # ({"api_key": "..."}) — accept either so a Secrets Manager console
    # edit that wraps it in JSON doesn't silently break this module.
    secret_str = (secret_str or "").strip()
    if secret_str.startswith("{"):
        import json
        try:
            parsed = json.loads(secret_str)
            key = parsed.get("api_key") or parsed.get("MERAKI_API_KEY")
            if key:
                return key
        except (ValueError, AttributeError):
            pass
    if not secret_str:
        raise MerakiError("Meraki API key secret is empty.")
    return secret_str


def _headers() -> dict:
    return {"X-Cisco-Meraki-API-Key": _get_api_key(), "Accept": "application/json"}


# ── HTTP + retry/backoff (Meraki: ~10 calls/sec/org) ──────────────────────

def _request_with_retry(method: str, url: str, params: Optional[dict] = None) -> httpx.Response:
    last_exc: Optional[Exception] = None
    backoff = _INITIAL_BACKOFF_SECONDS
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = httpx.request(
                method, url, headers=_headers(), params=params, timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            last_exc = e
            logger.warning("meraki_monitor: request error (%s attempt %d/%d): %s", url, attempt, _MAX_RETRIES, e)
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else backoff
            logger.warning(
                "meraki_monitor: %s from %s (attempt %d/%d) — retrying in %.1fs",
                resp.status_code, url, attempt, _MAX_RETRIES, wait,
            )
            time.sleep(wait)
            backoff *= 2
            last_exc = None
            continue

        return resp

    if last_exc:
        raise MerakiError(f"Could not reach Meraki API ({url}): {last_exc}") from last_exc
    raise MerakiError(f"Meraki API kept returning errors for {url} after {_MAX_RETRIES} attempts.")


def _get_json(path: str, params: Optional[dict] = None) -> tuple[object, dict]:
    """Single GET, returns (parsed_json_body, response_headers)."""
    url = f"{MERAKI_BASE_URL}{path}"
    resp = _request_with_retry("GET", url, params)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise MerakiError(f"Meraki API error for {path}: {e}") from e
    try:
        return resp.json(), resp.headers
    except ValueError as e:
        raise MerakiError(f"Meraki API returned invalid JSON for {path}: {e}") from e


def _parse_next_link(link_header: Optional[str]) -> Optional[str]:
    """Meraki's pagination Link header looks like:
    '<https://.../foo?startingAfter=X>; rel=next, <...>; rel=last'
    Returns the absolute URL for rel="next", or None if there isn't one."""
    if not link_header:
        return None
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part or "rel=next" in part:
            start = part.find("<")
            end = part.find(">")
            if start != -1 and end != -1:
                return part[start + 1:end]
    return None


def _get_paginated(path: str, params: Optional[dict] = None, max_pages: int = _MAX_PAGES) -> list:
    """Follows Meraki's Link-header pagination. `path` is a relative API
    path (e.g. '/organizations/{id}/devices'); the very first request
    uses `params`, every subsequent page uses the exact URL Meraki
    returned in the Link header (which already encodes the cursor), so
    `params` is not re-sent on later pages."""
    url = f"{MERAKI_BASE_URL}{path}"
    all_items: list = []
    current_params = params
    for _ in range(max_pages):
        resp = _request_with_retry("GET", url, current_params)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise MerakiError(f"Meraki API error for {path}: {e}") from e
        try:
            page_items = resp.json()
        except ValueError as e:
            raise MerakiError(f"Meraki API returned invalid JSON for {path}: {e}") from e
        if isinstance(page_items, list):
            all_items.extend(page_items)
        next_url = _parse_next_link(resp.headers.get("Link"))
        if not next_url:
            break
        url = next_url
        current_params = None  # already encoded in next_url
    return all_items


# ── Networks (site names) ─────────────────────────────────────────────────

def _fetch_networks() -> dict:
    items = _get_paginated(f"/organizations/{MERAKI_ORG_ID}/networks", {"perPage": 1000})
    networks = [{"id": n.get("id"), "name": n.get("name")} for n in items]
    return {"networks": networks}


def list_networks(force_refresh: bool = False) -> dict:
    """{id: name} for every network (site) in the org — used to label
    devices/events with a human-readable Site name, and as the Logs
    tab's network filter dropdown. Rarely changes, so cached longer than
    the snapshot-style data below."""
    return _CACHE_LONG.get_or_fetch("networks", _fetch_networks, force_refresh)


def _network_name_map() -> dict[str, str]:
    return {n["id"]: n["name"] for n in list_networks()["networks"] if n.get("id")}


# ── AP inventory (Site / AP Name / Tag / Status) — also the "WiFi Status"
# topic, since both the user's "show all AP" ask and "WiFi Status" topic
# resolve to the same underlying per-device online/offline/alerting/
# dormant state — one table serves both. ────────────────────────────────

def _fetch_access_points() -> dict:
    devices = _get_paginated(
        f"/organizations/{MERAKI_ORG_ID}/devices",
        {"perPage": 1000, "productTypes[]": "wireless"},
    )
    statuses = _get_paginated(
        f"/organizations/{MERAKI_ORG_ID}/devices/statuses",
        {"perPage": 1000, "productTypes[]": "wireless"},
    )
    status_by_serial = {s.get("serial"): s.get("status") for s in statuses}
    net_names = _network_name_map()

    access_points = []
    for d in devices:
        serial = d.get("serial")
        network_id = (d.get("networkId") or "")
        access_points.append({
            "serial": serial,
            "name": d.get("name") or serial,
            "site": net_names.get(network_id, network_id or "—"),
            "network_id": network_id,
            "tags": d.get("tags") or [],
            "model": d.get("model"),
            "status": status_by_serial.get(serial, "unknown"),
            "lan_ip": d.get("lanIp"),
            "firmware": d.get("firmware"),
        })
    access_points.sort(key=lambda a: (a["site"] or "", a["name"] or ""))
    return {"access_points": access_points, "count": len(access_points)}


def list_access_points(force_refresh: bool = False) -> dict:
    return _CACHE_SHORT.get_or_fetch("access_points", _fetch_access_points, force_refresh)


# ── Overview (Devices Up / Down) ──────────────────────────────────────────

def _fetch_devices_overview() -> dict:
    body, _ = _get_json(f"/organizations/{MERAKI_ORG_ID}/devices/statuses/overview")
    by_status = ((body or {}).get("counts") or {}).get("byStatus") or {}
    return {
        "online": int(by_status.get("online", 0)),
        "offline": int(by_status.get("offline", 0)),
        "alerting": int(by_status.get("alerting", 0)),
        "dormant": int(by_status.get("dormant", 0)),
    }


def get_devices_overview(force_refresh: bool = False) -> dict:
    return _CACHE_SHORT.get_or_fetch("devices_overview", _fetch_devices_overview, force_refresh)


# ── AP availability change history — up/down status-change log ──────────
# Powers the "AP Network Monitoring" tab's up/down audit log. Meraki's
# getOrganizationDevicesAvailabilitiesChangeHistory endpoint already IS a
# native "log of every up/down transition", so this is a thin wrapper —
# no derivation/fan-out needed, unlike channel-utilization "change" above
# (which had to compute its own delta since Meraki has no native metric
# for that). Hard org-wide lookback cap from Meraki: 31 days from today;
# this defaults to 30 days to stay comfortably under that cap.

_AVAILABILITY_HISTORY_MAX_TIMESPAN = 31 * 24 * 3600  # Meraki's hard cap for this endpoint
_AVAILABILITY_HISTORY_DEFAULT_TIMESPAN = 30 * 24 * 3600


def _map_availability_history_entry(entry: dict) -> Optional[dict]:
    device = entry.get("device") or {}
    network = entry.get("network") or {}
    details = entry.get("details") or {}
    old_status = next((d.get("value") for d in (details.get("old") or []) if d.get("name") == "status"), None)
    new_status = next((d.get("value") for d in (details.get("new") or []) if d.get("name") == "status"), None)
    if new_status is None:
        return None
    return {
        "occurred_at": entry.get("ts"),
        "serial": device.get("serial"),
        "name": device.get("name") or device.get("serial"),
        "model": device.get("model"),
        "site": network.get("name") or network.get("id") or "—",
        "network_id": network.get("id"),
        "old_status": old_status,
        "new_status": new_status,
        # Binary up/down — same "online = up, everything else = down"
        # convention already used elsewhere on this page (Overview tab's
        # per-site breakdown, Access Points tab's StatusBadge). Meraki
        # itself reports finer-grained statuses (alerting/dormant/
        # offline), but the up/down ask here is deliberately binary.
        "direction": "up" if new_status == "online" else "down",
    }


def _fetch_ap_availability_history_raw(params: dict) -> dict:
    items = _get_paginated(
        f"/organizations/{MERAKI_ORG_ID}/devices/availabilities/changeHistory", params,
    )
    rows = [r for r in (_map_availability_history_entry(e) for e in items) if r is not None]
    rows.sort(key=lambda r: r.get("occurred_at") or "", reverse=True)
    return {"events": rows}


def _fetch_ap_availability_history(timespan: int) -> dict:
    timespan = max(1, min(timespan, _AVAILABILITY_HISTORY_MAX_TIMESPAN))
    result = _fetch_ap_availability_history_raw({"perPage": 1000, "timespan": timespan, "productTypes[]": "wireless"})
    result["timespan"] = timespan
    return result


def get_ap_availability_history(timespan: int = _AVAILABILITY_HISTORY_DEFAULT_TIMESPAN, force_refresh: bool = False) -> dict:
    """Up/Down status-change log for every wireless AP in the org, over
    the requested lookback window (clamped to Meraki's 31-day hard cap
    for this endpoint). Frontend applies Site/Date/Status filters and
    HKT (UTC+8) timestamp formatting client-side from this one fetch."""
    return _CACHE_SHORT.get_or_fetch(
        f"availability_history:{timespan}", lambda: _fetch_ap_availability_history(timespan), force_refresh,
    )


# ── AP availability history — arbitrary user-picked date range, for the
# "AP Network Monitoring" tab's period-select + download feature. Unlike
# get_ap_availability_history() above (a rolling "last N days" window),
# this takes explicit start/end dates so a manager can export e.g. "last
# month" or a specific incident week. Meraki's t0/t1 params accept full
# ISO8601 timestamps; t1 - t0 must be <= 31 days (same hard cap as the
# timespan-based call above), validated here so the user gets a clear
# MerakiError instead of an opaque 400 from Meraki. ─────────────────────

def get_ap_availability_history_range(start_iso: str, end_iso: str, force_refresh: bool = False) -> dict:
    try:
        from datetime import datetime as _dt
        start_dt = _dt.fromisoformat(start_iso.replace("Z", "+00:00"))
        end_dt = _dt.fromisoformat(end_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as e:
        raise MerakiError(f"Invalid date range: {e}") from e
    if end_dt <= start_dt:
        raise MerakiError("End date must be after start date.")
    if (end_dt - start_dt).total_seconds() > _AVAILABILITY_HISTORY_MAX_TIMESPAN:
        raise MerakiError("Date range cannot exceed 31 days (Meraki's limit for this report).")

    cache_key = f"availability_history_range:{start_iso}:{end_iso}"
    return _CACHE_SHORT.get_or_fetch(
        cache_key,
        lambda: _fetch_ap_availability_history_raw({
            "perPage": 1000, "t0": start_iso, "t1": end_iso, "productTypes[]": "wireless",
        }),
        force_refresh,
    )


# ── Channel utilization — by network (current snapshot) ──────────────────
# Powers: "Avg AP Channel Utilization by Network" and "Top AP Channel
# Utilization by Network (802.11)" — the frontend sorts/slices this same
# list by wifi_pct for the latter, so one fetch serves both widgets.

def _band_overall(by_band: list, band_key: str) -> Optional[float]:
    """Average a percentage field across every band entry (2.4/5/6GHz) a
    network/device reported — Meraki returns one row per band, but most
    widgets want one summary number per network/device. Returns None if
    no band reported that field (rather than 0, so the frontend can
    distinguish "no data" from "confirmed zero utilization")."""
    values = [b.get(band_key, {}).get("percentage") for b in (by_band or [])]
    values = [v for v in values if isinstance(v, (int, float))]
    if not values:
        return None
    return sum(values) / len(values)


def _fetch_channel_utilization_by_network(timespan: int) -> dict:
    items = _get_paginated(
        f"/organizations/{MERAKI_ORG_ID}/wireless/devices/channelUtilization/byNetwork",
        {"perPage": 1000, "timespan": timespan},
    )
    net_names = _network_name_map()
    rows = []
    for item in items:
        network = item.get("network") or {}
        network_id = network.get("id")
        by_band = item.get("byBand") or []
        rows.append({
            "network_id": network_id,
            "network_name": network.get("name") or net_names.get(network_id, network_id),
            "wifi_pct": _band_overall(by_band, "wifi"),
            "non_wifi_pct": _band_overall(by_band, "nonWifi"),
            "total_pct": _band_overall(by_band, "total"),
            "by_band": by_band,
        })
    return {"networks": rows, "timespan": timespan}


def get_channel_utilization_by_network(timespan: int = 3600, force_refresh: bool = False) -> dict:
    return _CACHE_SHORT.get_or_fetch(
        f"cu_by_network:{timespan}", lambda: _fetch_channel_utilization_by_network(timespan), force_refresh,
    )


# ── Channel utilization — by device (current snapshot) ───────────────────
# Powers: "Avg AP Channel Utilization by Device", "Top AP Channel
# Utilization by Device (Non-802.11)", "Top 10 APs — Wi-Fi Channel Util %",
# "Top 10 APs — Non-Wi-Fi Channel Util %", and the top-level "AP WiFi
# Channel Utilization (Top 10 APs)" topic — all derived from this one
# fetch by sorting/slicing differently on the frontend.

def _fetch_channel_utilization_by_device(timespan: int) -> dict:
    items = _get_paginated(
        f"/organizations/{MERAKI_ORG_ID}/wireless/devices/channelUtilization/byDevice",
        {"perPage": 1000, "timespan": timespan},
    )
    ap_lookup = {ap["serial"]: ap for ap in list_access_points()["access_points"]}
    net_names = _network_name_map()

    rows = []
    for item in items:
        serial = item.get("serial")
        ap = ap_lookup.get(serial)
        network_id = (item.get("network") or {}).get("id")
        by_band = item.get("byBand") or []
        rows.append({
            "serial": serial,
            "name": ap["name"] if ap else serial,
            "site": ap["site"] if ap else net_names.get(network_id, network_id or "—"),
            "network_id": network_id,
            "wifi_pct": _band_overall(by_band, "wifi"),
            "non_wifi_pct": _band_overall(by_band, "nonWifi"),
            "total_pct": _band_overall(by_band, "total"),
            "by_band": by_band,
        })
    return {"devices": rows, "timespan": timespan}


def get_channel_utilization_by_device(timespan: int = 3600, force_refresh: bool = False) -> dict:
    return _CACHE_SHORT.get_or_fetch(
        f"cu_by_device:{timespan}", lambda: _fetch_channel_utilization_by_device(timespan), force_refresh,
    )


# ── Channel utilization — "Top Change" (two-point delta) ─────────────────
# Meraki has no native "delta"/"change" metric — this computes it by
# requesting a 2-hour window at 1-hour resolution (exactly 2 buckets: the
# previous hour and the current hour) and diffing total_pct between them,
# per network/device. Sorted by absolute change (biggest swing either
# direction is "interesting", not just increases).

def _group_history_rows_by_key(rows: list, key_fn) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for row in rows:
        key = key_fn(row)
        if key is None:
            continue
        grouped.setdefault(key, []).append(row)
    for key in grouped:
        grouped[key].sort(key=lambda r: r.get("startTs") or "")
    return grouped


def _fetch_channel_utilization_change_by_network(timespan: int, interval: int) -> dict:
    items = _get_paginated(
        f"/organizations/{MERAKI_ORG_ID}/wireless/devices/channelUtilization/history/byNetwork/byInterval",
        {"perPage": 1000, "timespan": timespan, "interval": interval},
    )
    net_names = _network_name_map()
    grouped = _group_history_rows_by_key(items, lambda r: (r.get("network") or {}).get("id"))

    results = []
    for network_id, points in grouped.items():
        if len(points) < 2:
            continue
        first_total = _band_overall(points[0].get("byBand") or [], "total")
        last_total = _band_overall(points[-1].get("byBand") or [], "total")
        if first_total is None or last_total is None:
            continue
        results.append({
            "network_id": network_id,
            "network_name": net_names.get(network_id, network_id),
            "from_pct": first_total,
            "to_pct": last_total,
            "change_pct": last_total - first_total,
        })
    results.sort(key=lambda r: abs(r["change_pct"]), reverse=True)
    return {"networks": results, "timespan": timespan, "interval": interval}


def get_channel_utilization_change_by_network(timespan: int = 7200, interval: int = 3600, force_refresh: bool = False) -> dict:
    return _CACHE_SHORT.get_or_fetch(
        f"cu_change_network:{timespan}:{interval}",
        lambda: _fetch_channel_utilization_change_by_network(timespan, interval), force_refresh,
    )


def _fetch_channel_utilization_change_by_device(timespan: int, interval: int) -> dict:
    items = _get_paginated(
        f"/organizations/{MERAKI_ORG_ID}/wireless/devices/channelUtilization/history/byDevice/byInterval",
        {"perPage": 1000, "timespan": timespan, "interval": interval},
    )
    ap_lookup = {ap["serial"]: ap for ap in list_access_points()["access_points"]}
    grouped = _group_history_rows_by_key(items, lambda r: r.get("serial"))

    results = []
    for serial, points in grouped.items():
        if len(points) < 2:
            continue
        first_total = _band_overall(points[0].get("byBand") or [], "total")
        last_total = _band_overall(points[-1].get("byBand") or [], "total")
        if first_total is None or last_total is None:
            continue
        ap = ap_lookup.get(serial)
        results.append({
            "serial": serial,
            "name": ap["name"] if ap else serial,
            "site": ap["site"] if ap else "—",
            "from_pct": first_total,
            "to_pct": last_total,
            "change_pct": last_total - first_total,
        })
    results.sort(key=lambda r: abs(r["change_pct"]), reverse=True)
    return {"devices": results, "timespan": timespan, "interval": interval}


def get_channel_utilization_change_by_device(timespan: int = 7200, interval: int = 3600, force_refresh: bool = False) -> dict:
    return _CACHE_SHORT.get_or_fetch(
        f"cu_change_device:{timespan}:{interval}",
        lambda: _fetch_channel_utilization_change_by_device(timespan, interval), force_refresh,
    )


# ── Packet loss by network — "Top 10 Networks — Wireless Loss %" ─────────

def _fetch_packet_loss_by_network(timespan: int) -> dict:
    items = _get_paginated(
        f"/organizations/{MERAKI_ORG_ID}/wireless/devices/packetLoss/byNetwork",
        {"perPage": 1000, "timespan": timespan},
    )
    net_names = _network_name_map()
    rows = []
    for item in items:
        network = item.get("network") or {}
        network_id = network.get("id")
        downstream = item.get("downstream") or {}
        upstream = item.get("upstream") or {}
        total_pkts = (downstream.get("total") or 0) + (upstream.get("total") or 0)
        lost_pkts = (downstream.get("lost") or 0) + (upstream.get("lost") or 0)
        loss_pct = (lost_pkts / total_pkts * 100) if total_pkts > 0 else 0.0
        rows.append({
            "network_id": network_id,
            "network_name": network.get("name") or net_names.get(network_id, network_id),
            "loss_pct": loss_pct,
            "downstream_loss_pct": downstream.get("lossPercentage"),
            "upstream_loss_pct": upstream.get("lossPercentage"),
        })
    rows.sort(key=lambda r: r["loss_pct"], reverse=True)
    return {"networks": rows, "timespan": timespan}


def get_packet_loss_by_network(timespan: int = 3600, force_refresh: bool = False) -> dict:
    return _CACHE_SHORT.get_or_fetch(
        f"packet_loss:{timespan}", lambda: _fetch_packet_loss_by_network(timespan), force_refresh,
    )


# ── Top networks by client count — "Wireless Client Count by Network" ────

def _fetch_top_networks_by_clients(per_page: int) -> dict:
    items = _get_paginated(
        f"/organizations/{MERAKI_ORG_ID}/summary/top/networks/byStatus",
        {"perPage": per_page},
    )
    rows = []
    for item in items:
        clients = ((item.get("clients") or {}).get("counts") or {}).get("total", 0)
        rows.append({
            "network_id": item.get("networkId"),
            "network_name": item.get("name"),
            "client_count": clients,
            "status": (item.get("statuses") or {}).get("overall"),
        })
    rows.sort(key=lambda r: r["client_count"], reverse=True)
    return {"networks": rows}


def get_top_networks_by_clients(per_page: int = 1000, force_refresh: bool = False) -> dict:
    return _CACHE_SHORT.get_or_fetch(
        f"top_networks_clients:{per_page}", lambda: _fetch_top_networks_by_clients(per_page), force_refresh,
    )


# ── Top devices by client count — "Top 10 APs — Client Count" ────────────

def _fetch_top_devices_by_clients(per_page: int) -> dict:
    items = _get_paginated(
        f"/organizations/{MERAKI_ORG_ID}/summary/top/devices/byUsage",
        {"perPage": per_page},
    )
    rows = []
    for item in items:
        network = item.get("network") or {}
        clients = ((item.get("clients") or {}).get("counts") or {}).get("total", 0)
        rows.append({
            "serial": item.get("serial"),
            "name": item.get("name"),
            "site": network.get("name"),
            "network_id": network.get("id"),
            "client_count": clients,
        })
    rows.sort(key=lambda r: r["client_count"], reverse=True)
    return {"devices": rows}


def get_top_devices_by_clients(per_page: int = 500, force_refresh: bool = False) -> dict:
    return _CACHE_SHORT.get_or_fetch(
        f"top_devices_clients:{per_page}", lambda: _fetch_top_devices_by_clients(per_page), force_refresh,
    )


# ── Wireless latency per network — fan-out over the busiest networks only
# (see module docstring's "Fan-out cost control" section). Powers both
# "Wireless Average Latency per Network" and "Top 10 Networks — Avg
# Latency (ms)" (frontend just sorts/slices this same list). ────────────

def _fetch_one_network_latency(network_id: str, timespan: int) -> Optional[float]:
    try:
        body, _ = _get_json(f"/networks/{network_id}/wireless/latencyStats", {"timespan": timespan})
    except MerakiError as e:
        logger.warning("meraki_monitor: latencyStats failed for network %s: %s", network_id, e)
        return None
    if not isinstance(body, dict):
        return None
    # Average across every traffic class Meraki reports (background/
    # best-effort/video/voice) that actually has data — a single overall
    # "average latency for this network" isn't a field Meraki itself
    # returns, so this is a derived approximation, documented here and
    # surfaced to the frontend as "avg across traffic classes".
    values = []
    for traffic_class in ("backgroundTraffic", "bestEffortTraffic", "videoTraffic", "voiceTraffic"):
        avg = (body.get(traffic_class) or {}).get("avg")
        if isinstance(avg, (int, float)):
            values.append(avg)
    if not values:
        return None
    return sum(values) / len(values)


def _fetch_top_latency_networks(timespan: int) -> dict:
    top_networks = get_top_networks_by_clients()["networks"][:_LATENCY_NETWORK_LIMIT]
    rows_by_id = {n["network_id"]: dict(n) for n in top_networks if n.get("network_id")}

    with ThreadPoolExecutor(max_workers=_FANOUT_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_one_network_latency, network_id, timespan): network_id
            for network_id in rows_by_id
        }
        for fut in as_completed(futures):
            network_id = futures[fut]
            rows_by_id[network_id]["avg_latency_ms"] = fut.result()

    rows = [r for r in rows_by_id.values() if r.get("avg_latency_ms") is not None]
    rows.sort(key=lambda r: r["avg_latency_ms"], reverse=True)
    return {
        "networks": rows,
        "timespan": timespan,
        "note": f"Limited to the top {_LATENCY_NETWORK_LIMIT} networks by client count "
                "to stay within Meraki's per-organization API rate limit.",
    }


def get_top_latency_networks(timespan: int = 3600, force_refresh: bool = False) -> dict:
    """See module docstring's 'Fan-out cost control' — only the busiest
    _LATENCY_NETWORK_LIMIT networks (by current client count) are probed
    for latency, not all 91 networks in this org."""
    return _CACHE_FANOUT.get_or_fetch(
        f"top_latency:{timespan}", lambda: _fetch_top_latency_networks(timespan), force_refresh,
    )


# ── Meraki event log — "Meraki Error Logs" AND "Logs (source: meraki)" ───
# Same underlying data serves both widgets: every row already comes from
# Meraki (there is no other source in this feature), so a literal
# "source:meraki" filter is trivially satisfied by showing this table.

_EVENT_ROW_FIELDS = ("occurred_at", "network_id", "network_name", "ap_name", "category", "ssid", "description")


def _map_event_row(event: dict, network_id: str, network_name: str) -> dict:
    return {
        "occurred_at": event.get("occurredAt"),
        "network_id": network_id,
        "network_name": network_name,
        "ap_name": event.get("deviceName"),
        "category": event.get("category") or event.get("type"),
        "ssid": event.get("ssidName"),
        "device_name": event.get("deviceName"),
        "description": event.get("description"),
        "client_mac": event.get("clientMac"),
    }


def _fetch_events_for_network(network_id: str, network_name: str, per_page: int) -> list[dict]:
    try:
        body, _ = _get_json(
            f"/networks/{network_id}/events",
            {"productType": "wireless", "perPage": per_page},
        )
    except MerakiError as e:
        logger.warning("meraki_monitor: events fetch failed for network %s: %s", network_id, e)
        return []
    events = (body or {}).get("events") or []
    return [_map_event_row(e, network_id, network_name) for e in events]


def _fetch_meraki_events(network_id: Optional[str], per_page: int) -> dict:
    net_names = _network_name_map()

    if network_id:
        rows = _fetch_events_for_network(network_id, net_names.get(network_id, network_id), per_page)
        rows.sort(key=lambda r: r.get("occurred_at") or "", reverse=True)
        return {"events": rows[:per_page], "network_id": network_id}

    # No specific network requested — fan out over the busiest networks
    # only (same rationale as latency), then merge + sort + cap.
    top_networks = get_top_networks_by_clients()["networks"][:_EVENTS_NETWORK_LIMIT]
    per_network_page = max(10, per_page // max(1, len(top_networks)))

    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=_FANOUT_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_events_for_network, n["network_id"], n["network_name"], per_network_page): n
            for n in top_networks if n.get("network_id")
        }
        for fut in as_completed(futures):
            all_rows.extend(fut.result())

    all_rows.sort(key=lambda r: r.get("occurred_at") or "", reverse=True)
    return {
        "events": all_rows[:per_page],
        "network_id": None,
        "note": f"Limited to the top {_EVENTS_NETWORK_LIMIT} networks by client count "
                "to stay within Meraki's per-organization API rate limit.",
    }


def get_meraki_events(network_id: Optional[str] = None, per_page: int = 100, force_refresh: bool = False) -> dict:
    cache_key = f"events:{network_id or 'all'}:{per_page}"
    return _CACHE_FANOUT.get_or_fetch(cache_key, lambda: _fetch_meraki_events(network_id, per_page), force_refresh)
