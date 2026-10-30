// Festive-season detection — purely date-driven (HKT), no manual on/off
// switch needed each year. Returns which festival (if any) is "near" or
// "in" right now, so UI components can show seasonal flourishes
// automatically and have them disappear on their own once the window ends.

import { todayHKYmd } from './time.js'

// Chinese New Year moves every year (lunar calendar) — there's no formula
// simple enough to compute client-side, so known dates are listed here a
// few years out. Add a new year's date as it becomes known; if the
// current year isn't listed, the CNY window simply doesn't activate (no
// crash, just no CNY decoration that year) — Christmas/New Year detection
// below doesn't depend on this list at all.
const CNY_DATE_BY_YEAR = {
  2026: '2026-02-17',
  2027: '2027-02-06',
  2028: '2028-01-26',
  2029: '2029-02-13',
  2030: '2030-02-03',
}

// Mid-Autumn Festival (中秋節) — also lunar-calendar-based, same
// "list known dates, skip years not listed" approach as CNY above.
const MID_AUTUMN_DATE_BY_YEAR = {
  2026: '2026-09-25',
  2027: '2027-09-15',
  2028: '2028-10-03',
}

const FESTIVALS = {
  christmas: {
    key: 'christmas',
    navIcon: '🎄',
    greeting: 'Merry Christmas from the IT Team! 🎅',
    gradient: 'linear-gradient(90deg, #7f1d1d 0%, #14532d 60%, #7f1d1d 100%)',
    particles: ['❄️', '🎄', '⭐', '🔔'],
  },
  newyear: {
    key: 'newyear',
    navIcon: '🎇',
    greeting: 'Happy New Year! Wishing the team a great year ahead 🎉',
    gradient: 'linear-gradient(90deg, #1e1b4b 0%, #6d28d9 60%, #1e1b4b 100%)',
    particles: ['🎉', '✨', '🎆', '🥂'],
  },
  cny: {
    key: 'cny',
    navIcon: '🧧',
    greeting: 'Kung Hei Fat Choi! 🧧 Happy Chinese New Year',
    gradient: 'linear-gradient(90deg, #7f1d1d 0%, #b91c1c 60%, #7f1d1d 100%)',
    particles: ['🧧', '🏮', '🍊', '🐉'],
  },
  midautumn: {
    key: 'midautumn',
    navIcon: '🥮',
    greeting: 'Happy Mid-Autumn Festival! 🥮🏮 Wishing everyone reunion and joy',
    gradient: 'linear-gradient(90deg, #78350f 0%, #b45309 60%, #78350f 100%)',
    particles: ['🥮', '🏮', '🌕', '🐇'],
  },
}

/**
 * Returns the active festival config object, or null if none is active
 * right now (evaluated in HKT). Windows are deliberately generous on the
 * lead-in side ("nearing the festival") and short on the trail-out side.
 */
export function getActiveFestival() {
  const todayYmd = todayHKYmd()             // 'YYYY-MM-DD'
  const [y, m, d] = todayYmd.split('-').map(Number)
  const md = m * 100 + d                    // e.g. 1225 for Dec 25, 102 for Jan 2

  // Christmas build-up: 10 Dec – 26 Dec
  if (md >= 1210 && md <= 1226) return FESTIVALS.christmas

  // New Year's: 27 Dec – 2 Jan (spans the year boundary)
  if (md >= 1227 || md <= 102) return FESTIVALS.newyear

  // Chinese New Year: ~12 days before through 4 days after the lunar date
  const cnyStr = CNY_DATE_BY_YEAR[y]
  if (cnyStr && withinWindow(todayYmd, cnyStr, 12, 4)) return FESTIVALS.cny

  // Mid-Autumn Festival: ~7 days before through 2 days after the lunar date
  const midAutumnStr = MID_AUTUMN_DATE_BY_YEAR[y]
  if (midAutumnStr && withinWindow(todayYmd, midAutumnStr, 7, 2)) return FESTIVALS.midautumn

  return null
}

function withinWindow(todayYmd, targetYmd, daysBefore, daysAfter) {
  const target = new Date(`${targetYmd}T00:00:00`)
  const today = new Date(`${todayYmd}T00:00:00`)
  const diffDays = Math.round((today - target) / 86400000)
  return diffDays >= -daysBefore && diffDays <= daysAfter
}
