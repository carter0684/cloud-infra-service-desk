import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'
import { formatDateTimeHK } from '../utils/time.js'
import { COLORS, CARD, Badge } from '../utils/theme.js'
import Avatar from '../components/Avatar.jsx'
import { findUserPhoto } from '../utils/userLookup.js'

const TYPE_LABEL = { feedback: '💬 Feedback', bug: '🐞 Bug Report' }
const TYPE_COLOR = { feedback: COLORS.brand, bug: COLORS.danger }

const STATUS_COLOR = {
  Open: '#2563eb',
  'In Review': '#d97706',
  Resolved: '#16a34a',
  Closed: '#6b7280',
}
const STATUS_OPTIONS = ['Open', 'In Review', 'Resolved', 'Closed']

const SEVERITY_COLOR = { Low: '#16a34a', Medium: '#2563eb', High: '#d97706', Critical: '#dc2626' }
const SEVERITY_OPTIONS = ['Low', 'Medium', 'High', 'Critical']

// ── Submit form — collapsible, above the list ───────────────────────────
function SubmitForm({ onSubmitted }) {
  const [open, setOpen] = useState(false)
  const [reportType, setReportType] = useState('feedback')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [severity, setSeverity] = useState('Medium')
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr] = useState('')

  const reset = () => {
    setReportType('feedback')
    setTitle('')
    setDescription('')
    setSeverity('Medium')
    setErr('')
  }

  const submit = async () => {
    setErr('')
    if (!title.trim()) { setErr('Title is required'); return }
    if (!description.trim()) { setErr('Description is required'); return }
    setSubmitting(true)
    try {
      await api.createFeedback({
        report_type: reportType,
        title: title.trim(),
        description: description.trim(),
        severity: reportType === 'bug' ? severity : null,
        page_url: window.location.href,
      })
      reset()
      setOpen(false)
      onSubmitted()
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not submit report')
    } finally {
      setSubmitting(false)
    }
  }

  if (!open) {
    return (
      <button onClick={() => setOpen(true)}
        style={{ padding: '9px 18px', borderRadius: 7, border: 'none', background: COLORS.brand, color: '#fff', fontWeight: 600, fontSize: 13, cursor: 'pointer', marginBottom: 16 }}>
        ➕ Submit Feedback / Bug Report
      </button>
    )
  }

  return (
    <div style={{ ...CARD, padding: '20px 24px', marginBottom: 20 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 14 }}>
        <h3 style={{ margin: 0, fontSize: 15, color: COLORS.textPrimary }}>Submit Feedback / Bug Report</h3>
        <button onClick={() => { setOpen(false); reset() }}
          style={{ border: 'none', background: 'none', fontSize: 18, cursor: 'pointer', color: COLORS.textMuted, lineHeight: 1 }}>×</button>
      </div>

      <div style={{ display: 'flex', gap: 10, marginBottom: 14 }}>
        {['feedback', 'bug'].map(t => (
          <button key={t} onClick={() => setReportType(t)}
            style={{
              flex: 1, padding: '9px 0', borderRadius: 7, cursor: 'pointer', fontSize: 13, fontWeight: 600,
              border: `1px solid ${reportType === t ? TYPE_COLOR[t] : COLORS.border}`,
              background: reportType === t ? TYPE_COLOR[t] + '15' : '#fff',
              color: reportType === t ? TYPE_COLOR[t] : COLORS.textSecondary,
            }}>
            {TYPE_LABEL[t]}
          </button>
        ))}
      </div>

      <div style={{
        fontSize: 11.5, color: COLORS.textMuted, background: '#f8fafc', border: `1px solid ${COLORS.border}`,
        borderRadius: 6, padding: '8px 10px', marginBottom: 14,
      }}>
        {reportType === 'feedback'
          ? 'ℹ️ Feedback is private — only you and admins can see and reply to it.'
          : 'ℹ️ Bug reports are reviewed by admins only — once submitted, only admins can see and comment on it (not even you).'}
      </div>

      <div style={{ marginBottom: 12 }}>
        <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: COLORS.textSecondary, marginBottom: 5 }}>Title</label>
        <input type="text" value={title} onChange={e => setTitle(e.target.value)} maxLength={200}
          placeholder={reportType === 'bug' ? 'e.g. Create Job button clipped on Health Dashboard' : 'e.g. Suggestion for the Cases page'}
          style={{ width: '100%', padding: '9px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13, boxSizing: 'border-box' }} />
      </div>

      {reportType === 'bug' && (
        <div style={{ marginBottom: 12 }}>
          <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: COLORS.textSecondary, marginBottom: 5 }}>Severity</label>
          <select value={severity} onChange={e => setSeverity(e.target.value)}
            style={{ padding: '9px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13 }}>
            {SEVERITY_OPTIONS.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
      )}

      <div style={{ marginBottom: 14 }}>
        <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: COLORS.textSecondary, marginBottom: 5 }}>Description</label>
        <textarea value={description} onChange={e => setDescription(e.target.value)} maxLength={5000}
          placeholder={reportType === 'bug' ? 'What happened, what did you expect, steps to reproduce…' : 'What would you like to see improved?'}
          style={{ width: '100%', minHeight: 110, padding: '9px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13, resize: 'vertical', boxSizing: 'border-box', fontFamily: 'inherit' }} />
      </div>

      {err && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '8px 12px', marginBottom: 12, color: COLORS.danger, fontSize: 12.5 }}>
          {err}
        </div>
      )}

      <button onClick={submit} disabled={submitting}
        style={{ padding: '9px 18px', borderRadius: 7, border: 'none', background: COLORS.brand, color: '#fff', fontWeight: 600, fontSize: 13, cursor: submitting ? 'default' : 'pointer', opacity: submitting ? 0.6 : 1 }}>
        {submitting ? '⏳ Submitting…' : '📤 Submit'}
      </button>
    </div>
  )
}

// ── Threaded replies + reply box for one report ──────────────────────────
// Who may see this component's reply box is decided by the caller (only
// rendered at all when canReply is true) — this component itself just
// renders the existing thread and, if canReply, an input to add to it.
function ReplyThread({ report, canReply, onUpdated, teamMembers }) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [err, setErr] = useState('')

  const send = async () => {
    if (!text.trim()) return
    setSending(true)
    setErr('')
    try {
      const updated = await api.addFeedbackReply(report.id, text.trim())
      setText('')
      onUpdated(updated)
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not send reply')
    } finally {
      setSending(false)
    }
  }

  const replies = report.replies || []

  return (
    <div style={{ marginTop: 10, paddingTop: 10, borderTop: `1px dashed ${COLORS.border}` }}>
      {replies.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 10 }}>
          {replies.map(rep => (
            <div key={rep.id} style={{
              display: 'flex', gap: 8, alignItems: 'flex-start',
              background: rep.is_admin ? COLORS.brandLight : '#f8fafc',
              borderRadius: 7, padding: '9px 12px',
            }}>
              <Avatar photoUrl={findUserPhoto(teamMembers, rep.author_email)}
                name={rep.author_name} email={rep.author_email} size={34} fontSize={14} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: rep.is_admin ? COLORS.brand : COLORS.textSecondary, marginBottom: 3 }}>
                  {rep.is_admin ? '🛡️ ' : ''}{rep.author_name || rep.author_email}
                  {rep.created_at ? ` · ${formatDateTimeHK(rep.created_at)}` : ''}
                </div>
                <div style={{ fontSize: 13, color: COLORS.textPrimary, whiteSpace: 'pre-wrap' }}>{rep.text}</div>
              </div>
            </div>
          ))}
        </div>
      )}

      {canReply && (
        <div style={{ display: 'flex', gap: 8 }}>
          <input type="text" value={text} onChange={e => setText(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') send() }}
            placeholder="Write a reply…"
            style={{ flex: 1, padding: '7px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5 }} />
          <button onClick={send} disabled={sending || !text.trim()}
            style={{ padding: '7px 14px', borderRadius: 6, border: 'none', background: COLORS.brand, color: '#fff', fontWeight: 600, fontSize: 12, cursor: (sending || !text.trim()) ? 'default' : 'pointer', opacity: (sending || !text.trim()) ? 0.6 : 1 }}>
            {sending ? '⏳' : '↩️ Reply'}
          </button>
        </div>
      )}
      {err && <div style={{ color: COLORS.danger, fontSize: 11.5, marginTop: 6 }}>{err}</div>}
    </div>
  )
}

// ── AI triage note for a bug report (admins only) ───────────────────────
// Generated automatically after a bug is submitted (Claude Sonnet 5.5 via
// Bedrock); this box lets a reviewing engineer open it, re-run it with a
// chosen model, or copy it into the report's thread. Everything is rendered
// as plain text (React escapes it) and labelled as AI-generated.
const AI_PENDING_STALE_MS = 3 * 60 * 1000
const AI_POLL_MS = 4000
const CONFIDENCE_COLOR = { low: '#d97706', medium: '#2563eb', high: '#16a34a' }
const REPLY_MAX = 3000 // matches the backend's reply limit

function aiPendingIsStale(ai) {
  const t = ai?.requested_at ? Date.parse(ai.requested_at) : NaN
  return Number.isFinite(t) && Date.now() - t > AI_PENDING_STALE_MS
}

function aiAsReplyText(ai) {
  const a = ai.analysis || {}
  const bullets = (title, items) => (items && items.length ? [`${title}:`, ...items.map(i => `- ${i}`)] : [])
  const lines = [
    `🤖 AI analysis (${ai.model_label || 'Claude Sonnet 5.5'}) — AI-generated, please verify`,
    `Summary: ${a.summary || ''}`,
    a.likely_cause ? `Likely cause (${a.confidence || 'low'} confidence): ${a.likely_cause}` : '',
    ...bullets('Suggested fix', a.suggested_fix),
    ...bullets('Enhancement ideas', a.enhancement_ideas),
    ...bullets('Questions for the reporter', a.questions_for_reporter),
    a.files_to_check && a.files_to_check.length ? `Files to check: ${a.files_to_check.join(', ')}` : '',
  ].filter(Boolean)
  const text = lines.join('\n')
  return text.length > REPLY_MAX ? text.slice(0, REPLY_MAX - 1) + '…' : text
}

function AiList({ title, items }) {
  if (!items || items.length === 0) return null
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 11.5, fontWeight: 700, color: COLORS.textSecondary, marginBottom: 3 }}>{title}</div>
      <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: COLORS.textPrimary }}>
        {items.map((it, i) => <li key={i} style={{ marginBottom: 2 }}>{it}</li>)}
      </ul>
    </div>
  )
}

// `onPatch(reportId, patch)` must be a stable reference (useCallback) — it is
// a dependency of the polling effect below.
function AiAnalysisBox({ report, models, onPatch }) {
  const ai = report.ai_analysis || null
  const status = ai?.status || 'none'
  const stale = status === 'pending' && aiPendingIsStale(ai)
  const [open, setOpen] = useState(false)
  const [model, setModel] = useState('') // '' = default (Claude Sonnet 5.5, retried once)
  const [busy, setBusy] = useState(false)
  const [posting, setPosting] = useState(false)
  const [err, setErr] = useState('')
  const [posted, setPosted] = useState(false)

  // While the note is being generated, poll just this report's note.
  useEffect(() => {
    if (status !== 'pending' || stale) return undefined
    const timer = setInterval(() => {
      api.getFeedbackAiAnalysis(report.id)
        .then(d => { if (d.ai_analysis && d.ai_analysis.status !== 'pending') onPatch(report.id, { ai_analysis: d.ai_analysis }) })
        .catch(() => { /* keep polling; a transient error must not break the page */ })
    }, AI_POLL_MS)
    return () => clearInterval(timer)
  }, [status, stale, report.id, onPatch])

  const run = async () => {
    setBusy(true)
    setErr('')
    setPosted(false)
    try {
      const d = await api.requestFeedbackAiAnalysis(report.id, model || null)
      onPatch(report.id, { ai_analysis: d.ai_analysis })
      setOpen(true)
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not start the AI analysis')
    } finally {
      setBusy(false)
    }
  }

  const postToThread = async () => {
    setPosting(true)
    setErr('')
    try {
      const updated = await api.addFeedbackReply(report.id, aiAsReplyText(ai))
      onPatch(report.id, updated)
      setPosted(true)
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not post to the thread')
    } finally {
      setPosting(false)
    }
  }

  const a = ai?.analysis
  const headerChip = {
    none: { text: 'Not analysed', color: '#6b7280' },
    pending: stale ? { text: 'Taking too long', color: '#d97706' } : { text: 'Analysing…', color: '#2563eb' },
    failed: { text: 'Failed', color: '#dc2626' },
    done: { text: ai?.model_label || 'Done', color: '#7c3aed' },
  }[status] || { text: status, color: '#6b7280' }

  return (
    <div style={{ marginTop: 10, border: `1px solid ${COLORS.border}`, borderRadius: 8, background: '#faf5ff' }}>
      <button onClick={() => setOpen(o => !o)}
        style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 8, padding: '9px 12px', border: 'none', background: 'none', cursor: 'pointer', textAlign: 'left' }}>
        <span style={{ fontSize: 12.5, fontWeight: 700, color: '#6d28d9' }}>🤖 AI analysis</span>
        <Badge text={headerChip.text} color={headerChip.color} />
        {status === 'done' && a?.suggested_severity && a.suggested_severity !== report.severity && (
          <span style={{ fontSize: 11, color: COLORS.warning }}>suggests severity {a.suggested_severity}</span>
        )}
        {!open && status === 'done' && a?.summary && (
          <span style={{ flex: 1, minWidth: 0, fontSize: 12, color: COLORS.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.summary}</span>
        )}
        <span style={{ marginLeft: 'auto', color: COLORS.textMuted, fontSize: 12 }}>{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div style={{ padding: '0 14px 12px' }}>
          {status === 'pending' && !stale && (
            <div style={{ fontSize: 12.5, color: COLORS.textSecondary }}>⏳ The AI is analysing this report — this usually takes under a minute. The result will appear here automatically.</div>
          )}
          {stale && (
            <div style={{ fontSize: 12.5, color: COLORS.warning }}>The analysis is taking longer than expected (it may have been interrupted). You can run it again.</div>
          )}
          {status === 'failed' && (
            <div style={{ fontSize: 12.5, color: COLORS.danger }}>⚠️ {ai.error || 'The AI analysis failed.'}</div>
          )}
          {status === 'none' && (
            <div style={{ fontSize: 12.5, color: COLORS.textSecondary }}>This report has not been analysed yet.</div>
          )}

          {status === 'done' && a && (
            <>
              <div style={{ fontSize: 13, color: COLORS.textPrimary }}>{a.summary}</div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 8, alignItems: 'center' }}>
                {a.category && <Badge text={a.category} color="#6b7280" />}
                {a.confidence && <Badge text={`${a.confidence} confidence`} color={CONFIDENCE_COLOR[a.confidence] || '#6b7280'} />}
                {a.suggested_severity && (
                  <span style={{ fontSize: 11.5, color: COLORS.textSecondary }}>
                    AI severity: <b style={{ color: SEVERITY_COLOR[a.suggested_severity] }}>{a.suggested_severity}</b>
                    {a.severity_reason ? ` — ${a.severity_reason}` : ''}
                  </span>
                )}
              </div>
              {a.likely_cause && (
                <div style={{ marginTop: 10 }}>
                  <div style={{ fontSize: 11.5, fontWeight: 700, color: COLORS.textSecondary, marginBottom: 3 }}>Likely cause</div>
                  <div style={{ fontSize: 12.5, color: COLORS.textPrimary }}>{a.likely_cause}</div>
                </div>
              )}
              <AiList title="Suggested fix" items={a.suggested_fix} />
              <AiList title="Enhancement ideas" items={a.enhancement_ideas} />
              <AiList title="Questions to ask the reporter" items={a.questions_for_reporter} />
              {a.files_to_check && a.files_to_check.length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <div style={{ fontSize: 11.5, fontWeight: 700, color: COLORS.textSecondary, marginBottom: 3 }}>Files to check</div>
                  <div style={{ fontFamily: 'monospace', fontSize: 11.5, color: COLORS.textPrimary }}>
                    {a.files_to_check.map(f => <div key={f}>{f}</div>)}
                  </div>
                </div>
              )}
              <div style={{ marginTop: 10, fontSize: 11, color: COLORS.textMuted }}>
                AI-generated by {ai.model_label}{ai.generated_at ? ` · ${formatDateTimeHK(ai.generated_at)}` : ''} — a starting point for the reviewer, not a verified diagnosis.
              </div>
            </>
          )}

          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 12 }}>
            <select value={model} onChange={e => setModel(e.target.value)} disabled={busy || (status === 'pending' && !stale)}
              title="Model to use"
              style={{ padding: '5px 8px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12 }}>
              <option value="">Auto (Claude Sonnet 5.5)</option>
              {(models || []).map(m => <option key={m.key} value={m.key}>{m.label}</option>)}
            </select>
            <button onClick={run} disabled={busy || (status === 'pending' && !stale)}
              style={{ padding: '5px 12px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontWeight: 600, fontSize: 12, cursor: (busy || (status === 'pending' && !stale)) ? 'default' : 'pointer', opacity: (busy || (status === 'pending' && !stale)) ? 0.6 : 1 }}>
              {busy ? '⏳ Starting…' : status === 'none' ? '🤖 Analyse with AI' : '🔄 Regenerate'}
            </button>
            {status === 'done' && (
              <button onClick={postToThread} disabled={posting || posted}
                title="Adds this analysis to the report's thread as a reply from you, labelled as AI-generated"
                style={{ padding: '5px 12px', borderRadius: 6, border: 'none', background: COLORS.brand, color: '#fff', fontWeight: 600, fontSize: 12, cursor: (posting || posted) ? 'default' : 'pointer', opacity: (posting || posted) ? 0.6 : 1 }}>
                {posting ? '⏳ Posting…' : posted ? '✅ Added to thread' : '↩️ Add to thread'}
              </button>
            )}
          </div>
          {err && <div style={{ color: COLORS.danger, fontSize: 11.5, marginTop: 6 }}>{err}</div>}
        </div>
      )}
    </div>
  )
}

export default function FeedbackPage() {
  const { user } = useAuth()
  const [reports, setReports] = useState([])
  const [aiModels, setAiModels] = useState([])
  const [teamMembers, setTeamMembers] = useState([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [filterType, setFilterType] = useState('all')

  const load = useCallback(() => {
    setLoading(true)
    setErr('')
    return api.listFeedback()
      .then(d => { setReports(d.reports || []); setAiModels(d.ai_models || []); return d })
      .catch(e => setErr(e?.response?.data?.detail || 'Could not load feedback/bug reports'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])
  useEffect(() => { api.getOptions().then(o => setTeamMembers(o.team_members || [])).catch(() => {}) }, [])

  // Merge (not replace) so fields the server didn't send back — notably the
  // admin-only ai_analysis — are never dropped by a status/reply update.
  const patchReport = useCallback((id, patch) => {
    setReports(prev => prev.map(r => r.id === id ? { ...r, ...patch } : r))
  }, [])
  const applyUpdated = (updated) => patchReport(updated.id, updated)

  const changeStatus = async (report, status) => {
    try {
      const updated = await api.updateFeedbackStatus(report.id, status)
      applyUpdated(updated)
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not update status')
    }
  }

  const removeReport = async (id) => {
    if (!window.confirm('Delete this report? This cannot be undone.')) return
    try {
      await api.deleteFeedback(id)
      setReports(prev => prev.filter(r => r.id !== id))
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not delete report')
    }
  }

  const visible = filterType === 'all' ? reports : reports.filter(r => r.report_type === filterType)

  return (
    <div style={{ padding: '24px 28px', maxWidth: 900 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 18 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>💬 Feedback & Bug Report</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            {user?.is_admin
              ? 'All feedback threads (private with each submitter) and all bug reports.'
              : 'Feedback is a private thread between you and admins. Bug reports are reviewed by admins only — you won\'t see them here after submitting.'}
          </div>
        </div>
        <button onClick={load} disabled={loading}
          style={{ padding: '8px 16px', borderRadius: 7, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontWeight: 600, fontSize: 13, cursor: loading ? 'default' : 'pointer' }}>
          {loading ? '⏳ Loading…' : '🔄 Refresh'}
        </button>
      </div>

      <SubmitForm onSubmitted={load} />

      {err && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {err}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
        {['all', 'feedback', 'bug'].map(t => (
          <button key={t} onClick={() => setFilterType(t)}
            style={{
              padding: '6px 14px', borderRadius: 999, cursor: 'pointer', fontSize: 12.5, fontWeight: 600,
              border: `1px solid ${filterType === t ? COLORS.brand : COLORS.border}`,
              background: filterType === t ? COLORS.brand + '15' : '#fff',
              color: filterType === t ? COLORS.brand : COLORS.textSecondary,
            }}>
            {t === 'all' ? 'All' : TYPE_LABEL[t]}
          </button>
        ))}
      </div>

      {loading ? (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted }}>Loading…</div>
      ) : visible.length === 0 ? (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted }}>No reports yet — be the first to submit one!</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {visible.map(r => {
            // Reply permission mirrors the server-side rule: "feedback" —
            // admin or the original submitter; "bug" — admin only.
            const canReply = user?.is_admin || (r.report_type === 'feedback' && user?.email === r.submitted_by_email)
            return (
              <div key={r.id} style={{ ...CARD, padding: '16px 20px' }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 10, marginBottom: 8, flexWrap: 'wrap' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <Badge text={TYPE_LABEL[r.report_type] || r.report_type} color={TYPE_COLOR[r.report_type] || '#6b7280'} />
                    {user?.is_admin ? (
                      <select value={r.status} onChange={e => changeStatus(r, e.target.value)}
                        style={{ padding: '3px 8px', borderRadius: 6, border: `1px solid ${STATUS_COLOR[r.status] || COLORS.border}40`, background: (STATUS_COLOR[r.status] || '#6b7280') + '18', color: STATUS_COLOR[r.status] || '#6b7280', fontWeight: 600, fontSize: 11.5, cursor: 'pointer' }}>
                        {STATUS_OPTIONS.map(s => <option key={s} value={s}>{s}</option>)}
                      </select>
                    ) : (
                      <Badge text={r.status} color={STATUS_COLOR[r.status] || '#6b7280'} />
                    )}
                    {r.severity && <Badge text={r.severity} color={SEVERITY_COLOR[r.severity] || '#6b7280'} />}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <span style={{ fontSize: 11.5, color: COLORS.textMuted }}>{r.created_at ? formatDateTimeHK(r.created_at) : ''}</span>
                    {(user?.is_admin || user?.email === r.submitted_by_email) && (
                      <button onClick={() => removeReport(r.id)} title="Delete this report"
                        style={{ border: 'none', background: 'none', cursor: 'pointer', color: COLORS.danger, fontSize: 13 }}>
                        🗑️
                      </button>
                    )}
                  </div>
                </div>
                <h4 style={{ margin: '0 0 6px', color: COLORS.textPrimary, fontSize: 14.5 }}>{r.title}</h4>
                <div style={{ fontSize: 13, color: COLORS.textSecondary, whiteSpace: 'pre-wrap', marginBottom: 8 }}>{r.description}</div>
                <div style={{ fontSize: 11.5, color: COLORS.textMuted, display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Avatar photoUrl={findUserPhoto(teamMembers, r.submitted_by_email)}
                    name={r.submitted_by_name} email={r.submitted_by_email} size={26} fontSize={12} />
                  Submitted by {r.submitted_by_name || r.submitted_by_email}
                  {r.report_type === 'bug' && user?.is_admin && (
                    <span style={{ marginLeft: 8, color: COLORS.warning }}>🔒 Only visible to admins</span>
                  )}
                </div>

                {user?.is_admin && r.report_type === 'bug' && (
                  <AiAnalysisBox report={r} models={aiModels} onPatch={patchReport} />
                )}

                <ReplyThread report={r} canReply={canReply} onUpdated={applyUpdated} teamMembers={teamMembers} />
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
