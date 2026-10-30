import useOrg from '../utils/useOrg.js'
import React from 'react'
import { COLORS } from '../utils/theme.js'

// ── Licensing (Azure License Dashboard) ─────────────────────────────────
// A separate Azure AD-protected dashboard (azure-license-dashboard.
// example.com), NOT part of this job-portal codebase. It uses the
// SAME Azure AD tenant as this portal's own SSO, so a user who's already
// signed in here usually does NOT need to re-enter their password there.
//
// Native <iframe> embedding was tried (via IframeWithFallback.jsx) and
// confirmed NOT to work — Azure AD's own sign-in flow refuses to render
// inside a frame (X-Frame-Options/CSP frame-ancestors), so it's opened
// in a new top-level tab instead. That single top-level navigation is
// still usually enough for Azure AD to silently reuse this browser's
// existing tenant session, so most users won't see a password prompt —
// just not literally inline inside this page.
// URL comes from the backend setting LICENSING_DASHBOARD_URL (GET /api/org-config).

export default function LicensingPage() {
  const { licensingUrl } = useOrg()
  return (
    <div style={{ padding: '16px 20px 0' }}>
      <div style={{ marginBottom: 16 }}>
        <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>📜 Licensing</h2>
        <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
          Azure License Dashboard — uses the same Azure AD sign-in as this portal.
        </div>
      </div>

      <div style={{
        border: `1px solid ${COLORS.border}`, borderRadius: 10, background: '#fff',
        padding: '40px 32px', textAlign: 'center', maxWidth: 480,
      }}>
        <div style={{ fontSize: 40, marginBottom: 12 }}>📜</div>
        <div style={{ color: COLORS.textPrimary, fontSize: 15, fontWeight: 600, marginBottom: 6 }}>
          Azure License Dashboard
        </div>
        <div style={{ color: COLORS.textSecondary, fontSize: 13, marginBottom: 20, lineHeight: 1.5 }}>
          Opens in a new tab. It uses the same Azure AD tenant as this portal, so you normally
          won't be asked to sign in again — embedding it directly here doesn't work because
          Azure AD's own sign-in flow refuses to run inside a frame.
        </div>
        <a
          href={licensingUrl}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: 'inline-block', padding: '10px 22px', borderRadius: 7,
            background: COLORS.brand || '#1a4080', color: '#fff', fontWeight: 600,
            fontSize: 14, textDecoration: 'none',
          }}
        >
          Open Azure License Dashboard ↗
        </a>
      </div>
    </div>
  )
}
