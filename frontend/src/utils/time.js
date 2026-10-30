// Portal-wide time handling — the Cloud Infra Job Portal always displays
// times in Hong Kong time (UTC+8), regardless of the viewer's browser/OS
// timezone. The backend stores all timestamps in UTC (see
// datetime.now(timezone.utc) / datetime.utcnow() across the backend), so
// every display point must explicitly convert using timeZone:
// 'Asia/Hong_Kong' rather than relying on the browser's local timezone
// (which toLocaleString()/toLocaleDateString() use by default).

export const PORTAL_TIMEZONE = 'Asia/Hong_Kong'

/**
 * Format an ISO/UTC timestamp string as "YYYY-MM-DD HH:MM" in HKT (24h).
 * Returns '' for null/undefined input.
 */
export function formatDateTimeHK(isoString) {
  if (!isoString) return ''
  const d = new Date(isoString)
  if (isNaN(d.getTime())) return ''
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: PORTAL_TIMEZONE,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
    hour12: false,
  }).formatToParts(d)
  const get = (type) => parts.find(p => p.type === type)?.value
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}`
}

/**
 * Format an ISO/UTC timestamp string as just "YYYY-MM-DD" in HKT.
 * Returns '' for null/undefined input.
 */
export function formatDateHK(isoString) {
  if (!isoString) return ''
  const d = new Date(isoString)
  if (isNaN(d.getTime())) return ''
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: PORTAL_TIMEZONE,
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(d)
}

/**
 * Format an ISO/UTC timestamp string as "HH:MM" (24h) in HKT.
 */
export function formatTimeHK(isoString) {
  if (!isoString) return ''
  const d = new Date(isoString)
  if (isNaN(d.getTime())) return ''
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: PORTAL_TIMEZONE,
    hour: '2-digit', minute: '2-digit',
    hour12: false,
  }).format(d)
}

/**
 * Today's calendar date in HKT as a "YYYY-MM-DD" string — handy for
 * `<input type="date" min={...}>` so the minimum selectable due-date
 * matches Hong Kong's current date, not the browser's local date.
 */
export function todayHKYmd() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: PORTAL_TIMEZONE,
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date())
}

/**
 * "Today" as a plain JS Date whose year/month/day components represent
 * the current calendar date in Hong Kong time — NOT the browser's local
 * timezone. Used by the calendar pages so "today" highlighting and month
 * navigation are correct for viewers outside HKT (e.g. someone opening
 * the portal while travelling).
 *
 * Only the Y/M/D components of the returned Date should be read (via
 * getFullYear()/getMonth()/getDate()) — its exact instant/hour is not
 * meaningful, this is a calendar-date value, not a timestamp.
 */
export function todayHK() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: PORTAL_TIMEZONE,
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date())
  const get = (type) => Number(parts.find(p => p.type === type)?.value)
  return new Date(get('year'), get('month') - 1, get('day'))
}

/**
 * "YYYY-MM" key for the month an ISO/UTC timestamp falls in, computed in
 * HKT. Used to group jobs by creation month (see JobList.jsx) — rows with
 * the same key are adjacent since the job list is already sorted by
 * created_at desc, so consecutive same-key rows can be merged into one
 * spanning cell.
 */
export function monthKeyHK(isoString) {
  if (!isoString) return ''
  const d = new Date(isoString)
  if (isNaN(d.getTime())) return ''
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: PORTAL_TIMEZONE, year: 'numeric', month: '2-digit',
  }).formatToParts(d)
  const get = (type) => parts.find(p => p.type === type)?.value
  return `${get('year')}-${get('month')}`
}

/**
 * Human label for the month an ISO/UTC timestamp falls in, in HKT —
 * e.g. "Jul 2026". Paired with monthKeyHK() above.
 */
export function monthLabelHK(isoString) {
  if (!isoString) return ''
  const d = new Date(isoString)
  if (isNaN(d.getTime())) return ''
  return new Intl.DateTimeFormat('en-US', {
    timeZone: PORTAL_TIMEZONE, year: 'numeric', month: 'short',
  }).format(d)
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/**
 * Format a plain "YYYY-MM-DD" calendar-date string (not a UTC timestamp)
 * as "Mon D, YYYY" — done via string parsing only, deliberately NOT via
 * `new Date(str)` + toLocaleDateString(), which would reinterpret the
 * string in the browser's local timezone and could show the wrong day.
 */
export function formatYmdLabel(ymdString) {
  if (!ymdString) return ''
  const [y, m, d] = ymdString.split('-').map(Number)
  if (!y || !m || !d) return ymdString
  return `${MONTH_NAMES[m - 1]} ${d}, ${y}`
}

/**
 * Current date+time in HKT as "YYYY-MM-DD HH:MM:SS" (24h) — used by the
 * live clock in the top nav bar.
 */
export function nowHK() {
  const d = new Date()
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: PORTAL_TIMEZONE,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).formatToParts(d)
  const get = (type) => parts.find(p => p.type === type)?.value
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')}`
}
