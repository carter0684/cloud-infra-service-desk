import React, { useState, useRef, useEffect } from 'react'
import { COLORS, CARD } from '../utils/theme.js'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'
import Avatar from '../components/Avatar.jsx'

// ── "AWS DevOps Agent" sidebar tab ──────────────────────────────────────────
// Forwards questions to a dedicated AWS DevOps Agent Agent Space
// (100000000001, us-east-1: "testing-pj-devops-agent") via the A2A v1.0
// remote server — POST /api/devops-agent/chat (see app/devops_agent.py).
//
// Unlike the AI Assistant tab, this agent CAN see real AWS resource state
// in its associated accounts (investigations, cost, topology, CloudWatch/
// X-Ray) — gated behind the "devops_agent" tab_id so an admin can restrict
// it to the Cloud Infra team via Admin > Tab Access Control.
//
// No conversation history is sent per request: the Agent Space keeps its
// own server-side chat/task history tied to the access token, so each
// message here is effectively a fresh turn against that same underlying
// chat context on AWS's side — this page just displays the running
// transcript locally for the user's convenience.
//
// Async task model (2026-09-14 redesign): the "investigate" skill can
// take 5-8 minutes, and even "chat"-skill answers can occasionally take
// 30-60+ seconds — too long for a single blocking HTTP request/ALB
// connection to hold open reliably (this caused a production "Network
// Error", see api.js's comment). Both api.devopsAgentChat() and
// api.getDevOpsAgentTaskStatus() are now always fast individually;
// send() below drives a client-side poll loop against the latter until
// the task completes.

const SUGGESTIONS = [
  'What skills do you support?',
  'Analyze cost optimization opportunities for this AWS account.',
  'Review my architecture for production readiness.',
  'What runbooks do you have for troubleshooting?',
]

// How often to re-check an in-progress task. "investigate"-skill runs are
// documented as taking 5-8 minutes, so this is deliberately relaxed —
// no need to hammer the backend every second for something that slow.
const POLL_INTERVAL_MS = 6000

export default function DevOpsAgentPage() {
  const { user } = useAuth()
  const [messages, setMessages] = useState([
    {
      role: 'assistant',
      content: "Hi! I'm the AWS DevOps Agent — ask me about investigations, cost optimization, architecture review, or your AWS infrastructure. Complex investigations can take a few minutes to complete.",
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef()
  const inputRef = useRef()
  // Tracks the pending setTimeout for the active poll loop, if any, so it
  // can be cancelled on unmount (e.g. user navigates away mid-investigation).
  const pollTimeoutRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  // Stop any in-flight poll loop. Safe to call even if none is active.
  const clearPoll = () => {
    if (pollTimeoutRef.current) {
      clearTimeout(pollTimeoutRef.current)
      pollTimeoutRef.current = null
    }
  }

  useEffect(() => clearPoll, []) // cancel any pending poll on unmount

  const describeError = (e) =>
    e?.response?.data?.detail || e?.message || 'AWS DevOps Agent is unavailable. Please try again.'

  // Replaces the trailing placeholder/loading message with a final
  // (non-loading) message and stops the loading state.
  const finish = (content) => {
    setMessages(prev => [...prev.slice(0, -1), { role: 'assistant', content }])
    setLoading(false)
  }

  // Poll GET /api/devops-agent/tasks/{taskId} on a timer until it reports
  // done=true (or errors out). Keeps the trailing message in a "loading"
  // state with an "Investigating…" indicator the whole time.
  const pollTask = (taskId) => {
    const tick = async () => {
      try {
        const res = await api.getDevOpsAgentTaskStatus(taskId)
        if (res.done) {
          finish(res.answer)
          return
        }
      } catch (e) {
        finish('⚠️ ' + describeError(e))
        return
      }
      pollTimeoutRef.current = setTimeout(tick, POLL_INTERVAL_MS)
    }
    pollTimeoutRef.current = setTimeout(tick, POLL_INTERVAL_MS)
  }

  const send = async (text) => {
    const msg = (text || input).trim()
    if (!msg || loading) return
    setInput('')
    clearPoll() // defensive — shouldn't be one active while loading=false

    setMessages(prev => [...prev, { role: 'user', content: msg }, { role: 'assistant', content: '⏳ Thinking…', loading: true }])
    setLoading(true)

    try {
      const res = await api.devopsAgentChat(msg)
      if (res.done) {
        finish(res.answer)
      } else {
        // Long-running task (typically "investigate") — keep the loading
        // indicator up and start polling for the real answer instead of
        // clearing `loading` here.
        setMessages(prev => [
          ...prev.slice(0, -1),
          { role: 'assistant', content: '⏳ Investigating… this can take a few minutes. I\'ll show the result here once it\'s ready.', loading: true },
        ])
        pollTask(res.task_id)
      }
    } catch (e) {
      finish('⚠️ ' + describeError(e))
    }
  }

  const handleKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
  }

  const clearChat = () => {
    clearPoll()
    setLoading(false)
    setMessages([{ role: 'assistant', content: 'Chat cleared. What can I help you with?' }])
  }

  return (
    <div style={{ padding: 24, maxWidth: 860, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 19, color: COLORS.textPrimary }}>☁️ AWS DevOps Agent</h2>
          <div style={{ fontSize: 12.5, color: COLORS.textSecondary, marginTop: 2 }}>
            Investigations, cost optimization, architecture review, and topology mapping for AWS infrastructure.
          </div>
        </div>
        <button onClick={clearChat}
          style={{
            background: COLORS.pageBg, color: COLORS.textSecondary, border: `1px solid ${COLORS.border}`,
            borderRadius: 6, padding: '6px 12px', fontSize: 12, cursor: 'pointer',
          }}>
          Clear chat
        </button>
      </div>

      <div style={{ ...CARD, display: 'flex', flexDirection: 'column', height: 'calc(100vh - 190px)', overflow: 'hidden' }}>
        {/* Messages */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          {messages.map((m, i) => (
            <div key={i} style={{
              display: 'flex', alignItems: 'flex-end', gap: 8,
              justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start',
            }}>
              {m.role === 'user' && (
                <Avatar photoUrl={user?.photo_data_url} name={user?.name} email={user?.email} size={30} fontSize={13} />
              )}
              {m.role === 'assistant' && (
                <div style={{
                  width: 30, height: 30, borderRadius: '50%', flexShrink: 0,
                  background: 'linear-gradient(135deg, #f59e0b 0%, #1a4080 100%)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 15,
                }}>☁️</div>
              )}
              <div style={{
                maxWidth: '75%',
                background: m.role === 'user' ? COLORS.brand : COLORS.pageBg,
                color: m.role === 'user' ? '#fff' : COLORS.textPrimary,
                padding: '10px 14px', borderRadius: m.role === 'user' ? '12px 12px 3px 12px' : '12px 12px 12px 3px',
                fontSize: 13.5, lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
              }}>
                {m.loading ? <span style={{ opacity: 0.6 }}>{m.content}</span> : m.content}
              </div>
            </div>
          ))}

          {messages.length === 1 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 6 }}>
              <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginBottom: 2 }}>Try asking:</div>
              {SUGGESTIONS.map((s, i) => (
                <button key={i} onClick={() => send(s)}
                  style={{
                    textAlign: 'left', padding: '8px 12px', borderRadius: 7,
                    border: `1px solid ${COLORS.border}`, background: COLORS.pageBg,
                    color: COLORS.brand, fontSize: 12.5, cursor: 'pointer',
                  }}>
                  {s}
                </button>
              ))}
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <div style={{ padding: '12px 16px', borderTop: `1px solid ${COLORS.border}`, background: COLORS.pageBg }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKey}
              placeholder="Ask about your AWS infrastructure… (Enter to send, Shift+Enter for new line)"
              rows={2}
              style={{
                flex: 1, padding: '9px 12px', borderRadius: 8,
                border: `1px solid ${COLORS.border}`, fontSize: 13.5, resize: 'none',
                fontFamily: 'inherit', lineHeight: 1.4,
              }}
            />
            <button
              onClick={() => send()}
              disabled={loading || !input.trim()}
              style={{
                padding: '9px 16px', borderRadius: 8, border: 'none',
                background: loading || !input.trim() ? COLORS.textMuted : COLORS.brand,
                color: '#fff', fontWeight: 700, fontSize: 16,
                cursor: loading || !input.trim() ? 'default' : 'pointer',
                alignSelf: 'stretch',
              }}>
              ➤
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
