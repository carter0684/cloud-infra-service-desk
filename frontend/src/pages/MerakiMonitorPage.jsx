import { useState, useEffect, useCallback, Fragment } from 'react'
import { api } from '../api.js'
import { COLORS, CARD, TAB_BAR, tabButtonStyle, InfoBanner, badgeStyle } from '../utils/theme.js'
import { BarChart, GaugeRing } from '../components/SimpleCharts.jsx'
import { getPageCache, setPageCache } from '../utils/pageCache.js'
import { formatDateTimeHK } from '../utils/time.js'

// ── Local Infra Hardware Monitor (Catalogue) ────────────────────────────
// Cisco Meraki Access Point monitoring dashboard — a single AWS-external
// SaaS data source (Meraki Dashboard API), read-only throughout. See
// backend/app/meraki_monitor.py's module docstring for the full data-
// source rationale (91 networks in this org, ~10 calls/sec/org rate
// limit, so latency/events widgets are limited to the busiest networks
// by client count rather than fanning out over every network).
//
// Structured as an internal-tab page (same pattern as WafMonitoringPage/
// HelpdeskTeamPage) since the user explicitly asked for this as a
// "Catalogue" — one sidebar entry, several topic tabs inside.
//
// VISUAL STYLE: uses the portal's standard light theme (COLORS/CARD from
// theme.js) — same white cards, borders, and text colors as every other
// page. An earlier iteration used a dark Grafana/NOC-style look scoped
// to just this page, but the user asked to revert to the portal's own
// color style, so the layout below (colored section banners, big-number
// KPI tiles, panelled charts) is kept but re-skinned with the shared
// light-theme tokens instead of a page-local dark palette.

// Section-header banner colors — cycled per topic block, kept as flat
// colored bars (bold white text) for visual separation between topics,
// but now drawn from the portal's own palette (brand/success/warning/
// info) rather than dark-theme greens/oranges/olives.
const BANNERS = {
  overview: COLORS.brand,
  access_points: COLORS.brand,
  ap_network_monitoring: '#059669', // distinct green — an audit-log tab, kept visually separate from the KPI-style Overview banner
  wireless_network: COLORS.warning,
  access_point_util: COLORS.info,
  logs: '#7c3aed', // matches the portal's existing purple accent (see PRIORITY_COLOR.High-adjacent tones in theme.js)
  sonicwall: '#f97316', // orange — visually distinct from every Meraki-topic banner, since this is a different vendor/data source entirely
  fortigate: '#dc2626', // red — Fortinet's own brand color, and visually distinct from both the Meraki (blue/green/purple) and SonicWall (orange) banners above
  fortiswitch: '#0891b2', // teal — a distinct Fortinet-family sub-tab, visually separate from the red Fortigate Firewall banner even though both use the same underlying API
}

function fmtNum(n) {
  return (n ?? 0).toLocaleString()
}

function fmtPct(n, digits = 1) {
  return typeof n === 'number' && Number.isFinite(n) ? `${n.toFixed(digits)}%` : '—'
}

/** Format an ISO timestamp (Meraki always returns UTC) into HKT
 * (UTC+8), "YYYY-MM-DD HH:MM:SS" — used by the AP Network Monitoring
 * tab so up/down timestamps read in the org's local timezone instead of
 * raw UTC. Computed via a fixed +8h offset rather than a timezone
 * library (no new dependency needed for a single fixed-offset zone with
 * no DST). Returns '—' for missing/invalid timestamps. */
function fmtHkt(isoString) {
  if (!isoString) return '—'
  const d = new Date(isoString)
  if (Number.isNaN(d.getTime())) return '—'
  const hkt = new Date(d.getTime() + 8 * 60 * 60 * 1000)
  const pad = n => String(n).padStart(2, '0')
  return `${hkt.getUTCFullYear()}-${pad(hkt.getUTCMonth() + 1)}-${pad(hkt.getUTCDate())} ${pad(hkt.getUTCHours())}:${pad(hkt.getUTCMinutes())}:${pad(hkt.getUTCSeconds())} HKT`
}

/** Extract just the "YYYY-MM-DD" HKT calendar date from an ISO
 * timestamp — used both to render a compact date column and to compare
 * against the AP Network Monitoring tab's date filter (which is also an
 * HKT calendar date, since that's the timezone the user asked to filter
 * in). */
function hktDateOnly(isoString) {
  const full = fmtHkt(isoString)
  return full === '—' ? '' : full.slice(0, 10)
}

const STATUS_COLOR = { online: COLORS.success, offline: COLORS.danger, alerting: COLORS.warning, dormant: COLORS.textMuted, unknown: COLORS.textMuted }

function StatusBadge({ status }) {
  const color = STATUS_COLOR[status] || COLORS.textMuted
  return <span style={badgeStyle(color)}>{status || 'unknown'}</span>
}

/** Colored section banner — the "Overview" / "Device Status" / "Wireless
 * Network" / "Access Point" title bars, kept from the reference layout
 * but colored from the portal's own palette (see BANNERS above). */
function SectionBanner({ title, color }) {
  return (
    <div style={{
      background: color, color: '#ffffff', textAlign: 'center',
      fontWeight: 700, fontSize: 13, letterSpacing: 0.3,
      padding: '9px 16px', borderRadius: 6, marginBottom: 14,
    }}>
      {title}
    </div>
  )
}

/** Panel wrapper — this page's chart/table container, using the portal's
 * shared CARD style (white background, standard border/shadow) instead
 * of a page-local dark panel. */
function Panel({ children, style }) {
  return (
    <div style={{ ...CARD, padding: 16, ...style }}>
      {children}
    </div>
  )
}

function PanelTitle({ children }) {
  return <div style={{ fontSize: 13, fontWeight: 700, color: COLORS.textPrimary, marginBottom: 12 }}>{children}</div>
}

/** Simple sortable/searchable data table — shared by several tabs below
 * so each doesn't re-implement its own filter/sort logic. Styled with
 * the portal's standard light-theme tokens (COLORS/CARD), matching every
 * other data table in the app. */
function DataTable({ columns, rows, emptyText = 'No data to display.', searchable = false, searchPlaceholder = 'Search…' }) {
  const [search, setSearch] = useState('')
  const filtered = searchable && search.trim()
    ? rows.filter(row => columns.some(col => String(row[col.key] ?? '').toLowerCase().includes(search.trim().toLowerCase())))
    : rows

  return (
    <div>
      {searchable && (
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder={searchPlaceholder}
          style={{ width: '100%', maxWidth: 320, padding: '7px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 13, marginBottom: 10 }} />
      )}
      <div style={{ overflowX: 'auto' }}>
        <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
          <thead>
            <tr style={{ textAlign: 'left', color: COLORS.textMuted, borderBottom: `1px solid ${COLORS.border}` }}>
              {columns.map(col => <th key={col.key} style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>{col.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr><td colSpan={columns.length} style={{ padding: '18px 10px', color: COLORS.textMuted, textAlign: 'center' }}>{emptyText}</td></tr>
            ) : filtered.map((row, i) => (
              <tr key={i} style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                {columns.map(col => (
                  <td key={col.key} style={{ padding: '6px 10px' }}>
                    {col.render ? col.render(row) : (row[col.key] ?? '—')}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** Large colored KPI tile — the "Devices up / Devices down" big-number
 * blocks, kept as solid-fill colored tiles (still readable/high-contrast
 * against the portal's white page background). */
function KpiTile({ label, value, bg }) {
  return (
    <div style={{
      background: bg, borderRadius: 8, padding: '18px 20px', flex: '1 1 200px', minWidth: 200,
      color: '#fff', position: 'relative', overflow: 'hidden',
    }}>
      <div style={{ fontSize: 12, fontWeight: 700, opacity: 0.9, marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 48, fontWeight: 800, lineHeight: 1 }}>{fmtNum(value)}</div>
    </div>
  )
}

/** Colored % text — used in the per-site breakdown table so a site's
 * uptime % is instantly readable as good/warning/bad without needing a
 * separate legend (management-dashboard convention: green ≥95%, amber
 * 80-94.9%, red <80%). */
function uptimeColor(pct) {
  if (!Number.isFinite(pct)) return COLORS.textMuted
  if (pct >= 95) return COLORS.success
  if (pct >= 80) return COLORS.warning
  return COLORS.danger
}

// ── Overview tab — framed as the page's "management overview health
// dashboard" landing view: an org-wide health gauge + KPI tiles up top
// (the "overall %" and Devices Up/Down the user asked to restore), a
// per-site Devices Up/Down breakdown (worst-uptime sites surfaced
// first — the actual "management view" ask, so a manager scanning this
// tab immediately sees which sites need attention without hunting
// through the full per-AP table), and the existing per-device status
// table underneath for drill-down. ──────────────────────────────────────
function OverviewTab() {
  const [overview, setOverview] = useState(getPageCache('meraki-overview') || null)
  const [aps, setAps] = useState(getPageCache('meraki-aps') || null)
  const [loading, setLoading] = useState(!overview || !aps)
  const [error, setError] = useState('')
  const [selectedSite, setSelectedSite] = useState('')
  const [selectedDeviceSite, setSelectedDeviceSite] = useState('')

  useEffect(() => {
    let cancelled = false
    Promise.all([api.getMerakiOverview(), api.getMerakiAccessPoints()])
      .then(([ov, apData]) => {
        if (cancelled) return
        setOverview(ov); setPageCache('meraki-overview', ov)
        setAps(apData); setPageCache('meraki-aps', apData)
      })
      .catch(e => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not fetch Meraki overview') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  if (loading && !overview) return <div style={{ padding: 24, color: COLORS.textMuted }}>Loading overview…</div>
  if (error && !overview) return <div style={{ padding: 24, color: COLORS.danger }}>{error}</div>

  const apRows = aps?.access_points || []
  const statusCounts = {}
  apRows.forEach(ap => { statusCounts[ap.status] = (statusCounts[ap.status] || 0) + 1 })

  // Org-wide overall % up — the gauge the user asked to "add back".
  const totalDevices = (overview?.online || 0) + (overview?.offline || 0) + (overview?.alerting || 0) + (overview?.dormant || 0)
  const overallUpPct = totalDevices > 0 ? ((overview?.online || 0) / totalDevices) * 100 : 0

  // Per-site Devices Up/Down breakdown — grouped client-side from the
  // full AP inventory (each row already carries `site` + `status`, so
  // no extra backend endpoint is needed). Sorted worst-uptime-first so
  // the "management view" surfaces problem sites at the top.
  const siteMap = {}
  apRows.forEach(ap => {
    const site = ap.site || '—'
    if (!siteMap[site]) siteMap[site] = { site, up: 0, down: 0, total: 0 }
    siteMap[site].total += 1
    if (ap.status === 'online') siteMap[site].up += 1
    else siteMap[site].down += 1
  })
  const allSiteRows = Object.values(siteMap)
    .map(s => ({ ...s, pct: s.total > 0 ? (s.up / s.total) * 100 : 0 }))
    .sort((a, b) => a.pct - b.pct)
  const sitesWithIssues = allSiteRows.filter(s => s.down > 0).length

  // Site filter for the per-site breakdown table below — options derived
  // from the site map itself so it only ever lists sites that actually
  // have APs, same pattern as the Access Points tab's site filter.
  const siteFilterOptions = allSiteRows.map(s => s.site).sort()
  const siteRows = selectedSite ? allSiteRows.filter(s => s.site === selectedSite) : allSiteRows

  // Network Name filter for the Device Status table — independent from
  // the per-site breakdown's filter above, since a manager may want to
  // drill into one site's per-device table without also having pre-
  // filtered the site breakdown to the same site.
  const deviceSiteOptions = Array.from(new Set(apRows.map(ap => ap.site).filter(Boolean))).sort()
  const deviceStatusRows = selectedDeviceSite ? apRows.filter(ap => ap.site === selectedDeviceSite) : apRows

  const siteColumns = [
    { key: 'site', label: 'Site' },
    { key: 'up', label: 'Devices Up', render: r => <span style={{ color: COLORS.success, fontWeight: 700 }}>{fmtNum(r.up)}</span> },
    { key: 'down', label: 'Devices Down', render: r => <span style={{ color: r.down > 0 ? COLORS.danger : COLORS.textMuted, fontWeight: 700 }}>{fmtNum(r.down)}</span> },
    { key: 'total', label: 'Total APs' },
    {
      key: 'pct', label: '% Up',
      render: r => (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 120 }}>
          <div style={{ flex: 1, background: COLORS.border, borderRadius: 4, height: 8, position: 'relative' }}>
            <div style={{ width: `${Math.max(2, r.pct)}%`, height: '100%', borderRadius: 4, background: uptimeColor(r.pct) }} />
          </div>
          <span style={{ fontWeight: 800, color: uptimeColor(r.pct), fontSize: 12.5, whiteSpace: 'nowrap' }}>{fmtPct(r.pct)}</span>
        </div>
      ),
    },
  ]

  return (
    <div>
      <InfoBanner text="Management overview health dashboard — read-only, live data fetched from the Cisco Meraki Dashboard API." />

      {/* Row 1: overall health gauge + org-wide management KPI tiles */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr', gap: 16, alignItems: 'start', marginBottom: 20 }}>
        <div>
          <SectionBanner title="Overall Health" color={BANNERS.overview} />
          <Panel>
            <GaugeRing
              pct={overallUpPct}
              ringColor={uptimeColor(overallUpPct)}
              stats={[
                { value: fmtNum(totalDevices), label: 'Total Devices' },
                { value: fmtNum(siteRows.length), label: 'Sites Monitored' },
                { value: fmtNum(sitesWithIssues), label: 'Sites Needing Attention' },
              ]}
            />
          </Panel>
        </div>
        <div>
          <SectionBanner title="Devices Up / Down (Org-wide)" color={BANNERS.overview} />
          <Panel style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
            <KpiTile label="Devices up" value={overview?.online} bg={COLORS.success} />
            <KpiTile label="Devices down" value={overview?.offline} bg={COLORS.danger} />
            <KpiTile label="Alerting" value={overview?.alerting} bg={COLORS.warning} />
            <KpiTile label="Dormant" value={overview?.dormant} bg={COLORS.textMuted} />
          </Panel>
        </div>
      </div>

      {/* Row 2: per-site Devices Up/Down breakdown — the management view */}
      <div style={{ marginBottom: 20 }}>
        <SectionBanner title="Devices Up / Down by Site — worst uptime first" color={BANNERS.overview} />

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
          <label style={{ fontSize: 12.5, color: COLORS.textSecondary, fontWeight: 600 }}>Filter by site:</label>
          <select value={selectedSite} onChange={e => setSelectedSite(e.target.value)}
            style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5, minWidth: 220 }}>
            <option value="">All sites ({fmtNum(allSiteRows.length)})</option>
            {siteFilterOptions.map(site => <option key={site} value={site}>{site}</option>)}
          </select>
          {selectedSite && (
            <button onClick={() => setSelectedSite('')}
              style={{ padding: '6px 12px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontSize: 12.5, fontWeight: 600, cursor: 'pointer' }}>
              Clear filter
            </button>
          )}
        </div>

        <Panel>
          <DataTable emptyText="No per-site data yet." columns={siteColumns} rows={siteRows} />
        </Panel>
      </div>

      {/* Row 3: per-device status table for drill-down */}
      <div>
        <SectionBanner title="Device Status" color={BANNERS.overview} />

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
          <label style={{ fontSize: 12.5, color: COLORS.textSecondary, fontWeight: 600 }}>Filter by Network Name:</label>
          <select value={selectedDeviceSite} onChange={e => setSelectedDeviceSite(e.target.value)}
            style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5, minWidth: 220 }}>
            <option value="">All networks ({fmtNum(apRows.length)})</option>
            {deviceSiteOptions.map(site => <option key={site} value={site}>{site}</option>)}
          </select>
          {selectedDeviceSite && (
            <button onClick={() => setSelectedDeviceSite('')}
              style={{ padding: '6px 12px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontSize: 12.5, fontWeight: 600, cursor: 'pointer' }}>
              Clear filter
            </button>
          )}
        </div>

        <Panel style={{ padding: 0 }}>
          <div style={{ maxHeight: 320, overflowY: 'auto', padding: 12 }}>
            <DataTable
              searchable searchPlaceholder="Search by site, device name, or serial…"
              emptyText="No device status data yet."
              columns={[
                { key: 'name', label: 'Device Hostname' },
                { key: 'serial', label: 'Serial' },
                { key: 'status', label: 'Status', render: r => <StatusBadge status={r.status} /> },
                { key: 'site', label: 'Network Name' },
              ]}
              rows={deviceStatusRows}
            />
          </div>
        </Panel>
      </div>
    </div>
  )
}

// ── Access Points tab: full AP inventory (Site / AP Name / Tag / Status /
// Model / Firmware / Serial / LAN IP). ──────────────────────────────────
function AccessPointsTab() {
  const [data, setData] = useState(getPageCache('meraki-aps') || null)
  const [loading, setLoading] = useState(!data)
  const [error, setError] = useState('')
  const [selectedSite, setSelectedSite] = useState('')

  useEffect(() => {
    let cancelled = false
    api.getMerakiAccessPoints()
      .then(d => { if (!cancelled) { setData(d); setPageCache('meraki-aps', d) } })
      .catch(e => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not fetch access point inventory') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  if (loading && !data) return <div style={{ padding: 24, color: COLORS.textMuted }}>Loading access points…</div>
  if (error && !data) return <div style={{ padding: 24, color: COLORS.danger }}>{error}</div>

  const allRows = data?.access_points || []
  // Site filter options — derived from the AP inventory itself (sorted,
  // deduped) rather than a separate /networks call, since every AP row
  // already carries its site name and this list should only ever show
  // sites that actually have APs in them.
  const siteOptions = Array.from(new Set(allRows.map(r => r.site).filter(Boolean))).sort()
  const rows = selectedSite ? allRows.filter(r => r.site === selectedSite) : allRows

  return (
    <div>
      <SectionBanner title={`Access Points — ${fmtNum(rows.length)} of ${fmtNum(allRows.length)} devices${selectedSite ? ` at ${selectedSite}` : ' across every site in this Meraki organization'}`} color={BANNERS.access_points} />

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <label style={{ fontSize: 12.5, color: COLORS.textSecondary, fontWeight: 600 }}>Filter by site:</label>
        <select value={selectedSite} onChange={e => setSelectedSite(e.target.value)}
          style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5, minWidth: 220 }}>
          <option value="">All sites ({fmtNum(allRows.length)})</option>
          {siteOptions.map(site => <option key={site} value={site}>{site}</option>)}
        </select>
        {selectedSite && (
          <button onClick={() => setSelectedSite('')}
            style={{ padding: '6px 12px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontSize: 12.5, fontWeight: 600, cursor: 'pointer' }}>
            Clear filter
          </button>
        )}
      </div>

      <Panel>
        <DataTable
          searchable searchPlaceholder="Search by site, AP name, tag, model, firmware, or serial…"
          columns={[
            { key: 'site', label: 'Site' },
            { key: 'name', label: 'AP Name' },
            { key: 'tags', label: 'Tag', render: r => (r.tags || []).join(', ') || '—' },
            { key: 'status', label: 'Status', render: r => <StatusBadge status={r.status} /> },
            { key: 'model', label: 'Model' },
            { key: 'firmware', label: 'Firmware Version', render: r => r.firmware || '—' },
            { key: 'serial', label: 'Serial Number', render: r => <span style={{ fontFamily: 'monospace', letterSpacing: 0.3 }}>{r.serial || '—'}</span> },
            { key: 'lan_ip', label: 'LAN IP' },
          ]}
          rows={rows}
        />
      </Panel>
    </div>
  )
}

// ── AP Network Monitoring tab: full up/down status-change audit log for
// every AP in the org, sourced from Meraki's native "availabilities
// change history" endpoint (a real event log, not a derived metric —
// see meraki_monitor.py's comment on this). Each row already carries
// the exact moment a device flipped online<->offline, so "last up" /
// "last down" per device is just the most recent row of each direction
// once grouped by serial. Filterable by Site / Date (HKT calendar day) /
// Status (Up or Down). ───────────────────────────────────────────────
/** Default export period — today back to 30 days ago, as
 * "YYYY-MM-DD" strings for the two <input type="date"> pickers below.
 * Matches this tab's own default lookback window (30 days) so the
 * period selector starts pre-filled with something sensible. */
function defaultExportRange() {
  const end = new Date()
  const start = new Date(end.getTime() - 30 * 24 * 60 * 60 * 1000)
  const toDateInput = d => d.toISOString().slice(0, 10)
  return { start: toDateInput(start), end: toDateInput(end) }
}

function ApNetworkMonitoringTab() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedSite, setSelectedSite] = useState('')
  const [selectedDate, setSelectedDate] = useState('')
  const [selectedStatus, setSelectedStatus] = useState('')
  const [exportRange, setExportRange] = useState(defaultExportRange)
  const [exportFormat, setExportFormat] = useState('xlsx')
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState('')

  useEffect(() => {
    let cancelled = false
    api.getMerakiAvailabilityHistory()
      .then(d => { if (!cancelled) setData(d) })
      .catch(e => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not fetch AP up/down history') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  if (loading && !data) return <div style={{ padding: 24, color: COLORS.textMuted }}>Loading AP up/down history…</div>
  if (error && !data) return <div style={{ padding: 24, color: COLORS.danger }}>{error}</div>

  const allEvents = data?.events || []

  // "Last Up" / "Last Down" summary per device — the most recent event
  // of each direction, computed from the same event log (events are
  // already sorted newest-first by the backend, so the first match per
  // serial+direction is the most recent one).
  const lastByDevice = {}
  allEvents.forEach(e => {
    if (!lastByDevice[e.serial]) lastByDevice[e.serial] = { serial: e.serial, name: e.name, site: e.site }
    const bucket = e.direction === 'up' ? 'last_up' : 'last_down'
    if (!lastByDevice[e.serial][bucket]) lastByDevice[e.serial][bucket] = e.occurred_at
  })
  const deviceSummaryRows = Object.values(lastByDevice).sort((a, b) => (a.site || '').localeCompare(b.site || '') || (a.name || '').localeCompare(b.name || ''))

  const siteOptions = Array.from(new Set(allEvents.map(e => e.site).filter(Boolean))).sort()
  const dateOptions = Array.from(new Set(allEvents.map(e => hktDateOnly(e.occurred_at)).filter(Boolean))).sort().reverse()

  const filteredEvents = allEvents.filter(e => {
    if (selectedSite && e.site !== selectedSite) return false
    if (selectedDate && hktDateOnly(e.occurred_at) !== selectedDate) return false
    if (selectedStatus && e.direction !== selectedStatus) return false
    return true
  })

  const anyFilterActive = selectedSite || selectedDate || selectedStatus
  const clearFilters = () => { setSelectedSite(''); setSelectedDate(''); setSelectedStatus('') }

  // Period-select + download — a separate date range from the on-screen
  // event log above (that one is a rolling 30-day window fetched once on
  // load; this lets the user pick an arbitrary period, up to Meraki's
  // 31-day hard cap for this report, and export it directly from the
  // API rather than being limited to whatever's already loaded on
  // screen). Reuses the Site/Status filters already selected above (but
  // not the on-screen Date filter, since the export has its own explicit
  // period) so the download matches what the user is currently viewing.
  async function handleExport() {
    setExportError('')
    if (!exportRange.start || !exportRange.end) {
      setExportError('Please select both a start and end date.')
      return
    }
    const startIso = new Date(`${exportRange.start}T00:00:00+08:00`).toISOString()
    const endIso = new Date(`${exportRange.end}T23:59:59+08:00`).toISOString()
    if (new Date(endIso) <= new Date(startIso)) {
      setExportError('End date must be after start date.')
      return
    }
    if ((new Date(endIso) - new Date(startIso)) / (24 * 60 * 60 * 1000) > 31) {
      setExportError('Date range cannot exceed 31 days (Meraki\'s limit for this report).')
      return
    }
    setExporting(true)
    try {
      await api.exportMerakiAvailabilityHistory(startIso, endIso, exportFormat, selectedSite, selectedStatus)
    } catch (e) {
      setExportError(e?.response?.data?.detail || 'Could not export AP up/down history')
    } finally {
      setExporting(false)
    }
  }

  const summaryColumns = [
    { key: 'site', label: 'Site' },
    { key: 'name', label: 'AP Name' },
    { key: 'serial', label: 'Serial', render: r => <span style={{ fontFamily: 'monospace', letterSpacing: 0.3 }}>{r.serial || '—'}</span> },
    { key: 'last_up', label: 'Last Up (HKT)', render: r => <span style={{ color: COLORS.success }}>{fmtHkt(r.last_up)}</span> },
    { key: 'last_down', label: 'Last Down (HKT)', render: r => <span style={{ color: r.last_down ? COLORS.danger : COLORS.textMuted }}>{fmtHkt(r.last_down)}</span> },
  ]

  const eventColumns = [
    { key: 'occurred_at', label: 'Date / Time (HKT)', render: r => fmtHkt(r.occurred_at) },
    { key: 'site', label: 'Site' },
    { key: 'name', label: 'AP Name' },
    { key: 'serial', label: 'Serial', render: r => <span style={{ fontFamily: 'monospace', letterSpacing: 0.3 }}>{r.serial || '—'}</span> },
    {
      key: 'direction', label: 'Status',
      render: r => (
        <span style={badgeStyle(r.direction === 'up' ? COLORS.success : COLORS.danger)}>
          {r.direction === 'up' ? '▲ Up' : '▼ Down'}
        </span>
      ),
    },
    { key: 'old_status', label: 'From', render: r => r.old_status || '—' },
    { key: 'new_status', label: 'To', render: r => r.new_status || '—' },
  ]

  return (
    <div>
      <SectionBanner title="AP Network Monitoring — Up/Down status-change log" color={BANNERS.ap_network_monitoring} />
      <InfoBanner text={`Read-only up/down audit log sourced from Meraki's device availability history (last ${Math.round((data?.timespan || 0) / 86400)} days, org-wide cap is 31 days). Timestamps shown in Hong Kong Time (UTC+8).`} />

      {/* Period-select + download */}
      <div style={{ marginBottom: 20 }}>
        <PanelTitle>Download Report</PanelTitle>
        <Panel>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 14, flexWrap: 'wrap' }}>
            <div>
              <label style={{ fontSize: 12, color: COLORS.textSecondary, fontWeight: 600, display: 'block', marginBottom: 4 }}>From</label>
              <input type="date" value={exportRange.start} max={exportRange.end}
                onChange={e => setExportRange(r => ({ ...r, start: e.target.value }))}
                style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5 }} />
            </div>
            <div>
              <label style={{ fontSize: 12, color: COLORS.textSecondary, fontWeight: 600, display: 'block', marginBottom: 4 }}>To</label>
              <input type="date" value={exportRange.end} min={exportRange.start}
                onChange={e => setExportRange(r => ({ ...r, end: e.target.value }))}
                style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5 }} />
            </div>
            <div>
              <label style={{ fontSize: 12, color: COLORS.textSecondary, fontWeight: 600, display: 'block', marginBottom: 4 }}>Format</label>
              <select value={exportFormat} onChange={e => setExportFormat(e.target.value)}
                style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5 }}>
                <option value="xlsx">Excel (.xlsx)</option>
                <option value="csv">CSV (.csv)</option>
              </select>
            </div>
            <button onClick={handleExport} disabled={exporting}
              style={{
                padding: '8px 16px', borderRadius: 7, border: `1px solid ${COLORS.brand}`,
                background: exporting ? COLORS.border : COLORS.brand, color: exporting ? COLORS.textMuted : '#fff',
                fontWeight: 700, fontSize: 12.5, cursor: exporting ? 'default' : 'pointer',
              }}>
              {exporting ? '⏳ Exporting…' : '📥 Download'}
            </button>
            {(selectedSite || selectedStatus) && (
              <div style={{ fontSize: 11.5, color: COLORS.textMuted }}>
                Applies current Site{selectedSite ? ` (${selectedSite})` : ''} / Status{selectedStatus ? ` (${selectedStatus})` : ''} filter{selectedSite && selectedStatus ? 's' : ''} from below.
              </div>
            )}
          </div>
          {exportError && <div style={{ color: COLORS.danger, fontSize: 12.5, marginTop: 10 }}>{exportError}</div>}
        </Panel>
      </div>

      {/* Last Up / Last Down summary per device */}
      <div style={{ marginBottom: 20 }}>
        <PanelTitle>Last Up / Last Down per Device</PanelTitle>
        <Panel style={{ padding: 0 }}>
          <div style={{ maxHeight: 280, overflowY: 'auto', padding: 12 }}>
            <DataTable
              searchable searchPlaceholder="Search by site, AP name, or serial…"
              emptyText="No up/down events recorded in this window yet."
              columns={summaryColumns}
              rows={deviceSummaryRows}
            />
          </div>
        </Panel>
      </div>

      {/* Full event log with Site / Date / Status filters */}
      <PanelTitle>Full Up/Down Event Log</PanelTitle>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <label style={{ fontSize: 12.5, color: COLORS.textSecondary, fontWeight: 600 }}>Filter by Site:</label>
        <select value={selectedSite} onChange={e => setSelectedSite(e.target.value)}
          style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5, minWidth: 180 }}>
          <option value="">All sites</option>
          {siteOptions.map(site => <option key={site} value={site}>{site}</option>)}
        </select>

        <label style={{ fontSize: 12.5, color: COLORS.textSecondary, fontWeight: 600 }}>Filter by Date (HKT):</label>
        <select value={selectedDate} onChange={e => setSelectedDate(e.target.value)}
          style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5, minWidth: 150 }}>
          <option value="">All dates</option>
          {dateOptions.map(date => <option key={date} value={date}>{date}</option>)}
        </select>

        <label style={{ fontSize: 12.5, color: COLORS.textSecondary, fontWeight: 600 }}>Filter by Status:</label>
        <select value={selectedStatus} onChange={e => setSelectedStatus(e.target.value)}
          style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5, minWidth: 140 }}>
          <option value="">Up + Down</option>
          <option value="up">Up only</option>
          <option value="down">Down only</option>
        </select>

        {anyFilterActive && (
          <button onClick={clearFilters}
            style={{ padding: '6px 12px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontSize: 12.5, fontWeight: 600, cursor: 'pointer' }}>
            Clear filters
          </button>
        )}
      </div>

      <Panel>
        <DataTable
          emptyText="No up/down events match the current filters."
          columns={eventColumns}
          rows={filteredEvents}
        />
      </Panel>
    </div>
  )
}

// ── Wireless Network tab: client count by network, avg latency per
// network, top 10 loss %, top 10 latency ────────────────────────────────
function WirelessNetworkTab() {
  const [clients, setClients] = useState(null)
  const [latency, setLatency] = useState(null)
  const [loss, setLoss] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    Promise.all([
      api.getMerakiTopNetworksByClients(),
      api.getMerakiTopLatencyNetworks(),
      api.getMerakiPacketLossByNetwork(),
    ])
      .then(([c, l, p]) => { if (!cancelled) { setClients(c); setLatency(l); setLoss(p) } })
      .catch(e => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not fetch wireless network data') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  if (loading) return <div style={{ padding: 24, color: COLORS.textMuted }}>Loading wireless network data…</div>
  if (error) return <div style={{ padding: 24, color: COLORS.danger }}>{error}</div>

  const clientNetworks = clients?.networks || []
  const latencyNetworks = latency?.networks || []
  const lossNetworks = loss?.networks || []

  const clientChartItems = clientNetworks.slice(0, 15).map(n => ({ label: n.network_name, value: n.client_count }))
  const latencyChartItems = latencyNetworks.slice(0, 10).map(n => ({ label: n.network_name, value: Math.round(n.avg_latency_ms) }))
  const lossChartItems = lossNetworks.slice(0, 10).map(n => ({ label: n.network_name, value: Math.round(n.loss_pct * 10) / 10 }))

  return (
    <div>
      <SectionBanner title="Wireless Network metrics" color={BANNERS.wireless_network} />
      {latency?.note && <InfoBanner text={latency.note} />}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 16 }}>
        <Panel>
          <PanelTitle>Wireless Client Count by Network</PanelTitle>
          <BarChart items={clientChartItems} valueFormatter={v => fmtNum(v)} />
        </Panel>
        <Panel>
          <PanelTitle>Wireless Average Latency per Network (ms)</PanelTitle>
          <BarChart items={latencyChartItems} valueFormatter={v => `${fmtNum(v)} ms`} />
        </Panel>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <Panel>
          <PanelTitle>Top 10 Networks — Wireless Loss %</PanelTitle>
          <BarChart items={lossChartItems.slice(0, 10)} valueFormatter={v => fmtPct(v)} />
        </Panel>
        <Panel>
          <PanelTitle>Top 10 Networks — Avg Latency (ms)</PanelTitle>
          <BarChart items={latencyChartItems.slice(0, 10)} valueFormatter={v => `${fmtNum(v)} ms`} />
        </Panel>
      </div>
    </div>
  )
}

// ── Access Point tab: channel utilization by network/device (current +
// top-change), and top-10 AP lists by client count / Wi-Fi util /
// non-Wi-Fi util. ───────────────────────────────────────────────────────
function AccessPointUtilTab() {
  const [cuNetwork, setCuNetwork] = useState(null)
  const [cuDevice, setCuDevice] = useState(null)
  const [changeNetwork, setChangeNetwork] = useState(null)
  const [changeDevice, setChangeDevice] = useState(null)
  const [topDevicesByClients, setTopDevicesByClients] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    Promise.all([
      api.getMerakiChannelUtilizationByNetwork(),
      api.getMerakiChannelUtilizationByDevice(),
      api.getMerakiChannelUtilizationChangeByNetwork(),
      api.getMerakiChannelUtilizationChangeByDevice(),
      api.getMerakiTopDevicesByClients(),
    ])
      .then(([cn, cd, chn, chd, td]) => {
        if (cancelled) return
        setCuNetwork(cn); setCuDevice(cd); setChangeNetwork(chn); setChangeDevice(chd); setTopDevicesByClients(td)
      })
      .catch(e => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not fetch access point utilization data') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  if (loading) return <div style={{ padding: 24, color: COLORS.textMuted }}>Loading access point utilization…</div>
  if (error) return <div style={{ padding: 24, color: COLORS.danger }}>{error}</div>

  const networks = cuNetwork?.networks || []
  const devices = cuDevice?.devices || []
  const changeNetworks = changeNetwork?.networks || []
  const changeDevices = changeDevice?.devices || []
  const topClientDevices = topDevicesByClients?.devices || []

  const avgUtilNetworkItems = networks.slice(0, 15)
    .sort((a, b) => (b.total_pct || 0) - (a.total_pct || 0))
    .map(n => ({ label: n.network_name, value: Math.round((n.total_pct || 0) * 10) / 10 }))

  const top80211NetworkItems = [...networks]
    .sort((a, b) => (b.wifi_pct || 0) - (a.wifi_pct || 0))
    .slice(0, 10)
    .map(n => ({ label: n.network_name, value: Math.round((n.wifi_pct || 0) * 10) / 10 }))

  const avgUtilDeviceItems = devices.slice(0, 15)
    .sort((a, b) => (b.total_pct || 0) - (a.total_pct || 0))
    .map(d => ({ label: d.name, value: Math.round((d.total_pct || 0) * 10) / 10 }))

  const topNonWifiDeviceItems = [...devices]
    .sort((a, b) => (b.non_wifi_pct || 0) - (a.non_wifi_pct || 0))
    .slice(0, 10)
    .map(d => ({ label: d.name, value: Math.round((d.non_wifi_pct || 0) * 10) / 10 }))

  const top10ApWifiItems = [...devices]
    .sort((a, b) => (b.wifi_pct || 0) - (a.wifi_pct || 0))
    .slice(0, 10)
    .map(d => ({ label: d.name, value: Math.round((d.wifi_pct || 0) * 10) / 10 }))

  const top10ApNonWifiItems = [...devices]
    .sort((a, b) => (b.non_wifi_pct || 0) - (a.non_wifi_pct || 0))
    .slice(0, 10)
    .map(d => ({ label: d.name, value: Math.round((d.non_wifi_pct || 0) * 10) / 10 }))

  const top10ApClientItems = topClientDevices.slice(0, 10).map(d => ({ label: d.name, value: d.client_count }))

  const pctFormatter = v => fmtPct(v)

  const changeColumns = [
    { key: 'name_or_network', label: 'Network / Device' },
    { key: 'from_pct', label: 'From %', render: r => fmtPct(r.from_pct) },
    { key: 'to_pct', label: 'To %', render: r => fmtPct(r.to_pct) },
    {
      key: 'change_pct', label: 'Change',
      render: r => (
        <span style={{ color: r.change_pct >= 0 ? COLORS.danger : COLORS.success, fontWeight: 700 }}>
          {r.change_pct >= 0 ? '▲' : '▼'} {Math.abs(r.change_pct).toFixed(1)} pts
        </span>
      ),
    },
  ]

  return (
    <div>
      <SectionBanner title="Access Point metrics" color={BANNERS.access_point_util} />

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 20 }}>
        <Panel>
          <PanelTitle>Avg AP Channel Utilization by Network</PanelTitle>
          <BarChart items={avgUtilNetworkItems} valueFormatter={pctFormatter} />
        </Panel>
        <Panel>
          <PanelTitle>Avg AP Channel Utilization by Device</PanelTitle>
          <BarChart items={avgUtilDeviceItems} valueFormatter={pctFormatter} />
        </Panel>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 20 }}>
        <Panel>
          <PanelTitle>Top Change in AP Channel Utilization by Network</PanelTitle>
          <DataTable columns={changeColumns} rows={changeNetworks.map(n => ({ ...n, name_or_network: n.network_name }))} emptyText="No change data yet." />
        </Panel>
        <Panel>
          <PanelTitle>Top Change in AP Channel Utilization by Device</PanelTitle>
          <DataTable columns={changeColumns} rows={changeDevices.map(d => ({ ...d, name_or_network: d.name }))} emptyText="No change data yet." />
        </Panel>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 20 }}>
        <Panel>
          <PanelTitle>Top AP Channel Utilization by Network (802.11)</PanelTitle>
          <BarChart items={top80211NetworkItems} valueFormatter={pctFormatter} />
        </Panel>
        <Panel>
          <PanelTitle>Top AP Channel Utilization by Device (Non-802.11)</PanelTitle>
          <BarChart items={topNonWifiDeviceItems} valueFormatter={pctFormatter} />
        </Panel>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 16 }}>
        <Panel>
          <PanelTitle>Top 10 APs — Client Count</PanelTitle>
          <BarChart items={top10ApClientItems} valueFormatter={v => fmtNum(v)} />
        </Panel>
        <Panel>
          <PanelTitle>Top 10 APs — Wi-Fi Channel Util %</PanelTitle>
          <BarChart items={top10ApWifiItems} valueFormatter={pctFormatter} />
        </Panel>
        <Panel>
          <PanelTitle>Top 10 APs — Non-Wi-Fi Channel Util %</PanelTitle>
          <BarChart items={top10ApNonWifiItems} valueFormatter={pctFormatter} />
        </Panel>
      </div>
    </div>
  )
}

// ── Logs tab: Meraki Error Logs table AND "Logs (source: meraki)" —
// same underlying event-log data (see meraki_monitor.py's docstring for
// why one table satisfies both: everything here already originates from
// Meraki, so a literal "source:meraki" filter is trivially satisfied). ─
function LogsTab() {
  const [events, setEvents] = useState(null)
  const [networks, setNetworks] = useState([])
  const [selectedNetwork, setSelectedNetwork] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    api.getMerakiNetworks()
      .then(d => { if (!cancelled) setNetworks(d.networks || []) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])

  const fetchEvents = useCallback((networkId) => {
    let cancelled = false
    setLoading(true)
    setError('')
    api.getMerakiEvents(networkId || null, 100)
      .then(d => { if (!cancelled) setEvents(d) })
      .catch(e => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not fetch Meraki event log') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    const cancel = fetchEvents(selectedNetwork)
    return cancel
  }, [selectedNetwork, fetchEvents])

  const rows = events?.events || []

  return (
    <div>
      <SectionBanner title='Logs — every row originates from Cisco Meraki (device/client events)' color={BANNERS.logs} />

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <label style={{ fontSize: 12.5, color: COLORS.textSecondary, fontWeight: 600 }}>Filter by network (site):</label>
        <select value={selectedNetwork} onChange={e => setSelectedNetwork(e.target.value)}
          style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5 }}>
          <option value="">All (busiest networks)</option>
          {networks.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
        </select>
        <button onClick={() => fetchEvents(selectedNetwork)}
          style={{ padding: '6px 12px', borderRadius: 6, border: `1px solid ${COLORS.brand}`, background: '#fff', color: COLORS.brand, fontSize: 12.5, fontWeight: 700, cursor: 'pointer' }}>
          ↻ Refresh
        </button>
      </div>

      {events?.note && <InfoBanner text={events.note} />}

      {loading ? (
        <div style={{ padding: 24, color: COLORS.textMuted }}>Loading event log…</div>
      ) : error ? (
        <div style={{ padding: 24, color: COLORS.danger }}>{error}</div>
      ) : (
        <Panel>
          <DataTable
            searchable searchPlaceholder="Search AP name, category, SSID, or description…"
            emptyText="No Meraki events found in this window."
            columns={[
              { key: 'occurred_at', label: 'Date / Time' },
              { key: 'ap_name', label: 'AP Name' },
              { key: 'category', label: 'Category' },
              { key: 'ssid', label: 'SSID', render: r => r.ssid || '—' },
              { key: 'device_name', label: 'Device Name' },
              { key: 'description', label: 'Error Description' },
              { key: 'network_name', label: 'Site' },
            ]}
            rows={rows}
          />
        </Panel>
      )}
    </div>
  )
}

// ── SonicWall tab — a "simulated health dashboard" sourced from the
// MySonicWall MSSP API (see backend/app/sonicwall_monitor.py's module
// docstring). Unlike every Meraki topic above, this API does not expose
// real-time device up/down telemetry — only MSSP tenant/cloud-service
// provisioning ratios (e.g. "6 of 6" firewalls provisioned under FW
// Management) — so the KPI tiles and gauge below are deliberately framed
// as "Provisioning %" rather than "Uptime %", to avoid implying a
// real-time health signal the underlying API can't actually provide. ────
function SonicWallTab() {
  const [overview, setOverview] = useState(getPageCache('sonicwall-overview') || null)
  const [tenants, setTenants] = useState(getPageCache('sonicwall-tenants') || null)
  const [loading, setLoading] = useState(!overview || !tenants)
  const [error, setError] = useState('')

  const load = useCallback((refresh = false) => {
    setLoading(true)
    Promise.all([api.getSonicWallOverview(refresh), api.getSonicWallTenants(refresh)])
      .then(([ov, tn]) => {
        setOverview(ov); setPageCache('sonicwall-overview', ov)
        setTenants(tn); setPageCache('sonicwall-tenants', tn)
        setError('')
      })
      .catch(e => setError(e?.response?.data?.detail || 'Could not fetch SonicWall data'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load(false) }, [load])

  if (loading && !overview) return <div style={{ padding: 24, color: COLORS.textMuted }}>Loading SonicWall data…</div>
  if (error && !overview) return <div style={{ padding: 24, color: COLORS.danger }}>{error}</div>

  const tenantRows = tenants?.tenants || []
  const provisioningPct = overview?.provisioning_pct

  // Flatten tenant → service rows for the per-service table, so every
  // licensed cloud service under every tenant gets its own row (a tenant
  // can have several services — FW Management, Reports, Wireless, etc.)
  const serviceRows = []
  tenantRows.forEach(t => {
    (t.services || []).forEach(svc => {
      serviceRows.push({
        tenant_name: t.name,
        tenant_serial: svc.tenant_serial || '—',
        service_name: svc.service_name,
        is_enabled: svc.is_enabled,
        ratio: (svc.provisioned != null && svc.licensed != null) ? `${svc.provisioned} of ${svc.licensed}` : '—',
        pct: (svc.provisioned != null && svc.licensed != null && svc.licensed > 0) ? (svc.provisioned / svc.licensed) * 100 : null,
      })
    })
  })

  const tenantColumns = [
    { key: 'name', label: 'Tenant' },
    { key: 'product_count', label: 'Products' },
    { key: 'services', label: 'Licensed Services', render: r => (r.services || []).length },
  ]

  const serviceColumns = [
    { key: 'tenant_name', label: 'Tenant' },
    { key: 'service_name', label: 'Cloud Service' },
    { key: 'tenant_serial', label: 'Firewall Serial' },
    {
      key: 'is_enabled', label: 'Enabled',
      render: r => <span style={badgeStyle(r.is_enabled ? COLORS.success : COLORS.textMuted)}>{r.is_enabled ? 'Enabled' : 'Disabled'}</span>,
    },
    { key: 'ratio', label: 'Provisioned of Licensed' },
    {
      key: 'pct', label: 'Provisioning %',
      render: r => r.pct == null ? '—' : (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 120 }}>
          <div style={{ flex: 1, background: COLORS.border, borderRadius: 4, height: 8, position: 'relative' }}>
            <div style={{ width: `${Math.max(2, r.pct)}%`, height: '100%', borderRadius: 4, background: uptimeColor(r.pct) }} />
          </div>
          <span style={{ fontWeight: 800, color: uptimeColor(r.pct), fontSize: 12.5, whiteSpace: 'nowrap' }}>{fmtPct(r.pct)}</span>
        </div>
      ),
    },
  ]

  return (
    <div>
      <InfoBanner text="Simulated health dashboard — read-only data from the MySonicWall MSSP API. This API exposes MSSP tenant / cloud-service provisioning ratios (licensed vs. provisioned firewall seats), not real-time per-device uptime, so figures below reflect provisioning status rather than live device health." />

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr', gap: 16, alignItems: 'start', marginBottom: 20 }}>
        <div>
          <SectionBanner title="Overall Provisioning Health" color={BANNERS.sonicwall} />
          <Panel>
            <GaugeRing
              pct={provisioningPct ?? 0}
              ringColor={uptimeColor(provisioningPct ?? 0)}
              stats={[
                { value: fmtNum(overview?.tenant_count), label: 'MSSP Tenants' },
                { value: fmtNum(overview?.total_products), label: 'Total Products' },
                { value: fmtNum(overview?.enabled_service_count), label: 'Enabled Services' },
              ]}
            />
          </Panel>
        </div>
        <div>
          <SectionBanner title="Provisioning Summary" color={BANNERS.sonicwall} />
          <Panel>
            <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
              <KpiTile label="Provisioned Seats" value={overview?.total_provisioned} bg={COLORS.success} />
              <KpiTile label="Licensed Seats" value={overview?.total_licensed} bg={COLORS.info} />
              <KpiTile label="MSSP Tenants" value={overview?.tenant_count} bg={BANNERS.sonicwall} />
            </div>
          </Panel>
        </div>
      </div>

      <div style={{ marginBottom: 20 }}>
        <SectionBanner title="MSSP Tenants" color={BANNERS.sonicwall} />
        <Panel>
          <PanelTitle>{tenantRows.length} tenant(s) under this MySonicWall account</PanelTitle>
          <DataTable columns={tenantColumns} rows={tenantRows} searchable searchPlaceholder="Search tenants…" />
        </Panel>
      </div>

      <div>
        <SectionBanner title="Cloud Services by Tenant" color={BANNERS.sonicwall} />
        <Panel>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 10 }}>
            <button onClick={() => load(true)} style={{ ...tabButtonStyle(false), padding: '6px 14px' }}>Refresh</button>
          </div>
          <DataTable columns={serviceColumns} rows={serviceRows} searchable searchPlaceholder="Search services…" emptyText="No licensed cloud services found." />
        </Panel>
      </div>
    </div>
  )
}

// ── Fortigate Firewall sub-tab — health dashboard sourced from the
// FortiManager Cloud JSON-RPC API (see backend/app/fortigate_monitor.py's
// module docstring for the 3-step OAuth2 -> session -> jsonrpc auth
// flow). Unlike SonicWallTab above (a provisioning-ratio proxy, since
// MySonicWall's public API has no real-time telemetry), FortiManager's
// conn_status IS a genuine real-time "is this FortiGate currently
// reachable" signal, so this is presented as an actual Up/Down health
// view rather than "simulated". ─────────────────────────────────────────
function fmtBytes(n) {
  if (n == null || !Number.isFinite(n)) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n, i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`
}

function fmtEpochHkt(epochSeconds) {
  if (!epochSeconds) return '—'
  try {
    return formatDateTimeHK(new Date(epochSeconds * 1000).toISOString())
  } catch {
    return new Date(epochSeconds * 1000).toLocaleString()
  }
}

/** Expandable detail panel for one FortiGate — CPU/Memory, Interface
 * Bandwidth, and VPN Tunnel status, fetched on demand only when this
 * device's row is expanded (see backend/app/fortigate_monitor.py's
 * "Per-device live detail" section docstring for why this is never
 * fetched for the whole device list at once). */
function FortigateDeviceDetailPanel({ deviceName }) {
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.getFortigateDeviceDetail(deviceName)
      .then(d => { if (!cancelled) { setDetail(d); setError('') } })
      .catch(e => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not fetch device detail') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [deviceName])

  if (loading) return <div style={{ padding: '14px 16px', color: COLORS.textMuted, fontSize: 12.5 }}>Loading CPU/Memory, interface, and VPN data for {deviceName}…</div>
  if (error) return <div style={{ padding: '14px 16px', color: COLORS.danger, fontSize: 12.5 }}>{error}</div>

  const interfaces = detail?.interfaces || []
  const tunnels = detail?.vpn_tunnels || []

  return (
    <div style={{ padding: '14px 16px', background: '#fafafa', borderTop: `1px solid ${COLORS.border}` }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 14 }}>
        <div>
          <div style={{ fontSize: 11.5, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', marginBottom: 6 }}>CPU / Memory</div>
          {detail?.cpu_error ? (
            <div style={{ fontSize: 12, color: COLORS.danger }}>{detail.cpu_error}</div>
          ) : (
            <div style={{ display: 'flex', gap: 20 }}>
              <div>
                <div style={{ fontSize: 22, fontWeight: 800, color: uptimeColor(100 - (detail?.cpu_pct ?? 0)) }}>{fmtPct(detail?.cpu_pct)}</div>
                <div style={{ fontSize: 11.5, color: COLORS.textMuted }}>CPU</div>
              </div>
              <div>
                <div style={{ fontSize: 22, fontWeight: 800, color: uptimeColor(100 - (detail?.mem_pct ?? 0)) }}>{fmtPct(detail?.mem_pct)}</div>
                <div style={{ fontSize: 11.5, color: COLORS.textMuted }}>
                  Memory{detail?.mem_used_bytes != null ? ` (${fmtBytes(detail.mem_used_bytes)} / ${fmtBytes(detail.mem_total_bytes)})` : ''}
                </div>
              </div>
            </div>
          )}
        </div>
        <div>
          <div style={{ fontSize: 11.5, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', marginBottom: 6 }}>VPN Tunnels</div>
          {detail?.vpn_error ? (
            <div style={{ fontSize: 12, color: COLORS.danger }}>{detail.vpn_error}</div>
          ) : tunnels.length === 0 ? (
            <div style={{ fontSize: 12, color: COLORS.textMuted }}>No VPN tunnels configured.</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {tunnels.map((t, i) => (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5 }}>
                  <span style={badgeStyle(t.status === 'up' ? COLORS.success : COLORS.danger)}>{t.status === 'up' ? '▲' : '▼'}</span>
                  <span style={{ fontWeight: 600 }}>{t.name || '—'}</span>
                  <span style={{ color: COLORS.textMuted }}>{t.remote_gateway || ''}</span>
                  <span style={{ color: COLORS.textMuted, marginLeft: 'auto', whiteSpace: 'nowrap' }}>
                    ↓{fmtBytes(t.incoming_bytes)} / ↑{fmtBytes(t.outgoing_bytes)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div>
        <div style={{ fontSize: 11.5, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', marginBottom: 6 }}>Interface Bandwidth</div>
        {detail?.interfaces_error ? (
          <div style={{ fontSize: 12, color: COLORS.danger }}>{detail.interfaces_error}</div>
        ) : interfaces.length === 0 ? (
          <div style={{ fontSize: 12, color: COLORS.textMuted }}>No active interfaces reported.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ textAlign: 'left', color: COLORS.textMuted }}>
                  <th style={{ padding: '4px 8px' }}>Interface</th>
                  <th style={{ padding: '4px 8px' }}>IP</th>
                  <th style={{ padding: '4px 8px' }}>Link</th>
                  <th style={{ padding: '4px 8px' }}>Speed</th>
                  <th style={{ padding: '4px 8px' }}>TX</th>
                  <th style={{ padding: '4px 8px' }}>RX</th>
                </tr>
              </thead>
              <tbody>
                {interfaces.map((i, idx) => (
                  <tr key={idx} style={{ borderTop: `1px solid ${COLORS.border}` }}>
                    <td style={{ padding: '4px 8px', fontWeight: 600 }}>{i.name}</td>
                    <td style={{ padding: '4px 8px' }}>{i.ip || '—'}</td>
                    <td style={{ padding: '4px 8px' }}><span style={badgeStyle(i.link ? COLORS.success : COLORS.textMuted)}>{i.link ? 'Up' : 'Down'}</span></td>
                    <td style={{ padding: '4px 8px' }}>{i.speed_mbps ? `${i.speed_mbps} Mbps` : '—'}</td>
                    <td style={{ padding: '4px 8px' }}>{fmtBytes(i.tx_bytes)}</td>
                    <td style={{ padding: '4px 8px' }}>{fmtBytes(i.rx_bytes)}</td>
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

function FortigateTab() {
  const [overview, setOverview] = useState(getPageCache('fortigate-overview') || null)
  const [devices, setDevices] = useState(getPageCache('fortigate-devices') || null)
  const [loading, setLoading] = useState(!overview || !devices)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  // Which device's detail row is currently expanded, keyed by device
  // name — only one open at a time, same convention as the Admin page's
  // group/tab-write pickers.
  const [expandedDevice, setExpandedDevice] = useState('')

  const load = useCallback((refresh = false) => {
    setLoading(true)
    Promise.all([api.getFortigateOverview(refresh), api.getFortigateDevices(refresh)])
      .then(([ov, dv]) => {
        setOverview(ov); setPageCache('fortigate-overview', ov)
        setDevices(dv); setPageCache('fortigate-devices', dv)
        setError('')
      })
      .catch(e => setError(e?.response?.data?.detail || 'Could not fetch Fortigate data'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load(false) }, [load])

  if (loading && !overview) return <div style={{ padding: 24, color: COLORS.textMuted }}>Loading Fortigate data…</div>
  if (error && !overview) return <div style={{ padding: 24, color: COLORS.danger }}>{error}</div>

  const allDeviceRows = devices?.devices || []
  const deviceRows = search.trim()
    ? allDeviceRows.filter(r => `${r.name} ${r.ip} ${r.platform} ${r.serial}`.toLowerCase().includes(search.trim().toLowerCase()))
    : allDeviceRows
  const upPct = overview?.up_pct

  return (
    <div>
      <InfoBanner text="Health dashboard — read-only, live data fetched from the FortiManager Cloud API. Status reflects each FortiGate's real-time management connection to FortiManager. Click a device's Details button to load its live CPU/Memory, interface bandwidth, and VPN tunnel status." />

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr', gap: 16, alignItems: 'start', marginBottom: 20 }}>
        <div>
          <SectionBanner title="Overall Device Health" color={BANNERS.fortigate} />
          <Panel>
            <GaugeRing
              pct={upPct ?? 0}
              ringColor={uptimeColor(upPct ?? 0)}
              stats={[
                { value: fmtNum(overview?.device_count), label: 'Devices' },
                { value: fmtNum(overview?.up_count), label: 'Up' },
                { value: fmtNum(overview?.down_count), label: 'Down' },
              ]}
            />
          </Panel>
        </div>
        <div>
          <SectionBanner title="Device Summary" color={BANNERS.fortigate} />
          <Panel>
            <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
              <KpiTile label="Devices Up" value={overview?.up_count} bg={COLORS.success} />
              <KpiTile label="Devices Down" value={overview?.down_count} bg={COLORS.danger} />
              <KpiTile label="Total Devices" value={overview?.device_count} bg={BANNERS.fortigate} />
            </div>
          </Panel>
        </div>
      </div>

      <div>
        <SectionBanner title="Managed FortiGates" color={BANNERS.fortigate} />
        <Panel>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10, gap: 10, flexWrap: 'wrap' }}>
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search devices…"
              style={{ flex: '1 1 260px', maxWidth: 320, padding: '7px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 13 }} />
            <button onClick={() => load(true)} style={{ ...tabButtonStyle(false), padding: '6px 14px' }}>Refresh</button>
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
              <thead>
                <tr style={{ textAlign: 'left', color: COLORS.textMuted, borderBottom: `1px solid ${COLORS.border}` }}>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Name</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>IP Address</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Model</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>OS Version</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Status</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Config Sync</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Location</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Connected Since</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}></th>
                </tr>
              </thead>
              <tbody>
                {deviceRows.length === 0 ? (
                  <tr><td colSpan={9} style={{ padding: '18px 10px', color: COLORS.textMuted, textAlign: 'center' }}>No managed FortiGates found.</td></tr>
                ) : deviceRows.map(r => (
                  <Fragment key={r.name}>
                    <tr style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                      <td style={{ padding: '6px 10px', fontWeight: 600 }}>{r.name}</td>
                      <td style={{ padding: '6px 10px' }}>{r.ip || '—'}</td>
                      <td style={{ padding: '6px 10px' }}>{r.platform || '—'}</td>
                      <td style={{ padding: '6px 10px' }}>{r.os_version || '—'}</td>
                      <td style={{ padding: '6px 10px' }}>
                        <span style={badgeStyle(r.conn_status === 'up' ? COLORS.success : COLORS.danger)}>{r.conn_status === 'up' ? '▲ Up' : '▼ Down'}</span>
                      </td>
                      <td style={{ padding: '6px 10px' }}>{r.conf_status || '—'}</td>
                      <td style={{ padding: '6px 10px' }}>
                        {r.latitude && r.longitude ? (
                          <a href={`https://www.google.com/maps?q=${r.latitude},${r.longitude}`} target="_blank" rel="noopener noreferrer" style={{ color: COLORS.brand }}>
                            📍 Map
                          </a>
                        ) : '—'}
                      </td>
                      <td style={{ padding: '6px 10px', whiteSpace: 'nowrap' }} title="When this device's management tunnel to FortiManager first came up — not the device's own OS uptime, which FortiOS's monitor API does not expose">
                        {fmtEpochHkt(r.first_tunnel_up)}
                      </td>
                      <td style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>
                        <button onClick={() => setExpandedDevice(prev => prev === r.name ? '' : r.name)}
                          style={{ border: 'none', background: 'none', color: COLORS.brand, fontSize: 12, fontWeight: 700, cursor: 'pointer', padding: '2px 4px' }}>
                          {expandedDevice === r.name ? 'Hide' : 'Details'}
                        </button>
                      </td>
                    </tr>
                    {expandedDevice === r.name && (
                      <tr>
                        <td colSpan={9} style={{ padding: 0 }}>
                          <FortigateDeviceDetailPanel deviceName={r.name} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </div>
  )
}

// ── FortiSwitch sub-tab — a separate sub-tab from Fortigate Firewall
// (per the user's explicit choice), even though it uses the SAME
// FortiManager Cloud API credentials. FortiSwitches are managed via
// FortiLink through a parent FortiGate rather than directly by
// FortiManager (see backend/app/fortigate_monitor.py's "FortiSwitches"
// section docstring), so "Parent FortiGate" is a first-class column
// here rather than switches being nested under the Fortigate Firewall
// tab's device rows. ─────────────────────────────────────────────────
function FortiSwitchTab() {
  const [overview, setOverview] = useState(getPageCache('fortiswitch-overview') || null)
  const [switches, setSwitches] = useState(getPageCache('fortiswitch-list') || null)
  const [loading, setLoading] = useState(!overview || !switches)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  // Which switch's port/VLAN detail row is currently expanded, keyed by
  // switch_id — only one open at a time, same convention as the
  // Fortigate Firewall tab's device detail rows. No separate fetch is
  // needed on expand: the ports array already arrived with the switch
  // list (see fortigate_monitor.py's list_switches() docstring).
  const [expandedSwitch, setExpandedSwitch] = useState('')

  const load = useCallback((refresh = false) => {
    setLoading(true)
    Promise.all([api.getFortigateSwitchOverview(refresh), api.getFortigateSwitches(refresh)])
      .then(([ov, sw]) => {
        setOverview(ov); setPageCache('fortiswitch-overview', ov)
        setSwitches(sw); setPageCache('fortiswitch-list', sw)
        setError('')
      })
      .catch(e => setError(e?.response?.data?.detail || 'Could not fetch FortiSwitch data'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load(false) }, [load])

  if (loading && !overview) return <div style={{ padding: 24, color: COLORS.textMuted }}>Loading FortiSwitch data…</div>
  if (error && !overview) return <div style={{ padding: 24, color: COLORS.danger }}>{error}</div>

  const allRows = switches?.switches || []
  const rows = search.trim()
    ? allRows.filter(r => `${r.switch_id} ${r.parent_device} ${r.serial}`.toLowerCase().includes(search.trim().toLowerCase()))
    : allRows
  const connectedPct = overview?.connected_pct

  return (
    <div>
      <InfoBanner text="Health dashboard — read-only, live data proxied through each parent FortiGate's FortiLink connection via the FortiManager Cloud API. A FortiSwitch's status reflects its real-time connection to its parent FortiGate." />

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr', gap: 16, alignItems: 'start', marginBottom: 20 }}>
        <div>
          <SectionBanner title="Overall Switch Health" color={BANNERS.fortiswitch} />
          <Panel>
            <GaugeRing
              pct={connectedPct ?? 0}
              ringColor={uptimeColor(connectedPct ?? 0)}
              stats={[
                { value: fmtNum(overview?.switch_count), label: 'Switches' },
                { value: fmtNum(overview?.connected_count), label: 'Connected' },
                { value: fmtNum(overview?.disconnected_count), label: 'Disconnected' },
              ]}
            />
          </Panel>
        </div>
        <div>
          <SectionBanner title="Switch Summary" color={BANNERS.fortiswitch} />
          <Panel>
            <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
              <KpiTile label="Connected" value={overview?.connected_count} bg={COLORS.success} />
              <KpiTile label="Disconnected" value={overview?.disconnected_count} bg={COLORS.danger} />
              <KpiTile label="Total Switches" value={overview?.switch_count} bg={BANNERS.fortiswitch} />
            </div>
          </Panel>
        </div>
      </div>

      <div>
        <SectionBanner title="Managed FortiSwitches" color={BANNERS.fortiswitch} />
        <Panel>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10, gap: 10, flexWrap: 'wrap' }}>
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search switches…"
              style={{ flex: '1 1 260px', maxWidth: 320, padding: '7px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 13 }} />
            <button onClick={() => load(true)} style={{ ...tabButtonStyle(false), padding: '6px 14px' }}>Refresh</button>
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
              <thead>
                <tr style={{ textAlign: 'left', color: COLORS.textMuted, borderBottom: `1px solid ${COLORS.border}` }}>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Switch ID</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Parent FortiGate</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Status</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>OS Version</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Serial</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Ports (Up/Down)</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>Joined</th>
                  <th style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}></th>
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 ? (
                  <tr><td colSpan={8} style={{ padding: '18px 10px', color: COLORS.textMuted, textAlign: 'center' }}>No managed FortiSwitches found.</td></tr>
                ) : rows.map(r => (
                  <Fragment key={r.switch_id}>
                    <tr style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                      <td style={{ padding: '6px 10px', fontWeight: 600 }}>{r.switch_id}</td>
                      <td style={{ padding: '6px 10px' }}>{r.parent_device}</td>
                      <td style={{ padding: '6px 10px' }}>
                        <span style={badgeStyle(r.connected ? COLORS.success : COLORS.danger)}>{r.connected ? '▲ Connected' : `▼ ${r.status || 'Disconnected'}`}</span>
                      </td>
                      <td style={{ padding: '6px 10px' }}>{r.os_version || '—'}</td>
                      <td style={{ padding: '6px 10px' }}>{r.serial || '—'}</td>
                      <td style={{ padding: '6px 10px' }}>{r.port_up_count} / {r.port_down_count}</td>
                      <td style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>{r.join_time || '—'}</td>
                      <td style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}>
                        <button onClick={() => setExpandedSwitch(prev => prev === r.switch_id ? '' : r.switch_id)}
                          style={{ border: 'none', background: 'none', color: COLORS.brand, fontSize: 12, fontWeight: 700, cursor: 'pointer', padding: '2px 4px' }}>
                          {expandedSwitch === r.switch_id ? 'Hide' : 'Ports/VLAN'}
                        </button>
                      </td>
                    </tr>
                    {expandedSwitch === r.switch_id && (
                      <tr>
                        <td colSpan={8} style={{ padding: 0 }}>
                          <FortiSwitchPortDetailPanel ports={r.ports || []} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </div>
  )
}

/** Per-port VLAN/status/speed/PoE table for one FortiSwitch — shown
 * inline when a switch's row is expanded. No separate fetch: the ports
 * array already arrived with the switch list itself (see
 * fortigate_monitor.py's list_switches() docstring for why FortiSwitch
 * needs no on-demand detail endpoint the way Fortigate Firewall's
 * CPU/Memory/Interfaces/VPN does). */
function FortiSwitchPortDetailPanel({ ports }) {
  if (!ports || ports.length === 0) {
    return <div style={{ padding: '14px 16px', color: COLORS.textMuted, fontSize: 12.5, background: '#fafafa', borderTop: `1px solid ${COLORS.border}` }}>No port data reported.</div>
  }
  return (
    <div style={{ padding: '14px 16px', background: '#fafafa', borderTop: `1px solid ${COLORS.border}` }}>
      <div style={{ overflowX: 'auto' }}>
        <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ textAlign: 'left', color: COLORS.textMuted }}>
              <th style={{ padding: '4px 8px' }}>Port</th>
              <th style={{ padding: '4px 8px' }}>VLAN</th>
              <th style={{ padding: '4px 8px' }}>Status</th>
              <th style={{ padding: '4px 8px' }}>Speed</th>
              <th style={{ padding: '4px 8px' }}>Duplex</th>
              <th style={{ padding: '4px 8px' }}>PoE</th>
            </tr>
          </thead>
          <tbody>
            {ports.map((p, i) => (
              <tr key={i} style={{ borderTop: `1px solid ${COLORS.border}` }}>
                <td style={{ padding: '4px 8px', fontWeight: 600 }}>{p.name || '—'}</td>
                <td style={{ padding: '4px 8px' }}>{p.vlan || '—'}</td>
                <td style={{ padding: '4px 8px' }}><span style={badgeStyle(p.status === 'up' ? COLORS.success : COLORS.textMuted)}>{p.status === 'up' ? 'Up' : 'Down'}</span></td>
                <td style={{ padding: '4px 8px' }}>{p.speed_mbps ? `${p.speed_mbps} Mbps` : '—'}</td>
                <td style={{ padding: '4px 8px' }}>{p.duplex || '—'}</td>
                <td style={{ padding: '4px 8px' }}>{p.poe_status || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── AP Monitor tab — a second-level container holding every Cisco Meraki
// Access Point topic as its own sub-tab bar (Overview / Access Points /
// AP Network Monitoring / Wireless Network metrics / Access Point
// metrics / Logs). This used to be the page's top-level tab bar, but per
// user request the page now has just two top-level tabs — "AP Monitor"
// and "Firewall" — with each vendor's own topic tabs nested one level
// deeper inside. ────────────────────────────────────────────────────────
const AP_MONITOR_SUB_TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'access_points', label: 'Access Points' },
  { id: 'ap_network_monitoring', label: 'AP Network Monitoring' },
  { id: 'wireless_network', label: 'Wireless Network metrics' },
  { id: 'access_point_util', label: 'Access Point metrics' },
  { id: 'logs', label: 'Logs' },
]

function ApMonitorTab() {
  const [activeSubTab, setActiveSubTab] = useState('overview')

  return (
    <div>
      <div style={{ ...TAB_BAR, marginBottom: 20 }}>
        {AP_MONITOR_SUB_TABS.map(tab => (
          <button key={tab.id} onClick={() => setActiveSubTab(tab.id)} style={tabButtonStyle(activeSubTab === tab.id)}>
            {tab.label}
          </button>
        ))}
      </div>

      {activeSubTab === 'overview' && <OverviewTab />}
      {activeSubTab === 'access_points' && <AccessPointsTab />}
      {activeSubTab === 'ap_network_monitoring' && <ApNetworkMonitoringTab />}
      {activeSubTab === 'wireless_network' && <WirelessNetworkTab />}
      {activeSubTab === 'access_point_util' && <AccessPointUtilTab />}
      {activeSubTab === 'logs' && <LogsTab />}
    </div>
  )
}

// ── Firewall tab — a second-level container, not a data source itself.
// Holds its own sub-tab bar for individual firewall vendors/products
// (currently just SonicWall, but structured so another vendor could be
// added as a sibling sub-tab later without touching the top-level TABS
// array again). ──────────────────────────────────────────────────────
const FIREWALL_SUB_TABS = [
  { id: 'sonicwall', label: 'SonicWall' },
  { id: 'fortigate', label: 'Fortigate Firewall' },
  { id: 'fortiswitch', label: 'FortiSwitch' },
]

function FirewallTab() {
  const [activeSubTab, setActiveSubTab] = useState('sonicwall')

  return (
    <div>
      <div style={{ ...TAB_BAR, marginBottom: 20 }}>
        {FIREWALL_SUB_TABS.map(tab => (
          <button key={tab.id} onClick={() => setActiveSubTab(tab.id)} style={tabButtonStyle(activeSubTab === tab.id, BANNERS[tab.id])}>
            {tab.label}
          </button>
        ))}
      </div>

      {activeSubTab === 'sonicwall' && <SonicWallTab />}
      {activeSubTab === 'fortigate' && <FortigateTab />}
      {activeSubTab === 'fortiswitch' && <FortiSwitchTab />}
    </div>
  )
}

// Top-level tabs — one per hardware category, each a container for its
// own vendor-topic sub-tabs (see ApMonitorTab/FirewallTab above).
const TABS = [
  { id: 'ap_monitor', label: 'AP' },
  { id: 'firewall', label: 'Firewall' },
]

export default function MerakiMonitorPage() {
  const [activeTab, setActiveTab] = useState('ap_monitor')

  return (
    <div>
      <div style={{ marginBottom: 18 }}>
        <h1 style={{ fontSize: 22, fontWeight: 800, color: COLORS.textPrimary, margin: 0 }}>Local Infra Hardware Monitor — Management Overview Health Dashboard</h1>
        <div style={{ fontSize: 13, color: COLORS.textMuted, marginTop: 4 }}>
          Cisco Meraki Access Point monitoring — read-only, live data from the Meraki Dashboard API.
        </div>
      </div>

      <div style={{ ...TAB_BAR, marginBottom: 20 }}>
        {TABS.map(tab => (
          <button key={tab.id} onClick={() => setActiveTab(tab.id)} style={tabButtonStyle(activeTab === tab.id)}>
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'ap_monitor' && <ApMonitorTab />}
      {activeTab === 'firewall' && <FirewallTab />}
    </div>
  )
}
