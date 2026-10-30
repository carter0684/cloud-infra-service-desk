import React, { useEffect, useState } from 'react'
import { useAuth } from '../auth/AuthContext.jsx'
import Avatar from './Avatar.jsx'
import { COLORS } from '../utils/theme.js'

const FLAG_KEY = 'job_portal_just_logged_in'

// Fires exactly once per real login event (set by LoginPage right before
// login()/navigate() in local mode, or right before the Azure AD redirect
// in OIDC mode — see LoginPage.jsx). Mounted once in Shell so it appears
// no matter which page the user lands on right after signing in, then
// self-clears the sessionStorage flag so it never reappears on normal
// navigation/refresh within the same session.
export default function WelcomeBanner() {
  const { user } = useAuth()
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    if (!user) return
    if (sessionStorage.getItem(FLAG_KEY) === '1') {
      sessionStorage.removeItem(FLAG_KEY)
      setVisible(true)
      const timer = setTimeout(() => setVisible(false), 6000)
      return () => clearTimeout(timer)
    }
  }, [user])

  if (!visible || !user) return null

  return (
    <div
      className="welcome-banner-animate"
      style={{
        position: 'fixed', top: 70, right: 24, zIndex: 200,
        background: '#fff', borderRadius: 12, boxShadow: '0 8px 28px rgba(15,23,42,0.18)',
        border: `1px solid ${COLORS.border}`, padding: '16px 20px', display: 'flex',
        alignItems: 'center', gap: 14,
        // min() keeps this from overflowing the left edge on a narrow
        // phone viewport (360 + the 24px right offset would otherwise
        // clip on anything under ~384px wide) while leaving desktop's
        // original 360px cap untouched on wider screens.
        maxWidth: 'min(360px, calc(100vw - 48px))',
      }}
    >
      <Avatar photoUrl={user.photo_data_url} name={user.name} email={user.email} size={48} fontSize={18} bg={COLORS.brand} />
      <div style={{ minWidth: 0 }}>
        <div style={{ color: COLORS.textPrimary, fontSize: 14.5, fontWeight: 700, marginBottom: 2 }}>
          {user.name}, Welcome to Cloud Infra Service Desk 👋
        </div>
        <div style={{ color: COLORS.textSecondary, fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {user.email}
        </div>
        <div style={{ color: COLORS.textMuted, fontSize: 11.5, marginTop: 1 }}>
          {user.is_admin ? 'Administrator' : 'Team Member'}
        </div>
      </div>
      <button
        onClick={() => setVisible(false)}
        aria-label="Dismiss"
        style={{
          background: 'none', border: 'none', color: COLORS.textMuted, fontSize: 16,
          cursor: 'pointer', alignSelf: 'flex-start', padding: 0, lineHeight: 1,
        }}
      >
        ✕
      </button>
    </div>
  )
}
