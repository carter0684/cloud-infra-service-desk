import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'
import useOrg from '../utils/useOrg.js'
import { COLORS, CARD } from '../utils/theme.js'
import { BarChart, PieChart, TrendBadge } from '../components/SimpleCharts.jsx'
import { getPageCache, setPageCache } from '../utils/pageCache.js'

const CACHE_KEY = 'cost-analysis'

function isFiniteNum(n) {
  return typeof n === 'number' && Number.isFinite(n)
}

function isValidCurrency(code) {
  return typeof code === 'string' && /^[A-Za-z]{3}$/.test(code)
}

function money(amount, currency = 'USD') {
  if (!isFiniteNum(amount)) return '—'
  const safeCurrency = isValidCurrency(currency) ? currency.toUpperCase() : 'USD'
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: safeCurrency, maximumFractionDigits: 2 }).format(amount)
  } catch {
    return `${safeCurrency} ${amount.toFixed(2)}`
  }
}

function formatAge(seconds) {
  if (!seconds || seconds < 60) return 'just now'
  const mins = Math.floor(seconds / 60)
  return `${mins} min${mins === 1 ? '' : 's'} ago`
}

// Breakdown column definitions — mirrors the finance team's "AWS Actual
// Expenses" workbook per-account monthly columns (AWS Usage except MP /
// AWS Marketplace / AWS Shield / AWS Enterprise Support / Credits /
// Subtotal).
const BREAKDOWN_COLS = [
  { key: 'usage', label: 'AWS Usage' },
  { key: 'marketplace', label: 'Marketplace' },
  { key: 'shield', label: 'Shield' },
  { key: 'support', label: 'Support' },
  { key: 'credits', label: 'Credits' },
  // Admin-entered manual line items (extra fees/corrections not visible
  // in Cost Explorer) — always additive, never mixed into the AWS-reported
  // columns above. See cost_explorer.apply_manual_adjustments().
  { key: 'adjustments', label: 'Adjustments', manual: true },
  { key: 'subtotal', label: 'Subtotal', bold: true },
]

const PERIOD_OPTIONS = [
  { key: 'mtd', label: 'Month-to-date' },
  { key: 'last_month', label: 'Last full month' },
]

// ── Manual cost adjustments panel — inside the account drill-down modal ───
// Admins can add/edit/delete; everyone else sees a read-only list. These
// entries are stored completely separately from Cost Explorer data and are
// only ever layered on top of it as their own "adjustments" bucket — never
// mixed into usage/marketplace/shield/support/credits.

const CURRENT_MONTH_KEY = new Date().toISOString().slice(0, 7) // "YYYY-MM"

function emptyAdjustmentForm() {
  return { period: CURRENT_MONTH_KEY, label: '', amount: '', currency: 'USD', note: '' }
}

function AdjustmentsPanel({ account, canManage, onChanged }) {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [form, setForm] = useState(emptyAdjustmentForm())
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')

  const refresh = useCallback(() => {
    setLoading(true)
    setError('')
    api.listCostAdjustments(account.account_id)
      .then(data => setItems(Array.isArray(data) ? data : []))
      .catch(e => setError(e?.response?.data?.detail || 'Could not load adjustments'))
      .finally(() => setLoading(false))
  }, [account.account_id])

  useEffect(() => { refresh() }, [refresh])

  function startAdd() {
    setForm(emptyAdjustmentForm())
    setEditingId(null)
    setFormError('')
    setShowForm(true)
  }

  function startEdit(item) {
    setForm({
      period: item.period, label: item.label, amount: String(item.amount),
      currency: item.currency || 'USD', note: item.note || '',
    })
    setEditingId(item.id)
    setFormError('')
    setShowForm(true)
  }

  function cancelForm() {
    setShowForm(false)
    setEditingId(null)
    setFormError('')
  }

  async function submitForm() {
    const label = form.label.trim()
    const amountNum = parseFloat(form.amount)
    if (!label) { setFormError('Label is required.'); return }
    if (!Number.isFinite(amountNum)) { setFormError('Amount must be a valid number.'); return }
    if (!/^[A-Za-z]{3}$/.test(form.currency || '')) { setFormError('Currency must be a 3-letter code, e.g. USD.'); return }
    if (!/^\d{4}-\d{2}$/.test(form.period || '')) { setFormError('Month must be in YYYY-MM format.'); return }

    setSaving(true)
    setFormError('')
    try {
      if (editingId) {
        await api.updateCostAdjustment(editingId, {
          label, amount: amountNum, currency: form.currency.toUpperCase(), note: form.note || null,
        })
      } else {
        await api.createCostAdjustment({
          account_id: account.account_id, period: form.period, label,
          amount: amountNum, currency: form.currency.toUpperCase(), note: form.note || null,
        })
      }
      setShowForm(false)
      setEditingId(null)
      refresh()
      onChanged?.()
    } catch (e) {
      setFormError(e?.response?.data?.detail || 'Could not save adjustment')
    } finally {
      setSaving(false)
    }
  }

  async function removeItem(item) {
    if (!window.confirm(`Delete adjustment "${item.label}" (${money(item.amount, item.currency)})?`)) return
    try {
      await api.deleteCostAdjustment(item.id)
      refresh()
      onChanged?.()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Could not delete adjustment')
    }
  }

  const INPUT = { width: '100%', padding: '6px 9px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5, boxSizing: 'border-box' }
  const LABEL = { fontSize: 10.5, color: COLORS.textMuted, fontWeight: 700, textTransform: 'uppercase', marginBottom: 3, display: 'block' }

  return (
    <div style={{ marginTop: 18, borderTop: `1px solid ${COLORS.border}`, paddingTop: 14 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <div style={{ fontSize: 12, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.3 }}>
          Manual Adjustments <span style={{ fontWeight: 500, textTransform: 'none' }}>(extra fees / corrections)</span>
        </div>
        {canManage && !showForm && (
          <button onClick={startAdd}
            style={{ padding: '4px 10px', borderRadius: 6, border: `1px solid ${COLORS.brand}`, background: '#fff', color: COLORS.brand, fontSize: 11.5, fontWeight: 700, cursor: 'pointer' }}>
            + Add
          </button>
        )}
      </div>

      {error && <div style={{ color: COLORS.danger, fontSize: 12, marginBottom: 8 }}>{error}</div>}

      {showForm && (
        <div style={{ background: '#f8fafc', borderRadius: 8, padding: 12, marginBottom: 10 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 8 }}>
            <div>
              <label style={LABEL}>Month</label>
              <input type="month" value={form.period} onChange={e => setForm(f => ({ ...f, period: e.target.value }))} style={INPUT} />
            </div>
            <div>
              <label style={LABEL}>Amount</label>
              <input type="number" step="0.01" value={form.amount} onChange={e => setForm(f => ({ ...f, amount: e.target.value }))} placeholder="e.g. 150.00" style={INPUT} />
            </div>
          </div>
          <div style={{ marginBottom: 8 }}>
            <label style={LABEL}>Label</label>
            <input type="text" value={form.label} onChange={e => setForm(f => ({ ...f, label: e.target.value }))} placeholder="e.g. Partner invoice - Q3 support" style={INPUT} maxLength={200} />
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: 8, marginBottom: 8 }}>
            <div>
              <label style={LABEL}>Currency</label>
              <input type="text" value={form.currency} onChange={e => setForm(f => ({ ...f, currency: e.target.value.toUpperCase() }))} maxLength={3} style={INPUT} />
            </div>
            <div>
              <label style={LABEL}>Note (optional)</label>
              <input type="text" value={form.note} onChange={e => setForm(f => ({ ...f, note: e.target.value }))} style={INPUT} maxLength={1000} />
            </div>
          </div>
          {formError && <div style={{ color: COLORS.danger, fontSize: 11.5, marginBottom: 8 }}>{formError}</div>}
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={submitForm} disabled={saving}
              style={{ padding: '6px 14px', borderRadius: 6, border: 'none', background: COLORS.brand, color: '#fff', fontSize: 12, fontWeight: 700, cursor: saving ? 'default' : 'pointer', opacity: saving ? 0.6 : 1 }}>
              {saving ? 'Saving…' : editingId ? 'Save changes' : 'Add adjustment'}
            </button>
            <button onClick={cancelForm} disabled={saving}
              style={{ padding: '6px 14px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontSize: 12, fontWeight: 600, cursor: 'pointer' }}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <div style={{ color: COLORS.textMuted, fontSize: 12.5 }}>Loading adjustments…</div>
      ) : items.length === 0 ? (
        <div style={{ color: COLORS.textMuted, fontSize: 12.5 }}>No manual adjustments recorded for this account.</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {items.map(item => (
            <div key={item.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', background: '#fffbeb', border: '1px solid #fde68a', borderRadius: 7, padding: '7px 12px' }}>
              <div>
                <div style={{ fontSize: 12.5, fontWeight: 600, color: COLORS.textPrimary }}>{item.label}</div>
                <div style={{ fontSize: 11, color: COLORS.textMuted }}>
                  {item.period} · added by {item.created_by_name || item.created_by_email}
                  {item.note && <> · {item.note}</>}
                </div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ fontSize: 13, fontWeight: 700, color: item.amount < 0 ? COLORS.success : COLORS.textPrimary }}>
                  {money(item.amount, item.currency)}
                </span>
                {canManage && (
                  <div style={{ display: 'flex', gap: 4 }}>
                    <button onClick={() => startEdit(item)} title="Edit" style={{ border: 'none', background: 'none', cursor: 'pointer', fontSize: 13 }}>✏️</button>
                    <button onClick={() => removeItem(item)} title="Delete" style={{ border: 'none', background: 'none', cursor: 'pointer', fontSize: 13, color: COLORS.danger }}>🗑️</button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Account drill-down modal — per-service spend for one account ────────────

function AccountDetailModal({ account, period, currency, canManageAdjustments, onAdjustmentsChanged, onClose }) {
  const { coreAccountId } = useOrg()
  if (!account) return null
  const services = (period === 'mtd' ? account.services_mtd : account.services_last_month) || []
  const periodData = account[period] || {}
  const pct = period === 'mtd' ? account.mtd_vs_last_month_pct : account.last_month_vs_prior_pct
  const chartItems = services.map(s => ({ label: s.service, value: s.amount }))

  return (
    <div onClick={onClose}
      style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200 }}>
      <div onClick={e => e.stopPropagation()}
        style={{ background: '#fff', borderRadius: 12, padding: 24, width: 620, maxHeight: '85vh', overflowY: 'auto', boxShadow: '0 10px 40px rgba(0,0,0,0.25)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 6 }}>
          <div>
            <div style={{ fontSize: 17, fontWeight: 700, color: COLORS.textPrimary }}>
              {account.account_id === coreAccountId && coreAccountId && '⭐ '}{account.account_name}
            </div>
            <div style={{ fontSize: 12, color: '#64748b', fontFamily: 'monospace', marginTop: 2 }}>
              {account.account_id} · {account.bu} Business Unit
            </div>
          </div>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 20, cursor: 'pointer', color: COLORS.textMuted, lineHeight: 1 }}>×</button>
        </div>

        <div style={{ display: 'flex', gap: 10, margin: '16px 0' }}>
          <div style={{ flex: 1, background: '#f8fafc', borderRadius: 8, padding: '10px 14px' }}>
            <div style={{ fontSize: 10.5, color: COLORS.textMuted, textTransform: 'uppercase', fontWeight: 700 }}>{PERIOD_OPTIONS.find(p => p.key === period)?.label}</div>
            <div style={{ fontSize: 20, fontWeight: 700, color: COLORS.textPrimary }}>{money(periodData.subtotal, account.currency || currency)}</div>
          </div>
          <div style={{ flex: 1, background: '#f8fafc', borderRadius: 8, padding: '10px 14px' }}>
            <div style={{ fontSize: 10.5, color: COLORS.textMuted, textTransform: 'uppercase', fontWeight: 700 }}>vs prior period</div>
            <div style={{ fontSize: 20 }}><TrendBadge pct={pct} size={18} /></div>
          </div>
        </div>

        <div style={{ fontSize: 12, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.3, marginBottom: 10 }}>
          Spend by AWS service
        </div>
        {chartItems.length === 0 ? (
          <div style={{ color: COLORS.textMuted, fontSize: 13, padding: '20px 0' }}>No per-service data for this period.</div>
        ) : (
          <BarChart items={chartItems} currency={account.currency || currency} />
        )}

        <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 16, borderTop: `1px solid ${COLORS.border}`, paddingTop: 10 }}>
          Cost breakdown (Usage / Marketplace / Shield / Support / Credits / Adjustments): {' '}
          {money(periodData.usage, currency)} usage, {money(periodData.marketplace, currency)} marketplace,{' '}
          {money(periodData.shield, currency)} shield, {money(periodData.support, currency)} support,{' '}
          {money(periodData.credits, currency)} credits, {money(periodData.adjustments, currency)} adjustments.
        </div>

        <AdjustmentsPanel account={account} canManage={canManageAdjustments} onChanged={onAdjustmentsChanged} />
      </div>
    </div>
  )
}

// ── AI executive-summary modal ───────────────────────────────────────────────

function AiSummaryModal({ summary, loading, error, onClose, onRetry }) {
  const TREND_LABEL = { increasing: '📈 Increasing', decreasing: '📉 Decreasing', stable: '➡️ Stable' }
  return (
    <div onClick={onClose}
      style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200 }}>
      <div onClick={e => e.stopPropagation()}
        style={{ background: '#fff', borderRadius: 12, padding: 24, width: 620, maxHeight: '85vh', overflowY: 'auto', boxShadow: '0 10px 40px rgba(0,0,0,0.25)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
          <div style={{ fontSize: 17, fontWeight: 700, color: COLORS.textPrimary }}>🤖 AI Cost Analysis Summary</div>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 20, cursor: 'pointer', color: COLORS.textMuted, lineHeight: 1 }}>×</button>
        </div>

        {loading && (
          <div style={{ padding: '30px 0', textAlign: 'center', color: COLORS.textMuted }}>⏳ Generating executive summary…</div>
        )}

        {error && !loading && (
          <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 12, color: COLORS.danger, fontSize: 13 }}>
            {error}
            <div style={{ marginTop: 8 }}>
              <button onClick={onRetry} style={{ padding: '5px 12px', borderRadius: 6, border: `1px solid ${COLORS.danger}`, background: '#fff', color: COLORS.danger, fontSize: 12, fontWeight: 600, cursor: 'pointer' }}>Retry</button>
            </div>
          </div>
        )}

        {summary && !loading && (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
              <span style={{ padding: '4px 12px', borderRadius: 20, background: COLORS.brandLight, color: COLORS.brand, fontSize: 12.5, fontWeight: 700 }}>
                {TREND_LABEL[summary.trend] || summary.trend}
              </span>
            </div>

            <div style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.3, marginBottom: 6 }}>Executive Summary</div>
              <div style={{ fontSize: 14, color: COLORS.textPrimary, lineHeight: 1.6 }}>{summary.executive_summary}</div>
            </div>

            {summary.key_cost_drivers?.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.3, marginBottom: 6 }}>Key Cost Drivers</div>
                <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13.5, color: COLORS.textPrimary, lineHeight: 1.7 }}>
                  {summary.key_cost_drivers.map((d, i) => <li key={i}>{d}</li>)}
                </ul>
              </div>
            )}

            {summary.recommendations?.length > 0 && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.3, marginBottom: 6 }}>Cost-Control Recommendations</div>
                <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13.5, color: COLORS.textPrimary, lineHeight: 1.7 }}>
                  {summary.recommendations.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              </div>
            )}

            {summary.bottom_line && (
              <div style={{ background: COLORS.brandLight, borderRadius: 8, padding: '12px 16px', fontSize: 13.5, color: COLORS.brandDark, fontWeight: 600 }}>
                💡 {summary.bottom_line}
              </div>
            )}

            <div style={{ fontSize: 10.5, color: COLORS.textMuted, marginTop: 16, textAlign: 'right' }}>
              Generated by {summary.model_used} · {summary.generated_at ? new Date(summary.generated_at).toLocaleString() : ''}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────

export default function CostAnalysisPage() {
  const { user } = useAuth()
  // Seed from the in-tab page cache (see utils/pageCache.js) so
  // re-visiting this page renders instantly with the last-seen data
  // instead of a blank "Loading…" screen; the real refresh below still
  // runs, just silently in the background when data is already cached.
  const cached = getPageCache(CACHE_KEY)
  const [data, setData] = useState(cached || null)
  const [dataStale, setDataStale] = useState(false)
  const [loading, setLoading] = useState(!cached)
  const [refreshing, setRefreshing] = useState(false)
  const [err, setErr] = useState('')
  const [period, setPeriod] = useState('mtd')
  const [buFilter, setBuFilter] = useState('All')
  const [collapsedBu, setCollapsedBu] = useState({})
  const [selectedAccount, setSelectedAccount] = useState(null)

  const [aiOpen, setAiOpen] = useState(false)
  const [aiSummary, setAiSummary] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState('')

  // Request-lifecycle guards: an AbortController per in-flight request so
  // a superseded/older request (a stale refresh, or one still pending
  // when the component unmounts) can never overwrite state with an
  // out-of-date response. requestSeq additionally lets the .then/.catch
  // handlers themselves detect "am I still the latest request" even when
  // the abort races the response (fetch already resolved before abort()
  // took effect).
  const costAbortRef = React.useRef(null)
  const costSeqRef = React.useRef(0)
  const aiAbortRef = React.useRef(null)
  const aiSeqRef = React.useRef(0)
  const mountedRef = React.useRef(true)
  // Tracks whether we currently have any previously-loaded data, read
  // inside load()'s stable (empty-deps) closure — a plain `data` read
  // there would always see the value from the very first render.
  const hasDataRef = React.useRef(false)
  useEffect(() => { hasDataRef.current = data != null }, [data])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      costAbortRef.current?.abort()
      aiAbortRef.current?.abort()
    }
  }, [])

  const load = useCallback((refresh = false) => {
    // Cancel any still-in-flight cost-analysis request before starting a
    // new one — prevents an old, slower response from landing after a
    // newer one and clobbering fresher state.
    costAbortRef.current?.abort()
    const controller = new AbortController()
    costAbortRef.current = controller
    const mySeq = ++costSeqRef.current

    // Only show the full-page loading spinner when there's nothing
    // cached to show yet — if data is already on screen (from the page
    // cache or a previous load), this call runs invisibly in the
    // background instead.
    if (refresh) setRefreshing(true)
    else if (!getPageCache(CACHE_KEY)) setLoading(true)
    setErr('')
    return api.getOrgCostAnalysis(refresh, controller.signal)
      .then(d => {
        if (!mountedRef.current || mySeq !== costSeqRef.current) return null
        setData(d)
        setPageCache(CACHE_KEY, d)
        setDataStale(false)
        // Keep an open account drill-down modal in sync with the freshly
        // reloaded numbers (e.g. right after a manual adjustment was
        // added/edited/deleted for the account currently being viewed).
        setSelectedAccount(prev => {
          if (!prev) return prev
          return (d?.accounts || []).find(a => a.account_id === prev.account_id) || prev
        })
        return d
      })
      .catch(e => {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return null
        if (!mountedRef.current || mySeq !== costSeqRef.current) return null
        setErr(e?.response?.data?.detail || 'Could not load AWS Cost Analysis')
        // Keep any previously-loaded data on screen, but flag it as
        // stale rather than silently pretending it's still fresh.
        if (hasDataRef.current) setDataStale(true)
        return null
      })
      .finally(() => {
        if (!mountedRef.current || mySeq !== costSeqRef.current) return
        setLoading(false)
        setRefreshing(false)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => { load(false) }, [load])

  // No manual Refresh button on this page — data silently re-fetches
  // every 6 hours in the background instead. force_refresh=false rides
  // the server-side cache (cost_explorer.py's _CACHE_TTL_SECONDS) rather
  // than always bypassing it.
  useEffect(() => {
    const id = setInterval(() => { load(false) }, 6 * 60 * 60 * 1000)
    return () => clearInterval(id)
  }, [load])

  function runAiAnalysis() {
    // Cancel any previous AI request (e.g. user re-opened/retried before
    // the first one resolved) and clear stale results immediately so the
    // modal never briefly shows a prior summary as if it were fresh.
    aiAbortRef.current?.abort()
    const controller = new AbortController()
    aiAbortRef.current = controller
    const mySeq = ++aiSeqRef.current

    setAiOpen(true)
    setAiSummary(null)
    setAiLoading(true)
    setAiError('')
    api.getCostAnalysisAiSummary(false, controller.signal)
      .then(result => {
        if (!mountedRef.current || mySeq !== aiSeqRef.current) return
        setAiSummary(result)
      })
      .catch(e => {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
        if (!mountedRef.current || mySeq !== aiSeqRef.current) return
        setAiError(e?.response?.data?.detail || 'Could not generate AI cost analysis summary')
      })
      .finally(() => {
        if (!mountedRef.current || mySeq !== aiSeqRef.current) return
        setAiLoading(false)
      })
  }

  function closeAiModal() {
    // Cancel the in-flight AI request so closing the modal actually stops
    // waiting on (and bills for) a response nobody will see, and bump the
    // sequence so if it still resolves, its handlers no-op.
    aiAbortRef.current?.abort()
    aiSeqRef.current += 1
    setAiOpen(false)
  }

  const accounts = data?.accounts || []
  const businessUnits = data?.business_units || []
  const topServicesMtd = data?.top_services_mtd || []
  // top_services_last_month is additive to the API contract; fall back
  // to the MTD list (old behavior) if an older backend doesn't send it,
  // rather than showing an empty chart for the "Last full month" period.
  const topServicesLastMonth = data?.top_services_last_month || data?.top_services_mtd || []
  const topServices = period === 'mtd' ? topServicesMtd : topServicesLastMonth
  const currency = data?.currency || 'USD'

  const buOptions = useMemo(() => {
    const seen = new Set(accounts.map(a => a.bu || 'Other'))
    return ['All', ...businessUnits.map(b => b.bu).filter(bu => seen.has(bu))]
  }, [accounts, businessUnits])

  // Group accounts by Business Unit (in the same order as business_units,
  // which is already sorted by MTD spend descending), matching the
  // workbook's per-BU sectioning.
  const grouped = useMemo(() => {
    const byBu = new Map()
    for (const bu of businessUnits) byBu.set(bu.bu, { bu: bu.bu, totals: bu, rows: [] })
    for (const a of accounts) {
      const key = a.bu || 'Other'
      if (!byBu.has(key)) byBu.set(key, { bu: key, totals: { bu: key, mtd_cost: 0, last_month_cost: 0, account_count: 0 }, rows: [] })
      byBu.get(key).rows.push(a)
    }
    for (const group of byBu.values()) {
      group.rows.sort((x, y) => (y[period]?.subtotal || 0) - (x[period]?.subtotal || 0))
    }
    const groups = [...byBu.values()]
    groups.sort((x, y) => (y.totals.mtd_cost || 0) - (x.totals.mtd_cost || 0))
    return buFilter === 'All' ? groups : groups.filter(g => g.bu === buFilter)
  }, [accounts, businessUnits, buFilter, period])

  function toggleBu(bu) {
    setCollapsedBu(prev => ({ ...prev, [bu]: !prev[bu] }))
  }

  const totalForPeriod = period === 'mtd' ? data?.total_mtd_cost : data?.total_last_month_cost
  const trendPct = period === 'mtd' ? data?.mtd_vs_last_month_pct : data?.last_month_vs_prior_pct

  const topAccountsChart = useMemo(() => (
    [...accounts]
      .filter(a => !a.error)
      .map(a => ({ label: a.account_name, raw: a[period]?.subtotal }))
      .map(a => ({ label: a.label, value: isFiniteNum(a.raw) ? Math.max(0, a.raw) : 0 }))
      .sort((x, y) => y.value - x.value)
      .slice(0, 8)
  ), [accounts, period])

  return (
    <div style={{ padding: '24px 28px', maxWidth: 1400 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 18, flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>💰 Cost Analysis</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            AWS spend across the whole AWS Organization, grouped by Business Unit — Core account queried directly,
            all other accounts via <code>PortalReadOnlyRole</code>.
            {data && !data.is_mock && (
              <> Cached {formatAge(data.cache_age_seconds)}.</>
            )}
            {data?.is_mock && <> (local mock data — no real AWS calls in dev mode)</>}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={runAiAnalysis} disabled={loading}
            style={{ padding: '8px 16px', borderRadius: 7, border: 'none', background: 'linear-gradient(135deg, #1a4080 0%, #7c3aed 100%)', color: '#fff', fontWeight: 700, fontSize: 13, cursor: loading ? 'default' : 'pointer', opacity: loading ? 0.6 : 1 }}>
            🤖 AI Analysis
          </button>
        </div>
      </div>

      {err && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {err}
          {dataStale && <div style={{ marginTop: 4, fontWeight: 600 }}>⚠️ Showing previously-loaded data below — it may be out of date.</div>}
        </div>
      )}

      {data && !data.is_mock && (
        <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginBottom: 10 }}>
          ℹ️ "Month-to-date" is a partial-month figure (1st of this month through today) and is not directly comparable to a full month — use "Last full month" for like-for-like comparisons.
          {data.stale_fallback && <span style={{ color: COLORS.danger, fontWeight: 600 }}> This data is a last-known-good snapshot — the most recent refresh attempt failed for all accounts.</span>}
        </div>
      )}

      {data && (
        <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 220 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>Month-to-date</div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 4 }}>
              <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.textPrimary }}>{money(data.total_mtd_cost, currency)}</div>
              <TrendBadge pct={data.mtd_vs_last_month_pct} />
            </div>
            <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 5 }}>Current month from the 1st through today · % vs last full month</div>
          </div>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 220 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>Last full month</div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 4 }}>
              <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.textPrimary }}>{money(data.total_last_month_cost, currency)}</div>
              <TrendBadge pct={data.last_month_vs_prior_pct} />
            </div>
            <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 5 }}>Previous complete calendar month · % vs the month before</div>
          </div>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 180 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>Accounts</div>
            <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.textPrimary, marginTop: 4 }}>{data.account_count}</div>
            <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 5 }}>AWS accounts included in this report</div>
          </div>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 180 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>Business Units</div>
            <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.textPrimary, marginTop: 4 }}>{businessUnits.length}</div>
            <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 5 }}>Business units represented by the accounts</div>
          </div>
          {data.error_count > 0 && (
            <div style={{ ...CARD, padding: '14px 20px', minWidth: 180, borderColor: COLORS.danger + '40' }}>
              <div style={{ fontSize: 11, color: COLORS.danger, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>Errors</div>
              <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.danger, marginTop: 4 }}>{data.error_count}</div>
              <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 5 }}>Accounts whose cost data could not be loaded</div>
            </div>
          )}
        </div>
      )}

      {/* Charts row — top-spending accounts (bar) + service mix (pie),
          both for the currently-selected period. Gives senior management
          a fast visual read before drilling into the table. */}
      {data && accounts.length > 0 && (
        <div style={{ display: 'flex', gap: 14, marginBottom: 16, flexWrap: 'wrap' }}>
          <div style={{ ...CARD, padding: '16px 20px', flex: '1 1 380px' }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.3, marginBottom: 14 }}>
              Top Spending Accounts ({PERIOD_OPTIONS.find(p => p.key === period)?.label})
            </div>
            <BarChart items={topAccountsChart} currency={currency} />
          </div>
          <div style={{ ...CARD, padding: '16px 20px', flex: '1 1 420px' }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.3, marginBottom: 14 }}>
              Org-wide Spend by AWS Service (MTD)
            </div>
            {topServices.length === 0 ? (
              <div style={{ color: COLORS.textMuted, fontSize: 13 }}>No service-level data available.</div>
            ) : (
              <PieChart items={topServices.map(s => ({ label: s.service, value: s.amount }))} currency={currency} size={170} />
            )}
          </div>
        </div>
      )}

      {/* BU spend summary strip — quick visual matching the workbook's
          per-BU subtotal rows, before drilling into the detailed table. */}
      {businessUnits.length > 0 && (
        <div style={{ ...CARD, padding: '14px 18px', marginBottom: 16 }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 10 }}>
            Spend by Business Unit ({PERIOD_OPTIONS.find(p => p.key === period)?.label})
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
            {businessUnits.map(bu => {
              const amount = period === 'mtd' ? bu.mtd_cost : bu.last_month_cost
              const pct = totalForPeriod ? (amount / totalForPeriod) * 100 : 0
              return (
                <button key={bu.bu} onClick={() => setBuFilter(buFilter === bu.bu ? 'All' : bu.bu)}
                  style={{
                    display: 'flex', flexDirection: 'column', gap: 2, padding: '8px 14px', borderRadius: 8,
                    border: `1.5px solid ${buFilter === bu.bu ? COLORS.brand : COLORS.border}`,
                    background: buFilter === bu.bu ? COLORS.brandLight : '#fff',
                    cursor: 'pointer', textAlign: 'left', minWidth: 130,
                  }}>
                  <span style={{ fontSize: 12, fontWeight: 700, color: COLORS.textPrimary }}>{bu.bu}</span>
                  <span style={{ fontSize: 15, fontWeight: 700, color: COLORS.textPrimary }}>{money(amount, currency)}</span>
                  <span style={{ fontSize: 11, color: COLORS.textMuted }}>{bu.account_count} acct · {pct.toFixed(1)}%</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10, flexWrap: 'wrap', gap: 10 }}>
        <div style={{ display: 'flex', gap: 6 }}>
          {PERIOD_OPTIONS.map(p => (
            <button key={p.key} onClick={() => setPeriod(p.key)}
              style={{
                padding: '6px 14px', borderRadius: 7, fontSize: 12.5, fontWeight: 600, cursor: 'pointer',
                border: `1px solid ${period === p.key ? COLORS.brand : COLORS.border}`,
                background: period === p.key ? COLORS.brand : '#fff',
                color: period === p.key ? '#fff' : COLORS.textSecondary,
              }}>
              {p.label}
            </button>
          ))}
        </div>
        {buOptions.length > 1 && (
          <select value={buFilter} onChange={e => setBuFilter(e.target.value)}
            style={{ padding: '6px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 12.5, color: COLORS.textSecondary, background: '#fff' }}>
            {buOptions.map(bu => <option key={bu} value={bu}>{bu === 'All' ? 'All Business Units' : bu}</option>)}
          </select>
        )}
      </div>

      <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginBottom: 8 }}>
        💡 Click any account row to see its per-service cost breakdown.
      </div>

      <div style={{ ...CARD, overflowX: 'auto', overflowY: 'hidden' }}>
        <table style={{ width: '100%', minWidth: 1150, borderCollapse: 'collapse', fontSize: 12.5 }}>
          <thead>
            <tr style={{ background: '#f8fafc', borderBottom: `2px solid ${COLORS.border}` }}>
              <th style={{ padding: '10px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.3 }}>Account</th>
              <th style={{ padding: '10px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.3 }}>Account ID</th>
              {BREAKDOWN_COLS.map(c => (
                <th key={c.key} style={{ padding: '10px 14px', textAlign: 'right', color: '#475569', fontWeight: 600, fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.3 }}>{c.label}</th>
              ))}
              <th style={{ padding: '10px 14px', textAlign: 'right', color: '#475569', fontWeight: 600, fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.3 }}>Trend</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={BREAKDOWN_COLS.length + 3} style={{ padding: 40, textAlign: 'center', color: COLORS.textMuted }}>Loading…</td></tr>
            ) : grouped.length === 0 ? (
              <tr><td colSpan={BREAKDOWN_COLS.length + 3} style={{ padding: 40, textAlign: 'center', color: COLORS.textMuted }}>No cost data available</td></tr>
            ) : grouped.map(group => {
              const isCollapsed = !!collapsedBu[group.bu]
              const buAmount = period === 'mtd' ? group.totals.mtd_cost : group.totals.last_month_cost
              return (
                <React.Fragment key={group.bu}>
                  <tr onClick={() => toggleBu(group.bu)} style={{ cursor: 'pointer', background: '#eef2f7', borderTop: `1px solid ${COLORS.border}`, borderBottom: `1px solid ${COLORS.border}` }}>
                    <td colSpan={2} style={{ padding: '9px 14px', fontWeight: 700, color: COLORS.textPrimary }}>
                      <span style={{ display: 'inline-block', width: 14, transform: isCollapsed ? 'rotate(-90deg)' : 'none', transition: 'transform .15s' }}>▾</span>
                      {' '}{group.bu} <span style={{ color: COLORS.textMuted, fontWeight: 500 }}>({group.rows.length} account{group.rows.length === 1 ? '' : 's'})</span>
                    </td>
                    {BREAKDOWN_COLS.map((c, i) => (
                      <td key={c.key} style={{ padding: '9px 14px', textAlign: 'right', fontWeight: 700, color: COLORS.textPrimary }}>
                        {i === BREAKDOWN_COLS.length - 1 ? money(buAmount, currency) : ''}
                      </td>
                    ))}
                    <td />
                  </tr>
                  {!isCollapsed && group.rows.map(a => {
                    const p = a[period] || {}
                    const pct = period === 'mtd' ? a.mtd_vs_last_month_pct : a.last_month_vs_prior_pct
                    return (
                      <tr key={a.account_id} onClick={() => !a.error && setSelectedAccount(a)}
                        style={{ borderBottom: '1px solid #f1f5f9', cursor: a.error ? 'default' : 'pointer' }}
                        onMouseEnter={e => { if (!a.error) e.currentTarget.style.background = '#f8fafc' }}
                        onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}>
                        <td style={{ padding: '8px 14px 8px 32px', color: COLORS.textPrimary, fontWeight: 600 }}>
                          {a.account_id === coreAccountId && coreAccountId && '⭐ '}{a.account_name}
                        </td>
                        <td style={{ padding: '8px 14px', color: '#64748b', fontFamily: 'monospace', fontSize: 11.5 }}>{a.account_id}</td>
                        {a.error ? (
                          <td colSpan={BREAKDOWN_COLS.length} style={{ padding: '8px 14px' }}>
                            <span title={a.error} style={{ color: COLORS.danger, fontSize: 11.5, fontWeight: 600, cursor: 'help' }}>⚠️ Fetch failed</span>
                          </td>
                        ) : BREAKDOWN_COLS.map(c => (
                          <td key={c.key} style={{
                            padding: '8px 14px', textAlign: 'right',
                            fontWeight: c.bold ? 700 : 400,
                            color: c.key === 'credits' && p.credits < 0 ? COLORS.success
                              : c.manual && p.adjustments ? '#b45309'
                              : (c.bold ? COLORS.textPrimary : '#475569'),
                          }}>
                            {c.manual && !p.adjustments ? '—' : money(p[c.key], a.currency || currency)}
                          </td>
                        ))}
                        <td style={{ padding: '8px 14px', textAlign: 'right' }}>
                          {!a.error && <TrendBadge pct={pct} />}
                        </td>
                      </tr>
                    )
                  })}
                </React.Fragment>
              )
            })}
          </tbody>
          {grouped.length > 0 && (
            <tfoot>
              <tr style={{ borderTop: `2px solid ${COLORS.border}`, background: '#f8fafc' }}>
                <td colSpan={2 + BREAKDOWN_COLS.length - 1} style={{ padding: '10px 14px', fontWeight: 700, color: COLORS.textPrimary }}>
                  Total ({buFilter === 'All' ? 'all business units' : buFilter})
                </td>
                <td style={{ padding: '10px 14px', textAlign: 'right', fontWeight: 700, color: COLORS.textPrimary }}>
                  {money(grouped.reduce((sum, g) => sum + (g.rows.reduce((s2, a) => s2 + ((a[period]?.subtotal) || 0), 0)), 0), currency)}
                </td>
                <td />
              </tr>
            </tfoot>
          )}
        </table>
      </div>

      {selectedAccount && (
        <AccountDetailModal
          account={selectedAccount} period={period} currency={currency}
          canManageAdjustments={!!user?.is_admin}
          onAdjustmentsChanged={() => load(true)}
          onClose={() => setSelectedAccount(null)}
        />
      )}

      {aiOpen && (
        <AiSummaryModal
          summary={aiSummary} loading={aiLoading} error={aiError}
          onClose={closeAiModal} onRetry={runAiAnalysis}
        />
      )}
    </div>
  )
}
