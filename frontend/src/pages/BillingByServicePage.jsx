import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../api.js'
import { COLORS, CARD } from '../utils/theme.js'
import { getPageCache, setPageCache } from '../utils/pageCache.js'

const CACHE_KEY = 'billing-by-service'

// ── Billing by Service — per-account, per-month AWS spend by service ────────
// Ported from the standalone billing_by_service.py CLI script: mirrors the
// AWS Billing Console's "Bills -> Charges by service" view. Any logged-in
// portal user can view/export; this is read-only (no adjustments here —
// see the Cost Analysis page for manual adjustment entries).

function isFiniteNum(n) {
  return typeof n === 'number' && Number.isFinite(n)
}

function money(amount) {
  if (!isFiniteNum(amount)) return '—'
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(amount)
  } catch {
    return `$${amount.toFixed(2)}`
  }
}

function currentMonthKey() {
  return new Date().toISOString().slice(0, 7) // "YYYY-MM"
}

function formatAge(seconds) {
  if (!seconds || seconds < 60) return 'just now'
  const mins = Math.floor(seconds / 60)
  return `${mins} min${mins === 1 ? '' : 's'} ago`
}

// Cache is keyed per (months, accountIds) filter combination — mirrors
// the backend's own per-combination cache key (see billing_by_service.py's
// _cache_key()), so switching back to a filter combination the user
// already viewed in this tab also renders instantly.
function reportCacheKey(months, accountIds) {
  return `${CACHE_KEY}:${JSON.stringify(months)}:${JSON.stringify([...accountIds].sort())}`
}

// ── AI executive summary (auto — no button click required) ─────────────────
// Mirrors the Dashboard's inline "Executive Summary" card, but here it
// always tracks whatever (months, accounts) filter combination the user
// currently has selected on this page, and regenerates automatically the
// moment that filter changes (see the effect below). Backend endpoint is
// GET /api/billing-by-service/ai-summary — cached 15 minutes server-side
// per exact (accounts, months) combination (see main.py's
// _BILLING_AI_SUMMARY_CACHE), so re-selecting a filter combination already
// viewed in this session resolves instantly from that cache.
const TREND_LABEL = { increasing: '📈 Increasing', decreasing: '📉 Decreasing', stable: '➡️ Stable' }

function AiSummaryCard({ summary, loading, error, onRegenerate }) {
  return (
    <div style={{ ...CARD, padding: '16px 20px', marginBottom: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <h3 style={{ margin: 0, fontSize: 14.5, color: COLORS.brandDark, fontWeight: 700 }}>🤖 AI Executive Summary</h3>
        <button onClick={onRegenerate} disabled={loading}
          style={{ border: 'none', background: 'none', color: COLORS.brand, fontSize: 12, fontWeight: 700, cursor: loading ? 'default' : 'pointer', padding: 0 }}>
          {loading ? '⏳ Generating…' : '🔄 Regenerate'}
        </button>
      </div>

      {loading && !summary && (
        <div style={{ padding: '14px 0', textAlign: 'center', color: COLORS.textMuted, fontSize: 13 }}>⏳ Generating summary for the selected filters…</div>
      )}

      {error && !loading && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '9px 12px', color: COLORS.danger, fontSize: 12.5 }}>
          {error}
        </div>
      )}

      {summary && (
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
            {summary.model_used ? `Generated by ${summary.model_used} · ` : ''}
            {summary.generated_at ? new Date(summary.generated_at).toLocaleString() : ''}
          </div>
        </>
      )}
    </div>
  )
}

// ── Total cost card — sum of every selected account × every selected
// month, for the current filter combination. Purely a client-side sum
// over the report already loaded (accounts[].totals[month]), so it
// updates instantly the moment a new filter is applied — no extra
// request needed.
function TotalCostCard({ months, monthLabels, perMonthTotals, grandTotal, failedAccountCount, accountCount }) {
  const showPerMonthBreakdown = months.length > 1
  return (
    <div style={{ ...CARD, padding: '16px 20px', marginBottom: 16, borderLeft: `4px solid ${COLORS.brand}` }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 14 }}>
        <div>
          <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 4 }}>
            Total Cost — {accountCount} account{accountCount === 1 ? '' : 's'} × {months.length} month{months.length === 1 ? '' : 's'}
          </div>
          <div style={{ fontSize: 28, fontWeight: 800, color: COLORS.textPrimary }}>{money(grandTotal)}</div>
          {failedAccountCount > 0 && (
            <div style={{ fontSize: 11.5, color: COLORS.danger, marginTop: 4 }}>
              ⚠️ {failedAccountCount} account{failedAccountCount === 1 ? '' : 's'} failed to fetch and {failedAccountCount === 1 ? 'is' : 'are'} excluded from this total.
            </div>
          )}
        </div>

        {showPerMonthBreakdown && (
          <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap' }}>
            {monthLabels.map((label, i) => (
              <div key={label} style={{ textAlign: 'right' }}>
                <div style={{ fontSize: 10, color: COLORS.textMuted, textTransform: 'uppercase', fontWeight: 700 }}>{label}</div>
                <div style={{ fontSize: 14, fontWeight: 700, color: COLORS.textPrimary }}>{money(perMonthTotals[months[i]])}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default function BillingByServicePage() {
  const [allAccounts, setAllAccounts] = useState([])
  const [selectedAccountIds, setSelectedAccountIds] = useState([]) // empty = all
  const [months, setMonths] = useState([currentMonthKey()])
  const [monthInput, setMonthInput] = useState(currentMonthKey())

  // Seed from the in-tab page cache (see utils/pageCache.js) so
  // re-visiting this page with the same (default) filters renders
  // instantly instead of a blank "Loading…" screen.
  const initialCached = getPageCache(reportCacheKey([currentMonthKey()], []))
  const [report, setReport] = useState(initialCached || null)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [err, setErr] = useState('')
  const [collapsed, setCollapsed] = useState({})
  const [accountPickerOpen, setAccountPickerOpen] = useState(false)

  // AI executive summary — always tracks the current (months,
  // selectedAccountIds) filter; regenerated automatically whenever either
  // changes (see the effect below), no button click required.
  const [aiSummary, setAiSummary] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState('')

  const abortRef = React.useRef(null)
  const seqRef = React.useRef(0)
  const mountedRef = React.useRef(true)
  const aiAbortRef = React.useRef(null)
  const aiSeqRef = React.useRef(0)

  useEffect(() => {
    mountedRef.current = true
    api.getBillingByServiceAccounts()
      .then(d => { if (mountedRef.current) setAllAccounts(d.accounts || []) })
      .catch(() => {})
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
    }
  }, [])

  const load = useCallback((refresh = false) => {
    if (months.length === 0) return
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const mySeq = ++seqRef.current
    const key = reportCacheKey(months, selectedAccountIds)

    // If this exact filter combination was already viewed in this tab,
    // render it immediately (no loading spinner) while refreshing in the
    // background. Otherwise (a genuinely new filter combination, or an
    // explicit Refresh click), show the normal loading/refreshing state.
    const cachedForThisFilter = getPageCache(key)
    if (cachedForThisFilter && !refresh) {
      setReport(cachedForThisFilter)
    } else if (refresh) {
      setRefreshing(true)
    } else {
      setLoading(true)
    }
    setErr('')
    api.getBillingByServiceReport(months, selectedAccountIds, refresh, controller.signal)
      .then(d => {
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setReport(d)
        setPageCache(key, d)
      })
      .catch(e => {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setErr(e?.response?.data?.detail || 'Could not load billing-by-service data')
      })
      .finally(() => {
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setLoading(false)
        setRefreshing(false)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [months, selectedAccountIds])

  useEffect(() => { load(false) }, [load])

  // No manual Refresh button on this page — data silently re-fetches
  // every 6 hours in the background instead. force_refresh=false rides
  // the server-side cache (billing_by_service.py's _CACHE_TTL_SECONDS)
  // rather than always bypassing it.
  useEffect(() => {
    const id = setInterval(() => { load(false) }, 6 * 60 * 60 * 1000)
    return () => clearInterval(id)
  }, [load])

  // Auto AI summary — regenerates every time `months` or
  // `selectedAccountIds` changes (i.e. whenever the user applies a new
  // filter), with no button click needed. Uses the same
  // AbortController-per-request + sequence-number pattern as `load`
  // above so a rapid filter change cancels the now-stale in-flight
  // request instead of racing it.
  const loadAiSummary = useCallback((refresh = false) => {
    if (months.length === 0) return
    aiAbortRef.current?.abort()
    const controller = new AbortController()
    aiAbortRef.current = controller
    const mySeq = ++aiSeqRef.current

    setAiLoading(true)
    setAiError('')
    api.getBillingByServiceAiSummary(months, selectedAccountIds, refresh, controller.signal)
      .then(result => {
        if (!mountedRef.current || mySeq !== aiSeqRef.current) return
        setAiSummary(result)
      })
      .catch(e => {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
        if (!mountedRef.current || mySeq !== aiSeqRef.current) return
        setAiError(e?.response?.data?.detail || 'Could not generate AI executive summary')
      })
      .finally(() => {
        if (!mountedRef.current || mySeq !== aiSeqRef.current) return
        setAiLoading(false)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [months, selectedAccountIds])

  useEffect(() => { loadAiSummary(false) }, [loadAiSummary])

  function addMonth() {
    if (!monthInput) return
    if (months.includes(monthInput)) return
    if (months.length >= 12) { setErr('You can compare up to 12 months at once.'); return }
    setMonths(prev => [...prev, monthInput].sort())
    setErr('')
  }

  function removeMonth(m) {
    setMonths(prev => prev.filter(x => x !== m))
  }

  function toggleAccount(id) {
    setSelectedAccountIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }

  function selectAllAccounts() { setSelectedAccountIds([]) }

  async function handleExport() {
    if (months.length === 0) return
    setExporting(true)
    setErr('')
    try {
      await api.exportBillingByServiceXlsx(months, selectedAccountIds)
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not export billing-by-service Excel file')
    } finally {
      setExporting(false)
    }
  }

  function toggleCollapse(accountId) {
    setCollapsed(prev => ({ ...prev, [accountId]: !prev[accountId] }))
  }

  const monthLabels = report?.month_labels || months
  const accounts = report?.accounts || []

  // Grand total — sum of every account's spend across every selected
  // month, for the current filter combination. Accounts whose fetch
  // failed (account.error set) are excluded from the sum since they have
  // no reliable totals to add. Recomputed automatically whenever the
  // report or filters change — no extra button needed, it's just a
  // client-side reduction over data already on the page.
  const perMonthTotals = useMemo(() => {
    const totals = {}
    for (const m of months) {
      totals[m] = accounts.reduce((sum, acc) => {
        if (acc.error) return sum
        const v = acc.totals?.[m]
        return sum + (isFiniteNum(v) ? v : 0)
      }, 0)
    }
    return totals
  }, [accounts, months])

  const grandTotal = useMemo(
    () => Object.values(perMonthTotals).reduce((a, b) => a + b, 0),
    [perMonthTotals]
  )

  const failedAccountCount = accounts.filter(a => a.error).length

  const accountPickerLabel = useMemo(() => {
    if (selectedAccountIds.length === 0) return `All accounts (${allAccounts.length})`
    return `${selectedAccountIds.length} account${selectedAccountIds.length === 1 ? '' : 's'} selected`
  }, [selectedAccountIds, allAccounts])

  return (
    <div style={{ padding: '24px 28px', maxWidth: 1400 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 18, flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>🧾 Billing by Service</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            Per-account, per-month AWS spend broken down by service — matches the Billing Console's "Charges by service" view.
            {report && !report.is_mock && <> Cached {formatAge(report.cache_age_seconds)}.</>}
            {report?.is_mock && <> (local mock data — no real AWS calls in dev mode)</>}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={handleExport} disabled={exporting || loading || months.length === 0}
            style={{ padding: '8px 16px', borderRadius: 7, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontWeight: 600, fontSize: 13, cursor: (exporting || loading) ? 'default' : 'pointer' }}>
            {exporting ? '⏳ Exporting…' : '📥 Export to Excel'}
          </button>
        </div>
      </div>

      {err && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {err}
        </div>
      )}

      {/* Controls: months + account picker */}
      <div style={{ ...CARD, padding: '14px 18px', marginBottom: 16, display: 'flex', gap: 24, flexWrap: 'wrap' }}>
        <div style={{ minWidth: 260 }}>
          <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 8 }}>
            Months to compare
          </div>
          <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
            <input type="month" value={monthInput} onChange={e => setMonthInput(e.target.value)}
              style={{ padding: '6px 9px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5 }} />
            <button onClick={addMonth}
              style={{ padding: '6px 12px', borderRadius: 6, border: `1px solid ${COLORS.brand}`, background: '#fff', color: COLORS.brand, fontSize: 12, fontWeight: 700, cursor: 'pointer' }}>
              + Add
            </button>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {months.map(m => (
              <span key={m} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 8px', borderRadius: 20, background: COLORS.brandLight, color: COLORS.brand, fontSize: 12, fontWeight: 600 }}>
                {m}
                {months.length > 1 && (
                  <button onClick={() => removeMonth(m)} style={{ border: 'none', background: 'none', cursor: 'pointer', color: COLORS.brand, fontSize: 13, lineHeight: 1, padding: 0 }}>×</button>
                )}
              </span>
            ))}
          </div>
        </div>

        <div style={{ minWidth: 260, position: 'relative' }}>
          <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 8 }}>
            Accounts
          </div>
          <button onClick={() => setAccountPickerOpen(o => !o)}
            style={{ padding: '6px 14px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textPrimary, fontSize: 12.5, fontWeight: 600, cursor: 'pointer', minWidth: 220, textAlign: 'left' }}>
            {accountPickerLabel} ▾
          </button>
          {accountPickerOpen && (
            <div style={{ position: 'absolute', top: '100%', left: 0, marginTop: 4, background: '#fff', border: `1px solid ${COLORS.border}`, borderRadius: 8, boxShadow: '0 6px 20px rgba(0,0,0,0.12)', zIndex: 50, width: 320, maxHeight: 320, overflowY: 'auto', padding: 10 }}>
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
      </div>

      {report && accounts.length > 0 && (
        <TotalCostCard
          months={months} monthLabels={monthLabels}
          perMonthTotals={perMonthTotals} grandTotal={grandTotal}
          failedAccountCount={failedAccountCount} accountCount={accounts.length}
        />
      )}

      <AiSummaryCard summary={aiSummary} loading={aiLoading} error={aiError} onRegenerate={() => loadAiSummary(true)} />

      {loading && !report && (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted }}>Loading…</div>
      )}

      {!loading && accounts.length === 0 && report && (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted }}>No billing data available for the selected accounts/months.</div>
      )}

      {accounts.map(account => {
        const isCollapsed = !!collapsed[account.account_id]
        return (
          <div key={account.account_id} style={{ ...CARD, marginBottom: 14, overflow: 'hidden' }}>
            <div onClick={() => toggleCollapse(account.account_id)}
              style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px 18px', cursor: 'pointer', background: '#f8fafc', borderBottom: isCollapsed ? 'none' : `1px solid ${COLORS.border}` }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ display: 'inline-block', width: 12, transform: isCollapsed ? 'rotate(-90deg)' : 'none', transition: 'transform .15s', color: COLORS.textMuted }}>▾</span>
                <span style={{ fontWeight: 700, color: COLORS.textPrimary, fontSize: 14 }}>{account.alias}</span>
                <span style={{ color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11.5 }}>{account.account_id}</span>
              </div>
              {account.error ? (
                <span title={account.error} style={{ color: COLORS.danger, fontSize: 11.5, fontWeight: 600, cursor: 'help' }}>⚠️ Fetch failed</span>
              ) : (
                <div style={{ display: 'flex', gap: 14 }}>
                  {monthLabels.map((label, i) => (
                    <div key={label} style={{ textAlign: 'right' }}>
                      <div style={{ fontSize: 10, color: COLORS.textMuted, textTransform: 'uppercase', fontWeight: 700 }}>{label}</div>
                      <div style={{ fontSize: 13, fontWeight: 700, color: COLORS.textPrimary }}>{money(account.totals?.[months[i]])}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {!isCollapsed && !account.error && (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', minWidth: 500 + months.length * 110, borderCollapse: 'collapse', fontSize: 12.5 }}>
                  <thead>
                    <tr style={{ background: '#fafbfc', borderBottom: `1px solid ${COLORS.border}` }}>
                      <th style={{ padding: '8px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Category</th>
                      <th style={{ padding: '8px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Service</th>
                      {monthLabels.map(label => (
                        <th key={label} style={{ padding: '8px 14px', textAlign: 'right', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>{label}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(account.rows || []).length === 0 ? (
                      <tr><td colSpan={2 + months.length} style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted }}>No service-level spend recorded.</td></tr>
                    ) : account.rows.map((row, idx) => (
                      <tr key={idx} style={{ borderBottom: '1px solid #f1f5f9' }}>
                        <td style={{ padding: '7px 14px', color: row.category === 'Marketplace' ? COLORS.warning : COLORS.textMuted, fontSize: 11.5, fontWeight: 600 }}>{row.category}</td>
                        <td style={{ padding: '7px 14px', color: COLORS.textPrimary }}>{row.service}</td>
                        {months.map(m => (
                          <td key={m} style={{ padding: '7px 14px', textAlign: 'right', color: '#475569' }}>{money(row.amounts?.[m])}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                  <tfoot>
                    <tr style={{ borderTop: `2px solid ${COLORS.border}`, background: '#f8fafc' }}>
                      <td colSpan={2} style={{ padding: '9px 14px', fontWeight: 700, color: COLORS.textPrimary }}>Grand total</td>
                      {months.map(m => (
                        <td key={m} style={{ padding: '9px 14px', textAlign: 'right', fontWeight: 700, color: COLORS.textPrimary }}>{money(account.totals?.[m])}</td>
                      ))}
                    </tr>
                  </tfoot>
                </table>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
