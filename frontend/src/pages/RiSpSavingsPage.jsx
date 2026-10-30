import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'
import { COLORS, CARD } from '../utils/theme.js'
import { getPageCache, setPageCache } from '../utils/pageCache.js'

const CACHE_KEY = 'ri-sp-savings'

// ── Savings Plan Cost ─────────────────────────────────────────
// Per-account, per-month realized savings from Savings Plans, sourced
// directly from AWS Cost Explorer's GetCostAndUsage API filtered to
// RECORD_TYPE=SavingsPlanCoveredUsage and grouped by LINKED_ACCOUNT +
// SERVICE — the same methodology behind the Billing Console's own
// Bills > Savings breakdown. See backend/app/ri_sp_savings.py for the
// full explanation of why the commitment-utilization APIs
// (GetSavingsPlansUtilization/GetReservationUtilization) are NOT used.
// Classic Reserved Instances currently have no active commitments
// org-wide, so the RI side of this report is $0 for every account.
// Any logged-in portal user can view (read-only).
//
// There is no manual "Refresh" button on this page — the AWS data is
// refreshed automatically once a day at 08:00 HKT (see
// ri_sp_savings._current_refresh_epoch()), so nothing here ever needs a
// user-triggered refresh. The Reseller On-Demand Cost comparison box
// (admin-only to edit) is layered on top of that report per month and
// updates immediately on save, independent of the daily AWS refresh.

function isFiniteNum(n) {
  return typeof n === 'number' && Number.isFinite(n)
}

function money(amount, currency = 'USD') {
  if (!isFiniteNum(amount)) return '—'
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: currency || 'USD', maximumFractionDigits: 2 }).format(amount)
  } catch {
    return `$${amount.toFixed(2)}`
  }
}

function pct(n) {
  if (!isFiniteNum(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(1)}%`
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
// the backend's own per-combination cache key (see ri_sp_savings.py's
// _cache_key()).
function reportCacheKey(months, accountIds) {
  return `${CACHE_KEY}:${JSON.stringify(months)}:${JSON.stringify([...accountIds].sort())}`
}

// ── Reseller On-Demand Cost comparison — admin-only input, PER ACCOUNT ────
// Compares what the reseller invoiced for one AWS account (manually
// entered, since AWS has no visibility into a reseller's own invoice)
// against that same account's own AWS On-Demand Equivalent figure for
// that month. `comparison` is the `reseller_comparison` object already
// attached server-side to that account's row (or null if no admin has
// entered a value for this account+month yet) — see
// app/ri_sp_savings.py's _build_reseller_comparison(). Rendered inline as
// a table cell so each account can carry its own reseller-cost entry
// (different accounts can be on different reseller contracts).
function ResellerCostCell({ accountId, month, comparison, canManage, onChanged }) {
  const [editing, setEditing] = useState(false)
  const [amountInput, setAmountInput] = useState('')
  const [currencyInput, setCurrencyInput] = useState('USD')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')

  function startEdit(e) {
    e.stopPropagation()
    setAmountInput(comparison ? String(comparison.reseller_ondemand_cost) : '')
    setCurrencyInput(comparison?.currency || 'USD')
    setFormError('')
    setEditing(true)
  }

  async function save(e) {
    e.stopPropagation()
    const amountNum = parseFloat(amountInput)
    if (!Number.isFinite(amountNum) || amountNum < 0) { setFormError('Amount must be zero or a positive number.'); return }
    if (!/^[A-Za-z]{3}$/.test(currencyInput || '')) { setFormError('Currency must be a 3-letter code, e.g. USD.'); return }
    setSaving(true)
    setFormError('')
    try {
      await api.setRiSpSavingsResellerCost(accountId, month, amountNum, currencyInput.toUpperCase())
      setEditing(false)
      onChanged?.()
    } catch (err) {
      setFormError(err?.response?.data?.detail || 'Could not save reseller cost')
    } finally {
      setSaving(false)
    }
  }

  async function clear(e) {
    e.stopPropagation()
    if (!window.confirm(`Clear the reseller cost entered for this account (${month})?`)) return
    setSaving(true)
    try {
      await api.deleteRiSpSavingsResellerCost(accountId, month)
      setEditing(false)
      onChanged?.()
    } catch (err) {
      setFormError(err?.response?.data?.detail || 'Could not clear reseller cost')
    } finally {
      setSaving(false)
    }
  }

  const INPUT_SM = { padding: '3px 6px', borderRadius: 5, border: `1px solid ${COLORS.border}`, fontSize: 11.5, boxSizing: 'border-box' }

  if (editing) {
    return (
      <td colSpan={3} style={{ padding: '6px 14px' }} onClick={e => e.stopPropagation()}>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          <input type="number" step="0.01" value={amountInput} onChange={e => setAmountInput(e.target.value)}
            placeholder="Reseller cost" style={{ ...INPUT_SM, width: 110 }} />
          <input type="text" value={currencyInput} onChange={e => setCurrencyInput(e.target.value.toUpperCase())}
            maxLength={3} style={{ ...INPUT_SM, width: 46 }} />
          <button onClick={save} disabled={saving}
            style={{ padding: '4px 10px', borderRadius: 5, border: 'none', background: COLORS.brand, color: '#fff', fontSize: 11, fontWeight: 700, cursor: saving ? 'default' : 'pointer', opacity: saving ? 0.6 : 1 }}>
            {saving ? '…' : 'Save'}
          </button>
          <button onClick={e => { e.stopPropagation(); setEditing(false) }} disabled={saving}
            style={{ padding: '4px 10px', borderRadius: 5, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontSize: 11, fontWeight: 600, cursor: 'pointer' }}>
            Cancel
          </button>
          {comparison && (
            <button onClick={clear} disabled={saving}
              style={{ padding: '4px 10px', borderRadius: 5, border: `1px solid ${COLORS.danger}40`, background: '#fff', color: COLORS.danger, fontSize: 11, fontWeight: 600, cursor: 'pointer' }}>
              Clear
            </button>
          )}
        </div>
        {formError && <div style={{ color: COLORS.danger, fontSize: 11, marginTop: 4, textAlign: 'right' }}>{formError}</div>}
      </td>
    )
  }

  if (!comparison) {
    return (
      <td colSpan={3} style={{ padding: '7px 14px', textAlign: 'right', color: COLORS.textMuted }} onClick={e => e.stopPropagation()}>
        {canManage ? (
          <button onClick={startEdit}
            style={{ padding: '3px 9px', borderRadius: 5, border: `1px solid ${COLORS.brand}`, background: '#fff', color: COLORS.brand, fontSize: 11, fontWeight: 700, cursor: 'pointer' }}>
            + Reseller cost
          </button>
        ) : '—'}
      </td>
    )
  }

  return (
    <>
      <td style={{ padding: '7px 14px', textAlign: 'right', color: '#475569' }} onClick={e => e.stopPropagation()}>
        {money(comparison.reseller_ondemand_cost, comparison.currency)}
        {canManage && (
          <button onClick={startEdit} title="Edit reseller cost"
            style={{ marginLeft: 6, border: 'none', background: 'none', cursor: 'pointer', fontSize: 11 }}>✏️</button>
        )}
      </td>
      <td style={{ padding: '7px 14px', textAlign: 'right', color: comparison.diff <= 0 ? COLORS.success : COLORS.danger, fontWeight: 600 }}>
        {money(comparison.diff)}
      </td>
      <td style={{ padding: '7px 14px', textAlign: 'right', fontWeight: 700, color: comparison.discount_pct === null ? COLORS.textMuted : comparison.discount_pct >= 0 ? COLORS.success : COLORS.danger }}>
        {comparison.discount_pct === null ? '—' : pct(comparison.discount_pct)}
      </td>
    </>
  )
}

export default function RiSpSavingsPage() {
  const { user } = useAuth()
  const canManageResellerCost = !!user?.is_admin

  const [allAccounts, setAllAccounts] = useState([])
  const [selectedAccountIds, setSelectedAccountIds] = useState([]) // empty = all
  const [months, setMonths] = useState([currentMonthKey()])
  const [monthInput, setMonthInput] = useState(currentMonthKey())

  const initialCached = getPageCache(reportCacheKey([currentMonthKey()], []))
  const [report, setReport] = useState(initialCached || null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [expanded, setExpanded] = useState({})
  const [accountPickerOpen, setAccountPickerOpen] = useState(false)

  const abortRef = React.useRef(null)
  const seqRef = React.useRef(0)
  const mountedRef = React.useRef(true)

  useEffect(() => {
    mountedRef.current = true
    api.getRiSpSavingsAccounts()
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

    const cachedForThisFilter = getPageCache(key)
    if (cachedForThisFilter && !refresh) {
      setReport(cachedForThisFilter)
    } else {
      setLoading(true)
    }
    setErr('')
    api.getRiSpSavingsReport(months, selectedAccountIds, refresh, controller.signal)
      .then(d => {
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setReport(d)
        setPageCache(key, d)
      })
      .catch(e => {
        if (e?.code === 'ERR_CANCELED' || e?.name === 'CanceledError') return
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setErr(e?.response?.data?.detail || 'Could not load Savings Plan cost data')
      })
      .finally(() => {
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setLoading(false)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [months, selectedAccountIds])

  useEffect(() => { load(false) }, [load])

  // There is no manual "Refresh" button on this page — the underlying AWS
  // data only actually changes once a day at 08:00 HKT (see the backend's
  // daily refresh-epoch cache), so a tab left open only needs to re-poll
  // occasionally to eventually pick that up; anything more frequent than
  // hourly just re-hits the server-side cache for no benefit.
  useEffect(() => {
    const id = setInterval(() => { load(false) }, 60 * 60 * 1000)
    return () => clearInterval(id)
  }, [load])

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

  function toggleExpand(key) {
    setExpanded(prev => ({ ...prev, [key]: !prev[key] }))
  }

  const monthReports = report?.months || []
  const latestMonthReport = monthReports[monthReports.length - 1] || null

  const accountPickerLabel = useMemo(() => {
    if (selectedAccountIds.length === 0) return `All accounts (${allAccounts.length})`
    return `${selectedAccountIds.length} account${selectedAccountIds.length === 1 ? '' : 's'} selected`
  }, [selectedAccountIds, allAccounts])

  return (
    <div style={{ padding: '24px 28px', maxWidth: 1400 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 18, flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>💵 Savings Plan Cost</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            Per-account, per-service realized savings from Savings Plans, matching the Billing Console's
            "Bills &gt; Savings" breakdown (AWS Cost Explorer, RECORD_TYPE = SavingsPlanCoveredUsage).
            Data refreshes automatically once a day at 08:00 HKT — no manual refresh needed.
            {report && !report.is_mock && <> Last fetched {formatAge(report.cache_age_seconds)}.</>}
            {report?.is_mock && <> (local mock data — no real AWS calls in dev mode)</>}
          </div>
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

      {loading && !report && (
        <div style={{ ...CARD, padding: 40, textAlign: 'center', color: COLORS.textMuted }}>Loading…</div>
      )}

      {/* Org-wide KPI cards for the latest requested month */}
      {latestMonthReport && (
        <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 200 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>
              Total Saved ({latestMonthReport.month_label})
            </div>
            <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.success, marginTop: 4 }}>
              {money(latestMonthReport.totals?.total_net_savings)}
            </div>
          </div>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 200 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>
              On-Demand Equivalent
            </div>
            <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.textPrimary, marginTop: 4 }}>
              {money(latestMonthReport.totals?.sp_on_demand_equivalent)}
            </div>
          </div>
          <div style={{ ...CARD, padding: '14px 20px', minWidth: 200 }}>
            <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>
              Amortized Cost (Actually Paid)
            </div>
            <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.textPrimary, marginTop: 4 }}>
              {money(latestMonthReport.totals?.sp_amortized_cost)}
            </div>
          </div>
          {latestMonthReport.totals?.error_count > 0 && (
            <div style={{ ...CARD, padding: '14px 20px', minWidth: 160, borderColor: COLORS.danger + '40' }}>
              <div style={{ fontSize: 11, color: COLORS.danger, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>Errors</div>
              <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.danger, marginTop: 4 }}>{latestMonthReport.totals.error_count}</div>
            </div>
          )}
        </div>
      )}

      <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginBottom: 10 }}>
        ℹ️ "Net savings" = On-Demand-equivalent cost minus what was actually paid (amortized) for Savings-Plan-covered
        usage, per AWS Cost Explorer — this is exactly the Billing Console's own Bills &gt; Savings figure. Click an
        account row to expand its per-service breakdown. Classic Reserved Instances currently have no active
        commitments org-wide, so RI savings show as $0 for every account.
      </div>

      {/* One table per requested month, one row per account (expandable
          for the per-service breakdown), Savings-Plan-only. Each account
          row also carries its own Reseller On-Demand Cost comparison
          columns (admin-only to edit — see ResellerCostCell), since
          different accounts can be on different reseller contracts. */}
      {monthReports.map(monthReport => {
        const accounts = monthReport.accounts || []
        const resellerTotals = monthReport.totals?.reseller_comparison || null
        return (
          <React.Fragment key={monthReport.month}>
          <div style={{ ...CARD, marginBottom: 14, overflow: 'hidden' }}>
            <div style={{ padding: '12px 18px', background: '#f8fafc', borderBottom: `1px solid ${COLORS.border}`, fontWeight: 700, color: COLORS.textPrimary, fontSize: 14, display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'center' }}>
              <span>{monthReport.month_label}</span>
              <span style={{ fontWeight: 700, color: COLORS.success }}>
                Total saved: {money(monthReport.totals?.total_net_savings)}
              </span>
              {resellerTotals && (
                <span style={{ fontWeight: 600, fontSize: 12, color: COLORS.textSecondary }}>
                  · Reseller total ({resellerTotals.accounts_with_entry} account{resellerTotals.accounts_with_entry === 1 ? '' : 's'}):
                  {' '}{money(resellerTotals.reseller_ondemand_cost, resellerTotals.currency)} vs AWS {money(resellerTotals.aws_ondemand_equivalent)}
                  {' '}(<span style={{ color: resellerTotals.discount_pct === null ? COLORS.textMuted : resellerTotals.discount_pct >= 0 ? COLORS.success : COLORS.danger }}>
                    {resellerTotals.discount_pct === null ? '—' : pct(resellerTotals.discount_pct)}
                  </span>)
                </span>
              )}
            </div>
            {!loading && accounts.length === 0 ? (
              <div style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted }}>No accounts to show for this month.</div>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', minWidth: 1040, borderCollapse: 'collapse', fontSize: 12.5 }}>
                  <thead>
                    <tr style={{ background: '#fafbfc', borderBottom: `1px solid ${COLORS.border}` }}>
                      <th style={{ padding: '8px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase', width: 32 }}></th>
                      <th style={{ padding: '8px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Account</th>
                      <th style={{ padding: '8px 14px', textAlign: 'right', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>On-Demand Equiv.</th>
                      <th style={{ padding: '8px 14px', textAlign: 'right', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Amortized Cost</th>
                      <th style={{ padding: '8px 14px', textAlign: 'right', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Net Savings</th>
                      <th style={{ padding: '8px 14px', textAlign: 'right', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Reseller Cost</th>
                      <th style={{ padding: '8px 14px', textAlign: 'right', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Diff</th>
                      <th style={{ padding: '8px 14px', textAlign: 'right', color: '#475569', fontWeight: 600, fontSize: 11.5, textTransform: 'uppercase' }}>Discount %</th>
                    </tr>
                  </thead>
                  <tbody>
                    {accounts.map(account => {
                      const rowKey = `${monthReport.month}:${account.account_id}`
                      const isOpen = !!expanded[rowKey]
                      const services = account.services || []
                      const hasServices = services.length > 0 && !account.error
                      return (
                        <React.Fragment key={rowKey}>
                          <tr
                            onClick={() => hasServices && toggleExpand(rowKey)}
                            style={{ borderBottom: '1px solid #f1f5f9', cursor: hasServices ? 'pointer' : 'default' }}>
                            <td style={{ padding: '7px 14px', textAlign: 'center', color: COLORS.textMuted, fontSize: 11 }}>
                              {hasServices ? (isOpen ? '▾' : '▸') : ''}
                            </td>
                            <td style={{ padding: '7px 14px' }}>
                              <span style={{ fontWeight: 600, color: COLORS.textPrimary }}>{account.alias}</span>
                              <span style={{ marginLeft: 6, color: COLORS.textMuted, fontFamily: 'monospace', fontSize: 11 }}>{account.account_id}</span>
                              {account.error && (
                                <span title={account.error} style={{ marginLeft: 8, color: COLORS.danger, fontSize: 11, fontWeight: 600, cursor: 'help' }}>⚠️ Fetch failed</span>
                              )}
                              {!account.error && services.length === 0 && (
                                <span style={{ marginLeft: 8, color: COLORS.textMuted, fontSize: 11 }}>No Savings Plan coverage this month</span>
                              )}
                            </td>
                            {account.error ? (
                              <td colSpan={3} style={{ padding: '7px 14px', color: COLORS.textMuted }}>—</td>
                            ) : (
                              <>
                                <td style={{ padding: '7px 14px', textAlign: 'right', color: '#475569' }}>{money(account.sp_on_demand_equivalent)}</td>
                                <td style={{ padding: '7px 14px', textAlign: 'right', color: '#475569' }}>{money(account.sp_amortized_cost)}</td>
                                <td style={{ padding: '7px 14px', textAlign: 'right', fontWeight: 700, color: account.total_net_savings > 0 ? COLORS.success : '#475569' }}>{money(account.total_net_savings)}</td>
                              </>
                            )}
                            <ResellerCostCell
                              accountId={account.account_id}
                              month={monthReport.month}
                              comparison={account.reseller_comparison || null}
                              canManage={canManageResellerCost}
                              onChanged={() => load(true)}
                            />
                          </tr>
                          {isOpen && hasServices && services.map(svc => (
                            <tr key={`${rowKey}:${svc.service}`} style={{ borderBottom: '1px solid #f8fafc', background: '#fbfdff' }}>
                              <td></td>
                              <td style={{ padding: '6px 14px 6px 34px', color: COLORS.textSecondary, fontSize: 12 }}>{svc.service}</td>
                              <td style={{ padding: '6px 14px', textAlign: 'right', color: '#64748b', fontSize: 12 }}>{money(svc.on_demand_equivalent)}</td>
                              <td style={{ padding: '6px 14px', textAlign: 'right', color: '#64748b', fontSize: 12 }}>{money(svc.amortized_cost)}</td>
                              <td style={{ padding: '6px 14px', textAlign: 'right', fontWeight: 600, color: svc.net_savings > 0 ? COLORS.success : '#64748b', fontSize: 12 }}>{money(svc.net_savings)}</td>
                              <td colSpan={3}></td>
                            </tr>
                          ))}
                        </React.Fragment>
                      )
                    })}
                  </tbody>
                  <tfoot>
                    <tr style={{ borderTop: `2px solid ${COLORS.border}`, background: '#f8fafc' }}>
                      <td style={{ padding: '9px 14px' }}></td>
                      <td style={{ padding: '9px 14px', fontWeight: 700, color: COLORS.textPrimary }}>Total</td>
                      <td style={{ padding: '9px 14px', textAlign: 'right', fontWeight: 700, color: COLORS.textPrimary }}>{money(monthReport.totals?.sp_on_demand_equivalent)}</td>
                      <td style={{ padding: '9px 14px', textAlign: 'right', fontWeight: 700, color: COLORS.textPrimary }}>{money(monthReport.totals?.sp_amortized_cost)}</td>
                      <td style={{ padding: '9px 14px', textAlign: 'right', fontWeight: 700, color: COLORS.success }}>{money(monthReport.totals?.total_net_savings)}</td>
                      <td style={{ padding: '9px 14px', textAlign: 'right', fontWeight: 700, color: COLORS.textPrimary }}>{resellerTotals ? money(resellerTotals.reseller_ondemand_cost, resellerTotals.currency) : '—'}</td>
                      <td style={{ padding: '9px 14px', textAlign: 'right', fontWeight: 700, color: resellerTotals ? (resellerTotals.diff <= 0 ? COLORS.success : COLORS.danger) : COLORS.textPrimary }}>{resellerTotals ? money(resellerTotals.diff) : '—'}</td>
                      <td style={{ padding: '9px 14px', textAlign: 'right', fontWeight: 700, color: resellerTotals?.discount_pct == null ? COLORS.textMuted : resellerTotals.discount_pct >= 0 ? COLORS.success : COLORS.danger }}>{resellerTotals?.discount_pct == null ? '—' : pct(resellerTotals.discount_pct)}</td>
                    </tr>
                  </tfoot>
                </table>
              </div>
            )}
          </div>
          </React.Fragment>
        )
      })}
    </div>
  )
}
