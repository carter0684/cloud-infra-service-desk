import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../api.js'
import { COLORS, CARD, TAB_BAR, tabButtonStyle } from '../utils/theme.js'
import { LineChart } from '../components/SimpleCharts.jsx'
import { getPageCache, setPageCache } from '../utils/pageCache.js'

const CACHE_KEY = 'waf-monitoring-summary'

// ── AWS WAF Rule Monitoring ────────────────────────────────────────────────
// Every WAFv2 Web ACL (REGIONAL + CLOUDFRONT scope) across every AWS
// account (same superset Billing by Service / Security Hub use), fetched
// via the same cross-account AssumeRole(PortalReadOnlyRole) pattern as
// those tabs.
//
// Two sub-tabs:
//  - "Web ACLs by Account" — the structural view (Web ACLs, linked
//    resources, rule list). NO CloudWatch calls at all, ever — this is
//    the cheapest possible view and what loads by default.
//  - "By Action" — pick one Web ACL and see totals for every action
//    (Block/Allow/Count/CAPTCHA/Challenge) over a selected time range,
//    each broken down by which rule(s) caused it. Fetched only once a
//    Web ACL is explicitly selected on this tab — never eagerly.
//
// Expanding a rule on the "Web ACLs by Account" tab still surfaces its
// own metrics chart + live request samples (unchanged from before) —
// that per-rule drill-down lives there since it's scoped to a single
// rule the user already found via the account/Web-ACL tree, while "By
// Action" is the account-agnostic-within-one-ACL, action-first view.

const TIME_RANGES = [
  { value: '1h', label: '1h' },
  { value: '3h', label: '3h' },
  { value: '12h', label: '12h' },
  { value: '1d', label: '1d' },
  { value: '3d', label: '3d' },
  { value: '1w', label: '1w' },
]
const DEFAULT_TIME_RANGE = '3h'

const METRIC_LINES = [
  { key: 'blocked', label: 'Blocked requests', color: COLORS.danger },
  { key: 'counted', label: 'Counted requests', color: COLORS.warning },
  { key: 'allowed', label: 'Allowed requests', color: COLORS.success },
]

// Auto-refresh intervals for the on-demand Metrics charts, mirroring the
// CloudWatch console's own "Auto refresh" dropdown (Off / 10s / 1m / 2m /
// 5m / 15m). Off (0) is the default — a chart only ever re-polls
// CloudWatch when a user explicitly turns this on for that specific
// chart, and polling stops the instant the chart is collapsed/unmounted
// (see MetricsChart's cleanup), so this never becomes a silent
// background cost the way it would if it defaulted to "on".
const REFRESH_INTERVALS = [
  { value: 0, label: 'Off' },
  { value: 10, label: '10s' },
  { value: 60, label: '1m' },
  { value: 120, label: '2m' },
  { value: 300, label: '5m' },
  { value: 900, label: '15m' },
]
const DEFAULT_REFRESH_INTERVAL = 0 // Off

// Order + display labels for the "By Action" tab's action picker —
// matches the snake_case action keys accepted/returned by
// get_account_action_breakdown() (mapped 1:1 to AWS/WAFV2 CloudWatch
// metric names on the backend).
const ACTION_KEYS = ['blocked', 'allowed', 'counted', 'captcha', 'challenge']
const ACTION_LABEL = { blocked: 'Block', allowed: 'Allow', counted: 'Count', captcha: 'CAPTCHA', challenge: 'Challenge' }
const ACTION_COLOR = {
  blocked: COLORS.danger, allowed: COLORS.success, counted: COLORS.warning,
  captcha: COLORS.info, challenge: '#7c3aed',
}
// Uppercase-AWS-style action name (matches GetSampledRequests' own
// `action` field values) -> the same color mapping, for the per-sample
// action pill in the rule drill-down table.
const SAMPLE_ACTION_COLOR = { BLOCK: COLORS.danger, ALLOW: COLORS.success, COUNT: COLORS.warning, CAPTCHA: COLORS.info, CHALLENGE: '#7c3aed' }

function summaryCacheKey(accountIds) {
  return `${CACHE_KEY}:${JSON.stringify([...accountIds].sort())}`
}

function formatAge(seconds) {
  if (!seconds || seconds < 60) return 'just now'
  const mins = Math.floor(seconds / 60)
  if (mins < 60) return `${mins} min${mins === 1 ? '' : 's'} ago`
  const hours = Math.floor(mins / 60)
  return `${hours} hour${hours === 1 ? '' : 's'} ago`
}

function fmtNum(n) {
  return (n ?? 0).toLocaleString()
}

const SCOPE_LABEL = { REGIONAL: 'Regional (ALB / API GW / AppSync)', CLOUDFRONT: 'CloudFront' }

// ── Shared time-range picker (1h/3h/12h/1d/3d/1w) ──────────────────────────
function TimeRangePicker({ value, onChange, disabled }) {
  return (
    <div style={{ display: 'flex', gap: 4 }}>
      {TIME_RANGES.map(tr => (
        <button key={tr.value} disabled={disabled} onClick={() => onChange(tr.value)}
          style={{
            padding: '4px 10px', borderRadius: 5, fontSize: 11.5, fontWeight: 700, cursor: disabled ? 'default' : 'pointer',
            border: `1px solid ${value === tr.value ? COLORS.brand : COLORS.border}`,
            background: value === tr.value ? COLORS.brand : '#fff',
            color: value === tr.value ? '#fff' : COLORS.textSecondary,
          }}>
          {tr.label}
        </button>
      ))}
    </div>
  )
}

// ── Auto-refresh interval picker — CloudWatch-console-style "Off / 10s /
// 1m / 2m / 5m / 15m" dropdown. Off by default; only this specific
// chart re-polls CloudWatch while it's open AND a non-Off interval is
// selected. ─────────────────────────────────────────────────────────────
function RefreshIntervalPicker({ value, onChange }) {
  return (
    <select value={value} onChange={e => onChange(Number(e.target.value))}
      title="Auto refresh"
      style={{
        padding: '4px 8px', borderRadius: 5, fontSize: 11.5, fontWeight: 700, cursor: 'pointer',
        border: `1px solid ${value > 0 ? COLORS.brand : COLORS.border}`,
        background: '#fff', color: value > 0 ? COLORS.brand : COLORS.textSecondary,
      }}>
      {REFRESH_INTERVALS.map(ri => (
        <option key={ri.value} value={ri.value}>
          {ri.value === 0 ? '↻ Auto-refresh: Off' : `↻ Every ${ri.label}`}
        </option>
      ))}
    </select>
  )
}

function ActionPill({ action }) {
  const color = SAMPLE_ACTION_COLOR[action] || COLORS.textMuted
  return (
    <span style={{
      fontSize: 10.5, fontWeight: 700, padding: '2px 8px', borderRadius: 10,
      background: color + '18', color, whiteSpace: 'nowrap',
    }}>
      {action || '—'}
    </span>
  )
}

// ── Live sampled-requests drill-down (per rule) — heaviest call.
// Automatically fetched as soon as a rule row is expanded (no extra
// click), showing EVERY action WAF recorded (Block/Allow/Count/
// CAPTCHA/Challenge), not filtered down to blocked-only. AWS's
// GetSampledRequests only ever covers roughly the last 3 hours — that's
// a hard AWS API limitation, not something this panel's own time-range
// picker can extend (this panel doesn't have a time-range picker for
// that reason; the metrics chart above it does, but this always reads
// the same fixed last-3h window). ───────────────────────────────────────
function SampledRequestsPanel({ accountId, webAcl, rule }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    api.getWafMonitoringSampledRequests(accountId, webAcl.name, webAcl.id, webAcl.scope, rule.name)
      .then(d => { if (!cancelled) setData(d) })
      .catch(e => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not fetch sampled requests') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, webAcl.id, rule.name])

  if (loading) return <div style={{ padding: '10px 0', color: COLORS.textMuted, fontSize: 12.5 }}>Loading recent samples…</div>
  if (error) return <div style={{ padding: '10px 0', color: COLORS.danger, fontSize: 12.5 }}>{error}</div>
  if (!data) return null

  const samples = data.samples || []

  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ fontSize: 11, color: COLORS.textMuted, marginBottom: 8 }}>
        Live sample — every action (Block/Allow/Count/CAPTCHA/Challenge), last {data.window_hours}h only
        (AWS GetSampledRequests hard limit — cannot be extended to "any time") · population size {fmtNum(data.population_size)}
        {data.is_mock && ' (local mock data)'}
      </div>
      {samples.length === 0 ? (
        <div style={{ fontSize: 12, color: COLORS.textMuted }}>No requests sampled for this rule in the last {data.window_hours}h.</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
            <thead>
              <tr style={{ textAlign: 'left', color: COLORS.textMuted, borderBottom: `1px solid ${COLORS.border}` }}>
                <th style={{ padding: '4px 8px' }}>Time</th>
                <th style={{ padding: '4px 8px' }}>Action</th>
                <th style={{ padding: '4px 8px' }}>Client IP</th>
                <th style={{ padding: '4px 8px' }}>Country</th>
                <th style={{ padding: '4px 8px' }}>Method</th>
                <th style={{ padding: '4px 8px' }}>URI</th>
                <th style={{ padding: '4px 8px' }}>Matched sub-rule</th>
              </tr>
            </thead>
            <tbody>
              {samples.map((s, i) => (
                <tr key={i} style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                  <td style={{ padding: '4px 8px', color: COLORS.textMuted, whiteSpace: 'nowrap' }}>{s.timestamp}</td>
                  <td style={{ padding: '4px 8px' }}><ActionPill action={s.action} /></td>
                  <td style={{ padding: '4px 8px', fontFamily: 'monospace' }}>{s.client_ip}</td>
                  <td style={{ padding: '4px 8px' }}>{s.country || '—'}</td>
                  <td style={{ padding: '4px 8px' }}>{s.method}</td>
                  <td style={{ padding: '4px 8px', maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis' }} title={s.uri}>{s.uri}</td>
                  <td style={{ padding: '4px 8px', color: COLORS.textMuted }}>{s.rule_name_within_rule_group || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Metrics chart shared by the per-rule drill-down view on the "Web
// ACLs by Account" tab. `fetcher(timeRange)` is provided by the caller
// so this component stays agnostic to which on-demand endpoint it's
// calling. Fetches ONLY on mount and whenever timeRange changes — i.e.
// only while its parent is actually rendering it (which itself only
// happens after an explicit user click to expand a rule). ─────────────
function MetricsChart({ fetcher, cacheDeps, footer }) {
  const [timeRange, setTimeRange] = useState(DEFAULT_TIME_RANGE)
  const [refreshInterval, setRefreshInterval] = useState(DEFAULT_REFRESH_INTERVAL)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const [lastUpdated, setLastUpdated] = useState(null)

  const fetch = useCallback((isBackgroundRefresh) => {
    let cancelled = false
    if (isBackgroundRefresh) setRefreshing(true)
    else { setLoading(true); setError('') }
    fetcher(timeRange)
      .then(d => {
        if (cancelled) return
        setData(d)
        setLastUpdated(new Date())
        if (isBackgroundRefresh) setError('') // a successful background tick clears a previous transient error
      })
      .catch(e => {
        if (cancelled) return
        // A failed background auto-refresh tick keeps showing the last
        // good chart (silently) rather than blanking the whole chart —
        // only a failed initial/manual fetch shows the error state.
        if (!isBackgroundRefresh) setError(e?.response?.data?.detail || 'Could not fetch metrics')
      })
      .finally(() => { if (!cancelled) { setLoading(false); setRefreshing(false) } })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timeRange, ...cacheDeps])

  // Initial load + reload whenever the time range (or underlying rule
  // identity) changes.
  useEffect(() => {
    const cancel = fetch(false)
    return cancel
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timeRange, ...cacheDeps])

  // Auto-refresh — CloudWatch-console-style polling. Only runs while
  // this specific chart is mounted (i.e. its parent rule row is still
  // expanded) AND the user has picked a non-Off interval. Stops
  // immediately (interval cleared) the moment the chart is
  // collapsed/unmounted, the interval is set back to Off, or the time
  // range changes (restarts on the fresh timeRange instead).
  useEffect(() => {
    if (!refreshInterval) return undefined
    const id = setInterval(() => { fetch(true) }, refreshInterval * 1000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshInterval, timeRange, ...cacheDeps])

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10, flexWrap: 'wrap', gap: 8 }}>
        <div style={{ fontSize: 11, color: COLORS.textMuted }}>
          {data && !data.is_mock && <>CloudWatch AWS/WAFV2 metrics — period {Math.round(data.period_seconds / 60)}m buckets</>}
          {data?.is_mock && <>(local mock data — no real AWS calls in dev mode)</>}
          {lastUpdated && <> · updated {lastUpdated.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</>}
          {refreshing && <> · refreshing…</>}
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <TimeRangePicker value={timeRange} onChange={setTimeRange} disabled={loading} />
          <RefreshIntervalPicker value={refreshInterval} onChange={setRefreshInterval} />
        </div>
      </div>
      {loading && <div style={{ padding: '20px 0', color: COLORS.textMuted, fontSize: 12.5, textAlign: 'center' }}>Loading metrics…</div>}
      {!loading && error && <div style={{ padding: '10px 0', color: COLORS.danger, fontSize: 12.5 }}>{error}</div>}
      {!loading && !error && data && (
        <>
          <LineChart series={data.series} lines={METRIC_LINES} />
          {footer}
        </>
      )}
    </div>
  )
}

// ── One rule row — structural info always visible; expanding it fetches
// BOTH the rule's own metrics chart AND its live request samples (every
// action, not just blocked) in one go — no extra nested click. Expanding
// a rule is itself the explicit user action that authorizes both calls,
// consistent with "only call when user clicked for detail". ───────────
function RuleRow({ accountId, webAcl, rule }) {
  const [open, setOpen] = useState(false)

  const fetcher = useCallback(
    timeRange => api.getWafMonitoringRuleMetrics(accountId, webAcl.name, webAcl.id, webAcl.scope, rule.name, timeRange),
    [accountId, webAcl.id, webAcl.name, webAcl.scope, rule.name],
  )

  return (
    <div style={{ borderBottom: `1px solid ${COLORS.border}` }}>
      <div onClick={() => setOpen(o => !o)}
        style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 14px', cursor: 'pointer', flexWrap: 'wrap' }}>
        <span style={{ color: COLORS.textMuted, fontSize: 11, width: 14 }}>{open ? '▾' : '▸'}</span>
        <span style={{ fontSize: 11, color: COLORS.textMuted, width: 26, textAlign: 'right' }}>#{rule.priority}</span>
        <span style={{ fontSize: 13, color: COLORS.textPrimary, fontWeight: 600, minWidth: 220 }}>{rule.name}</span>
        <span style={{ fontSize: 11, color: COLORS.textMuted }}>{rule.type}</span>
        <span style={{ fontSize: 11, color: COLORS.textMuted, fontFamily: 'monospace' }}>{rule.action}</span>
        <span style={{ marginLeft: 'auto', fontSize: 11.5, color: COLORS.brand, fontWeight: 600 }}>
          {open ? 'Hide metrics & samples' : '📊 View metrics & samples'}
        </span>
      </div>
      {open && (
        <div style={{ padding: '4px 14px 14px 38px' }}>
          <MetricsChart
            fetcher={fetcher}
            cacheDeps={[accountId, webAcl.id, rule.name]}
            footer={
              <div style={{ marginTop: 14, paddingTop: 12, borderTop: `1px solid ${COLORS.border}` }}>
                <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 6 }}>
                  Live Request Samples
                </div>
                <SampledRequestsPanel accountId={accountId} webAcl={webAcl} rule={rule} />
              </div>
            }
          />
        </div>
      )}
    </div>
  )
}

// ── One Web ACL card — PURELY STRUCTURAL (resources + rule list). No
// metrics chart at the Web-ACL level anymore — that view moved to the
// "By Action" tab. Expanding a rule still drills into its own metrics +
// samples (see RuleRow above), since that's scoped to one rule the user
// found by browsing the account/Web-ACL tree, not an eager Web-ACL-wide
// chart. ─────────────────────────────────────────────────────────────
function WebAclCard({ accountId, webAcl }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div style={{ ...CARD, marginBottom: 12, overflow: 'hidden' }}>
      <div onClick={() => setExpanded(e => !e)}
        style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px', background: '#f8fafc', borderBottom: expanded ? `1px solid ${COLORS.border}` : 'none', cursor: 'pointer', flexWrap: 'wrap' }}>
        <span style={{ color: COLORS.textMuted, fontSize: 11 }}>{expanded ? '▾' : '▸'}</span>
        <span style={{ fontWeight: 700, color: COLORS.textPrimary, fontSize: 14 }}>{webAcl.name}</span>
        <span style={{
          fontSize: 10.5, fontWeight: 700, padding: '2px 8px', borderRadius: 12,
          background: webAcl.scope === 'CLOUDFRONT' ? '#7c3aed18' : `${COLORS.brand}18`,
          color: webAcl.scope === 'CLOUDFRONT' ? '#7c3aed' : COLORS.brand,
        }}>
          {SCOPE_LABEL[webAcl.scope] || webAcl.scope}
        </span>
        <span style={{ fontSize: 11, color: COLORS.textMuted }}>Default action: {webAcl.default_action}</span>
        <span style={{ marginLeft: 'auto', fontSize: 11.5, color: COLORS.textMuted }}>
          {webAcl.rules?.length || 0} rule{(webAcl.rules?.length || 0) === 1 ? '' : 's'} · {webAcl.associated_resources?.length || 0} linked resource{(webAcl.associated_resources?.length || 0) === 1 ? '' : 's'}
        </span>
      </div>
      {expanded && (
        <div>
          {webAcl.error && (
            <div style={{ padding: '10px 16px', color: COLORS.danger, fontSize: 12.5 }}>⚠️ {webAcl.error}</div>
          )}

          {/* Associated resources */}
          <div style={{ padding: '10px 16px', borderBottom: `1px solid ${COLORS.border}` }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 6 }}>
              Linked Resources ({webAcl.associated_resources?.length || 0})
            </div>
            {(!webAcl.associated_resources || webAcl.associated_resources.length === 0) ? (
              <div style={{ fontSize: 12, color: COLORS.textMuted }}>No resources currently associated with this Web ACL.</div>
            ) : (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {webAcl.associated_resources.map((r, i) => (
                  <span key={i} title={r.arn}
                    style={{ padding: '3px 10px', borderRadius: 14, background: '#fff', border: `1px solid ${COLORS.border}`, fontSize: 12, fontWeight: 600, color: COLORS.textPrimary }}>
                    {r.type}: {r.extra || (r.arn ? r.arn.split('/').slice(-1)[0] : '—')}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* Rules */}
          <div>
            <div style={{ padding: '10px 16px 4px', fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>
              Rules ({webAcl.rules?.length || 0}) — sorted by evaluation priority. Click a rule to view its own metrics.
            </div>
            {(!webAcl.rules || webAcl.rules.length === 0) ? (
              <div style={{ padding: '4px 16px 14px', fontSize: 12, color: COLORS.textMuted }}>No rules configured.</div>
            ) : (
              webAcl.rules.map(rule => <RuleRow key={rule.name} accountId={accountId} webAcl={webAcl} rule={rule} />)
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Tab 1: "Web ACLs by Account" — the original structural view, now
// with NO metrics/CloudWatch anywhere in it. Takes the already-loaded
// `summary` + account list as props (both tabs share one summary fetch
// so switching tabs never re-fetches the structural data). ────────────
function WebAclsByAccountTab({ summary, loading, accounts, expandedAccounts, toggleExpand }) {
  return (
    <div style={{ ...CARD, overflow: 'hidden' }}>
      <div style={{ padding: '12px 18px', background: '#f8fafc', borderBottom: `1px solid ${COLORS.border}`, fontWeight: 700, color: COLORS.textPrimary, fontSize: 14 }}>
        Web ACLs by Account
      </div>
      {!loading && accounts.length === 0 ? (
        <div style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted }}>No accounts to show.</div>
      ) : (
        accounts.map(account => {
          const isOpen = !!expandedAccounts[account.account_id]
          const hasWebAcls = (account.web_acls || []).length > 0
          return (
            <div key={account.account_id} style={{ borderBottom: `1px solid ${COLORS.border}` }}>
              <div onClick={() => (!account.error) && toggleExpand(account.account_id)}
                style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 18px', cursor: !account.error ? 'pointer' : 'default', flexWrap: 'wrap' }}>
                <span style={{ color: COLORS.textMuted, fontSize: 11, width: 14 }}>
                  {!account.error ? (isOpen ? '▾' : '▸') : ''}
                </span>
                <span style={{ fontWeight: 600, color: COLORS.textPrimary }}>{account.alias}</span>
                <span style={{ color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11 }}>{account.account_id}</span>

                {account.error && (
                  <span title={account.error} style={{ color: COLORS.danger, fontSize: 11.5, fontWeight: 600, cursor: 'help' }}>⚠️ Fetch failed</span>
                )}
                {!account.error && !hasWebAcls && (
                  <span style={{ color: COLORS.textMuted, fontSize: 11.5 }}>No Web ACLs in this account</span>
                )}
                {!account.error && hasWebAcls && (
                  <span style={{ marginLeft: 'auto', color: COLORS.textMuted, fontSize: 12.5, fontWeight: 600 }}>
                    {account.web_acls.length} Web ACL{account.web_acls.length === 1 ? '' : 's'}
                  </span>
                )}
              </div>
              {isOpen && hasWebAcls && (
                <div style={{ padding: '4px 18px 16px' }}>
                  {account.web_acls.map(acl => <WebAclCard key={acl.id} accountId={account.account_id} webAcl={acl} />)}
                </div>
              )}
            </div>
          )
        })
      )}
    </div>
  )
}

// ── One impacted-rule row: horizontal bar + count, CLICKABLE to expand
// its live request samples (every action, last 3h — same
// SampledRequestsPanel used on the "Web ACLs by Account" tab's per-rule
// drill-down). Clicking the rule name/row is itself the explicit user
// action that authorizes the GetSampledRequests call — nothing is
// fetched until a specific rule row is expanded. ──────────────────────
function ImpactedRuleRow({ accountId, rule, color, barWidthPct }) {
  const [open, setOpen] = useState(false)
  const canDrillDown = !!(rule.web_acl_id && rule.scope)
  const webAclForPanel = useMemo(
    () => ({ name: rule.web_acl_name, id: rule.web_acl_id, scope: rule.scope }),
    [rule.web_acl_name, rule.web_acl_id, rule.scope],
  )
  const ruleForPanel = useMemo(() => ({ name: rule.rule_name }), [rule.rule_name])

  return (
    <div style={{ borderBottom: `1px solid ${COLORS.border}` }}>
      <div onClick={() => canDrillDown && setOpen(o => !o)}
        style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 4px', cursor: canDrillDown ? 'pointer' : 'default' }}>
        <span style={{ color: COLORS.textMuted, fontSize: 11, width: 12, flexShrink: 0 }}>{canDrillDown ? (open ? '▾' : '▸') : ''}</span>
        <div style={{ width: 250, flexShrink: 0, overflow: 'hidden' }}>
          <div style={{
            fontSize: 12, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            color: canDrillDown ? COLORS.brand : COLORS.textPrimary, textDecoration: canDrillDown ? 'underline' : 'none',
          }} title={canDrillDown ? `${rule.rule_name} — click to view live samples` : rule.rule_name}>
            {rule.rule_name}
          </div>
          {rule.web_acl_name && (
            <div style={{ fontSize: 10.5, color: COLORS.textMuted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={rule.web_acl_name}>
              {rule.web_acl_name}
            </div>
          )}
        </div>
        <div style={{ flex: 1, background: '#f1f5f9', borderRadius: 4, height: 18, position: 'relative' }}>
          <div style={{ width: `${barWidthPct}%`, height: '100%', borderRadius: 4, background: color, transition: 'width .3s' }} />
        </div>
        <div style={{ width: 70, fontSize: 12.5, fontWeight: 700, color: COLORS.textPrimary, flexShrink: 0, textAlign: 'right' }}>
          {fmtNum(rule.count)}
        </div>
      </div>
      {open && canDrillDown && (
        <div style={{ padding: '4px 4px 14px 22px' }}>
          <SampledRequestsPanel accountId={accountId} webAcl={webAclForPanel} rule={ruleForPanel} />
        </div>
      )}
    </div>
  )
}

// ── Horizontal-bar list of every impacted rule for one action — each
// row is clickable (see ImpactedRuleRow above) to drill into that
// specific rule's live request-sample log. Not a full SimpleCharts
// BarChart (which is money-formatted and built for cost data), so this
// stays purpose-built for plain counts. ────────────────────────────────
function ImpactedRulesBar({ accountId, rules, color }) {
  if (!rules || rules.length === 0) {
    return <div style={{ fontSize: 12, color: COLORS.textMuted }}>No rules recorded this action in the selected time range.</div>
  }
  const max = Math.max(1, ...rules.map(r => r.count))
  return (
    <div>
      {rules.map((r, i) => (
        <ImpactedRuleRow
          key={`${r.web_acl_name || ''}__${r.rule_name}__${i}`}
          accountId={accountId}
          rule={r}
          color={color}
          barWidthPct={Math.max(2, (r.count / max) * 100)}
        />
      ))}
    </div>
  )
}

// ── Action picker — 5 pill buttons (Block/Allow/Count/CAPTCHA/
// Challenge), CloudWatch-console-metric-style. Nothing is fetched until
// one of these is clicked (and an account is already selected). ───────
function ActionPicker({ value, onChange, disabled }) {
  return (
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
      {ACTION_KEYS.map(key => {
        const active = value === key
        const color = ACTION_COLOR[key]
        return (
          <button key={key} disabled={disabled} onClick={() => onChange(key)}
            style={{
              padding: '6px 14px', borderRadius: 16, fontSize: 12.5, fontWeight: 700, cursor: disabled ? 'default' : 'pointer',
              border: `1px solid ${active ? color : COLORS.border}`,
              background: active ? color : '#fff',
              color: active ? '#fff' : COLORS.textSecondary,
            }}>
            {ACTION_LABEL[key]}
          </button>
        )
      })}
    </div>
  )
}

// ── Tab 2: "By Action" — account-level, NO Web ACL selector at all.
// Flow: pick an AWS account first, then pick ONE action
// (Block/Allow/Count/CAPTCHA/Challenge). Only once BOTH are picked does
// this fetch — merging every Web ACL already known to exist in that
// account (from the shared structural summary) into one account-wide
// total + a combined "impacted rules" list for that single action.
// Switching the action re-fetches (still just that one action); nothing
// is ever fetched for all five actions at once. ────────────────────────
function ByActionTab({ accounts, loading }) {
  // Account picker options — every account from the shared structural
  // summary (not just ones with Web ACLs, since a fetch failure or a
  // genuinely-empty account should still be selectable and show a clear
  // "no Web ACLs / fetch failed" message rather than being hidden).
  const accountOptions = useMemo(
    () => accounts.map(a => ({ accountId: a.account_id, alias: a.alias, error: a.error, webAclCount: (a.web_acls || []).length })),
    [accounts],
  )

  const [selectedAccountId, setSelectedAccountId] = useState('')
  const [selectedAction, setSelectedAction] = useState('')
  const [timeRange, setTimeRange] = useState(DEFAULT_TIME_RANGE)
  const [data, setData] = useState(null)
  const [fetching, setFetching] = useState(false)
  const [error, setError] = useState('')

  const selectedAccount = useMemo(
    () => accountOptions.find(a => a.accountId === selectedAccountId) || null,
    [accountOptions, selectedAccountId],
  )

  // Changing account clears any previously-picked action's result —
  // avoids showing stale numbers from the old account while the new
  // account's data (if an action is re-picked) loads.
  useEffect(() => {
    setSelectedAction('')
    setData(null)
    setError('')
  }, [selectedAccountId])

  useEffect(() => {
    if (!selectedAccountId || !selectedAction) { setData(null); return }
    let cancelled = false
    setFetching(true)
    setError('')
    api.getWafMonitoringActionBreakdown(selectedAccountId, selectedAction, timeRange)
      .then(d => { if (!cancelled) setData(d) })
      .catch(e => { if (!cancelled) setError(e?.response?.data?.detail || 'Could not fetch action breakdown') })
      .finally(() => { if (!cancelled) setFetching(false) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAccountId, selectedAction, timeRange])

  const color = selectedAction ? ACTION_COLOR[selectedAction] : COLORS.brand

  return (
    <div>
      <div style={{ ...CARD, padding: '14px 18px', marginBottom: 16 }}>
        <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 8 }}>
          1. Select an AWS account
        </div>
        {loading ? (
          <div style={{ color: COLORS.textMuted, fontSize: 12.5 }}>Loading accounts…</div>
        ) : (
          <select value={selectedAccountId} onChange={e => setSelectedAccountId(e.target.value)}
            style={{ padding: '6px 10px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5, fontWeight: 600, color: COLORS.textPrimary, minWidth: 320 }}>
            <option value="">— Choose an account —</option>
            {accountOptions.map(a => (
              <option key={a.accountId} value={a.accountId}>
                {a.alias} ({a.accountId}){a.error ? ' — fetch failed' : a.webAclCount === 0 ? ' — no Web ACLs' : ` — ${a.webAclCount} Web ACL${a.webAclCount === 1 ? '' : 's'}`}
              </option>
            ))}
          </select>
        )}
      </div>

      {selectedAccount && (
        <div style={{ ...CARD, padding: '14px 18px', marginBottom: 16 }}>
          <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 8 }}>
            2. Select an action
          </div>
          {selectedAccount.error ? (
            <div style={{ color: COLORS.danger, fontSize: 12.5 }}>⚠️ Could not load Web ACLs for this account: {selectedAccount.error}</div>
          ) : selectedAccount.webAclCount === 0 ? (
            <div style={{ color: COLORS.textMuted, fontSize: 12.5 }}>This account has no Web ACLs — nothing to break down by action.</div>
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
              <ActionPicker value={selectedAction} onChange={setSelectedAction} disabled={fetching} />
              {selectedAction && <TimeRangePicker value={timeRange} onChange={setTimeRange} disabled={fetching} />}
            </div>
          )}
        </div>
      )}

      {!selectedAccountId && (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted, fontSize: 13 }}>
          Pick an AWS account above to continue. Nothing is fetched from CloudWatch until you also pick an action.
        </div>
      )}

      {selectedAccountId && !selectedAction && selectedAccount && !selectedAccount.error && selectedAccount.webAclCount > 0 && (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted, fontSize: 13 }}>
          Pick an action above (Block / Allow / Count / CAPTCHA / Challenge) to load its breakdown for this account.
        </div>
      )}

      {selectedAccountId && selectedAction && fetching && (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted }}>Loading action breakdown…</div>
      )}

      {selectedAccountId && selectedAction && !fetching && error && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', color: COLORS.danger, fontSize: 13 }}>
          {error}
        </div>
      )}

      {selectedAccountId && selectedAction && !fetching && !error && data && (
        <div>
          <div style={{ fontSize: 11, color: COLORS.textMuted, marginBottom: 12 }}>
            {selectedAccount?.alias} — across {data.web_acl_count} Web ACL{data.web_acl_count === 1 ? '' : 's'} — period {Math.round(data.period_seconds / 60)}m buckets
            {data.is_mock && ' (local mock data — no real AWS calls in dev mode)'}
          </div>
          <div style={{ ...CARD, padding: '18px 20px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
              <span style={{
                fontSize: 12.5, fontWeight: 700, padding: '4px 12px', borderRadius: 14,
                background: color + '18', color,
              }}>
                {ACTION_LABEL[selectedAction]}
              </span>
              <span style={{ fontSize: 28, fontWeight: 700, color: COLORS.textPrimary }}>{fmtNum(data.total)}</span>
            </div>
            <div style={{ fontSize: 10.5, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 8 }}>
              Impacted rules (across every Web ACL in this account)
            </div>
            <ImpactedRulesBar accountId={selectedAccountId} rules={data.rules} color={color} />
          </div>
        </div>
      )}
    </div>
  )
}

export default function WafMonitoringPage() {
  const [activeTab, setActiveTab] = useState('accounts') // 'accounts' | 'action'
  const [allAccounts, setAllAccounts] = useState([])
  const [selectedAccountIds, setSelectedAccountIds] = useState([]) // empty = all
  const [accountPickerOpen, setAccountPickerOpen] = useState(false)

  const initialCached = getPageCache(summaryCacheKey([]))
  const [summary, setSummary] = useState(initialCached || null)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [err, setErr] = useState('')
  const [expandedAccounts, setExpandedAccounts] = useState({})

  const abortRef = React.useRef(null)
  const seqRef = React.useRef(0)
  const mountedRef = React.useRef(true)

  useEffect(() => {
    mountedRef.current = true
    api.getWafMonitoringAccounts()
      .then(d => { if (mountedRef.current) setAllAccounts(d.accounts || []) })
      .catch(() => {})
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
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
    api.getWafMonitoringSummary(selectedAccountIds, refresh, controller.signal)
      .then(d => {
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setSummary(d)
        setPageCache(key, d)
      })
      .catch(e => {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setErr(e?.response?.data?.detail || 'Could not load AWS WAF Web ACL data')
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

  const accountPickerLabel = useMemo(() => {
    if (selectedAccountIds.length === 0) return `All accounts (${allAccounts.length})`
    return `${selectedAccountIds.length} account${selectedAccountIds.length === 1 ? '' : 's'} selected`
  }, [selectedAccountIds, allAccounts])

  const totals = summary?.totals || {}
  const accounts = summary?.accounts || []

  return (
    <div style={{ padding: '24px 28px', maxWidth: 1400 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 18, flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>🛡️ WAF Rule Monitoring</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            Every AWS WAF Web ACL across all AWS accounts and which resources it's linked to. The "Web ACLs by
            Account" tab is purely structural (no CloudWatch calls). The "By Action" tab loads CloudWatch data
            for one selected account + one selected action only after you pick both.
            {summary && !summary.is_mock && <> Last fetched {formatAge(summary.cache_age_seconds)}.</>}
            {summary?.is_mock && <> (local mock data — no real AWS calls in dev mode)</>}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
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

      {/* Account picker — filters the structural summary shared by both tabs */}
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

      {/* Org-wide KPI cards — structural only, no CloudWatch-derived counts */}
      {summary && (
        <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 160 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>
              Web ACLs Found
            </div>
            <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.textPrimary, marginTop: 4 }}>
              {totals.web_acl_count ?? 0}
            </div>
          </div>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 160 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>
              Accounts With WAF
            </div>
            <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.textPrimary, marginTop: 4 }}>
              {totals.accounts_with_waf_count ?? 0} / {totals.account_count ?? 0}
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

      {/* Sub-tabs */}
      <div style={{ ...TAB_BAR, marginBottom: 16 }}>
        <button style={tabButtonStyle(activeTab === 'accounts')} onClick={() => setActiveTab('accounts')}>
          Web ACLs by Account
        </button>
        <button style={tabButtonStyle(activeTab === 'action')} onClick={() => setActiveTab('action')}>
          By Action
        </button>
      </div>

      {activeTab === 'accounts' && (
        <WebAclsByAccountTab
          summary={summary}
          loading={loading}
          accounts={accounts}
          expandedAccounts={expandedAccounts}
          toggleExpand={toggleExpand}
        />
      )}
      {activeTab === 'action' && (
        <ByActionTab accounts={accounts} loading={loading} />
      )}
    </div>
  )
}
