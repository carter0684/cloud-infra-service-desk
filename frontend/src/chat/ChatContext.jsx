import React, { createContext, useContext, useState, useEffect, useRef, useCallback } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'

// job-portal-chat-voice Task 13.3 — establishes and maintains the single
// multiplexed WebSocket connection (see backend main.py's
// `@app.websocket("/ws")`), and exposes conversation list, active
// conversation's messages, a presence map, and incoming-call state to
// every consumer via React context.
//
// Derives the WS URL from the SAME `VITE_API_BASE_URL` constant api.js's
// own `API_BASE` uses (design.md's explicit instruction), so this never
// drifts out of sync with whichever backend host the REST calls target:
//   - VITE_API_BASE_URL="" (local dev, Vite proxy) -> same-origin,
//     scheme derived from window.location.protocol.
//   - VITE_API_BASE_URL="https://portal-api.example.com" (prod)
//     -> "wss://portal-api.example.com/ws".
const API_BASE = import.meta.env.VITE_API_BASE_URL || ''
const AUTH_MODE = import.meta.env.VITE_AUTH_MODE === 'oidc' ? 'oidc' : 'local'

function toWsUrl(base) {
  if (base) {
    // "https://host" -> "wss://host/ws", "http://host" -> "ws://host/ws"
    return base.replace(/^https/, 'wss').replace(/^http/, 'ws') + '/ws'
  }
  // Same-origin (local dev via Vite's proxy, or a same-host deployment)
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${window.location.host}/ws`
}

// 15-second heartbeat — matches ws_connections.HEARTBEAT_INTERVAL_SECONDS
// (backend) exactly, so a connection this client considers "alive" is
// refreshed well within the backend's 45-second staleness window
// (ws_connections.STALE_CONNECTION_SECONDS) even accounting for network
// jitter between beats.
const HEARTBEAT_INTERVAL_MS = 15 * 1000

// Reconnect-with-backoff schedule: starts fast (a dropped connection
// should recover almost immediately for a real network blip) and caps at
// 30 seconds so a genuinely offline client doesn't hammer the ALB.
const RECONNECT_DELAYS_MS = [1000, 2000, 5000, 10000, 15000, 30000]

// How often to re-fetch GET /api/users/directory as a presence-map
// correctness fallback. Necessary because the backend's live
// `presence.update` WS push (see main.py's websocket_route()) is
// deliberately LOCAL-ONLY — it only reaches WS connections held by the
// SAME ECS task that produced the event, since no cross-task pub/sub
// broker exists for presence changes (design.md's fan-out design only
// covers the `messages` and `call_sessions` DynamoDB Streams). Two
// Team_Members whose connections land on different tasks behind the ALB
// therefore only converge on each other's true presence via this
// periodic re-fetch, not the WS push. 30 seconds keeps that worst-case
// staleness window roughly in line with Requirement 8.2's own 30-second
// offline-detection budget.
const DIRECTORY_REFRESH_INTERVAL_MS = 30 * 1000

const ChatContext = createContext(null)

export function ChatProvider({ children }) {
  const { user, authMode: contextAuthMode } = useAuth() || {}
  // Prefer the live AuthContext's authMode when available (it's the same
  // AUTH_MODE constant, just already resolved there) — falling back to
  // this module's own constant keeps ChatProvider usable even if it's
  // ever mounted without AuthProvider as an ancestor (e.g. isolated
  // tests), matching AuthContext.jsx's own defensive style elsewhere in
  // this app.
  const authMode = contextAuthMode || AUTH_MODE

  const [connectionStatus, setConnectionStatus] = useState('disconnected') // 'connecting' | 'connected' | 'disconnected'
  const [conversations, setConversations] = useState([])
  // messagesByConversation: { [conversation_id]: Message[] }, ascending
  // chronological order (oldest first) — matching ChatPage.jsx's thread
  // rendering order (Task 14.1).
  const [messagesByConversation, setMessagesByConversation] = useState({})
  const [activeConversationId, setActiveConversationId] = useState(null)
  // presenceMap: { [email]: "online" | "in_call" | "offline" }
  const [presenceMap, setPresenceMap] = useState({})
  // incomingCall: the most recent call.update payload where the current
  // user has a "ringing" participant entry, or null once accepted/
  // declined/cancelled/timed out. IncomingCallOverlay.jsx (Task 14.4)
  // reads this to decide whether to render itself, regardless of which
  // page is currently focused (Requirement 8.6).
  const [incomingCall, setIncomingCall] = useState(null)
  // activeCall: the call the current user is currently "joined" to (if
  // any) — drives CallView.jsx (Task 15) without it needing its own WS
  // subscription.
  const [activeCall, setActiveCall] = useState(null)

  const wsRef = useRef(null)
  const heartbeatIntervalRef = useRef(null)
  const reconnectAttemptRef = useRef(0)
  const reconnectTimeoutRef = useRef(null)
  const closedByUsRef = useRef(false)
  // Mirrors activeConversationId without handleServerEvent (and
  // therefore ws.onmessage/connect) needing to depend on — and thus be
  // recreated whenever — the active conversation changes. Read-only
  // snapshot for the WS message handler; the state itself remains the
  // single source of truth for everything else (rendering, effects).
  const activeConversationIdRef = useRef(null)

  useEffect(() => {
    activeConversationIdRef.current = activeConversationId
  }, [activeConversationId])

  const userEmail = user?.email || ''

  const refreshConversations = useCallback(() => {
    if (!userEmail) return
    api.listChatConversations().then((data) => {
      setConversations(data?.conversations || [])
    }).catch(() => { /* transient — next poll/WS event will retry */ })
  }, [userEmail])

  const refreshDirectoryPresence = useCallback(() => {
    if (!userEmail) return
    api.listUsersDirectory().then((data) => {
      const next = {}
      for (const u of data?.users || []) next[u.email] = u.presence || 'offline'
      setPresenceMap((prev) => ({ ...prev, ...next }))
    }).catch(() => { /* transient — next periodic refresh will retry */ })
  }, [userEmail])

  // Requirement 2.5: mark every currently-visible Message NOT sent by
  // the caller and not yet read by the caller as read, in the given
  // Conversation. Safe to call liberally (e.g. every time the active
  // Conversation's message list changes) — the backend route
  // (POST .../messages/read) silently no-ops on already-read message_ids,
  // and this itself skips the call entirely when there's nothing new to
  // mark, so re-marking an already-read message never happens on the
  // hot path here.
  const markConversationRead = useCallback((conversationId, messages) => {
    if (!conversationId || !userEmail || !messages?.length) return
    const unreadIds = messages
      .filter((m) => m.sender_email !== userEmail && !(m.read_by || {})[userEmail])
      .map((m) => m.message_id)
    if (!unreadIds.length) return
    api.markMessagesRead(conversationId, unreadIds).catch(() => {
      // Transient — the next time this Conversation's messages change
      // (e.g. a new message arrives) this will simply retry against
      // the (still) unread set.
    })
  }, [userEmail])

  const loadMessagesForConversation = useCallback((conversationId) => {
    if (!conversationId) return
    api.listChatMessages(conversationId).then((data) => {
      const messages = data?.messages || []
      setMessagesByConversation((prev) => ({ ...prev, [conversationId]: messages }))
      markConversationRead(conversationId, messages)
    }).catch(() => { /* ChatPage.jsx surfaces its own loading-error state */ })
  }, [markConversationRead])

  // Merge one freshly-arrived/edited Message into
  // messagesByConversation, keeping ascending chronological order and
  // replacing an existing row with the same message_id (an edit) rather
  // than appending a duplicate.
  const upsertMessage = useCallback((message) => {
    const convId = message?.conversation_id
    if (!convId) return
    setMessagesByConversation((prev) => {
      const existing = prev[convId] || []
      const idx = existing.findIndex((m) => m.message_id === message.message_id)
      let next
      if (idx >= 0) {
        next = existing.slice()
        next[idx] = message
      } else {
        // chat_store's message dict uses `sent_at` (see _msg_to_dict()),
        // not `created_at` — Conversations have a `created_at` but
        // Messages don't, so sorting on the wrong field here would
        // silently fall back to comparing empty strings on every
        // message (effectively no-op, relying on array insertion order
        // alone, which breaks for an out-of-order WS delivery or a
        // reconnect resync).
        next = [...existing, message].sort((a, b) => (a.sent_at || '').localeCompare(b.sent_at || ''))
      }
      return { ...prev, [convId]: next }
    })
  }, [])

  const handleCallUpdate = useCallback((call) => {
    if (!call || !userEmail) return
    const myParticipant = (call.participants || {})[userEmail]
    if (myParticipant?.state === 'ringing' && call.status !== 'ended') {
      setIncomingCall(call)
    } else if (incomingCall?.call_id === call.call_id) {
      // This call is no longer ringing for us (accepted/declined/
      // cancelled/timed out elsewhere, or ended) — clear the overlay.
      setIncomingCall(null)
    }
    if (myParticipant?.state === 'joined' && call.status !== 'ended') {
      setActiveCall(call)
    } else if (activeCall?.call_id === call.call_id) {
      setActiveCall(null)
    }
    // A Call event is also Conversation activity — refresh the list so
    // its sort order / any future "ongoing call" badge stays current.
    refreshConversations()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userEmail, incomingCall, activeCall, refreshConversations])

  const handleServerEvent = useCallback((payload) => {
    const { event_type: eventType, data } = payload || {}
    switch (eventType) {
      case 'message.new':
      case 'message.edited':
        upsertMessage(data)
        refreshConversations() // bumps unread indicator / re-sorts by activity
        // If this Message belongs to the Conversation the user has
        // open right now, mark it read immediately rather than waiting
        // for them to navigate away and back — matches Requirement
        // 2.5's "opens a Conversation" trigger extended to "already has
        // it open when a new Message arrives".
        if (data?.conversation_id && data.conversation_id === activeConversationIdRef.current) {
          markConversationRead(data.conversation_id, [data])
        }
        break
      case 'presence.update':
        if (data?.email) {
          setPresenceMap((prev) => ({ ...prev, [data.email]: data.presence || 'offline' }))
        }
        break
      case 'call.update':
        handleCallUpdate(data)
        break
      default:
        // Unknown/forward-compatible event type — ignore rather than throw,
        // so an older client never breaks on a future backend addition.
        break
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [upsertMessage, refreshConversations, handleCallUpdate, markConversationRead])

  const clearHeartbeat = useCallback(() => {
    if (heartbeatIntervalRef.current) {
      clearInterval(heartbeatIntervalRef.current)
      heartbeatIntervalRef.current = null
    }
  }, [])

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current)
      reconnectTimeoutRef.current = null
    }
  }, [])

  const connect = useCallback(() => {
    if (!userEmail) return
    if (wsRef.current && (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING)) {
      return // already connecting/connected — avoid a duplicate socket
    }
    closedByUsRef.current = false
    setConnectionStatus('connecting')

    let url = toWsUrl(API_BASE)
    if (authMode !== 'oidc') {
      // Local dev: the browser WebSocket() constructor cannot set custom
      // request headers (unlike a normal fetch()/XHR call), so this is
      // the closest local-dev equivalent of api.js's X-Actor-Email
      // header — see main.py's _authenticate_ws() docstring. In OIDC
      // mode the ALB attaches x-amzn-oidc-data to the upgrade request
      // itself; nothing needs adding here.
      url += `?x_actor_email=${encodeURIComponent(userEmail)}`
    }

    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => {
      setConnectionStatus('connected')
      reconnectAttemptRef.current = 0
      clearHeartbeat()
      heartbeatIntervalRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send('ping')
      }, HEARTBEAT_INTERVAL_MS)
      // A fresh connection means our own previous view of the world may
      // be stale (e.g. this is a reconnect after a network blip) — pull
      // a full snapshot rather than waiting for the next WS push.
      refreshConversations()
      refreshDirectoryPresence()
    }

    ws.onmessage = (evt) => {
      try {
        const payload = JSON.parse(evt.data)
        handleServerEvent(payload)
      } catch {
        // Not JSON (shouldn't happen — the backend only ever sends the
        // {event_type, table, data} shape) — ignore rather than crash
        // the whole connection over one malformed frame.
      }
    }

    ws.onclose = () => {
      clearHeartbeat()
      setConnectionStatus('disconnected')
      wsRef.current = null
      if (closedByUsRef.current) return // deliberate close (logout/unmount) — do not reconnect
      const attempt = reconnectAttemptRef.current
      const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)]
      reconnectAttemptRef.current = attempt + 1
      clearReconnectTimer()
      reconnectTimeoutRef.current = setTimeout(connect, delay)
    }

    ws.onerror = () => {
      // onclose always fires after onerror for a WebSocket — the
      // reconnect scheduling above is sufficient; nothing extra to do
      // here beyond not letting an uncaught error surface to the console
      // as an unhandled exception.
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userEmail, authMode, handleServerEvent, refreshConversations, refreshDirectoryPresence, clearHeartbeat, clearReconnectTimer])

  const disconnect = useCallback(() => {
    closedByUsRef.current = true
    clearReconnectTimer()
    clearHeartbeat()
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    setConnectionStatus('disconnected')
  }, [clearHeartbeat, clearReconnectTimer])

  // Connect once a user is known; tear down on logout/unmount. Does NOT
  // depend on `connect` itself changing identity on every render (it's
  // memoised via useCallback above) — only on userEmail actually
  // changing (login/logout), so this never re-opens the socket on an
  // unrelated re-render.
  useEffect(() => {
    if (!userEmail) {
      disconnect()
      return
    }
    connect()
    return () => disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userEmail])

  // Initial REST snapshot as soon as we know who's logged in — the WS
  // connection's own onopen handler above also refreshes both, but that
  // can lag slightly behind the very first render (WS handshake takes a
  // moment), so this avoids a blank ChatPage.jsx on first load.
  useEffect(() => {
    if (!userEmail) return
    refreshConversations()
    refreshDirectoryPresence()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userEmail])

  // Periodic presence re-fetch fallback — see DIRECTORY_REFRESH_INTERVAL_MS's
  // comment above for why the WS push alone is not sufficient.
  useEffect(() => {
    if (!userEmail) return
    const intervalId = setInterval(refreshDirectoryPresence, DIRECTORY_REFRESH_INTERVAL_MS)
    return () => clearInterval(intervalId)
  }, [userEmail, refreshDirectoryPresence])

  // Lazily load a conversation's message history the first time it
  // becomes the active one (not eagerly for every conversation up
  // front) — subsequent updates arrive via the WS push (upsertMessage).
  useEffect(() => {
    if (activeConversationId && !messagesByConversation[activeConversationId]) {
      loadMessagesForConversation(activeConversationId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeConversationId])

  const value = {
    connectionStatus,
    conversations,
    refreshConversations,
    activeConversationId,
    setActiveConversationId,
    messages: messagesByConversation[activeConversationId] || [],
    messagesByConversation,
    loadMessagesForConversation,
    markConversationRead,
    presenceMap,
    incomingCall,
    setIncomingCall,
    activeCall,
    setActiveCall,
  }

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>
}

export function useChat() {
  return useContext(ChatContext)
}
