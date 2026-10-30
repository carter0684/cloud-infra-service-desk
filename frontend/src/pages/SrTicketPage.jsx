import React, { useState, useEffect, useMemo } from 'react'
import { api } from '../api.js'
import { getPageCache, setPageCache } from '../utils/pageCache.js'

// Renders the separate "SR Ticket Information" system's "Service Item
// Report" natively in the portal — no iframe, no separate login. That
// system's own username/password live only in AWS Secrets Manager
// ("job-portal/sr-ticket-credentials") and are used server-side by
// backend/app/sr_ticket.py (session-cookie login against its ITSM
// Support Portal); the frontend just calls our own /api/sr-ticket/*
// proxy route (same Azure AD session as the rest of the portal), so
// users never see a second login prompt. Mirrors the Freshservice
// Change Calendar tab (see CalendarsPage.jsx / FsChangeCalendar.jsx).

function defaultRange() {
  const end = new Date()
  const start = new Date()
  start.setDate(start.getDate() - 32)
  const ymd = d => d.toISOString().slice(0, 10)
  return { start: ymd(start), end: ymd(end) }
}

function cacheKey(filters) {
  return `sr-ticket-information:${JSON.stringify(filters)}`
}

const STATUS_COLOR = {
  approved: { bg: '#dcfce7', text: '#166534' },
  fulfilled: { bg: '#dcfce7', text: '#166534' },
  requested: { bg: '#fef3c7', text: '#92400e' },
  'not requested': { bg: '#f1f5f9', text: '#475569' },
  cancelled: { bg: '#f1f5f9', text: '#64748b' },
  rejected: { bg: '#fee2e2', text: '#991b1b' },
  'partially fulfilled': { bg: '#dbeafe', text: '#1e40af' },
  delivered: { bg: '#dcfce7', text: '#166534' },
}
function statusColor(status) {
  const key = String(status || '').toLowerCase()
  return STATUS_COLOR[key] || { bg: '#f1f5f9', text: '#475569' }
}

export default function SrTicketPage() {
  const [filters, setFilters] = useState(() => ({
    ...defaultRange(),
    serviceItem: '', workspace: '', requester: '', approvalStatus: '', requestStatus: '',
    outstandingOnly: false,
  }))

  const initialCached = getPageCache(cacheKey(filters))
  const [report, setReport] = useState(initialCached || null)
  const [loading, setLoading] = useState(!initialCached)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')

  const load = React.useCallback((refresh = false) => {
    const key = cacheKey(filters)
    const cached = getPageCache(key)
    if (cached && !refresh) {
      setReport(cached)
    } else if (refresh) {
      setRefreshing(true)
    } else {
      setLoading(true)
    }
    setError('')
    return api.srTicketTickets(filters, refresh)
      .then(d => {
        setReport(d)
        setPageCache(key, d)
      })
      .catch(e => setError(e?.response?.data?.detail || 'Could not load SR Ticket Information'))
      .finally(() => { setLoading(false); setRefreshing(false) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters])

  useEffect(() => { load(false) }, [load])

  // Server-side cache is 5 minutes (see sr_ticket.py's _CACHE) —
  // silently re-fetch every 5 minutes in the background so a tab left
  // open never sits stale for long.
  useEffect(() => {
    const id = setInterval(() => { load(false) }, 5 * 60 * 1000)
    return () => clearInterval(id)
  }, [load])

  function setFilter(key, value) {
    setFilters(f => ({ ...f, [key]: value }))
  }

  const columns = report?.details?.columns || []
  const rows = report?.details?.rows || []
  const options = report?.options || {}

  const filteredRows = useMemo(() => {
    if (!query.trim()) return rows
    const q = query.trim().toLowerCase()
    return rows.filter(r => JSON.stringify(r.values || {}).toLowerCase().includes(q))
  }, [rows, query])

  return (
    <div style={{ maxWidth: 1300, margin: '0 auto', padding: '20px 20px 40px' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 16, flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, color: '#0f2e5a', fontSize: 20 }}>🎫 SR Ticket Information</h2>
          <div style={{ color: '#64748b', fontSize: 13, marginTop: 4 }}>
            Service Item Report from the ITSM Support Portal — no separate login required.
          </div>
        </div>
        <button onClick={() => load(true)} disabled={loading || refreshing}
          style={{ padding: '8px 16px', borderRadius: 7, border: '1px solid #e2e8f0', background: '#fff', color: '#64748b', fontWeight: 600, fontSize: 13, cursor: (loading || refreshing) ? 'default' : 'pointer' }}>
          {refreshing ? '⏳ Refreshing…' : loading ? '⏳ Loading…' : '🔄 Refresh'}
        </button>
      </div>

      {/* Filters — mirrors the source app's own filter bar */}
      <div style={{ background: '#fff', borderRadius: 10, boxShadow: '0 1px 6px rgba(0,0,0,0.07)', padding: '14px 16px', marginBottom: 16, display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <Field label="Request date from">
          <input type="date" value={filters.start} onChange={e => setFilter('start', e.target.value)} style={inputStyle} />
        </Field>
        <Field label="Request date to">
          <input type="date" value={filters.end} onChange={e => setFilter('end', e.target.value)} style={inputStyle} />
        </Field>
        <Field label="Workspace">
          <select value={filters.workspace} onChange={e => setFilter('workspace', e.target.value)} style={inputStyle}>
            <option value="">All workspaces</option>
            {(options.workspaces || []).map(w => <option key={w} value={w}>{w}</option>)}
          </select>
        </Field>
        <Field label="Service item">
          <select value={filters.serviceItem} onChange={e => setFilter('serviceItem', e.target.value)} style={inputStyle}>
            <option value="">All service items</option>
            {(options.serviceItems || []).map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </Field>
        <Field label="Requester">
          <select value={filters.requester} onChange={e => setFilter('requester', e.target.value)} style={inputStyle}>
            <option value="">All requesters</option>
            {(options.requesters || []).map(r => <option key={r} value={r}>{r}</option>)}
          </select>
        </Field>
        <Field label="Approval status">
          <select value={filters.approvalStatus} onChange={e => setFilter('approvalStatus', e.target.value)} style={inputStyle}>
            <option value="">All</option>
            {(options.approvalStatuses || []).map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </Field>
        <Field label="Request status">
          <select value={filters.requestStatus} onChange={e => setFilter('requestStatus', e.target.value)} style={inputStyle}>
            <option value="">All</option>
            {(options.requestStatuses || []).map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </Field>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, fontWeight: 600, color: '#374151', height: 34 }}>
          <input type="checkbox" checked={filters.outstandingOnly} onChange={e => setFilter('outstandingOnly', e.target.checked)} />
          Outstanding only
        </label>
      </div>

      {!loading && !error && report && (
        <div style={{ fontSize: 12.5, color: '#64748b', marginBottom: 10 }}>
          Period {report.period?.label} · Outstanding {report.totals?.outstanding} · New {report.totals?.new} · Total {report.totals?.total}.
          {report.cache_age_seconds > 60 && ` (cached ${Math.floor(report.cache_age_seconds / 60)} min ago, refreshes automatically every 5 minutes)`}
        </div>
      )}

      {error && (
        <div style={{ background: '#fef2f2', color: '#dc2626', padding: '10px 14px', borderRadius: 6, fontSize: 13, marginBottom: 14 }}>{error}</div>
      )}

      {/* Summary by service item */}
      {report?.summary?.length > 0 && (
        <div style={{ background: '#fff', borderRadius: 10, boxShadow: '0 1px 6px rgba(0,0,0,0.07)', overflow: 'hidden', marginBottom: 16 }}>
          <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
            <thead>
              <tr style={{ background: '#f8fafc', borderBottom: '2px solid #e2e8f0' }}>
                {['Service Item', 'Outstanding', 'New', 'Total', 'Fulfilment Group'].map(h => (
                  <th key={h} style={{ padding: '8px 14px', textAlign: h === 'Service Item' || h === 'Fulfilment Group' ? 'left' : 'center', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {report.summary.map(s => (
                <tr key={s.item} style={{ borderBottom: '1px solid #f1f5f9' }}>
                  <td style={{ padding: '7px 14px', fontWeight: 600, color: '#1e293b' }}>{s.item}</td>
                  <td style={{ padding: '7px 14px', textAlign: 'center' }}>{s.outstanding}</td>
                  <td style={{ padding: '7px 14px', textAlign: 'center' }}>{s.new}</td>
                  <td style={{ padding: '7px 14px', textAlign: 'center', fontWeight: 700 }}>{s.total}</td>
                  <td style={{ padding: '7px 14px', color: '#64748b' }}>{s.fulfilmentGroup || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!loading && rows.length > 0 && (
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder="Search tickets…"
          style={{ padding: '8px 12px', borderRadius: 7, border: '1px solid #e2e8f0', fontSize: 13, width: 280, marginBottom: 14 }}
        />
      )}

      <div style={{ background: '#fff', borderRadius: 10, boxShadow: '0 1px 6px rgba(0,0,0,0.07)', overflow: 'hidden' }}>
        {loading ? (
          <div style={{ padding: 40, textAlign: 'center', color: '#94a3b8' }}>Loading…</div>
        ) : columns.length === 0 ? (
          <div style={{ padding: 40, textAlign: 'center', color: '#94a3b8' }}>No SR tickets found for the selected date range and filters.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table data-cards style={{ width: '100%', minWidth: 700 + columns.length * 90, borderCollapse: 'collapse', fontSize: 12.5 }}>
              <thead>
                <tr style={{ background: '#f8fafc', borderBottom: '2px solid #e2e8f0' }}>
                  {columns.map(c => (
                    <th key={c} style={{ padding: '10px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.3, whiteSpace: 'nowrap' }}>
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filteredRows.length === 0 ? (
                  <tr><td colSpan={columns.length} style={{ padding: 30, textAlign: 'center', color: '#94a3b8' }}>No matching tickets.</td></tr>
                ) : filteredRows.map(row => (
                  <tr key={row.id} style={{ borderBottom: '1px solid #f1f5f9' }}>
                    {columns.map(c => {
                      const value = row.values?.[c]
                      if (c === 'Ticket ID') {
                        return (
                          <td key={c} style={{ padding: '8px 14px' }}>
                            {row.url ? (
                              <a href={row.url} target="_blank" rel="noopener noreferrer" style={{ color: '#2563eb', fontWeight: 600, textDecoration: 'none' }}>{value || `#${row.id}`}</a>
                            ) : (value || `#${row.id}`)}
                          </td>
                        )
                      }
                      if (c === 'Approval Status' || c === 'Fulfilment Status') {
                        const color = statusColor(value)
                        return (
                          <td key={c} style={{ padding: '8px 14px' }}>
                            <span style={{ padding: '3px 10px', borderRadius: 20, fontSize: 11.5, fontWeight: 600, background: color.bg, color: color.text }}>
                              {value || '—'}
                            </span>
                          </td>
                        )
                      }
                      return (
                        <td key={c} style={{ padding: '8px 14px', color: '#1e293b' }}>{value ?? '—'}</td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function Field({ label, children }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <label style={{ fontSize: 11.5, fontWeight: 600, color: '#374151' }}>{label}</label>
      {children}
    </div>
  )
}

const inputStyle = {
  padding: '8px 10px', borderRadius: 7, border: '1px solid #e2e8f0', fontSize: 13,
  fontFamily: 'inherit', background: '#fff', color: '#1e293b', minWidth: 150,
}
