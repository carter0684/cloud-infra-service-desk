// Phone layout for data tables: "one card per row, column name beside each value".
//
// The CSS half lives in src/mobile.css (rules for `table[data-cards]`). That CSS can
// hide the header row and stack the cells, but it cannot know which column each
// cell belongs to, so this file copies the header text onto every cell as a
// `data-label` attribute, which mobile.css prints with `td::before`.
//
// Opt-in per table: add the bare attribute `data-cards` to the <table>. Tables
// where the grid itself is the information (cost-by-month matrices, permission
// checkbox matrices) are deliberately NOT marked and keep scrolling sideways.
//
// Works with React: React never removes an attribute it did not set, so the labels
// survive re-renders, and a MutationObserver relabels rows that are added later
// (data loading, sorting, filtering, expanding a row).

const SELECTOR = 'table[data-cards]'
const PHONE_QUERY = '(max-width: 768px)'

function cleanLabel(th) {
  // Sortable headers append an arrow glyph; collapse whitespace so wrapped
  // JSX text reads as one phrase.
  return (th.textContent || '').replace(/[▲▼↑↓]/g, '').replace(/\s+/g, ' ').trim()
}

function headerLabels(table) {
  const head = table.tHead
  if (!head || head.rows.length === 0) return []
  const row = head.rows[head.rows.length - 1]
  const labels = []
  for (const th of row.cells) {
    labels.push(cleanLabel(th))
    // A header spanning N columns labels only the first of them.
    for (let i = 1; i < (th.colSpan || 1); i++) labels.push('')
  }
  return labels
}

function labelTable(table) {
  const labels = headerLabels(table)
  for (const body of table.tBodies) {
    for (const row of body.rows) {
      let col = 0
      for (const cell of row.cells) {
        const span = cell.colSpan || 1
        if (span > 1) {
          // Full-width rows (loading skeletons, empty state, expanded detail
          // panels) are not "a value of a column": no label.
          if (cell.hasAttribute('data-label')) cell.removeAttribute('data-label')
        } else {
          const label = labels[col] ?? ''
          if (cell.getAttribute('data-label') !== label) cell.setAttribute('data-label', label)
        }
        col += span
      }
    }
  }
}

function relabelAll() {
  document.querySelectorAll(SELECTOR).forEach(labelTable)
}

// Only react to changes that can actually affect a marked table, so the
// per-second header clock (and similar churn) costs nothing.
function touchesMarkedTable(mutation) {
  const node = mutation.target
  const el = node.nodeType === 1 ? node : node.parentElement
  if (!el) return false
  return !!(el.closest(SELECTOR) || (mutation.type === 'childList' && el.querySelector(SELECTOR)))
}

export function initResponsiveTables() {
  if (typeof window === 'undefined' || typeof document === 'undefined' || window.__responsiveTablesInit) return
  window.__responsiveTablesInit = true

  const phone = typeof window.matchMedia === 'function' ? window.matchMedia(PHONE_QUERY) : null
  let frame = 0
  const schedule = () => {
    if (frame) return
    frame = requestAnimationFrame(() => {
      frame = 0
      if (!phone || phone.matches) relabelAll()
    })
  }

  new MutationObserver(mutations => {
    for (const m of mutations) {
      if (touchesMarkedTable(m)) { schedule(); return }
    }
  }).observe(document.body, { childList: true, subtree: true, characterData: true })

  if (phone && typeof phone.addEventListener === 'function') phone.addEventListener('change', schedule)
  schedule()
}
