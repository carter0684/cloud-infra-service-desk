import React, { useEffect, useRef, useState, useMemo } from 'react'
import { COLORS } from '../utils/theme.js'
import { useAuth } from '../auth/AuthContext.jsx'
import { useCall } from './CallContext.jsx'
import { api } from '../api.js'
import Avatar from '../components/Avatar.jsx'

// job-portal-chat-voice Task 15.2 — the full-screen active-call view:
// local + remote video tiles, mute/camera/screen-share/leave controls,
// and a roster panel. Mounted from App.jsx's Shell() as a sibling of
// IncomingCallOverlay.jsx — rendered ON TOP of whatever page/route the
// user is currently on (per Requirement 5.4/6.4's "the call continues
// regardless of navigation" intent) rather than as a route of its own,
// so navigating elsewhere in the Portal never has to first end the call
// just to get out of a dedicated "/call" page.
//
// Screen-share capability check (Requirement 7.1's Web/Desktop-only
// scope): navigator.mediaDevices.getDisplayMedia simply does not exist
// on mobile Safari/Chrome — this is used both here (to hide the Share
// Screen button entirely on an unsupported platform, rather than
// showing a button that would always fail) and again defensively inside
// CallContext.jsx's startScreenShare() itself.
const SCREEN_SHARE_SUPPORTED = typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getDisplayMedia

function VideoTile({ tileId, meetingSession, label, isLocal, isContent }) {
  const videoRef = useRef(null)

  useEffect(() => {
    const session = meetingSession
    const el = videoRef.current
    if (!session || !el || tileId == null) return
    session.audioVideo.bindVideoElement(tileId, el)
    return () => {
      // unbindVideoElement's second arg (cleanUpVideoElement) defaults
      // to true in the SDK, which is exactly what's wanted here — the
      // <video> element itself is about to be unmounted/reused for a
      // different tileId regardless.
      try { session.audioVideo.unbindVideoElement(tileId) } catch { /* tile may already be gone */ }
    }
  }, [meetingSession, tileId])

  return (
    <div style={{
      position: 'relative', background: '#111827', borderRadius: 10, overflow: 'hidden',
      aspectRatio: isContent ? '16/9' : '4/3', width: '100%',
    }}>
      <video ref={videoRef} autoPlay playsInline muted={isLocal}
        style={{ width: '100%', height: '100%', objectFit: isContent ? 'contain' : 'cover', display: 'block', background: '#111827' }} />
      <div style={{
        position: 'absolute', bottom: 6, left: 8, color: '#fff', fontSize: 11.5, fontWeight: 600,
        textShadow: '0 1px 3px rgba(0,0,0,0.8)',
      }}>
        {label}{isLocal ? ' (You)' : ''}{isContent ? ' — Screen' : ''}
      </div>
    </div>
  )
}

// Renders an audio-only participant (no active video tile) as a plain
// avatar-in-a-box tile, so the layout stays a consistent grid regardless
// of who currently has their camera on.
function AudioOnlyTile({ email, name, photoUrl, isLocal }) {
  return (
    <div style={{
      position: 'relative', background: COLORS.sidebarBg, borderRadius: 10, overflow: 'hidden',
      aspectRatio: '4/3', width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <Avatar photoUrl={photoUrl} name={name} email={email} size={56} fontSize={20} />
      <div style={{
        position: 'absolute', bottom: 6, left: 8, color: '#fff', fontSize: 11.5, fontWeight: 600,
        textShadow: '0 1px 3px rgba(0,0,0,0.8)',
      }}>
        {name}{isLocal ? ' (You)' : ''}
      </div>
    </div>
  )
}

function ControlButton({ onClick, active, danger, disabled, icon, label }) {
  return (
    <button onClick={onClick} disabled={disabled} title={label}
      style={{
        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
        width: 64, padding: '9px 0', borderRadius: 10, border: 'none', cursor: disabled ? 'default' : 'pointer',
        background: danger ? COLORS.danger : active ? '#fff' : 'rgba(255,255,255,0.15)',
        color: danger ? '#fff' : active ? COLORS.textPrimary : '#fff',
        opacity: disabled ? 0.5 : 1,
      }}>
      <span style={{ fontSize: 18 }}>{icon}</span>
      <span style={{ fontSize: 9.5, fontWeight: 600 }}>{label}</span>
    </button>
  )
}

export default function CallView() {
  const { user } = useAuth() || {}
  const call = useCall()
  const [directory, setDirectory] = useState([])
  const [showRoster, setShowRoster] = useState(false)
  const audioElRef = useRef(null)

  useEffect(() => {
    api.listUsersDirectory().then((data) => setDirectory(data?.users || [])).catch(() => {})
  }, [])

  const usersByEmail = useMemo(() => {
    const map = {}
    for (const u of directory) map[u.email] = u
    return map
  }, [directory])

  // Bind the single shared <audio> element to the meeting's audio mix
  // as soon as a MeetingSession exists — every remote attendee's audio
  // plays through this ONE element (the SDK internally mixes all remote
  // audio streams together), unlike video which is one <video> element
  // per tile.
  useEffect(() => {
    const session = call?.meetingSession
    const el = audioElRef.current
    if (!session || !el) return
    session.audioVideo.bindAudioElement(el).catch(() => {
      call.setError?.('Could not start call audio playback.')
    })
    return () => { try { session.audioVideo.unbindAudioElement() } catch { /* session already stopped */ } }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [call?.meetingSession])

  // Nothing to render at all while there is no active call for this
  // user — this component is mounted unconditionally at the app-shell
  // level (like IncomingCallOverlay.jsx) precisely so it can appear
  // instantly the moment a call starts, without needing its own route.
  if (!call?.activeCall) return null

  const activeCall = call.activeCall
  const isGroup = activeCall.call_type === 'group'
  // Every OTHER participant currently "joined" (has an active/possible
  // video tile) — the caller's own roster entry never appears in
  // `participants` (see database.py's CallSession model comment), so
  // for a Direct_Call the "other party" is derived from caller_email
  // vs the current user instead of the participants map alone.
  const joinedEmails = Object.entries(activeCall.participants || {})
    .filter(([, p]) => p.state === 'joined')
    .map(([email]) => email)
  const otherPartyEmails = activeCall.caller_email !== user?.email
    ? [activeCall.caller_email, ...joinedEmails.filter((e) => e !== user?.email)]
    : joinedEmails

  const localName = 'You'
  const localContentTile = call.roster[`${user?.email}::content`]

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 1900, background: '#1e293b',
      display: 'flex', flexDirection: 'column',
    }}>
      <audio ref={audioElRef} style={{ display: 'none' }} />

      <div style={{ padding: '12px 20px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', color: '#fff' }}>
        <div style={{ fontSize: 13, fontWeight: 600 }}>
          {call.sessionStatus === 'connecting' ? '📞 Connecting…' : isGroup ? '👥 Group Call' : '📞 Call'}
        </div>
        <button onClick={() => setShowRoster((v) => !v)}
          style={{ border: 'none', background: 'rgba(255,255,255,0.15)', color: '#fff', borderRadius: 7, padding: '5px 12px', fontSize: 11.5, cursor: 'pointer' }}>
          👥 {otherPartyEmails.length + 1}
        </button>
      </div>

      {call.error && (
        <div style={{ margin: '0 20px 8px', padding: '8px 12px', background: COLORS.dangerBg, color: COLORS.danger, borderRadius: 8, fontSize: 12 }}>
          ⚠️ {call.error}
        </div>
      )}

      <div style={{ flex: 1, overflowY: 'auto', padding: '0 20px 20px' }}>
        <div style={{
          display: 'grid', gap: 12,
          gridTemplateColumns: `repeat(${Math.min(otherPartyEmails.length + 1, 3)}, 1fr)`,
        }}>
          {call.isVideoOn && call.localTileId != null
            ? <VideoTile tileId={call.localTileId} meetingSession={call.meetingSession} label={localName} isLocal />
            : <AudioOnlyTile email={user?.email} name={localName} photoUrl={user?.photo_data_url} isLocal />}

          {otherPartyEmails.map((email) => {
            const info = call.roster[email]
            const person = usersByEmail[email]
            const displayName = person?.name || email
            if (info?.active && info.tileId != null) {
              return <VideoTile key={email} tileId={info.tileId} meetingSession={call.meetingSession} label={displayName} />
            }
            return <AudioOnlyTile key={email} email={email} name={displayName} photoUrl={person?.photo_data_url} />
          })}

          {/* Remote screen-share tiles — one per attendee currently
              sharing, rendered as extra wide tiles appended after the
              camera grid above. Requirement 7.2/7.3. */}
          {Object.entries(call.roster)
            .filter(([key, info]) => info.isContent && key !== `${user?.email}::content`)
            .map(([key, info]) => (
              <div key={key} style={{ gridColumn: '1 / -1' }}>
                <VideoTile tileId={info.tileId} meetingSession={call.meetingSession} label={usersByEmail[info.email]?.name || info.email} isContent />
              </div>
            ))}

          {localContentTile && (
            <div style={{ gridColumn: '1 / -1' }}>
              <VideoTile tileId={localContentTile.tileId} meetingSession={call.meetingSession} label={localName} isLocal isContent />
            </div>
          )}
        </div>
      </div>

      {showRoster && (
        <div style={{
          position: 'absolute', top: 52, right: 20, width: 220, background: '#fff', borderRadius: 10,
          boxShadow: '0 4px 20px rgba(0,0,0,0.25)', padding: 10, maxHeight: 300, overflowY: 'auto',
        }}>
          <div style={{ fontSize: 11.5, fontWeight: 700, color: COLORS.textMuted, marginBottom: 6, textTransform: 'uppercase' }}>Roster</div>
          {[activeCall.caller_email, ...Object.keys(activeCall.participants || {})]
            .filter((e, i, arr) => arr.indexOf(e) === i)
            .map((email) => {
              const state = email === activeCall.caller_email ? 'caller' : (activeCall.participants || {})[email]?.state
              const person = usersByEmail[email]
              return (
                <div key={email} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 2px' }}>
                  <Avatar photoUrl={person?.photo_data_url} name={person?.name || email} email={email} size={24} fontSize={11} />
                  <span style={{ flex: 1, fontSize: 12, color: COLORS.textPrimary, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {person?.name || email}
                  </span>
                  <span style={{ fontSize: 10, color: COLORS.textMuted }}>{state}</span>
                </div>
              )
            })}
        </div>
      )}

      <div style={{ padding: '14px 0 26px', display: 'flex', justifyContent: 'center', gap: 14 }}>
        <ControlButton
          onClick={call.toggleMute} active={call.isMuted}
          icon={call.isMuted ? '🔇' : '🎤'} label={call.isMuted ? 'Unmute' : 'Mute'}
        />
        <ControlButton
          onClick={call.toggleVideo} active={call.isVideoOn}
          icon={call.isVideoOn ? '📷' : '📷'} label={call.isVideoOn ? 'Stop Video' : 'Start Video'}
        />
        {SCREEN_SHARE_SUPPORTED && (
          <ControlButton
            onClick={call.isSharingScreen ? call.stopScreenShare : call.startScreenShare}
            active={call.isSharingScreen}
            icon="🖥️" label={call.isSharingScreen ? 'Stop Share' : 'Share Screen'}
          />
        )}
        <ControlButton onClick={call.hangUp} danger icon="📴" label="Leave" />
      </div>
    </div>
  )
}
