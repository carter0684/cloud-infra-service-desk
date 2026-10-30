import React, { useState, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { todayHK, formatYmdLabel } from '../utils/time.js'
import { useAuth } from '../auth/AuthContext.jsx'

// ── Color palette — cycles per job so different jobs on the same day are
//    visually distinct, similar to the reference screenshot's pastel pills.
const PILL_PALETTE = [
  { bg: '#dbeafe', border: '#93c5fd', text: '#1e40af' }, // blue
  { bg: '#dcfce7', border: '#86efac', text: '#166534' }, // green
  { bg: '#fef3c7', border: '#fcd34d', text: '#92400e' }, // amber
  { bg: '#fce7f3', border: '#f9a8d4', text: '#9d174d' }, // pink
  { bg: '#e0e7ff', border: '#a5b4fc', text: '#3730a3' }, // indigo
  { bg: '#fee2e2', border: '#fca5a5', text: '#991b1b' }, // red
]
const PRIORITY_STRIPE = { ASAP: '#dc2626', High: '#ea580c', Medium: '#2563eb', Low: '#16a34a' }

function pillStyleFor(jobId) {
  // Deterministic hash so the same job always gets the same color across renders.
  let hash = 0
  for (let i = 0; i < jobId.length; i++) hash = (hash * 31 + jobId.charCodeAt(i)) >>> 0
  return PILL_PALETTE[hash % PILL_PALETTE.length]
}

function fmtMonthYear(date) {
  return date.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
}

function ymd(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

function isSameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

// Build a 6-row x 7-col grid of Date objects covering the full month view
// (including leading/trailing days from adjacent months, like the screenshot).
function buildMonthGrid(year, month) {
  const firstOfMonth = new Date(year, month, 1)
  const startWeekday = firstOfMonth.getDay() // 0=Sun
  const gridStart = new Date(year, month, 1 - startWeekday)
  const days = []
  for (let i = 0; i < 42; i++) {
    const d = new Date(gridStart)
    d.setDate(gridStart.getDate() + i)
    days.push(d)
  }
  return days
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

export default function CalendarPage() {
  const { user } = useAuth()
  // Per-user tab WRITE grant for "jobs" — see database.py's
  // PortalUser.tab_write docstring. Full admins always have every
  // tab_id here.
  const canWriteJobs = (user?.tab_write || []).includes('jobs')
  const navigate = useNavigate()
  const today = useMemo(() => todayHK(), [])
  const [cursor, setCursor] = useState(new Date(today.getFullYear(), today.getMonth(), 1))
  const [jobs, setJobs] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [expandedDay, setExpandedDay] = useState(null) // ymd string or null

  useEffect(() => {
    setLoading(true)
    setError('')
    // Fetch a generous page — this is a small internal-team dataset, so one
    // page covers everything without needing server-side date filtering.
    api.listJobs({ per_page: 500 })
      .then(d => setJobs(d.jobs || []))
      .catch(() => setError('Failed to load jobs for calendar'))
      .finally(() => setLoading(false))
  }, [])

  const days = useMemo(() => buildMonthGrid(cursor.getFullYear(), cursor.getMonth()), [cursor])

  // Group jobs by due_date (yyyy-mm-dd). Jobs without a due_date are excluded
  // from the calendar since they have no date to place them on.
  const jobsByDate = useMemo(() => {
    const map = {}
    for (const j of jobs) {
      if (!j.due_date) continue
      const key = j.due_date.slice(0, 10)
      if (!map[key]) map[key] = []
      map[key].push(j)
    }
    // Sort each day's jobs by time-of-day if due_date carries a time, else by title
    for (const key of Object.keys(map)) {
      map[key].sort((a, b) => (a.due_date || '').localeCompare(b.due_date || ''))
    }
    return map
  }, [jobs])

  const goPrev = () => setCursor(c => new Date(c.getFullYear(), c.getMonth() - 1, 1))
  const goNext = () => setCursor(c => new Date(c.getFullYear(), c.getMonth() + 1, 1))
  const goToday = () => setCursor(new Date(today.getFullYear(), today.getMonth(), 1))

  const MAX_VISIBLE = 2

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: '24px 20px', overflowX: 'hidden', boxSizing: 'border-box' }}>
      {/* Header toolbar */}
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 18, position: 'relative' }}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button onClick={goPrev} style={navBtn}>‹</button>
          <button onClick={goToday} style={{ ...navBtn, width: 'auto', padding: '0 14px', fontSize: 13, fontWeight: 600 }}>Today</button>
          <button onClick={goNext} style={navBtn}>›</button>
        </div>
        <h2 style={{ margin: '0 auto', color: '#0f2e5a', fontSize: 20, fontWeight: 700, position: 'absolute', left: '50%', transform: 'translateX(-50%)' }}>
          {fmtMonthYear(cursor)}
        </h2>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 10, alignItems: 'center' }}>
          {loading && <span style={{ fontSize: 12, color: '#94a3b8' }}>Loading…</span>}
          {canWriteJobs && (
            <button onClick={() => navigate('/jobs/new')}
              style={{ background: '#1a4080', color: '#fff', border: 'none', borderRadius: 7, padding: '8px 16px', fontWeight: 600, fontSize: 13, cursor: 'pointer' }}>
              + New Job
            </button>
          )}
        </div>
      </div>

      {error && (
        <div style={{ background: '#fef2f2', color: '#dc2626', padding: '10px 14px', borderRadius: 6, fontSize: 13, marginBottom: 14 }}>{error}</div>
      )}

      {/* Legend */}
      <div style={{ display: 'flex', gap: 16, marginBottom: 12, fontSize: 12, color: '#64748b', flexWrap: 'wrap' }}>
        {Object.entries(PRIORITY_STRIPE).map(([p, color]) => (
          <span key={p} style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: color, display: 'inline-block' }} />
            {p}
          </span>
        ))}
      </div>

      {/* Calendar grid */}
      <div style={{ background: '#fff', borderRadius: 10, boxShadow: '0 1px 6px rgba(0,0,0,0.07)', overflow: 'hidden' }}>
        {/* Weekday header row */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(0, 1fr))', borderBottom: '1px solid #e2e8f0' }}>
          {WEEKDAYS.map(w => (
            <div key={w} style={{ padding: '8px 0', textAlign: 'center', fontSize: 11, fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: 0.4 }}>
              {w}
            </div>
          ))}
        </div>

        {/* Day cells — 6 rows of 7 */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(0, 1fr))', gridAutoRows: '1fr' }}>
          {days.map((d, i) => {
            const key = ymd(d)
            const isCurrentMonth = d.getMonth() === cursor.getMonth()
            const isToday = isSameDay(d, today)
            const dayJobs = jobsByDate[key] || []
            const visible = dayJobs.slice(0, MAX_VISIBLE)
            const hiddenCount = dayJobs.length - visible.length
            const hasUrgent = dayJobs.some(j => j.priority === 'ASAP' || j.priority === 'High')

            return (
              <div key={i}
                style={{
                  position: 'relative',
                  minHeight: 118,
                  minWidth: 0,
                  overflow: 'hidden',
                  padding: '6px 6px 8px',
                  borderRight: (i % 7 !== 6) ? '1px solid #f1f5f9' : 'none',
                  borderBottom: i < 35 ? '1px solid #f1f5f9' : 'none',
                  background: isToday ? '#eff6ff' : (isCurrentMonth ? '#fff' : '#fafbfc'),
                  boxShadow: isToday ? 'inset 0 0 0 2px #2563eb' : 'none',
                }}>
                {hasUrgent && (
                  <span style={{ position: 'absolute', top: 6, left: 6, width: 8, height: 8, borderRadius: '50%', background: '#dc2626' }} title="Urgent job(s) this day" />
                )}
                <div style={{
                  textAlign: 'right', fontSize: 13, fontWeight: isToday ? 700 : 500,
                  color: isToday ? '#fff' : (isCurrentMonth ? '#334155' : '#cbd5e1'),
                  marginBottom: 4,
                }}>
                  {isToday ? (
                    <span style={{ display: 'inline-block', width: 22, height: 22, lineHeight: '22px', borderRadius: '50%', background: '#2563eb', textAlign: 'center' }}>
                      {d.getDate()}
                    </span>
                  ) : d.getDate()}
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
                  {visible.map(j => {
                    const pill = pillStyleFor(j.id)
                    const stripe = PRIORITY_STRIPE[j.priority] || '#94a3b8'
                    const timePart = j.due_date && j.due_date.length > 10 ? j.due_date.slice(11, 16) : null
                    return (
                      <div key={j.id} onClick={() => navigate(`/jobs/${j.id}`)}
                        title={`${j.title} — ${j.status} — ${j.assignee_name}`}
                        style={{
                          cursor: 'pointer',
                          borderLeft: `3px solid ${stripe}`,
                          background: pill.bg, color: pill.text,
                          borderRadius: 4, padding: '3px 6px',
                          fontSize: 11, lineHeight: 1.35,
                          minWidth: 0, maxWidth: '100%', boxSizing: 'border-box',
                          overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis',
                        }}>
                        {timePart && <span style={{ fontWeight: 700, marginRight: 4 }}>{timePart}</span>}
                        <span style={{ fontWeight: 600 }}>{j.title}</span>
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

      {/* Day detail popover — shown when "+N more" is clicked */}
      {expandedDay && (
        <div onClick={() => setExpandedDay(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}>
          <div onClick={e => e.stopPropagation()}
            style={{ background: '#fff', borderRadius: 10, padding: 20, width: 380, maxHeight: '70vh', overflowY: 'auto', boxShadow: '0 10px 40px rgba(0,0,0,0.25)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
              <h3 style={{ margin: 0, fontSize: 15, color: '#0f2e5a' }}>
                Jobs on {formatYmdLabel(expandedDay)}
              </h3>
              <button onClick={() => setExpandedDay(null)} style={{ background: 'none', border: 'none', fontSize: 18, cursor: 'pointer', color: '#94a3b8' }}>×</button>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {(jobsByDate[expandedDay] || []).map(j => {
                const pill = pillStyleFor(j.id)
                const stripe = PRIORITY_STRIPE[j.priority] || '#94a3b8'
                return (
                  <div key={j.id} onClick={() => navigate(`/jobs/${j.id}`)}
                    style={{ cursor: 'pointer', borderLeft: `4px solid ${stripe}`, background: pill.bg, color: pill.text, borderRadius: 6, padding: '8px 10px' }}>
                    <div style={{ fontWeight: 700, fontSize: 13 }}>{j.title}</div>
                    <div style={{ fontSize: 11, opacity: 0.85, marginTop: 2 }}>{j.status} · {j.assignee_name} · {j.priority}</div>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

const navBtn = {
  width: 32, height: 32, borderRadius: 7, border: '1px solid #d1d5db',
  background: '#fff', fontSize: 16, cursor: 'pointer', color: '#334155',
  display: 'flex', alignItems: 'center', justifyContent: 'center',
}
