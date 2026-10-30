import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../api.js'
import { COLORS, CARD } from '../utils/theme.js'
import { getPageCache, setPageCache } from '../utils/pageCache.js'

const CACHE_KEY = 'security-hub-summary'

// ── AWS Security Hub ──────────────────────────────────────────────────────
// Active (RecordState=ACTIVE, WorkflowStatus=NEW) findings across every
// AWS account (same superset Billing by Service / Savings Plan Cost use),
// fetched via the same cross-account AssumeRole(PortalReadOnlyRole) pattern
// as those tabs. Read-only for everyone — this page never mutates a
// finding's workflow status; that stays a deliberate action in the AWS
// console. "AI Analysis" calls Bedrock for an executive summary plus a
// prioritized remediation plan (advisory only).

const SEVERITY_COLOR = {
  CRITICAL: COLORS.danger,
  HIGH: '#ea580c',
  MEDIUM: COLORS.warning,
  LOW: '#2563eb',
  INFORMATIONAL: COLORS.textMuted,
}
const SEVERITY_ORDER = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFORMATIONAL']

function formatAge(seconds) {
  if (!seconds || seconds < 60) return 'just now'
  const mins = Math.floor(seconds / 60)
  if (mins < 60) return `${mins} min${mins === 1 ? '' : 's'} ago`
  const hours = Math.floor(mins / 60)
  return `${hours} hour${hours === 1 ? '' : 's'} ago`
}

function summaryCacheKey(accountIds) {
  return `${CACHE_KEY}:${JSON.stringify([...accountIds].sort())}`
}

function SeverityPill({ label, count }) {
  if (!count) return null
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4, padding: '3px 10px', borderRadius: 20,
      background: `${SEVERITY_COLOR[label]}18`, color: SEVERITY_COLOR[label], fontSize: 12, fontWeight: 700,
    }}>
      {label} {count}
    </span>
  )
}

// ── AI executive-summary + remediation plan modal ─────────────────────────
function AiSummaryModal({ summary, loading, error, onClose, onRetry }) {
  const RISK_LABEL = {
    low: { text: '🟢 Low Risk', color: COLORS.success },
    moderate: { text: '🟡 Moderate Risk', color: COLORS.warning },
    high: { text: '🟠 High Risk', color: '#ea580c' },
    critical: { text: '🔴 Critical Risk', color: COLORS.danger },
  }
  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200, padding: 20 }}
      onClick={onClose}>
      <div style={{ background: '#fff', borderRadius: 12, maxWidth: 720, width: '100%', maxHeight: '85vh', overflowY: 'auto', padding: 28, boxShadow: '0 20px 60px rgba(0,0,0,0.25)' }}
        onClick={e => e.stopPropagation()}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 18 }}>
          <h3 style={{ margin: 0, fontSize: 18, color: COLORS.textPrimary, display: 'flex', alignItems: 'center', gap: 8 }}>
            🤖 AI Security Analysis
          </h3>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 20, cursor: 'pointer', color: COLORS.textMuted }}>×</button>
        </div>

        {loading && <div style={{ padding: 40, textAlign: 'center', color: COLORS.textMuted }}>Analyzing findings…</div>}

        {!loading && error && (
          <div>
            <div style={{ color: COLORS.danger, marginBottom: 14, fontSize: 13.5 }}>{error}</div>
            <button onClick={onRetry} style={{ padding: '8px 16px', borderRadius: 7, border: 'none', background: COLORS.brand, color: '#fff', fontWeight: 700, fontSize: 13, cursor: 'pointer' }}>
              Retry
            </button>
          </div>
        )}

        {!loading && !error && summary && (
          <div>
            {summary.risk_level && (
              <div style={{ marginBottom: 16 }}>
                <span style={{
                  display: 'inline-flex', alignItems: 'center', padding: '5px 14px', borderRadius: 20,
                  background: `${RISK_LABEL[summary.risk_level]?.color || COLORS.textMuted}18`,
                  color: RISK_LABEL[summary.risk_level]?.color || COLORS.textMuted, fontWeight: 700, fontSize: 13,
                }}>
                  {RISK_LABEL[summary.risk_level]?.text || summary.risk_level}
                </span>
              </div>
            )}

            <div style={{ fontSize: 14, lineHeight: 1.6, color: COLORS.textPrimary, marginBottom: 20 }}>
              {summary.executive_summary}
            </div>

            {summary.top_issues?.length > 0 && (
              <div style={{ marginBottom: 20 }}>
                <div style={{ fontSize: 12, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 10 }}>
                  Top Issues to Prioritize
                </div>
                <ol style={{ margin: 0, paddingLeft: 20, fontSize: 13.5, color: COLORS.textPrimary, lineHeight: 1.7 }}>
                  {summary.top_issues.map((issue, i) => <li key={i} style={{ marginBottom: 6 }}>{issue}</li>)}
                </ol>
              </div>
            )}

            {summary.remediation_plan?.length > 0 && (
              <div style={{ marginBottom: 20 }}>
                <div style={{ fontSize: 12, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 10 }}>
                  Remediation Plan
                </div>
                <ol style={{ margin: 0, paddingLeft: 20, fontSize: 13.5, color: COLORS.textPrimary, lineHeight: 1.7 }}>
                  {summary.remediation_plan.map((step, i) => <li key={i} style={{ marginBottom: 6 }}>{step}</li>)}
                </ol>
              </div>
            )}

            {summary.bottom_line && (
              <div style={{ padding: '12px 16px', borderRadius: 8, background: COLORS.brandLight, color: COLORS.brandDark, fontWeight: 600, fontSize: 13.5, marginBottom: 16 }}>
                📌 {summary.bottom_line}
              </div>
            )}

            <div style={{ fontSize: 11, color: COLORS.textMuted, borderTop: `1px solid ${COLORS.border}`, paddingTop: 10 }}>
              Generated {summary.generated_at} · Model: {summary.model_used} · {summary.total_findings} active findings across {summary.enabled_count} account(s)
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

const URGENCY_LABEL = {
  immediate: { text: '🔴 Fix Immediately', color: COLORS.danger },
  soon: { text: '🟠 Fix Soon', color: '#ea580c' },
  monitor: { text: '🟡 Monitor', color: COLORS.warning },
  informational: { text: '🔵 Informational', color: '#2563eb' },
}

// ── Impacted IAM users/groups/roles (live IAM lookup, not AI-guessed) ─────
// Shown for IAM-policy findings only (e.g. IAM.1 "IAM policies should not
// allow full '*' administrative privileges") — surfaces exactly which
// users/groups/roles the offending policy is attached to, expanding
// group membership down to individual users, so the security team knows
// exactly who is affected before touching anything.
function ImpactedPrincipals({ impacted }) {
  if (!impacted || !impacted.supported) return null

  const users = impacted.directly_attached_users || []
  const groups = impacted.directly_attached_groups || []
  const roles = impacted.directly_attached_roles || []
  const usersViaGroups = impacted.users_via_groups || []
  const hasAny = users.length || groups.length || roles.length

  return (
    <div style={{ marginBottom: 12, border: `1px solid ${COLORS.warning}40`, borderRadius: 8, padding: '10px 14px', background: COLORS.warningBg }}>
      <div style={{ fontSize: 11.5, color: COLORS.warning, fontWeight: 700, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
        ⚠️ Impacted Users & Groups
      </div>

      {impacted.error && <div style={{ fontSize: 12, color: COLORS.danger }}>{impacted.error}</div>}

      {!impacted.error && !hasAny && (
        <div style={{ fontSize: 12, color: COLORS.textSecondary }}>This policy is not currently attached to any user, group, or role.</div>
      )}

      {!impacted.error && hasAny && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {groups.length > 0 && (
            <div>
              <div style={{ fontSize: 11, color: COLORS.textMuted, fontWeight: 700, marginBottom: 4 }}>Groups ({groups.length})</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {groups.map(g => (
                  <span key={g.group_id || g.group_name} style={{ padding: '3px 10px', borderRadius: 14, background: '#fff', border: `1px solid ${COLORS.border}`, fontSize: 12, fontWeight: 600, color: COLORS.textPrimary }}>
                    👥 {g.group_name}
                  </span>
                ))}
              </div>
            </div>
          )}

          {usersViaGroups.length > 0 && (
            <div>
              <div style={{ fontSize: 11, color: COLORS.textMuted, fontWeight: 700, marginBottom: 4 }}>
                Users affected via group membership ({usersViaGroups.length})
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {usersViaGroups.map((u, i) => (
                  <span key={`${u.user_name}-${i}`} title={`via group ${u.via_group}`}
                    style={{ padding: '3px 10px', borderRadius: 14, background: '#fff', border: `1px solid ${COLORS.border}`, fontSize: 12, fontWeight: 600, color: COLORS.textPrimary }}>
                    👤 {u.user_name}
                  </span>
                ))}
              </div>
            </div>
          )}

          {users.length > 0 && (
            <div>
              <div style={{ fontSize: 11, color: COLORS.textMuted, fontWeight: 700, marginBottom: 4 }}>Directly attached users ({users.length})</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {users.map(u => (
                  <span key={u.user_id || u.user_name} style={{ padding: '3px 10px', borderRadius: 14, background: '#fff', border: `1px solid ${COLORS.border}`, fontSize: 12, fontWeight: 600, color: COLORS.textPrimary }}>
                    👤 {u.user_name}
                  </span>
                ))}
              </div>
            </div>
          )}

          {roles.length > 0 && (
            <div>
              <div style={{ fontSize: 11, color: COLORS.textMuted, fontWeight: 700, marginBottom: 4 }}>Roles ({roles.length})</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {roles.map(r => (
                  <span key={r.role_id || r.role_name} style={{ padding: '3px 10px', borderRadius: 14, background: '#fff', border: `1px solid ${COLORS.border}`, fontSize: 12, fontWeight: 600, color: COLORS.textPrimary }}>
                    🎭 {r.role_name}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── One finding row ────────────────────────────────────────────────────────
// Clicking a row IS the trigger for AI analysis — no separate button.
// Expands to show basic AWS-reported details immediately, and
// auto-fetches (once, cached per finding) a per-finding AI summary +
// step-by-step remediation plan + urgency from Bedrock, shown inline
// once it returns.
function FindingRow({ finding, accountId }) {
  const [open, setOpen] = useState(false)
  const [aiAnalysis, setAiAnalysis] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState('')
  const fetchedRef = React.useRef(false)

  function handleClick() {
    const willOpen = !open
    setOpen(willOpen)
    if (willOpen && !fetchedRef.current) {
      fetchedRef.current = true
      setAiLoading(true)
      setAiError('')
      api.getSecurityHubFindingAiAnalysis(accountId, finding.id)
        .then(result => setAiAnalysis(result))
        .catch(e => {
          fetchedRef.current = false
          setAiError(e?.response?.data?.detail || 'Could not generate AI analysis for this finding')
        })
        .finally(() => setAiLoading(false))
    }
  }

  function retryAi(e) {
    e.stopPropagation()
    fetchedRef.current = true
    setAiLoading(true)
    setAiError('')
    api.getSecurityHubFindingAiAnalysis(accountId, finding.id)
      .then(result => setAiAnalysis(result))
      .catch(e2 => {
        fetchedRef.current = false
        setAiError(e2?.response?.data?.detail || 'Could not generate AI analysis for this finding')
      })
      .finally(() => setAiLoading(false))
  }

  return (
    <div style={{ borderBottom: `1px solid ${COLORS.border}` }}>
      <div onClick={handleClick}
        style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 14px', cursor: 'pointer' }}>
        <span style={{ color: COLORS.textMuted, fontSize: 11, width: 14 }}>{open ? '▾' : '▸'}</span>
        <span style={{
          fontSize: 11, fontWeight: 700, padding: '2px 8px', borderRadius: 12, minWidth: 78, textAlign: 'center',
          background: `${SEVERITY_COLOR[finding.severity] || COLORS.textMuted}18`, color: SEVERITY_COLOR[finding.severity] || COLORS.textMuted,
        }}>
          {finding.severity}
        </span>
        <span style={{ fontSize: 13, color: COLORS.textPrimary, flex: 1 }}>{finding.title}</span>
        <span style={{ fontSize: 11, color: COLORS.brand, fontWeight: 600 }}>🤖 Click for AI analysis</span>
        {finding.security_control_id && (
          <span style={{ fontSize: 11, color: COLORS.textMuted, fontFamily: 'monospace' }}>{finding.security_control_id}</span>
        )}
      </div>
      {open && (
        <div style={{ padding: '4px 14px 14px 38px', fontSize: 12.5, color: COLORS.textSecondary, lineHeight: 1.6 }}>
          {finding.description && <div style={{ marginBottom: 8 }}>{finding.description}</div>}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, marginBottom: 10, color: COLORS.textMuted, fontSize: 11.5 }}>
            {finding.resource_type && <span>Resource type: {finding.resource_type}</span>}
            {finding.resource_id && <span title={finding.resource_id}>Resource: {finding.resource_id.length > 40 ? finding.resource_id.slice(-40) : finding.resource_id}</span>}
            {finding.region && <span>Region: {finding.region}</span>}
            {finding.compliance_status && <span>Compliance: {finding.compliance_status}</span>}
          </div>

          <div style={{ border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: '12px 14px', background: '#fafbff' }}>
            <div style={{ fontSize: 11.5, color: COLORS.brand, fontWeight: 700, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
              🤖 AI Analysis & Remediation Plan
            </div>

            {aiLoading && <div style={{ color: COLORS.textMuted, fontSize: 12.5 }}>Analyzing this finding…</div>}

            {!aiLoading && aiError && (
              <div>
                <div style={{ color: COLORS.danger, fontSize: 12.5, marginBottom: 8 }}>{aiError}</div>
                <button onClick={retryAi}
                  style={{ padding: '5px 12px', borderRadius: 6, border: 'none', background: COLORS.brand, color: '#fff', fontWeight: 700, fontSize: 11.5, cursor: 'pointer' }}>
                  Retry
                </button>
              </div>
            )}

            {!aiLoading && !aiError && aiAnalysis && (
              <div>
                {aiAnalysis.urgency && (
                  <span style={{
                    display: 'inline-flex', alignItems: 'center', padding: '3px 10px', borderRadius: 16, marginBottom: 10,
                    background: `${URGENCY_LABEL[aiAnalysis.urgency]?.color || COLORS.textMuted}18`,
                    color: URGENCY_LABEL[aiAnalysis.urgency]?.color || COLORS.textMuted, fontWeight: 700, fontSize: 11.5,
                  }}>
                    {URGENCY_LABEL[aiAnalysis.urgency]?.text || aiAnalysis.urgency}
                  </span>
                )}
                <div style={{ fontSize: 13, color: COLORS.textPrimary, marginBottom: 10 }}>{aiAnalysis.summary}</div>

                <ImpactedPrincipals impacted={aiAnalysis.impacted_principals} />

                {aiAnalysis.remediation_steps?.length > 0 && (
                  <div style={{ marginBottom: 10 }}>
                    <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 6 }}>
                      Step-by-Step Remediation
                    </div>
                    <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: COLORS.textPrimary, lineHeight: 1.7 }}>
                      {aiAnalysis.remediation_steps.map((step, i) => <li key={i} style={{ marginBottom: 4 }}>{step}</li>)}
                    </ol>
                  </div>
                )}

                {aiAnalysis.awareness_items?.length > 0 && (
                  <div>
                    <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 6 }}>
                      Watch Out For
                    </div>
                    <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: COLORS.textSecondary, lineHeight: 1.7 }}>
                      {aiAnalysis.awareness_items.map((item, i) => <li key={i} style={{ marginBottom: 4 }}>{item}</li>)}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </div>

          {finding.remediation_url && (
            <div style={{ marginTop: 8, fontSize: 11.5 }}>
              <a href={finding.remediation_url} target="_blank" rel="noopener noreferrer" style={{ color: COLORS.brand }}>
                AWS official remediation guide ↗
              </a>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function SecurityHubPage() {
  const [allAccounts, setAllAccounts] = useState([])
  const [selectedAccountIds, setSelectedAccountIds] = useState([]) // empty = all
  const [accountPickerOpen, setAccountPickerOpen] = useState(false)

  const initialCached = getPageCache(summaryCacheKey([]))
  const [summary, setSummary] = useState(initialCached || null)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [err, setErr] = useState('')
  const [expandedAccounts, setExpandedAccounts] = useState({})

  const [aiOpen, setAiOpen] = useState(false)
  const [aiSummary, setAiSummary] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState('')

  const abortRef = React.useRef(null)
  const aiAbortRef = React.useRef(null)
  const seqRef = React.useRef(0)
  const aiSeqRef = React.useRef(0)
  const mountedRef = React.useRef(true)

  useEffect(() => {
    mountedRef.current = true
    api.getSecurityHubAccounts()
      .then(d => { if (mountedRef.current) setAllAccounts(d.accounts || []) })
      .catch(() => {})
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
      aiAbortRef.current?.abort()
    }
  }, [])

  const load = useCallback((refresh = false) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const mySeq = ++seqRef.current
    const key = summaryCacheKey(selectedAccountIds)

    const cachedForThisFilter = getPageCache(key)
    if (cachedForThisFilter && !refresh) {
      setSummary(cachedForThisFilter)
    } else if (refresh) {
      setRefreshing(true)
    } else {
      setLoading(true)
    }
    setErr('')
    api.getSecurityHubSummary(selectedAccountIds, refresh, controller.signal)
      .then(d => {
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setSummary(d)
        setPageCache(key, d)
      })
      .catch(e => {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setErr(e?.response?.data?.detail || 'Could not load AWS Security Hub findings')
      })
      .finally(() => {
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setLoading(false)
        setRefreshing(false)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAccountIds])

  useEffect(() => { load(false) }, [load])

  function toggleAccount(id) {
    setSelectedAccountIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }
  function selectAllAccounts() { setSelectedAccountIds([]) }
  function toggleExpand(accountId) {
    setExpandedAccounts(prev => ({ ...prev, [accountId]: !prev[accountId] }))
  }

  function runAiAnalysis(refresh = false) {
    aiAbortRef.current?.abort()
    const controller = new AbortController()
    aiAbortRef.current = controller
    const mySeq = ++aiSeqRef.current

    setAiOpen(true)
    setAiSummary(null)
    setAiError('')
    setAiLoading(true)
    api.getSecurityHubAiSummary(selectedAccountIds, refresh, controller.signal)
      .then(result => {
        if (!mountedRef.current || mySeq !== aiSeqRef.current) return
        setAiSummary(result)
      })
      .catch(e => {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
        if (!mountedRef.current || mySeq !== aiSeqRef.current) return
        setAiError(e?.response?.data?.detail || 'Could not generate AI security analysis')
      })
      .finally(() => {
        if (!mountedRef.current || mySeq !== aiSeqRef.current) return
        setAiLoading(false)
      })
  }

  const accountPickerLabel = useMemo(() => {
    if (selectedAccountIds.length === 0) return `All accounts (${allAccounts.length})`
    return `${selectedAccountIds.length} account${selectedAccountIds.length === 1 ? '' : 's'} selected`
  }, [selectedAccountIds, allAccounts])

  const totals = summary?.totals || {}
  const severityCounts = totals.severity_counts || {}
  const accounts = summary?.accounts || []

  return (
    <div style={{ padding: '24px 28px', maxWidth: 1400 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 18, flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>🔐 AWS Security Hub</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            Active findings across all AWS accounts, with AI-powered analysis and remediation guidance.
            {summary && !summary.is_mock && <> Last fetched {formatAge(summary.cache_age_seconds)}.</>}
            {summary?.is_mock && <> (local mock data — no real AWS calls in dev mode)</>}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <button onClick={() => runAiAnalysis(false)} disabled={loading}
            style={{ padding: '8px 16px', borderRadius: 7, border: 'none', background: 'linear-gradient(135deg, #1a4080 0%, #7c3aed 100%)', color: '#fff', fontWeight: 700, fontSize: 13, cursor: loading ? 'default' : 'pointer', opacity: loading ? 0.6 : 1 }}>
            🤖 AI Analysis
          </button>
          <button onClick={() => load(true)} disabled={loading || refreshing}
            style={{ padding: '8px 16px', borderRadius: 7, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textPrimary, fontWeight: 700, fontSize: 13, cursor: (loading || refreshing) ? 'default' : 'pointer', opacity: (loading || refreshing) ? 0.6 : 1 }}>
            {refreshing ? '⏳ Refreshing…' : '🔄 Refresh'}
          </button>
        </div>
      </div>

      {err && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {err}
        </div>
      )}

      {/* Account picker */}
      <div style={{ ...CARD, padding: '14px 18px', marginBottom: 16, position: 'relative', display: 'inline-block' }}>
        <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 8 }}>
          Accounts
        </div>
        <button onClick={() => setAccountPickerOpen(o => !o)}
          style={{ padding: '6px 14px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textPrimary, fontSize: 12.5, fontWeight: 600, cursor: 'pointer', minWidth: 220, textAlign: 'left' }}>
          {accountPickerLabel} ▾
        </button>
        {accountPickerOpen && (
          <div style={{ position: 'absolute', top: '100%', left: 18, marginTop: 4, background: '#fff', border: `1px solid ${COLORS.border}`, borderRadius: 8, boxShadow: '0 6px 20px rgba(0,0,0,0.12)', zIndex: 50, width: 320, maxHeight: 320, overflowY: 'auto', padding: 10 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
              <button onClick={selectAllAccounts}
                style={{ fontSize: 11.5, border: 'none', background: 'none', color: COLORS.brand, cursor: 'pointer', fontWeight: 700 }}>
                Select all
              </button>
              <button onClick={() => setAccountPickerOpen(false)}
                style={{ fontSize: 11.5, border: 'none', background: 'none', color: COLORS.textMuted, cursor: 'pointer' }}>
                Close
              </button>
            </div>
            {allAccounts.map(a => (
              <label key={a.account_id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 2px', fontSize: 12.5, color: COLORS.textPrimary, cursor: 'pointer' }}>
                <input type="checkbox"
                  checked={selectedAccountIds.length === 0 || selectedAccountIds.includes(a.account_id)}
                  onChange={() => toggleAccount(a.account_id)} />
                <span style={{ fontWeight: 600 }}>{a.alias}</span>
                <span style={{ color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11 }}>{a.account_id}</span>
              </label>
            ))}
          </div>
        )}
      </div>

      {loading && !summary && (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted }}>Loading…</div>
      )}

      {/* Org-wide KPI cards */}
      {summary && (
        <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 160 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>
              Total Active Findings
            </div>
            <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.textPrimary, marginTop: 4 }}>
              {totals.total_findings ?? 0}
            </div>
          </div>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 260 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 8 }}>
              By Severity
            </div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {SEVERITY_ORDER.map(label => <SeverityPill key={label} label={label} count={severityCounts[label]} />)}
              {SEVERITY_ORDER.every(l => !severityCounts[l]) && <span style={{ color: COLORS.textMuted, fontSize: 12 }}>No active findings</span>}
            </div>
          </div>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 160 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>
              Security Hub Enabled
            </div>
            <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.textPrimary, marginTop: 4 }}>
              {totals.enabled_count ?? 0} / {totals.account_count ?? 0}
            </div>
          </div>
          {totals.error_count > 0 && (
            <div style={{ ...CARD, padding: '14px 20px', minWidth: 140, borderColor: COLORS.danger + '40' }}>
              <div style={{ fontSize: 11, color: COLORS.danger, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>Errors</div>
              <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.danger, marginTop: 4 }}>{totals.error_count}</div>
            </div>
          )}
        </div>
      )}

      {/* Per-account breakdown */}
      <div style={{ ...CARD, overflow: 'hidden' }}>
        <div style={{ padding: '12px 18px', background: '#f8fafc', borderBottom: `1px solid ${COLORS.border}`, fontWeight: 700, color: COLORS.textPrimary, fontSize: 14 }}>
          Findings by Account
        </div>
        {!loading && accounts.length === 0 ? (
          <div style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted }}>No accounts to show.</div>
        ) : (
          accounts.map(account => {
            const isOpen = !!expandedAccounts[account.account_id]
            const accSeverity = account.severity_counts || {}
            const hasFindings = (account.findings || []).length > 0
            return (
              <div key={account.account_id} style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                <div onClick={() => (account.enabled && !account.error) && toggleExpand(account.account_id)}
                  style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 18px', cursor: (account.enabled && !account.error) ? 'pointer' : 'default', flexWrap: 'wrap' }}>
                  <span style={{ color: COLORS.textMuted, fontSize: 11, width: 14 }}>
                    {(account.enabled && !account.error) ? (isOpen ? '▾' : '▸') : ''}
                  </span>
                  <span style={{ fontWeight: 600, color: COLORS.textPrimary }}>{account.alias}</span>
                  <span style={{ color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11 }}>{account.account_id}</span>

                  {account.error && (
                    <span title={account.error} style={{ color: COLORS.danger, fontSize: 11.5, fontWeight: 600, cursor: 'help' }}>⚠️ Fetch failed</span>
                  )}
                  {!account.error && !account.enabled && (
                    <span style={{ color: COLORS.textMuted, fontSize: 11.5 }}>Security Hub not enabled</span>
                  )}
                  {!account.error && account.enabled && (
                    <div style={{ display: 'flex', gap: 6, marginLeft: 'auto', flexWrap: 'wrap' }}>
                      {SEVERITY_ORDER.map(label => <SeverityPill key={label} label={label} count={accSeverity[label]} />)}
                      {SEVERITY_ORDER.every(l => !accSeverity[l]) && <span style={{ color: COLORS.success, fontSize: 12, fontWeight: 600 }}>✓ No active findings</span>}
                      {account.findings_capped && (
                        <span style={{ color: COLORS.textMuted, fontSize: 11 }} title={`Showing top findings only — ${account.total_findings}+ total`}>
                          (capped)
                        </span>
                      )}
                    </div>
                  )}
                </div>
                {isOpen && hasFindings && (
                  <div style={{ borderTop: `1px solid ${COLORS.border}` }}>
                    {account.findings.map(f => <FindingRow key={f.id} finding={f} accountId={account.account_id} />)}
                  </div>
                )}
              </div>
            )
          })
        )}
      </div>

      {aiOpen && (
        <AiSummaryModal
          summary={aiSummary} loading={aiLoading} error={aiError}
          onClose={() => setAiOpen(false)} onRetry={() => runAiAnalysis(true)}
        />
      )}
    </div>
  )
}
