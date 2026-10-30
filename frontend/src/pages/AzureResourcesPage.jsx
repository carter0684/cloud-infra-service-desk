import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../api.js'
import { COLORS, CARD, TAB_BAR, tabButtonStyle } from '../utils/theme.js'
import { getPageCache, setPageCache } from '../utils/pageCache.js'

// ── Azure Resources ──────────────────────────────────────────────────────
// New, independent sidebar category (per user's explicit choice — NOT
// folded into "AWS Resources") that inventories a single Azure
// subscription via Azure Resource Manager. See
// backend/app/azure_monitor.py's module docstring for the auth flow
// (Service Principal, client-credentials OAuth2, Reader role only —
// v1 is deliberately read-only, no start/stop/create/delete actions
// anywhere on this page).
//
// Deliberately simpler than AwsResourcesDashboardPage.jsx: there's only
// ONE Azure subscription configured (see azure_monitor.py's
// AZURE_SUBSCRIPTION_ID), so no multi-account picker is needed — the
// resource-type tab bar + generic headers/rows table is otherwise the
// same pattern.
//
// WAF and Cost sub-tabs were dropped from this v1 launch per user
// request, after live-testing during setup found this org has no WAF
// policies deployed at all, and Cost's Microsoft.CostManagement/query
// call kept returning a 400 even after granting the Service Principal
// both Cost Management Reader and Billing Reader roles (this org's
// billing account is a Microsoft Customer Agreement; the actual fix
// point — Cost Management + Billing > Settings > Properties > Policies
// > Manage policies — was not pursued further per the user's explicit
// "skip cost and WAF part" instruction). Only VM/AVD/Resource
// Group/NIC/NSG remain as sub-tabs. See main.py's
// _AZURE_RESOURCE_TYPE_FUNCS comment for the backend-side rationale;
// azure_monitor.py's list_waf_policies()/get_cost_summary() functions
// still exist (unused) in case this is revisited later.

const RESOURCE_TYPES_CACHE_KEY = 'azure-resources-types'
const DATA_CACHE_PREFIX = 'azure-resources-data'

function dataCacheKey(resourceType) {
  return `${DATA_CACHE_PREFIX}:${resourceType}`
}

function formatAge(seconds) {
  if (!seconds || seconds < 60) return 'just now'
  const mins = Math.floor(seconds / 60)
  if (mins < 60) return `${mins} min${mins === 1 ? '' : 's'} ago`
  const hours = Math.floor(mins / 60)
  return `${hours} hour${hours === 1 ? '' : 's'} ago`
}

function Cell({ value }) {
  if (value === null || value === undefined || value === '') {
    return <span style={{ color: COLORS.textMuted }}>—</span>
  }
  return <span style={{ whiteSpace: 'pre-wrap' }}>{String(value)}</span>
}

// NSG security rules — one row per rule, sourced from a row's
// row_details entry (see azure_monitor.py's list_nsgs(): {"type":
// "nsg_rules", "rules": [...]}). Rendered only when a user expands an
// NSG row (see the expand toggle in AzureInventoryTable below) — per
// the user's explicit requirement, this is collapsed by default and
// only ever shown on click, never rendered up-front for every NSG.
function NsgRuleDetailPanel({ detail }) {
  const rules = detail?.rules || []
  if (rules.length === 0) {
    return <div style={{ padding: '10px 14px', color: COLORS.textMuted, fontSize: 12.5 }}>No security rules.</div>
  }
  const cols = [
    { key: 'priority', label: 'Priority' },
    { key: 'name', label: 'Name' },
    { key: 'direction', label: 'Direction' },
    { key: 'access', label: 'Access' },
    { key: 'protocol', label: 'Protocol' },
    { key: 'source', label: 'Source' },
    { key: 'source_port', label: 'Source Port' },
    { key: 'destination', label: 'Destination' },
    { key: 'destination_port', label: 'Destination Port' },
  ]
  return (
    <div style={{ padding: '10px 14px', background: '#f8fafc' }}>
      <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr>
            {cols.map(c => (
              <th key={c.key} style={{ textAlign: 'left', padding: '5px 10px', color: COLORS.textMuted, fontWeight: 700, fontSize: 10.5, textTransform: 'uppercase', letterSpacing: 0.3, whiteSpace: 'nowrap' }}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rules.map((rule, ri) => (
            <tr key={ri} style={{ borderTop: `1px solid ${COLORS.border}` }}>
              {cols.map(c => {
                const isAllow = rule.access === 'Allow'
                const accessStyle = c.key === 'access'
                  ? { color: isAllow ? COLORS.success || '#16a34a' : COLORS.danger, fontWeight: 700 }
                  : {}
                return (
                  <td key={c.key} style={{ padding: '5px 10px', color: COLORS.textPrimary, verticalAlign: 'top', ...accessStyle }}>
                    <Cell value={rule[c.key]} />
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// A generic {headers, rows} inventory table — shared by VM / AVD /
// Resource Group / NIC / NSG / WAF, each of which returns a different
// column set from the SAME shape (see azure_monitor.py's list_*()
// functions), so one render path handles all six.
function AzureInventoryTable({ activeType }) {
  const [data, setData] = useState(getPageCache(dataCacheKey(activeType)) || null)
  const [loading, setLoading] = useState(!getPageCache(dataCacheKey(activeType)))
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  // Which rows (by original index in data.rows, NOT the filtered
  // visibleRows index) currently have their detail panel expanded.
  // Collapsed by default — nothing is ever added here up-front, only
  // on click — per the user's explicit "default it is collapse only
  // expand when user click it" requirement.
  const [expandedRows, setExpandedRows] = useState(() => new Set())

  const load = useCallback((refresh = false) => {
    const key = dataCacheKey(activeType)
    const cached = getPageCache(key)
    if (cached && !refresh) setData(cached)
    if (refresh) setRefreshing(true)
    else if (!cached) setLoading(true)
    setError('')
    api.getAzureResourcesData(activeType, refresh)
      .then(d => { setData(d); setPageCache(key, d) })
      .catch(e => setError(e?.response?.data?.detail || `Could not fetch Azure ${activeType} data`))
      .finally(() => { setLoading(false); setRefreshing(false) })
  }, [activeType])

  useEffect(() => { setSearch(''); setExpandedRows(new Set()); load(false) }, [activeType, load])

  const headers = data?.headers || []
  const allRows = data?.rows || []
  // Only NSG's response includes this key at all (see
  // azure_monitor.py's list_nsgs() vs list_vms()/etc.) — its absence
  // means "this resource type has no expandable detail", so the
  // expand column is only rendered when it's present.
  const rowDetails = data?.row_details || null
  const visibleRows = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return allRows.map((row, i) => ({ row, originalIndex: i }))
    return allRows
      .map((row, i) => ({ row, originalIndex: i }))
      .filter(({ row }) => row.some(cell => String(cell ?? '').toLowerCase().includes(q)))
  }, [allRows, search])

  function toggleRow(originalIndex) {
    setExpandedRows(prev => {
      const next = new Set(prev)
      if (next.has(originalIndex)) next.delete(originalIndex)
      else next.add(originalIndex)
      return next
    })
  }

  return (
    <div>
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
        <button onClick={() => load(true)} disabled={loading || refreshing}
          style={{ padding: '8px 16px', borderRadius: 7, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textPrimary, fontWeight: 700, fontSize: 13, cursor: (loading || refreshing) ? 'default' : 'pointer', opacity: (loading || refreshing) ? 0.6 : 1 }}>
          {refreshing ? '⏳ Refreshing…' : '🔄 Refresh'}
        </button>
        <span style={{ fontSize: 12.5, color: COLORS.textMuted, paddingBottom: 8 }}>
          {visibleRows.length} of {allRows.length} row{allRows.length === 1 ? '' : 's'}
          {data && <> · fetched {formatAge(data.cache_age_seconds)}</>}
        </span>
      </div>

      {error && !data && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {error}
        </div>
      )}

      <div style={{ ...CARD, overflow: 'auto', maxHeight: '65vh' }}>
        {loading && !data ? (
          <div style={{ padding: 40, textAlign: 'center', color: COLORS.textMuted }}>Loading…</div>
        ) : headers.length === 0 ? (
          <div style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted }}>No data available for this resource type.</div>
        ) : visibleRows.length === 0 ? (
          <div style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted }}>
            No {allRows.length === 0 ? 'resources found' : 'rows match your search'}.
          </div>
        ) : (
          <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
            <thead>
              <tr style={{ background: '#f8fafc', position: 'sticky', top: 0, zIndex: 1 }}>
                {rowDetails && <th style={{ width: 32, borderBottom: `1px solid ${COLORS.border}` }} />}
                {headers.map(h => (
                  <th key={h} style={{ textAlign: 'left', padding: '9px 12px', borderBottom: `1px solid ${COLORS.border}`, color: COLORS.textMuted, fontWeight: 700, fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.3, whiteSpace: 'nowrap' }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visibleRows.map(({ row, originalIndex }) => {
                const detail = rowDetails ? rowDetails[originalIndex] : null
                const isExpandable = !!detail
                const isExpanded = expandedRows.has(originalIndex)
                return (
                  <React.Fragment key={originalIndex}>
                    <tr
                      style={{ borderBottom: `1px solid ${COLORS.border}`, cursor: isExpandable ? 'pointer' : 'default' }}
                      onClick={isExpandable ? () => toggleRow(originalIndex) : undefined}
                    >
                      {rowDetails && (
                        <td style={{ padding: '8px 6px', textAlign: 'center', color: COLORS.textMuted, fontSize: 11 }}>
                          {isExpandable ? (isExpanded ? '▾' : '▸') : ''}
                        </td>
                      )}
                      {headers.map((h, ci) => (
                        <td key={h} style={{ padding: '8px 12px', color: COLORS.textPrimary, verticalAlign: 'top', maxWidth: 320 }}>
                          <Cell value={row[ci]} />
                        </td>
                      ))}
                    </tr>
                    {isExpandable && isExpanded && (
                      <tr>
                        <td colSpan={headers.length + 1} style={{ padding: 0, borderBottom: `1px solid ${COLORS.border}` }}>
                          <NsgRuleDetailPanel detail={detail} />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

export default function AzureResourcesPage() {
  const [resourceTypes, setResourceTypes] = useState(getPageCache(RESOURCE_TYPES_CACHE_KEY)?.resource_types || [])
  const [typesErr, setTypesErr] = useState('')
  const [activeType, setActiveType] = useState('vm')

  useEffect(() => {
    api.getAzureResourceTypes()
      .then(d => { setResourceTypes(d.resource_types || []); setPageCache(RESOURCE_TYPES_CACHE_KEY, d) })
      .catch(e => setTypesErr(e?.response?.data?.detail || 'Could not load Azure resource types'))
  }, [])

  return (
    <div style={{ padding: '24px 28px', maxWidth: 1500 }}>
      <div style={{ marginBottom: 18 }}>
        <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>☁️ Azure Resources</h2>
        <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
          Read-only inventory for this org's Azure subscription (Reader role only — no changes can be made from here).
        </div>
      </div>

      {typesErr && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {typesErr}
        </div>
      )}

      <div style={{ ...TAB_BAR, marginBottom: 16, overflowX: 'auto' }}>
        {resourceTypes.map(rt => (
          <button key={rt.id} onClick={() => setActiveType(rt.id)} style={tabButtonStyle(activeType === rt.id)}>
            {rt.icon} {rt.label}
          </button>
        ))}
      </div>

      <AzureInventoryTable activeType={activeType} />
    </div>
  )
}
