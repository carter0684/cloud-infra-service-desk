import React, { useState, useRef, useEffect } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'
import Avatar from './Avatar.jsx'

const SUGGESTIONS = [
  'How do I troubleshoot AD replication issues?',
  'What are common causes of VPN disconnections on AWS?',
  'How was the last EC2 outage handled?',
  'Steps to migrate on-prem AD to AWS Managed AD?',
  'What is the runbook for storage volume expansion?',
]

function SourceBadge({ kbChunks, jobsMatched, webCitations }) {
  if (!kbChunks && !jobsMatched && !webCitations) return null
  return (
    <div style={{ display: 'flex', gap: 6, marginTop: 6, flexWrap: 'wrap' }}>
      {kbChunks > 0 && (
        <span style={{ fontSize: 10, background: '#eff6ff', color: '#1d4ed8', border: '1px solid #bfdbfe', borderRadius: 99, padding: '2px 8px', fontWeight: 600 }}>
          🗂️ {kbChunks} KB chunks
        </span>
      )}
      {jobsMatched > 0 && (
        <span style={{ fontSize: 10, background: '#f0fdf4', color: '#15803d', border: '1px solid #86efac', borderRadius: 99, padding: '2px 8px', fontWeight: 600 }}>
          📋 {jobsMatched} past jobs
        </span>
      )}
      {webCitations > 0 && (
        <span style={{ fontSize: 10, background: '#fef9c3', color: '#854d0e', border: '1px solid #fde047', borderRadius: 99, padding: '2px 8px', fontWeight: 600 }}>
          🌐 {webCitations} web source{webCitations > 1 ? 's' : ''}
        </span>
      )}
    </div>
  )
}

export default function ChatBot() {
  const { user } = useAuth()
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState([
    {
      role: 'assistant',
      content: "Hi! I'm your Cloud Infra assistant. Ask me anything — I can search the knowledge base, past job notes, and use my cloud expertise to help you.",
      kbChunks: 0,
      jobsMatched: 0,
    }
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef()
  const inputRef = useRef()

  // Scroll to bottom on new message
  useEffect(() => {
    if (open) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, open])

  // Focus input when opened
  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 100)
  }, [open])

  if (!user) return null  // only show when logged in

  const send = async (text) => {
    const msg = (text || input).trim()
    if (!msg || loading) return
    setInput('')

    const userMsg = { role: 'user', content: msg }
    const history = messages
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .map(m => ({ role: m.role, content: m.content }))

    setMessages(prev => [...prev, userMsg, { role: 'assistant', content: '…', loading: true }])
    setLoading(true)

    try {
      const res = await api.chat(msg, history)
      setMessages(prev => [
        ...prev.slice(0, -1),
        {
          role: 'assistant',
          content: res.answer,
          kbChunks: res.kb_chunks_used,
          jobsMatched: res.past_jobs_matched,
          webCitations: res.web_citations || 0,
        }
      ])
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || 'AI service unavailable. Please try again.'
      setMessages(prev => [
        ...prev.slice(0, -1),
        { role: 'assistant', content: '⚠️ ' + detail, kbChunks: 0, jobsMatched: 0 }
      ])
    } finally {
      setLoading(false)
    }
  }

  const handleKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
  }

  const clearChat = () => {
    setMessages([{
      role: 'assistant',
      content: "Chat cleared. What can I help you with?",
      kbChunks: 0,
      jobsMatched: 0,
    }])
  }

  return (
    <>
      {/* Floating bubble */}
      <button
        onClick={() => setOpen(o => !o)}
        title="Ask the AI assistant"
        style={{
          position: 'fixed', bottom: 28, right: 28, zIndex: 1000,
          width: 54, height: 54, borderRadius: '50%',
          background: open ? '#475569' : 'linear-gradient(135deg, #1a4080 0%, #7c3aed 100%)',
          color: '#fff', border: 'none', fontSize: 22,
          boxShadow: '0 4px 16px rgba(0,0,0,0.25)',
          cursor: 'pointer', transition: 'all 0.2s',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
        {open ? '✕' : '🤖'}
      </button>

      {/* Chat panel */}
      {open && (
        <div style={{
          position: 'fixed', bottom: 92, right: 28, zIndex: 999,
          // width/height use CSS min()/calc() (not a JS breakpoint) so
          // this panel never overflows a narrow phone viewport — on
          // desktop-width screens these resolve to the original fixed
          // 420x560, but on a phone (e.g. 360px wide) they shrink to fit
          // within the viewport minus a margin instead of clipping off
          // the right/bottom edge.
          width: 'min(420px, calc(100vw - 24px))',
          height: 'min(560px, calc(100vh - 140px))',
          background: '#fff', borderRadius: 14,
          boxShadow: '0 8px 40px rgba(0,0,0,0.18)',
          display: 'flex', flexDirection: 'column',
          overflow: 'hidden', border: '1px solid #e2e8f0',
        }}>
          {/* Header */}
          <div style={{
            background: 'linear-gradient(90deg, #1a4080, #7c3aed)',
            padding: '14px 16px', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          }}>
            <div>
              <div style={{ color: '#fff', fontWeight: 700, fontSize: 14 }}>🤖 Cloud Infra Assistant</div>
              <div style={{ color: '#c4b5fd', fontSize: 11 }}>KB · Past jobs · Web search · Reasoning</div>
            </div>
            <button onClick={clearChat}
              style={{ background: 'rgba(255,255,255,0.15)', color: '#fff', border: 'none', borderRadius: 5, padding: '4px 10px', fontSize: 11, cursor: 'pointer' }}>
              Clear
            </button>
          </div>

          {/* Messages */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 10 }}>
            {messages.map((m, i) => (
              <div key={i} style={{
                display: 'flex',
                alignItems: 'flex-end',
                gap: 6,
                justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start',
              }}>
                {m.role === 'user' && (
                  <Avatar photoUrl={user?.photo_data_url} name={user?.name} email={user?.email} size={30} fontSize={13} />
                )}
                <div style={{
                  maxWidth: '85%',
                  background: m.role === 'user' ? '#1a4080' : '#f1f5f9',
                  color: m.role === 'user' ? '#fff' : '#1e293b',
                  padding: '9px 13px', borderRadius: m.role === 'user' ? '12px 12px 3px 12px' : '12px 12px 12px 3px',
                  fontSize: 13, lineHeight: 1.55,
                  whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                }}>
                  {m.loading ? (
                    <span style={{ opacity: 0.6 }}>⏳ Thinking…</span>
                  ) : m.content}
                  {m.role === 'assistant' && !m.loading && (
                    <SourceBadge kbChunks={m.kbChunks} jobsMatched={m.jobsMatched} webCitations={m.webCitations} />
                  )}
                </div>
              </div>
            ))}

            {/* Suggestions — shown only when only the greeting is there */}
            {messages.length === 1 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 4 }}>
                <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 2 }}>Try asking:</div>
                {SUGGESTIONS.map((s, i) => (
                  <button key={i} onClick={() => send(s)}
                    style={{
                      textAlign: 'left', padding: '7px 10px', borderRadius: 6,
                      border: '1px solid #e2e8f0', background: '#f8fafc',
                      color: '#1e40af', fontSize: 12, cursor: 'pointer',
                    }}>
                    {s}
                  </button>
                ))}
              </div>
            )}

            <div ref={bottomRef} />
          </div>

          {/* Input */}
          <div style={{ padding: '10px 12px', borderTop: '1px solid #e5e7eb', background: '#f8fafc' }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
              <textarea
                ref={inputRef}
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={handleKey}
                placeholder="Ask anything… (Enter to send, Shift+Enter for new line)"
                rows={2}
                style={{
                  flex: 1, padding: '8px 10px', borderRadius: 8,
                  border: '1px solid #d1d5db', fontSize: 13, resize: 'none',
                  fontFamily: 'inherit', lineHeight: 1.4,
                }}
              />
              <button
                onClick={() => send()}
                disabled={loading || !input.trim()}
                style={{
                  padding: '8px 14px', borderRadius: 8, border: 'none',
                  background: loading || !input.trim() ? '#94a3b8' : '#1a4080',
                  color: '#fff', fontWeight: 700, fontSize: 16, cursor: loading || !input.trim() ? 'default' : 'pointer',
                  alignSelf: 'stretch',
                }}>
                ➤
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
