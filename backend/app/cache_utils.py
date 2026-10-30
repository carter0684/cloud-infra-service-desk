"""
Generic in-process TTL cache with single-flight semantics.

cost_explorer.py, billing_by_service.py, and cmdb.py each independently
grew their own "lock + time.monotonic() + deepcopy + single-flight" cache
because they also needed extra concerns specific to their own multi-minute
cross-account fetches (last-known-good fallback, background threads for
cmdb.py's ~15-minute org scan). This module is NOT a replacement for those
— it's the shared, reusable building block for simpler cached endpoints
that just need "don't re-hit a slow upstream API on every page load,
refresh in the background every N minutes" without any of that extra
machinery. Used by the AWS Health Dashboard (org-wide describe_events_for_
organization) and the Freshservice Change Calendar proxy (fs_calendar.py).

Thread-safe (this backend runs multi-threaded under uvicorn's default
worker); each ECS task has its own independent in-process cache (no
cross-task sharing), which is an acceptable tradeoff here since — unlike
CMDB's DynamoDB-backed cross-task snapshot store — these two endpoints
answer fast enough (seconds, not ~15 minutes) that a per-task cache miss
just costs one real upstream call, not a multi-minute wait.
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Callable, Hashable


class TTLCache:
    """Keyed cache: `key` -> the dict last returned by `fetch_fn` for that
    key, refreshed once every `ttl_seconds`. `fetch_fn` must return a
    dict — a `cache_age_seconds` key is added to the returned copy (0 on a
    fresh fetch, seconds-since-fetch on a cache hit) without mutating the
    cached original."""

    def __init__(self, ttl_seconds: float):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[Hashable, dict] = {}  # key -> {"value": ..., "fetched_at": monotonic()}
        self._inflight: dict[Hashable, threading.Event] = {}

    def get_or_fetch(self, key: Hashable, fetch_fn: Callable[[], dict], force_refresh: bool = False) -> dict:
        with self._lock:
            entry = self._entries.get(key)
            if not force_refresh and entry is not None:
                age = time.monotonic() - entry["fetched_at"]
                if age < self._ttl:
                    result = copy.deepcopy(entry["value"])
                    result["cache_age_seconds"] = int(age)
                    return result

            # Single-flight per key: concurrent requests for the same key
            # (e.g. two tabs both loading the same month) wait for one
            # in-flight fetch instead of issuing duplicate upstream calls.
            inflight = self._inflight.get(key)
            if inflight is not None:
                wait_for_existing = True
            else:
                inflight = threading.Event()
                self._inflight[key] = inflight
                wait_for_existing = False

        if wait_for_existing:
            inflight.wait(timeout=120)
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None:
                    age = time.monotonic() - entry["fetched_at"]
                    result = copy.deepcopy(entry["value"])
                    result["cache_age_seconds"] = int(age)
                    return result
            # The fetch we waited on didn't leave anything usable (e.g. it
            # raised) — fall through and fetch ourselves.

        try:
            value = fetch_fn()
            with self._lock:
                self._entries[key] = {"value": copy.deepcopy(value), "fetched_at": time.monotonic()}
            result = copy.deepcopy(value)
            result["cache_age_seconds"] = 0
            return result
        finally:
            if not wait_for_existing:
                with self._lock:
                    self._inflight.pop(key, None)
                inflight.set()

    def reset_for_tests(self) -> None:
        """Test-only helper: clear all cached entries/single-flight state.
        Not used by production code paths."""
        with self._lock:
            self._entries.clear()
            self._inflight.clear()
