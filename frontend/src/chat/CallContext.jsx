import React, { createContext, useContext, useState, useRef, useCallback, useEffect } from 'react'
import { api } from '../api.js'
import { useChat } from './ChatContext.jsx'
import { buildMeetingSessionFromCallResponse, buildMeetingSessionFromJoinInfo } from './chimeSdk.js'

// job-portal-chat-voice Task 15.1-15.4 — owns the ONE live
// amazon-chime-sdk-js `MeetingSession` instance (if any) this browser
// tab currently holds, plus the roster/self state IncomingCallOverlay.jsx
// and CallView.jsx both need to render. Deliberately a SEPARATE context
// from ChatContext.jsx (Task 13.3) rather than folded into it —
// ChatContext's `activeCall`/`incomingCall` are plain, WS-driven,
// serializable call dicts (safe to reference from anywhere, including
// this very file below), but a MeetingSession is a live, stateful SDK
// object with its own audio/video pipeline and WebSocket signaling
// connection to Chime's servers; mixing the two into one context would
// make ChatContext's simple "just mirrors the latest WS payload" model
// much harder to reason about.
//
// Lifecycle summary:
//   1. ChatContext.jsx's WS handler sets `incomingCall`/`activeCall`
//      from the `call.update` broadcast — this context reacts to THOSE,
//      it never itself listens to the WS directly.
//   2. Caller path: startCall() -> api.createCall() -> immediately
//      build+start a MeetingSession from the response (the Caller
//      already has their own attendee/join_token from create_call()
//      per Task 15.0's backend fix) -> ChatContext's own `call.update`
//      handling will separately flip `activeCall` once the broadcast
//      arrives, but this context doesn't need to wait for that to
//      start rendering the Caller's own local video tile.
//   3. Callee path: acceptIncomingCall() -> api.acceptCall() -> build+
//      start a MeetingSession from the response's `_join_info`.
//   4. hangUp()/declineIncomingCall() always call the matching REST
//      action AND locally tear down/null the MeetingSession — never
//      rely solely on a future WS broadcast to know to stop, since the
//      acting client already knows the outcome from its own REST
//      response.
const CallContext = createContext(null)

export function CallProvider({ children }) {
  const chat = useChat()
  const incomingCall = chat?.incomingCall
  const activeCall = chat?.activeCall
  const setIncomingCall = chat?.setIncomingCall
  const setActiveCall = chat?.setActiveCall

  // 'idle' | 'connecting' | 'connected' | 'ending'
  const [sessionStatus, setSessionStatus] = useState('idle')
  const [error, setError] = useState('')
  // roster: { [email]: { tileId, active, paused } } — populated purely
  // from amazon-chime-sdk-js's own AudioVideoObserver/videoTileDidUpdate
  // callbacks below, NOT from the call dict's `participants` map (that
  // map is signaling-state only — ringing/joined/declined/etc — and has
  // no notion of a Chime video tile id at all).
  const [roster, setRoster] = useState({})
  const [isMuted, setIsMuted] = useState(false)
  const [isVideoOn, setIsVideoOn] = useState(false)
  const [isSharingScreen, setIsSharingScreen] = useState(false)
  const [localTileId, setLocalTileId] = useState(null)

  const meetingSessionRef = useRef(null)
  const observerRef = useRef(null)
  const contentShareObserverRef = useRef(null)

  const teardownMeetingSession = useCallback(() => {
    const session = meetingSessionRef.current
    if (session) {
      try {
        if (observerRef.current) session.audioVideo.removeObserver(observerRef.current)
        if (contentShareObserverRef.current) session.audioVideo.removeContentShareObserver(contentShareObserverRef.current)
        session.audioVideo.stop()
      } catch {
        // Best-effort — the meeting is ending regardless (the backend
        // call/meeting is already gone by the time this runs), so a
        // teardown-path error here must never block the UI from
        // returning to its idle state.
      }
    }
    meetingSessionRef.current = null
    observerRef.current = null
    contentShareObserverRef.current = null
    setSessionStatus('idle')
    setRoster({})
    setIsMuted(false)
    setIsVideoOn(false)
    setIsSharingScreen(false)
    setLocalTileId(null)
  }, [])

  const attachObservers = useCallback((session) => {
    const observer = {
      videoTileDidUpdate: (tileState) => {
        if (!tileState.boundAttendeeId) return
        // Chime's AttendeeId for THIS Portal's attendees is always
        // created with ExternalUserId=<email> (see chime_client.py's
        // add_attendee()) — attendeeId itself is an opaque Chime UUID,
        // but externalUserId round-trips the email back out, which is
        // exactly what the roster UI needs to label each tile.
        const email = tileState.boundExternalUserId
        if (tileState.localTile) {
          setLocalTileId(tileState.tileId)
          setIsVideoOn(true)
          return
        }
        if (!email) return
        // A remote screen-share arrives as a SEPARATE tile from that
        // same attendee's camera tile (tileState.isContent === true,
        // its own distinct tileId) — keyed here by
        // `${email}::content` so it never overwrites that attendee's
        // regular camera roster entry. CallView.jsx renders it as an
        // extra, larger tile when present.
        const key = tileState.isContent ? `${email}::content` : email
        setRoster((prev) => ({
          ...prev,
          [key]: { tileId: tileState.tileId, active: tileState.active, paused: tileState.paused, isContent: tileState.isContent, email },
        }))
      },
      videoTileWasRemoved: (tileId) => {
        setRoster((prev) => {
          const next = { ...prev }
          for (const [key, info] of Object.entries(next)) {
            if (info.tileId === tileId) delete next[key]
          }
          return next
        })
        setLocalTileId((prev) => (prev === tileId ? null : prev))
      },
      audioVideoDidStop: () => {
        // The SDK itself decided to stop (e.g. the meeting ended server-
        // side, or a fatal connection failure) — make sure this
        // context's own state reflects that even if hangUp()/decline
        // was never explicitly called by this client.
        teardownMeetingSession()
      },
    }
    observerRef.current = observer
    session.audioVideo.addObserver(observer)

    const contentShareObserver = {
      contentShareDidStop: () => setIsSharingScreen(false),
    }
    contentShareObserverRef.current = contentShareObserver
    session.audioVideo.addContentShareObserver(contentShareObserver)
  }, [teardownMeetingSession])

  const startMeetingSession = useCallback(async (session) => {
    meetingSessionRef.current = session
    attachObservers(session)
    setSessionStatus('connecting')
    try {
      // Requirement 5.3/5.4: audio starts immediately on join — video
      // is opt-in via the CallView.jsx camera toggle (startLocalVideo
      // below), not started automatically, so a joining participant
      // never has their camera switched on without their own action.
      const audioInputs = await session.audioVideo.listAudioInputDevices()
      if (audioInputs[0]) await session.audioVideo.startAudioInput(audioInputs[0].deviceId)
      session.audioVideo.start()
      setSessionStatus('connected')
    } catch (e) {
      setError('Could not start the call audio. Please check your microphone permissions.')
      teardownMeetingSession()
      throw e
    }
  }, [attachObservers, teardownMeetingSession])

  // ── Caller path ──────────────────────────────────────────────────────
  const startCall = useCallback(async (conversationId, calleeEmails) => {
    setError('')
    try {
      const call = await api.createCall(conversationId, calleeEmails)
      const session = buildMeetingSessionFromCallResponse(call)
      await startMeetingSession(session)
      setActiveCall?.(call)
      return call
    } catch (e) {
      setError(e?.response?.data?.detail || 'Could not start the call.')
      throw e
    }
  }, [startMeetingSession, setActiveCall])

  // ── Callee path ──────────────────────────────────────────────────────
  const acceptIncomingCall = useCallback(async () => {
    if (!incomingCall) return
    setError('')
    try {
      const result = await api.acceptCall(incomingCall.call_id)
      const session = buildMeetingSessionFromJoinInfo(result._join_info)
      await startMeetingSession(session)
      setActiveCall?.(result)
      setIncomingCall?.(null)
    } catch (e) {
      setError(e?.response?.data?.detail || 'Could not join the call.')
      setIncomingCall?.(null)
      throw e
    }
  }, [incomingCall, startMeetingSession, setActiveCall, setIncomingCall])

  const declineIncomingCall = useCallback(async () => {
    if (!incomingCall) return
    const callId = incomingCall.call_id
    setIncomingCall?.(null) // optimistic — the overlay must disappear instantly regardless of the REST outcome
    try {
      await api.declineCall(callId)
    } catch {
      // Transient network error declining a ring the user has already
      // dismissed from their own screen — nothing more this client can
      // usefully do; the other side's ring will simply time out on its
      // own schedule instead (Requirement 5.6's backstop).
    }
  }, [incomingCall, setIncomingCall])

  // ── Either side, while in an active call ────────────────────────────
  const hangUp = useCallback(async () => {
    const callId = activeCall?.call_id
    setSessionStatus('ending')
    teardownMeetingSession()
    setActiveCall?.(null)
    if (callId) {
      try {
        await api.leaveCall(callId)
      } catch {
        // The meeting/call may already be gone server-side (e.g. the
        // other party ended it a moment earlier) — this client's own
        // local teardown above already happened either way.
      }
    }
  }, [activeCall, teardownMeetingSession, setActiveCall])

  const toggleMute = useCallback(() => {
    const session = meetingSessionRef.current
    if (!session) return
    if (isMuted) {
      session.audioVideo.realtimeUnmuteLocalAudio()
    } else {
      session.audioVideo.realtimeMuteLocalAudio()
    }
    setIsMuted((prev) => !prev)
  }, [isMuted])

  const toggleVideo = useCallback(async () => {
    const session = meetingSessionRef.current
    if (!session) return
    try {
      if (isVideoOn) {
        session.audioVideo.stopLocalVideoTile()
        setIsVideoOn(false)
        setLocalTileId(null)
      } else {
        const videoInputs = await session.audioVideo.listVideoInputDevices()
        if (!videoInputs[0]) {
          setError('No camera was found on this device.')
          return
        }
        await session.audioVideo.startVideoInput(videoInputs[0].deviceId)
        session.audioVideo.startLocalVideoTile()
        // isVideoOn/localTileId are actually flipped true by the
        // videoTileDidUpdate observer above once the SDK confirms the
        // local tile — not set here optimistically, since a camera
        // permission prompt can still be denied after this call returns.
      }
    } catch {
      setError('Could not access your camera. Please check camera permissions.')
    }
  }, [isVideoOn])

  // Requirement 7.1-7.4: screen share, gated to platforms where the
  // browser's getDisplayMedia() exists at all (mobile Safari/Chrome do
  // not implement it — CallView.jsx checks this same capability before
  // even showing the button) — a denied permission prompt or an
  // unsupported platform both surface as the same friendly error rather
  // than an uncaught promise rejection breaking the call.
  const startScreenShare = useCallback(async () => {
    const session = meetingSessionRef.current
    if (!session) return
    if (!navigator.mediaDevices?.getDisplayMedia) {
      setError('Screen sharing is not supported on this device.')
      return
    }
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false })
      await session.audioVideo.startContentShare(stream)
      setIsSharingScreen(true)
    } catch {
      // Covers both "user clicked Cancel on the OS/browser share
      // picker" and any other getDisplayMedia()/startContentShare()
      // failure — Requirement 7.4's "abort gracefully, call continues
      // unaffected" — the call's own audio/video is untouched either way.
      setIsSharingScreen(false)
    }
  }, [])

  const stopScreenShare = useCallback(() => {
    const session = meetingSessionRef.current
    if (!session) return
    session.audioVideo.stopContentShare()
    setIsSharingScreen(false)
  }, [])

  // If activeCall is cleared out from under this context (e.g. another
  // of this user's own devices left the call, or the call ended server-
  // side and ChatContext's WS handler nulled it) while THIS tab still
  // thinks it holds a live MeetingSession, tear it down rather than
  // leaving an orphaned local audio/video pipeline running silently.
  useEffect(() => {
    if (!activeCall && meetingSessionRef.current) {
      teardownMeetingSession()
    }
  }, [activeCall, teardownMeetingSession])

  const value = {
    incomingCall,
    activeCall,
    sessionStatus,
    error,
    setError,
    roster,
    isMuted,
    isVideoOn,
    isSharingScreen,
    localTileId,
    meetingSession: meetingSessionRef.current,
    startCall,
    acceptIncomingCall,
    declineIncomingCall,
    hangUp,
    toggleMute,
    toggleVideo,
    startScreenShare,
    stopScreenShare,
  }

  return <CallContext.Provider value={value}>{children}</CallContext.Provider>
}

export function useCall() {
  return useContext(CallContext)
}
