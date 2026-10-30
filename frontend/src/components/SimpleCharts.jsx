import React from 'react'

/**
 * Lightweight, dependency-free SVG bar/pie charts for the Cost Analysis
 * page. Deliberately not pulling in a charting library (recharts/chart.js)
 * for two small charts — keeps bundle size down and avoids adding a new
 * dependency (this app also ships as a Capacitor mobile build) for what
 * is fundamentally < 100 lines of SVG.
 */

const PALETTE = [
  '#1a4080', '#2563eb', '#0891b2', '#16a34a', '#d97706', '#dc2626',
  '#7c3aed', '#db2777', '#059669', '#ea580c', '#4f46e5', '#0284c7',
  '#94a3b8',
]

export function colorFor(index) {
  return PALETTE[index % PALETTE.length]
}

/** True only for finite, non-negative numbers — the only values that are
 * safe to feed into chart geometry (widths, arc angles, totals). */
function isFiniteNonNegative(n) {
  return typeof n === 'number' && Number.isFinite(n) && n >= 0
}

/** Sanitize a raw items array into [{label, value}] with:
 *  - non-string/empty labels replaced by a stable placeholder
 *  - non-finite or negative values coerced to 0 (never dropped, so bar/
 *    pie counts still match what the caller passed in)
 *  - duplicate labels merged (summed) instead of colliding on React key
 */
function sanitizeItems(items) {
  const merged = new Map()
  ;(items || []).forEach((item, i) => {
    const label = typeof item?.label === 'string' && item.label.trim() ? item.label : `Item ${i + 1}`
    const value = isFiniteNonNegative(item?.value) ? item.value : 0
    merged.set(label, (merged.get(label) || 0) + value)
  })
  return Array.from(merged.entries()).map(([label, value]) => ({ label, value }))
}

function isValidCurrency(code) {
  return typeof code === 'string' && /^[A-Za-z]{3}$/.test(code)
}

function money(amount, currency = 'USD') {
  const safeCurrency = isValidCurrency(currency) ? currency.toUpperCase() : 'USD'
  if (!isFiniteNonNegative(amount) && amount !== 0) return '—'
  const safeAmount = isFiniteNonNegative(amount) ? amount : 0
  try {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: safeCurrency, maximumFractionDigits: 0 }).format(safeAmount)
  } catch {
    return `${safeCurrency} ${safeAmount.toFixed(0)}`
  }
}

/** Horizontal bar chart. items: [{label, value}], sorted desc by caller.
 *
 * Optional style-override props (all default to the original light-theme
 * look, so every existing call site — Cost Analysis's dollar-amount bars,
 * etc. — renders identically to before): `barColor` forces every bar to
 * one flat color instead of the rotating rainbow palette (used by the
 * Meraki monitor's dark-dashboard styling, which wants a single
 * consistent blue rather than a multi-color legend-less chart);
 * `labelColor`/`valueColor`/`trackColor` recolor the row label, the
 * value text, and the empty-bar track for dark backgrounds;
 * `valueFormatter` replaces the default `money()` formatting for
 * non-currency values (percentages, milliseconds, plain counts) — money()
 * always falls back to USD formatting for an invalid/empty `currency`,
 * which is wrong for e.g. "87.3%" or "42 ms". */
export function BarChart({
  items, currency = 'USD', height = 26, gap = 8,
  barColor, labelColor = '#475569', valueColor = '#1e2a3a', trackColor = '#f1f5f9',
  valueFormatter,
}) {
  const clean = sanitizeItems(items)
  if (clean.length === 0) {
    return <div style={{ color: labelColor, fontSize: 13, padding: '12px 0', opacity: 0.7 }}>No data to display.</div>
  }
  const max = Math.max(1, ...clean.map(i => i.value))
  const formatValue = typeof valueFormatter === 'function' ? valueFormatter : (v => money(v, currency))
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap }}>
      {clean.map((item, i) => (
        <div key={`${item.label}__${i}`} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ width: 150, fontSize: 12, color: labelColor, textAlign: 'right', flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={item.label}>
            {item.label}
          </div>
          <div style={{ flex: 1, background: trackColor, borderRadius: 4, height, position: 'relative' }}>
            <div style={{
              width: `${Math.max(2, (item.value / max) * 100)}%`, height: '100%', borderRadius: 4,
              background: barColor || colorFor(i), transition: 'width .3s',
            }} />
          </div>
          <div style={{ width: 90, fontSize: 12.5, fontWeight: 600, color: valueColor, flexShrink: 0 }}>
            {formatValue(item.value)}
          </div>
        </div>
      ))}
    </div>
  )
}

/** Simple SVG donut/pie chart. items: [{label, value}]. */
export function PieChart({ items, currency = 'USD', size = 200 }) {
  const clean = sanitizeItems(items).filter(i => i.value > 0)
  const total = clean.reduce((s, i) => s + i.value, 0)
  const radius = size / 2
  const innerRadius = radius * 0.55

  if (clean.length === 0 || total <= 0) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
          <circle cx={radius} cy={radius} r={radius - 2} fill="#f1f5f9" />
          <circle cx={radius} cy={radius} r={innerRadius} fill="#fff" />
          <text x={radius} y={radius - 4} textAnchor="middle" fontSize="11" fill="#94a3b8" fontWeight="600">TOTAL</text>
          <text x={radius} y={radius + 14} textAnchor="middle" fontSize="14" fill="#1e2a3a" fontWeight="700">{money(0, currency)}</text>
        </svg>
        <div style={{ color: '#94a3b8', fontSize: 13 }}>No data to display.</div>
      </div>
    )
  }

  // Single-slice special case (including the "everything is one item"
  // scenario) drawn as a full ring via two arcs — the general arcPath()
  // sweep-flag math degenerates when start === end (angleSpan === 2π), so
  // this avoids ever asking SVG to draw a zero-length arc for 100%.
  const singleFullCircle = clean.length === 1
  let cumulative = 0

  function arcPath(startAngle, endAngle, r, ir) {
    const toXY = (angle, rad) => [radius + rad * Math.cos(angle), radius + rad * Math.sin(angle)]
    const [x1, y1] = toXY(startAngle, r)
    const [x2, y2] = toXY(endAngle, r)
    const [x3, y3] = toXY(endAngle, ir)
    const [x4, y4] = toXY(startAngle, ir)
    const largeArc = endAngle - startAngle > Math.PI ? 1 : 0
    return `M ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2} L ${x3} ${y3} A ${ir} ${ir} 0 ${largeArc} 0 ${x4} ${y4} Z`
  }

  /** Full donut ring as two concentric circles combined with the
   * evenodd fill rule (outer circle minus inner circle = ring). Used
   * for the "one slice = 100%" case, where the general sweep-arc path
   * math degenerates (start === end angle for a full sweep). */
  function fullRingPath(r, ir) {
    const outer = `M ${radius + r} ${radius} A ${r} ${r} 0 1 1 ${radius - r} ${radius} A ${r} ${r} 0 1 1 ${radius + r} ${radius} Z`
    const inner = `M ${radius + ir} ${radius} A ${ir} ${ir} 0 1 1 ${radius - ir} ${radius} A ${ir} ${ir} 0 1 1 ${radius + ir} ${radius} Z`
    return `${outer} ${inner}`
  }

  const slices = clean.map((item, i) => {
    const pct = (item.value / total) * 100
    let path
    if (singleFullCircle) {
      path = fullRingPath(radius - 2, innerRadius)
    } else {
      const angleSpan = (item.value / total) * Math.PI * 2
      const start = cumulative - Math.PI / 2
      cumulative += angleSpan
      const end = cumulative - Math.PI / 2
      path = arcPath(start, end, radius - 2, innerRadius)
    }
    return { ...item, path, color: colorFor(i), pct }
  })

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 24, flexWrap: 'wrap' }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        {slices.map((s, i) => (
          <path key={`${s.label}__${i}`} d={s.path} fill={s.color} fillRule="evenodd">
            <title>{`${s.label}: ${money(s.value, currency)} (${s.pct.toFixed(1)}%)`}</title>
          </path>
        ))}
        <text x={radius} y={radius - 4} textAnchor="middle" fontSize="11" fill="#94a3b8" fontWeight="600">TOTAL</text>
        <text x={radius} y={radius + 14} textAnchor="middle" fontSize="14" fill="#1e2a3a" fontWeight="700">{money(total, currency)}</text>
      </svg>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 200 }}>
        {slices.map((s, i) => (
          <div key={`${s.label}__legend__${i}`} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5 }}>
            <span style={{ width: 10, height: 10, borderRadius: 3, background: s.color, flexShrink: 0 }} />
            <span style={{ color: '#475569', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={s.label}>{s.label}</span>
            <span style={{ fontWeight: 600, color: '#1e2a3a' }}>{s.pct.toFixed(1)}%</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** Format a UTC "YYYY-MM-DDTHH:MM:SSZ" bucket timestamp into a short
 * axis label. Shows time-of-day for short ranges and adds the date once
 * a series spans more than a day, so a 1w chart's x-axis doesn't just
 * repeat "00:00" with no way to tell days apart. */
function formatTimeLabel(iso, spansMultipleDays) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const time = d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
  if (!spansMultipleDays) return time
  const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  return `${date} ${time}`
}

/**
 * Dependency-free SVG multi-line time-series chart — used by the WAF
 * Rule Monitoring page's on-demand Metrics view (Web-ACL-level and
 * per-rule Blocked/Counted/Allowed CloudWatch series), mirroring the
 * CloudWatch console's own line-chart + legend layout without pulling in
 * a charting library.
 *
 * `series`: [{ timestamp: 'YYYY-MM-DDTHH:MM:SSZ', ...lineKeys }]
 * `lines`: [{ key: 'blocked', label: 'Blocked requests', color: '#dc2626' }]
 */
export function LineChart({ series, lines, height = 220, emptyText = 'No data to display.' }) {
  const points = Array.isArray(series) ? series : []
  const activeLines = Array.isArray(lines) ? lines : []

  if (points.length === 0 || activeLines.length === 0) {
    return <div style={{ color: '#94a3b8', fontSize: 13, padding: '12px 0' }}>{emptyText}</div>
  }

  const width = 720
  const padLeft = 44
  const padRight = 16
  const padTop = 14
  const padBottom = 28
  const plotWidth = width - padLeft - padRight
  const plotHeight = height - padTop - padBottom

  const allValues = points.flatMap(p => activeLines.map(l => (isFiniteNonNegative(p[l.key]) ? p[l.key] : 0)))
  const maxValue = Math.max(1, ...allValues)

  const xStep = points.length > 1 ? plotWidth / (points.length - 1) : 0
  const xAt = i => padLeft + i * xStep
  const yAt = v => padTop + plotHeight - (Math.min(v, maxValue) / maxValue) * plotHeight

  const first = new Date(points[0].timestamp)
  const last = new Date(points[points.length - 1].timestamp)
  const spansMultipleDays = !Number.isNaN(first.getTime()) && !Number.isNaN(last.getTime())
    && (last.getTime() - first.getTime()) > 24 * 60 * 60 * 1000

  // Horizontal grid lines + y-axis labels at 0/50%/100% of maxValue.
  const gridFractions = [0, 0.25, 0.5, 0.75, 1]

  // X-axis tick labels — at most ~6 to avoid overlap, evenly spaced by index.
  const tickCount = Math.min(6, points.length)
  const tickIndices = tickCount <= 1 ? [0] : Array.from({ length: tickCount }, (_, i) => Math.round(i * (points.length - 1) / (tickCount - 1)))

  function linePath(key) {
    return points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i)} ${yAt(isFiniteNonNegative(p[key]) ? p[key] : 0)}`).join(' ')
  }

  return (
    <div>
      <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet">
        {/* Grid + y-axis labels */}
        {gridFractions.map((f, i) => {
          const y = padTop + plotHeight - f * plotHeight
          const value = Math.round(f * maxValue)
          return (
            <g key={`grid-${i}`}>
              <line x1={padLeft} y1={y} x2={width - padRight} y2={y} stroke="#eef1f6" strokeWidth={1} />
              <text x={padLeft - 8} y={y + 3} textAnchor="end" fontSize="9.5" fill="#94a3b8">{value}</text>
            </g>
          )
        })}

        {/* X-axis tick labels */}
        {tickIndices.map(i => (
          <text key={`tick-${i}`} x={xAt(i)} y={height - 8} textAnchor="middle" fontSize="9.5" fill="#94a3b8">
            {formatTimeLabel(points[i].timestamp, spansMultipleDays)}
          </text>
        ))}

        {/* One path per line */}
        {activeLines.map(l => (
          <path key={l.key} d={linePath(l.key)} fill="none" stroke={l.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        ))}
      </svg>

      {/* Legend / "Graphed metrics" style row below the chart */}
      <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', marginTop: 6, paddingLeft: padLeft - 4 }}>
        {activeLines.map(l => {
          const total = points.reduce((s, p) => s + (isFiniteNonNegative(p[l.key]) ? p[l.key] : 0), 0)
          return (
            <div key={l.key} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
              <span style={{ width: 16, height: 3, background: l.color, borderRadius: 2, flexShrink: 0 }} />
              <span style={{ color: '#475569' }}>{l.label}</span>
              <span style={{ fontWeight: 700, color: '#1e2a3a' }}>{total.toLocaleString()}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/**
 * Thin-ring gauge with a big percentage centered inside, plus a list of
 * "big number / small label" stats to its right — modelled on the
 * reference lounge-ops dashboard's "Capacity Usage" widget (a thin
 * blue-on-grey ring showing e.g. "2%", with "6 Current Occupancy" and
 * "347 Total Capacity" stacked beside it). Distinct from PieChart above
 * (which is a multi-slice donut with a color-coded legend) — this is a
 * single-value gauge for exactly one "usage / of a total" metric.
 *
 * `pct`: 0-100 (clamped). `stats`: [{value, label}, ...] rendered to the
 * right, largest/most prominent first.
 */
export function GaugeRing({
  pct, stats = [], size = 120, ringColor = '#2563eb', trackColor = '#eef1f6', strokeWidth = 10,
  textColor = '#1e2a3a', statLabelColor = '#94a3b8',
}) {
  const safePct = Math.max(0, Math.min(100, isFiniteNonNegative(pct) ? pct : 0))
  const radius = (size - strokeWidth) / 2
  const circumference = 2 * Math.PI * radius
  const dashOffset = circumference * (1 - safePct / 100)

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 28, flexWrap: 'wrap' }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke={trackColor} strokeWidth={strokeWidth} />
        <circle
          cx={size / 2} cy={size / 2} r={radius} fill="none" stroke={ringColor} strokeWidth={strokeWidth}
          strokeDasharray={circumference} strokeDashoffset={dashOffset} strokeLinecap="round"
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={{ transition: 'stroke-dashoffset .4s' }}
        />
        <text x={size / 2} y={size / 2 + 6} textAnchor="middle" fontSize={size * 0.19} fill={textColor} fontWeight="800">
          {Math.round(safePct)}%
        </text>
      </svg>
      {stats.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {stats.map((s, i) => (
            <div key={i}>
              <div style={{ fontSize: 22, fontWeight: 800, color: textColor, lineHeight: 1.1 }}>{s.value}</div>
              <div style={{ fontSize: 11.5, color: statLabelColor, fontWeight: 600 }}>{s.label}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** Small inline up/down trend badge, e.g. "▲ 12.3%" in red/green. Renders
 * an em dash for null/undefined/NaN/Infinity — any value that isn't a
 * genuinely comparable percentage. */
export function TrendBadge({ pct, size = 12.5 }) {
  const isValid = typeof pct === 'number' && Number.isFinite(pct)
  if (!isValid) return <span style={{ color: '#94a3b8', fontSize: size }}>—</span>
  const up = pct > 0
  const flat = Math.abs(pct) < 0.1
  const color = flat ? '#64748b' : (up ? '#dc2626' : '#16a34a')
  const arrow = flat ? '→' : (up ? '▲' : '▼')
  return (
    <span style={{ color, fontWeight: 700, fontSize: size, whiteSpace: 'nowrap' }}>
      {arrow} {Math.abs(pct).toFixed(1)}%
    </span>
  )
}
