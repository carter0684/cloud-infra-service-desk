import React, { useState, useEffect } from 'react'
import { api } from '../api.js'

// Site-wide yellow banner. Content (message text) and visibility
// (enabled on/off) are now admin-editable from Administration > Admin
// page > "Site Notice (yellow banner)" — see AdminPage.jsx — instead of
// being hardcoded here. This component just fetches the current value on
// mount and renders it (or nothing, if disabled/still-loading/errored).
//
// The dismiss button still only hides it for the current browser session
// (sessionStorage) — an admin turning it back on, or a new session, will
// show it again. Dismissal key is versioned by a hash of the message text
// itself (see dismissKeyFor) so that editing the message re-surfaces the
// banner even for users who already dismissed the OLD text this session.

function dismissKeyFor(message) {
  // Cheap non-cryptographic hash — just needs to change when the message
  // text changes, not to be secure.
  let h = 0
  for (let i = 0; i < message.length; i++) {
    h = (h * 31 + message.charCodeAt(i)) | 0
  }
  return `job_portal_freeze_notice_dismissed_${h}`
}

// Minimal **bold** + newline renderer — the admin edit box is a plain
// textarea (no rich text editor), so `**like this**` is the only markup
// supported, kept intentionally simple rather than pulling in a markdown
// library for a single banner.
function renderMessage(message) {
  const lines = message.split('\n')
  return lines.map((line, li) => {
    const parts = line.split(/(\*\*[^*]+\*\*)/g).filter(Boolean)
    return (
      <React.Fragment key={li}>
        {li > 0 && <br />}
        {parts.map((part, pi) => {
          if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
            return <strong key={pi}>{part.slice(2, -2)}</strong>
          }
          return <span key={pi}>{part}</span>
        })}
      </React.Fragment>
    )
  })
}

const WRAP = {
  display: 'flex', justifyContent: 'center', background: '#fef9c3',
  borderBottom: '1px solid #fde047',
}
const INNER = {
  maxWidth: 980, width: '100%', padding: '10px 20px', display: 'flex',
  alignItems: 'flex-start', gap: 12, color: '#713f12', fontSize: 13,
}
const CLOSE_BTN = {
  marginLeft: 'auto', background: 'transparent', border: 'none', cursor: 'pointer',
  color: '#92400e', fontSize: 16, lineHeight: 1, padding: '0 4px', flexShrink: 0,
}

// Phones: the full message can fill half of the first screen, so below this
// width it shows two lines and a "Show more" toggle (same 768px breakpoint as
// the sidebar drawer in App.jsx).
function useNarrowScreen() {
  const query = '(max-width: 768px)'
  const [narrow, setNarrow] = useState(() => typeof window !== 'undefined' && window.matchMedia(query).matches)
  useEffect(() => {
    const mql = window.matchMedia(query)
    const onChange = (e) => setNarrow(e.matches)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [])
  return narrow
}

const CLAMPED = {
  display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden',
}
const TOGGLE_BTN = {
  background: 'none', border: 'none', padding: 0, marginTop: 4, color: '#92400e',
  fontSize: 12, fontWeight: 700, textDecoration: 'underline', cursor: 'pointer',
}

export default function ProductionFreezeNotice() {
  const [notice, setNotice] = useState(null) // { enabled, message } | null while loading/errored
  const [dismissed, setDismissed] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const narrow = useNarrowScreen()

  useEffect(() => {
    api.getFreezeNotice()
      .then(d => {
        setNotice(d)
        setDismissed(sessionStorage.getItem(dismissKeyFor(d.message || '')) === '1')
      })
      .catch(() => setNotice(null)) // fail silently — a broken banner call should never block the rest of the portal
  }, [])

  if (!notice || !notice.enabled || !notice.message) return null
  if (dismissed) return null

  const dismiss = () => {
    sessionStorage.setItem(dismissKeyFor(notice.message), '1')
    setDismissed(true)
  }

  return (
    <div style={WRAP}>
      <div style={INNER}>
        <span style={{ fontSize: 16, flexShrink: 0 }}>❄️</span>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={narrow && !expanded ? CLAMPED : undefined}>{renderMessage(notice.message)}</div>
          {narrow && (
            <button type="button" onClick={() => setExpanded(v => !v)} aria-expanded={expanded} style={TOGGLE_BTN}>
              {expanded ? 'Show less' : 'Show more'}
            </button>
          )}
        </div>
        <button onClick={dismiss} title="Dismiss for this session" aria-label="Dismiss notice" style={CLOSE_BTN}>✕</button>
      </div>
    </div>
  )
}
