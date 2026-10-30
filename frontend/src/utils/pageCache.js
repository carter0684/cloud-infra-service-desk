/**
 * Tiny in-memory, module-scoped cache for page-level data.
 *
 * Why this exists: the backend already caches the AWS Health Dashboard,
 * Cost Analysis, Billing by Service, and Calendars endpoints for 5
 * minutes (see backend/app/cache_utils.py) — but that alone doesn't make
 * repeat visits to those pages feel instant, because React Router
 * unmounts each page's component when the user navigates away, which
 * wipes its `useState` data back to null. Coming back to the page then
 * re-renders a full loading spinner and re-fires the API call, even
 * though the server would have answered from its own cache in
 * milliseconds — the round trip + render-from-empty is still visible to
 * the user.
 *
 * This module lives outside any component's lifecycle (a plain JS module
 * — imported once, its Map persists for as long as the browser tab/SPA
 * session does), so a page can seed its initial state directly from here
 * on mount and render immediately, then silently re-fetch in the
 * background to refresh both this cache and the on-screen data — the
 * "stale-while-revalidate" pattern. No TTL is enforced here on purpose:
 * the server-side cache is already the source of truth for freshness;
 * this layer only avoids re-showing a blank/loading page for data the
 * user already saw a few seconds/minutes ago in this same tab. A full
 * page reload (F5) naturally clears it, which is fine — that's already
 * a "give me a truly fresh load" gesture.
 */

const _store = new Map()

export function getPageCache(key) {
  return _store.has(key) ? _store.get(key) : undefined
}

export function setPageCache(key, value) {
  _store.set(key, value)
}

export function clearPageCache(key) {
  if (key === undefined) {
    _store.clear()
  } else {
    _store.delete(key)
  }
}
