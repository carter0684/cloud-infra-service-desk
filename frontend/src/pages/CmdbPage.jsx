import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../api.js'
import { COLORS, CARD, Badge, InfoBanner, Skeleton, SkeletonLines } from '../utils/theme.js'
import CmdbTopology from '../components/CmdbTopology.jsx'

// Resource types that get the Illumio-style topology diagram, and which
// backend endpoint each one maps to.
const TOPOLOGY_RESOURCE_TYPES = new Set(['EC2 Instance', 'RDS Instance', 'Load Balancer'])

// ── CMDB — Configuration Management Database ────────────────────────────
// Live inventory of AWS hardware/software assets (EC2/RDS/Lambda/S3/
// DynamoDB/Load Balancers) across the whole AWS Organization, reusing
// the same cross-account AssumeRole pattern as Cost Analysis / Billing by
// Service. Read-only for everyone — any logged-in portal user can view,
// browse history, and run an AI impact analysis on a resource.

const STATUS_COLORS = {
  running: COLORS.success, available: COLORS.success, active: COLORS.success,
  ACTIVE: COLORS.success,
  stopped: COLORS.textMuted, inactive: COLORS.textMuted,
  stopping: COLORS.warning, starting: COLORS.warning, creating: COLORS.warning,
  pending: COLORS.warning, modifying: COLORS.warning,
  failed: COLORS.danger, deleting: COLORS.danger, terminated: COLORS.danger,
}

const RESOURCE_TYPE_ICON = {
  'EC2 Instance': '🖥️',
  'RDS Instance': '🗄️',
  'Lambda Function': 'λ',
  'S3 Bucket': '🪣',
  'DynamoDB Table': '📊',
  'Load Balancer': '⚖️',
}

const SEVERITY_COLOR = { critical: COLORS.danger, high: '#ea580c', medium: COLORS.warning, low: COLORS.success }

// ── "What is a CMDB?" educational intro panel ─────────────────────────────
// A short explainer + the 4 core CMDB use cases, shown above the live
// inventory so first-time users understand what this page is for and why
// it matters (asset visibility, change safety, faster incident response,
// capacity planning) — content provided by the user, not auto-generated.
// Collapsible + remembered in localStorage so returning users who've
// already read it aren't forced to scroll past it every visit.
const CMDB_INTRO_COLLAPSED_KEY = 'cmdb_intro_collapsed'

const CMDB_PILLARS = [
  {
    icon: '🔍',
    title: 'Asset Discovery and Inventory',
    desc: "As organizations move to the cloud, they need to maintain an accurate inventory of their cloud-based assets. A CMDB helps to discover, catalog, and track cloud resources such as virtual machines, containers, storage, and network configurations. This inventory aids in providing visibility and ensuring the organization adheres to compliance requirements.",
  },
  {
    icon: '🔄',
    title: 'Change Management',
    desc: 'The dynamic nature of cloud environments necessitates robust change management processes. The CMDB aids in planning and implementing changes to cloud resources by providing a clear view of the existing environment, dependencies, and potential risks. It also helps in tracking the impact of changes and aids in rollback procedures if necessary.',
  },
  {
    icon: '🚨',
    title: 'Incident and Problem Management',
    desc: 'The CMDB assists in identifying and resolving incidents and problems in cloud environments by offering a comprehensive view of the affected resources and their dependencies. This information helps to speed up root cause analysis and ensure that the correct remediation actions are taken.',
  },
  {
    icon: '📈',
    title: 'Capacity Management',
    desc: 'As organizations scale their cloud infrastructure, they need to manage resources efficiently to avoid overprovisioning and wasted costs. The CMDB provides insights into resource usage trends, enabling IT teams to make informed decisions about capacity planning and optimization.',
  },
]

function statusColor(status) {
  if (!status) return COLORS.textMuted
  return STATUS_COLORS[status] || STATUS_COLORS[status.toLowerCase()] || COLORS.textMuted
}

function formatAge(seconds) {
  if (!seconds || seconds < 60) return 'just now'
  const mins = Math.floor(seconds / 60)
  return `${mins} min${mins === 1 ? '' : 's'} ago`
}

function formatEventLabel(eventType) {
  return { added: '➕ Added', removed: '➖ Removed', status_changed: '🔄 Status changed' }[eventType] || eventType
}

// Resource status / AI impact severity badges use the shared <Badge>
// from theme.js (imported above) so they look identical to every other
// status pill across the portal (ticket status/priority, feedback type,
// case account tag, etc.) instead of each page having its own copy.

export default function CmdbPage() {
  const [inventory, setInventory] = useState(null)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [err, setErr] = useState('')
  const [collapsed, setCollapsed] = useState({})
  const [typeFilter, setTypeFilter] = useState('') // '' = all types
  const [searchText, setSearchText] = useState('')

  const [historyOpen, setHistoryOpen] = useState(false)
  const [history, setHistory] = useState([])
  const [historyLoading, setHistoryLoading] = useState(false)

  const [impactModal, setImpactModal] = useState(null) // { resource, accountId, accountName, loading, result, error }
  const [topologyModal, setTopologyModal] = useState(null) // { resource, accountId, accountName, loading, result, error }

  const [introCollapsed, setIntroCollapsed] = useState(() => {
    try { return localStorage.getItem(CMDB_INTRO_COLLAPSED_KEY) === '1' } catch { return false }
  })
  function toggleIntro() {
    setIntroCollapsed(prev => {
      const next = !prev
      try { localStorage.setItem(CMDB_INTRO_COLLAPSED_KEY, next ? '1' : '0') } catch { /* ignore storage errors */ }
      return next
    })
  }

  const abortRef = React.useRef(null)
  const seqRef = React.useRef(0)
  const mountedRef = React.useRef(true)
  const pollTimerRef = React.useRef(null)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
    }
  }, [])

  // The org-wide scan (24 accounts x 6 services x every region) now runs
  // as a background job on the server — the API call itself always
  // returns immediately with whatever is cached (or a "pending" empty
  // shell on the very first call). fetch_status tells us whether a
  // background fetch is still running, in which case we keep polling
  // every few seconds until it flips to "ready", instead of the old
  // design where the HTTP request itself blocked for up to ~15 minutes
  // (which was starving the rest of the portal of CPU on the shared
  // backend container).
  const load = useCallback((refresh = false) => {
    abortRef.current?.abort()
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current)
    const controller = new AbortController()
    abortRef.current = controller
    const mySeq = ++seqRef.current

    ;(refresh ? setRefreshing : setLoading)(true)
    setErr('')

    const poll = (isRefresh) => {
      api.getCmdbInventory(isRefresh, controller.signal)
        .then(d => {
          if (!mountedRef.current || mySeq !== seqRef.current) return
          setInventory(d)
          if (d?.fetch_status === 'pending' || d?.fetch_status === 'refreshing') {
            pollTimerRef.current = setTimeout(() => poll(false), 5000)
          } else {
            setLoading(false)
            setRefreshing(false)
          }
        })
        .catch(e => {
          if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
          if (!mountedRef.current || mySeq !== seqRef.current) return
          setErr(e?.response?.data?.detail || 'Could not load CMDB inventory')
          setLoading(false)
          setRefreshing(false)
        })
    }
    poll(refresh)
  }, [])

  useEffect(() => { load(false) }, [load])

  function toggleCollapse(accountId) {
    setCollapsed(prev => ({ ...prev, [accountId]: !prev[accountId] }))
  }

  function openHistory() {
    setHistoryOpen(true)
    setHistoryLoading(true)
    api.getCmdbHistory(null, 300)
      .then(d => setHistory(d.events || []))
      .catch(() => setHistory([]))
      .finally(() => setHistoryLoading(false))
  }

  function openImpactAnalysis(resource, accountId, accountName) {
    setImpactModal({ resource, accountId, accountName, loading: true, result: null, error: '' })
    api.analyzeCmdbImpact(resource, accountId, accountName)
      .then(result => setImpactModal(prev => (prev ? { ...prev, loading: false, result } : prev)))
      .catch(e => setImpactModal(prev => (prev ? { ...prev, loading: false, error: e?.response?.data?.detail || 'AI analysis failed' } : prev)))
  }

  function openTopology(resource, accountId, accountName) {
    setTopologyModal({ resource, accountId, accountName, loading: true, result: null, error: '' })
    const fetchPromise = resource.resource_type === 'RDS Instance'
      ? api.getCmdbRdsTopology(accountId, resource.resource_id)
      : resource.resource_type === 'Load Balancer'
        ? api.getCmdbLbTopology(accountId, resource.resource_id)
        : api.getCmdbEc2Topology(accountId, resource.resource_id)
    fetchPromise
      .then(result => setTopologyModal(prev => (prev ? { ...prev, loading: false, result } : prev)))
      .catch(e => setTopologyModal(prev => (prev ? { ...prev, loading: false, error: e?.response?.data?.detail || 'Could not load network topology' } : prev)))
  }

  const accounts = inventory?.accounts || []

  const availableTypes = useMemo(() => {
    const types = new Set()
    for (const a of accounts) for (const r of (a.resources || [])) types.add(r.resource_type)
    return Array.from(types).sort()
  }, [accounts])

  const filteredAccounts = useMemo(() => {
    const q = searchText.trim().toLowerCase()
    return accounts.map(a => ({
      ...a,
      resources: (a.resources || []).filter(r => {
        if (typeFilter && r.resource_type !== typeFilter) return false
        if (!q) return true
        return (r.name || '').toLowerCase().includes(q) || (r.resource_id || '').toLowerCase().includes(q)
      }),
    }))
  }, [accounts, typeFilter, searchText])

  return (
    <div style={{ padding: '24px 28px', maxWidth: 1400 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 18, flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>🗄️ CMDB — Asset Inventory</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            Live AWS resource inventory (EC2 / RDS / Lambda / S3 / DynamoDB / Load Balancers) across every account.
            {inventory && !inventory.is_mock && inventory.fetch_status === 'ready' && <> Cached {formatAge(inventory.cache_age_seconds)}.</>}
            {inventory?.is_mock && <> (local mock data — no real AWS calls in dev mode)</>}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={openHistory}
            style={{ padding: '8px 16px', borderRadius: 7, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontWeight: 600, fontSize: 13, cursor: 'pointer' }}>
            🕘 History
          </button>
          <button onClick={() => load(true)} disabled={loading || refreshing}
            style={{ padding: '8px 16px', borderRadius: 7, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontWeight: 600, fontSize: 13, cursor: (loading || refreshing) ? 'default' : 'pointer' }}>
            {refreshing ? '⏳ Refreshing in background…' : loading ? '⏳ Loading…' : '🔄 Refresh'}
          </button>
        </div>
      </div>

      <InfoBanner icon="🔒" text="Read-only view — resource data is fetched live from AWS across every account and cannot be modified here." />

      <div style={{ ...CARD, marginBottom: 18, overflow: 'hidden', background: 'linear-gradient(135deg, #0f2e5a 0%, #1a4080 100%)', border: 'none' }}>
        <div onClick={toggleIntro}
          style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '16px 22px', cursor: 'pointer' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ fontSize: 20 }}>📖</span>
            <span style={{ fontWeight: 700, color: '#fff', fontSize: 15 }}>What is a CMDB?</span>
          </div>
          <span style={{ color: '#cbd5e1', fontSize: 12, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6 }}>
            {introCollapsed ? 'Show details' : 'Hide'}
            <span style={{ display: 'inline-block', transform: introCollapsed ? 'rotate(-90deg)' : 'none', transition: 'transform .15s' }}>▾</span>
          </span>
        </div>
        {!introCollapsed && (
          <div style={{ padding: '0 22px 22px' }}>
            <p style={{ color: '#e2e8f0', fontSize: 13.5, lineHeight: 1.6, margin: '0 0 18px' }}>
              A Configuration Management Database (CMDB) is a centralized repository of information related to all the hardware and software components (Configuration Items or CIs) within an organization's IT environment. This database serves as a comprehensive source of truth, enabling IT teams to track and manage assets, plan and execute changes, and maintain a clear understanding of the relationships and dependencies between assets.
            </p>
            <p style={{ color: '#e2e8f0', fontSize: 13.5, lineHeight: 1.6, margin: '0 0 20px' }}>
              The main goal of a CMDB is to provide an organization with the information needed to make better business decisions and run efficient ITSM processes. By centralizing all configuration information, leaders can better understand critical CIs and their relationships. CMDBs are important in impact analysis, root cause analysis, legal compliance as well as incident and change management.
            </p>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 14 }}>
              {CMDB_PILLARS.map((p, i) => (
                <div key={p.title} style={{ background: 'rgba(255,255,255,0.07)', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 10, padding: '14px 16px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                    <span style={{ fontSize: 17 }}>{p.icon}</span>
                    <span style={{ fontWeight: 700, color: '#fff', fontSize: 13 }}>{i + 1}. {p.title}</span>
                  </div>
                  <div style={{ color: '#cbd5e1', fontSize: 12, lineHeight: 1.55 }}>{p.desc}</div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {inventory?.stale_fallback && (
        <div style={{ background: COLORS.warningBg, border: `1px solid ${COLORS.warning}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.warning, fontSize: 13 }}>
          ⚠️ Every account failed on the last refresh attempt — showing the last known-good inventory instead.
        </div>
      )}

      {(inventory?.fetch_status === 'pending' || inventory?.fetch_status === 'refreshing') && (
        <div style={{ background: '#eff6ff', border: `1px solid ${COLORS.brand}30`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.brand, fontSize: 13 }}>
          ⏳ Scanning all accounts/regions in the background — this can take up to ~15 minutes on the first load. The page will update automatically; feel free to keep browsing other tabs while it runs.
          {inventory.accounts?.length > 0 && <> Showing {inventory.fetch_status === 'refreshing' ? 'the previous' : ''} cached data below in the meantime.</>}
        </div>
      )}

      {err && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {err}
        </div>
      )}

      {inventory && (
        <div style={{ ...CARD, padding: '14px 18px', marginBottom: 16, display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'center' }}>
          <div>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', fontWeight: 700 }}>Total resources</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: COLORS.textPrimary }}>{inventory.total_resources}</div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', fontWeight: 700 }}>Accounts</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: COLORS.textPrimary }}>
              {inventory.account_count}{inventory.error_count > 0 && <span style={{ color: COLORS.danger, fontSize: 13, marginLeft: 6 }}>({inventory.error_count} failed)</span>}
            </div>
          </div>
          {Object.entries(inventory.resource_type_totals || {}).map(([type, count]) => (
            <div key={type}>
              <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', fontWeight: 700 }}>{RESOURCE_TYPE_ICON[type] || ''} {type}</div>
              <div style={{ fontSize: 15, fontWeight: 700, color: COLORS.textPrimary }}>{count}</div>
            </div>
          ))}
        </div>
      )}

      <div style={{ ...CARD, padding: '12px 18px', marginBottom: 16, display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'center' }}>
        <input placeholder="Search by name or ID…" value={searchText} onChange={e => setSearchText(e.target.value)}
          style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5, minWidth: 220 }} />
        <select value={typeFilter} onChange={e => setTypeFilter(e.target.value)}
          style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5 }}>
          <option value="">All resource types</option>
          {availableTypes.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>

      {loading && (!inventory || inventory.accounts?.length === 0) && (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted }}>
          {inventory?.fetch_status === 'pending' ? '⏳ Starting first-time scan of all accounts… this can take a while.' : 'Loading…'}
        </div>
      )}

      {!loading && filteredAccounts.length === 0 && inventory && (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted }}>No accounts found.</div>
      )}

      {filteredAccounts.map(account => {
        const isCollapsed = !!collapsed[account.account_id]
        return (
          <div key={account.account_id} style={{ ...CARD, marginBottom: 14, overflow: 'hidden' }}>
            <div onClick={() => toggleCollapse(account.account_id)}
              style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px 18px', cursor: 'pointer', background: '#f8fafc', borderBottom: isCollapsed ? 'none' : `1px solid ${COLORS.border}` }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ display: 'inline-block', width: 12, transform: isCollapsed ? 'rotate(-90deg)' : 'none', transition: 'transform .15s', color: COLORS.textMuted }}>▾</span>
                <span style={{ fontWeight: 700, color: COLORS.textPrimary, fontSize: 14 }}>{account.account_name}</span>
                <span style={{ color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11.5 }}>{account.account_id}</span>
              </div>
              {account.error ? (
                <span title={account.error} style={{ color: COLORS.danger, fontSize: 11.5, fontWeight: 600, cursor: 'help' }}>⚠️ Fetch failed</span>
              ) : (
                <span style={{ color: COLORS.textSecondary, fontSize: 12.5, fontWeight: 600 }}>{account.resources.length} resource{account.resources.length === 1 ? '' : 's'}</span>
              )}
            </div>

            {!isCollapsed && !account.error && (
              <div style={{ overflowX: 'auto' }}>
                <table data-cards style={{ width: '100%', minWidth: 800, borderCollapse: 'collapse', fontSize: 12.5 }}>
                  <thead>
                    <tr style={{ background: '#fafbfc', borderBottom: `1px solid ${COLORS.border}` }}>
                      <th style={{ padding: '8px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Type</th>
                      <th style={{ padding: '8px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Name / ID</th>
                      <th style={{ padding: '8px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Region</th>
                      <th style={{ padding: '8px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Status</th>
                      <th style={{ padding: '8px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Tags</th>
                      <th style={{ padding: '8px 14px' }}></th>
                    </tr>
                  </thead>
                  <tbody>
                    {account.resources.length === 0 ? (
                      <tr><td colSpan={6} style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted }}>No resources match the current filter.</td></tr>
                    ) : account.resources.map((r, idx) => (
                      <tr key={idx} style={{ borderBottom: '1px solid #f1f5f9' }}>
                        <td style={{ padding: '7px 14px', color: COLORS.textPrimary, whiteSpace: 'nowrap' }}>{RESOURCE_TYPE_ICON[r.resource_type] || ''} {r.resource_type}</td>
                        <td style={{ padding: '7px 14px', color: COLORS.textPrimary }}>
                          <div style={{ fontWeight: 600 }}>{r.name}</div>
                          {r.name !== r.resource_id && <div style={{ color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11 }}>{r.resource_id}</div>}
                        </td>
                        <td style={{ padding: '7px 14px', color: '#475569' }}>{r.region}</td>
                        <td style={{ padding: '7px 14px' }}>
                          <Badge text={r.status || 'unknown'} color={statusColor(r.status)} />
                        </td>
                        <td style={{ padding: '7px 14px', color: COLORS.textMuted, fontSize: 11 }}>
                          {Object.keys(r.tags || {}).length === 0 ? '—' : Object.entries(r.tags).slice(0, 3).map(([k, v]) => `${k}=${v}`).join(', ')}
                        </td>
                        <td style={{ padding: '7px 14px', textAlign: 'right', whiteSpace: 'nowrap' }}>
                          {TOPOLOGY_RESOURCE_TYPES.has(r.resource_type) && (
                            <button onClick={() => openTopology(r, account.account_id, account.account_name)}
                              style={{ padding: '4px 10px', borderRadius: 6, border: `1px solid ${COLORS.success}`, background: '#fff', color: COLORS.success, fontSize: 11, fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap', marginRight: 6 }}>
                              🔗 Topology
                            </button>
                          )}
                          <button onClick={() => openImpactAnalysis(r, account.account_id, account.account_name)}
                            style={{ padding: '4px 10px', borderRadius: 6, border: `1px solid ${COLORS.brand}`, background: '#fff', color: COLORS.brand, fontSize: 11, fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap' }}>
                            🤖 AI Impact
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )
      })}

      {/* History drawer/modal */}
      {historyOpen && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)', display: 'flex', justifyContent: 'flex-end', zIndex: 100 }}
          onClick={() => setHistoryOpen(false)}>
          <div style={{ background: '#fff', width: 480, maxWidth: '90vw', height: '100%', overflowY: 'auto', padding: 20, boxShadow: '-4px 0 20px rgba(0,0,0,0.15)' }}
            onClick={e => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
              <h3 style={{ margin: 0, fontSize: 16, color: COLORS.textPrimary }}>🕘 Resource History</h3>
              <button onClick={() => setHistoryOpen(false)} style={{ border: 'none', background: 'none', fontSize: 18, cursor: 'pointer', color: COLORS.textMuted }}>×</button>
            </div>
            <div style={{ color: COLORS.textSecondary, fontSize: 12, marginBottom: 12 }}>
              Recent added/removed/status-changed events, across all accounts, newest first.
            </div>
            {historyLoading && <div style={{ padding: '4px 0 12px' }}><SkeletonLines lines={4} /></div>}
            {!historyLoading && history.length === 0 && (
              <div style={{ color: COLORS.textMuted, padding: 20, textAlign: 'center' }}>No history recorded yet.</div>
            )}
            {!historyLoading && history.map((ev, idx) => (
              <div key={ev.id || idx} style={{ padding: '10px 0', borderBottom: `1px solid ${COLORS.border}` }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12.5, fontWeight: 600, color: COLORS.textPrimary }}>
                  <span>{formatEventLabel(ev.event_type)}</span>
                  <span style={{ color: COLORS.textMuted, fontWeight: 400, fontSize: 11 }}>{ev.timestamp ? new Date(ev.timestamp).toLocaleString() : ''}</span>
                </div>
                <div style={{ fontSize: 12, color: COLORS.textSecondary, marginTop: 2 }}>
                  {RESOURCE_TYPE_ICON[ev.resource_type] || ''} {ev.resource_type} — <strong>{ev.name || ev.resource_id}</strong> ({ev.region || '—'})
                </div>
                {ev.event_type === 'status_changed' && (
                  <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginTop: 2 }}>{ev.previous_status || '?'} → {ev.status || '?'}</div>
                )}
                <div style={{ fontSize: 10.5, color: COLORS.textMuted, marginTop: 2, fontFamily: 'monospace' }}>Account: {ev.account_id}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* EC2 network topology modal (Illumio-style traffic diagram) */}
      {topologyModal && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}
          onClick={() => setTopologyModal(null)}>
          <div style={{ background: '#fff', width: 900, maxWidth: '95vw', maxHeight: '92vh', overflowY: 'auto', borderRadius: 12, padding: 22, boxShadow: '0 10px 40px rgba(0,0,0,0.2)' }}
            onClick={e => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 4 }}>
              <h3 style={{ margin: 0, fontSize: 16, color: COLORS.textPrimary }}>🔗 Network Topology</h3>
              <button onClick={() => setTopologyModal(null)} style={{ border: 'none', background: 'none', fontSize: 18, cursor: 'pointer', color: COLORS.textMuted }}>×</button>
            </div>
            <div style={{ fontSize: 12.5, color: COLORS.textSecondary, marginBottom: 16 }}>
              {RESOURCE_TYPE_ICON[topologyModal.resource.resource_type] || ''} {topologyModal.resource.resource_type} — <strong>{topologyModal.resource.name}</strong>
              <div style={{ fontFamily: 'monospace', fontSize: 11, color: COLORS.textMuted }}>{topologyModal.accountName} ({topologyModal.accountId})</div>
            </div>

            {topologyModal.loading && <div style={{ padding: 30, textAlign: 'center', color: COLORS.textMuted }}>⏳ Building network topology (checking VPC Flow Logs, security groups, and traffic)…</div>}

            {topologyModal.error && (
              <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', color: COLORS.danger, fontSize: 13 }}>
                {topologyModal.error}
              </div>
            )}

            {topologyModal.result && (
              <div>
                <CmdbTopology topology={topologyModal.result} />
                <div style={{ marginTop: 12, fontSize: 11, color: COLORS.textMuted }}>
                  {topologyModal.result.has_flow_logs
                    ? `Live traffic peers from VPC Flow Logs, last ${topologyModal.result.lookback_minutes} minutes. Drag nodes to rearrange; hover a security group for its rules.`
                    : `No CloudWatch-Logs-backed VPC Flow Log found for this resource's VPC — showing topology with a generic traffic-activity indicator from CloudWatch metrics instead (last ${topologyModal.result.lookback_minutes} minutes).`}
                  {' '}Generated {topologyModal.result.as_of}.
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* AI impact analysis modal */}
      {impactModal && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}
          onClick={() => setImpactModal(null)}>
          <div style={{ background: '#fff', width: 520, maxWidth: '90vw', maxHeight: '85vh', overflowY: 'auto', borderRadius: 12, padding: 22, boxShadow: '0 10px 40px rgba(0,0,0,0.2)' }}
            onClick={e => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 4 }}>
              <h3 style={{ margin: 0, fontSize: 16, color: COLORS.textPrimary }}>🤖 AI Impact Analysis</h3>
              <button onClick={() => setImpactModal(null)} style={{ border: 'none', background: 'none', fontSize: 18, cursor: 'pointer', color: COLORS.textMuted }}>×</button>
            </div>
            <div style={{ fontSize: 12.5, color: COLORS.textSecondary, marginBottom: 16 }}>
              {RESOURCE_TYPE_ICON[impactModal.resource.resource_type] || ''} {impactModal.resource.resource_type} — <strong>{impactModal.resource.name}</strong>
              <div style={{ fontFamily: 'monospace', fontSize: 11, color: COLORS.textMuted }}>{impactModal.accountName} ({impactModal.accountId})</div>
            </div>

            {impactModal.loading && <div style={{ padding: 30, textAlign: 'center', color: COLORS.textMuted }}>⏳ Analysing what would break if this resource fails…</div>}

            {impactModal.error && (
              <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', color: COLORS.danger, fontSize: 13 }}>
                {impactModal.error}
              </div>
            )}

            {impactModal.result && (
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
                  <Badge text={`${(impactModal.result.severity || 'unknown').toUpperCase()} IMPACT`} color={SEVERITY_COLOR[impactModal.result.severity] || COLORS.textMuted} />
                </div>
                <p style={{ fontSize: 13, color: COLORS.textPrimary, lineHeight: 1.5, marginTop: 0 }}>{impactModal.result.impact_summary}</p>

                {impactModal.result.likely_affected?.length > 0 && (
                  <div style={{ marginTop: 14 }}>
                    <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', fontWeight: 700, marginBottom: 6 }}>Likely affected</div>
                    <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: COLORS.textSecondary }}>
                      {impactModal.result.likely_affected.map((x, i) => <li key={i}>{x}</li>)}
                    </ul>
                  </div>
                )}

                {impactModal.result.recommendations?.length > 0 && (
                  <div style={{ marginTop: 14 }}>
                    <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', fontWeight: 700, marginBottom: 6 }}>Recommendations</div>
                    <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: COLORS.textSecondary }}>
                      {impactModal.result.recommendations.map((x, i) => <li key={i}>{x}</li>)}
                    </ul>
                  </div>
                )}

                {impactModal.result.bottom_line && (
                  <div style={{ marginTop: 14, padding: '10px 14px', background: COLORS.brandLight, borderRadius: 8, fontSize: 12.5, color: COLORS.brand, fontWeight: 600 }}>
                    {impactModal.result.bottom_line}
                  </div>
                )}

                <div style={{ marginTop: 14, fontSize: 10.5, color: COLORS.textMuted }}>
                  Generated by {impactModal.result.model_used} · {impactModal.result.generated_at}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
