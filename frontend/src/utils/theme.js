import React from 'react'

// Shared design tokens for the ITSM-style redesign — modelled loosely on
// Freshservice's visual language (deep teal/navy sidebar, clean white
// content area, pill-shaped status/priority badges, Inter-style
// typography) without copying any of their actual assets/CSS. Centralised
// here so every page pulls colors/spacing from one place instead of
// re-declaring the same hex codes inline.

export const COLORS = {
  sidebarBg: '#0b2540',
  sidebarBgActive: '#123a63',
  sidebarText: '#9fb4cc',
  sidebarTextActive: '#ffffff',
  sidebarBorder: 'rgba(255,255,255,0.08)',

  brand: '#1a4080',
  brandDark: '#0f2e5a',
  brandLight: '#e8f0fe',

  pageBg: '#f4f6f9',
  cardBg: '#ffffff',
  border: '#e5e9f0',

  textPrimary: '#1e2a3a',
  textSecondary: '#5b6b82',
  textMuted: '#94a3b8',

  success: '#16a34a',
  successBg: '#f0fdf4',
  warning: '#d97706',
  warningBg: '#fffbeb',
  danger: '#dc2626',
  dangerBg: '#fef2f2',
  info: '#0891b2',
  infoBg: '#f0fdff',
}

// KPI "tile" accent colors — a small, fixed rotation of solid brand-ish
// colors used for the square icon chip on Dashboard-style KPI cards (see
// KpiCard in DashboardPage.jsx). Modelled on the reference lounge-ops
// dashboard screenshot (blue/green/orange/purple square icon tiles next
// to each stat) rather than the single accent-bar-per-card look this
// portal used before. Cycle through KPI_ACCENTS[i % length] when
// rendering a row of KPI cards so an arbitrary-length row still gets a
// sensible color without every call site hardcoding four hex values.
export const KPI_ACCENTS = ['#2563eb', '#16a34a', '#d97706', '#7c3aed', '#0891b2', '#db2777']

/** Standard blue "info" banner — used for read-only notices, scoping
 * hints, etc. (e.g. "Read-only view — data is fetched from X and cannot
 * be modified here."). Centralised here (rather than each page inlining
 * its own blue box) so every such notice across the portal looks
 * identical. Renders nothing if `text` is falsy, so a call site can pass
 * a conditional message without an extra guard. */
export function InfoBanner({ icon = 'ℹ️', text, right }) {
  if (!text) return null
  return React.createElement(
    'div',
    {
      style: {
        display: 'flex', alignItems: 'center', gap: 10,
        background: COLORS.infoBg, border: `1px solid ${COLORS.info}30`,
        borderRadius: 8, padding: '9px 14px', marginBottom: 16,
        color: COLORS.brandDark, fontSize: 12.5,
      },
    },
    React.createElement('span', { style: { fontSize: 14, flexShrink: 0 } }, icon),
    React.createElement('span', { style: { flex: 1 } }, text),
    right ? React.createElement('span', { style: { flexShrink: 0, color: COLORS.textMuted, fontSize: 11.5 } }, right) : null
  )
}

export const STATUS_COLOR = {
  'Open': '#2563eb',
  'Review by Engineer': '#7c3aed',
  'Pending Release': '#9333ea',
  'Progress': '#0891b2',
  'On Hold': '#d97706',
  'Fail and Rollback': '#dc2626',
  'Resolved': '#16a34a',
  'Pending Review': '#0284c7',
  'Closed': '#6b7280',
}

export const PRIORITY_COLOR = { ASAP: '#dc2626', High: '#ea580c', Medium: '#2563eb', Low: '#16a34a' }

// AWS Support / AMS raw case status strings (e.g. "opened",
// "pending-customer-action", "resolved") -> color. Shared by JobDetail's
// Escalation Actions panel and the All Cases page so both render the same
// badge colors for the same status.
export const CASE_STATUS_COLOR = {
  opened: '#2563eb',
  'pending-customer-action': '#d97706',
  reopened: '#ea580c',
  unassigned: '#6b7280',
  'work-in-progress': '#0891b2',
  resolved: '#16a34a',
}

export function caseStatusColor(status) {
  return CASE_STATUS_COLOR[status] || '#6b7280'
}

export function caseStatusLabel(status) {
  return status
    ? status.split('-').map(w => w[0].toUpperCase() + w.slice(1)).join(' ')
    : 'Unknown'
}

export const CARD = {
  background: COLORS.cardBg,
  borderRadius: 10,
  boxShadow: '0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04)',
  border: `1px solid ${COLORS.border}`,
}

/** Standard outlined-pill style for status/priority/severity/type text —
 * tinted background (color+'18'), matching-tint border (color+'40'),
 * solid colored text. This is the one shared look every status-ish badge
 * across the portal should use (screenshot reference: Open/Progress/
 * Resolved/ASAP-style pills), instead of each page re-declaring its own
 * slightly different inline object (some pages used solid color fills
 * with white text, others had tiny padding/font-size differences). Call
 * as a plain style object — `<span style={badgeStyle(color)}>text</span>`
 * — or use the <Badge> component below for the common case. */
export function badgeStyle(color) {
  return {
    display: 'inline-block',
    background: color + '18',
    color,
    border: `1px solid ${color}40`,
    borderRadius: 4,
    padding: '2px 9px',
    fontSize: 11.5,
    fontWeight: 700,
    whiteSpace: 'nowrap',
    lineHeight: 1.5,
  }
}

/** Shared status/priority/severity/type pill component. Renders `text` in
 * the standard outlined style (see badgeStyle above). Every page that
 * used to declare its own local `function Badge({ text, color })` now
 * imports this one instead, so every badge in the portal — CMDB resource
 * status, AI impact severity, ticket status/priority, feedback type,
 * case account tag, admin role — looks pixel-identical. */
export function Badge({ text, color }) {
  return React.createElement('span', { style: badgeStyle(color) }, text)
}

/** Standard underline-style section tab bar (used by pages with 2+ sub-
 * views switched via buttons, e.g. Calendars' Job/Freshservice tabs and
 * the AWS Health Dashboard's category tabs) — a flex row under a 2px
 * border, active tab gets a colored bottom border + brand text color.
 * tabButtonStyle(active) returns the per-button style; TAB_BAR is the
 * shared container style for the row itself. */
export const TAB_BAR = {
  display: 'flex', gap: 4, flexWrap: 'wrap', borderBottom: `2px solid ${COLORS.border}`,
}

export function tabButtonStyle(active, activeColor = COLORS.brand) {
  return {
    padding: '10px 16px', border: 'none', background: 'none', cursor: 'pointer',
    fontSize: 13.5, fontWeight: 600,
    color: active ? activeColor : COLORS.textSecondary,
    borderBottom: active ? `2px solid ${activeColor}` : '2px solid transparent',
    marginBottom: -2,
  }
}

/** Shared "shimmer" loading skeleton — a gray, gently-pulsing block used
 * in place of the plain "Loading…" text most pages showed before. Pass
 * `width`/`height`/`radius` to shape it for the context (a table row, a
 * card, a KPI number, etc.). Uses a CSS animation defined once globally
 * in index.css (`skeletonPulse`) rather than an inline @keyframes —
 * inline style objects can't declare keyframes, so the animation name is
 * referenced here and defined there. */
export function Skeleton({ width = '100%', height = 14, radius = 6, style }) {
  return React.createElement('div', {
    style: {
      width, height, borderRadius: radius,
      background: '#eef1f6',
      animation: 'skeletonPulse 1.4s ease-in-out infinite',
      ...style,
    },
  })
}

/** A row of Skeleton blocks stacked vertically with gaps — the common
 * "a few lines of placeholder text" shape used for list/table loading
 * states. `lines`: how many bars; `lastLineWidth`: the final bar is
 * shorter by default (mimics ragged real text) unless overridden. */
export function SkeletonLines({ lines = 3, gap = 10, lastLineWidth = '60%' }) {
  return React.createElement(
    'div',
    { style: { display: 'flex', flexDirection: 'column', gap } },
    Array.from({ length: lines }, (_, i) =>
      React.createElement(Skeleton, { key: i, width: i === lines - 1 ? lastLineWidth : '100%' })
    )
  )
}

/** Render a ticket's sequential number as "INC-001042", falling back to a
 * short ID prefix for legacy records that predate ticket numbering. */
export function formatTicketNumber(job) {
  if (!job) return ''
  if (job.ticket_number) return `INC-${String(job.ticket_number).padStart(6, '0')}`
  return job.id ? `#${job.id.slice(0, 8).toUpperCase()}` : ''
}


