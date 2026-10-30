"""
Website Monitoring (sidebar: Monitoring > Website Monitoring).

Shows UP/DOWN, HTTP status code, response time and the latest successful
screenshot of each monitored website, as seen from several AWS Regions
(US, London, Singapore, Mumbai, Tokyo).

## Where the data comes from

The probing is NOT done by this portal. One CloudWatch Synthetics canary per
Region (CloudFormation stacks in the `website_monitor` project, deployed
separately) visits every site on a schedule and writes its findings to one
private "central" S3 bucket:

    config/sites.json                         the monitored site list + regions
    results/<site_id>/<region_key>.json       latest result of one site from one Region
    screenshots/<site_id>/<region_key>.jpg    latest SUCCESSFUL screenshot

This module only reads those objects (and edits config/sites.json), so it
makes no calls to the monitored websites itself.

## Who may do what

* Reading (status, screenshots): every logged-in user - main.py hands the
  "website_monitoring" tab to everyone (settings_store.EVERYONE_TAB_IDS) and
  the status route only needs get_actor.
* Adding / removing a website: admins only (main.py's require_admin).
  Because config/sites.json is what the canaries read on every run, a change
  takes effect on the next canary run (at most one check interval later).

## Design notes

* Screenshots are returned as short-lived presigned S3 URLs (the bucket is
  private) so the browser can load <img> tags without carrying auth headers.
* The status payload is cached for 30 seconds per ECS task (35 small S3 GETs
  per refresh otherwise).
* The site list is edited with a read-modify-write of one small JSON object.
  Edits are admin-only and rare, so a per-process lock is used and the last
  writer wins if two admins save in the same second on different tasks.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .cache_utils import TTLCache

logger = logging.getLogger(__name__)

CONFIG_KEY = "config/sites.json"

# Matches the canary stack's defaults; only used to bootstrap the config object
# if the monitor stack has not seeded it yet.
DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_REGIONS = [
    {"key": "us", "label": "US (N. Virginia)"},
    {"key": "uk", "label": "UK (London)"},
    {"key": "sg", "label": "Singapore"},
    {"key": "in", "label": "Mumbai"},
    {"key": "jp", "label": "Tokyo"},
]

# One canary run has a 240 s timeout and stops starting new sites after ~200 s
# (~5 s per site), so cap the list well inside that.
MAX_SITES = 30
MAX_NAME_LEN = 80
MAX_URL_LEN = 500

# A probe that has not reported for this many check intervals is "stale".
STALE_AFTER_INTERVALS = 2.5
# An UP site answering slower than this is flagged "slow" (still UP, no alert).
SLOW_RESPONSE_MS = 15_000
SCREENSHOT_URL_TTL_SECONDS = 900
_STATUS_CACHE_TTL_SECONDS = 30

_SITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_REGION_KEY_RE = re.compile(r"^[a-z]{2,6}$")

_cache = TTLCache(ttl_seconds=_STATUS_CACHE_TTL_SECONDS)
_cache_generation = 0  # part of the cache key; bumped on every site-list change
_write_lock = threading.Lock()
_s3_client = None
_s3_client_lock = threading.Lock()


# ── Errors ────────────────────────────────────────────────────────────────

class WebsiteMonitorNotConfigured(Exception):
    """WEBSITE_MONITOR_BUCKET is not set for this deployment."""


class WebsiteMonitorStorageError(Exception):
    """The central bucket could not be read or written."""


class SiteValidationError(ValueError):
    """The submitted name/URL is not acceptable (HTTP 400)."""


class SiteConflictError(Exception):
    """The site (URL) is already monitored (HTTP 409)."""


class SiteNotFoundError(KeyError):
    """No such monitored site (HTTP 404)."""


# ── S3 plumbing ───────────────────────────────────────────────────────────

def _bucket() -> str:
    name = os.environ.get("WEBSITE_MONITOR_BUCKET", "").strip()
    if not name:
        raise WebsiteMonitorNotConfigured("Website Monitoring is not configured for this environment")
    return name


def _s3():
    """Lazily-built S3 client in the central bucket's Region. Presigned URLs
    must be signed for that Region with SigV4, hence the explicit config."""
    global _s3_client
    with _s3_client_lock:
        if _s3_client is None:
            region = os.environ.get("WEBSITE_MONITOR_REGION", "ap-southeast-1")
            # The regional endpoint + virtual-hosted style is required for the
            # presigned screenshot URLs: by default boto3 signs them for the
            # global "<bucket>.s3.amazonaws.com" host, which S3 answers with a
            # 307 redirect to the regional host, and the signature then fails
            # (403) - the browser shows a broken image.
            _s3_client = boto3.client(
                "s3",
                region_name=region,
                endpoint_url=f"https://s3.{region}.amazonaws.com",
                config=Config(
                    s3={"addressing_style": "virtual"},
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=10,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        return _s3_client


def reset_for_tests() -> None:
    """Test-only: drop the cached client and cached status payload."""
    global _s3_client
    with _s3_client_lock:
        _s3_client = None
    _cache.reset_for_tests()


def _get_json(key: str) -> Optional[dict]:
    """Parsed JSON object at `key`, or None if it does not exist."""
    try:
        resp = _s3().get_object(Bucket=_bucket(), Key=key)
        return json.loads(resp["Body"].read())
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        logger.error("website_monitor: S3 read failed for %s: %s", key, e)
        raise WebsiteMonitorStorageError("Could not read monitoring data") from e
    except (ValueError, TypeError) as e:
        logger.error("website_monitor: invalid JSON in %s: %s", key, e)
        return None


# ── Site list (config/sites.json) ─────────────────────────────────────────

def _default_config() -> dict:
    return {
        "intervalMinutes": DEFAULT_INTERVAL_MINUTES,
        "regions": [dict(r) for r in DEFAULT_REGIONS],
        "sites": [],
    }


def _load_config() -> dict:
    cfg = _get_json(CONFIG_KEY)
    if not isinstance(cfg, dict):
        return _default_config()
    out = _default_config()
    interval = cfg.get("intervalMinutes")
    if isinstance(interval, int) and 1 <= interval <= 60:
        out["intervalMinutes"] = interval
    regions = [
        {"key": r["key"], "label": str(r.get("label") or r["key"])}
        for r in (cfg.get("regions") or [])
        if isinstance(r, dict) and isinstance(r.get("key"), str) and _REGION_KEY_RE.match(r["key"])
    ]
    if regions:
        out["regions"] = regions
    out["sites"] = [
        s for s in (cfg.get("sites") or [])
        if isinstance(s, dict) and _SITE_ID_RE.match(str(s.get("id", ""))) and s.get("url")
    ]
    # Preserve who/when last changed the list, if recorded.
    out["updatedBy"] = cfg.get("updatedBy")
    out["updatedAt"] = cfg.get("updatedAt")
    return out


def _save_config(cfg: dict, actor: str) -> None:
    cfg = dict(cfg)
    cfg["updatedBy"] = actor
    cfg["updatedAt"] = _now_iso()
    try:
        _s3().put_object(
            Bucket=_bucket(),
            Key=CONFIG_KEY,
            Body=json.dumps(cfg, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
            CacheControl="no-cache",
        )
    except ClientError as e:
        logger.error("website_monitor: S3 write failed for %s: %s", CONFIG_KEY, e)
        raise WebsiteMonitorStorageError("Could not save the website list") from e
    global _cache_generation
    _cache_generation += 1  # next status call must show the change immediately


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Validation ────────────────────────────────────────────────────────────

_BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")


def normalize_url(raw: str) -> str:
    """Validate and canonicalise a URL to monitor. Raises SiteValidationError.

    Only public http(s) websites are accepted. The probes run from AWS
    public infrastructure so private addresses could never be reached
    anyway; rejecting them keeps the list meaningful and avoids using the
    canaries to poke at link-local/metadata style addresses."""
    raw = (raw or "").strip()
    if not raw or len(raw) > MAX_URL_LEN:
        raise SiteValidationError(f"URL is required and must be at most {MAX_URL_LEN} characters")
    if re.search(r"[\s\x00-\x1f\x7f]", raw):
        raise SiteValidationError("URL must not contain spaces or control characters")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port  # raises ValueError on a bad port
    except ValueError:
        raise SiteValidationError("URL is not valid")
    if parts.scheme not in ("http", "https"):
        raise SiteValidationError("Only http:// and https:// URLs can be monitored")
    if parts.username or parts.password:
        raise SiteValidationError("URLs containing a username or password are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise SiteValidationError("URL has no host name")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            raise SiteValidationError("Private, loopback and link-local addresses cannot be monitored")
    else:
        if "." not in host or host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
            raise SiteValidationError("Host must be a public domain name (for example www.example.com)")
        if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", host):
            raise SiteValidationError("Host name contains invalid characters")
        # "127.1"-style shorthand is read as an IP address by browsers; a real
        # public domain always has a top-level label containing a letter.
        if not re.search(r"[a-z]", host.rsplit(".", 1)[-1]):
            raise SiteValidationError("Host must be a public domain name (for example www.example.com)")

    netloc = host if ip is None or ip.version == 4 else f"[{host}]"
    if port and not ((parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)):
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def _clean_name(raw: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", (raw or "")).strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        raise SiteValidationError("Name is required")
    if len(name) > MAX_NAME_LEN:
        raise SiteValidationError(f"Name must be at most {MAX_NAME_LEN} characters")
    return name


def _make_site_id(url: str, existing_ids: set[str]) -> str:
    host = (urlsplit(url).hostname or "site").lower()
    host = re.sub(r"^www\.", "", host)
    base = re.sub(r"[^a-z0-9]+", "-", host).strip("-")[:34] or "site"
    candidate, n = base, 2
    while candidate in existing_ids:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


# ── Public API ────────────────────────────────────────────────────────────

def add_site(name: str, url: str, actor: str) -> dict:
    """Start monitoring a website. Returns the stored site entry."""
    name = _clean_name(name)
    url = normalize_url(url)
    with _write_lock:
        cfg = _load_config()
        sites = cfg["sites"]
        if len(sites) >= MAX_SITES:
            raise SiteValidationError(f"At most {MAX_SITES} websites can be monitored")
        if any(normalize_url(s["url"]) == url for s in sites):
            raise SiteConflictError("This website is already being monitored")
        site = {
            "id": _make_site_id(url, {s["id"] for s in sites}),
            "name": name,
            "url": url,
            "addedBy": actor,
            "addedAt": _now_iso(),
        }
        sites.append(site)
        _save_config(cfg, actor)
    logger.info("website_monitor: %s added %s (%s)", actor, site["id"], url)
    return site


def remove_site(site_id: str, actor: str) -> dict:
    """Stop monitoring a website and delete its stored results/screenshots."""
    if not _SITE_ID_RE.match(site_id or ""):
        raise SiteNotFoundError(site_id)
    with _write_lock:
        cfg = _load_config()
        match = next((s for s in cfg["sites"] if s["id"] == site_id), None)
        if match is None:
            raise SiteNotFoundError(site_id)
        cfg["sites"] = [s for s in cfg["sites"] if s["id"] != site_id]
        _save_config(cfg, actor)
    logger.info("website_monitor: %s removed %s (%s)", actor, site_id, match.get("url"))
    _delete_site_objects(site_id)
    return match


def _delete_site_objects(site_id: str) -> None:
    """Best effort: the site is already gone from the list, so a failure here
    only leaves harmless orphans (never shown, overwritten if the site is re-added)."""
    try:
        for prefix in (f"results/{site_id}/", f"screenshots/{site_id}/"):
            resp = _s3().list_objects_v2(Bucket=_bucket(), Prefix=prefix)
            keys = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
            if keys:
                _s3().delete_objects(Bucket=_bucket(), Delete={"Objects": keys, "Quiet": True})
    except Exception:  # noqa: BLE001 - cleanup must never fail the removal
        logger.warning("website_monitor: cleanup of objects for %s failed", site_id, exc_info=True)


def _parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _shape_result(site: dict, region_key: str, raw: Optional[dict], interval_minutes: int, now: datetime) -> dict:
    """One cell of the status grid, with a server-computed `state`:
    up | down | stale (probe silent too long) | nodata (never reported)."""
    if not isinstance(raw, dict):
        return {"state": "nodata"}
    checked = _parse_iso(raw.get("checkedAt"))
    if checked is None:
        return {"state": "nodata"}
    age = max(0, int((now - checked).total_seconds()))
    if age > interval_minutes * 60 * STALE_AFTER_INTERVALS:
        state = "stale"
    else:
        state = "up" if raw.get("up") else "down"

    screenshot_url = None
    screenshot_at = raw.get("screenshotAt")
    if isinstance(screenshot_at, str):
        try:
            screenshot_url = _s3().generate_presigned_url(
                "get_object",
                Params={"Bucket": _bucket(), "Key": f"screenshots/{site['id']}/{region_key}.jpg"},
                ExpiresIn=SCREENSHOT_URL_TTL_SECONDS,
            )
        except Exception:  # noqa: BLE001
            logger.warning("website_monitor: could not presign screenshot for %s/%s", site["id"], region_key)

    def _num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    error = raw.get("error")
    return {
        "state": state,
        "up": bool(raw.get("up")),
        "status_code": _num(raw.get("statusCode")),
        "response_time_ms": _num(raw.get("responseTimeMs")),
        # Slow = up but slow to answer; never counted as down.
        "slow": state == "up" and (
            bool(raw.get("slow"))
            or (_num(raw.get("responseTimeMs")) or 0) >= SLOW_RESPONSE_MS
        ),
        "checked_at": raw.get("checkedAt"),
        "age_seconds": age,
        "error": str(error)[:300] if error else None,
        "consecutive_failures": int(_num(raw.get("consecutiveFailures")) or 0),
        "last_success_at": raw.get("lastSuccessAt"),
        "screenshot_url": screenshot_url,
        "screenshot_at": screenshot_at if screenshot_url else None,
    }


def _fetch_status() -> dict:
    cfg = _load_config()
    regions = cfg["regions"]
    sites = cfg["sites"]
    now = datetime.now(timezone.utc)

    pairs = [(s, r["key"]) for s in sites for r in regions]
    raw_results: dict[tuple[str, str], Optional[dict]] = {}
    if pairs:
        def _read(pair):
            site, region_key = pair
            return pair, _get_json(f"results/{site['id']}/{region_key}.json")

        with ThreadPoolExecutor(max_workers=min(16, len(pairs))) as pool:
            for (site, region_key), raw in pool.map(_read, pairs):
                raw_results[(site["id"], region_key)] = raw

    out_sites = []
    for s in sites:
        out_sites.append({
            "id": s["id"],
            "name": s.get("name") or s["id"],
            "url": s["url"],
            "added_by": s.get("addedBy"),
            "added_at": s.get("addedAt"),
            "results": {
                r["key"]: _shape_result(s, r["key"], raw_results.get((s["id"], r["key"])), cfg["intervalMinutes"], now)
                for r in regions
            },
        })
    return {
        "interval_minutes": cfg["intervalMinutes"],
        "regions": regions,
        "sites": out_sites,
        "max_sites": MAX_SITES,
        "list_updated_by": cfg.get("updatedBy"),
        "list_updated_at": cfg.get("updatedAt"),
        "generated_at": _now_iso(),
    }


def get_status(force_refresh: bool = False) -> dict:
    """Grid of every monitored site x probe Region (cached for 30 s)."""
    _bucket()  # fail fast, uncached, when the feature is not configured
    return _cache.get_or_fetch(("status", _cache_generation), _fetch_status, force_refresh=force_refresh)
