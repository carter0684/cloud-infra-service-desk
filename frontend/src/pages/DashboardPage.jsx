import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { COLORS, CARD, STATUS_COLOR, formatTicketNumber, Skeleton, SkeletonLines } from '../utils/theme.js'
import { GaugeRing } from '../components/SimpleCharts.jsx'

// ITSM-style landing dashboard — the kind of "at a glance" ops view tools
// like Freshservice/ServiceNow show by default, which this portal didn't
// have before (everything opened straight into the raw ticket list). Pulls
// from GET /api/dashboard/stats (main.py), computed on-demand server-side.
//
// KPI card look (square colored icon tile + big number, no left accent
// bar) modeled on a reference lounge-operations dashboard screenshot the
// user asked to match, replacing the previous accent-bar-only card style.

function KpiCard({ label, value, sub, color, icon, onClick }) {
  return (
    <div onClick={onClick} style={{
      ...CARD, padding: '18px 20px', cursor: onClick ? 'pointer' : 'default',
      display: 'flex', alignItems: 'flex-start', gap: 14,
    }}>
      <div style={{
        width: 44, height: 44, borderRadius: 10, background: color, color: '#fff',
        display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 20, flexShrink: 0,
      }}>
        {icon}
      </div>
      <div>
        <div style={{ fontSize: 26, fontWeight: 800, color: COLORS.textPrimary, lineHeight: 1.1 }}>{value}</div>
        <div style={{ fontSize: 12, color: COLORS.textMuted, marginTop: 3 }}>{label}</div>
        {sub && <div style={{ fontSize: 11.5, color: COLORS.textSecondary, marginTop: 2 }}>{sub}</div>}
      </div>
    </div>
  )
}

function Panel({ title, children, right }) {
  return (
    <div style={{ ...CARD, padding: '18px 22px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
        <h3 style={{ margin: 0, fontSize: 14.5, color: COLORS.brandDark, fontWeight: 700 }}>{title}</h3>
        {right}
      </div>
      {children}
    </div>
  )
}

function Bar({ label, count, max, color }) {
  const pct = max > 0 ? Math.max(4, (count / max) * 100) : 0
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 9 }}>
      <div style={{ width: 110, fontSize: 12.5, color: COLORS.textSecondary, flexShrink: 0 }}>{label}</div>
      <div style={{ flex: 1, background: '#f1f5f9', borderRadius: 4, height: 10, overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: 4 }} />
      </div>
      <div style={{ width: 28, textAlign: 'right', fontSize: 12.5, fontWeight: 700, color: COLORS.textPrimary }}>{count}</div>
    </div>
  )
}

// ── Executive Summary panels (AI-generated) ─────────────────────────────────
// For the management-facing landing view: auto-loads the same Bedrock
// "AI Analysis" briefings already available on-demand on the Cost
// Analysis / Billing by Service pages themselves, but rendered inline
// here (no button click needed) so a manager sees the narrative the
// moment they open the Dashboard. Both source endpoints are cached
// server-side for 15 minutes (see main.py), so repeat Dashboard loads
// don't re-trigger Bedrock every time.
const TREND_LABEL = { increasing: '📈 Increasing', decreasing: '📉 Decreasing', stable: '➡️ Stable' }

function AiExecutiveSummaryCard({ title, icon, summary, loading, error, onLinkClick, linkLabel }) {
  return (
    <div style={{ ...CARD, padding: '18px 22px', flex: 1, minWidth: 'min(320px, 100%)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <h3 style={{ margin: 0, fontSize: 14.5, color: COLORS.brandDark, fontWeight: 700 }}>{icon} {title}</h3>
        {onLinkClick && (
          <button onClick={onLinkClick} style={{ border: 'none', background: 'none', color: COLORS.brand, fontSize: 12, fontWeight: 700, cursor: 'pointer', padding: 0 }}>
            {linkLabel} →
          </button>
        )}
      </div>

      {loading && (
        <div style={{ padding: '18px 0', textAlign: 'center', color: COLORS.textMuted, fontSize: 13 }}>⏳ Generating executive summary…</div>
      )}

      {error && !loading && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '9px 12px', color: COLORS.danger, fontSize: 12.5 }}>
          {error}
        </div>
      )}

      {summary && !loading && !error && (
        <>
          <div style={{ marginBottom: 10 }}>
            <span style={{ padding: '3px 10px', borderRadius: 20, background: COLORS.brandLight, color: COLORS.brand, fontSize: 11.5, fontWeight: 700 }}>
              {TREND_LABEL[summary.trend] || summary.trend}
            </span>
          </div>

          <div style={{ fontSize: 13, color: COLORS.textPrimary, lineHeight: 1.6, marginBottom: 12 }}>{summary.executive_summary}</div>

          {summary.key_cost_drivers?.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.3, marginBottom: 4 }}>Key Cost Drivers</div>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: COLORS.textPrimary, lineHeight: 1.6 }}>
                {summary.key_cost_drivers.map((d, i) => <li key={i}>{d}</li>)}
              </ul>
            </div>
          )}

          {summary.recommendations?.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.3, marginBottom: 4 }}>Recommendations</div>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: COLORS.textPrimary, lineHeight: 1.6 }}>
                {summary.recommendations.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </div>
          )}

          {summary.bottom_line && (
            <div style={{ background: COLORS.brandLight, borderRadius: 8, padding: '9px 13px', fontSize: 12.5, color: COLORS.brandDark, fontWeight: 600 }}>
              💡 {summary.bottom_line}
            </div>
          )}

          <div style={{ fontSize: 10, color: COLORS.textMuted, marginTop: 10, textAlign: 'right' }}>
            {summary.generated_at ? new Date(summary.generated_at).toLocaleString() : ''}
          </div>
        </>
      )}
    </div>
  )
}

export default function DashboardPage() {
  const navigate = useNavigate()
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  // Executive Summary (AI) — Cost Analysis + Billing by Service, loaded
  // automatically for management visibility (see AiExecutiveSummaryCard
  // above). Independent loading/error state per card so one failing
  // doesn't block the other.
  const [costAiSummary, setCostAiSummary] = useState(null)
  const [costAiLoading, setCostAiLoading] = useState(true)
  const [costAiError, setCostAiError] = useState('')
  const [billingAiSummary, setBillingAiSummary] = useState(null)
  const [billingAiLoading, setBillingAiLoading] = useState(true)
  const [billingAiError, setBillingAiError] = useState('')

  // Loads the two AI executive summaries. `silent=true` (used by the
  // background refresh below) skips flipping the loading spinner back on
  // — the currently-shown summary just stays on screen until the fresh
  // one replaces it, matching the "silently re-fetch in background" UX
  // already used by the Health Dashboard / Cost Analysis / Billing by
  // Service pages.
  function loadAiSummaries(silent = false) {
    if (!silent) setCostAiLoading(true)
    api.getCostAnalysisAiSummary()
      .then(d => { setCostAiSummary(d); setCostAiError('') })
      .catch(e => setCostAiError(e?.response?.data?.detail || 'Could not load Cost Analysis executive summary'))
      .finally(() => setCostAiLoading(false))

    if (!silent) setBillingAiLoading(true)
    api.getBillingByServiceAiSummary()
      .then(d => { setBillingAiSummary(d); setBillingAiError('') })
      .catch(e => setBillingAiError(e?.response?.data?.detail || 'Could not load Billing by Service executive summary'))
      .finally(() => setBillingAiLoading(false))
  }

  useEffect(() => {
    api.dashboardStats()
      .then(setStats)
      .catch(e => setErr(e?.response?.data?.detail || 'Failed to load dashboard'))
      .finally(() => setLoading(false))

    loadAiSummaries(false)

    // Server-side cache for both AI-summary endpoints is 15 minutes (see
    // main.py's _COST_AI_SUMMARY_CACHE / _BILLING_AI_SUMMARY_CACHE) — so
    // silently re-fetch every 15 minutes in the background while the
    // Dashboard tab is left open, so a manager who keeps this page up
    // never sees a summary go stale for longer than the cache window.
    const id = setInterval(() => loadAiSummaries(true), 15 * 60 * 1000)
    return () => clearInterval(id)
  }, [])

  // Skeleton placeholder in the shape of the real dashboard (4 KPI-card
  // tiles + a couple of taller panel blocks below) rather than a single
  // "Loading dashboard…" line — feels closer to what's about to render,
  // and reads as "content is on its way" instead of "please wait".
  if (loading) return (
    <div style={{ maxWidth: 1240, margin: '0 auto', padding: '24px 24px 40px' }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 16, marginBottom: 20 }}>
        {[0, 1, 2, 3].map(i => (
          <div key={i} style={{ ...CARD, padding: '18px 20px', display: 'flex', alignItems: 'flex-start', gap: 14 }}>
            <Skeleton width={44} height={44} radius={10} />
            <div style={{ flex: 1 }}>
              <Skeleton width={60} height={22} style={{ marginBottom: 8 }} />
              <Skeleton width="80%" height={11} />
            </div>
          </div>
        ))}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 16 }}>
        <div style={{ ...CARD, padding: '18px 22px' }}><SkeletonLines lines={5} /></div>
        <div style={{ ...CARD, padding: '18px 22px' }}><SkeletonLines lines={5} /></div>
      </div>
    </div>
  )
  if (err) return <div style={{ padding: 40, textAlign: 'center', color: COLORS.danger }}>{err}</div>
  if (!stats) return null

  const t = stats.totals
  const maxStatus = Math.max(1, ...Object.values(stats.by_status || {}))
  const maxCategory = Math.max(1, ...Object.values(stats.by_category || {}))

  return (
    <div style={{ maxWidth: 1240, margin: '0 auto', padding: '24px 24px 40px' }}>
      <div style={{ marginBottom: 20 }}>
        <h1 style={{ margin: 0, fontSize: 22, color: COLORS.brandDark, fontWeight: 800 }}>Service Desk Dashboard</h1>
        <p style={{ margin: '4px 0 0', color: COLORS.textSecondary, fontSize: 13 }}>
          Live snapshot of ticket volume and recent activity.
        </p>
      </div>

      {/* KPI row — colored icon-tile cards (see KpiCard above) */}
      <div className="kpi-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 14, marginBottom: 20 }}>
        <KpiCard label="Open Tickets" value={t.open} sub={`${t.total} total all-time`} color="#2563eb" icon="🎫" onClick={() => navigate('/jobs')} />
        <KpiCard label="Past Due Date" value={t.overdue_due_date} sub="manual due date passed" color="#d97706" icon="📅" />
        <KpiCard label="Created This Month" value={t.created_this_month} color="#7c3aed" icon="🆕" />
        <KpiCard label="Resolved This Month" value={t.resolved_this_month} color="#16a34a" icon="✅" />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 16, marginBottom: 16 }}>
        {/* Resolution Rate — thin-ring gauge with big side stats, styled
            after the reference "Capacity Usage" widget: % in the middle
            of the ring, "big number / small label" stats beside it. */}
        <Panel title="Resolution Rate (All-Time)">
          <GaugeRing
            pct={t.total > 0 ? (t.closed / t.total) * 100 : 0}
            ringColor={COLORS.success}
            stats={[
              { value: t.closed, label: 'Resolved Tickets' },
              { value: t.total, label: 'Total Tickets' },
            ]}
          />
        </Panel>
        <Panel title="Tickets by Status">
          {Object.entries(stats.by_status || {}).map(([status, count]) => (
            <Bar key={status} label={status} count={count} max={maxStatus} color={STATUS_COLOR[status] || COLORS.textMuted} />
          ))}
        </Panel>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 16, marginBottom: 16 }}>
        <Panel title="Open Tickets by Category">
          {Object.keys(stats.by_category || {}).length === 0 ? (
            <div style={{ color: COLORS.textMuted, fontSize: 13 }}>No data yet</div>
          ) : Object.entries(stats.by_category).map(([cat, count], i) => (
            <Bar key={cat} label={cat} count={count} max={maxCategory} color={['#2563eb', '#16a34a', '#d97706', '#7c3aed', '#0891b2', '#db2777'][i % 6]} />
          ))}
        </Panel>
        <Panel title="🕒 Recent Activity">
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 260, overflowY: 'auto' }}>
            {stats.recent_activity.map(j => (
              <div key={j.id} onClick={() => navigate(`/jobs/${j.id}`)}
                style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px', borderRadius: 6, cursor: 'pointer' }}
                onMouseEnter={e => e.currentTarget.style.background = '#f8fafc'}
                onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                <span style={{ fontSize: 11, fontWeight: 700, color: COLORS.brand, fontFamily: 'monospace' }}>{formatTicketNumber(j)}</span>
                <span style={{ fontSize: 13, color: COLORS.textPrimary, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{j.title}</span>
                <span style={{
                  fontSize: 10.5, fontWeight: 700, padding: '2px 7px', borderRadius: 4,
                  background: (STATUS_COLOR[j.status] || COLORS.textMuted) + '18', color: STATUS_COLOR[j.status] || COLORS.textMuted,
                }}>{j.status}</span>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      {/* Executive Summary — AI-generated Cost Analysis + Billing by
          Service briefings, for management presentation. Sits at the
          bottom of the page so the ticket KPIs and charts come first;
          the cards load independently of the ticket stats. */}
      <div style={{ marginTop: 8 }}>
        <h2 style={{ margin: '0 0 12px', fontSize: 16, color: COLORS.brandDark, fontWeight: 800 }}>📊 Executive Summary</h2>
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
          <AiExecutiveSummaryCard
            title="AWS Cost Analysis" icon="💰"
            summary={costAiSummary} loading={costAiLoading} error={costAiError}
            onLinkClick={() => navigate('/cost-analysis')} linkLabel="View Cost Analysis"
          />
          <AiExecutiveSummaryCard
            title="Billing by Service" icon="🧾"
            summary={billingAiSummary} loading={billingAiLoading} error={billingAiError}
            onLinkClick={() => navigate('/billing-by-service')} linkLabel="View Billing by Service"
          />
        </div>
      </div>
    </div>
  )
}
