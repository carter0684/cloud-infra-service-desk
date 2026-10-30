import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { api } from '../api.js'
import { todayHK, formatYmdLabel } from '../utils/time.js'
import { getPageCache, setPageCache } from '../utils/pageCache.js'
import useIsMobile from '../utils/useIsMobile.js'

// "Worldwide Public Holidays" sub-tab of Calendars. Data comes from our own
// backend (GET /api/world-holidays, see backend/app/world_holidays.py), which
// reads holidays-calendar.net — an unofficial aggregator — so every region
// links back to its source page and the footer says the government list wins.
//
// The grid is the same month layout as the other two calendars. On a phone the
// cells show coloured dots (the 7-column grid is too narrow for text) and the
// list underneath the grid is the readable view.

const STORAGE_KEY = 'world-holidays:regions'
const DEFAULT_REGIONS = ['hongkong']

// One colour per region (13). stripe = solid accent, bg/text = pill colours.
const REGION_COLORS = {
  hongkong:   { stripe: '#dc2626', bg: '#fee2e2', text: '#991b1b' },
  china:      { stripe: '#ea580c', bg: '#ffedd5', text: '#9a3412' },
  macau:      { stripe: '#16a34a', bg: '#dcfce7', text: '#166534' },
  taiwan:     { stripe: '#2563eb', bg: '#dbeafe', text: '#1e40af' },
  japan:      { stripe: '#db2777', bg: '#fce7f3', text: '#9d174d' },
  southkorea: { stripe: '#0891b2', bg: '#cffafe', text: '#155e75' },
  singapore:  { stripe: '#e11d48', bg: '#ffe4e6', text: '#9f1239' },
  thailand:   { stripe: '#7c3aed', bg: '#ede9fe', text: '#5b21b6' },
  vietnam:    { stripe: '#ca8a04', bg: '#fef9c3', text: '#854d0e' },
  indonesia:  { stripe: '#65a30d', bg: '#ecfccb', text: '#3f6212' },
  philippine: { stripe: '#0d9488', bg: '#ccfbf1', text: '#115e59' },
  usa:        { stripe: '#4338ca', bg: '#e0e7ff', text: '#3730a3' },
  uk:         { stripe: '#475569', bg: '#e2e8f0', text: '#334155' },
}
const FALLBACK_COLOR = { stripe: '#94a3b8', bg: '#f1f5f9', text: '#475569' }

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MAX_VISIBLE = 3

function ymd(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}
function isSameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}
function buildMonthGrid(year, month) {
  const gridStart = new Date(year, month, 1 - new Date(year, month, 1).getDay())
  return Array.from({ length: 42 }, (_, i) => {
    const d = new Date(gridStart)
    d.setDate(gridStart.getDate() + i)
    return d
  })
}
function fmtMonthYear(date) {
  return date.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
}
function loadSavedRegions() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
    if (Array.isArray(saved) && saved.length) return saved
  } catch { /* ignore a corrupt value */ }
  return DEFAULT_REGIONS
}
function cacheKey(year, regionKeys) {
  return `world-holidays:${year}:${[...regionKeys].sort().join(',')}`
}

export default function WorldHolidaysCalendar() {
  const today = useMemo(() => todayHK(), [])
  const isMobile = useIsMobile()
  const [cursor, setCursor] = useState(new Date(today.getFullYear(), today.getMonth(), 1))
  const [regions, setRegions] = useState([])             // [{key, code, en, zh, caveat}]
  const [selected, setSelected] = useState(loadSavedRegions)
  const [byYear, setByYear] = useState({})                // { 2026: <API response>, ... }
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [expandedDay, setExpandedDay] = useState(null)
  const [view, setView] = useState('calendar')           // 'calendar' | 'table'

  useEffect(() => {
    api.worldHolidayRegions()
      .then(d => setRegions(d.regions || []))
      .catch(e => setError(e?.response?.data?.detail || 'Failed to load the region list'))
  }, [])

  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(selected)) } catch { /* private mode */ }
  }, [selected])

  const regionInfo = useMemo(() => Object.fromEntries(regions.map(r => [r.key, r])), [regions])

  // January and December grids also show days of the neighbouring year, so
  // that year is requested too (best effort — its failure is not shown).
  // The table view shows one whole year, so it never needs the neighbours.
  const yearsNeeded = useMemo(() => {
    const y = cursor.getFullYear()
    const m = cursor.getMonth()
    if (view === 'table') return [y]
    return m === 0 ? [y, y - 1] : m === 11 ? [y, y + 1] : [y]
  }, [cursor, view])

  const load = useCallback(async (refresh = false) => {
    if (!selected.length) { setByYear({}); setLoading(false); return }
    const mainYear = yearsNeeded[0]
    const cached = {}
    for (const y of yearsNeeded) {
      const hit = getPageCache(cacheKey(y, selected))
      if (hit && !refresh) cached[y] = hit
    }
    if (cached[mainYear]) { setByYear(prev => ({ ...prev, ...cached })); setLoading(false) } else { setLoading(true) }
    setError('')
    const wanted = yearsNeeded.filter(y => refresh || !cached[y])
    if (wanted.length) {
      const results = await Promise.allSettled(wanted.map(y => api.worldHolidays(y, selected, refresh)))
      const fresh = {}
      results.forEach((res, i) => {
        const y = wanted[i]
        if (res.status === 'fulfilled') {
          fresh[y] = res.value
          setPageCache(cacheKey(y, selected), res.value)
        } else if (y === mainYear) {
          setError(res.reason?.response?.data?.detail || 'Failed to load public holidays')
        }
      })
      setByYear(prev => ({ ...prev, ...cached, ...fresh }))
    }
    setLoading(false)
  }, [selected, yearsNeeded])

  useEffect(() => { load(false) }, [load])

  const mainYear = cursor.getFullYear()
  const mainData = byYear[mainYear]

  // date -> [{...holiday, region}] across every loaded year and selected region.
  const eventsByDate = useMemo(() => {
    const map = {}
    for (const data of Object.values(byYear)) {
      for (const [key, r] of Object.entries(data?.regions || {})) {
        if (!selected.includes(key)) continue
        for (const h of r.holidays || []) {
          (map[h.date] ||= []).push({ ...h, region: key })
        }
      }
    }
    for (const list of Object.values(map)) {
      list.sort((a, b) => (a.kind === b.kind ? a.region.localeCompare(b.region) : a.kind === 'holiday' ? -1 : 1))
    }
    return map
  }, [byYear, selected])

  const days = useMemo(() => buildMonthGrid(cursor.getFullYear(), cursor.getMonth()), [cursor])

  const monthPrefix = `${cursor.getFullYear()}-${String(cursor.getMonth() + 1).padStart(2, '0')}-`
  const monthList = useMemo(
    () => Object.keys(eventsByDate).filter(k => k.startsWith(monthPrefix)).sort().map(k => [k, eventsByDate[k]]),
    [eventsByDate, monthPrefix],
  )

  // One merged table for the cursor's whole year: every selected region, sorted by date.
  const yearRows = useMemo(() => {
    const prefix = `${cursor.getFullYear()}-`
    const rows = []
    for (const [date, list] of Object.entries(eventsByDate)) {
      if (date.startsWith(prefix)) for (const ev of list) rows.push(ev)
    }
    rows.sort((a, b) => a.date.localeCompare(b.date) || a.region.localeCompare(b.region))
    return rows
  }, [eventsByDate, cursor])
  const yearRegionCount = useMemo(() => new Set(yearRows.map(r => r.region)).size, [yearRows])

  const toggleRegion = (key) => setSelected(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  const step = view === 'table' ? 12 : 1   // table view moves a year at a time
  const goPrev = () => setCursor(c => new Date(c.getFullYear(), c.getMonth() - step, 1))
  const goNext = () => setCursor(c => new Date(c.getFullYear(), c.getMonth() + step, 1))
  const goToday = () => setCursor(new Date(today.getFullYear(), today.getMonth(), 1))

  const cacheAge = mainData?.cache_age_seconds
  // Per-region notices for the main (cursor) year.
  const notices = useMemo(() => {
    const list = []
    for (const key of selected) {
      const name = regionInfo[key]?.en || key
      const r = mainData?.regions?.[key]
      const err = mainData?.errors?.[key]
      if (err) list.push({ level: 'error', text: `${name}: ${err}`, url: null })
      else if (r && !r.available) list.push({ level: 'warn', text: `${name}: ${r.reason || 'not available'} (${mainYear}).`, url: r.source_url })
      else if (r) {
        if (r.provisional) list.push({ level: 'warn', text: `${name} ${mainYear}: the source marks this schedule as not yet confirmed. Check the government announcement.`, url: r.source_url })
        if (r.unparsed?.length) list.push({ level: 'warn', text: `${name} ${mainYear}: ${r.unparsed.length} holiday(s) could not be read and are not shown: ${r.unparsed.map(u => u.name).join(', ')}.`, url: r.source_url })
        if (regionInfo[key]?.caveat) list.push({ level: 'info', text: `${name}: ${regionInfo[key].caveat}`, url: r.source_url })
      }
    }
    return list
  }, [selected, regionInfo, mainData, mainYear])

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: '20px 20px 40px', overflowX: 'hidden', boxSizing: 'border-box' }}>
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 14, gap: 8, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button onClick={goPrev} style={navBtn} aria-label="Previous month">‹</button>
          <button onClick={goToday} style={{ ...navBtn, width: 'auto', padding: '0 14px', fontSize: 13, fontWeight: 600 }}>Today</button>
          <button onClick={goNext} style={navBtn} aria-label="Next month">›</button>
        </div>
        <h2 style={{ margin: '0 auto', color: '#0f2e5a', fontSize: 20, fontWeight: 700 }}>
          {view === 'table' ? cursor.getFullYear() : fmtMonthYear(cursor)}
        </h2>
        <div role="group" aria-label="View" style={{ display: 'inline-flex', border: '1px solid #d1d5db', borderRadius: 7, overflow: 'hidden' }}>
          {[['calendar', '📅 Calendar'], ['table', '📋 Table']].map(([key, label]) => (
            <button key={key} onClick={() => setView(key)} aria-pressed={view === key}
              style={{
                padding: '6px 12px', border: 'none', fontSize: 12.5, fontWeight: 600, cursor: 'pointer',
                background: view === key ? '#0f2e5a' : '#fff', color: view === key ? '#fff' : '#475569',
              }}>
              {label}
            </button>
          ))}
        </div>
        <button onClick={() => load(true)} disabled={loading}
          style={{ padding: '6px 12px', borderRadius: 7, border: '1px solid #e2e8f0', background: '#fff', color: '#64748b', fontWeight: 600, fontSize: 12, cursor: loading ? 'default' : 'pointer' }}>
          {loading ? '⏳ Loading…' : '🔄 Refresh'}
        </button>
      </div>

      {/* Region picker */}
      <div style={{ background: '#fff', borderRadius: 10, boxShadow: '0 1px 6px rgba(0,0,0,0.07)', padding: '12px 14px', marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12, fontWeight: 700, color: '#64748b', textTransform: 'uppercase', letterSpacing: 0.4 }}>Regions</span>
          {/* Disabled until the region list has loaded — otherwise a fast click selects an empty list. */}
          <button onClick={() => setSelected(regions.map(r => r.key))} disabled={!regions.length}
            style={{ ...linkBtn, opacity: regions.length ? 1 : 0.4, cursor: regions.length ? 'pointer' : 'default' }}>Select all</button>
          <button onClick={() => setSelected([])} style={linkBtn}>Clear</button>
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {regions.map(r => {
            const on = selected.includes(r.key)
            const c = REGION_COLORS[r.key] || FALLBACK_COLOR
            return (
              <button key={r.key} onClick={() => toggleRegion(r.key)} aria-pressed={on} title={r.zh}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6, padding: '5px 10px', borderRadius: 16,
                  border: `1px solid ${on ? c.stripe : '#e2e8f0'}`, background: on ? c.bg : '#fff',
                  color: on ? c.text : '#64748b', fontSize: 12.5, fontWeight: on ? 700 : 500, cursor: 'pointer',
                }}>
                <span style={{ width: 9, height: 9, borderRadius: '50%', background: on ? c.stripe : '#cbd5e1' }} />
                {r.en}
              </button>
            )
          })}
        </div>
      </div>

      {error && <div style={{ background: '#fef2f2', color: '#dc2626', padding: '10px 14px', borderRadius: 6, fontSize: 13, marginBottom: 12 }}>{error}</div>}
      {notices.map((n, i) => (
        <div key={i} style={{
          background: n.level === 'error' ? '#fef2f2' : n.level === 'warn' ? '#fffbeb' : '#eff6ff',
          color: n.level === 'error' ? '#b91c1c' : n.level === 'warn' ? '#92400e' : '#1e40af',
          padding: '8px 12px', borderRadius: 6, fontSize: 12.5, marginBottom: 8,
        }}>
          {n.text}{' '}
          {n.url && <a href={n.url} target="_blank" rel="noopener noreferrer" style={{ color: 'inherit', fontWeight: 600 }}>Source page ↗</a>}
        </div>
      ))}

      {!selected.length && (
        <div style={{ background: '#fff', borderRadius: 10, padding: 24, textAlign: 'center', color: '#64748b', fontSize: 13.5, boxShadow: '0 1px 6px rgba(0,0,0,0.07)' }}>
          Pick at least one region above to see its public holidays.
        </div>
      )}

      {selected.length > 0 && view === 'table' && (
        <HolidayTable rows={yearRows} year={cursor.getFullYear()} today={today} regionInfo={regionInfo}
          regionCount={yearRegionCount} loading={loading} />
      )}

      {selected.length > 0 && view === 'calendar' && (
        <div style={{ background: '#fff', borderRadius: 10, boxShadow: '0 1px 6px rgba(0,0,0,0.07)', overflow: 'hidden' }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(0, 1fr))', borderBottom: '1px solid #e2e8f0' }}>
            {WEEKDAYS.map(w => (
              <div key={w} style={{ padding: '8px 0', textAlign: 'center', fontSize: 11, fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: 0.4 }}>{w}</div>
            ))}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(0, 1fr))' }}>
            {days.map((d, i) => {
              const key = ymd(d)
              const inMonth = d.getMonth() === cursor.getMonth()
              const isToday = isSameDay(d, today)
              const isWeekend = d.getDay() === 0 || d.getDay() === 6
              const events = eventsByDate[key] || []
              const visible = events.slice(0, MAX_VISIBLE)
              const hidden = events.length - visible.length
              return (
                <div key={i} onClick={() => events.length && setExpandedDay(key)}
                  style={{
                    position: 'relative', minHeight: isMobile ? 54 : 112, minWidth: 0, overflow: 'hidden',
                    padding: isMobile ? '4px 3px' : '6px 6px 8px', cursor: events.length ? 'pointer' : 'default',
                    borderRight: (i % 7 !== 6) ? '1px solid #f1f5f9' : 'none',
                    borderBottom: i < 35 ? '1px solid #f1f5f9' : 'none',
                    background: isToday ? '#eff6ff' : (inMonth ? (isWeekend ? '#fcfcfd' : '#fff') : '#fafbfc'),
                    boxShadow: isToday ? 'inset 0 0 0 2px #2563eb' : 'none',
                  }}>
                  <div style={{ textAlign: 'right', fontSize: 13, fontWeight: isToday ? 700 : 500, color: inMonth ? (isWeekend ? '#94a3b8' : '#334155') : '#cbd5e1', marginBottom: 4 }}>
                    {d.getDate()}
                  </div>
                  {isMobile ? (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3, justifyContent: 'center' }}>
                      {events.slice(0, 6).map(ev => (
                        <span key={ev.id + ev.region} style={{
                          width: 8, height: 8, borderRadius: '50%',
                          background: ev.kind === 'substitute' ? '#fff' : (REGION_COLORS[ev.region] || FALLBACK_COLOR).stripe,
                          border: `2px solid ${(REGION_COLORS[ev.region] || FALLBACK_COLOR).stripe}`, boxSizing: 'border-box',
                        }} />
                      ))}
                    </div>
                  ) : (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
                      {visible.map(ev => <EventPill key={ev.id + ev.region} ev={ev} code={regionInfo[ev.region]?.code} />)}
                      {hidden > 0 && <div style={{ fontSize: 11, color: '#2563eb', fontWeight: 600, padding: '2px 6px' }}>+{hidden} more</div>}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Legend */}
      {selected.length > 0 && view === 'calendar' && (
        <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', margin: '10px 2px', fontSize: 11.5, color: '#64748b' }}>
          <span><span style={{ ...legendSwatch, background: '#e2e8f0' }} /> Public holiday</span>
          <span><span style={{ ...legendSwatch, background: '#fff', border: '1px dashed #64748b' }} /> Substitute day (補假)</span>
          <span>½ half day · ~ approximate date</span>
        </div>
      )}

      {/* Readable list for the month */}
      {selected.length > 0 && view === 'calendar' && (
        <div style={{ background: '#fff', borderRadius: 10, boxShadow: '0 1px 6px rgba(0,0,0,0.07)', padding: '14px 16px', marginTop: 8 }}>
          <h3 style={{ margin: '0 0 10px', fontSize: 14, color: '#0f2e5a' }}>Holidays in {fmtMonthYear(cursor)}</h3>
          {loading && !monthList.length && <div style={{ fontSize: 13, color: '#94a3b8' }}>Loading…</div>}
          {!loading && !monthList.length && <div style={{ fontSize: 13, color: '#94a3b8' }}>No public holidays in the selected regions this month.</div>}
          {monthList.map(([date, list]) => (
            <div key={date} style={{ display: 'flex', gap: 12, padding: '8px 0', borderTop: '1px solid #f1f5f9', flexWrap: 'wrap' }}>
              <div style={{ width: 130, flexShrink: 0, fontSize: 12.5, fontWeight: 600, color: '#334155' }}>{formatYmdLabel(date)}</div>
              <div style={{ flex: 1, minWidth: 200, display: 'flex', flexDirection: 'column', gap: 4 }}>
                {list.map(ev => <EventRow key={ev.id + ev.region} ev={ev} info={regionInfo[ev.region]} />)}
              </div>
            </div>
          ))}
        </div>
      )}

      <div style={{ fontSize: 11.5, color: '#94a3b8', marginTop: 12, lineHeight: 1.6 }}>
        Source: <a href={mainData?.source?.url || 'https://holidays-calendar.net'} target="_blank" rel="noopener noreferrer" style={{ color: '#64748b' }}>holidays-calendar.net</a>
        {' '}(unofficial). The government announcement for each region always takes precedence.
        {cacheAge != null && !loading && <> · {cacheAge < 60 ? 'Loaded just now' : `Cached ${Math.floor(cacheAge / 60)} min ago`}; the list is re-read from the source at most once a day.</>}
      </div>

      {/* Day popover */}
      {expandedDay && (
        <div onClick={() => setExpandedDay(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}>
          <div onClick={e => e.stopPropagation()}
            style={{ background: '#fff', borderRadius: 10, padding: 20, width: 420, maxHeight: '70vh', overflowY: 'auto', boxShadow: '0 10px 40px rgba(0,0,0,0.25)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
              <h3 style={{ margin: 0, fontSize: 15, color: '#0f2e5a' }}>{formatYmdLabel(expandedDay)}</h3>
              <button onClick={() => setExpandedDay(null)} aria-label="Close" style={{ background: 'none', border: 'none', fontSize: 20, cursor: 'pointer', color: '#94a3b8' }}>×</button>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {(eventsByDate[expandedDay] || []).map(ev => <EventRow key={ev.id + ev.region} ev={ev} info={regionInfo[ev.region]} boxed />)}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// One merged table: every selected region, whole year, sorted by date and
// grouped by month. On a phone `data-cards` turns each row into a card (see
// mobile.css + utils/responsiveTables.js).
function HolidayTable({ rows, year, today, regionInfo, regionCount, loading }) {
  const todayKey = ymd(today)
  const groups = []
  for (const ev of rows) {
    const month = Number(ev.date.slice(5, 7))
    if (!groups.length || groups[groups.length - 1].month !== month) groups.push({ month, rows: [] })
    groups[groups.length - 1].rows.push(ev)
  }
  const th = { textAlign: 'left', padding: '9px 10px', fontSize: 11.5, fontWeight: 700, color: '#64748b', textTransform: 'uppercase', letterSpacing: 0.3, background: '#f8fafc', borderBottom: '2px solid #e2e8f0', whiteSpace: 'nowrap' }
  const td = { padding: '8px 10px', fontSize: 13, color: '#1e293b', borderBottom: '1px solid #f1f5f9', verticalAlign: 'top' }

  return (
    <div style={{ background: '#fff', borderRadius: 10, boxShadow: '0 1px 6px rgba(0,0,0,0.07)', overflow: 'hidden' }}>
      <div style={{ padding: '12px 16px', fontSize: 13, color: '#475569', borderBottom: '1px solid #f1f5f9' }}>
        {loading && !rows.length
          ? 'Loading…'
          : <><strong>{rows.length}</strong> holiday day{rows.length === 1 ? '' : 's'} across <strong>{regionCount}</strong> region{regionCount === 1 ? '' : 's'} in {year}. Past dates are greyed out.</>}
      </div>
      {!loading && !rows.length && (
        <div style={{ padding: 24, textAlign: 'center', color: '#94a3b8', fontSize: 13 }}>No public holidays for the selected regions in {year}.</div>
      )}
      {rows.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table data-cards style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={th}>Date</th>
                <th style={th}>Day</th>
                <th style={th}>Region</th>
                <th style={th}>Holiday</th>
                <th style={th}>English name</th>
                <th style={th}>Notes</th>
              </tr>
            </thead>
            <tbody>
              {groups.map(g => (
                <React.Fragment key={g.month}>
                  <tr>
                    <td colSpan={6} style={{ padding: '7px 10px', background: '#eef2f7', color: '#0f2e5a', fontSize: 12.5, fontWeight: 700 }}>
                      {new Date(year, g.month - 1, 1).toLocaleDateString('en-US', { month: 'long' })}
                    </td>
                  </tr>
                  {g.rows.map(ev => {
                    const c = REGION_COLORS[ev.region] || FALLBACK_COLOR
                    const past = ev.date < todayKey
                    const [y, m, d] = ev.date.split('-').map(Number)
                    const sub = ev.kind === 'substitute'
                    const tags = eventTags(ev)
                    return (
                      <tr key={ev.id + ev.region} style={{ opacity: past ? 0.5 : 1, background: ev.date === todayKey ? '#eff6ff' : 'transparent' }}>
                        <td style={{ ...td, whiteSpace: 'nowrap', borderLeft: `4px solid ${c.stripe}`, fontVariantNumeric: 'tabular-nums' }}>{ev.date}</td>
                        <td style={{ ...td, whiteSpace: 'nowrap', color: '#64748b' }}>{WEEKDAYS[new Date(y, m - 1, d).getDay()]}</td>
                        <td style={{ ...td, whiteSpace: 'nowrap' }}>
                          <span style={{ background: c.bg, color: c.text, border: `1px solid ${c.stripe}`, borderRadius: 4, padding: '0 6px', fontSize: 11, fontWeight: 700, marginRight: 6 }}>
                            {regionInfo[ev.region]?.code || ev.region}
                          </span>
                          {regionInfo[ev.region]?.en || ev.region}
                        </td>
                        <td style={{ ...td, fontWeight: 600, fontStyle: sub ? 'italic' : 'normal' }}>{ev.approximate ? '~' : ''}{ev.name}</td>
                        <td style={{ ...td, color: '#64748b' }}>{ev.en_name || ''}</td>
                        <td style={td}>
                          {tags.map(t => (
                            <span key={t} style={{ display: 'inline-block', fontSize: 11, color: '#92400e', background: '#fef3c7', borderRadius: 4, padding: '0 6px', margin: '0 4px 2px 0' }}>{t}</span>
                          ))}
                        </td>
                      </tr>
                    )
                  })}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function eventTags(ev) {
  const tags = []
  if (ev.kind === 'substitute') tags.push('Substitute day')
  if (ev.half_day) tags.push('Half day')
  if (ev.approximate) tags.push('Approximate date')
  for (const s of ev.scope || []) tags.push(s)
  return tags
}

function EventPill({ ev, code }) {
  const c = REGION_COLORS[ev.region] || FALLBACK_COLOR
  const sub = ev.kind === 'substitute'
  return (
    <div title={`${ev.name}${ev.en_name ? ` — ${ev.en_name}` : ''}${eventTags(ev).length ? ` (${eventTags(ev).join(', ')})` : ''}`}
      style={{
        borderLeft: `3px ${sub ? 'dashed' : 'solid'} ${c.stripe}`, background: sub ? '#fff' : c.bg, color: c.text,
        outline: sub ? `1px dashed ${c.stripe}` : 'none', outlineOffset: -1,
        borderRadius: 4, padding: '2px 6px', fontSize: 11, lineHeight: 1.35, minWidth: 0, maxWidth: '100%',
        boxSizing: 'border-box', overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis',
        fontStyle: sub ? 'italic' : 'normal',
      }}>
      <span style={{ fontWeight: 700 }}>{code}</span>{' '}
      {ev.approximate ? '~' : ''}{ev.name}{ev.half_day ? ' ½' : ''}
    </div>
  )
}

function EventRow({ ev, info, boxed = false }) {
  const c = REGION_COLORS[ev.region] || FALLBACK_COLOR
  const tags = eventTags(ev)
  return (
    <div style={{
      display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap',
      ...(boxed ? { borderLeft: `4px solid ${c.stripe}`, background: c.bg, borderRadius: 6, padding: '8px 10px' } : {}),
    }}>
      <span style={{ background: c.bg, color: c.text, border: `1px solid ${c.stripe}`, borderRadius: 4, padding: '0 6px', fontSize: 11, fontWeight: 700, flexShrink: 0 }}>
        {info?.code || ev.region}
      </span>
      <span style={{ fontSize: 13, fontWeight: 600, color: '#1e293b' }}>{ev.name}</span>
      {ev.en_name && <span style={{ fontSize: 12, color: '#64748b' }}>{ev.en_name}</span>}
      {tags.map(t => (
        <span key={t} style={{ fontSize: 11, color: '#92400e', background: '#fef3c7', borderRadius: 4, padding: '0 6px' }}>{t}</span>
      ))}
    </div>
  )
}

const navBtn = {
  width: 32, height: 32, borderRadius: 7, border: '1px solid #d1d5db',
  background: '#fff', fontSize: 16, cursor: 'pointer', color: '#334155',
  display: 'flex', alignItems: 'center', justifyContent: 'center',
}
const linkBtn = { background: 'none', border: 'none', color: '#2563eb', fontSize: 12, fontWeight: 600, cursor: 'pointer', padding: 0 }
const legendSwatch = { display: 'inline-block', width: 12, height: 12, borderRadius: 3, verticalAlign: 'middle', marginRight: 4 }
