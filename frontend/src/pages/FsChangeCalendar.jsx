import React, { useState, useEffect, useMemo } from 'react'
import { api } from '../api.js'
import { todayHK, formatTimeHK, formatDateTimeHK, formatYmdLabel, PORTAL_TIMEZONE } from '../utils/time.js'
import { getPageCache, setPageCache } from '../utils/pageCache.js'

// Cache is keyed per (year, month) — mirrors the backend's own cache key
// (see fs_calendar.py) so revisiting a previously-viewed month in this
// tab also renders instantly.
function changesCacheKey(year, month) {
  return `fs-calendar-changes:${year}-${month}`
}

// Renders Freshservice change requests (fetched via our backend proxy —
// see api.js fsCalendarChanges / backend/app/fs_calendar.py) in the same
// month-grid style as the reference "Freshservice Change Calendar" screenshot:
// pastel pills per change, colored by type/risk, showing #CHN-xxxx + time.

const TYPE_COLOR = {
  Emergency: { bg: '#fee2e2', border: '#fca5a5', text: '#991b1b', stripe: '#dc2626' },
  Major:     { bg: '#fef3c7', border: '#fcd34d', text: '#92400e', stripe: '#ea580c' },
  Normal:    { bg: '#dbeafe', border: '#93c5fd', text: '#1e40af', stripe: '#2563eb' },
  Standard:  { bg: '#dcfce7', border: '#86efac', text: '#166534', stripe: '#16a34a' },
  Minor:     { bg: '#e0e7ff', border: '#a5b4fc', text: '#3730a3', stripe: '#6366f1' },
}
const DEFAULT_COLOR = { bg: '#f1f5f9', border: '#cbd5e1', text: '#475569', stripe: '#94a3b8' }

function ymd(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}
function isSameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}
function buildMonthGrid(year, month) {
  const firstOfMonth = new Date(year, month, 1)
  const startWeekday = firstOfMonth.getDay()
  const gridStart = new Date(year, month, 1 - startWeekday)
  const days = []
  for (let i = 0; i < 42; i++) {
    const d = new Date(gridStart)
    d.setDate(gridStart.getDate() + i)
    days.push(d)
  }
  return days
}
function fmtMonthYear(date) {
  return date.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
}
// Returns the "YYYY-MM-DD" calendar date (in HKT) that a UTC timestamp
// falls on — used to place changes on the correct day of the HKT month grid.
function ymdInHK(iso) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: PORTAL_TIMEZONE,
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date(iso))
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MAX_VISIBLE = 2

export default function FsChangeCalendar() {
  const today = useMemo(() => todayHK(), [])
  const [cursor, setCursor] = useState(new Date(today.getFullYear(), today.getMonth(), 1))
  // Seed from the in-tab page cache (see utils/pageCache.js) so
  // re-visiting the Calendars tab on the same month renders instantly
  // instead of a blank "Loading…" grid.
  const initialCached = getPageCache(changesCacheKey(today.getFullYear(), today.getMonth() + 1))
  const [changes, setChanges] = useState(initialCached?.data || [])
  const [loading, setLoading] = useState(!initialCached)
  const [error, setError] = useState('')
  const [expandedDay, setExpandedDay] = useState(null)
  const [selectedChange, setSelectedChange] = useState(null)
  const [cacheAgeSeconds, setCacheAgeSeconds] = useState(initialCached?.cache_age_seconds ?? null)

  const loadChanges = React.useCallback((refresh = false) => {
    const key = changesCacheKey(cursor.getFullYear(), cursor.getMonth() + 1)
    const cachedForThisMonth = getPageCache(key)
    // Only show the "Loading…" state when there's nothing cached yet for
    // this exact month — switching between previously-viewed months (or
    // the periodic background refresh) then renders instantly and
    // refetches silently underneath.
    if (cachedForThisMonth && !refresh) {
      setChanges(cachedForThisMonth.data || [])
      setCacheAgeSeconds(cachedForThisMonth.cache_age_seconds ?? null)
    } else {
      setLoading(true)
    }
    setError('')
    return api.fsCalendarChanges(cursor.getFullYear(), cursor.getMonth() + 1, refresh)
      .then(d => {
        setChanges(d.data || [])
        setCacheAgeSeconds(d.cache_age_seconds ?? null)
        setPageCache(key, d)
      })
      .catch(e => setError(e?.response?.data?.detail || 'Failed to load Freshservice change calendar'))
      .finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor])

  useEffect(() => { loadChanges(false) }, [loadChanges])

  // Server-side cache is 5 minutes (see backend/app/fs_calendar.py) —
  // silently re-fetch every 5 minutes in the background so a Calendars
  // tab left open doesn't show change data older than that, without
  // hammering the separate Freshservice Calendar app's API on every visit.
  useEffect(() => {
    const id = setInterval(() => { loadChanges(false) }, 5 * 60 * 1000)
    return () => clearInterval(id)
  }, [loadChanges])

  const days = useMemo(() => buildMonthGrid(cursor.getFullYear(), cursor.getMonth()), [cursor])

  const changesByDate = useMemo(() => {
    const map = {}
    for (const c of changes) {
      if (!c.scheduledStartTime) continue
      const key = ymdInHK(c.scheduledStartTime)
      if (!map[key]) map[key] = []
      map[key].push(c)
    }
    for (const key of Object.keys(map)) {
      map[key].sort((a, b) => a.scheduledStartTime.localeCompare(b.scheduledStartTime))
    }
    return map
  }, [changes])

  const goPrev = () => setCursor(c => new Date(c.getFullYear(), c.getMonth() - 1, 1))
  const goNext = () => setCursor(c => new Date(c.getFullYear(), c.getMonth() + 1, 1))
  const goToday = () => setCursor(new Date(today.getFullYear(), today.getMonth(), 1))

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: '20px 20px 40px', overflowX: 'hidden', boxSizing: 'border-box' }}>
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 18, position: 'relative' }}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button onClick={goPrev} style={navBtn}>‹</button>
          <button onClick={goToday} style={{ ...navBtn, width: 'auto', padding: '0 14px', fontSize: 13, fontWeight: 600 }}>Today</button>
          <button onClick={goNext} style={navBtn}>›</button>
        </div>
        <h2 style={{ margin: '0 auto', color: '#0f2e5a', fontSize: 20, fontWeight: 700, position: 'absolute', left: '50%', transform: 'translateX(-50%)' }}>
          {fmtMonthYear(cursor)}
        </h2>
        <button onClick={() => loadChanges(true)} disabled={loading}
          style={{ marginLeft: 'auto', padding: '6px 12px', borderRadius: 7, border: '1px solid #e2e8f0', background: '#fff', color: '#64748b', fontWeight: 600, fontSize: 12, cursor: loading ? 'default' : 'pointer' }}>
          {loading ? '⏳ Loading…' : '🔄 Refresh'}
        </button>
      </div>

      {!loading && (
        <div style={{ fontSize: 11.5, color: '#94a3b8', marginTop: -8, marginBottom: 10, textAlign: 'right' }}>
          {(!cacheAgeSeconds || cacheAgeSeconds < 60)
            ? 'Data as of just now.'
            : `Cached ${Math.floor(cacheAgeSeconds / 60)} min${Math.floor(cacheAgeSeconds / 60) === 1 ? '' : 's'} ago (refreshes automatically every 5 minutes).`}
        </div>
      )}

      {error && (
        <div style={{ background: '#fef2f2', color: '#dc2626', padding: '10px 14px', borderRadius: 6, fontSize: 13, marginBottom: 14 }}>{error}</div>
      )}

      <div style={{ display: 'flex', gap: 16, marginBottom: 12, fontSize: 12, color: '#64748b', flexWrap: 'wrap' }}>
        {Object.entries(TYPE_COLOR).map(([t, c]) => (
          <span key={t} style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: c.stripe, display: 'inline-block' }} />
            {t}
          </span>
        ))}
      </div>

      <div style={{ background: '#fff', borderRadius: 10, boxShadow: '0 1px 6px rgba(0,0,0,0.07)', overflow: 'hidden' }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(0, 1fr))', borderBottom: '1px solid #e2e8f0' }}>
          {WEEKDAYS.map(w => (
            <div key={w} style={{ padding: '8px 0', textAlign: 'center', fontSize: 11, fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: 0.4 }}>
              {w}
            </div>
          ))}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(0, 1fr))', gridAutoRows: '1fr' }}>
          {days.map((d, i) => {
            const key = ymd(d)
            const isCurrentMonth = d.getMonth() === cursor.getMonth()
            const isToday = isSameDay(d, today)
            const dayChanges = changesByDate[key] || []
            const visible = dayChanges.slice(0, MAX_VISIBLE)
            const hiddenCount = dayChanges.length - visible.length
            const hasEmergency = dayChanges.some(c => c.type === 'Emergency')

            return (
              <div key={i}
                style={{
                  position: 'relative', minHeight: 118, minWidth: 0, overflow: 'hidden',
                  padding: '6px 6px 8px',
                  borderRight: (i % 7 !== 6) ? '1px solid #f1f5f9' : 'none',
                  borderBottom: i < 35 ? '1px solid #f1f5f9' : 'none',
                  background: isToday ? '#eff6ff' : (isCurrentMonth ? '#fff' : '#fafbfc'),
                  boxShadow: isToday ? 'inset 0 0 0 2px #2563eb' : 'none',
                }}>
                {hasEmergency && (
                  <span style={{ position: 'absolute', top: 6, left: 6, width: 8, height: 8, borderRadius: '50%', background: '#dc2626' }} title="Emergency change(s) this day" />
                )}
                <div style={{
                  textAlign: 'right', fontSize: 13, fontWeight: isToday ? 700 : 500,
                  color: isToday ? '#fff' : (isCurrentMonth ? '#334155' : '#cbd5e1'), marginBottom: 4,
                }}>
                  {isToday ? (
                    <span style={{ display: 'inline-block', width: 22, height: 22, lineHeight: '22px', borderRadius: '50%', background: '#2563eb', textAlign: 'center' }}>
                      {d.getDate()}
                    </span>
                  ) : d.getDate()}
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
                  {visible.map(c => {
                    const color = TYPE_COLOR[c.type] || DEFAULT_COLOR
                    return (
                      <div key={c.id} onClick={() => setSelectedChange(c)}
                        title={`${c.displayId} — ${c.title}`}
                        style={{
                          cursor: 'pointer', borderLeft: `3px solid ${color.stripe}`,
                          background: color.bg, color: color.text,
                          borderRadius: 4, padding: '3px 6px', fontSize: 11, lineHeight: 1.35,
                          minWidth: 0, maxWidth: '100%', boxSizing: 'border-box',
                          overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis',
                        }}>
                        <span style={{ fontWeight: 700 }}>#{c.displayId}</span>{' '}
                        <span style={{ opacity: 0.85 }}>{formatTimeHK(c.scheduledStartTime)}</span>
                        <div style={{ fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.title}</div>
                      </div>
                    )
                  })}
                  {hiddenCount > 0 && (
                    <div onClick={() => setExpandedDay(key)}
                      style={{ fontSize: 11, color: '#2563eb', fontWeight: 600, cursor: 'pointer', padding: '2px 6px' }}>
                      +{hiddenCount} more
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* Day detail popover */}
      {expandedDay && (
        <div onClick={() => setExpandedDay(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}>
          <div onClick={e => e.stopPropagation()}
            style={{ background: '#fff', borderRadius: 10, padding: 20, width: 400, maxHeight: '70vh', overflowY: 'auto', boxShadow: '0 10px 40px rgba(0,0,0,0.25)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
              <h3 style={{ margin: 0, fontSize: 15, color: '#0f2e5a' }}>
                Changes on {formatYmdLabel(expandedDay)}
              </h3>
              <button onClick={() => setExpandedDay(null)} style={{ background: 'none', border: 'none', fontSize: 18, cursor: 'pointer', color: '#94a3b8' }}>×</button>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {(changesByDate[expandedDay] || []).map(c => {
                const color = TYPE_COLOR[c.type] || DEFAULT_COLOR
                return (
                  <div key={c.id} onClick={() => { setExpandedDay(null); setSelectedChange(c) }}
                    style={{ cursor: 'pointer', borderLeft: `4px solid ${color.stripe}`, background: color.bg, color: color.text, borderRadius: 6, padding: '8px 10px' }}>
                    <div style={{ fontWeight: 700, fontSize: 13 }}>#{c.displayId} — {c.title}</div>
                    <div style={{ fontSize: 11, opacity: 0.85, marginTop: 2 }}>{c.status} · {c.type} · Risk: {c.riskLevel} · {formatTimeHK(c.scheduledStartTime)} HKT</div>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}

      {/* Change detail modal */}
      {selectedChange && (
        <div onClick={() => setSelectedChange(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 60 }}>
          <div onClick={e => e.stopPropagation()}
            style={{ background: '#fff', borderRadius: 10, padding: 22, width: 460, boxShadow: '0 10px 40px rgba(0,0,0,0.25)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 14 }}>
              <div>
                <div style={{ fontSize: 12, color: '#94a3b8', fontWeight: 600 }}>#{selectedChange.displayId}</div>
                <h3 style={{ margin: '2px 0 0', fontSize: 16, color: '#0f2e5a' }}>{selectedChange.title}</h3>
              </div>
              <button onClick={() => setSelectedChange(null)} style={{ background: 'none', border: 'none', fontSize: 20, cursor: 'pointer', color: '#94a3b8' }}>×</button>
            </div>
            <DetailRow label="Status" value={selectedChange.status} />
            <DetailRow label="Type" value={selectedChange.type} />
            <DetailRow label="Risk Level" value={selectedChange.riskLevel} />
            <DetailRow label="Category" value={`${selectedChange.category || '—'}${selectedChange.subCategory ? ' / ' + selectedChange.subCategory : ''}${selectedChange.itemCategory ? ' / ' + selectedChange.itemCategory : ''}`} />
            <DetailRow label="Scheduled (HKT)" value={`${formatDateTimeHK(selectedChange.scheduledStartTime)} → ${formatDateTimeHK(selectedChange.scheduledEndTime)}`} />
            {selectedChange.closureCode && <DetailRow label="Closure Code" value={selectedChange.closureCode} />}
          </div>
        </div>
      )}
    </div>
  )
}

function DetailRow({ label, value }) {
  return (
    <div style={{ display: 'flex', gap: 10, padding: '6px 0', borderBottom: '1px solid #f1f5f9', fontSize: 13 }}>
      <span style={{ width: 110, color: '#64748b', fontWeight: 600, flexShrink: 0 }}>{label}</span>
      <span style={{ color: '#1e293b' }}>{value || '—'}</span>
    </div>
  )
}

const navBtn = {
  width: 32, height: 32, borderRadius: 7, border: '1px solid #d1d5db',
  background: '#fff', fontSize: 16, cursor: 'pointer', color: '#334155',
  display: 'flex', alignItems: 'center', justifyContent: 'center',
}
