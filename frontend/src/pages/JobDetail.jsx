import React, { useState, useEffect } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'
import { formatDateHK, formatDateTimeHK } from '../utils/time.js'
import { COLORS, STATUS_COLOR, PRIORITY_COLOR, formatTicketNumber, caseStatusColor, caseStatusLabel, Badge, CARD, Skeleton, SkeletonLines } from '../utils/theme.js'
import Avatar from '../components/Avatar.jsx'
import { findUserPhoto } from '../utils/userLookup.js'

function Section({ title, children }) {
  return (
    <div style={{ background: '#fff', borderRadius: 10, boxShadow: '0 1px 6px rgba(0,0,0,0.07)', padding: '20px 24px', marginBottom: 16 }}>
      <h3 style={{ margin: '0 0 14px', color: '#0f2e5a', fontSize: 15, borderBottom: '1px solid #e2e8f0', paddingBottom: 10 }}>{title}</h3>
      {children}
    </div>
  )
}

function formatBytes(bytes) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let n = bytes
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${units[i]}`
}

// One attachment inside a note — resolves a fresh presigned S3 URL on
// mount (URLs expire after a few minutes, so this is never cached
// across page loads) and renders an inline <img> for photos or a
// download link/button for everything else.
function NoteAttachment({ jobId, noteId, attachment, canDelete, onDelete }) {
  const [url, setUrl] = useState(null)
  const [err, setErr] = useState(false)

  useEffect(() => {
    let cancelled = false
    api.getNoteAttachmentUrl(jobId, noteId, attachment.id)
      .then(r => { if (!cancelled) setUrl(r.url) })
      .catch(() => { if (!cancelled) setErr(true) })
    return () => { cancelled = true }
  }, [jobId, noteId, attachment.id])

  const wrapperStyle = {
    position: 'relative', display: 'inline-flex', flexDirection: 'column',
    border: '1px solid #fde68a', borderRadius: 6, overflow: 'hidden', background: '#fff',
  }

  if (attachment.is_image) {
    return (
      <div style={wrapperStyle}>
        {url ? (
          <a href={url} target="_blank" rel="noreferrer">
            <img src={url} alt={attachment.filename} style={{ maxWidth: 220, maxHeight: 220, display: 'block' }} />
          </a>
        ) : (
          <div style={{ width: 120, height: 90, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 11, color: err ? '#dc2626' : '#94a3b8' }}>
            {err ? 'Failed to load' : 'Loading…'}
          </div>
        )}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '3px 6px', fontSize: 10, color: '#92400e', background: '#fefce8' }}>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 150 }} title={attachment.filename}>{attachment.filename}</span>
          {canDelete && <button onClick={() => onDelete(attachment.id)} title="Delete attachment"
            style={{ border: 'none', background: 'none', color: '#dc2626', cursor: 'pointer', fontSize: 11, padding: 0, marginLeft: 4 }}>🗑</button>}
        </div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, border: '1px solid #fde68a', borderRadius: 6, padding: '5px 9px', background: '#fff', fontSize: 12 }}>
      📎 {url ? (
        <a href={url} download={attachment.filename} style={{ color: '#1a4080', textDecoration: 'none', fontWeight: 600 }}>
          {attachment.filename}
        </a>
      ) : (
        <span style={{ color: err ? '#dc2626' : '#94a3b8' }}>{err ? 'Failed to load link' : attachment.filename}</span>
      )}
      <span style={{ color: '#94a3b8', fontSize: 11 }}>({formatBytes(attachment.size_bytes)})</span>
      {canDelete && <button onClick={() => onDelete(attachment.id)} title="Delete attachment"
        style={{ border: 'none', background: 'none', color: '#dc2626', cursor: 'pointer', fontSize: 12, padding: 0 }}>🗑</button>}
    </div>
  )
}

export default function JobDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { user } = useAuth()
  const [job, setJob] = useState(null)
  const [transitions, setTransitions] = useState({ allowed_next: [], all_statuses: [] })
  const [loading, setLoading] = useState(true)
  const [statusChanging, setStatusChanging] = useState(false)
  const [analysing, setAnalysing] = useState(false)
  const [escalating, setEscalating] = useState(false)
  const [editDraft, setEditDraft] = useState(null)
  const [escalateErr, setEscalateErr] = useState('')
  const [editMode, setEditMode] = useState(false)
  const [editForm, setEditForm] = useState({})
  const [options, setOptions] = useState({ categories: [], priorities: [], team_members: [] })
  const [saveErr, setSaveErr] = useState('')
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteErr, setDeleteErr] = useState('')
  const [showCrStayTuned, setShowCrStayTuned] = useState(false)
  const [refreshingAmsStatus, setRefreshingAmsStatus] = useState(false)
  const [refreshAmsErr, setRefreshAmsErr] = useState('')

  // Notes state
  const [noteText, setNoteText] = useState('')
  const [savingNote, setSavingNote] = useState(false)
  const [noteErr, setNoteErr] = useState('')
  const [deletingNoteId, setDeletingNoteId] = useState(null)
  // Photos pasted into the note textarea (Ctrl+V) or files picked via the
  // "Attach file" button, staged locally and uploaded right after the
  // note itself is created (see submitNote()).
  const [pendingFiles, setPendingFiles] = useState([])
  const [uploadingAttachments, setUploadingAttachments] = useState(false)

  const load = () => {
    setLoading(true)
    Promise.all([api.getJob(id), api.getTransitions(id), api.getOptions()])
      .then(([j, t, o]) => {
        setJob(j)
        setTransitions(t)
        setOptions(o)
        setEditDraft(j.ai_draft ? { ...j.ai_draft } : null)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [id])

  const changeStatus = async (newStatus) => {
    setStatusChanging(true)
    try {
      const updated = await api.changeStatus(id, newStatus)
      setJob(updated)
      const t = await api.getTransitions(id)
      setTransitions(t)
    } catch (e) {
      alert(e?.response?.data?.detail || 'Status change failed')
    } finally { setStatusChanging(false) }
  }

  const runAI = async () => {
    setAnalysing(true)
    try {
      const updated = await api.analyseJob(id)
      setJob(updated)
      setEditDraft({ ...updated.ai_draft })
    } catch (e) {
      alert(e?.response?.data?.detail || 'AI analysis failed')
    } finally { setAnalysing(false) }
  }

  const AWS_SUPPORT_FAMILY = ['aws_support', 'both', 'aws_ams_sr', 'aws_ams_incident']

  const escalate = async (action) => {
    if (AWS_SUPPORT_FAMILY.includes(action) && !editDraft) {
      setEscalateErr('AI analysis is required before escalating to AWS Support. Click "Analyse with AI" first.')
      return
    }
    setEscalating(true)
    setEscalateErr('')
    try {
      const updated = await api.escalate(id, action, editDraft || {})
      setJob(updated)
    } catch (e) {
      setEscalateErr(e?.response?.data?.detail || 'Escalation failed')
    } finally { setEscalating(false) }
  }

  const refreshAmsStatus = async () => {
    setRefreshingAmsStatus(true)
    setRefreshAmsErr('')
    try {
      const updated = await api.refreshAmsStatus(id)
      setJob(updated)
    } catch (e) {
      setRefreshAmsErr(e?.response?.data?.detail || 'Could not refresh AWS AMS case status')
    } finally { setRefreshingAmsStatus(false) }
  }

  // AWS Support/AMS raw status strings (e.g. "opened",
  // "pending-customer-action", "resolved") -> badge color/label, shared
  // with the All Cases page via utils/theme.js.
  const amsStatusColor = caseStatusColor
  const amsStatusLabel = caseStatusLabel

  const startEdit = () => {
    setEditForm({
      title: job.title, description: job.description,
      category: job.category, category_other: job.category_other || '',
      priority: job.priority, assignee_email: job.assignee_email,
      due_date: job.due_date || '',
    })
    setEditMode(true)
    setSaveErr('')
  }

  const saveEdit = async () => {
    setSaveErr('')
    try {
      const updated = await api.updateJob(id, {
        title: editForm.title, description: editForm.description,
        category: editForm.category, category_other: editForm.category_other || undefined,
        priority: editForm.priority, assignee_email: editForm.assignee_email,
        due_date: editForm.due_date || null,
      })
      setJob(updated)
      setEditMode(false)
    } catch (e) {
      setSaveErr(e?.response?.data?.detail || 'Save failed')
    }
  }

  const submitNote = async () => {
    if (!noteText.trim()) return
    setSavingNote(true)
    setNoteErr('')
    try {
      const updated = await api.addNote(id, noteText.trim())
      const newNoteId = updated.notes[updated.notes.length - 1].id
      let finalJob = updated
      if (pendingFiles.length > 0) {
        setUploadingAttachments(true)
        try {
          finalJob = await api.addNoteAttachments(id, newNoteId, pendingFiles)
        } catch (e) {
          setNoteErr(e.message || 'Note saved, but attachment upload failed')
        } finally {
          setUploadingAttachments(false)
        }
      }
      setJob(finalJob)
      setNoteText('')
      setPendingFiles([])
    } catch (e) {
      setNoteErr(e?.response?.data?.detail || 'Failed to save note')
    } finally {
      setSavingNote(false)
    }
  }

  // Ctrl+V into the note textarea: if the clipboard carries an image
  // (screenshot copy-paste is the common case), stage it as a pending
  // file instead of letting the browser insert garbage text/nothing.
  const handleNotePaste = (e) => {
    const items = e.clipboardData?.items || []
    const imageFiles = []
    for (const item of items) {
      if (item.type && item.type.startsWith('image/')) {
        const file = item.getAsFile()
        if (file) {
          const ext = item.type.split('/')[1] || 'png'
          imageFiles.push(new File([file], `pasted-image-${Date.now()}.${ext}`, { type: item.type }))
        }
      }
    }
    if (imageFiles.length > 0) {
      e.preventDefault()
      setPendingFiles(prev => [...prev, ...imageFiles])
    }
  }

  const handleFilePick = (e) => {
    const picked = Array.from(e.target.files || [])
    if (picked.length > 0) setPendingFiles(prev => [...prev, ...picked])
    e.target.value = ''
  }

  const removePendingFile = (idx) => {
    setPendingFiles(prev => prev.filter((_, i) => i !== idx))
  }

  const removeNote = async (noteId) => {
    try {
      const updated = await api.deleteNote(id, noteId)
      setJob(updated)
    } catch (e) {
      alert(e?.response?.data?.detail || 'Failed to delete note')
    } finally {
      setDeletingNoteId(null)
    }
  }

  const removeNoteAttachment = async (noteId, attachmentId) => {
    try {
      const updated = await api.deleteNoteAttachment(id, noteId, attachmentId)
      setJob(updated)
    } catch (e) {
      alert(e?.response?.data?.detail || 'Failed to delete attachment')
    }
  }

  const removeJob = async () => {
    setDeleting(true)
    setDeleteErr('')
    try {
      await api.deleteJob(id)
      navigate('/jobs')
    } catch (e) {
      setDeleteErr(e?.response?.data?.detail || 'Failed to delete job')
      setDeleting(false)
    }
  }

  // Skeleton shaped like a ticket-detail page (a header line + a couple
  // of section cards) instead of one centered "Loading…" line.
  if (loading) return (
    <div style={{ maxWidth: 1000, margin: '0 auto', padding: '24px 24px 40px' }}>
      <Skeleton width={220} height={22} style={{ marginBottom: 20 }} />
      <div style={{ ...CARD, padding: '20px 24px', marginBottom: 16 }}><SkeletonLines lines={4} /></div>
      <div style={{ ...CARD, padding: '20px 24px' }}><SkeletonLines lines={3} /></div>
    </div>
  )
  if (!job) return <div style={{ padding: 40, textAlign: 'center', color: '#dc2626' }}>Job not found</div>

  // Edit/Delete are restricted to the job's creator (or an admin) — mirrors
  // the server-side check in main.py's update_job()/delete_job(). The
  // frontend check is purely UX (hide buttons that would 403); the backend
  // is the actual enforcement point regardless of what's shown here.
  const canManage = job.created_by_email === user?.email || user?.is_admin
  const INPUT = { width: '100%', padding: '7px 10px', borderRadius: 6, border: '1px solid #d1d5db', fontSize: 13, boxSizing: 'border-box' }

  return (
    <div style={{ maxWidth: 860, margin: '0 auto', padding: '20px' }}>
      {/* Breadcrumb */}
      <div style={{ marginBottom: 14, fontSize: 13, color: '#64748b' }}>
        <Link to="/jobs" style={{ color: '#2563eb', textDecoration: 'none' }}>← Jobs</Link> / {job.title}
      </div>

      {/* Header */}
      <div style={{ background: '#fff', borderRadius: 10, boxShadow: '0 1px 6px rgba(0,0,0,0.07)', padding: '20px 24px', marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
          <div>
            <div style={{ fontFamily: 'monospace', fontWeight: 700, color: COLORS.brand, fontSize: 13, marginBottom: 4 }}>
              {formatTicketNumber(job)}
            </div>
            {editMode ? (
              <input style={{ ...INPUT, fontSize: 16, fontWeight: 500, color: '#0f2e5a', fontFamily: 'monospace', letterSpacing: -0.3 }}
                value={editForm.title} onChange={e => setEditForm(f => ({ ...f, title: e.target.value }))} maxLength={200} />
            ) : (
              <h2 style={{ margin: '0 0 8px', color: '#0f2e5a', fontSize: 17, fontFamily: 'monospace', fontWeight: 500, letterSpacing: -0.3 }}>{job.title}</h2>
            )}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <Badge text={job.status} color={STATUS_COLOR[job.status] || '#6b7280'} />
              <Badge text={job.priority} color={PRIORITY_COLOR[job.priority] || '#6b7280'} />
              <span style={{ fontSize: 12, color: '#64748b' }}>{job.category}{job.category === 'Other' && job.category_other ? ` (${job.category_other})` : ''}</span>
              {job.due_date && <span style={{ fontSize: 12, color: '#64748b' }}>Due: {job.due_date}</span>}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            {!editMode && canManage && job.status !== 'Closed' && (
              <button onClick={startEdit} style={{ padding: '7px 14px', borderRadius: 6, border: '1px solid #d1d5db', background: '#fff', fontSize: 13, cursor: 'pointer' }}>Edit</button>
            )}
            {!editMode && canManage && !confirmingDelete && (
              <button onClick={() => setConfirmingDelete(true)}
                style={{ padding: '7px 14px', borderRadius: 6, border: '1px solid #fca5a5', background: '#fff', color: '#dc2626', fontSize: 13, cursor: 'pointer' }}>
                🗑 Delete
              </button>
            )}
          </div>
        </div>

        {confirmingDelete && (
          <div style={{ marginTop: 14, background: '#fef2f2', border: '1px solid #fca5a5', borderRadius: 8, padding: '14px 16px' }}>
            <div style={{ fontSize: 13, color: '#991b1b', fontWeight: 600, marginBottom: 4 }}>
              Delete this job permanently?
            </div>
            <div style={{ fontSize: 12.5, color: '#7f1d1d', marginBottom: 10 }}>
              This removes the job and its notes/audit history entirely — for cases created by
              mistake. This cannot be undone. Any Freshservice/AWS Support tickets already raised
              are not cancelled automatically.
            </div>
            {deleteErr && <div style={{ color: '#dc2626', fontSize: 12, marginBottom: 8 }}>{deleteErr}</div>}
            <div style={{ display: 'flex', gap: 8 }}>
              <button onClick={removeJob} disabled={deleting}
                style={{ padding: '7px 16px', borderRadius: 6, border: 'none', background: deleting ? '#94a3b8' : '#dc2626', color: '#fff', fontSize: 13, fontWeight: 600, cursor: deleting ? 'default' : 'pointer' }}>
                {deleting ? 'Deleting…' : 'Yes, delete permanently'}
              </button>
              <button onClick={() => { setConfirmingDelete(false); setDeleteErr('') }} disabled={deleting}
                style={{ padding: '7px 14px', borderRadius: 6, border: '1px solid #d1d5db', background: '#fff', fontSize: 13, cursor: 'pointer' }}>
                Cancel
              </button>
            </div>
          </div>
        )}

        {editMode && (
          <div style={{ marginTop: 14 }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 10 }}>
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 4 }}>Category</div>
                <select style={{ ...INPUT, background: '#fff' }} value={editForm.category} onChange={e => setEditForm(f => ({ ...f, category: e.target.value }))}>
                  {options.categories.map(c => <option key={c}>{c}</option>)}
                </select>
              </div>
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 4 }}>Priority</div>
                <select style={{ ...INPUT, background: '#fff' }} value={editForm.priority} onChange={e => setEditForm(f => ({ ...f, priority: e.target.value }))}>
                  {options.priorities.map(p => <option key={p}>{p}</option>)}
                </select>
              </div>
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 4 }}>Assignee</div>
                <select style={{ ...INPUT, background: '#fff' }} value={editForm.assignee_email} onChange={e => setEditForm(f => ({ ...f, assignee_email: e.target.value }))}>
                  {options.team_members?.map(t => <option key={t.email} value={t.email}>{t.name}</option>)}
                </select>
              </div>
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 4 }}>Due Date</div>
                <input type="date" style={INPUT} value={editForm.due_date} onChange={e => setEditForm(f => ({ ...f, due_date: e.target.value }))} />
              </div>
            </div>
            {editForm.category === 'Other' && (
              <input style={{ ...INPUT, marginBottom: 10 }} placeholder="Other category description" value={editForm.category_other}
                onChange={e => setEditForm(f => ({ ...f, category_other: e.target.value }))} maxLength={200} />
            )}
            <div style={{ fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 4 }}>Description</div>
            <textarea style={{ ...INPUT, minHeight: 80, resize: 'vertical', marginBottom: 10 }}
              value={editForm.description} onChange={e => setEditForm(f => ({ ...f, description: e.target.value }))} maxLength={5000} />
            {saveErr && <div style={{ color: '#dc2626', fontSize: 12, marginBottom: 8 }}>{saveErr}</div>}
            <div style={{ display: 'flex', gap: 8 }}>
              <button onClick={saveEdit} style={{ padding: '7px 16px', borderRadius: 6, border: 'none', background: '#1a4080', color: '#fff', fontSize: 13, fontWeight: 600, cursor: 'pointer' }}>Save Changes</button>
              <button onClick={() => setEditMode(false)} style={{ padding: '7px 14px', borderRadius: 6, border: '1px solid #d1d5db', background: '#fff', fontSize: 13, cursor: 'pointer' }}>Cancel</button>
            </div>
          </div>
        )}

        {!editMode && (
          <div style={{ marginTop: 14, background: '#f8fafc', borderRadius: 7, padding: '12px 14px', fontSize: 13, color: '#374151', whiteSpace: 'pre-wrap' }}>
            {job.description}
          </div>
        )}

        <div style={{ marginTop: 12, display: 'flex', gap: 20, alignItems: 'center', fontSize: 12, color: '#64748b' }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Avatar photoUrl={findUserPhoto(options.team_members, job.assignee_email)}
              name={job.assignee_name} email={job.assignee_email} size={26} fontSize={12} />
            Assigned to: <strong style={{ color: '#374151' }}>{job.assignee_name}</strong>
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Avatar photoUrl={findUserPhoto(options.team_members, job.created_by_email)}
              name={job.created_by_name} email={job.created_by_email} size={26} fontSize={12} />
            Created by: <strong style={{ color: '#374151' }}>{job.created_by_name}</strong>
          </span>
          <span>Created: <strong style={{ color: '#374151' }}>{formatDateHK(job.created_at)}</strong></span>
        </div>
      </div>

      {/* Status Transitions */}
      <Section title="Update Status">
        <div style={{ fontSize: 12, color: '#64748b', marginBottom: 10 }}>
          Current: <Badge text={job.status} color={STATUS_COLOR[job.status] || '#6b7280'} />
          {transitions.allowed_next.length === 0 && <span style={{ marginLeft: 8, color: '#94a3b8' }}>No further transitions available</span>}
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {transitions.all_statuses.filter(s => s !== job.status).map(s => {
            const allowed = transitions.allowed_next.includes(s)
            return (
              <button key={s} onClick={() => allowed && changeStatus(s)} disabled={!allowed || statusChanging}
                title={!allowed ? `Cannot transition from '${job.status}' to '${s}'. Allowed: ${transitions.allowed_next.join(', ') || 'none'}` : ''}
                style={{
                  padding: '7px 14px', borderRadius: 6, fontSize: 13, cursor: allowed ? 'pointer' : 'not-allowed',
                  border: `1px solid ${allowed ? (STATUS_COLOR[s] || '#6b7280') : '#e2e8f0'}`,
                  background: allowed ? 'transparent' : '#f8fafc',
                  color: allowed ? (STATUS_COLOR[s] || '#6b7280') : '#cbd5e1',
                  fontWeight: allowed ? 600 : 400,
                  opacity: allowed ? 1 : 0.5,
                }}>
                → {s}
              </button>
            )
          })}
        </div>
      </Section>

      {/* AI Analysis */}
      <Section title="🤖 AI Analysis (AWS Bedrock)">
        {!job.ai_draft ? (
          <div>
            <p style={{ fontSize: 13, color: '#64748b', marginTop: 0 }}>
              Click below to let AI analyse this job and prepare a support ticket draft.
              The AI will suggest a severity, categorise the issue, and recommend solutions.
            </p>
            <button onClick={runAI} disabled={analysing}
              style={{ padding: '9px 18px', borderRadius: 7, border: 'none', background: analysing ? '#94a3b8' : '#7c3aed', color: '#fff', fontWeight: 600, fontSize: 14, cursor: analysing ? 'default' : 'pointer' }}>
              {analysing ? '⏳ Analysing…' : '🔍 Analyse with AI'}
            </button>
          </div>
        ) : (
          <div>
            <div style={{ background: '#f0f9ff', border: '1px solid #bae6fd', borderRadius: 7, padding: '12px 14px', marginBottom: 14, fontSize: 13 }}>
              <div style={{ fontWeight: 700, color: '#0369a1', marginBottom: 6 }}>
                AI Analysis Complete — {formatDateTimeHK(job.ai_analysed_at)} HKT
                {editDraft?.model_used && <span style={{ marginLeft: 10, fontSize: 11, background: '#1a4080', color: '#fff', borderRadius: 3, padding: '2px 7px', fontWeight: 400 }}>{editDraft.model_used}</span>}
              </div>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 8 }}>
                <span><strong>Issue Type:</strong> {editDraft?.issue_type}</span>
                {editDraft?.cloud_platform && (
                  <span style={{
                    background: editDraft.cloud_platform === 'Azure' ? '#e0f2fe' : editDraft.cloud_platform === 'Aliyun' ? '#fef3c7' : '#dcfce7',
                    color: editDraft.cloud_platform === 'Azure' ? '#0369a1' : editDraft.cloud_platform === 'Aliyun' ? '#92400e' : '#15803d',
                    border: `1px solid ${editDraft.cloud_platform === 'Azure' ? '#bae6fd' : editDraft.cloud_platform === 'Aliyun' ? '#fde68a' : '#86efac'}`,
                    borderRadius: 4, padding: '1px 8px', fontSize: 12, fontWeight: 600,
                  }}>
                    ☁️ {editDraft.cloud_platform}
                  </span>
                )}
                <span><strong>Severity:</strong> <span style={{ textTransform: 'capitalize', fontWeight: 600 }}>{editDraft?.severity}</span> — {editDraft?.severity_label}</span>
              </div>
              <div><strong>Recommended Steps:</strong>
                <ol style={{ margin: '4px 0 0 16px', padding: 0 }}>
                  {(editDraft?.recommended_steps || []).map((s, i) => <li key={i} style={{ marginBottom: 2 }}>{s}</li>)}
                </ol>
              </div>
            </div>

            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 4 }}>Ticket Subject (editable)</div>
              <input style={{ width: '100%', padding: '7px 10px', borderRadius: 6, border: '1px solid #d1d5db', fontSize: 13, boxSizing: 'border-box' }}
                value={editDraft?.ticket_subject || ''} onChange={e => setEditDraft(d => ({ ...d, ticket_subject: e.target.value }))} maxLength={100} />
            </div>
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 4 }}>Ticket Description (editable)</div>
              <textarea style={{ width: '100%', padding: '7px 10px', borderRadius: 6, border: '1px solid #d1d5db', fontSize: 13, minHeight: 120, resize: 'vertical', boxSizing: 'border-box' }}
                value={editDraft?.ticket_description || ''} onChange={e => setEditDraft(d => ({ ...d, ticket_description: e.target.value }))} />
            </div>

            <button onClick={runAI} disabled={analysing}
              style={{ padding: '7px 14px', borderRadius: 6, border: '1px solid #7c3aed', background: 'transparent', color: '#7c3aed', fontSize: 13, fontWeight: 600, cursor: analysing ? 'default' : 'pointer' }}>
              {analysing ? '⏳ Re-analysing…' : '🔄 Re-analyse'}
            </button>
          </div>
        )}
      </Section>

      {/* Escalation */}
      <Section title="🚀 Escalation Actions">
        {job.aws_support_case_id && (
          <div style={{ background: '#f0f9ff', border: '1px solid #bae6fd', borderRadius: 7, padding: '10px 14px', marginBottom: 12, fontSize: 13 }}>
            ✅ <strong>AWS Support:</strong> Case ID <code style={{ background: '#e0f2fe', padding: '1px 6px', borderRadius: 3 }}>{job.aws_support_case_display_id || job.aws_support_case_id}</code>
            <a href={`https://support.console.aws.amazon.com/support/home#/case/?caseId=${job.aws_support_case_id}`}
              target="_blank" rel="noreferrer" style={{ marginLeft: 8, color: '#2563eb', fontSize: 12 }}>Open in AWS Console ↗</a>
          </div>
        )}
        {job.freshservice_ticket_id && (
          <div style={{ background: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: 7, padding: '10px 14px', marginBottom: 12, fontSize: 13 }}>
            ✅ <strong>Freshservice Ticket:</strong> <code style={{ background: '#dcfce7', padding: '1px 6px', borderRadius: 3 }}>{job.freshservice_ticket_id}</code>
            <a href={job.freshservice_ticket_url} target="_blank" rel="noreferrer" style={{ marginLeft: 8, color: '#16a34a', fontSize: 12 }}>Open in Freshservice ↗</a>
          </div>
        )}
        {(job.aws_ams_sr_case_id || job.aws_ams_incident_case_id) && (
          <div style={{ marginBottom: 12 }}>
            {job.aws_ams_sr_case_id && (
              <div style={{ background: '#faf5ff', border: '1px solid #e9d5ff', borderRadius: 7, padding: '10px 14px', marginBottom: 8, fontSize: 13, display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                <span>✅ <strong>AWS AMS Service Request:</strong> Case ID <code style={{ background: '#f3e8ff', padding: '1px 6px', borderRadius: 3 }}>{job.aws_ams_sr_case_display_id || job.aws_ams_sr_case_id}</code></span>
                <Badge text={amsStatusLabel(job.aws_ams_sr_case_status)} color={amsStatusColor(job.aws_ams_sr_case_status)} />
                <span style={{ color: '#9333ea', fontSize: 12 }}>AMS account</span>
                <a href={`https://support.console.aws.amazon.com/support/home#/case/?caseId=${job.aws_ams_sr_case_id}`}
                  target="_blank" rel="noreferrer" style={{ color: '#9333ea', fontSize: 12 }}>Open in AWS Console ↗</a>
              </div>
            )}
            {job.aws_ams_incident_case_id && (
              <div style={{ background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 7, padding: '10px 14px', marginBottom: 8, fontSize: 13, display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                <span>✅ <strong>AWS AMS Incident:</strong> Case ID <code style={{ background: '#fee2e2', padding: '1px 6px', borderRadius: 3 }}>{job.aws_ams_incident_case_display_id || job.aws_ams_incident_case_id}</code></span>
                <Badge text={amsStatusLabel(job.aws_ams_incident_case_status)} color={amsStatusColor(job.aws_ams_incident_case_status)} />
                <span style={{ color: '#dc2626', fontSize: 12 }}>AMS account</span>
                <a href={`https://support.console.aws.amazon.com/support/home#/case/?caseId=${job.aws_ams_incident_case_id}`}
                  target="_blank" rel="noreferrer" style={{ color: '#dc2626', fontSize: 12 }}>Open in AWS Console ↗</a>
              </div>
            )}
            <button onClick={refreshAmsStatus} disabled={refreshingAmsStatus}
              style={{ padding: '5px 12px', borderRadius: 6, border: '1px solid #94a3b8', background: 'transparent', color: '#475569', fontSize: 12, fontWeight: 600, cursor: refreshingAmsStatus ? 'default' : 'pointer' }}>
              {refreshingAmsStatus ? '⏳ Refreshing…' : '🔄 Refresh AMS Status'}
            </button>
            {refreshAmsErr && (
              <div style={{ marginTop: 6, fontSize: 12, color: '#dc2626' }}>{refreshAmsErr}</div>
            )}
          </div>
        )}

        {escalateErr && (
          <div style={{ background: '#fee2e2', border: '1px solid #fca5a5', borderRadius: 6, padding: '10px 14px', marginBottom: 12, color: '#dc2626', fontSize: 13 }}>
            {escalateErr}
          </div>
        )}

        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <button onClick={() => escalate('freshservice')} disabled={escalating}
            style={{ padding: '9px 16px', borderRadius: 7, border: '1px solid #16a34a', background: 'transparent', color: '#16a34a', fontWeight: 600, fontSize: 13, cursor: escalating ? 'default' : 'pointer' }}>
            📋 Raise in Freshservice
          </button>
          <button onClick={() => escalate('aws_support')} disabled={escalating}
            style={{ padding: '9px 16px', borderRadius: 7, border: '1px solid #1a4080', background: 'transparent', color: '#1a4080', fontWeight: 600, fontSize: 13, cursor: escalating ? 'default' : 'pointer' }}>
            ☁️ Escalate to AWS Support
          </button>
          <button onClick={() => escalate('aws_ams_sr')} disabled={escalating}
            title="Raises a Service Request directly in the AWS AMS support account"
            style={{ padding: '9px 16px', borderRadius: 7, border: '1px solid #9333ea', background: 'transparent', color: '#9333ea', fontWeight: 600, fontSize: 13, cursor: escalating ? 'default' : 'pointer' }}>
            🛡️ Raise AMS Service Request
          </button>
          <button onClick={() => escalate('aws_ams_incident')} disabled={escalating}
            title="Raises an Incident directly in the AWS AMS support account"
            style={{ padding: '9px 16px', borderRadius: 7, border: '1px solid #dc2626', background: 'transparent', color: '#dc2626', fontWeight: 600, fontSize: 13, cursor: escalating ? 'default' : 'pointer' }}>
            🚨 Raise AMS Incident
          </button>
          <button onClick={() => escalate('both')} disabled={escalating}
            style={{ padding: '9px 16px', borderRadius: 7, border: 'none', background: '#1a4080', color: '#fff', fontWeight: 600, fontSize: 13, cursor: escalating ? 'default' : 'pointer' }}>
            {escalating ? '⏳ Escalating…' : '🚀 Raise in Freshservice AND Escalate to AWS Support'}
          </button>
          <button onClick={() => setShowCrStayTuned(v => !v)}
            title="Raise Change Request (CR) in Freshservice — in development"
            style={{ padding: '9px 16px', borderRadius: 7, border: '1px solid #d97706', background: 'transparent', color: '#d97706', fontWeight: 600, fontSize: 13, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            🛠️ Raise Change Request (CR) in Freshservice
            <span style={{ background: '#fef3c7', color: '#92400e', borderRadius: 4, padding: '1px 6px', fontSize: 10, fontWeight: 700, letterSpacing: 0.3 }}>
              STAY TUNED
            </span>
          </button>
        </div>
        <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 8 }}>
          AWS Support escalation requires AI analysis to be run first.
        </div>

        {showCrStayTuned && (
          <div style={{ marginTop: 12, background: '#fffbeb', border: '1px solid #fde68a', borderRadius: 8, padding: '12px 16px' }}>
            <div style={{ fontSize: 13, color: '#92400e', fontWeight: 600, marginBottom: 4 }}>
              🛠️ Stay tuned — Raise CR in Freshservice is still being built
            </div>
            <div style={{ fontSize: 12.5, color: '#78350f', lineHeight: 1.5 }}>
              We're wiring up the Freshservice Change Request API for this portal. Requester, Group,
              and Agent will pre-fill automatically, Subject/Description/Risk fields will be
              AI-drafted from the job's analysis (editable before submit), and the rest you'll pick
              from dropdowns — same as raising a ticket today. This button will go live once
              Freshservice grants the required "Manage Changes" API permission on our side.
            </div>
          </div>
        )}
      </Section>

      {/* Notes */}
      <Section title="📝 Engineer Notes">
        <div style={{ fontSize: 12, color: '#64748b', marginBottom: 12 }}>
          Add notes about how this issue was handled. Notes are searchable by AI for future similar cases.
        </div>

        {/* Existing notes */}
        {(job.notes || []).length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 16 }}>
            {[...(job.notes || [])].reverse().map(note => (
              <div key={note.id} style={{
                background: '#fefce8', border: '1px solid #fde68a', borderRadius: 8,
                padding: '12px 14px', position: 'relative',
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8 }}>
                  <Avatar photoUrl={findUserPhoto(options.team_members, note.author_email)}
                    name={note.author} email={note.author_email} size={34} fontSize={14} />
                  <div style={{ fontSize: 13, color: '#1e293b', whiteSpace: 'pre-wrap', flex: 1 }}>
                    {note.text}
                    {(note.attachments || []).length > 0 && (
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 10 }}>
                        {note.attachments.map(att => (
                          <NoteAttachment key={att.id} jobId={id} noteId={note.id} attachment={att}
                            canDelete={att.uploaded_by_email === user?.email || note.author_email === user?.email}
                            onDelete={(attId) => removeNoteAttachment(note.id, attId)} />
                        ))}
                      </div>
                    )}
                  </div>
                  {note.author_email === user?.email && (
                    <div style={{ flexShrink: 0 }}>
                      {deletingNoteId === note.id ? (
                        <div style={{ display: 'flex', gap: 6 }}>
                          <button onClick={() => removeNote(note.id)}
                            style={{ padding: '3px 8px', borderRadius: 4, border: 'none', background: '#dc2626', color: '#fff', fontSize: 11, cursor: 'pointer' }}>
                            Delete
                          </button>
                          <button onClick={() => setDeletingNoteId(null)}
                            style={{ padding: '3px 8px', borderRadius: 4, border: '1px solid #d1d5db', background: '#fff', fontSize: 11, cursor: 'pointer' }}>
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <button onClick={() => setDeletingNoteId(note.id)}
                          style={{ padding: '3px 8px', borderRadius: 4, border: '1px solid #fca5a5', background: 'transparent', color: '#dc2626', fontSize: 11, cursor: 'pointer' }}>
                          🗑
                        </button>
                      )}
                    </div>
                  )}
                </div>
                <div style={{ marginTop: 8, fontSize: 11, color: '#92400e', display: 'flex', gap: 12, alignItems: 'center' }}>
                  <span>✍️ {note.author}</span>
                  <span>{formatDateTimeHK(note.created_at)} HKT</span>
                  <span style={{ background: '#fef3c7', border: '1px solid #fde68a', borderRadius: 99, padding: '1px 7px', fontSize: 10, fontWeight: 600, color: '#92400e' }}>
                    🗂️ auto-indexed to KB
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Add note box */}
        <div style={{ background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 8, padding: '14px 16px' }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: '#374151', marginBottom: 6 }}>
            Add a Note
          </div>
          <textarea
            value={noteText}
            onChange={e => setNoteText(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submitNote() }}
            onPaste={handleNotePaste}
            placeholder="Document how you resolved the issue, workarounds applied, commands used, lessons learned… Paste (Ctrl+V) a screenshot to attach it. (Ctrl+Enter to save)"
            style={{ width: '100%', minHeight: 90, padding: '8px 10px', borderRadius: 6, border: '1px solid #d1d5db', fontSize: 13, resize: 'vertical', boxSizing: 'border-box' }}
            maxLength={5000}
          />

          {pendingFiles.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
              {pendingFiles.map((f, idx) => (
                <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: 6, background: '#eef2ff', border: '1px solid #c7d2fe', borderRadius: 6, padding: '4px 8px', fontSize: 12 }}>
                  {f.type.startsWith('image/') ? '🖼️' : '📎'}
                  <span style={{ maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={f.name}>{f.name}</span>
                  <span style={{ color: '#94a3b8' }}>({formatBytes(f.size)})</span>
                  <button onClick={() => removePendingFile(idx)} style={{ border: 'none', background: 'none', color: '#dc2626', cursor: 'pointer', fontSize: 12, padding: 0 }}>✕</button>
                </div>
              ))}
            </div>
          )}

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 8 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{noteText.length} / 5000</span>
              <label style={{ fontSize: 12, color: '#1a4080', cursor: 'pointer', border: '1px solid #cbd5e1', borderRadius: 6, padding: '5px 10px' }}>
                📎 Attach file
                <input type="file" multiple onChange={handleFilePick} style={{ display: 'none' }} />
              </label>
            </div>
            <button
              onClick={submitNote}
              disabled={savingNote || uploadingAttachments || !noteText.trim()}
              style={{
                padding: '7px 16px', borderRadius: 6, border: 'none', fontSize: 13, fontWeight: 600, cursor: (savingNote || uploadingAttachments || !noteText.trim()) ? 'default' : 'pointer',
                background: (savingNote || uploadingAttachments || !noteText.trim()) ? '#94a3b8' : '#1a4080', color: '#fff',
              }}>
              {savingNote ? '⏳ Saving…' : uploadingAttachments ? '⏳ Uploading attachments…' : '💾 Save Note'}
            </button>
          </div>
          {noteErr && <div style={{ color: '#dc2626', fontSize: 12, marginTop: 6 }}>{noteErr}</div>}
        </div>
      </Section>

      {/* Audit Log */}
      <Section title="Audit History">
        {(job.audit_log || []).length === 0 ? (
          <div style={{ color: '#94a3b8', fontSize: 13 }}>No events yet</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {[...job.audit_log].reverse().map((entry, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 12, padding: '7px 10px', background: '#f8fafc', borderRadius: 5 }}>
                <span style={{ color: '#94a3b8', whiteSpace: 'nowrap' }}>{formatDateTimeHK(entry.timestamp)} HKT</span>
                <Avatar photoUrl={findUserPhoto(options.team_members, entry.actor_email)}
                  name={entry.actor} email={entry.actor_email} size={26} fontSize={12} />
                <span style={{ color: '#0891b2', fontWeight: 600 }}>{entry.actor}</span>
                <span style={{ color: '#374151' }}>{entry.event}</span>
              </div>
            ))}
          </div>
        )}
      </Section>
    </div>
  )
}
