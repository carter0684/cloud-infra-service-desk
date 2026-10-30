import React, { useMemo, useState, useEffect } from 'react'
import { COLORS, CARD } from '../utils/theme.js'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'
import { useCall } from './CallContext.jsx'
import Avatar from '../components/Avatar.jsx'

// job-portal-chat-voice Task 15.1 — Requirement 8.6/8.7: a ringing Call
// must surface to the invited Team_Member regardless of which page of
// the Portal they're currently looking at, so this is mounted once at
// the app-shell level (App.jsx's Shell(), a sibling of ChatBot) rather
// than inside ChatPage.jsx — a user on, say, the Dashboard tab still
// gets the incoming-call banner without needing to have Chat open.
//
// Renders nothing when there is no incoming ring for the current user
// (ChatContext.jsx's `incomingCall` is null) — CallContext.jsx already
// guarantees `incomingCall` is only ever set to a call where THIS
// user's own participant entry is "ringing", so no extra filtering is
// needed here.
export default function IncomingCallOverlay() {
  const { user } = useAuth() || {}
  const call = useCall()
  const [directory, setDirectory] = useState([])

  // Best-effort caller display name — falls back to the raw email if the
  // directory hasn't loaded yet (e.g. a ring arrives within the very
  // first second after login). Reuses whatever ChatPage.jsx's own
  // directory fetch would already show; this component fetches its own
  // copy rather than depending on ChatPage.jsx being mounted at all,
  // since — per this component's whole purpose — the user may well be
  // on a completely different page right now.
  useEffect(() => {
    if (!call?.incomingCall) return
    // Deliberately lazy — only bothers fetching the directory the
    // moment there's actually a ring to label, not on every page load
    // app-wide. api.js is already statically imported by most of this
    // app's other components anyway, so importing it directly here
    // (rather than a dynamic import()) avoids Vite/Rollup warning about
    // a module being both statically AND dynamically imported, which
    // otherwise defeats its own code-splitting attempt for no benefit.
    api.listUsersDirectory().then((data) => setDirectory(data?.users || [])).catch(() => {})
  }, [call?.incomingCall?.call_id])

  const callerInfo = useMemo(() => {
    if (!call?.incomingCall) return null
    const email = call.incomingCall.caller_email
    return directory.find((u) => u.email === email) || { email, name: email }
  }, [call?.incomingCall, directory])

  if (!user || !call?.incomingCall || !callerInfo) return null

  const isGroup = call.incomingCall.call_type === 'group'
  const otherRingingCount = Object.keys(call.incomingCall.participants || {}).length - 1

  const handleAccept = () => { call.acceptIncomingCall().catch(() => {}) }
  const handleDecline = () => { call.declineIncomingCall() }

  return (
    <div style={{
      position: 'fixed', top: 20, right: 20, zIndex: 2000,
      width: 320, animation: 'incomingCallPulse 1.6s ease-in-out infinite',
    }}>
      <style>{`
        @keyframes incomingCallPulse {
          0%, 100% { box-shadow: 0 4px 20px rgba(22,163,74,0.15); }
          50% { box-shadow: 0 4px 28px rgba(22,163,74,0.4); }
        }
      `}</style>
      <div style={{ ...CARD, padding: 18, border: `2px solid ${COLORS.success}` }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Avatar photoUrl={callerInfo.photo_data_url} name={callerInfo.name} email={callerInfo.email} size={44} fontSize={17} />
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: COLORS.textPrimary, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {callerInfo.name}
            </div>
            <div style={{ fontSize: 11.5, color: COLORS.textMuted }}>
              {isGroup
                ? `Group call${otherRingingCount > 0 ? ` · +${otherRingingCount} others invited` : ''}`
                : 'Incoming call'}
            </div>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
          <button onClick={handleDecline}
            style={{
              flex: 1, padding: '9px 0', borderRadius: 8, border: 'none',
              background: COLORS.dangerBg, color: COLORS.danger, fontWeight: 700, fontSize: 13,
              cursor: 'pointer',
            }}>
            ✕ Decline
          </button>
          <button onClick={handleAccept}
            style={{
              flex: 1, padding: '9px 0', borderRadius: 8, border: 'none',
              background: COLORS.success, color: '#fff', fontWeight: 700, fontSize: 13,
              cursor: 'pointer',
            }}>
            📞 Accept
          </button>
        </div>
      </div>
    </div>
  )
}
