import React, { useState, useRef, useEffect } from 'react'
import { COLORS, CARD } from '../utils/theme.js'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'
import Avatar from '../components/Avatar.jsx'
import {
  isSpeechRecognitionSupported, isSpeechSynthesisSupported,
  createSpeechRecognizer, speak, stopSpeaking, VOICE_LANGUAGES,
} from '../utils/speech.js'

// ── "AI Assistant" sidebar tab ──────────────────────────────────────────────
// A full-page, standalone AI chatbot — deliberately simpler than the
// floating ChatBot.jsx widget: pure general-purpose Q&A only, no
// Knowledge Base retrieval, no past-job lookups, no AWS resource access
// of any kind. Calls its own backend route (POST /api/ai-assistant/chat,
// see app/main.py) rather than the widget's /api/chat, so the two stay
// independent even though both ultimately call the same Bedrock model.
//
// Gated by the "ai_assistant" tab_id (see settings_store.py) — an admin
// can restrict this tab to selected users via the Admin page's Tab
// Access Control / User Groups, same as every other sidebar tab.
//
// Voice chat (Plan A — see conversation history for the options
// considered): both speech-to-text (mic button, fills the input as you
// speak and auto-sends on a final result) and text-to-speech (🔊 toggle,
// reads assistant replies aloud) run entirely via the browser's built-in
// Web Speech API (utils/speech.js) — no backend/AWS involvement, no new
// IAM permissions, no new cost. Gracefully hides the mic/speaker controls
// on browsers that don't support the relevant API (Firefox has no
// SpeechRecognition; Safari support is partial) rather than breaking.

const SUGGESTIONS = [
  'Explain the difference between a VPC and a subnet.',
  'What is the latest LTS version of Node.js?',
  'Give me a checklist for reviewing a pull request.',
  'How do I write a good incident postmortem?',
]

export default function AiAssistantPage() {
  const { user } = useAuth()
  const [messages, setMessages] = useState([
    {
      role: 'assistant',
      content: "Hi! I'm your AI Assistant — ask me anything. I don't have access to this portal's tickets, knowledge base, or AWS data; for those, use the relevant portal tab instead.",
      webCitations: 0,
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef()
  const inputRef = useRef()

  // Voice chat state (Web Speech API — see utils/speech.js)
  const [voiceLang, setVoiceLang] = useState('en-US')
  const [listening, setListening] = useState(false)
  const [voiceReplyOn, setVoiceReplyOn] = useState(false)
  const [micError, setMicError] = useState('')
  const recognizerRef = useRef(null)
  const speechSupported = isSpeechRecognitionSupported()
  const ttsSupported = isSpeechSynthesisSupported()

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  // Stop any in-flight recognition/speech when leaving the page.
  useEffect(() => {
    return () => {
      recognizerRef.current?.stop()
      stopSpeaking()
    }
  }, [])

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
      const res = await api.aiAssistantChat(msg, history)
      setMessages(prev => [
        ...prev.slice(0, -1),
        { role: 'assistant', content: res.answer, webCitations: res.web_citations || 0 },
      ])
      if (voiceReplyOn) speak(res.answer, { lang: voiceLang })
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || 'AI service unavailable. Please try again.'
      setMessages(prev => [
        ...prev.slice(0, -1),
        { role: 'assistant', content: '⚠️ ' + detail, webCitations: 0 },
      ])
    } finally {
      setLoading(false)
    }
  }

  const handleKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
  }

  const clearChat = () => {
    stopSpeaking()
    setMessages([{
      role: 'assistant',
      content: 'Chat cleared. What can I help you with?',
      webCitations: 0,
    }])
  }

  const toggleListening = () => {
    if (listening) {
      recognizerRef.current?.stop()
      return
    }
    setMicError('')
    const recognizer = createSpeechRecognizer({
      lang: voiceLang,
      onInterim: (text) => setInput(text),
      onFinal: (text) => {
        setInput(text)
        send(text)
      },
      onEnd: () => setListening(false),
      onError: (e) => {
        setMicError(
          e?.error === 'not-allowed' || e?.error === 'permission-denied'
            ? 'Microphone access was denied — check your browser permissions.'
            : `Voice input error (${e?.error || 'unknown'}).`
        )
        setListening(false)
      },
    })
    if (!recognizer) {
      setMicError('Voice input is not supported in this browser.')
      return
    }
    recognizerRef.current = recognizer
    setListening(true)
    recognizer.start()
  }

  const toggleVoiceReply = () => {
    if (voiceReplyOn) stopSpeaking()
    setVoiceReplyOn(v => !v)
  }

  return (
    <div style={{ padding: 24, maxWidth: 860, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 19, color: COLORS.textPrimary }}>🤖 AI Assistant</h2>
          <div style={{ fontSize: 12.5, color: COLORS.textSecondary, marginTop: 2 }}>
            General-purpose Q&amp;A only — no ticket, knowledge base, or AWS resource access.
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {(speechSupported || ttsSupported) && (
            <select
              value={voiceLang}
              onChange={e => setVoiceLang(e.target.value)}
              title="Voice language"
              style={{
                background: COLORS.pageBg, color: COLORS.textSecondary, border: `1px solid ${COLORS.border}`,
                borderRadius: 6, padding: '5px 8px', fontSize: 12, cursor: 'pointer',
              }}>
              {VOICE_LANGUAGES.map(l => <option key={l.value} value={l.value}>{l.label}</option>)}
            </select>
          )}
          {ttsSupported && (
            <button onClick={toggleVoiceReply}
              title={voiceReplyOn ? 'Voice reply on — click to mute' : 'Voice reply off — click to have replies read aloud'}
              style={{
                background: voiceReplyOn ? COLORS.brandLight : COLORS.pageBg,
                color: voiceReplyOn ? COLORS.brand : COLORS.textSecondary,
                border: `1px solid ${voiceReplyOn ? COLORS.brand + '60' : COLORS.border}`,
                borderRadius: 6, padding: '6px 10px', fontSize: 13, cursor: 'pointer',
              }}>
              {voiceReplyOn ? '🔊' : '🔈'}
            </button>
          )}
          <button onClick={clearChat}
            style={{
              background: COLORS.pageBg, color: COLORS.textSecondary, border: `1px solid ${COLORS.border}`,
              borderRadius: 6, padding: '6px 12px', fontSize: 12, cursor: 'pointer',
            }}>
            Clear chat
          </button>
        </div>
      </div>

      {micError && (
        <div style={{
          fontSize: 12, color: COLORS.danger, background: COLORS.dangerBg,
          border: `1px solid ${COLORS.danger}40`, borderRadius: 6, padding: '7px 12px', marginBottom: 10,
        }}>
          🎤 {micError}
        </div>
      )}

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
                  background: 'linear-gradient(135deg, #1a4080 0%, #7c3aed 100%)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 15,
                }}>🤖</div>
              )}
              <div style={{
                maxWidth: '75%',
                background: m.role === 'user' ? COLORS.brand : COLORS.pageBg,
                color: m.role === 'user' ? '#fff' : COLORS.textPrimary,
                padding: '10px 14px', borderRadius: m.role === 'user' ? '12px 12px 3px 12px' : '12px 12px 12px 3px',
                fontSize: 13.5, lineHeight: 1.6, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
              }}>
                {m.loading ? <span style={{ opacity: 0.6 }}>⏳ Thinking…</span> : m.content}
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
          {listening && (
            <div style={{ fontSize: 11.5, color: COLORS.brand, marginBottom: 6, display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{
                width: 8, height: 8, borderRadius: '50%', background: '#dc2626',
                display: 'inline-block', animation: 'pulse 1.2s ease-in-out infinite',
              }} />
              🎙️ Listening… speak now
            </div>
          )}
          <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
            {speechSupported && (
              <button
                onClick={toggleListening}
                disabled={loading}
                title={listening ? 'Stop listening' : 'Speak your question'}
                style={{
                  width: 40, height: 40, borderRadius: '50%', border: 'none', flexShrink: 0,
                  background: listening ? '#dc2626' : COLORS.brand,
                  color: '#fff', fontSize: 17, cursor: loading ? 'default' : 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}>
                {listening ? '⏹️' : '🎤'}
              </button>
            )}
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKey}
              placeholder="Ask anything… (Enter to send, Shift+Enter for new line, or click 🎤 to speak)"
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
