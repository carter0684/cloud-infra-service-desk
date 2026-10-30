import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import useOrg from '../utils/useOrg.js'
import {
  COLORS, CARD, Badge, InfoBanner, SkeletonLines, TAB_BAR, tabButtonStyle,
} from '../utils/theme.js'

// ── Security Team Portal ──────────────────────────────────────────────────
// Read-only view of the Security Team's "Security Monitoring Dashboard"
// (a separate app on AWS App Runner, NOT part of this codebase).
//
// Data comes through OUR backend (/api/security-portal/*, see
// backend/app/security_portal.py), which calls that app's X-API-Key
// /external-api/* surface server-side. The API key lives only in AWS
// Secrets Manager (rotatable from Admin > Connected Portal Credential
// Updates) and never reaches the browser, so there is no second login.
//
// Embedding the upstream UI in an <iframe> does not work (its login cookie
// is SameSite=Lax, so the session is dropped inside a cross-site frame),
// which is why this tab renders the data natively instead. The upstream
// dashboard is still one click away via the "Open Security Scan Portal"
// button for anything not shown here.
// The dashboard URL comes from the backend setting SECURITY_PORTAL_PUBLIC_URL (GET /api/org-config).

const SEVERITY_COLOR = {
  CRITICAL: '#7f1d1d', HIGH: '#dc2626', MEDIUM: '#d97706', LOW: '#2563eb',
  INFORMATIONAL: '#6b7280', INFO: '#6b7280',
}
const SEVERITY_ORDER = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFORMATIONAL: 4, INFO: 4 }
const severityKey = (s) => String(s || '').toUpperCase()
const severityRank = (s) => SEVERITY_ORDER[severityKey(s)] ?? 9

const STATE_COLOR = {
  OK: '#16a34a', ALARM: '#dc2626', INSUFFICIENT_DATA: '#d97706',
  COMPLETED: '#16a34a', FAILED: '#dc2626', EXPIRED: '#dc2626', RUNNING: '#2563eb', PARTIAL: '#d97706',
  AVAILABLE: '#16a34a', ISSUED: '#16a34a', RUNNING_: '#16a34a', STOPPED: '#6b7280',
  ONLINE: '#16a34a', CONNECTIONLOST: '#dc2626', INACTIVE: '#6b7280',
}
const stateColor = (s) => STATE_COLOR[String(s || '').toUpperCase().replace(/[-\s]/g, '_')]
  || STATE_COLOR[String(s || '').toUpperCase()] || '#6b7280'

const GRADE_COLOR = { A: '#16a34a', B: '#65a30d', C: '#d97706', D: '#ea580c', F: '#dc2626' }
const gradeColor = (g) => GRADE_COLOR[String(g || '').charAt(0).toUpperCase()] || '#6b7280'

const pctColor = (p) => (p >= 90 ? COLORS.danger : p >= 70 ? COLORS.warning : COLORS.success)

function fmtDate(v) {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return String(v)
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function fmtDay(v) {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return String(v)
  return d.toLocaleDateString(undefined, { dateStyle: 'medium' })
}

function errText(e) {
  return e?.response?.data?.detail || e?.message || 'Failed to load Security Team Portal data'
}

// ── data hook ─────────────────────────────────────────────────────────────
// Loads one allow-listed resource when `enabled` first becomes true, and
// again whenever reload() is called (reload(true) bypasses both caches).
function useResource(resource, enabled) {
  const [state, setState] = useState({ data: null, meta: null, loading: false, error: '' })
  const controller = useRef(null)

  const load = useCallback((refresh = false) => {
    controller.current?.abort()
    const ac = new AbortController()
    controller.current = ac
    setState(s => ({ ...s, loading: true, error: '' }))
    api.securityPortal(resource, refresh, ac.signal)
      .then(r => setState({
        data: r.data, meta: { cachedAt: r.cachedAt, fromCache: r.fromCache, age: r.cache_age_seconds },
        loading: false, error: '',
      }))
      .catch(e => {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
        setState(s => ({ ...s, loading: false, error: errText(e) }))
      })
  }, [resource])

  useEffect(() => {
    if (enabled && state.data === null && !state.loading && !state.error) load(false)
  }, [enabled]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => controller.current?.abort(), [])

  return { ...state, reload: load }
}

// ── small building blocks ─────────────────────────────────────────────────
function Stat({ label, value, color, sub }) {
  return (
    <div style={{ ...CARD, padding: '14px 18px', minWidth: 150, flex: '1 1 150px' }}>
      <div style={{ color: COLORS.textSecondary, fontSize: 12, fontWeight: 600 }}>{label}</div>
      <div style={{ color: color || COLORS.textPrimary, fontSize: 26, fontWeight: 700, marginTop: 2 }}>{value}</div>
      {sub ? <div style={{ color: COLORS.textMuted, fontSize: 11.5, marginTop: 2 }}>{sub}</div> : null}
    </div>
  )
}

function Section({ title, right, children }) {
  return (
    <div style={{ ...CARD, padding: '16px 18px', marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12, gap: 8 }}>
        <h3 style={{ margin: 0, fontSize: 15, color: COLORS.textPrimary }}>{title}</h3>
        {right}
      </div>
      {children}
    </div>
  )
}

function Freshness({ meta, onRefresh, loading }) {
  let text = ''
  if (meta?.cachedAt) {
    const secs = Math.max(0, Math.floor((Date.now() - meta.cachedAt) / 1000))
    text = secs < 60 ? 'Updated just now' : `Updated ${Math.floor(secs / 60)}m ago`
  } else if (meta) {
    text = 'Live'
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <span style={{ color: COLORS.textMuted, fontSize: 12 }}>{text}</span>
      <button
        type="button" onClick={onRefresh} disabled={loading}
        style={{
          padding: '5px 12px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff',
          color: COLORS.brand, fontWeight: 600, fontSize: 12.5, cursor: loading ? 'default' : 'pointer',
          opacity: loading ? 0.6 : 1,
        }}
      >
        {loading ? 'Refreshing…' : '↻ Refresh now'}
      </button>
    </div>
  )
}

function ErrorBox({ message, onRetry }) {
  return (
    <div role="alert" style={{
      background: COLORS.dangerBg, color: COLORS.danger, padding: '10px 14px', borderRadius: 8,
      fontSize: 13, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
    }}>
      <span>{message}</span>
      {onRetry ? <button type="button" onClick={onRetry} style={{
        border: `1px solid ${COLORS.danger}`, background: '#fff', color: COLORS.danger, borderRadius: 6,
        padding: '4px 10px', cursor: 'pointer', fontSize: 12.5, fontWeight: 600,
      }}>Retry</button> : null}
    </div>
  )
}

const TH = {
  textAlign: 'left', padding: '8px 10px', fontSize: 12, color: COLORS.textSecondary,
  borderBottom: `1px solid ${COLORS.border}`, whiteSpace: 'nowrap', background: '#f8fafc',
}
const TD = { padding: '8px 10px', fontSize: 13, color: COLORS.textPrimary, borderBottom: `1px solid ${COLORS.border}`, verticalAlign: 'top' }

const PAGE = 100

// Generic searchable table. columns: [{ key, label, render?(row), text?(row) }]
function DataTable({ rows, columns, searchPlaceholder = 'Search…', emptyText = 'No data', rowKey, renderExpanded, filters }) {
  const [q, setQ] = useState('')
  const [shown, setShown] = useState(PAGE)
  const [open, setOpen] = useState({})

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return rows.filter(r => {
      if (filters && !filters(r)) return false
      if (!needle) return true
      return columns.some(c => String((c.text ? c.text(r) : r[c.key]) ?? '').toLowerCase().includes(needle))
    })
  }, [rows, q, columns, filters])

  useEffect(() => { setShown(PAGE) }, [q, filters])

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 10, flexWrap: 'wrap' }}>
        <input
          type="search" value={q} onChange={e => setQ(e.target.value)} placeholder={searchPlaceholder}
          aria-label={searchPlaceholder}
          style={{ padding: '7px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 13, minWidth: 240 }}
        />
        <span style={{ color: COLORS.textMuted, fontSize: 12 }}>
          {filtered.length === rows.length ? `${rows.length} rows` : `${filtered.length} of ${rows.length} rows`}
        </span>
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {renderExpanded ? <th scope="col" style={{ ...TH, width: 28 }} /> : null}
              {columns.map(c => <th key={c.key} scope="col" style={TH}>{c.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {filtered.slice(0, shown).map((r, i) => {
              const id = rowKey ? rowKey(r) : i
              const isOpen = !!open[id]
              return (
                <React.Fragment key={id}>
                  <tr>
                    {renderExpanded ? (
                      <td style={TD}>
                        <button
                          type="button" aria-expanded={isOpen} aria-label={isOpen ? 'Collapse row' : 'Expand row'}
                          onClick={() => setOpen(o => ({ ...o, [id]: !o[id] }))}
                          style={{ border: 'none', background: 'none', cursor: 'pointer', color: COLORS.brand, fontSize: 13 }}
                        >{isOpen ? '▾' : '▸'}</button>
                      </td>
                    ) : null}
                    {columns.map(c => <td key={c.key} style={TD}>{c.render ? c.render(r) : (r[c.key] ?? '—')}</td>)}
                  </tr>
                  {renderExpanded && isOpen ? (
                    <tr><td colSpan={columns.length + 1} style={{ ...TD, background: '#f8fafc' }}>{renderExpanded(r)}</td></tr>
                  ) : null}
                </React.Fragment>
              )
            })}
            {filtered.length === 0 ? (
              <tr><td colSpan={columns.length + (renderExpanded ? 1 : 0)} style={{ ...TD, textAlign: 'center', color: COLORS.textMuted }}>{emptyText}</td></tr>
            ) : null}
          </tbody>
        </table>
      </div>
      {filtered.length > shown ? (
        <div style={{ textAlign: 'center', marginTop: 10 }}>
          <button type="button" onClick={() => setShown(n => n + PAGE)} style={{
            padding: '6px 16px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff',
            color: COLORS.brand, fontWeight: 600, fontSize: 12.5, cursor: 'pointer',
          }}>Show {Math.min(PAGE, filtered.length - shown)} more</button>
        </div>
      ) : null}
    </div>
  )
}

// Wraps a resource: loading skeleton, error + retry, then children(data).
function ResourcePane({ res, title, children }) {
  return (
    <Section title={title} right={<Freshness meta={res.meta} loading={res.loading} onRefresh={() => res.reload(true)} />}>
      {res.error ? <ErrorBox message={res.error} onRetry={() => res.reload(false)} /> : null}
      {!res.error && res.data === null ? <SkeletonLines lines={6} /> : null}
      {res.data !== null ? children(res.data) : null}
    </Section>
  )
}

// ── tab panes ─────────────────────────────────────────────────────────────
function OverviewPane({ active }) {
  const hub = useResource('securityhub/report', active)
  const ssc = useResource('securityscorecard/report', active)
  const alarms = useResource('aws/alarms', active)
  const certs = useResource('aws/certificates', active)

  return (
    <div>
      <Section title="AWS Security Hub" right={<Freshness meta={hub.meta} loading={hub.loading} onRefresh={() => hub.reload(true)} />}>
        {hub.error ? <ErrorBox message={hub.error} onRetry={() => hub.reload(false)} /> : null}
        {!hub.error && hub.data === null ? <SkeletonLines lines={2} /> : null}
        {hub.data ? (
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <Stat label="Security score" value={`${hub.data.score?.scorePercent ?? '—'}%`} color={pctColor(100 - (hub.data.score?.scorePercent ?? 0))} sub={`Account ${hub.data.score?.accountId ?? '—'}`} />
            <Stat label="Controls passed" value={hub.data.score?.passed ?? '—'} color={COLORS.success} sub={`of ${hub.data.score?.total ?? '—'}`} />
            <Stat label="Controls failed" value={hub.data.score?.failed ?? '—'} color={COLORS.danger} />
            <Stat label="Failed findings" value={(hub.data.failedFindings || []).length} sub={hub.data.asOf ? `As of ${hub.data.asOf}` : undefined} />
          </div>
        ) : null}
      </Section>

      <Section title="SecurityScorecard" right={<Freshness meta={ssc.meta} loading={ssc.loading} onRefresh={() => ssc.reload(true)} />}>
        {ssc.error ? <ErrorBox message={ssc.error} onRetry={() => ssc.reload(false)} /> : null}
        {!ssc.error && ssc.data === null ? <SkeletonLines lines={4} /> : null}
        {ssc.data ? (
          <>
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
              {(ssc.data.scoreboards || []).map(s => (
                <div key={s.domain} style={{ ...CARD, padding: '12px 16px', minWidth: 190, flex: '1 1 190px' }}>
                  <div style={{ color: COLORS.textSecondary, fontSize: 12, fontWeight: 600 }}>{s.label || s.domain}</div>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 2 }}>
                    <span style={{ fontSize: 28, fontWeight: 700, color: gradeColor(s.grade) }}>{s.grade}</span>
                    <span style={{ fontSize: 15, color: COLORS.textPrimary }}>{s.score}</span>
                  </div>
                  <div style={{ color: COLORS.textMuted, fontSize: 11.5 }}>{s.domain}</div>
                  {s.lastChange ? (
                    <div style={{ color: COLORS.textMuted, fontSize: 11.5 }}>Last change: {typeof s.lastChange === 'object' ? JSON.stringify(s.lastChange) : String(s.lastChange)}</div>
                  ) : null}
                </div>
              ))}
            </div>
            <h4 style={{ margin: '0 0 8px', fontSize: 13.5, color: COLORS.textPrimary }}>Top issues</h4>
            <DataTable
              rows={ssc.data.topIssues || []}
              searchPlaceholder="Search issues…"
              emptyText="No issues reported"
              columns={[
                { key: 'domain', label: 'Domain' },
                { key: 'issueName', label: 'Issue' },
                { key: 'severity', label: 'Severity', render: r => <Badge text={r.severity} color={SEVERITY_COLOR[severityKey(r.severity)] || '#6b7280'} /> },
                { key: 'count', label: 'Count' },
                { key: 'affectedScore', label: 'Score impact' },
              ]}
            />
          </>
        ) : null}
      </Section>

      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: '1 1 320px' }}>
          <Section title="CloudWatch alarms" right={<Freshness meta={alarms.meta} loading={alarms.loading} onRefresh={() => alarms.reload(true)} />}>
            {alarms.error ? <ErrorBox message={alarms.error} onRetry={() => alarms.reload(false)} /> : null}
            {!alarms.error && alarms.data === null ? <SkeletonLines lines={2} /> : null}
            {alarms.data ? (
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                {Object.entries(alarms.data.summary || {}).map(([k, v]) => (
                  <Stat key={k} label={k.replace(/_/g, ' ')} value={v} color={stateColor(k)} />
                ))}
              </div>
            ) : null}
          </Section>
        </div>
        <div style={{ flex: '1 1 320px' }}>
          <Section title="Certificates" right={<Freshness meta={certs.meta} loading={certs.loading} onRefresh={() => certs.reload(true)} />}>
            {certs.error ? <ErrorBox message={certs.error} onRetry={() => certs.reload(false)} /> : null}
            {!certs.error && certs.data === null ? <SkeletonLines lines={2} /> : null}
            {certs.data ? (
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                <Stat label="Total" value={certs.data.length} />
                <Stat label="Expiring soon" value={certs.data.filter(c => c.expiringSoon).length}
                  color={certs.data.some(c => c.expiringSoon) ? COLORS.warning : COLORS.success} />
              </div>
            ) : null}
          </Section>
        </div>
      </div>
    </div>
  )
}

function SecurityHubPane({ active }) {
  const res = useResource('securityhub/report', active)
  const [sev, setSev] = useState('ALL')
  const filters = useCallback((r) => sev === 'ALL' || severityKey(r.severity) === sev, [sev])

  return (
    <ResourcePane res={res} title="Failed Security Hub findings">
      {(d) => {
        const rows = [...(d.failedFindings || [])].sort((a, b) => severityRank(a.severity) - severityRank(b.severity))
        const counts = rows.reduce((m, r) => { const k = severityKey(r.severity); m[k] = (m[k] || 0) + 1; return m }, {})
        const sevs = Object.keys(counts).sort((a, b) => severityRank(a) - severityRank(b))
        return (
          <>
            <div role="group" aria-label="Filter by severity" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
              {['ALL', ...sevs].map(s => (
                <button key={s} type="button" aria-pressed={sev === s} onClick={() => setSev(s)} style={{
                  padding: '4px 12px', borderRadius: 14, cursor: 'pointer', fontSize: 12.5, fontWeight: 600,
                  border: `1px solid ${sev === s ? COLORS.brand : COLORS.border}`,
                  background: sev === s ? COLORS.brandLight : '#fff', color: sev === s ? COLORS.brand : COLORS.textSecondary,
                }}>{s === 'ALL' ? `All (${rows.length})` : `${s} (${counts[s]})`}</button>
              ))}
            </div>
            <DataTable
              rows={rows} filters={filters} searchPlaceholder="Search control, title, resource…"
              emptyText="No failed findings"
              rowKey={(r) => `${r.controlId}|${r.resourceId}|${r.region}`}
              columns={[
                { key: 'severity', label: 'Severity', render: r => <Badge text={r.severity} color={SEVERITY_COLOR[severityKey(r.severity)] || '#6b7280'} /> },
                { key: 'controlId', label: 'Control' },
                { key: 'title', label: 'Title' },
                { key: 'resourceType', label: 'Resource type' },
                { key: 'resourceId', label: 'Resource', render: r => <span style={{ wordBreak: 'break-all' }}>{r.resourceId}</span> },
                { key: 'region', label: 'Region' },
                { key: 'standard', label: 'Standard' },
                { key: 'updatedAt', label: 'Updated', text: r => fmtDate(r.updatedAt), render: r => fmtDate(r.updatedAt) },
              ]}
            />
          </>
        )
      }}
    </ResourcePane>
  )
}

function AlarmsPane({ active }) {
  const res = useResource('aws/alarms', active)
  return (
    <ResourcePane res={res} title="CloudWatch alarms">
      {(d) => (
        <>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 12 }}>
            {Object.entries(d.summary || {}).map(([k, v]) => <Stat key={k} label={k.replace(/_/g, ' ')} value={v} color={stateColor(k)} />)}
          </div>
          <DataTable
            rows={[...(d.alarms || [])].sort((a, b) => (a.state === 'ALARM' ? -1 : 0) - (b.state === 'ALARM' ? -1 : 0))}
            searchPlaceholder="Search alarms…" emptyText="No alarms"
            rowKey={(r) => `${r.region}|${r.name}`}
            columns={[
              { key: 'name', label: 'Alarm' },
              { key: 'state', label: 'State', render: r => <Badge text={r.state} color={stateColor(r.state)} /> },
              { key: 'metric', label: 'Metric' },
              { key: 'region', label: 'Region' },
              { key: 'updatedAt', label: 'Updated', text: r => fmtDate(r.updatedAt), render: r => fmtDate(r.updatedAt) },
            ]}
          />
        </>
      )}
    </ResourcePane>
  )
}

function CertificatesPane({ active }) {
  const res = useResource('aws/certificates', active)
  return (
    <ResourcePane res={res} title="ACM certificates">
      {(rows) => (
        <DataTable
          rows={[...rows].sort((a, b) => (a.daysToExpiry ?? 1e9) - (b.daysToExpiry ?? 1e9))}
          searchPlaceholder="Search certificates…" emptyText="No certificates"
          rowKey={(r) => `${r.domain}|${r.notAfter}`}
          columns={[
            { key: 'domain', label: 'Domain' },
            { key: 'status', label: 'Status', render: r => <Badge text={r.status} color={stateColor(r.status)} /> },
            { key: 'type', label: 'Type' },
            { key: 'inUse', label: 'In use', text: r => (r.inUse ? 'yes' : 'no'), render: r => (r.inUse ? 'Yes' : 'No') },
            { key: 'notAfter', label: 'Expires', text: r => fmtDay(r.notAfter), render: r => fmtDay(r.notAfter) },
            {
              key: 'daysToExpiry', label: 'Days left',
              render: r => <span style={{ fontWeight: 700, color: r.expiringSoon ? COLORS.warning : COLORS.textPrimary }}>
                {r.daysToExpiry}{r.expiringSoon ? ' ⚠️ expiring soon' : ''}
              </span>,
            },
          ]}
        />
      )}
    </ResourcePane>
  )
}

function Ec2Pane({ active }) {
  const res = useResource('aws/ec2', active)
  return (
    <ResourcePane res={res} title="EC2 instances">
      {(rows) => (
        <DataTable
          rows={rows} searchPlaceholder="Search instances…" emptyText="No instances" rowKey={(r) => r.id}
          columns={[
            { key: 'name', label: 'Name' },
            { key: 'id', label: 'Instance ID' },
            { key: 'state', label: 'State', render: r => <Badge text={r.state} color={stateColor(r.state)} /> },
            { key: 'type', label: 'Type' },
            { key: 'az', label: 'AZ' },
            { key: 'privateIp', label: 'Private IP' },
            { key: 'launchTime', label: 'Launched', text: r => fmtDay(r.launchTime), render: r => fmtDay(r.launchTime) },
          ]}
        />
      )}
    </ResourcePane>
  )
}

function UtilizationPane({ active }) {
  const res = useResource('aws/utilization', active)
  const pct = (v) => (v === null || v === undefined
    ? '—'
    : <span style={{ fontWeight: 700, color: pctColor(v) }}>{v}%</span>)
  return (
    <ResourcePane res={res} title="Instance utilization">
      {(rows) => (
        <DataTable
          rows={rows} searchPlaceholder="Search instances…" emptyText="No data"
          rowKey={(r) => `${r.instanceId}|${r.volumeId}`}
          columns={[
            { key: 'instanceName', label: 'Instance' },
            { key: 'instanceId', label: 'Instance ID' },
            { key: 'region', label: 'Region' },
            { key: 'volumeId', label: 'Volume' },
            { key: 'totalSizeGiB', label: 'Size (GiB)' },
            { key: 'usedStoragePercent', label: 'Disk used', render: r => pct(r.usedStoragePercent) },
            { key: 'cpuPercent', label: 'CPU', render: r => pct(r.cpuPercent) },
            { key: 'memoryPercent', label: 'Memory', render: r => pct(r.memoryPercent) },
          ]}
        />
      )}
    </ResourcePane>
  )
}

function BackupsPane({ active }) {
  const res = useResource('aws/backups', active)
  return (
    <ResourcePane res={res} title="AWS Backup jobs">
      {(rows) => (
        <DataTable
          rows={rows} searchPlaceholder="Search backups…" emptyText="No backup jobs"
          rowKey={(r) => `${r.resourceArn}|${r.creationDate}`}
          columns={[
            { key: 'resourceType', label: 'Type' },
            { key: 'resourceArn', label: 'Resource', render: r => <span style={{ wordBreak: 'break-all' }}>{r.resourceArn}</span> },
            { key: 'state', label: 'State', render: r => <Badge text={r.state} color={stateColor(r.state)} /> },
            { key: 'vault', label: 'Vault' },
            { key: 'creationDate', label: 'Started', text: r => fmtDate(r.creationDate), render: r => fmtDate(r.creationDate) },
            { key: 'completionDate', label: 'Completed', text: r => fmtDate(r.completionDate), render: r => fmtDate(r.completionDate) },
          ]}
        />
      )}
    </ResourcePane>
  )
}

function BackupImagesPane({ active }) {
  const res = useResource('aws/backup-images', active)
  return (
    <ResourcePane res={res} title="Backup AMIs">
      {(rows) => (
        <DataTable
          rows={[...rows].sort((a, b) => String(b.creationDate).localeCompare(String(a.creationDate)))}
          searchPlaceholder="Search instance, AMI…" emptyText="No backup images"
          rowKey={(r) => r.amiId}
          columns={[
            { key: 'instanceName', label: 'Instance' },
            { key: 'instanceId', label: 'Instance ID' },
            { key: 'amiId', label: 'AMI ID' },
            { key: 'region', label: 'Region' },
            { key: 'status', label: 'Status', render: r => <Badge text={r.status} color={stateColor(r.status)} /> },
            { key: 'creationDate', label: 'Created', text: r => fmtDate(r.creationDate), render: r => fmtDate(r.creationDate) },
          ]}
        />
      )}
    </ResourcePane>
  )
}

function InventoryPane({ active }) {
  const res = useResource('aws/inventory', active)
  return (
    <ResourcePane res={res} title="Software inventory (SSM)">
      {(rows) => (
        <DataTable
          rows={rows} searchPlaceholder="Search instance, platform…" emptyText="No inventory"
          rowKey={(r) => r.id}
          columns={[
            { key: 'name', label: 'Name' },
            { key: 'id', label: 'Instance ID' },
            { key: 'region', label: 'Region' },
            { key: 'pingStatus', label: 'SSM agent', render: r => <Badge text={r.pingStatus} color={stateColor(r.pingStatus)} /> },
            { key: 'platform', label: 'Platform' },
            { key: 'agentVersion', label: 'Agent version' },
            { key: 'applicationCount', label: 'Applications' },
          ]}
          renderExpanded={(r) => (
            (r.applications || []).length === 0
              ? <span style={{ color: COLORS.textMuted, fontSize: 13 }}>No application inventory reported.</span>
              : <DataTable
                  rows={r.applications} searchPlaceholder="Search applications…" emptyText="No applications"
                  rowKey={(a) => `${a.name}|${a.version}`}
                  columns={[
                    { key: 'name', label: 'Application' },
                    { key: 'version', label: 'Version' },
                    { key: 'publisher', label: 'Publisher' },
                  ]}
                />
          )}
        />
      )}
    </ResourcePane>
  )
}

const TABS = [
  { id: 'overview', label: 'Overview', Pane: OverviewPane },
  { id: 'securityhub', label: 'Security Hub findings', Pane: SecurityHubPane },
  { id: 'alarms', label: 'Alarms', Pane: AlarmsPane },
  { id: 'certificates', label: 'Certificates', Pane: CertificatesPane },
  { id: 'ec2', label: 'EC2', Pane: Ec2Pane },
  { id: 'utilization', label: 'Utilization', Pane: UtilizationPane },
  { id: 'backups', label: 'Backups', Pane: BackupsPane },
  { id: 'backup-images', label: 'Backup AMIs', Pane: BackupImagesPane },
  { id: 'inventory', label: 'Inventory', Pane: InventoryPane },
]

export default function SecurityTeamPortalPage() {
  const { securityPortalUrl } = useOrg()
  const [tab, setTab] = useState('overview')
  // Panes stay mounted once visited so switching tabs doesn't refetch or
  // lose their search/filter state.
  const [visited, setVisited] = useState({ overview: true })
  const select = (id) => { setTab(id); setVisited(v => ({ ...v, [id]: true })) }

  return (
    <div style={{ padding: '16px 20px 24px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>🛡️ Security Team Portal</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            AWS Security Hub, SecurityScorecard and AWS asset data from the Security Team's monitoring dashboard.
          </div>
        </div>
        <a
          href={securityPortalUrl} target="_blank" rel="noopener noreferrer"
          style={{
            display: 'inline-block', padding: '8px 16px', borderRadius: 7, background: COLORS.brand,
            color: '#fff', fontWeight: 600, fontSize: 13, textDecoration: 'none',
          }}
        >
          Open Security Scan Portal ↗
        </a>
      </div>

      <InfoBanner text="Read-only view. Data is fetched from the Security Team's portal and cached for up to 10 minutes; use “Refresh now” for a live pull." />

      <div role="tablist" aria-label="Security Team Portal sections" style={{ ...TAB_BAR, marginBottom: 16 }}>
        {TABS.map(t => (
          <button
            key={t.id} type="button" role="tab" aria-selected={tab === t.id}
            id={`stp-tab-${t.id}`} aria-controls={`stp-pane-${t.id}`}
            onClick={() => select(t.id)} style={tabButtonStyle(tab === t.id)}
          >{t.label}</button>
        ))}
      </div>

      {TABS.map(({ id, Pane }) => visited[id] ? (
        <div
          key={id} role="tabpanel" id={`stp-pane-${id}`} aria-labelledby={`stp-tab-${id}`}
          hidden={tab !== id}
        >
          <Pane active={tab === id} />
        </div>
      ) : null)}
    </div>
  )
}
