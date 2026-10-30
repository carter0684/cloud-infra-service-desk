import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react'
import dayjs from 'dayjs'
import { COLORS, CARD } from '../utils/theme.js'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'
import { useChat } from './ChatContext.jsx'
import { useCall } from './CallContext.jsx'
import Avatar from '../components/Avatar.jsx'
import AttachmentUpload from './AttachmentUpload.jsx'

// job-portal-chat-voice Task 14.1-14.3 — the "Chat" sidebar tab's main
// page: a conversation list (left) + the active conversation's message
// thread + composer (right). All real-time state (conversations,
// messages, presence) comes from ChatContext (Task 13.3); this
// component is purely presentational/interactive on top of it, plus the
// REST calls that only this page needs (create conversation, edit,
// send, presign+upload via AttachmentUpload.jsx).
//
// Requirement 3.1/3.4-3.7: the 10-minute Edit_Window is SERVER-enforced
// (chat_store.edit_message() rejects an edit past 10 minutes using
// server time) — the countdown/disabled affordance here is a client-side
// UX convenience only, computed from each message's own `sent_at`
// (dayjs), and can be slightly wrong if the client's clock is skewed;
// that's acceptable since the backend is the actual authority and will
// reject a late edit attempt regardless of what this UI showed.
const EDIT_WINDOW_MINUTES = 10

// Every backend timestamp here (Message.sent_at/edited_at) is produced
// by Python's datetime.utcnow().isoformat() — a NAIVE string with no
// "Z"/offset suffix (e.g. "2026-08-21T10:00:00.123456"). Both
// dayjs(iso) and the native Date constructor parse a string like that
// as LOCAL time, not UTC, which would silently shift every displayed
// timestamp by the browser's UTC offset (+8h in Hong Kong). Appending
// "Z" (if not already present) tells the parser to treat it as UTC,
// then dayjs's own default local-timezone rendering on .format() below
// converts it to the viewer's local time correctly — no dayjs plugin
// needed (dayjs.utc()/timezone plugins are NOT in this app's pinned
// dependency set — see package.json's exact-pinned dayjs entry).
function toLocalDayjs(iso) {
  if (!iso) return null
  const withZ = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`
  return dayjs(withZ)
}

function formatTimestamp(iso) {
  const d = toLocalDayjs(iso)
  if (!d) return ''
  const today = dayjs()
  if (d.isSame(today, 'day')) return d.format('HH:mm')
  if (d.isSame(today.subtract(1, 'day'), 'day')) return `Yesterday ${d.format('HH:mm')}`
  return d.format('DD MMM HH:mm')
}

// Requirement 3.4: minutes remaining in the sender's own Edit_Window for
// one message, floored at 0. Uses plain UTC-naive comparison — sent_at
// is already an ISO UTC string from the backend (datetime.utcnow()),
// and dayjs() parses/compares as local-vs-that-string consistently as
// long as neither side is coerced to a different zone, matching how the
// rest of this app already treats backend timestamps (see time.js's
// nowHK() being a display-only concern, never used for this kind of
// deadline math elsewhere in the codebase).
function editMinutesRemaining(sentAtIso) {
  // Uses toLocalDayjs() (see its comment above) so this deadline is
  // computed against the message's REAL UTC sent_at — getting this
  // wrong in Hong Kong's +8h offset would make the countdown show ~8
  // hours of extra remaining time that the server-side check (the real
  // authority, chat_store.py's EditWindowExpiredError) would already
  // have rejected, i.e. an edit button that looks usable but 400s.
  const sentAt = toLocalDayjs(sentAtIso)
  if (!sentAt) return 0
  const deadline = sentAt.add(EDIT_WINDOW_MINUTES, 'minute')
  const remaining = deadline.diff(dayjs(), 'minute', true)
  return Math.max(0, remaining)
}

function conversationDisplayName(conv, myEmail, usersByEmail) {
  if (conv.type === 'group') return conv.name || 'Group Chat'
  const other = (conv.participants || []).find((e) => e !== myEmail)
  return usersByEmail[other]?.name || other || 'Direct Message'
}

function presenceDot(presence) {
  const color = presence === 'online' ? '#16a34a' : presence === 'in_call' ? '#d97706' : COLORS.textMuted
  return (
    <span style={{
      display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
      background: color, flexShrink: 0,
    }} />
  )
}

// ── New Conversation modal (Task 14.2) ──────────────────────────────────────
function NewConversationModal({ directory, myEmail, onClose, onCreated }) {
  const [mode, setMode] = useState('direct') // 'direct' | 'group'
  const [selected, setSelected] = useState([]) // email[]
  const [groupName, setGroupName] = useState('')
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)

  const candidates = useMemo(() => {
    const term = search.trim().toLowerCase()
    return directory
      .filter((u) => u.email !== myEmail)
      .filter((u) => !term || u.name.toLowerCase().includes(term) || u.email.toLowerCase().includes(term))
  }, [directory, myEmail, search])

  const toggle = (email) => {
    setError('')
    if (mode === 'direct') {
      setSelected([email])
      return
    }
    setSelected((prev) => (prev.includes(email) ? prev.filter((e) => e !== email) : [...prev, email]))
  }

  const submit = async () => {
    setError('')
    if (mode === 'direct') {
      if (selected.length !== 1) { setError('Pick one person to message.'); return }
      setCreating(true)
      try {
        // Requirement 1.2: the backend itself reopens an existing Direct_
        // Conversation instead of duplicating — this modal doesn't need
        // any special "already exists" handling, it just always calls
        // create and trusts whichever conversation_id comes back.
        const conv = await api.createDirectConversation(selected[0])
        onCreated(conv)
      } catch (e) {
        setError(e?.response?.data?.detail || 'Could not start the conversation.')
      } finally {
        setCreating(false)
      }
      return
    }
    // group
    if (!groupName.trim()) { setError('Give the group a name.'); return }
    if (selected.length < 2) { setError('A group needs at least two other people.'); return }
    setCreating(true)
    try {
      const conv = await api.createGroupConversation(groupName.trim(), selected)
      onCreated(conv)
    } catch (e) {
      setError(e?.response?.data?.detail || 'Could not create the group.')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', zIndex: 1000,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }} onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        style={{ ...CARD, width: 420, maxHeight: '80vh', display: 'flex', flexDirection: 'column', padding: 0, overflow: 'hidden' }}
      >
        <div style={{ padding: '14px 18px', borderBottom: `1px solid ${COLORS.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <h3 style={{ margin: 0, fontSize: 15, color: COLORS.textPrimary }}>New Conversation</h3>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 18, cursor: 'pointer', color: COLORS.textMuted }}>✕</button>
        </div>

        <div style={{ display: 'flex', gap: 8, padding: '10px 18px 0' }}>
          {['direct', 'group'].map((m) => (
            <button key={m} onClick={() => { setMode(m); setSelected([]); setError('') }}
              style={{
                flex: 1, padding: '7px 0', borderRadius: 7, fontSize: 12.5, cursor: 'pointer',
                border: `1px solid ${mode === m ? COLORS.brand : COLORS.border}`,
                background: mode === m ? COLORS.brandLight : COLORS.pageBg,
                color: mode === m ? COLORS.brand : COLORS.textSecondary,
              }}>
              {m === 'direct' ? 'Direct Message' : 'Group Chat'}
            </button>
          ))}
        </div>

        {mode === 'group' && (
          <div style={{ padding: '12px 18px 0' }}>
            <input
              value={groupName}
              onChange={(e) => setGroupName(e.target.value)}
              placeholder="Group name (max 100 characters)"
              maxLength={100}
              style={{ width: '100%', padding: '8px 10px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13 }}
            />
          </div>
        )}

        <div style={{ padding: '12px 18px 8px' }}>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search people…"
            style={{ width: '100%', padding: '8px 10px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13 }}
          />
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: '0 10px 10px' }}>
          {candidates.map((u) => {
            const isSelected = selected.includes(u.email)
            return (
              <div key={u.email} onClick={() => toggle(u.email)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px', borderRadius: 8,
                  cursor: 'pointer', background: isSelected ? COLORS.brandLight : 'transparent',
                }}>
                <Avatar photoUrl={u.photo_data_url} name={u.name} email={u.email} size={30} fontSize={13} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, color: COLORS.textPrimary, fontWeight: isSelected ? 700 : 400 }}>{u.name}</div>
                  <div style={{ fontSize: 11, color: COLORS.textMuted, display: 'flex', alignItems: 'center', gap: 5 }}>
                    {presenceDot(u.presence)} {u.presence === 'in_call' ? 'In a call' : u.presence === 'online' ? 'Online' : 'Offline'}
                  </div>
                </div>
                {isSelected && <span style={{ color: COLORS.brand, fontSize: 15 }}>✓</span>}
              </div>
            )
          })}
          {candidates.length === 0 && (
            <div style={{ padding: 20, textAlign: 'center', color: COLORS.textMuted, fontSize: 12.5 }}>No matching people.</div>
          )}
        </div>

        {error && (
          <div style={{ padding: '0 18px 8px', fontSize: 12, color: COLORS.danger }}>⚠️ {error}</div>
        )}

        <div style={{ padding: '10px 18px 16px', borderTop: `1px solid ${COLORS.border}` }}>
          <button onClick={submit} disabled={creating}
            style={{
              width: '100%', padding: '9px 0', borderRadius: 8, border: 'none',
              background: creating ? COLORS.textMuted : COLORS.brand, color: '#fff',
              fontSize: 13.5, fontWeight: 700, cursor: creating ? 'default' : 'pointer',
            }}>
            {creating ? 'Creating…' : mode === 'direct' ? 'Start Chat' : 'Create Group'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Conversation list (left column) ─────────────────────────────────────────
function ConversationList({ conversations, activeConversationId, onSelect, myEmail, usersByEmail, presenceMap, onNewConversation }) {
  return (
    <div style={{ width: 300, flexShrink: 0, borderRight: `1px solid ${COLORS.border}`, display: 'flex', flexDirection: 'column' }}>
      <div style={{ padding: '14px 16px', borderBottom: `1px solid ${COLORS.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0, fontSize: 15, color: COLORS.textPrimary }}>💬 Chat</h3>
        <button onClick={onNewConversation} title="New conversation"
          style={{
            width: 30, height: 30, borderRadius: 8, border: 'none', background: COLORS.brand,
            color: '#fff', fontSize: 16, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
          +
        </button>
      </div>
      <div style={{ flex: 1, overflowY: 'auto' }}>
        {conversations.length === 0 && (
          <div style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted, fontSize: 12.5 }}>
            No conversations yet. Click + to start one.
          </div>
        )}
        {conversations.map((conv) => {
          const displayName = conversationDisplayName(conv, myEmail, usersByEmail)
          const isGroup = conv.type === 'group'
          const otherEmail = !isGroup ? (conv.participants || []).find((e) => e !== myEmail) : null
          const presence = otherEmail ? presenceMap[otherEmail] : null
          const isActive = conv.conversation_id === activeConversationId
          // Requirement 1.4/1.5-style unread badge: derived client-side
          // from whatever's already loaded for this conversation (Task
          // 13.3's messagesByConversation) — a conversation not yet
          // opened this session simply shows no badge rather than
          // fetching its full history just to compute one, matching
          // ChatContext.jsx's own lazy-load-on-open design.
          return (
            <div key={conv.conversation_id} onClick={() => onSelect(conv.conversation_id)}
              style={{
                display: 'flex', alignItems: 'center', gap: 10, padding: '10px 16px', cursor: 'pointer',
                background: isActive ? COLORS.brandLight : 'transparent',
                borderBottom: `1px solid ${COLORS.border}`,
              }}>
              {isGroup ? (
                <div style={{
                  width: 34, height: 34, borderRadius: '50%', background: COLORS.brand, color: '#fff',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 15, flexShrink: 0,
                }}>👥</div>
              ) : (
                <Avatar photoUrl={usersByEmail[otherEmail]?.photo_data_url} name={displayName} email={otherEmail} size={34} fontSize={14} />
              )}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 13.5, color: COLORS.textPrimary, fontWeight: isActive ? 700 : 500 }}>
                  {!isGroup && presenceDot(presence)}
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{displayName}</span>
                </div>
                <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 2 }}>
                  {isGroup ? `${(conv.participants || []).length} members` : (presence === 'in_call' ? 'In a call' : presence === 'online' ? 'Online' : 'Offline')}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── One message row in the thread ───────────────────────────────────────────
function MessageRow({ message, isMine, senderInfo, onEditRequested }) {
  const [showActions, setShowActions] = useState(false)
  const [attachmentUrl, setAttachmentUrl] = useState(null)

  const att = message.attachment
  const isPhoto = att?.is_photo
  const minutesLeft = isMine ? editMinutesRemaining(message.sent_at) : 0
  const canEdit = isMine && minutesLeft > 0
  // Requirement 2.5 read-receipt: shown only on the sender's own
  // outgoing messages — read_by is a {email: read_at_iso} map, so
  // "read" means at least one OTHER participant besides the sender has
  // a key in it (covers both Direct_Conversation and Group_Conversation
  // without needing to special-case either).
  const readByOthers = Object.keys(message.read_by || {}).filter((e) => e !== message.sender_email)
  // A message is a real content edit only if edited_at is actually set
  // — ws_manager.py tags BOTH a real edit AND a read-receipt-only
  // update as "message.edited" over the WS (see main.py's
  // mark_chat_messages_read() docstring), so this field (not the WS
  // event_type label) is what actually distinguishes the two here.
  const wasEdited = !!message.edited_at

  useEffect(() => {
    if (!att?.s3_key || !isPhoto) return
    let cancelled = false
    api.getChatAttachmentUrl(att.s3_key, message.conversation_id).then((res) => {
      if (!cancelled) setAttachmentUrl(res?.url || null)
    }).catch(() => { /* thumbnail just won't render; download link still works via on-demand fetch */ })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [att?.s3_key])

  const handleDownload = async () => {
    try {
      const res = await api.getChatAttachmentUrl(att.s3_key, message.conversation_id)
      window.open(res.url, '_blank', 'noopener')
    } catch { /* transient network error — user can just click again */ }
  }

  return (
    <div
      onMouseEnter={() => setShowActions(true)}
      onMouseLeave={() => setShowActions(false)}
      style={{ display: 'flex', alignItems: 'flex-end', gap: 8, justifyContent: isMine ? 'flex-end' : 'flex-start' }}>
      {!isMine && (
        <Avatar photoUrl={senderInfo?.photo_data_url} name={senderInfo?.name} email={message.sender_email} size={28} fontSize={12} />
      )}
      <div style={{ maxWidth: '68%', position: 'relative' }}>
        {!isMine && (
          <div style={{ fontSize: 11, color: COLORS.textMuted, marginBottom: 2 }}>{senderInfo?.name || message.sender_email}</div>
        )}
        <div style={{
          background: isMine ? COLORS.brand : COLORS.pageBg,
          color: isMine ? '#fff' : COLORS.textPrimary,
          padding: '9px 13px', borderRadius: isMine ? '12px 12px 3px 12px' : '12px 12px 12px 3px',
          fontSize: 13.5, lineHeight: 1.55, wordBreak: 'break-word',
        }}>
          {message.content && <div style={{ whiteSpace: 'pre-wrap' }}>{message.content}</div>}

          {att && isPhoto && (
            attachmentUrl
              ? <img src={attachmentUrl} alt={att.filename} onClick={handleDownload}
                  style={{ maxWidth: 220, maxHeight: 220, borderRadius: 8, marginTop: message.content ? 6 : 0, cursor: 'pointer', display: 'block' }} />
              : <div style={{ marginTop: message.content ? 6 : 0, fontSize: 12, opacity: 0.8 }}>⏳ Loading photo…</div>
          )}
          {att && !isPhoto && (
            <div onClick={handleDownload}
              style={{
                display: 'flex', alignItems: 'center', gap: 8, marginTop: message.content ? 6 : 0,
                padding: '8px 10px', borderRadius: 7, cursor: 'pointer',
                background: isMine ? 'rgba(255,255,255,0.15)' : '#fff', border: `1px solid ${isMine ? 'rgba(255,255,255,0.25)' : COLORS.border}`,
              }}>
              <span style={{ fontSize: 16 }}>📄</span>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{att.filename}</div>
                <div style={{ fontSize: 10.5, opacity: 0.75 }}>{(att.size_bytes / 1024 / 1024).toFixed(2)} MB · Download</div>
              </div>
            </div>
          )}
        </div>

        <div style={{
          display: 'flex', alignItems: 'center', gap: 6, marginTop: 3, fontSize: 10.5, color: COLORS.textMuted,
          justifyContent: isMine ? 'flex-end' : 'flex-start',
        }}>
          <span>{formatTimestamp(message.sent_at)}</span>
          {wasEdited && <span>· edited</span>}
          {isMine && readByOthers.length > 0 && <span title={`Read by ${readByOthers.join(', ')}`}>· ✓ Read</span>}
          {isMine && showActions && canEdit && (
            <button onClick={() => onEditRequested(message)}
              style={{ border: 'none', background: 'none', color: COLORS.brand, cursor: 'pointer', fontSize: 10.5, padding: 0 }}>
              · Edit ({Math.ceil(minutesLeft)}m left)
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Composer (Task 14.3) ─────────────────────────────────────────────────────
function Composer({ conversationId, editingMessage, onCancelEdit, onSent }) {
  const [text, setText] = useState('')
  const [pendingAttachment, setPendingAttachment] = useState(null)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const textareaRef = useRef(null)

  useEffect(() => {
    if (editingMessage) {
      setText(editingMessage.content || '')
      textareaRef.current?.focus()
    }
  }, [editingMessage])

  const submit = async () => {
    setError('')
    const trimmed = text.trim()
    if (!trimmed && !pendingAttachment && !editingMessage) return
    setSending(true)
    try {
      if (editingMessage) {
        if (!trimmed) { setError('Message cannot be empty.'); setSending(false); return }
        await api.editChatMessage(editingMessage.message_id, conversationId, trimmed)
        onCancelEdit()
      } else {
        await api.sendChatMessage(conversationId, trimmed || null, pendingAttachment || null)
      }
      setText('')
      setPendingAttachment(null)
      onSent?.()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Could not send. Please try again.')
    } finally {
      setSending(false)
    }
  }

  const handleKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() }
    if (e.key === 'Escape' && editingMessage) onCancelEdit()
  }

  return (
    <div style={{ padding: '10px 16px', borderTop: `1px solid ${COLORS.border}`, background: COLORS.cardBg }}>
      {editingMessage && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6, fontSize: 11.5, color: COLORS.brand }}>
          <span>✏️ Editing message — Enter to save, Esc to cancel</span>
          <button onClick={onCancelEdit} style={{ border: 'none', background: 'none', color: COLORS.textMuted, cursor: 'pointer', fontSize: 11 }}>Cancel</button>
        </div>
      )}
      {pendingAttachment && !editingMessage && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, fontSize: 11.5, color: COLORS.textSecondary }}>
          <span>📎 {pendingAttachment.filename} ({(pendingAttachment.size_bytes / 1024 / 1024).toFixed(2)} MB)</span>
          <button onClick={() => setPendingAttachment(null)} style={{ border: 'none', background: 'none', color: COLORS.danger, cursor: 'pointer', fontSize: 11 }}>Remove</button>
        </div>
      )}
      {error && <div style={{ fontSize: 11.5, color: COLORS.danger, marginBottom: 6 }}>⚠️ {error}</div>}
      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', position: 'relative' }}>
        {!editingMessage && (
          <AttachmentUpload conversationId={conversationId} disabled={sending} onUploaded={setPendingAttachment} />
        )}
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKey}
          placeholder={editingMessage ? 'Edit your message…' : 'Type a message… (Enter to send, Shift+Enter for new line)'}
          maxLength={5000}
          rows={1}
          style={{
            flex: 1, padding: '9px 12px', borderRadius: 8, border: `1px solid ${COLORS.border}`,
            fontSize: 13.5, resize: 'none', fontFamily: 'inherit', lineHeight: 1.4, maxHeight: 120,
          }}
        />
        <button onClick={submit} disabled={sending || (!text.trim() && !pendingAttachment)}
          style={{
            padding: '9px 16px', borderRadius: 8, border: 'none',
            background: sending || (!text.trim() && !pendingAttachment) ? COLORS.textMuted : COLORS.brand,
            color: '#fff', fontWeight: 700, fontSize: 15,
            cursor: sending || (!text.trim() && !pendingAttachment) ? 'default' : 'pointer',
            alignSelf: 'stretch',
          }}>
          {editingMessage ? '✓' : '➤'}
        </button>
      </div>
    </div>
  )
}

// ── Call History modal (Task 16.1) ───────────────────────────────────────────
// Requirement 11.3/11.4: GET /api/chat/conversations/{id}/calls already
// scopes results server-side to (a) this Team_Member being a current
// Conversation participant (main.py's _require_conversation_participant,
// checked before this route even calls call_store.list_call_history())
// and (b) each individual returned call record having actually included
// this requester as caller or an invited participant
// (call_store.list_call_history()'s own docstring) — this component
// trusts the response as-is with no extra client-side filtering, and
// simply renders whatever comes back in the order the backend already
// returns it (most-recent-first).
const CALL_OUTCOME_LABEL = {
  joined: 'Joined',
  declined: 'Declined',
  timed_out: 'No answer',
  left: 'Left',
  ringing: 'Never answered', // call ended (e.g. caller cancelled) while this entry was still ringing
}

function formatCallDuration(startedAtIso, endedAtIso) {
  const start = toLocalDayjs(startedAtIso)
  const end = toLocalDayjs(endedAtIso)
  if (!start || !end) return '—'
  const totalSeconds = Math.max(0, end.diff(start, 'second'))
  const mins = Math.floor(totalSeconds / 60)
  const secs = totalSeconds % 60
  return `${mins}:${String(secs).padStart(2, '0')}`
}

function CallHistoryModal({ conversation, myEmail, usersByEmail, onClose }) {
  const [calls, setCalls] = useState(null) // null = loading
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    api.listConversationCallHistory(conversation.conversation_id)
      .then((data) => { if (!cancelled) setCalls(data?.calls || []) })
      .catch((e) => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not load call history.') })
    return () => { cancelled = true }
  }, [conversation.conversation_id])

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', zIndex: 1000,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }} onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        style={{ ...CARD, width: 460, maxHeight: '80vh', display: 'flex', flexDirection: 'column', padding: 0, overflow: 'hidden' }}
      >
        <div style={{ padding: '14px 18px', borderBottom: `1px solid ${COLORS.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <h3 style={{ margin: 0, fontSize: 15, color: COLORS.textPrimary }}>📜 Call History</h3>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 18, cursor: 'pointer', color: COLORS.textMuted }}>✕</button>
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: '10px 18px 16px' }}>
          {calls === null && !error && (
            <div style={{ padding: 20, textAlign: 'center', color: COLORS.textMuted, fontSize: 12.5 }}>Loading…</div>
          )}
          {error && (
            <div style={{ padding: '8px 0', fontSize: 12, color: COLORS.danger }}>⚠️ {error}</div>
          )}
          {calls && calls.length === 0 && (
            <div style={{ padding: 20, textAlign: 'center', color: COLORS.textMuted, fontSize: 12.5 }}>No calls yet in this conversation.</div>
          )}
          {calls && calls.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {calls.map((call) => {
                const callerName = call.caller_email === myEmail ? 'You' : (usersByEmail[call.caller_email]?.name || call.caller_email)
                const ended = call.status === 'ended'
                const participantEntries = Object.entries(call.participants || {})
                return (
                  <div key={call.call_id} style={{ border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: '10px 12px' }}>
                    <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8 }}>
                      <div style={{ fontSize: 13, fontWeight: 700, color: COLORS.textPrimary }}>
                        {call.type === 'group' ? '👥 ' : '📞 '}{callerName} called
                      </div>
                      <div style={{ fontSize: 11.5, color: COLORS.textMuted, flexShrink: 0 }}>{formatTimestamp(call.started_at)}</div>
                    </div>
                    <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginTop: 2 }}>
                      {ended ? `Duration: ${formatCallDuration(call.started_at, call.ended_at)}` : 'In progress'}
                    </div>
                    {participantEntries.length > 0 && (
                      <div style={{ marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                        {participantEntries.map(([email, entry]) => (
                          <span key={email} style={{
                            fontSize: 11, padding: '2px 7px', borderRadius: 6,
                            background: COLORS.pageBg, color: COLORS.textSecondary,
                          }}>
                            {email === myEmail ? 'You' : (usersByEmail[email]?.name || email)}: {CALL_OUTCOME_LABEL[entry.state] || entry.state}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Thread view (right column) ──────────────────────────────────────────────
function ThreadView({ conversation, messages, myEmail, usersByEmail }) {
  const [editingMessage, setEditingMessage] = useState(null)
  const [showCallHistory, setShowCallHistory] = useState(false)
  const bottomRef = useRef(null)
  const call = useCall()

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length, conversation?.conversation_id])

  // Close a stale Call History modal if the Team_Member switches to a
  // different Conversation while it's open — otherwise it would keep
  // showing the PREVIOUS conversation's call records under the new
  // header, since the modal only re-fetches on conversation_id change,
  // not on open/close.
  useEffect(() => { setShowCallHistory(false) }, [conversation?.conversation_id])

  if (!conversation) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: COLORS.textMuted, fontSize: 13.5 }}>
        Select a conversation, or start a new one.
      </div>
    )
  }

  const displayName = conversationDisplayName(conversation, myEmail, usersByEmail)
  // Requirement 5.1/6.1: "Call"/"Start Group Call" — disabled while
  // there's already a live call for this user (any conversation) so a
  // second POST /api/calls is never fired accidentally on top of one
  // already ringing/active; CallContext.jsx's startCall() itself has no
  // such guard (it's a pure REST+SDK action), so this button is the one
  // place that check needs to live.
  const alreadyInACall = !!call?.activeCall || !!call?.incomingCall
  const handleStartCall = () => {
    if (alreadyInACall) return
    call?.startCall(conversation.conversation_id).catch(() => { /* CallContext.jsx surfaces its own error state */ })
  }

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
      <div style={{ padding: '13px 18px', borderBottom: `1px solid ${COLORS.border}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 700, color: COLORS.textPrimary }}>
            {conversation.type === 'group' ? '👥 ' : ''}{displayName}
          </div>
          {conversation.type === 'group' && (
            <div style={{ fontSize: 11, fontWeight: 400, color: COLORS.textMuted, marginTop: 2 }}>
              {(conversation.participants || []).map((e) => usersByEmail[e]?.name || e).join(', ')}
            </div>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          <button onClick={() => setShowCallHistory(true)} title="Call History"
            style={{
              border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: '6px 12px', fontSize: 12.5, fontWeight: 600,
              background: COLORS.pageBg, color: COLORS.textSecondary, cursor: 'pointer',
            }}>
            📜 History
          </button>
          <button onClick={handleStartCall} disabled={alreadyInACall}
            title={alreadyInACall ? 'You are already in a call' : (conversation.type === 'group' ? 'Start Group Call' : 'Call')}
            style={{
              border: 'none', borderRadius: 8, padding: '7px 14px', fontSize: 12.5, fontWeight: 700,
              background: alreadyInACall ? COLORS.textMuted : COLORS.success, color: '#fff',
              cursor: alreadyInACall ? 'default' : 'pointer',
            }}>
            📞 {conversation.type === 'group' ? 'Start Group Call' : 'Call'}
          </button>
        </div>
      </div>

      {showCallHistory && (
        <CallHistoryModal
          conversation={conversation}
          myEmail={myEmail}
          usersByEmail={usersByEmail}
          onClose={() => setShowCallHistory(false)}
        />
      )}

      <div style={{ flex: 1, overflowY: 'auto', padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: 12 }}>
        {messages.map((m) => (
          <MessageRow
            key={m.message_id}
            message={m}
            isMine={m.sender_email === myEmail}
            senderInfo={usersByEmail[m.sender_email]}
            onEditRequested={setEditingMessage}
          />
        ))}
        <div ref={bottomRef} />
      </div>

      <Composer
        conversationId={conversation.conversation_id}
        editingMessage={editingMessage}
        onCancelEdit={() => setEditingMessage(null)}
        onSent={() => setEditingMessage(null)}
      />
    </div>
  )
}

// ── Page entry point ─────────────────────────────────────────────────────────
export default function ChatPage() {
  const { user } = useAuth()
  const {
    conversations, activeConversationId, setActiveConversationId,
    messages, presenceMap, refreshConversations,
  } = useChat() || {}
  const [directory, setDirectory] = useState([])
  const [showNewConversation, setShowNewConversation] = useState(false)

  const loadDirectory = useCallback(() => {
    api.listUsersDirectory().then((data) => setDirectory(data?.users || [])).catch(() => { /* directory just won't populate; conversations still work */ })
  }, [])

  useEffect(() => { loadDirectory() }, [loadDirectory])

  const usersByEmail = useMemo(() => {
    const map = {}
    for (const u of directory) map[u.email] = u
    return map
  }, [directory])

  const activeConversation = useMemo(
    () => (conversations || []).find((c) => c.conversation_id === activeConversationId) || null,
    [conversations, activeConversationId],
  )

  const handleCreated = (conv) => {
    setShowNewConversation(false)
    refreshConversations?.()
    setActiveConversationId?.(conv.conversation_id)
  }

  if (!user) return null

  return (
    <div style={{ padding: 24 }}>
      <div style={{ ...CARD, display: 'flex', height: 'calc(100vh - 130px)', overflow: 'hidden', padding: 0 }}>
        <ConversationList
          conversations={conversations || []}
          activeConversationId={activeConversationId}
          onSelect={setActiveConversationId}
          myEmail={user.email}
          usersByEmail={usersByEmail}
          presenceMap={presenceMap || {}}
          onNewConversation={() => setShowNewConversation(true)}
        />
        <ThreadView
          conversation={activeConversation}
          messages={messages || []}
          myEmail={user.email}
          usersByEmail={usersByEmail}
        />
      </div>

      {showNewConversation && (
        <NewConversationModal
          directory={directory}
          myEmail={user.email}
          onClose={() => setShowNewConversation(false)}
          onCreated={handleCreated}
        />
      )}
    </div>
  )
}
