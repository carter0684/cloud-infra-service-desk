import React, { useState } from 'react'
import CalendarPage from './CalendarPage.jsx'
import FsChangeCalendar from './FsChangeCalendar.jsx'
import WorldHolidaysCalendar from './WorldHolidaysCalendar.jsx'
import { TAB_BAR, tabButtonStyle } from '../utils/theme.js'

// Both tabs render natively in React — no iframe, no separate login.
// The Freshservice Change Calendar's own username/password live only in
// AWS Secrets Manager and are read server-side by backend/app/fs_calendar.py;
// the frontend just calls our own /api/fs-calendar/* proxy routes (same
// Azure AD session as the rest of the portal), so users never see a
// second login prompt.

const TABS = [
  { key: 'jobs', label: '📅 Job Portal Calendar' },
  { key: 'freshservice', label: '🧊 Freshservice Change Calendar' },
  { key: 'holidays', label: '🌏 Worldwide Public Holidays' },
]

export default function CalendarsPage() {
  const [tab, setTab] = useState('jobs')

  return (
    <div>
      <div style={{ background: '#fff', paddingTop: 16 }}>
        <div style={{ padding: '0 20px', marginBottom: 8 }}>
          <h2 style={{ margin: 0, color: '#0f2e5a', fontSize: 20 }}>Calendars</h2>
          <p style={{ margin: '4px 0 0', color: '#64748b', fontSize: 13 }}>
            View job due-dates, the Freshservice IT change calendar and worldwide public holidays in one place.
          </p>
        </div>
        <div style={{ ...TAB_BAR, padding: '0 20px' }}>
          {TABS.map(t => (
            <button key={t.key} onClick={() => setTab(t.key)} style={tabButtonStyle(tab === t.key)}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      <div style={{ background: '#f0f2f5', minHeight: 'calc(100vh - 54px - 100px)', overflowX: 'hidden' }}>
        {tab === 'jobs' && <CalendarPage />}
        {tab === 'freshservice' && <FsChangeCalendar />}
        {tab === 'holidays' && <WorldHolidaysCalendar />}
      </div>
    </div>
  )
}
