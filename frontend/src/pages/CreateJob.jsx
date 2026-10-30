import React, { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { formatDateTimeHK, todayHKYmd } from '../utils/time.js'
import { Badge } from '../utils/theme.js'
import { useAuth } from '../auth/AuthContext.jsx'

const CARD = { maxWidth: 720, margin: '28px auto', background: '#fff', borderRadius: 10, padding: '32px 36px', boxShadow: '0 1px 8px rgba(0,0,0,0.08)' }
const LABEL = { fontSize: 13, fontWeight: 600, color: '#374151', display: 'block', marginBottom: 5, marginTop: 16 }
const INPUT = { width: '100%', padding: '9px 12px', borderRadius: 7, border: '1px solid #d1d5db', fontSize: 14, boxSizing: 'border-box' }
const SELECT = { ...INPUT, background: '#fff' }
const ERR = { color: '#dc2626', fontSize: 12, marginTop: 3 }

// Severity colors driving the shared outlined-pill Badge style (theme.js)
// — background/border tints are derived automatically from this single
// color per severity instead of each page hand-picking its own 3-color
// (bg/text/border) combo, so this pill now matches every other
// status/severity badge in the portal.
const SEVERITY_COLOR = { critical: '#be123c', high: '#c2410c', normal: '#b45309', low: '#15803d' }

export default function CreateJob() {
  const { user } = useAuth()
  // Per-user tab WRITE grant for "jobs" (see database.py's
  // PortalUser.tab_write docstring and main.py's
  // require_tab_write("jobs")). This tab (jobs_new) may still be VISIBLE
  // to this user via their User Group even without write access — the
  // backend enforces this on the actual POST /api/jobs regardless, but
  // showing a clear message here is better than a silent form that
  // fails only at the very end.
  const canWriteJobs = (user?.tab_write || []).includes('jobs')
  const navigate = useNavigate()
  const fileRef = useRef()
  const [options, setOptions] = useState({ categories: [], priorities: [], team_members: [] })
  const [form, setForm] = useState({
    title: '', description: '', category: '', category_other: '',
    priority: '', assignee_email: '', due_date: '',
  })
  const [errors, setErrors] = useState({})
  const [saving, setSaving] = useState(false)

  // AI extract states
  const [aiTab, setAiTab] = useState('file')          // 'file' | 'email'
  const [emailText, setEmailText] = useState('')
  const [extracting, setExtracting] = useState(false)
  const [extractMsg, setExtractMsg] = useState('')    // success/error feedback
  const [extractErr, setExtractErr] = useState(false)
  const [dragOver, setDragOver] = useState(false)

  // AI analysis preview states
  const [aiPreview, setAiPreview] = useState(null)
  const [analysing, setAnalysing] = useState(false)
  const [previewExpanded, setPreviewExpanded] = useState(true)

  useEffect(() => {
    api.getOptions().then(setOptions).catch(() => {})
  }, [])

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  // ── Apply extracted fields to form ──────────────────────────────────────────
  const applyExtraction = async (data, freshOpts) => {
    const opts = freshOpts || options
    const newForm = {
      title: data.title || form.title,
      description: data.description || form.description,
      category: data.category && opts.categories.includes(data.category) ? data.category : form.category,
      priority: data.priority && opts.priorities.includes(data.priority) ? data.priority : form.priority,
    }
    setForm(f => ({ ...f, ...newForm }))
    setExtractMsg(
      `✅ AI filled in the form from ${data.source === 'file' ? data.filename : 'email text'} ` +
      `(${data.raw_char_count} chars read). Review and edit before saving.`
    )
    setExtractErr(false)

    // Auto-trigger AI analysis preview using the extracted fields
    const title = newForm.title
    const description = newForm.description
    const category = newForm.category || 'Other'
    const priority = newForm.priority || 'Medium'
    if (title && description) {
      setAnalysing(true)
      setAiPreview(null)
      setPreviewExpanded(true)
      try {
        const preview = await api.analysePreview(title, description, category, priority)
        setAiPreview(preview)
      } catch {
        // Analysis preview failure is non-blocking — user can still submit
      } finally {
        setAnalysing(false)
      }
    }
  }

  // ── File upload handler ──────────────────────────────────────────────────────
  const handleFile = async (file) => {
    if (!file) return
    setExtracting(true)
    setExtractMsg('')
    setExtractErr(false)
    try {
      // Re-fetch options first so categories/priorities are up to date
      const freshOptions = await api.getOptions()
      setOptions(freshOptions)
      const data = await api.extractFromFile(file)
      await applyExtraction({ ...data, source: 'file' }, freshOptions)
    } catch (e) {
      // api.extractFromFile() uses fetch() and throws a plain Error (not an
      // axios error), so e.response.data.detail is never set for this path
      // — fall back to e.message (which already contains the backend's
      // real error text) before the generic placeholder.
      setExtractMsg(e?.response?.data?.detail || e?.message || 'Failed to extract from file')
      setExtractErr(true)
    } finally {
      setExtracting(false)
    }
  }

  // ── Email text extract handler ───────────────────────────────────────────────
  const handleEmailExtract = async () => {
    if (!emailText.trim()) return
    setExtracting(true)
    setExtractMsg('')
    setExtractErr(false)
    try {
      const freshOptions = await api.getOptions()
      setOptions(freshOptions)
      const data = await api.extractFromEmail(emailText)
      await applyExtraction({ ...data, source: 'email_text' }, freshOptions)
    } catch (e) {
      setExtractMsg(e?.response?.data?.detail || 'Failed to extract from email')
      setExtractErr(true)
    } finally {
      setExtracting(false)
    }
  }

  // ── Form validation ──────────────────────────────────────────────────────────
  const validate = () => {
    const e = {}
    if (!form.title.trim()) e.title = 'Title is required'
    else if (form.title.length > 200) e.title = 'Max 200 characters'
    if (!form.description.trim()) e.description = 'Description is required'
    else if (form.description.length > 5000) e.description = 'Max 5000 characters'
    if (!form.category) e.category = 'Category is required'
    if (form.category === 'Other' && !form.category_other.trim()) e.category_other = 'Please describe the category'
    if (!form.priority) e.priority = 'Priority is required'
    if (!form.assignee_email) e.assignee_email = 'Assignee is required'
    return e
  }

  const handleSubmit = async () => {
    const e = validate()
    setErrors(e)
    if (Object.keys(e).length > 0) return
    setSaving(true)
    try {
      const job = await api.createJob({
        title: form.title.trim(),
        description: form.description.trim(),
        category: form.category,
        category_other: form.category_other.trim() || undefined,
        priority: form.priority,
        assignee_email: form.assignee_email,
        due_date: form.due_date || undefined,
      })
      navigate(`/jobs/${job.id}`)
    } catch (e) {
      setErrors({ submit: e?.response?.data?.detail || 'Failed to create job' })
    } finally {
      setSaving(false)
    }
  }

  if (!canWriteJobs) {
    return (
      <div style={{ padding: '8px 20px' }}>
        <div style={CARD}>
          <h2 style={{ color: '#dc2626' }}>🚫 Write Access Required</h2>
          <p style={{ color: '#64748b' }}>
            You don't have write access to create new tickets. Ask a founding admin, the Management
            Team, or the Security Team to grant it.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div style={{ padding: '8px 20px' }}>
      <div style={CARD}>
        <h2 style={{ margin: '0 0 4px', color: '#0f2e5a', fontSize: 20 }}>Create New Job</h2>
        <p style={{ margin: '0 0 20px', color: '#64748b', fontSize: 13 }}>All Cloud Infra IT jobs</p>

        {/* ── AI Extract Panel ─────────────────────────────────────────────── */}
        <div style={{ background: '#f0f9ff', border: '1px solid #bae6fd', borderRadius: 9, padding: '16px 18px', marginBottom: 20 }}>
          <div style={{ fontWeight: 700, color: '#0369a1', fontSize: 14, marginBottom: 10 }}>
            🤖 AI Auto-fill — Upload a file or paste an email
          </div>
          <div style={{ fontSize: 12, color: '#0369a1', marginBottom: 12 }}>
            AI will read the content and auto-fill the Title, Description, Category and Priority fields below.
          </div>

          {/* Tab switcher */}
          <div style={{ display: 'flex', gap: 0, marginBottom: 14, borderRadius: 6, overflow: 'hidden', border: '1px solid #bae6fd', width: 'fit-content' }}>
            {[['file', '📎 Upload File'], ['email', '✉️ Paste Email']].map(([tab, label]) => (
              <button key={tab} onClick={() => setAiTab(tab)}
                style={{
                  padding: '6px 16px', fontSize: 13, fontWeight: 600, cursor: 'pointer', border: 'none',
                  background: aiTab === tab ? '#0369a1' : '#e0f2fe',
                  color: aiTab === tab ? '#fff' : '#0369a1',
                }}>
                {label}
              </button>
            ))}
          </div>

          {/* File upload tab */}
          {aiTab === 'file' && (
            <div
              onDragOver={e => { e.preventDefault(); setDragOver(true) }}
              onDragLeave={() => setDragOver(false)}
              onDrop={e => { e.preventDefault(); setDragOver(false); handleFile(e.dataTransfer.files[0]) }}
              onClick={() => fileRef.current?.click()}
              style={{
                border: `2px dashed ${dragOver ? '#0369a1' : '#93c5fd'}`,
                borderRadius: 8, padding: '24px 16px', textAlign: 'center',
                cursor: 'pointer', background: dragOver ? '#e0f2fe' : '#f8fafc',
                transition: 'all 0.15s',
              }}>
              <input ref={fileRef} type="file"
                accept=".pdf,.docx,.doc,.txt,.eml,.msg,.log,.md,.csv,.xlsx,.xls,.pptx,.ppt,.html,.htm,.jpg,.jpeg,.png,.gif,.bmp,.webp"
                style={{ display: 'none' }}
                onChange={e => handleFile(e.target.files[0])} />
              <div style={{ fontSize: 28, marginBottom: 6 }}>📄</div>
              <div style={{ fontSize: 13, fontWeight: 600, color: '#1e40af' }}>
                {extracting ? '⏳ Extracting…' : 'Click or drag & drop a file here'}
              </div>
              <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>
                Supports: PDF, DOCX, TXT, EML, MSG, XLSX, PPTX, images (max 20 MB)
              </div>
            </div>
          )}

          {/* Email paste tab */}
          {aiTab === 'email' && (
            <div>
              <textarea
                value={emailText}
                onChange={e => setEmailText(e.target.value)}
                placeholder="Paste the full email content here (including Subject, From, body)…"
                style={{ ...INPUT, minHeight: 120, resize: 'vertical', fontSize: 13 }}
              />
              <button onClick={handleEmailExtract} disabled={extracting || !emailText.trim()}
                style={{
                  marginTop: 8, padding: '8px 18px', borderRadius: 7, border: 'none',
                  background: emailText.trim() && !extracting ? '#0369a1' : '#94a3b8',
                  color: '#fff', fontWeight: 600, fontSize: 13,
                  cursor: emailText.trim() && !extracting ? 'pointer' : 'default',
                }}>
                {extracting ? '⏳ Extracting…' : '🤖 Extract with AI'}
              </button>
            </div>
          )}

          {/* Feedback message */}
          {extractMsg && (
            <div style={{
              marginTop: 12, padding: '9px 12px', borderRadius: 6, fontSize: 13,
              background: extractErr ? '#fee2e2' : '#dcfce7',
              color: extractErr ? '#dc2626' : '#15803d',
              border: `1px solid ${extractErr ? '#fca5a5' : '#86efac'}`,
            }}>
              {extractMsg}
            </div>
          )}
        </div>

        {/* ── Job Form ───────────────────────────────────────────────────────── */}
        <label style={LABEL}>Title *</label>
        <input style={{ ...INPUT, borderColor: errors.title ? '#dc2626' : '#d1d5db' }}
          placeholder="Short descriptive title (max 200 chars)"
          value={form.title} onChange={e => set('title', e.target.value)} maxLength={200} />
        {errors.title && <div style={ERR}>{errors.title}</div>}

        <label style={LABEL}>Description *</label>
        <textarea style={{ ...INPUT, minHeight: 130, resize: 'vertical', borderColor: errors.description ? '#dc2626' : '#d1d5db' }}
          placeholder="Describe the job in detail — or use AI auto-fill above (max 5000 chars)"
          value={form.description} onChange={e => set('description', e.target.value)} maxLength={5000} />
        {errors.description && <div style={ERR}>{errors.description}</div>}
        <div style={{ fontSize: 11, color: '#94a3b8', textAlign: 'right' }}>{form.description.length} / 5000</div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <div>
            <label style={LABEL}>Category *</label>
            <select style={{ ...SELECT, borderColor: errors.category ? '#dc2626' : '#d1d5db' }}
              value={form.category} onChange={e => set('category', e.target.value)}>
              <option value="">— Select category —</option>
              {options.categories.map(c => <option key={c}>{c}</option>)}
            </select>
            {errors.category && <div style={ERR}>{errors.category}</div>}
          </div>
          <div>
            <label style={LABEL}>Priority *</label>
            <select style={{ ...SELECT, borderColor: errors.priority ? '#dc2626' : '#d1d5db' }}
              value={form.priority} onChange={e => set('priority', e.target.value)}>
              <option value="">— Select priority —</option>
              {options.priorities.map(p => <option key={p}>{p}</option>)}
            </select>
            {errors.priority && <div style={ERR}>{errors.priority}</div>}
          </div>
        </div>

        {form.category === 'Other' && (
          <>
            <label style={LABEL}>Other Category (describe) *</label>
            <input style={{ ...INPUT, borderColor: errors.category_other ? '#dc2626' : '#d1d5db' }}
              placeholder="Describe the category (max 200 chars)"
              value={form.category_other} onChange={e => set('category_other', e.target.value)} maxLength={200} />
            {errors.category_other && <div style={ERR}>{errors.category_other}</div>}
          </>
        )}

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <div>
            <label style={LABEL}>Assign To *</label>
            <select style={{ ...SELECT, borderColor: errors.assignee_email ? '#dc2626' : '#d1d5db' }}
              value={form.assignee_email} onChange={e => set('assignee_email', e.target.value)}>
              <option value="">— Select team member —</option>
              {options.team_members?.map(t => (
                <option key={t.email} value={t.email}>{t.name}</option>
              ))}
            </select>
            {errors.assignee_email && <div style={ERR}>{errors.assignee_email}</div>}
          </div>
          <div>
            <label style={LABEL}>Due Date (optional)</label>
            <input type="date" style={INPUT}
              value={form.due_date} onChange={e => set('due_date', e.target.value)}
              min={todayHKYmd()} />
          </div>
        </div>

        {errors.submit && (
          <div style={{ background: '#fee2e2', border: '1px solid #fca5a5', borderRadius: 6, padding: '10px 14px', marginTop: 16, color: '#dc2626', fontSize: 13 }}>
            {errors.submit}
          </div>
        )}

        {/* ── AI Analysis Preview ────────────────────────────────────────── */}
        {(analysing || aiPreview) && (
          <div style={{
            marginTop: 24,
            border: `1px solid ${aiPreview ? (SEVERITY_COLOR[aiPreview.severity] || '#374151') + '40' : '#bae6fd'}`,
            borderRadius: 9,
            overflow: 'hidden',
          }}>
            {/* Header — always visible */}
            <div
              onClick={() => setPreviewExpanded(x => !x)}
              style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                padding: '12px 16px', cursor: 'pointer',
                background: aiPreview ? (SEVERITY_COLOR[aiPreview.severity] || '#374151') + '12' : '#f0f9ff',
              }}>
              <span style={{ fontWeight: 700, fontSize: 14, color: aiPreview ? SEVERITY_COLOR[aiPreview.severity] || '#374151' : '#0369a1' }}>
                {analysing
                  ? '🤖 AI is analysing your job…'
                  : `🤖 AI Analysis Preview — ${aiPreview.cloud_platform} · ${aiPreview.severity?.toUpperCase()}`}
              </span>
              <span style={{ fontSize: 12, color: '#94a3b8' }}>{previewExpanded ? '▲ collapse' : '▼ expand'}</span>
            </div>

            {/* Body */}
            {previewExpanded && (
              <div style={{ padding: '14px 16px', background: '#fff', fontSize: 13 }}>
                {analysing ? (
                  <div style={{ color: '#64748b', textAlign: 'center', padding: '12px 0' }}>
                    ⏳ Running AWS Bedrock analysis…
                  </div>
                ) : aiPreview && (
                  <>
                    {/* Summary */}
                    <div style={{ marginBottom: 10 }}>
                      <span style={{ fontWeight: 600, color: '#374151' }}>Issue Summary: </span>
                      <span style={{ color: '#1e293b' }}>{aiPreview.issue_summary}</span>
                    </div>

                    {/* Severity badge + label */}
                    <div style={{ marginBottom: 10, display: 'flex', alignItems: 'center', gap: 8 }}>
                      <Badge text={(aiPreview.severity || '').toUpperCase()} color={SEVERITY_COLOR[aiPreview.severity] || '#374151'} />
                      <span style={{ color: '#64748b', fontSize: 12 }}>{aiPreview.severity_label}</span>
                    </div>

                    {/* Recommended steps */}
                    {aiPreview.recommended_steps?.length > 0 && (
                      <div style={{ marginBottom: 10 }}>
                        <div style={{ fontWeight: 600, color: '#374151', marginBottom: 6 }}>Recommended Steps:</div>
                        <ol style={{ margin: 0, paddingLeft: 18, color: '#1e293b', lineHeight: 1.7 }}>
                          {aiPreview.recommended_steps.map((s, i) => (
                            <li key={i}>{s}</li>
                          ))}
                        </ol>
                      </div>
                    )}

                    {/* Ticket subject preview */}
                    <div style={{ background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 6, padding: '8px 12px', fontSize: 12, color: '#475569' }}>
                      <span style={{ fontWeight: 600 }}>Suggested Ticket Subject: </span>{aiPreview.ticket_subject}
                    </div>

                    <div style={{ marginTop: 8, fontSize: 11, color: '#94a3b8' }}>
                      Generated by {aiPreview.model_used} · {aiPreview.bedrock_region} · {formatDateTimeHK(aiPreview.generated_at)} HKT
                      {aiPreview.kb_chunks_used > 0 && (
                        <span style={{ marginLeft: 8, background: '#eff6ff', color: '#1d4ed8', border: '1px solid #bfdbfe', borderRadius: 99, padding: '1px 8px', fontSize: 11, fontWeight: 600 }}>
                          🗂️ {aiPreview.kb_chunks_used} KB chunks used
                        </span>
                      )}
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        )}

        <div style={{ display: 'flex', gap: 12, marginTop: 24 }}>
          <button onClick={handleSubmit} disabled={saving}
            style={{ flex: 1, padding: '11px', borderRadius: 7, border: 'none', background: saving ? '#94a3b8' : '#1a4080', color: '#fff', fontWeight: 600, fontSize: 15, cursor: saving ? 'default' : 'pointer' }}>
            {saving ? 'Creating…' : 'Create Job'}
          </button>
          <button onClick={() => navigate('/jobs')}
            style={{ padding: '11px 20px', borderRadius: 7, border: '1px solid #d1d5db', background: '#fff', fontSize: 15, cursor: 'pointer', color: '#475569' }}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
