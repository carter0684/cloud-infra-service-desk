import React, { useState, useEffect, useCallback } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { formatDateHK, monthKeyHK, monthLabelHK } from '../utils/time.js'
import { COLORS, CARD, STATUS_COLOR, PRIORITY_COLOR, formatTicketNumber, Badge, Skeleton } from '../utils/theme.js'
import { useAuth } from '../auth/AuthContext.jsx'
import useIsMobile from '../utils/useIsMobile.js'

function isDueSoon(due_date) {
  if (!due_date) return false
  const diff = new Date(due_date) - new Date()
  return diff > 0 && diff < 24 * 60 * 60 * 1000
}

// Table column headers — clicking any header with a `field` sorts the
// list by it (toggling increase/decrease on repeated clicks of the same
// column), replacing the old standalone "Sort By" dropdown + button.
// Category/Assignee/Created By have no backing sort field on the
// backend (JOB_SORT_FIELDS in main.py), so they stay plain, unclickable
// headers.
const COLUMNS = [
  { label: 'Ticket #', field: 'ticket_number', width: 110 },
  { label: 'Title', field: 'title', width: '26%' },
  { label: 'Category', field: null, width: '12%' },
  { label: 'Priority', field: 'priority', width: 90 },
  { label: 'Assignee', field: null, width: '13%' },
  { label: 'Status', field: 'status', width: 120 },
  { label: 'Created By', field: null, width: '12%' },
  { label: 'Created', field: 'created_at', width: 95 },
  { label: 'Due Date', field: 'due_date', width: 100 },
]

// Small badge + "↗" per raised case/ticket — opens straight to that case in the AWS
// Support console (or Freshservice) in a new tab. stopPropagation so tapping it
// does not also open the ticket's detail page. Shared by the desktop table and
// the phone card list.
function CaseBadges({ j }) {
  return (
    <>
          {j.aws_support_case_id && (
            <a href={`https://support.console.aws.amazon.com/support/home#/case/?caseId=${j.aws_support_case_id}`}
              target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}
              title={`Open AWS Support case ${j.aws_support_case_display_id || j.aws_support_case_id} ↗`}
              style={{ marginLeft: 6, fontSize: 10, background: COLORS.brand, color: '#fff', borderRadius: 3, padding: '1px 5px', whiteSpace: 'nowrap', textDecoration: 'none', display: 'inline-flex', alignItems: 'center', gap: 2 }}>
              AWS ↗
            </a>
          )}
          {j.aws_ams_sr_case_id && (
            <a href={`https://support.console.aws.amazon.com/support/home#/case/?caseId=${j.aws_ams_sr_case_id}`}
              target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}
              title={`Open AMS Service Request ${j.aws_ams_sr_case_display_id || j.aws_ams_sr_case_id} ↗`}
              style={{ marginLeft: 4, fontSize: 10, background: '#9333ea', color: '#fff', borderRadius: 3, padding: '1px 5px', whiteSpace: 'nowrap', textDecoration: 'none', display: 'inline-flex', alignItems: 'center', gap: 2 }}>
              AMS SR ↗
            </a>
          )}
          {j.aws_ams_incident_case_id && (
            <a href={`https://support.console.aws.amazon.com/support/home#/case/?caseId=${j.aws_ams_incident_case_id}`}
              target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}
              title={`Open AMS Incident ${j.aws_ams_incident_case_display_id || j.aws_ams_incident_case_id} ↗`}
              style={{ marginLeft: 4, fontSize: 10, background: '#dc2626', color: '#fff', borderRadius: 3, padding: '1px 5px', whiteSpace: 'nowrap', textDecoration: 'none', display: 'inline-flex', alignItems: 'center', gap: 2 }}>
              AMS INC ↗
            </a>
          )}
          {j.freshservice_ticket_id && (
            <a href={j.freshservice_ticket_url} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}
              title={`Open Freshservice ticket ${j.freshservice_ticket_id} ↗`}
              style={{ marginLeft: 4, fontSize: 10, background: COLORS.success, color: '#fff', borderRadius: 3, padding: '1px 5px', whiteSpace: 'nowrap', textDecoration: 'none', display: 'inline-flex', alignItems: 'center', gap: 2 }}>
              FS ↗
            </a>
          )}
    </>
  )
}

export default function JobList() {
  const { user } = useAuth()
  // Per-user tab WRITE grant for "jobs" (independent of visibility of
  // the "jobs_new" tab itself — see database.py's PortalUser.tab_write
  // docstring). Full admins always have every tab_id here.
  const canWriteJobs = (user?.tab_write || []).includes('jobs')
  const navigate = useNavigate()
  const isMobile = useIsMobile()
  const [showFilters, setShowFilters] = useState(false)
  const [data, setData] = useState({ jobs: [], total: 0, page: 1 })
  const [loading, setLoading] = useState(true)
  // Separate from `loading` — drives the "🔄 Refresh" button's own label
  // for an explicit click, so a background auto-refresh (below) never
  // shows the disruptive full-table "Loading…" state once data is
  // already on screen (same pattern as the AWS Health Dashboard page).
  const [refreshing, setRefreshing] = useState(false)
  const [options, setOptions] = useState({ categories: [], priorities: [], statuses: [], sort_fields: [], team_members: [] })
  const [filters, setFilters] = useState({ status: [], priority: [], category: [], assignee: [] })
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  // Sort By filter — field + direction ("asc" = increase, "desc" = decrease).
  // Defaults match the original always-newest-first behavior.
  const [sortBy, setSortBy] = useState('created_at')
  const [sortOrder, setSortOrder] = useState('desc')

  useEffect(() => {
    api.getOptions().then(setOptions).catch(() => {})
  }, [])

  // silent=true (used by the background 15-min auto-refresh) skips both
  // loading indicators entirely — the table just quietly updates once
  // the new data arrives, with no spinner/disabled-button flicker for a
  // refresh the user didn't ask for.
  const load = useCallback((manualRefresh = false, silent = false) => {
    if (silent) { /* no loading indicator */ }
    else if (manualRefresh) setRefreshing(true)
    else setLoading(true)
    const params = { page, per_page: 50, q: q || undefined, sort_by: sortBy, sort_order: sortOrder }
    if (filters.status.length) params.status = filters.status.join(',')
    if (filters.priority.length) params.priority = filters.priority.join(',')
    if (filters.category.length) params.category = filters.category.join(',')
    if (filters.assignee.length) params.assignee = filters.assignee.join(',')
    return api.listJobs(params)
      .then(d => setData(d))
      .catch(() => {})
      .finally(() => { setLoading(false); setRefreshing(false) })
  }, [filters, q, page, sortBy, sortOrder])

  useEffect(() => { load() }, [load])

  // Auto-refresh every 15 minutes so newly-raised AWS/AMS cases (and any
  // other ticket changes made outside this browser tab) show up without
  // the user having to manually reload — mirrors the AWS Health
  // Dashboard page's own periodic-refresh pattern. Rides the current
  // filters/page (via `load`'s own dependency list) rather than always
  // resetting to page 1, so a user mid-review of page 3 doesn't get
  // silently bounced back to page 1 every 15 minutes. silent=true so
  // this background tick never shows a loading spinner.
  useEffect(() => {
    const id = setInterval(() => { load(false, true) }, 15 * 60 * 1000)
    return () => clearInterval(id)
  }, [load])

  const toggleFilter = (key, val) => {
    setFilters(prev => {
      const arr = prev[key]
      return { ...prev, [key]: arr.includes(val) ? arr.filter(x => x !== val) : [...arr, val] }
    })
    setPage(1)
  }

  const activeFilterCount = Object.values(filters).reduce((n, arr) => n + arr.length, 0)

  const FILTER_SECTIONS = [
    { key: 'status', label: 'Status', values: options.statuses, colorFn: v => STATUS_COLOR[v] || '#6b7280' },
    { key: 'priority', label: 'Priority', values: options.priorities, colorFn: v => PRIORITY_COLOR[v] || '#6b7280' },
    { key: 'category', label: 'Category', values: options.categories, colorFn: () => COLORS.brand },
    { key: 'assignee', label: 'Assignee', values: options.team_members?.map(t => t.email) || [], labelFn: v => options.team_members?.find(t => t.email === v)?.name || v, colorFn: () => COLORS.info },
  ]

  const totalPages = Math.ceil(data.total / 50)

  // Which month-group headers are expanded — collapsed by default so the
  // list opens as a compact month-by-month summary; clicking a month row
  // toggles its jobs in/out of view. Jobs arrive sorted by created_at
  // desc, so same-month jobs are already contiguous in the API response —
  // group them (preserving order) rather than re-sorting anything.
  const [expandedMonths, setExpandedMonths] = useState(new Set())
  const toggleMonth = (key) => {
    setExpandedMonths(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }

  // Month-group headers only make sense when jobs arrive sorted by
  // created_at (same-month jobs are contiguous, letting us group
  // without re-sorting). Any other "Sort By" field/order shuffles the
  // list, so fall back to one flat "group" with no collapsible header —
  // isFlatSort short-circuits the row-rendering below to always show
  // every job regardless of expandedMonths.
  const isFlatSort = sortBy !== 'created_at'
  // "Closed" tickets are pulled out of their normal month bucket and
  // collected into one dedicated collapsible box at the very bottom,
  // instead of being scattered across whichever month they happened to
  // be created/closed in — closed work is done, and a manager reviewing
  // by month rarely cares which specific month a resolved ticket landed
  // in, so this keeps the active month groups focused on open work
  // while still keeping every closed ticket easy to find in one place.
  const closedJobs = data.jobs.filter(j => j.status === 'Closed')
  const openJobs = data.jobs.filter(j => j.status !== 'Closed')
  const monthGroups = (() => {
    const groups = isFlatSort
      ? (openJobs.length ? [{ key: '__flat__', label: null, jobs: openJobs, isClosedGroup: false }] : [])
      : (() => {
          const g = []
          for (const j of openJobs) {
            const key = monthKeyHK(j.created_at)
            const last = g[g.length - 1]
            if (last && last.key === key) {
              last.jobs.push(j)
            } else {
              g.push({ key, label: monthLabelHK(j.created_at), jobs: [j], isClosedGroup: false })
            }
          }
          return g
        })()
    if (closedJobs.length) {
      groups.push({ key: '__closed__', label: 'Closed', jobs: closedJobs, isClosedGroup: true })
    }
    return groups
  })()

  return (
    <div style={{ maxWidth: 1240, margin: '0 auto', padding: '24px 24px 40px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.brandDark, fontSize: 22, fontWeight: 800 }}>Tickets</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 2 }}>{data.total} total tickets</div>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <button onClick={() => load(true)} disabled={refreshing}
            style={{ background: '#fff', color: COLORS.textSecondary, border: `1px solid ${COLORS.border}`, borderRadius: 7, padding: '9px 16px', fontWeight: 600, fontSize: 14, cursor: refreshing ? 'default' : 'pointer' }}>
            {refreshing ? '⏳ Refreshing…' : '🔄 Refresh'}
          </button>
          {canWriteJobs && (
            <button onClick={() => navigate('/jobs/new')}
              style={{ background: COLORS.brand, color: '#fff', border: 'none', borderRadius: 7, padding: '9px 18px', fontWeight: 600, fontSize: 14, cursor: 'pointer' }}>
              + New Ticket
            </button>
          )}
        </div>
      </div>

      {/* Search */}
      <input
        placeholder="Search title or description…"
        value={q}
        onChange={e => { setQ(e.target.value); setPage(1) }}
        style={{ width: '100%', padding: '9px 14px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 14, marginBottom: 14, boxSizing: 'border-box' }}
      />

      {/* Filters — on a phone they sit behind a toggle so the tickets are not
          pushed below five screens of chips. */}
      {isMobile && (
        <button type="button" onClick={() => setShowFilters(v => !v)} aria-expanded={showFilters}
          style={{
            width: '100%', display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            padding: '10px 14px', marginBottom: 12, borderRadius: 7, cursor: 'pointer',
            border: `1px solid ${COLORS.border}`, background: '#fff', fontSize: 14, fontWeight: 600, color: COLORS.textPrimary,
          }}>
          <span>🔎 Filters{activeFilterCount ? ` (${activeFilterCount} active)` : ''}</span>
          <span aria-hidden="true">{showFilters ? '▲' : '▼'}</span>
        </button>
      )}
      <div style={{ display: (isMobile && !showFilters) ? 'none' : 'flex', gap: 20, flexWrap: 'wrap', marginBottom: 16 }}>
        {FILTER_SECTIONS.map(({ key, label, values, colorFn, labelFn }) => (
          <div key={key}>
            <div style={{ fontSize: 11, fontWeight: 700, color: '#6b7280', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>{label}</div>
            <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
              {values.map(v => {
                const isActive = filters[key].includes(v)
                const color = colorFn(v)
                const lbl = labelFn ? labelFn(v) : v
                return (
                  <button key={v} onClick={() => toggleFilter(key, v)}
                    style={{
                      padding: '3px 10px', borderRadius: 4, fontSize: 12, fontWeight: 500,
                      cursor: 'pointer', border: `1px solid ${color}`,
                      background: isActive ? color : 'transparent',
                      color: isActive ? '#fff' : color,
                    }}>
                    {lbl}
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>

      {/* Phones: one card per ticket (the 9-column table cannot fit). Same month /
          Closed grouping and the same open/collapse behaviour as the table. */}
      {isMobile && (
        <div>
          {loading ? (
            Array.from({ length: 4 }, (_, i) => (
              <div key={`sk-${i}`} style={{ ...CARD, padding: 14, marginBottom: 8 }}><Skeleton height={16} /></div>
            ))
          ) : data.jobs.length === 0 ? (
            <div style={{ ...CARD, padding: 28, textAlign: 'center', color: COLORS.textMuted, fontSize: 13 }}>No tickets found matching your filters</div>
          ) : monthGroups.map(group => {
            const isOpen = group.isClosedGroup ? expandedMonths.has(group.key) : (isFlatSort || expandedMonths.has(group.key))
            const showHeader = !isFlatSort || group.isClosedGroup
            return (
              <div key={group.key} style={{ marginBottom: 6 }}>
                {showHeader && (
                  <button type="button" onClick={() => toggleMonth(group.key)} aria-expanded={isOpen}
                    style={{
                      width: '100%', textAlign: 'left', padding: '10px 12px', marginBottom: 8, borderRadius: 8, border: 'none', cursor: 'pointer',
                      background: group.isClosedGroup ? '#f1f5f9' : '#eef2f9',
                      color: group.isClosedGroup ? COLORS.textSecondary : COLORS.brand, fontWeight: 700, fontSize: 14,
                    }}>
                    <span style={{ display: 'inline-block', transition: 'transform 0.15s', transform: isOpen ? 'rotate(90deg)' : 'rotate(0deg)', marginRight: 8 }}>▶</span>
                    {group.isClosedGroup ? '✅ Closed' : `📅 ${group.label}`}
                    <span style={{ marginLeft: 8, fontWeight: 500, color: COLORS.textSecondary, fontSize: 12 }}>
                      ({group.jobs.length} ticket{group.jobs.length > 1 ? 's' : ''}{group.isClosedGroup ? '' : ' created'})
                    </span>
                  </button>
                )}
                {isOpen && group.jobs.map(j => {
                  const dueSoon = isDueSoon(j.due_date)
                  const barColor = PRIORITY_COLOR[j.priority] || COLORS.textMuted
                  const open = () => navigate(`/jobs/${j.id}`)
                  return (
                    <div key={j.id} role="link" tabIndex={0} onClick={open}
                      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open() } }}
                      style={{
                        background: dueSoon ? '#fffbeb' : '#fff', borderRadius: 10, borderLeft: `4px solid ${barColor}`,
                        padding: '12px 14px', marginBottom: 8, boxShadow: '0 1px 4px rgba(15,23,42,0.08)', cursor: 'pointer',
                      }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                        <span style={{ fontFamily: 'monospace', fontWeight: 700, fontSize: 12, color: COLORS.brand }}>{formatTicketNumber(j)}</span>
                        <Badge text={j.status} color={STATUS_COLOR[j.status] || '#6b7280'} />
                      </div>
                      <div style={{ fontSize: 14, fontWeight: 600, color: COLORS.textPrimary, lineHeight: 1.4, overflowWrap: 'anywhere' }}>
                        {j.title}
                        <CaseBadges j={j} />
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginTop: 8, fontSize: 12.5, color: '#475569' }}>
                        <Badge text={j.priority} color={PRIORITY_COLOR[j.priority] || '#6b7280'} />
                        <span>{j.category}{j.category === 'Other' && j.category_other ? ` (${j.category_other})` : ''}</span>
                      </div>
                      <div style={{ marginTop: 8, fontSize: 12, color: '#64748b', lineHeight: 1.6 }}>
                        <div>👤 {j.assignee_name}{j.created_by_name ? <span> · created by {j.created_by_name}</span> : null}</div>
                        <div>
                          📅 {formatDateHK(j.created_at)}
                          {dueSoon
                            ? <span style={{ color: COLORS.warning, fontWeight: 600 }}> · ⏰ due {j.due_date}</span>
                            : (j.due_date ? <span> · due {j.due_date}</span> : null)}
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            )
          })}
        </div>
      )}

      {/* Table (desktop / tablet) */}
      {!isMobile && (
      <div style={{ ...CARD, overflow: 'hidden' }}>
        {/* table-layout: fixed + explicit <colgroup> widths — with the
            default auto layout, a td's maxWidth doesn't actually
            constrain the column (browsers just grow the column to fit
            content), so a long title like "AWS Health: SAGEMAKER —
            AWS_SAGEMAKER_NOTEBOOKS_SCHEDULED_MAINTENANCE" would overflow
            its cell and visually bleed into the Category column next to
            it instead of wrapping. Fixed layout forces every column to
            respect its declared width, and the Title cell below wraps
            long text onto multiple lines within that width instead of
            overflowing. */}
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13, tableLayout: 'fixed' }}>
          <colgroup>
            {COLUMNS.map(c => <col key={c.label} style={{ width: c.width }} />)}
          </colgroup>
          <thead>
            <tr style={{ background: '#f8fafc', borderBottom: `2px solid ${COLORS.border}` }}>
              {COLUMNS.map(({ label, field }) => {
                const isSortable = !!field
                const isActive = isSortable && sortBy === field
                const handleClick = () => {
                  if (!isSortable) return
                  if (isActive) {
                    setSortOrder(o => o === 'asc' ? 'desc' : 'asc')
                  } else {
                    setSortBy(field)
                    setSortOrder('desc')
                  }
                  setPage(1)
                }
                return (
                  <th key={label} onClick={handleClick}
                    title={isSortable ? (isActive ? (sortOrder === 'asc' ? 'Sorted increasing — click for decreasing' : 'Sorted decreasing — click for increasing') : `Click to sort by ${label}`) : undefined}
                    style={{
                      padding: '10px 14px', textAlign: 'left', fontWeight: 600, fontSize: 12,
                      textTransform: 'uppercase', letterSpacing: 0.3, overflow: 'hidden',
                      textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      color: isActive ? COLORS.brand : '#475569',
                      cursor: isSortable ? 'pointer' : 'default',
                      userSelect: 'none',
                    }}>
                    {label}
                    {isActive && <span style={{ marginLeft: 4 }}>{sortOrder === 'asc' ? '↑' : '↓'}</span>}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              // A few skeleton rows shaped like real ticket rows, instead
              // of one centered "Loading…" line — keeps the table's own
              // width/columns visible so the layout doesn't jump once
              // real rows arrive.
              Array.from({ length: 6 }, (_, i) => (
                <tr key={`sk-${i}`}>
                  <td colSpan={9} style={{ padding: '10px 16px' }}>
                    <Skeleton height={16} />
                  </td>
                </tr>
              ))
            ) : data.jobs.length === 0 ? (
              <tr><td colSpan={9} style={{ padding: 40, textAlign: 'center', color: COLORS.textMuted }}>No tickets found matching your filters</td></tr>
            ) : monthGroups.map(group => {
              // The Closed box collapses by default even under a flat
              // (non-created_at) sort, unlike the regular month groups
              // which only get a header under created_at sort — closed
              // tickets always get their own collapsible header
              // regardless of sort field, since they're a distinct
              // "done" bucket rather than a month grouping.
              const isOpen = group.isClosedGroup ? expandedMonths.has(group.key) : (isFlatSort || expandedMonths.has(group.key))
              const showHeader = !isFlatSort || group.isClosedGroup
              return (
                <React.Fragment key={group.key}>
                  {showHeader && (
                    <tr onClick={() => toggleMonth(group.key)}
                      style={{
                        cursor: 'pointer',
                        background: group.isClosedGroup ? '#f1f5f9' : '#eef2f9',
                        borderBottom: `1px solid ${group.isClosedGroup ? '#e2e8f0' : '#dbe3f0'}`,
                      }}
                      onMouseEnter={e => e.currentTarget.style.background = group.isClosedGroup ? '#e6ebf1' : '#e2e9f7'}
                      onMouseLeave={e => e.currentTarget.style.background = group.isClosedGroup ? '#f1f5f9' : '#eef2f9'}>
                      <td colSpan={9} style={{ padding: '9px 14px', color: group.isClosedGroup ? COLORS.textSecondary : COLORS.brand, fontWeight: 700, fontSize: 13 }}>
                        <span style={{ display: 'inline-block', transition: 'transform 0.15s', transform: isOpen ? 'rotate(90deg)' : 'rotate(0deg)', marginRight: 8 }}>▶</span>
                        {group.isClosedGroup ? '✅ Closed' : `📅 ${group.label}`}
                        <span style={{ marginLeft: 8, fontWeight: 500, color: COLORS.textSecondary, fontSize: 12 }}>
                          ({group.jobs.length} ticket{group.jobs.length > 1 ? 's' : ''}{group.isClosedGroup ? '' : ' created'})
                        </span>
                      </td>
                    </tr>
                  )}
                  {isOpen && group.jobs.map(j => {
                    const dueSoon = isDueSoon(j.due_date)
                    const priorityBarColor = PRIORITY_COLOR[j.priority] || COLORS.textMuted
                    return (
                      <tr key={j.id} onClick={() => navigate(`/jobs/${j.id}`)}
                        style={{ cursor: 'pointer', borderBottom: '1px solid #f1f5f9', background: dueSoon ? '#fffbeb' : 'transparent', borderLeft: `3px solid ${priorityBarColor}` }}
                        onMouseEnter={e => e.currentTarget.style.background = dueSoon ? '#fef3c7' : '#f8fafc'}
                        onMouseLeave={e => e.currentTarget.style.background = dueSoon ? '#fffbeb' : 'transparent'}>
                        <td style={{ padding: '10px 14px 10px 12px', fontFamily: 'monospace', fontWeight: 700, color: COLORS.brand, fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {formatTicketNumber(j)}
                        </td>
                        <td style={{ padding: '10px 14px', fontWeight: 400, color: COLORS.textPrimary, overflowWrap: 'break-word', wordBreak: 'break-word', fontFamily: 'monospace', fontSize: 12.5, letterSpacing: -0.3 }}>
                          {j.title}
                          {/* Small badge + "↗" redirect button per raised
                              case/ticket — opens straight to that case in
                              the AWS Support console (or Freshservice) in
                              a new tab, without needing to open the Job
                              detail page first. stopPropagation so
                              clicking it doesn't also trigger the row's
                              own onClick (navigate to Job detail). */}
                          <CaseBadges j={j} />
                        </td>
                        <td style={{ padding: '10px 14px', color: '#475569', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{j.category}{j.category === 'Other' && j.category_other ? ` (${j.category_other})` : ''}</td>
                        <td style={{ padding: '10px 14px' }}><Badge text={j.priority} color={PRIORITY_COLOR[j.priority] || '#6b7280'} /></td>
                        <td style={{ padding: '10px 14px', color: '#475569', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {j.assignee_name}
                        </td>
                        <td style={{ padding: '10px 14px' }}><Badge text={j.status} color={STATUS_COLOR[j.status] || '#6b7280'} /></td>
                        <td style={{ padding: '10px 14px', color: '#475569', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {j.created_by_name}
                        </td>
                        <td style={{ padding: '10px 14px', color: '#64748b', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{formatDateHK(j.created_at)}</td>
                        <td style={{ padding: '10px 14px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {dueSoon ? <span style={{ color: COLORS.warning, fontWeight: 600 }}>⏰ {j.due_date}</span> : (j.due_date || '—')}
                        </td>
                      </tr>
                    )
                  })}
                </React.Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div style={{ display: 'flex', gap: 8, justifyContent: 'center', marginTop: 16 }}>
          <button disabled={page === 1} onClick={() => setPage(p => p - 1)}
            style={{ padding: '6px 14px', borderRadius: 5, border: '1px solid #d1d5db', background: '#fff', cursor: page === 1 ? 'default' : 'pointer' }}>
            ← Prev
          </button>
          <span style={{ padding: '6px 14px', fontSize: 13, color: '#475569' }}>Page {page} / {totalPages}</span>
          <button disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}
            style={{ padding: '6px 14px', borderRadius: 5, border: '1px solid #d1d5db', background: '#fff', cursor: page >= totalPages ? 'default' : 'pointer' }}>
            Next →
          </button>
        </div>
      )}
    </div>
  )
}
