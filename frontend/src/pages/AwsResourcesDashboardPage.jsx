import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../api.js'
import { COLORS, CARD, TAB_BAR, tabButtonStyle } from '../utils/theme.js'
import { getPageCache, setPageCache } from '../utils/pageCache.js'

// ── AWS Resources ──────────────────────────────────────────────────────────
// Native in-portal inventory view, replacing the previous static
// "external tool" landing page that only linked out to an IP/URL-based
// dashboard. Data now comes directly from the resource-inventory-scanner
// S3 bucket (s3://itinfra-compliance-reports/resource-inventory-scanner/,
// account 100000000001 / Core account) via backend/app/aws_resources_inventory.py
// — same account/credentials pattern used by Cost Analysis / Billing by
// Service / Security Hub, except this particular bucket lives directly in
// Core account's own account, so no cross-account AssumeRole fan-out is
// involved. Read-only for everyone.

const SUMMARY_CACHE_KEY = 'aws-resources-summary'
const DATA_CACHE_PREFIX = 'aws-resources-data'

function summaryCacheKey(accountIds) {
  return `${SUMMARY_CACHE_KEY}:${JSON.stringify([...accountIds].sort())}`
}
function dataCacheKey(resourceType, accountIds) {
  return `${DATA_CACHE_PREFIX}:${resourceType}:${JSON.stringify([...accountIds].sort())}`
}

// Per-column text filters are offered for these specific headers only
// (as requested) — a small input per matching column, shown whenever
// the active resource type's headers include that column name. "Tags"
// is deliberately excluded here: it gets its own click-to-expand
// treatment (see TagsCell) rather than a filter box.
const FILTERABLE_COLUMNS = [
  'Account ID', 'Region', 'Instance ID', 'Instance Type', 'State',
  'VPC ID', 'Private IP', 'EBS Info', 'Name',
]

function formatAge(seconds) {
  if (!seconds || seconds < 60) return 'just now'
  const mins = Math.floor(seconds / 60)
  if (mins < 60) return `${mins} min${mins === 1 ? '' : 's'} ago`
  const hours = Math.floor(mins / 60)
  return `${hours} hour${hours === 1 ? '' : 's'} ago`
}

// A cell value may contain an HTML snippet written by the scanner (e.g.
// TGW route tables / ECS service details / S3 lifecycle rules are
// rendered as small <table> fragments for readability) — those are the
// only fields ever passed through dangerouslySetInnerHTML, and they come
// from our own trusted S3 bucket (written by our own scanner Lambda),
// never from third-party/user input.
function looksLikeHtmlFragment(value) {
  return typeof value === 'string' && value.trim().startsWith('<table')
}

function Cell({ value }) {
  if (value === null || value === undefined || value === '') {
    return <span style={{ color: COLORS.textMuted }}>—</span>
  }
  if (looksLikeHtmlFragment(value)) {
    return <div style={{ fontSize: 12 }} dangerouslySetInnerHTML={{ __html: value }} />
  }
  const text = String(value)
  return <span style={{ whiteSpace: 'pre-wrap' }}>{text}</span>
}

// The "Account ID" column only ever carries the raw numeric account ID
// (as written by the scanner). Same friendly-name lookup used by the
// Cost Analysis / Billing by Service / Security Hub tabs
// (billing_by_service.ACCOUNT_ALIAS, surfaced here via
// getAwsResourcesAccounts()'s "alias" field) is shown alongside it so
// users don't have to memorize which account ID is which environment.
function AccountIdCell({ accountId, accountNameById }) {
  if (accountId === null || accountId === undefined || accountId === '') {
    return <span style={{ color: COLORS.textMuted }}>—</span>
  }
  const id = String(accountId)
  const name = accountNameById[id]
  if (!name) return <span>{id}</span>
  return (
    <span>
      <span style={{ fontWeight: 600 }}>{name}</span>
      <span style={{ color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11 }}> ({id})</span>
    </span>
  )
}

// Tags are collapsed by default (per resource, per row) and only render
// their full content once the user clicks to expand — a "Tags" column
// can otherwise carry long key=value strings that would blow up row
// height for every single row in a large table.
function TagsCell({ value }) {
  const [expanded, setExpanded] = useState(false)
  if (value === null || value === undefined || value === '') {
    return <span style={{ color: COLORS.textMuted }}>—</span>
  }
  if (!expanded) {
    return (
      <button
        onClick={() => setExpanded(true)}
        style={{ background: 'none', border: 'none', padding: 0, color: COLORS.brand, fontSize: 12, fontWeight: 600, cursor: 'pointer' }}
      >
        ▸ Show tags
      </button>
    )
  }
  const text = String(value)
  if (looksLikeHtmlFragment(text)) {
    return (
      <div>
        <div style={{ fontSize: 12 }} dangerouslySetInnerHTML={{ __html: text }} />
        <button onClick={() => setExpanded(false)}
          style={{ background: 'none', border: 'none', padding: 0, marginTop: 4, color: COLORS.textMuted, fontSize: 11, cursor: 'pointer' }}>
          ▲ Hide tags
        </button>
      </div>
    )
  }
  return (
    <div>
      <span style={{ whiteSpace: 'pre-wrap' }}>{text}</span>
      <button onClick={() => setExpanded(false)}
        style={{ display: 'block', background: 'none', border: 'none', padding: 0, marginTop: 4, color: COLORS.textMuted, fontSize: 11, cursor: 'pointer' }}>
        ▲ Hide tags
      </button>
    </div>
  )
}

export default function AwsResourcesDashboardPage() {
  const [allAccounts, setAllAccounts] = useState([])
  const [selectedAccountIds, setSelectedAccountIds] = useState([]) // empty = all
  const [accountPickerOpen, setAccountPickerOpen] = useState(false)

  const initialSummaryCached = getPageCache(summaryCacheKey([]))
  const [summary, setSummary] = useState(initialSummaryCached || null)
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [summaryErr, setSummaryErr] = useState('')

  const [activeType, setActiveType] = useState('ec2')
  const [data, setData] = useState(null)
  const [dataLoading, setDataLoading] = useState(false)
  const [dataRefreshing, setDataRefreshing] = useState(false)
  const [dataErr, setDataErr] = useState('')
  const [search, setSearch] = useState('')

  // Sort controls — "Sort by" picks a column (defaults to "Account ID",
  // which every resource type's row carries as its first column — see
  // backend/app/aws_resources_inventory.py's _HEADERS, every entry
  // starts with "Account ID"), "direction" flips ascending/descending.
  // Reset to the Account ID default whenever the active resource-type
  // tab changes, since a column index/name from one tab's headers may
  // not exist (or may mean something different) on another tab.
  const [sortColumn, setSortColumn] = useState('Account ID')
  const [sortDirection, setSortDirection] = useState('asc')

  // Per-column filters — one free-text filter box per column named in
  // FILTERABLE_COLUMNS (only shown for the columns that actually exist
  // on the active resource type's headers). Keyed by header name so it
  // survives sort-column changes; cleared whenever the active tab
  // changes since a filter value from one tab's column has no meaning
  // on another tab.
  const [columnFilters, setColumnFilters] = useState({})

  const mountedRef = React.useRef(true)
  const summaryAbortRef = React.useRef(null)
  const dataAbortRef = React.useRef(null)
  const summarySeqRef = React.useRef(0)
  const dataSeqRef = React.useRef(0)

  useEffect(() => {
    mountedRef.current = true
    api.getAwsResourcesAccounts()
      .then(d => { if (mountedRef.current) setAllAccounts(d.accounts || []) })
      .catch(() => {})
    return () => {
      mountedRef.current = false
      summaryAbortRef.current?.abort()
      dataAbortRef.current?.abort()
    }
  }, [])

  // silent=true (used by the background 12-hour auto-refresh) skips the
  // loading indicator entirely — the KPI/availability data just quietly
  // updates once the new data arrives, with no spinner flicker for a
  // refresh the user didn't ask for.
  const loadSummary = useCallback((refresh = false, silent = false) => {
    summaryAbortRef.current?.abort()
    const controller = new AbortController()
    summaryAbortRef.current = controller
    const mySeq = ++summarySeqRef.current
    const key = summaryCacheKey(selectedAccountIds)

    const cached = getPageCache(key)
    if (cached && !refresh) setSummary(cached)
    if (!silent) setSummaryLoading(true)
    setSummaryErr('')
    api.getAwsResourcesSummary(selectedAccountIds, refresh, controller.signal)
      .then(d => {
        if (!mountedRef.current || mySeq !== summarySeqRef.current) return
        setSummary(d)
        setPageCache(key, d)
      })
      .catch(e => {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
        if (!mountedRef.current || mySeq !== summarySeqRef.current) return
        if (!silent) setSummaryErr(e?.response?.data?.detail || 'Could not load AWS Resources inventory summary')
      })
      .finally(() => {
        if (!mountedRef.current || mySeq !== summarySeqRef.current) return
        setSummaryLoading(false)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAccountIds])

  useEffect(() => { loadSummary(false) }, [loadSummary])

  // silent=true (used by the background 12-hour auto-refresh) skips both
  // loading indicators — the table just quietly updates in place once
  // the new data arrives, with no spinner/disabled-button flicker for a
  // refresh the user didn't ask for.
  const loadData = useCallback((resourceType, refresh = false, silent = false) => {
    dataAbortRef.current?.abort()
    const controller = new AbortController()
    dataAbortRef.current = controller
    const mySeq = ++dataSeqRef.current
    const key = dataCacheKey(resourceType, selectedAccountIds)

    const cached = getPageCache(key)
    if (cached && !refresh) {
      setData(cached)
    } else if (!silent) {
      if (refresh) setDataRefreshing(true)
      else setDataLoading(true)
    }
    if (!silent) setDataErr('')
    api.getAwsResourcesData(resourceType, selectedAccountIds, refresh, controller.signal)
      .then(d => {
        if (!mountedRef.current || mySeq !== dataSeqRef.current) return
        setData(d)
        setPageCache(key, d)
      })
      .catch(e => {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
        if (!mountedRef.current || mySeq !== dataSeqRef.current) return
        if (!silent) setDataErr(e?.response?.data?.detail || 'Could not load this resource type')
      })
      .finally(() => {
        if (!mountedRef.current || mySeq !== dataSeqRef.current) return
        setDataLoading(false)
        setDataRefreshing(false)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAccountIds])

  useEffect(() => {
    setSearch('')
    setColumnFilters({})
    loadData(activeType, false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeType, loadData])

  // Auto-refresh every 12 hours so the page always eventually reflects
  // whatever the resource-inventory-scanner most recently wrote to S3,
  // even if the user never clicks "🔄 Refresh" themselves — mirrors the
  // All Tickets page's own periodic-refresh pattern. Passes refresh=true
  // (not just re-reading the frontend's own pageCache) so this tick
  // actually bypasses the backend's 12-hour cache too and re-reads S3
  // directly, rather than potentially serving a summary/table that was
  // itself already served from a stale backend cache entry.
  useEffect(() => {
    const id = setInterval(() => {
      loadSummary(true, true)
      loadData(activeType, true, true)
    }, 12 * 60 * 60 * 1000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadSummary, loadData, activeType])

  function toggleAccount(id) {
    setSelectedAccountIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }
  function selectAllAccounts() { setSelectedAccountIds([]) }

  const accountPickerLabel = useMemo(() => {
    if (selectedAccountIds.length === 0) return `All accounts (${allAccounts.length})`
    return `${selectedAccountIds.length} account${selectedAccountIds.length === 1 ? '' : 's'} selected`
  }, [selectedAccountIds, allAccounts])

  // account_id -> friendly alias (e.g. "100000000001" -> "core"), same
  // mapping the account picker checkboxes already use — reused here so
  // the "Account ID" table column can show the name next to the number,
  // matching how Cost Analysis presents accounts.
  const accountNameById = useMemo(() => {
    const map = {}
    for (const a of allAccounts) map[a.account_id] = a.alias
    return map
  }, [allAccounts])

  const resourceTypes = summary?.resource_types || []

  const headers = data?.headers || []
  const allRows = data?.rows || []

  // Every inventory type includes Account ID. If a user changes tabs after
  // sorting by a type-specific field, fall back to Account ID rather than
  // leaving the sort selector on a column that no longer exists.
  useEffect(() => {
    if (headers.length > 0 && !headers.includes(sortColumn)) setSortColumn('Account ID')
  }, [headers, sortColumn])

  // Which of the always-eligible FILTERABLE_COLUMNS actually exist as a
  // header on the current resource type — e.g. "Instance ID" only makes
  // sense on the ec2 tab, so it's simply omitted from the filter row on
  // every other tab rather than showing a box that can never match.
  const activeFilterColumns = useMemo(
    () => FILTERABLE_COLUMNS.filter(col => headers.includes(col)),
    [headers]
  )

  // Distinct values available for each filterable column, computed from
  // ALL rows (not the already-filtered set) so every dropdown always
  // offers its full set of choices regardless of what's selected in the
  // other filter dropdowns — picking a value never makes another
  // column's options shrink/disappear. For "Account ID" the raw ids are
  // sorted, but displayed as "Name (id)" via accountNameById.
  const filterOptionsByColumn = useMemo(() => {
    const result = {}
    for (const col of activeFilterColumns) {
      const idx = headers.indexOf(col)
      if (idx < 0) { result[col] = []; continue }
      const seen = new Set()
      for (const row of allRows) {
        const v = row[idx]
        if (v !== null && v !== undefined && v !== '') seen.add(String(v))
      }
      const values = Array.from(seen)
      if (col === 'Account ID') {
        values.sort((a, b) => (accountNameById[a] || a).localeCompare(accountNameById[b] || b))
      } else {
        values.sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }))
      }
      result[col] = values
    }
    return result
  }, [activeFilterColumns, headers, allRows, accountNameById])

  function setColumnFilter(column, value) {
    setColumnFilters(prev => ({ ...prev, [column]: value }))
  }

  const visibleRows = useMemo(() => {
    const q = search.trim().toLowerCase()
    let searched = q
      ? allRows.filter(row => row.some(cell => String(cell ?? '').toLowerCase().includes(q)))
      : allRows

    for (const col of activeFilterColumns) {
      const selected = columnFilters[col] || ''
      if (!selected) continue
      const idx = headers.indexOf(col)
      if (idx < 0) continue
      // Exact match against the selected dropdown value (not a
      // substring search — the user picks one of the offered choices).
      searched = searched.filter(row => String(row[idx] ?? '') === selected)
    }

    const columnIndex = headers.indexOf(sortColumn)
    if (columnIndex < 0) return searched

    return searched
      .map((row, originalIndex) => ({ row, originalIndex }))
      .sort((a, b) => {
        const left = a.row[columnIndex]
        const right = b.row[columnIndex]
        const leftEmpty = left === null || left === undefined || left === ''
        const rightEmpty = right === null || right === undefined || right === ''
        if (leftEmpty || rightEmpty) {
          if (leftEmpty && rightEmpty) return a.originalIndex - b.originalIndex
          return leftEmpty ? 1 : -1
        }

        let comparison
        const leftText = String(left).trim()
        const rightText = String(right).trim()
        const numericColumn = sortColumn !== 'Account ID'
          && leftText !== '' && rightText !== ''
          && Number.isFinite(Number(leftText)) && Number.isFinite(Number(rightText))
        if (numericColumn) {
          comparison = Number(leftText) - Number(rightText)
        } else {
          comparison = leftText.localeCompare(rightText, undefined, {
            numeric: sortColumn !== 'Account ID',
            sensitivity: 'base',
          })
        }
        if (comparison === 0) return a.originalIndex - b.originalIndex
        return sortDirection === 'desc' ? -comparison : comparison
      })
      .map(item => item.row)
  }, [allRows, headers, search, sortColumn, sortDirection, activeFilterColumns, columnFilters, accountNameById])

  return (
    <div style={{ padding: '24px 28px', maxWidth: 1500 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 18, flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>🏷️ AWS Resources</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            Multi-account resource inventory, sourced directly from the Resource Inventory Scanner's S3 reports. Auto-refreshes every 12 hours, or click 🔄 Refresh anytime for the latest data.
            {summary && !summary.is_mock && <> Summary last fetched {formatAge(summary.cache_age_seconds)}.</>}
            {summary?.is_mock && <> (local mock data — no real AWS calls in dev mode)</>}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <button onClick={() => loadData(activeType, true)} disabled={dataLoading || dataRefreshing}
            style={{ padding: '8px 16px', borderRadius: 7, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textPrimary, fontWeight: 700, fontSize: 13, cursor: (dataLoading || dataRefreshing) ? 'default' : 'pointer', opacity: (dataLoading || dataRefreshing) ? 0.6 : 1 }}>
            {dataRefreshing ? '⏳ Refreshing…' : '🔄 Refresh'}
          </button>
        </div>
      </div>

      {summaryErr && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {summaryErr}
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

      {summaryLoading && !summary && (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted, marginBottom: 16 }}>Loading…</div>
      )}

      {/* Resource-type tabs */}
      <div style={{ ...TAB_BAR, marginBottom: 16, overflowX: 'auto' }}>
        {resourceTypes.map(rt => (
          <button key={rt.id} onClick={() => setActiveType(rt.id)} style={tabButtonStyle(activeType === rt.id)}>
            {rt.icon} {rt.label}
          </button>
        ))}
      </div>

      {dataErr && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {dataErr}
        </div>
      )}

      {/* Search, sorting controls + row count */}
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 12, marginBottom: 10, flexWrap: 'wrap' }}>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4, color: COLORS.textMuted, fontSize: 11.5, fontWeight: 700 }}>
          Search
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search this table…"
            style={{ padding: '7px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13, minWidth: 240, color: COLORS.textPrimary, background: '#fff' }}
          />
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4, color: COLORS.textMuted, fontSize: 11.5, fontWeight: 700 }}>
          Sort by
          <select
            value={headers.includes(sortColumn) ? sortColumn : 'Account ID'}
            onChange={e => setSortColumn(e.target.value)}
            disabled={headers.length === 0}
            style={{ padding: '7px 10px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13, minWidth: 180, color: COLORS.textPrimary, background: '#fff' }}
          >
            {headers.map(header => <option key={header} value={header}>{header}</option>)}
          </select>
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4, color: COLORS.textMuted, fontSize: 11.5, fontWeight: 700 }}>
          Sorting method
          <select
            value={sortDirection}
            onChange={e => setSortDirection(e.target.value)}
            style={{ padding: '7px 10px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 13, minWidth: 150, color: COLORS.textPrimary, background: '#fff' }}
          >
            <option value="asc">Ascending (A–Z)</option>
            <option value="desc">Descending (Z–A)</option>
          </select>
        </label>
        <span style={{ fontSize: 12.5, color: COLORS.textMuted, paddingBottom: 8 }}>
          {visibleRows.length} of {allRows.length} row{allRows.length === 1 ? '' : 's'}
          {data && !data.is_mock && <> · fetched {formatAge(data.cache_age_seconds)}</>}
          {data?.is_mock && <> (local mock data)</>}
        </span>
      </div>

      {/* Per-column filters — only for columns present on this resource
          type, per FILTERABLE_COLUMNS. Tags intentionally has no filter
          box here; it uses its own click-to-expand cell instead. */}
      {activeFilterColumns.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
          {activeFilterColumns.map(col => (
            <label key={col} style={{ display: 'flex', flexDirection: 'column', gap: 4, color: COLORS.textMuted, fontSize: 11, fontWeight: 700 }}>
              {col}
              <select
                value={columnFilters[col] || ''}
                onChange={e => setColumnFilter(col, e.target.value)}
                style={{ padding: '6px 10px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 12.5, minWidth: 160, color: COLORS.textPrimary, background: '#fff' }}
              >
                <option value="">All {col}</option>
                {(filterOptionsByColumn[col] || []).map(v => (
                  <option key={v} value={v}>
                    {col === 'Account ID' ? `${accountNameById[v] || v} (${v})` : v}
                  </option>
                ))}
              </select>
            </label>
          ))}
          {Object.values(columnFilters).some(v => (v || '').trim()) && (
            <button
              onClick={() => setColumnFilters({})}
              style={{ padding: '6px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textMuted, fontSize: 12, fontWeight: 600, cursor: 'pointer', height: 30 }}
            >
              Clear filters
            </button>
          )}
        </div>
      )}

      {/* Data table */}
      <div style={{ ...CARD, overflow: 'auto', maxHeight: '65vh' }}>
        {dataLoading && !data ? (
          <div style={{ padding: 40, textAlign: 'center', color: COLORS.textMuted }}>Loading…</div>
        ) : headers.length === 0 ? (
          <div style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted }}>No column definition for this resource type.</div>
        ) : visibleRows.length === 0 ? (
          <div style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted }}>
            No {allRows.length === 0 ? 'resources found for this selection' : 'rows match your search'}.
          </div>
        ) : (
          <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
            <thead>
              <tr style={{ background: '#f8fafc', position: 'sticky', top: 0, zIndex: 1 }}>
                {headers.map(h => (
                  <th key={h} style={{ textAlign: 'left', padding: '9px 12px', borderBottom: `1px solid ${COLORS.border}`, color: COLORS.textMuted, fontWeight: 700, fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.3, whiteSpace: 'nowrap' }}>
                    {h}{sortColumn === h && <span aria-label={sortDirection === 'asc' ? 'sorted ascending' : 'sorted descending'}> {sortDirection === 'asc' ? '▲' : '▼'}</span>}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visibleRows.map((row, i) => (
                <tr key={i} style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                  {headers.map((h, ci) => (
                    <td key={h} style={{ padding: '8px 12px', color: COLORS.textPrimary, verticalAlign: 'top', maxWidth: 320 }}>
                      {h === 'Tags' ? <TagsCell value={row[ci]} />
                        : h === 'Account ID' ? <AccountIdCell accountId={row[ci]} accountNameById={accountNameById} />
                        : <Cell value={row[ci]} />}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
