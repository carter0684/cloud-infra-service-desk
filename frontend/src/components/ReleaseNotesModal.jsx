import React, { useEffect, useState } from 'react'
import { COLORS } from '../utils/theme.js'

// "What's New" modal — surfaces newly released portal features so users
// don't have to be told verbally / dig through commit history to
// discover them. Shows once per RELEASE_VERSION per browser (dismissal
// is stored in localStorage, so it persists across sessions/tabs, unlike
// WelcomeBanner's per-login sessionStorage flag). Bump RELEASE_VERSION
// and update RELEASE_NOTES whenever a new batch of features ships — a
// user who already dismissed an OLDER version will see the modal again
// for the new one, since the dismissal key is versioned.
const RELEASE_VERSION = '2026-09-08'

const RELEASE_NOTES = [
  {
    icon: '🔐',
    title: 'VPN Profile Lookup',
    description:
      "Enter a site's WAN IP to look up its Site-to-Site VPN connection — full tunnel/IKE/IPsec/DPD profile, no more digging through the console. Found under Resources.",
  },
  {
    icon: '🛎️',
    title: 'S3-ScreenSaver',
    description:
      'Upload and manage the screensaver images served to devices via S3/CloudFront, directly from the portal. Found under Helpdesk Team.',
  },
  {
    icon: '🔓',
    title: 'Whitelist — LMS',
    description:
      "Check and add IPs to the LMS WAF IPSets / Security Group whitelist. Requires a SR number before adding, and every check/add is recorded to an audit log (visible to Cloud Infra/Security/Management Team). Found under Whitelist.",
  },
]

const DISMISS_KEY = `job_portal_release_notes_dismissed_${RELEASE_VERSION}`

export default function ReleaseNotesModal() {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    if (localStorage.getItem(DISMISS_KEY) !== '1') {
      setVisible(true)
    }
  }, [])

  if (!visible) return null

  const dismiss = () => {
    localStorage.setItem(DISMISS_KEY, '1')
    setVisible(false)
  }

  return (
    <div
      onClick={dismiss}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.5)', zIndex: 300,
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16,
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: '#fff', borderRadius: 14, padding: 26, maxWidth: 440, width: '100%',
          boxShadow: '0 20px 60px rgba(15,23,42,0.3)', maxHeight: '85vh', overflowY: 'auto',
        }}
      >
        <div style={{ fontSize: 18, fontWeight: 700, color: COLORS.textPrimary, marginBottom: 4 }}>
          🎉 What's New
        </div>
        <div style={{ fontSize: 12.5, color: COLORS.textMuted, marginBottom: 18 }}>
          New tools just added to the Cloud Infra Service Desk portal
        </div>

        {RELEASE_NOTES.map((note, i) => (
          <div key={i} style={{ display: 'flex', gap: 12, marginBottom: 16 }}>
            <div style={{ fontSize: 22, flexShrink: 0 }}>{note.icon}</div>
            <div>
              <div style={{ fontWeight: 600, fontSize: 13.5, color: COLORS.textPrimary }}>{note.title}</div>
              <div style={{ fontSize: 12.5, color: COLORS.textSecondary, marginTop: 2, lineHeight: 1.5 }}>
                {note.description}
              </div>
            </div>
          </div>
        ))}

        <button
          onClick={dismiss}
          style={{
            width: '100%', marginTop: 6, padding: '10px 0', borderRadius: 8, border: 'none',
            background: COLORS.brand, color: '#fff', fontWeight: 600, fontSize: 13.5, cursor: 'pointer',
          }}
        >
          Got it
        </button>
      </div>
    </div>
  )
}
