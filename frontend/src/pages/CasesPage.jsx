import React, { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { formatDateTimeHK } from '../utils/time.js'
import { COLORS, CARD, caseStatusColor, caseStatusLabel, Badge, tabButtonStyle, Skeleton, SkeletonLines } from '../utils/theme.js'
import Avatar from '../components/Avatar.jsx'
import { findUserPhotoByName } from '../utils/userLookup.js'
import { useAuth } from '../auth/AuthContext.jsx'
import useIsMobile from '../utils/useIsMobile.js'

const CARDS_MAX_WIDTH = 1100

const ACCOUNT_LABEL = {
  own: 'Own Account',
  ams: 'AMS',
}
const ACCOUNT_COLOR = {
  own: COLORS.brand,
  ams: '#9333ea',
}

// Case-title check for AWS's own auto-generated "Incident Detection and
// Response" cases. AWS wraps this in decorative brackets — observed as
// "[o~o] AWS Incident Detection and Response [o~o]" — but normalizing
// away everything except letters/digits before matching means we still
// catch it even if AWS tweaks the decoration/spacing/case in a future
// case, or if it appears mid-subject rather than strictly at the start.
function isIdrCase(subject) {
  const normalized = (subject || '').toLowerCase().replace(/[^a-z0-9]/g, '')
  return normalized.includes('awsincidentdetectionandresponse')
}

// AWS Security Incident Response ("SIR") cases. Unlike IDR (which is only
// recognisable by its subject), these carry a dedicated support service
// code, which GET /api/cases returns as `service_code` for both accounts
// — so match on that exactly rather than guessing from the subject.
const SIR_SERVICE_CODE = 'service-security-incident-response'

// The SIR console also has its OWN native case system (10-digit IDs) that
// the Support API never returns; the backend merges those into this list
// with source === 'security_ir'. Both kinds belong in the SIR tab.
const SIR_NATIVE_SOURCE = 'security_ir'

function isSirCase(c) {
  return c.source === SIR_NATIVE_SOURCE || c.service_code === SIR_SERVICE_CODE
}

// Native SIR case lifecycle (not Support API statuses, so the shared
// caseStatusColor() would render all of these gray).
const SIR_STATUS_COLOR = {
  'Submitted': '#2563eb',
  'Acknowledged': '#2563eb',
  'Detection and Analysis': '#ea580c',
  'Containment, Eradication and Recovery': '#dc2626',
  'Post-incident Activities': '#0891b2',
  'Ready to Close': '#16a34a',
  'Closed': '#6b7280',
}

// Returns { text, color } for a status badge, for either kind of case.
function statusBadge(c) {
  if (c.source === SIR_NATIVE_SOURCE) {
    return { text: c.status || 'Unknown', color: SIR_STATUS_COLOR[c.status] || '#6b7280' }
  }
  return { text: caseStatusLabel(c.status), color: caseStatusColor(c.status) }
}

function isResolvedCase(status) {
  return (status || '').toLowerCase() === 'resolved'
}

// Buckets every case into exactly one of the four tabs:
//  - "idr":      subject matches the AWS IDR pattern, regardless of
//                status — it's a permanent category, not a status bucket,
//                so a resolved IDR case still belongs here (it's still
//                IDR-flavored noise we don't want cluttering All Cases).
//  - "sir":      AWS Security Incident Response case (by service code),
//                regardless of status and account — also a permanent
//                category, so a resolved SIR case stays here rather than
//                dropping into Resolved. Checked after IDR so a case can
//                never be counted in two tabs.
//  - "resolved": status is Resolved and it's neither IDR nor SIR.
//  - "all":      everything else (i.e. still-open/pending cases, plus any
//                previously-resolved case that AWS reopened or that got a
//                reply — AWS Support automatically flips status away from
//                "resolved" once either side replies, so a re-opened case
//                falls out of "resolved" and lands back here on its own,
//                no extra bookkeeping needed).
function caseTab(c) {
  if (isIdrCase(c.subject)) return 'idr'
  if (isSirCase(c)) return 'sir'
  if (isResolvedCase(c.status)) return 'resolved'
  return 'all'
}

const TABS = [
  { key: 'all', label: 'All Cases', icon: '🎟️' },
  { key: 'resolved', label: 'Resolved', icon: '✅' },
  { key: 'idr', label: 'IDR', icon: '🤖' },
  { key: 'sir', label: 'SIR', icon: '🛡️' },
]

// ── List view ─────────────────────────────────────────────────────────────
export default function CasesPage() {
  const { user } = useAuth()
  // Per-user tab WRITE grant for "cases" (independent of whether this
  // user can even SEE this tab — see database.py's PortalUser.tab_write
  // docstring). Full admins always have every tab_id here (see
  // main.py's whoami()), so this naturally stays true for them too.
  const canWriteCases = (user?.tab_write || []).includes('cases')
  // The 8-column table needs ~1100px+ of content width; below that (phones, and
  // tablets where the sidebar is still open) show one card per case instead.
  const isMobile = useIsMobile(CARDS_MAX_WIDTH)
  const [data, setData] = useState({ cases: [], count: 0 })
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [selected, setSelected] = useState(null) // { case_id, account } | null
  const [tab, setTab] = useState('all')
  const [showCreate, setShowCreate] = useState(false)
  // Used only to resolve a "Raised By" avatar photo for a portal user's
  // name (falls back to the initial-letter placeholder if not found).
  const [teamMembers, setTeamMembers] = useState([])

  const load = useCallback(() => {
    setLoading(true)
    setErr('')
    api.listCases()
      .then(d => setData(d))
      .catch(e => setErr(e?.response?.data?.detail || 'Could not load cases'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])
  useEffect(() => { api.getOptions().then(o => setTeamMembers(o.team_members || [])).catch(() => {}) }, [])

  if (selected) {
    return <CaseDetail caseId={selected.case_id} account={selected.account} source={selected.source}
      onBack={() => { setSelected(null); load() }} />
  }

  const tabCounts = { all: 0, resolved: 0, idr: 0, sir: 0 }
  for (const c of data.cases) tabCounts[caseTab(c)]++
  const visibleCases = data.cases.filter(c => caseTab(c) === tab)

  return (
    <div style={{ padding: '24px 28px', maxWidth: 1200 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 18 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>🎟️ All AWS Support Cases</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            Cases from this portal's own AWS Support account and the AWS AMS support account —
            including cases raised manually outside the portal, and the AMS account's AWS Security
            Incident Response cases (SIR tab).
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          {/* Creating a case is now gated by the "cases" tab's WRITE
              grant (see database.py's PortalUser.tab_write docstring
              and main.py's require_tab_write("cases")) — hiding the
              only trigger for showCreate here is sufficient, since the
              backend also enforces this on POST /api/cases regardless. */}
          {canWriteCases && (
            <button onClick={() => setShowCreate(true)}
              style={{ padding: '8px 16px', borderRadius: 7, border: 'none', background: COLORS.brand, color: '#fff', fontWeight: 600, fontSize: 13, cursor: 'pointer' }}>
              ➕ Create AWS Case
            </button>
          )}
          <button onClick={load} disabled={loading}
            style={{ padding: '8px 16px', borderRadius: 7, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontWeight: 600, fontSize: 13, cursor: loading ? 'default' : 'pointer' }}>
            {loading ? '⏳ Loading…' : '🔄 Refresh'}
          </button>
        </div>
      </div>

      {showCreate && (
        <CreateCaseModal
          onClose={() => setShowCreate(false)}
          onCreated={() => { setShowCreate(false); load() }}
        />
      )}

      {err && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {err}
        </div>
      )}

      {data.partial_errors && data.partial_errors.length > 0 && (
        <div style={{ background: COLORS.warningBg, border: `1px solid ${COLORS.warning}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.warning, fontSize: 13 }}>
          ⚠️ Some accounts could not be queried: {data.partial_errors.map(e => `${ACCOUNT_LABEL[e.account] || e.account} — ${e.message}`).join('; ')}
        </div>
      )}

      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', borderBottom: `2px solid ${COLORS.border}`, marginBottom: 16 }}>
        {TABS.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)} style={tabButtonStyle(tab === t.key)}>
            {t.icon} {t.label} ({tabCounts[t.key]})
          </button>
        ))}
      </div>

      {/* Phones: one card per case — the 8-column table (fixed 130/170/90/185/160/130px
          columns) cannot fit in ~360px, so its Subject column collapsed to a few
          pixels and the rest was clipped with no way to scroll to it. */}
      {isMobile && (
        <div>
          {loading ? (
            Array.from({ length: 4 }, (_, i) => (
              <div key={`sk-${i}`} style={{ ...CARD, padding: 14, marginBottom: 8 }}><Skeleton height={16} /></div>
            ))
          ) : visibleCases.length === 0 ? (
            <div style={{ ...CARD, padding: 28, textAlign: 'center', color: COLORS.textMuted, fontSize: 13 }}>
              {data.cases.length === 0 ? 'No cases found in either AWS account' : `No ${TABS.find(t => t.key === tab)?.label || ''} cases`}
            </div>
          ) : visibleCases.map(c => {
            const open = () => setSelected({ case_id: c.case_id, account: c.account, source: c.source })
            const sb = statusBadge(c)
            const accountColor = ACCOUNT_COLOR[c.account] || '#6b7280'
            return (
              <div key={`${c.account}-${c.source || 'support'}-${c.case_id}`} role="link" tabIndex={0} onClick={open}
                onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open() } }}
                style={{
                  background: '#fff', borderRadius: 10, borderLeft: `4px solid ${accountColor}`,
                  padding: '12px 14px', marginBottom: 8, boxShadow: '0 1px 4px rgba(15,23,42,0.08)', cursor: 'pointer',
                }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                  <span style={{ fontFamily: 'monospace', fontWeight: 700, fontSize: 12, color: COLORS.brand, overflowWrap: 'anywhere' }}>
                    {c.display_id || c.case_id}
                  </span>
                  <Badge text={sb.text} color={sb.color} />
                </div>
                <div style={{ fontSize: 14, fontWeight: 600, color: COLORS.textPrimary, lineHeight: 1.4, overflowWrap: 'anywhere' }}>
                  {c.subject}
                </div>
                {c.source === SIR_NATIVE_SOURCE && c.pending_action === 'Customer' && c.status !== 'Closed' && (
                  <div style={{ marginTop: 6, fontSize: 11.5, fontWeight: 600, color: '#dc2626' }}>⚠️ Awaiting your action</div>
                )}
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginTop: 8, fontSize: 12.5, color: '#475569' }}>
                  <Badge text={ACCOUNT_LABEL[c.account] || c.account} color={accountColor} />
                  {c.case_type && <span>{c.case_type}</span>}
                </div>
                <div style={{ marginTop: 8, fontSize: 12, color: '#64748b', lineHeight: 1.7 }}>
                  <div style={{ overflowWrap: 'anywhere' }}>
                    {c.linked_job ? (
                      <Link to={`/jobs/${c.linked_job.job_id}`} onClick={e => e.stopPropagation()}
                        style={{ color: COLORS.brand, fontWeight: 600, textDecoration: 'none' }}>
                        🎫 {c.linked_job.ticket_number ? `INC-${String(c.linked_job.ticket_number).padStart(6, '0')}` : c.linked_job.job_id.slice(0, 8)}
                      </Link>
                    ) : c.linked_health_event ? (
                      <Link to={`/health-dashboard?event_arn=${encodeURIComponent(c.linked_health_event.event_arn)}`}
                        onClick={e => e.stopPropagation()}
                        style={{ color: '#9333ea', fontWeight: 600, textDecoration: 'none' }}>
                        🩺 {c.linked_health_event.service} — {c.linked_health_event.event_type_code}
                      </Link>
                    ) : (
                      <span style={{ color: COLORS.textMuted }}>— not linked</span>
                    )}
                  </div>
                  {c.raised_by_name && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <Avatar photoUrl={findUserPhotoByName(teamMembers, c.raised_by_name)}
                        name={c.raised_by_name} size={20} fontSize={9} />
                      <span style={{ overflowWrap: 'anywhere' }}>{c.raised_by_name}</span>
                    </div>
                  )}
                  <div>📅 {c.time_created ? formatDateTimeHK(c.time_created) : '—'}</div>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* Table (desktop / tablet) */}
      {!isMobile && (
      <div style={{ ...CARD, overflow: 'hidden' }}>
        {/* table-layout: fixed + <colgroup> — same fix as JobList: with
            the default auto layout, a td's maxWidth is silently ignored
            (the browser just grows the column to fit content), so a long
            AWS-generated case Subject would overflow past this column
            and visually bleed into "Status"/"Linked Job" next to it
            instead of wrapping. */}
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13, tableLayout: 'fixed' }}>
          <colgroup>
            <col style={{ width: 130 }} />
            <col style={{ width: 170 }} />
            <col style={{ width: 90 }} />
            <col style={{ width: '24%' }} />
            {/* Status needs enough room for the longest status pill,
                "Pending Customer Action" — at 140px the badge was
                getting clipped mid-word by the column's overflow:hidden
                (see the Status td below) instead of wrapping or shrinking,
                so the box only ever showed "Pending Customer Acti…" cut
                off rather than the full label. */}
            <col style={{ width: 185 }} />
            <col style={{ width: '13%' }} />
            {/* Raised By — which portal user raised this case (see
                standalone_case_store.py's raised_by_email/raised_by_name,
                surfaced by GET /api/cases). */}
            <col style={{ width: 160 }} />
            <col style={{ width: 130 }} />
          </colgroup>
          <thead>
            <tr style={{ background: '#f8fafc', borderBottom: `2px solid ${COLORS.border}` }}>
              {['Case ID', 'Account', 'Type', 'Subject', 'Status', 'Linked Job', 'Raised By', 'Created'].map(h => (
                <th key={h} style={{ padding: '10px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              Array.from({ length: 6 }, (_, i) => (
                <tr key={`sk-${i}`}>
                  <td colSpan={8} style={{ padding: '10px 16px' }}>
                    <Skeleton height={16} />
                  </td>
                </tr>
              ))
            ) : visibleCases.length === 0 ? (
              <tr><td colSpan={8} style={{ padding: 40, textAlign: 'center', color: COLORS.textMuted }}>
                {data.cases.length === 0 ? 'No cases found in either AWS account' : `No ${TABS.find(t => t.key === tab)?.label || ''} cases`}
              </td></tr>
            ) : visibleCases.map(c => (
              <tr key={`${c.account}-${c.source || 'support'}-${c.case_id}`} onClick={() => setSelected({ case_id: c.case_id, account: c.account, source: c.source })}
                style={{ cursor: 'pointer', borderBottom: '1px solid #f1f5f9' }}
                onMouseEnter={e => e.currentTarget.style.background = '#f8fafc'}
                onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                <td style={{ padding: '10px 14px', fontFamily: 'monospace', fontWeight: 700, color: COLORS.brand, fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {c.display_id || c.case_id}
                </td>
                <td style={{ padding: '10px 14px', overflow: 'hidden' }}>
                  <Badge text={ACCOUNT_LABEL[c.account] || c.account} color={ACCOUNT_COLOR[c.account] || '#6b7280'} />
                </td>
                <td style={{ padding: '10px 14px', color: '#475569', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.case_type}</td>
                <td style={{ padding: '10px 14px', color: COLORS.textPrimary, overflowWrap: 'break-word', wordBreak: 'break-word' }}>{c.subject}</td>
                <td style={{ padding: '10px 14px', overflow: 'hidden' }}>
                  <Badge text={statusBadge(c).text} color={statusBadge(c).color} />
                  {/* Native SIR: AWS is waiting on us — surface it in the list. */}
                  {c.source === SIR_NATIVE_SOURCE && c.pending_action === 'Customer' && c.status !== 'Closed' && (
                    <div style={{ marginTop: 4, fontSize: 11, fontWeight: 600, color: '#dc2626' }}>⚠️ Awaiting your action</div>
                  )}
                </td>
                <td style={{ padding: '10px 14px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {c.linked_job ? (
                    <Link to={`/jobs/${c.linked_job.job_id}`} onClick={e => e.stopPropagation()}
                      style={{ color: COLORS.brand, fontWeight: 600, textDecoration: 'none' }}>
                      🎫 {c.linked_job.ticket_number ? `INC-${String(c.linked_job.ticket_number).padStart(6, '0')}` : c.linked_job.job_id.slice(0, 8)}
                    </Link>
                  ) : c.linked_health_event ? (
                    <Link to={`/health-dashboard?event_arn=${encodeURIComponent(c.linked_health_event.event_arn)}`}
                      onClick={e => e.stopPropagation()}
                      style={{ color: '#9333ea', fontWeight: 600, textDecoration: 'none' }}>
                      🩺 {c.linked_health_event.service} — {c.linked_health_event.event_type_code}
                    </Link>
                  ) : (
                    <span style={{ color: COLORS.textMuted }}>— not linked</span>
                  )}
                </td>
                <td style={{ padding: '10px 14px', overflow: 'hidden' }}>
                  {c.raised_by_name ? (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, overflow: 'hidden' }}>
                      <Avatar photoUrl={findUserPhotoByName(teamMembers, c.raised_by_name)}
                        name={c.raised_by_name} size={22} fontSize={10} />
                      <span style={{ color: COLORS.textPrimary, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.raised_by_name}</span>
                    </div>
                  ) : (
                    <span style={{ color: COLORS.textMuted }}>—</span>
                  )}
                </td>
                <td style={{ padding: '10px 14px', color: '#64748b', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.time_created ? formatDateTimeHK(c.time_created) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      )}
    </div>
  )
}

// ── Create AWS Case modal ────────────────────────────────────────────────
// Lets any user raise a new AWS Support case or AMS Service
// Request/Incident directly, without needing to create a Job first —
// previously the only way to raise a case was via a Job's Escalation
// Actions section. Reuses the same category/priority option lists as
// the New Ticket form (api.getOptions()) for consistency.
// "aws_support" (this portal's own AWS Support account) is deliberately
// NOT offered here per user request — only the AMS support account
// case types can be raised directly from this page.
// Own-account AWS Support cases still go through a Job's Escalation
// Actions as before.
const CASE_TYPE_OPTIONS = [
  { value: 'aws_ams_sr', label: '🛡️ AMS Service Request', color: '#9333ea' },
  { value: 'aws_ams_incident', label: '🚨 AMS Incident', color: '#dc2626' },
]

// AWS Support's AddAttachmentsToSet hard limits — enforced client-side too
// so the user gets an immediate message rather than waiting for the
// backend/AWS round-trip.
const MAX_ATTACHMENTS = 3
const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024 // 5 MB

function validateAttachmentFiles(fileList) {
  const files = Array.from(fileList || [])
  if (files.length === 0) return { files, error: '' }
  if (files.length > MAX_ATTACHMENTS) {
    return { files: [], error: `AWS Support allows a maximum of ${MAX_ATTACHMENTS} attachments per case/reply.` }
  }
  const tooBig = files.find(f => f.size > MAX_ATTACHMENT_BYTES)
  if (tooBig) {
    return { files: [], error: `"${tooBig.name}" is too large (${Math.round(tooBig.size / 1024)} KB) — AWS Support allows a maximum of 5 MB per attachment.` }
  }
  return { files, error: '' }
}

// Small reusable "attach files" control — shared by the create-case
// modal and the reply form. Lets the user pick up to 3 files (any type
// AWS Support itself accepts — no restrictive accept= attribute) and
// shows a removable chip list.
function AttachmentPicker({ files, onChange, disabled }) {
  const inputRef = React.useRef(null)
  const [pickErr, setPickErr] = useState('')

  const handlePick = (e) => {
    const { files: picked, error } = validateAttachmentFiles(e.target.files)
    setPickErr(error)
    if (!error) onChange(picked)
    e.target.value = '' // allow re-picking the same file
  }

  const removeFile = (idx) => {
    onChange(files.filter((_, i) => i !== idx))
  }

  return (
    <div style={{ marginTop: 8 }}>
      <input ref={inputRef} type="file" multiple disabled={disabled}
        style={{ display: 'none' }} onChange={handlePick} />
      <button type="button" onClick={() => inputRef.current?.click()} disabled={disabled}
        style={{ padding: '6px 12px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontWeight: 600, fontSize: 12, cursor: disabled ? 'default' : 'pointer' }}>
        📎 Attach files
      </button>
      <span style={{ marginLeft: 8, fontSize: 11, color: COLORS.textMuted }}>Up to 3 files, 5 MB each</span>
      {pickErr && <div style={{ color: COLORS.danger, fontSize: 12, marginTop: 6 }}>{pickErr}</div>}
      {files.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
          {files.map((f, i) => (
            <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, background: '#f1f5f9', border: `1px solid ${COLORS.border}`, borderRadius: 5, padding: '3px 8px', fontSize: 12, color: COLORS.textPrimary }}>
              📄 {f.name} <span style={{ color: COLORS.textMuted }}>({Math.round(f.size / 1024)} KB)</span>
              {!disabled && (
                <button type="button" onClick={() => removeFile(i)}
                  style={{ border: 'none', background: 'none', color: COLORS.danger, cursor: 'pointer', fontSize: 13, lineHeight: 1, padding: 0 }}>×</button>
              )}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function CreateCaseModal({ onClose, onCreated }) {
  const [options, setOptions] = useState({ categories: [], priorities: [] })
  const [caseType, setCaseType] = useState('aws_ams_sr')
  const [subject, setSubject] = useState('')
  const [description, setDescription] = useState('')
  const [category, setCategory] = useState('')
  const [priority, setPriority] = useState('')
  const [attachFiles, setAttachFiles] = useState([])
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr] = useState('')
  const [result, setResult] = useState(null)

  useEffect(() => { api.getOptions().then(setOptions).catch(() => {}) }, [])

  const valid = subject.trim() && description.trim() && category && priority

  const submit = async () => {
    if (!valid) return
    setSubmitting(true)
    setErr('')
    try {
      let attachmentSetId = null
      if (attachFiles.length > 0) {
        const uploadRes = await api.uploadCaseAttachments('ams', attachFiles)
        attachmentSetId = uploadRes.attachment_set_id
      }
      const r = await api.createCase(caseType, subject.trim(), description.trim(), category, priority, attachmentSetId)
      setResult(r)
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || 'Could not create the case')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div onClick={onClose}
      style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
      <div onClick={e => e.stopPropagation()}
        style={{ background: '#fff', borderRadius: 12, padding: 24, width: 540, maxHeight: '85vh', overflowY: 'auto', boxShadow: '0 10px 40px rgba(0,0,0,0.25)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16 }}>
          <h3 style={{ margin: 0, fontSize: 16, color: COLORS.textPrimary }}>➕ Create AWS Case</h3>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 18, cursor: 'pointer', color: COLORS.textMuted, lineHeight: 1 }}>×</button>
        </div>

        {result ? (
          <div>
            <div style={{ background: COLORS.successBg, border: `1px solid ${COLORS.success}40`, borderRadius: 7, padding: '12px 14px', marginBottom: 16, fontSize: 13, color: COLORS.success }}>
              ✅ Case created: <code style={{ background: '#fff', padding: '1px 6px', borderRadius: 3, fontWeight: 700 }}>{result.case_id}</code>
              <div style={{ marginTop: 6 }}>
                <a href={result.url} target="_blank" rel="noreferrer" style={{ color: COLORS.brand, fontSize: 12 }}>Open in AWS Console ↗</a>
              </div>
            </div>
            <button onClick={onCreated}
              style={{ padding: '9px 18px', borderRadius: 7, border: 'none', background: COLORS.brand, color: '#fff', fontWeight: 600, fontSize: 13, cursor: 'pointer' }}>
              Done
            </button>
          </div>
        ) : (
          <>
            <div style={{ marginBottom: 14 }}>
              <label style={{ display: 'block', fontSize: 12.5, fontWeight: 600, color: COLORS.textSecondary, marginBottom: 6 }}>Case type</label>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {CASE_TYPE_OPTIONS.map(o => (
                  <label key={o.value} style={{
                    display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', borderRadius: 7,
                    border: `1px solid ${caseType === o.value ? o.color : COLORS.border}`,
                    background: caseType === o.value ? o.color + '10' : '#fff', cursor: 'pointer', fontSize: 13,
                  }}>
                    <input type="radio" name="case_type" value={o.value} checked={caseType === o.value}
                      onChange={() => setCaseType(o.value)} />
                    {o.label}
                  </label>
                ))}
              </div>
            </div>

            <div style={{ marginBottom: 14 }}>
              <label style={{ display: 'block', fontSize: 12.5, fontWeight: 600, color: COLORS.textSecondary, marginBottom: 6 }}>Subject *</label>
              <input value={subject} onChange={e => setSubject(e.target.value)} maxLength={255}
                placeholder="Short summary of the issue"
                style={{ width: '100%', padding: '9px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13, boxSizing: 'border-box' }} />
            </div>

            <div style={{ display: 'flex', gap: 12, marginBottom: 14 }}>
              <div style={{ flex: 1 }}>
                <label style={{ display: 'block', fontSize: 12.5, fontWeight: 600, color: COLORS.textSecondary, marginBottom: 6 }}>Category *</label>
                <select value={category} onChange={e => setCategory(e.target.value)}
                  style={{ width: '100%', padding: '9px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13, boxSizing: 'border-box' }}>
                  <option value="">— Select category —</option>
                  {options.categories.map(c => <option key={c}>{c}</option>)}
                </select>
              </div>
              <div style={{ flex: 1 }}>
                <label style={{ display: 'block', fontSize: 12.5, fontWeight: 600, color: COLORS.textSecondary, marginBottom: 6 }}>Priority *</label>
                <select value={priority} onChange={e => setPriority(e.target.value)}
                  style={{ width: '100%', padding: '9px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13, boxSizing: 'border-box' }}>
                  <option value="">— Select priority —</option>
                  {options.priorities.map(p => <option key={p}>{p}</option>)}
                </select>
              </div>
            </div>

            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: 12.5, fontWeight: 600, color: COLORS.textSecondary, marginBottom: 6 }}>Description *</label>
              <textarea value={description} onChange={e => setDescription(e.target.value)}
                placeholder="Describe the issue in detail — this becomes the case's initial communication body."
                style={{ width: '100%', minHeight: 110, padding: '9px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13, resize: 'vertical', boxSizing: 'border-box', fontFamily: 'inherit' }} />
              <AttachmentPicker files={attachFiles} onChange={setAttachFiles} disabled={submitting} />
            </div>

            {err && (
              <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 14, color: COLORS.danger, fontSize: 13 }}>
                {err}
              </div>
            )}

            <div style={{ display: 'flex', gap: 10 }}>
              <button onClick={submit} disabled={!valid || submitting}
                style={{
                  padding: '9px 18px', borderRadius: 7, border: 'none', background: COLORS.brand, color: '#fff',
                  fontWeight: 600, fontSize: 13, cursor: (!valid || submitting) ? 'default' : 'pointer',
                  opacity: (!valid || submitting) ? 0.6 : 1,
                }}>
                {submitting ? '⏳ Creating…' : '✅ Create Case'}
              </button>
              <button onClick={onClose}
                style={{ padding: '9px 18px', borderRadius: 7, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontWeight: 600, fontSize: 13, cursor: 'pointer' }}>
                Cancel
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// ── Detail + reply view ──────────────────────────────────────────────────
function CaseDetail({ caseId, account, source, onBack }) {
  const { user } = useAuth()
  // Same "cases" tab WRITE grant that gates Create AWS Case (and that the
  // SIR comment endpoint enforces server-side).
  const canWriteCases = (user?.tab_write || []).includes('cases')
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [replyText, setReplyText] = useState('')
  const [replyAttachFiles, setReplyAttachFiles] = useState([])
  const [sending, setSending] = useState(false)
  const [sendErr, setSendErr] = useState('')
  const [sent, setSent] = useState(false)
  const [teamMembers, setTeamMembers] = useState([])

  useEffect(() => { api.getOptions().then(o => setTeamMembers(o.team_members || [])).catch(() => {}) }, [])

  const load = useCallback(() => {
    setLoading(true)
    setErr('')
    api.getCase(caseId, account, source)
      .then(d => setDetail(d))
      .catch(e => setErr(e?.response?.data?.detail || 'Could not load case detail'))
      .finally(() => setLoading(false))
  }, [caseId, account, source])

  useEffect(() => { load() }, [load])

  // Native SIR cases: text-only comment through the dedicated endpoint.
  // Never retried automatically — a duplicate would be visible to AWS.
  const sendSirReply = async () => {
    if (!replyText.trim() || sending) return
    setSending(true)
    setSendErr('')
    setSent(false)
    try {
      await api.replyToSecurityIrCase(caseId, replyText.trim())
      setReplyText('')
      setSent(true)
      load() // refresh so the new comment shows up
    } catch (e) {
      setSendErr(e?.response?.data?.detail || e?.message || 'Could not send comment')
    } finally { setSending(false) }
  }

  const sendReply = async () => {
    if (!replyText.trim()) return
    setSending(true)
    setSendErr('')
    setSent(false)
    try {
      let attachmentSetId = null
      if (replyAttachFiles.length > 0) {
        const uploadRes = await api.uploadCaseAttachments(account, replyAttachFiles)
        attachmentSetId = uploadRes.attachment_set_id
      }
      await api.replyToCase(caseId, account, replyText.trim(), attachmentSetId)
      setReplyText('')
      setReplyAttachFiles([])
      setSent(true)
      load() // refresh communications so the new reply shows up
    } catch (e) {
      setSendErr(e?.response?.data?.detail || e?.message || 'Could not send reply')
    } finally { setSending(false) }
  }

  return (
    <div style={{ padding: '24px 28px', maxWidth: 900 }}>
      <button onClick={onBack} style={{ background: 'none', border: 'none', color: COLORS.brand, fontSize: 13, fontWeight: 600, cursor: 'pointer', padding: 0, marginBottom: 16 }}>
        ← Back to All Cases
      </button>

      {loading ? (
        <div style={{ ...CARD, padding: '20px 24px' }}><SkeletonLines lines={5} /></div>
      ) : err ? (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', color: COLORS.danger, fontSize: 13 }}>{err}</div>
      ) : detail && (
        <>
          <div style={{ ...CARD, padding: '20px 24px', marginBottom: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10, flexWrap: 'wrap' }}>
              <code style={{ background: '#f1f5f9', padding: '2px 8px', borderRadius: 4, fontWeight: 700, color: COLORS.brand }}>{detail.display_id || detail.case_id}</code>
              <Badge text={ACCOUNT_LABEL[detail.account] || detail.account} color={ACCOUNT_COLOR[detail.account] || '#6b7280'} />
              <Badge text={statusBadge(detail).text} color={statusBadge(detail).color} />
              <a href={detail.source === SIR_NATIVE_SOURCE
                  ? detail.console_url
                  : `https://support.console.aws.amazon.com/support/home#/case/?caseId=${detail.case_id}`}
                target="_blank" rel="noreferrer" style={{ marginLeft: 'auto', color: COLORS.brand, fontSize: 12 }}>
                {detail.source === SIR_NATIVE_SOURCE ? 'Open Security Incident Response console ↗' : 'Open in AWS Console ↗'}
              </a>
            </div>
            <h3 style={{ margin: '0 0 8px', color: COLORS.textPrimary, fontSize: 16 }}>{detail.subject}</h3>
            <div style={{ fontSize: 12.5, color: COLORS.textSecondary, display: 'flex', gap: 18, flexWrap: 'wrap' }}>
              {detail.service_code && <span>Service: {detail.service_code}</span>}
              {detail.category_code && <span>Category: {detail.category_code}</span>}
              {detail.severity_code && <span>Severity: {detail.severity_code}</span>}
              {detail.time_created && <span>Created: {formatDateTimeHK(detail.time_created)}</span>}
              {detail.raised_by_name && <span>Raised by: {detail.raised_by_name}</span>}
            </div>
            {detail.source === SIR_NATIVE_SOURCE && detail.pending_action === 'Customer' && detail.status !== 'Closed' && (
              <div style={{ marginTop: 10, background: COLORS.warningBg, border: `1px solid ${COLORS.warning}40`, borderRadius: 7, padding: '8px 12px', fontSize: 13, color: COLORS.warning, fontWeight: 600 }}>
                ⚠️ AWS is waiting for your action on this case.
              </div>
            )}
            {detail.linked_job && (
              <div style={{ marginTop: 10, fontSize: 13 }}>
                🎫 Linked to Job:{' '}
                <Link to={`/jobs/${detail.linked_job.job_id}`} style={{ color: COLORS.brand, fontWeight: 600, textDecoration: 'none' }}>
                  {detail.linked_job.ticket_number ? `INC-${String(detail.linked_job.ticket_number).padStart(6, '0')}` : detail.linked_job.job_id.slice(0, 8)} — {detail.linked_job.title}
                </Link>
              </div>
            )}
            {!detail.linked_job && detail.linked_health_event && (
              <div style={{ marginTop: 10, fontSize: 13 }}>
                🩺 Linked to AWS Health event:{' '}
                <Link to={`/health-dashboard?event_arn=${encodeURIComponent(detail.linked_health_event.event_arn)}`}
                  style={{ color: '#9333ea', fontWeight: 600, textDecoration: 'none' }}>
                  {detail.linked_health_event.service} — {detail.linked_health_event.event_type_code}
                </Link>
              </div>
            )}
          </div>

          {detail.source === SIR_NATIVE_SOURCE && (
            <div style={{ ...CARD, padding: '20px 24px', marginBottom: 16 }}>
              <h4 style={{ margin: '0 0 12px', color: COLORS.textPrimary, fontSize: 14 }}>🛡️ Incident details</h4>
              {detail.description && (
                <div style={{ fontSize: 13, color: COLORS.textPrimary, whiteSpace: 'pre-wrap', marginBottom: 14 }}>{detail.description}</div>
              )}
              <div style={{ display: 'grid', gridTemplateColumns: '170px 1fr', rowGap: 6, fontSize: 12.5 }}>
                {[
                  ['Engagement type', detail.engagement_type],
                  ['Pending action', detail.pending_action],
                  ['Resolver', detail.resolver_type],
                  ['Reported incident start', detail.reported_incident_start ? formatDateTimeHK(detail.reported_incident_start) : null],
                  ['Actual incident start', detail.actual_incident_start ? formatDateTimeHK(detail.actual_incident_start) : null],
                  ['Last updated', detail.last_updated ? formatDateTimeHK(detail.last_updated) : null],
                  ['Closed', detail.closed_date ? formatDateTimeHK(detail.closed_date) : null],
                  ['Closure code', detail.closure_code],
                  ['Impacted accounts', (detail.impacted_accounts || []).join(', ')],
                  ['Impacted services', (detail.impacted_services || []).join(', ')],
                  ['Impacted regions', (detail.impacted_regions || []).join(', ')],
                  ['Attachments', detail.attachment_count ? String(detail.attachment_count) : null],
                ].filter(([, v]) => v).map(([k, v]) => (
                  <React.Fragment key={k}>
                    <div style={{ color: COLORS.textMuted }}>{k}</div>
                    <div style={{ color: COLORS.textPrimary, overflowWrap: 'anywhere' }}>{v}</div>
                  </React.Fragment>
                ))}
              </div>
            </div>
          )}

          <div style={{ ...CARD, padding: '20px 24px', marginBottom: 16 }}>
            <h4 style={{ margin: '0 0 14px', color: COLORS.textPrimary, fontSize: 14 }}>💬 Communications</h4>
            {detail.communications.length === 0 ? (
              <div style={{ color: COLORS.textMuted, fontSize: 13 }}>No communications yet.</div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                {detail.communications.map((c, i) => (
                  <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', background: '#f8fafc', border: `1px solid ${COLORS.border}`, borderRadius: 7, padding: '10px 14px' }}>
                    {/* c.submitted_by is free text from AWS Support (may be an
                        AWS/AMS agent, not a portal user) — best-effort match
                        by name against our team so a real avatar shows when
                        it happens to be a teammate, else falls back to the
                        initial-letter placeholder. */}
                    <Avatar photoUrl={findUserPhotoByName(teamMembers, c.submitted_by)}
                      name={c.submitted_by} size={34} fontSize={14} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginBottom: 4, display: 'flex', justifyContent: 'space-between' }}>
                        <span>{c.submitted_by || 'Unknown'}</span>
                        <span>{c.time_created ? formatDateTimeHK(c.time_created) : ''}</span>
                      </div>
                      <div style={{ fontSize: 13, color: COLORS.textPrimary, whiteSpace: 'pre-wrap' }}>{c.body}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {detail.source === SIR_NATIVE_SOURCE ? (
            // Native SIR cases use their own comment API (text only — no
            // attachments, no status changes). Posting needs the "cases"
            // tab WRITE grant, and is not possible on a Closed case.
            detail.status === 'Closed' || !canWriteCases ? (
              <div style={{ ...CARD, padding: '16px 24px', fontSize: 13, color: COLORS.textSecondary }}>
                {detail.status === 'Closed'
                  ? 'This case is closed, so it can no longer be commented on.'
                  : 'You do not have write access to reply to this case — ask an admin to grant it.'}{' '}
                Other actions are available in the{' '}
                <a href={detail.console_url} target="_blank" rel="noreferrer" style={{ color: COLORS.brand }}>
                  Security Incident Response console ↗
                </a>.
              </div>
            ) : (
              <div style={{ ...CARD, padding: '20px 24px' }}>
                <h4 style={{ margin: '0 0 6px', color: COLORS.textPrimary, fontSize: 14 }}>✍️ Comment on this incident</h4>
                <div style={{ fontSize: 12, color: COLORS.textMuted, marginBottom: 10 }}>
                  Goes to the AWS Security Incident Response team. It is posted under the shared Portal
                  account, with your email added at the top so they know who wrote it. Text only.
                </div>
                <textarea value={replyText} onChange={e => setReplyText(e.target.value)} maxLength={10000}
                  placeholder="Type your comment to AWS Security Incident Response…"
                  style={{ width: '100%', minHeight: 100, padding: '10px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13, resize: 'vertical', boxSizing: 'border-box', fontFamily: 'inherit' }} />
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10 }}>
                  <button onClick={sendSirReply} disabled={sending || !replyText.trim()}
                    style={{ padding: '9px 18px', borderRadius: 7, border: 'none', background: COLORS.brand, color: '#fff', fontWeight: 600, fontSize: 13, cursor: (sending || !replyText.trim()) ? 'default' : 'pointer', opacity: (sending || !replyText.trim()) ? 0.6 : 1 }}>
                    {sending ? '⏳ Sending…' : '📤 Send comment'}
                  </button>
                  {sent && <span style={{ color: COLORS.success, fontSize: 13, fontWeight: 600 }}>✅ Comment sent</span>}
                  {sendErr && <span style={{ color: COLORS.danger, fontSize: 13 }}>{sendErr}</span>}
                </div>
              </div>
            )
          ) : (
          <div style={{ ...CARD, padding: '20px 24px' }}>
            <h4 style={{ margin: '0 0 10px', color: COLORS.textPrimary, fontSize: 14 }}>✍️ Reply to this case</h4>
            <textarea value={replyText} onChange={e => setReplyText(e.target.value)}
              placeholder="Type your reply to AWS Support / AMS…"
              style={{ width: '100%', minHeight: 100, padding: '10px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13, resize: 'vertical', boxSizing: 'border-box', fontFamily: 'inherit' }} />
            <AttachmentPicker files={replyAttachFiles} onChange={setReplyAttachFiles} disabled={sending} />
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10 }}>
              <button onClick={sendReply} disabled={sending || !replyText.trim()}
                style={{ padding: '9px 18px', borderRadius: 7, border: 'none', background: COLORS.brand, color: '#fff', fontWeight: 600, fontSize: 13, cursor: (sending || !replyText.trim()) ? 'default' : 'pointer', opacity: (sending || !replyText.trim()) ? 0.6 : 1 }}>
                {sending ? '⏳ Sending…' : '📤 Send Reply'}
              </button>
              {sent && <span style={{ color: COLORS.success, fontSize: 13, fontWeight: 600 }}>✅ Reply sent</span>}
              {sendErr && <span style={{ color: COLORS.danger, fontSize: 13 }}>{sendErr}</span>}
            </div>
          </div>
          )}
        </>
      )}
    </div>
  )
}
