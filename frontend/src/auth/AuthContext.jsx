import React, { createContext, useContext, useState, useEffect, useRef } from 'react'
import { api, registerSessionExpiredHandler } from '../api.js'

const AuthContext = createContext(null)

// Absolute session lifetime this app enforces client-side, matching the
// ALB's own session_timeout=Duration.hours(8) (compute_stack.py) — the
// ALB is the real security boundary (every request is still verified
// server-side by get_actor()'s ALB-JWT check regardless of anything
// here); this timer exists purely so the user gets sent back to the
// login screen the moment their session's 8 hours are up, rather than
// only finding out the next time they happen to click something and get
// a confusing failed-request error.
// Exported (not just module-scoped) so App.jsx's SessionCountdown
// component — rendered next to the HKClock in the topbar — can compute
// "time remaining" from the same localStorage key and deadline this
// file's own 8-hour timer (below) uses to trigger the actual logout,
// without duplicating the 8-hour constant or session_started_at key
// name in a second place that could drift out of sync.
export const SESSION_MAX_AGE_MS = 8 * 60 * 60 * 1000
export const SESSION_STARTED_AT_KEY = 'job_portal_session_started_at'
// How often to re-check the 8-hour deadline while the tab is open.
// A 1-minute cadence means the auto-logout fires within at most 1
// minute of the true 8-hour mark — tight enough that no one notices
// the difference, without running a check on every render/keystroke.
const SESSION_CHECK_INTERVAL_MS = 60 * 1000

// 'local'  — mock dropdown login (X-Actor-Email header), used by
//            `npm run dev` against the local FastAPI/SQLite backend.
// 'oidc'   — production build (Task 12): identity comes from the ALB's
//            Azure AD session cookie, verified server-side by
//            auth_alb.get_actor() on every request. There is no
//            client-side login call — the browser is redirected to the
//            backend's /login-landing path, which is itself gated behind
//            get_actor(), so *reaching* it at all means Azure AD + the
//            ALB have already authenticated the user.
const AUTH_MODE = import.meta.env.VITE_AUTH_MODE === 'oidc' ? 'oidc' : 'local'
const API_BASE = import.meta.env.VITE_API_BASE_URL || ''

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => {
    if (AUTH_MODE === 'oidc') return null // resolved async below, never from localStorage
    try {
      const saved = localStorage.getItem('job_portal_user')
      return saved ? JSON.parse(saved) : null
    } catch { return null }
  })
  const [checkingSession, setCheckingSession] = useState(AUTH_MODE === 'oidc')

  // Sidebar tab visibility — a map of {tab_id: boolean} resolved SOLELY
  // from the current user's User Group memberships (admins always see
  // everything — see main.py's get_my_tab_access()), used by both the
  // sidebar (hide the link) and route guards (block direct-URL
  // navigation to a hidden tab) in App.jsx. null while not yet loaded —
  // treated as "allow everything" by canSeeTab() below so the UI never
  // flashes a false "access denied" before the real matrix arrives.
  const [tabAccess, setTabAccess] = useState(null)

  const loadTabAccess = () => {
    api.getMyTabAccess().then(setTabAccess).catch(() => setTabAccess({}))
  }

  // Organisation settings from the backend (GET /api/org-config): name, key
  // account IDs and which optional integrations are switched on. `null` until
  // loaded; consumers should fall back to the neutral defaults below.
  const [orgConfig, setOrgConfig] = useState(null)

  // Tabs that only make sense when an optional integration is configured
  // (backend/app/org_config.py OPTIONAL_FEATURES). When the integration is
  // switched off the tab is hidden from the sidebar AND blocked as a route.
  const TAB_FEATURE = {
    vpn_lookup: 'vpn_lookup',
    s2s_vpn_profile_creation: 's2s_vpn',
    whitelist_lms: 'whitelist_lms',
    devops_agent: 'devops_agent',
    security_team_portal: 'security_portal',
    helpdesk_team_s3_screensaver: 'screensaver',
    licensing: 'licensing_dashboard',
    sr_ticket_information: 'sr_ticket',
    aws_resources: 'resource_inventory',
    meraki_monitor: 'meraki',
    azure_resources: 'azure_monitor',
  }

  // canSeeTab(tabId): fail OPEN (true) whenever the matrix hasn't loaded
  // yet or doesn't mention this tab_id at all — so a tab added to the
  // sidebar before it's ever added to the backend's TAB_REGISTRY doesn't
  // get silently hidden. Optional-integration tabs are additionally hidden
  // when their feature is switched off for this deployment.
  const canSeeTab = (tabId) => {
    const feature = TAB_FEATURE[tabId]
    if (feature && orgConfig && orgConfig.features && !orgConfig.features[feature]) return false
    if (!tabAccess) return true
    if (!(tabId in tabAccess)) return true
    return !!tabAccess[tabId]
  }

  useEffect(() => {
    if (AUTH_MODE !== 'oidc') return
    api.whoami().then(u => {
      if (u?.email) {
        // Save email to sessionStorage so api.js getActor() can read it in OIDC mode
        sessionStorage.setItem('job_portal_email', u.email)
        // Record when this 8-hour session window started — in
        // localStorage (NOT sessionStorage) deliberately, so it's
        // shared across every tab the user opens, not reset per-tab.
        // Only written the FIRST time it's missing: a still-valid ALB
        // session re-confirmed by a later whoami() (e.g. opening a
        // second tab, or a normal page reload) must NOT push the
        // 8-hour deadline back out, or the "8 hours from login" promise
        // would effectively become "8 hours from whichever tab/reload
        // happened last" — i.e. never actually expiring during active
        // use. See checkSessionExpiry() below for the enforcement side.
        if (!localStorage.getItem(SESSION_STARTED_AT_KEY)) {
          localStorage.setItem(SESSION_STARTED_AT_KEY, String(Date.now()))
        }
        // Fires the post-login WelcomeBanner (see WelcomeBanner.jsx) once
        // per browser TAB SESSION (sessionStorage — cleared when the tab
        // closes), the first time whoami() resolves a real user in this
        // tab. IMPORTANT — this covers BOTH cases:
        //   1. a brand-new Azure AD login round-trip, AND
        //   2. a normal page load/reopen while an existing ALB session
        //      cookie is still valid (session_timeout is 8 hours — see
        //      compute_stack.py), which is the far more common case for
        //      day-to-day use.
        // Previously this flag was only ever set right before the "Sign
        // in with Microsoft" button's redirect in LoginPage.jsx — but
        // LoginPage auto-redirects straight to /dashboard the instant
        // `user` resolves from a still-valid session, so most users
        // never actually clicked that button at all. That meant the
        // welcome banner effectively never fired for normal daily use —
        // only for whoever happened to test right after their ALB
        // session had expired or been cleared (which is why it only
        // seemed to work for one tester). The `_SEEN_KEY` guard below
        // ensures this still fires only ONCE per tab (not on every
        // hard-refresh) rather than turning into a "runs on every page
        // reload" banner.
        const SEEN_KEY = 'job_portal_session_seen'
        if (sessionStorage.getItem(SEEN_KEY) !== '1') {
          sessionStorage.setItem(SEEN_KEY, '1')
          sessionStorage.setItem('job_portal_just_logged_in', '1')
        }
      }
      setUser(u) // null if no valid ALB session yet — LoginPage shows the sign-in button
      setCheckingSession(false)
    })
  }, [])

  // Load the tab-access matrix whenever we have a logged-in user — covers
  // OIDC mode (right after whoami() resolves above) AND local mode
  // (right after the initial useState(() => ...) picks up a previously
  // saved localStorage session on page load, since that doesn't fire a
  // setUser() call this effect would otherwise catch). Re-running on
  // every `user` change also means the matrix is naturally in sync with
  // login()/logout() below without special-casing.
  useEffect(() => {
    if (user) {
      loadTabAccess()
      api.getOrgConfig().then(setOrgConfig).catch(() => setOrgConfig(null))
    } else {
      setTabAccess(null)
      setOrgConfig(null)
    }
  }, [user])

  const login = (userObj) => {
    setUser(userObj)
    if (AUTH_MODE === 'local') {
      localStorage.setItem('job_portal_user', JSON.stringify(userObj))
    } else {
      // OIDC mode: save email to sessionStorage so api.js headers() can read it.
      // sessionStorage is tab-scoped and cleared on tab close — no persistent risk.
      if (userObj?.email) {
        sessionStorage.setItem('job_portal_email', userObj.email)
      }
    }
  }

  const logout = () => {
    setUser(null)
    if (AUTH_MODE === 'local') {
      localStorage.removeItem('job_portal_user')
    } else {
      // Azure AD / ALB session cookie can't be cleared from JS (it's set
      // by the ALB, not this app). Sending the user back through the
      // OIDC-gated path forces a fresh Azure AD challenge on next visit
      // if their ALB session has actually expired; otherwise this just
      // re-establishes `user` from the still-valid session. A true
      // "sign out everywhere" would need an Azure AD logout redirect,
      // which is out of scope for this 3-person internal tool.
      sessionStorage.removeItem('job_portal_email')
      sessionStorage.removeItem('job_portal_session_seen')
      localStorage.removeItem(SESSION_STARTED_AT_KEY)
    }
  }

  const startOidcLogin = () => {
    window.location.href = `${API_BASE}/login-landing`
  }

  // Forced-logout path shared by BOTH the 8-hour timer below and api.js's
  // 401 interceptor: clears local auth state, then sends the browser to
  // the backend's /api/auth/logout redirect chain — which clears the
  // ALB's AWSELBAuthSessionCookie-* cookies AND bounces through Azure
  // AD's own end-session endpoint, so this is a REAL sign-out (not just
  // "re-confirm whatever session is still there", which is what
  // /login-landing does — that's the right target for the "Sign in
  // with Microsoft" button in LoginPage.jsx via startOidcLogin() above,
  // but the wrong one here: silently re-establishing the same session
  // that just hit its 8-hour deadline would defeat the whole point).
  // See main.py's logout() docstring for why both steps (cookie clear +
  // Azure AD redirect) are needed — clearing only one leaves either a
  // still-valid ALB cookie, or a live Azure AD SSO session that would
  // silently re-issue a fresh token on the very next OIDC challenge.
  // Guarded against firing more than once (e.g. several 401s arriving
  // in quick succession from parallel in-flight requests) with a ref,
  // so the user doesn't see multiple redirect attempts race each other.
  const forcedLogoutInFlight = useRef(false)
  const forceLogout = () => {
    if (AUTH_MODE !== 'oidc' || forcedLogoutInFlight.current) return
    forcedLogoutInFlight.current = true
    logout()
    window.location.href = `${API_BASE}/api/auth/logout`
  }

  // Register the 401 handler once — api.js calls this on ANY API
  // response with status 401, from any page, at any time (not just
  // while this 8-hour timer is running) — e.g. an admin revoking the
  // user's access mid-session, or the ALB session expiring for any
  // other reason. See api.js's registerSessionExpiredHandler() for the
  // full rationale.
  useEffect(() => {
    if (AUTH_MODE !== 'oidc') return
    registerSessionExpiredHandler(forceLogout)
  }, [])

  // 8-hour absolute session timer — re-checked every
  // SESSION_CHECK_INTERVAL_MS while a user is logged in. This is the
  // client-side half of "auto sign out after 8 hours"; the ALB's own
  // session_timeout=Duration.hours(8) is the actual security boundary
  // and would eventually force this anyway via 401s (caught by the
  // interceptor above) — this timer just makes the cutover happen
  // exactly on time instead of waiting for the user's next click.
  useEffect(() => {
    if (AUTH_MODE !== 'oidc' || !user) return
    const checkSessionExpiry = () => {
      const startedAt = Number(localStorage.getItem(SESSION_STARTED_AT_KEY) || 0)
      if (startedAt && Date.now() - startedAt >= SESSION_MAX_AGE_MS) {
        forceLogout()
      }
    }
    checkSessionExpiry() // catch an already-overdue session immediately, not just on the next interval tick
    const intervalId = setInterval(checkSessionExpiry, SESSION_CHECK_INTERVAL_MS)
    return () => clearInterval(intervalId)
  }, [user])

  // User-initiated sign-out (the sidebar's "Sign out" button in
  // App.jsx) — deliberately routes through the SAME real-logout path as
  // forceLogout() above in OIDC mode, rather than just clearing local
  // state and client-side navigating to "/". Before this, clicking
  // "Sign out" left the ALB's AWSELBAuthSessionCookie-* untouched, so a
  // user within their 8-hour window who clicked "Sign out" and then
  // reopened the app would find themselves silently signed back in —
  // "Sign out" only ever looked like it worked. Local mode is unaffected
  // (there's no ALB/Azure AD session to worry about there).
  const signOut = () => {
    if (AUTH_MODE === 'oidc') {
      logout()
      window.location.href = `${API_BASE}/api/auth/logout`
    } else {
      logout()
    }
  }

  return (
    <AuthContext.Provider value={{
      user, login, logout, signOut, authMode: AUTH_MODE, checkingSession, startOidcLogin,
      canSeeTab, refreshTabAccess: loadTabAccess, orgConfig,
    }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
