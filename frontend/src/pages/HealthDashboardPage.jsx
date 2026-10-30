import React, { useState, useEffect, useCallback, useRef } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'
import { formatDateTimeHK } from '../utils/time.js'
import { COLORS, CARD, Badge, TAB_BAR, tabButtonStyle, Skeleton, SkeletonLines } from '../utils/theme.js'
import { getPageCache, setPageCache } from '../utils/pageCache.js'

const CACHE_KEY = 'health-dashboard'

/** Render a Job's sequential ticket number as "INC-001042" — same format
 * as theme.js's formatTicketNumber(), duplicated inline here since the
 * Health Dashboard only has ticket_number (not a full job object). */
function formatJobTicket(ticketNumber, jobId) {
  if (ticketNumber) return `INC-${String(ticketNumber).padStart(6, '0')}`
  return jobId ? `#${jobId.slice(0, 8).toUpperCase()}` : ''
}

const STATUS_COLOR = {
  open: '#dc2626',
  upcoming: '#d97706',
  closed: '#16a34a',
}
function statusColor(status) {
  return STATUS_COLOR[status] || '#6b7280'
}
function statusLabel(status) {
  return status ? status.charAt(0).toUpperCase() + status.slice(1) : 'Unknown'
}

// Mirrors CostAnalysisPage.jsx / BillingByServicePage.jsx's formatAge().
function formatCacheAge(seconds) {
  if (!seconds || seconds < 60) return 'Data as of just now.'
  const mins = Math.floor(seconds / 60)
  return `Cached ${mins} min${mins === 1 ? '' : 's'} ago (refreshes automatically every 5 minutes).`
}

const CATEGORY_ICON = {
  issue: '🔴',
  scheduledChange: '🗓️',
  accountNotification: '🔔',
}

// Event-type codes that the backend (see main.py's _group_health_events)
// pulls out of "Scheduled changes" into their own dedicated tabs — kept
// here too so the frontend can route deep-links to the right tab without
// a round-trip.
const RETIREMENT_EVENT_CODES = new Set([
  'AWS_CLOUDSHELL_PERSISTENCE_EXPIRING',
  'AWS_EKS_FARGATE_POD_EVICTION',
  'AWS_ECS_TASK_PATCHING_RETIREMENT',
  'AWS_DIRECTCONNECT_MAINTENANCE_SCHEDULED',
])
const LIFECYCLE_EVENT_CODES = new Set([
  'AWS_RDS_ENGINE_UPGRADE',
  'AWS_LAMBDA_PLANNED_LIFECYCLE_EVENT',
  'AWS_EC2_PLANNED_LIFECYCLE_EVENT',
  'AWS_EKS_PLANNED_LIFECYCLE_EVENT',
  'AWS_RDS_PLANNED_LIFECYCLE_EVENT',
  'AWS_MQ_PLANNED_LIFECYCLE_EVENT',
  'AWS_SSM_PLANNED_LIFECYCLE_EVENT',
  'AWS_BILLING_PLANNED_LIFECYCLE_EVENT',
  'AWS_CLOUDWATCHSYNTHETICS_PLANNED_LIFECYCLE_EVENT',
  'AWS_CLOUDWATCH_PLANNED_LIFECYCLE_EVENT',
  'AWS_ORGANIZATIONS_PLANNED_LIFECYCLE_EVENT',
  'AWS_ECR_PLANNED_LIFECYCLE_EVENT',
  'AWS_ES_PLANNED_LIFECYCLE_EVENT',
  'AWS_S3_PLANNED_LIFECYCLE_EVENT',
  'AWS_SDK_PLANNED_LIFECYCLE_EVENT',
  'AWS_HEALTH_PLANNED_LIFECYCLE_EVENT',
  'AWS_TRUSTEDADVISOR_PLANNED_LIFECYCLE_EVENT',
  'AWS_MOBILETARGETING_PLANNED_LIFECYCLE_EVENT',
  'AWS_QDEVELOPER_PLANNED_LIFECYCLE_EVENT',
  'AWS_DS_PLANNED_LIFECYCLE_EVENT',
  'AWS_REDSHIFT_PLANNED_LIFECYCLE_EVENT',
])
// Event-type codes pulled OUT of whatever category AWS classifies them
// as into "Other notifications" specifically (per user request) — same
// carve-out pattern as RETIREMENT/LIFECYCLE above, mirrored from
// main.py's _OTHER_NOTIFICATION_EVENT_CODES so deep-link routing agrees
// with the backend's own classification.
const OTHER_NOTIFICATION_EVENT_CODES = new Set([
  'AWS_EKS_FARGATE_POD_EVICTIONS',
])

const TABS = [
  { key: 'open_and_recent_issues', label: 'Open and recent issues', icon: '🔴' },
  { key: 'scheduled_changes', label: 'Scheduled changes', icon: '🗓️' },
  { key: 'retirements', label: 'Retirements & evictions', icon: '⚠️' },
  { key: 'lifecycle_events', label: 'Lifecycle event', icon: '♻️' },
  { key: 'other_notifications', label: 'Other notifications', icon: '🔔' },
  { key: 'event_log', label: 'Event log', icon: '📜' },
]

// Pseudo-tab: not a real backend category, just a view over event_log
// (the flat superset of every event) filtered down to event.finished ===
// true. Kept out of TABS/_HEALTH_TAB_KEYS so the summary count cards at
// the top stay unchanged; handled specially wherever `tab` is read below.
const FINISHED_TAB_KEY = 'finished_items'

// Flatten every per-category tab list from a dashboard payload into one
// array — used both for deep-link matching and for re-locating an event
// after a save (checked/finished/remark/SR/Job) forces a reload.
function allEventsFrom(payload) {
  return [
    ...(payload.open_and_recent_issues || []),
    ...(payload.scheduled_changes || []),
    ...(payload.retirements || []),
    ...(payload.lifecycle_events || []),
    ...(payload.other_notifications || []),
  ]
}

// Which tab (if any) a given event category belongs to — used to jump to
// the right tab when deep-linking from the All Cases page (?event_arn=...),
// and to no-op a drag-and-drop drop onto the tab the event is already in.
// An admin's manual category_override (drag-and-drop) always wins over
// the automatic classification below — mirrors main.py's
// _group_health_events() precedence exactly.
function tabForEvent(event) {
  if (event.category_override) return event.category_override
  const code = event.event_type_code
  if (OTHER_NOTIFICATION_EVENT_CODES.has(code)) return 'other_notifications'
  if (event.event_type_category === 'scheduledChange') {
    if (RETIREMENT_EVENT_CODES.has(code)) return 'retirements'
    if (LIFECYCLE_EVENT_CODES.has(code)) return 'lifecycle_events'
    return 'scheduled_changes'
  }
  if (event.event_type_category === 'issue') return 'open_and_recent_issues'
  return 'other_notifications'
}

function EventDetailModal({ event, onClose, onRaiseSr, raisingSr, savingFinished, onToggleFinished, onCreateJob, creatingJob }) {
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  // resource_key -> true while a per-resource tick is mid-save, to
  // disable that row's checkbox and prevent a double-click race.
  const [savingResourceKeys, setSavingResourceKeys] = useState(() => new Set())
  // On-demand AI suggestion ("what to do / be aware of") — not fetched
  // automatically on open (would call Bedrock every time the popup is
  // opened); only requested when the engineer clicks the button below.
  const [aiSuggestion, setAiSuggestion] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const [aiErr, setAiErr] = useState('')

  const loadDetail = useCallback(() => {
    setLoading(true)
    setErr('')
    return api.getHealthEventDetail(event.arn)
      .then(d => { setDetail(d); return d })
      .catch(e => { setErr(e?.response?.data?.detail || 'Could not load event detail'); return null })
      .finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [event.arn])

  useEffect(() => { loadDetail() }, [loadDetail])

  // Reset any previously-shown AI suggestion when the popup switches to a
  // different event, so a stale suggestion from event A is never shown
  // while looking at event B.
  useEffect(() => {
    setAiSuggestion(null)
    setAiErr('')
  }, [event.arn])

  const loadAiSuggestion = async () => {
    setAiLoading(true)
    setAiErr('')
    try {
      const s = await api.getHealthEventAiSuggestion(event.arn)
      setAiSuggestion(s)
    } catch (e) {
      setAiErr(e?.response?.data?.detail || 'Could not generate AI suggestion')
    } finally {
      setAiLoading(false)
    }
  }

  // Per-resource tick — persisted server-side (who + when), same pattern
  // as the row-level "checked" checkbox on the main table, but scoped to
  // one entry inside this event's "Affected resources" list.
  const toggleResourceChecked = async (ent) => {
    setSavingResourceKeys(prev => new Set(prev).add(ent.resource_key))
    try {
      await api.setHealthEventResourceCheck(event.arn, ent.resource_key, !ent.checked)
      await loadDetail()
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not save resource checkbox state')
    } finally {
      setSavingResourceKeys(prev => {
        const next = new Set(prev)
        next.delete(ent.resource_key)
        return next
      })
    }
  }

  return (
    <div onClick={onClose}
      style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
      <div onClick={e => e.stopPropagation()}
        style={{ background: '#fff', borderRadius: 12, padding: 24, width: 580, maxHeight: '85vh', overflowY: 'auto', boxShadow: '0 10px 40px rgba(0,0,0,0.25)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 14 }}>
          <div>
            <h3 style={{ margin: 0, fontSize: 16, color: COLORS.textPrimary }}>{event.event_type_code}</h3>
            <div style={{ fontSize: 12.5, color: COLORS.textMuted, marginTop: 4 }}>
              {event.service} · {event.region || '—'}
            </div>
          </div>
          <button onClick={onClose} style={{ border: 'none', background: 'none', fontSize: 18, cursor: 'pointer', color: COLORS.textMuted, lineHeight: 1 }}>×</button>
        </div>

        {/* ── Task 12: "Mark as finished" checkbox — separate from the
            row-level "checked" checkbox on the main table. Lives at the
            top of the popup so it's the first thing an engineer sees. */}
        <label style={{
          display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14, padding: '10px 12px',
          background: event.finished ? COLORS.successBg : '#f8fafc',
          border: `1px solid ${event.finished ? COLORS.success + '40' : COLORS.border}`,
          borderRadius: 8, cursor: savingFinished ? 'default' : 'pointer',
        }}>
          <input type="checkbox" checked={!!event.finished} disabled={savingFinished}
            onChange={() => onToggleFinished(event)}
            style={{ cursor: savingFinished ? 'default' : 'pointer', width: 16, height: 16 }} />
          <span style={{ fontSize: 13, fontWeight: 600, color: event.finished ? COLORS.success : COLORS.textPrimary }}>
            ✅ Mark as finished (engineer remediation complete)
          </span>
          {event.finished && (
            <span style={{ fontSize: 11, color: COLORS.textMuted, marginLeft: 'auto' }}>
              by {event.finished_by_name || 'someone'}{event.finished_at ? ` · ${formatDateTimeHK(event.finished_at)}` : ''}
            </span>
          )}
        </label>

        {/* ── Task 12: one-click "Raise SR to AMS" + already-raised badge */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10, flexWrap: 'wrap' }}>
          {event.ams_sr_case_id ? (
            <Badge text={`🛡️ AMS SR raised: ${event.ams_sr_case_id}`} color="#9333ea" />
          ) : (
            <button onClick={() => onRaiseSr(event)} disabled={raisingSr}
              style={{
                padding: '8px 14px', borderRadius: 7, border: 'none', background: '#9333ea', color: '#fff',
                fontWeight: 600, fontSize: 12.5, cursor: raisingSr ? 'default' : 'pointer', opacity: raisingSr ? 0.6 : 1,
              }}>
              {raisingSr ? '⏳ Raising…' : '🛡️ Raise SR to AMS'}
            </button>
          )}
          {event.ams_sr_case_id && (
            <a href={`https://support.console.aws.amazon.com/support/home#/case/?caseId=${event.ams_sr_case_id}`}
              target="_blank" rel="noreferrer" style={{ fontSize: 12, color: COLORS.brand }}>
              Open in AWS Console ↗
            </a>
          )}
        </div>

        {/* ── Follow-up feature: one-click "Create Job" — independent of
            the AMS SR above; a Job can exist with or without an SR. */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 18, flexWrap: 'wrap' }}>
          {event.job_id ? (
            <Link to={`/jobs/${event.job_id}`}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 6, textDecoration: 'none',
                background: COLORS.brand + '18', color: COLORS.brand, border: `1px solid ${COLORS.brand}40`,
                borderRadius: 4, padding: '2px 8px', fontSize: 12, fontWeight: 600,
              }}>
              📋 Job created: {formatJobTicket(event.job_ticket_number, event.job_id)}
            </Link>
          ) : (
            <button onClick={() => onCreateJob(event)} disabled={creatingJob}
              style={{
                padding: '8px 14px', borderRadius: 7, border: 'none', background: COLORS.brand, color: '#fff',
                fontWeight: 600, fontSize: 12.5, cursor: creatingJob ? 'default' : 'pointer', opacity: creatingJob ? 0.6 : 1,
              }}>
              {creatingJob ? '⏳ Creating…' : '📋 Create Job for engineer follow-up'}
            </button>
          )}
        </div>

        {err && (
          <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 14, color: COLORS.danger, fontSize: 13 }}>
            {err}
          </div>
        )}

        {loading ? (
          <SkeletonLines lines={5} />
        ) : detail && (
          <>
            {/* AWS's own narrative for this event (from
                describe_event_details), same as what the AWS Health
                console shows in the event's detail pane. May be absent
                for some event types. */}
            <div style={{ marginBottom: 18 }}>
              <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 8 }}>
                Details
              </div>
              {detail.event_description ? (
                <div style={{
                  fontSize: 13, color: COLORS.textPrimary, lineHeight: 1.6, whiteSpace: 'pre-wrap',
                  background: '#f8fafc', border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: '12px 14px',
                }}>
                  {detail.event_description}
                </div>
              ) : (
                <div style={{ fontSize: 13, color: COLORS.textMuted }}>No additional details reported for this event.</div>
              )}
            </div>

            {/* On-demand AI suggestion — "what will need to be done or be
                aware of", generated by AWS Bedrock only when requested. */}
            <div style={{ marginBottom: 18 }}>
              <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 8 }}>
                🤖 AI suggestion
              </div>
              {!aiSuggestion && !aiLoading && (
                <button onClick={loadAiSuggestion}
                  style={{
                    padding: '8px 14px', borderRadius: 7, border: `1px solid ${COLORS.brand}`, background: '#fff',
                    color: COLORS.brand, fontWeight: 600, fontSize: 12.5, cursor: 'pointer',
                  }}>
                  🤖 Generate AI suggestion
                </button>
              )}
              {aiLoading && (
                <div style={{ fontSize: 13, color: COLORS.textMuted }}>⏳ Analysing with AWS Bedrock…</div>
              )}
              {aiErr && (
                <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginTop: 8, color: COLORS.danger, fontSize: 13 }}>
                  {aiErr}
                </div>
              )}
              {aiSuggestion && (
                <div style={{
                  background: '#f5f3ff', border: `1px solid #9333ea40`, borderRadius: 8, padding: '12px 14px',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                    <Badge
                      text={{
                        immediate: '🔴 Immediate attention',
                        soon: '🟠 Attend soon',
                        monitor: '🟡 Monitor',
                        informational: '🔵 Informational',
                      }[aiSuggestion.urgency] || aiSuggestion.urgency}
                      color={{
                        immediate: '#dc2626', soon: '#d97706', monitor: '#ca8a04', informational: '#2563eb',
                      }[aiSuggestion.urgency] || '#6b7280'}
                    />
                    <button onClick={loadAiSuggestion}
                      style={{ marginLeft: 'auto', border: 'none', background: 'none', color: COLORS.textMuted, fontSize: 11.5, cursor: 'pointer', textDecoration: 'underline' }}>
                      🔄 Regenerate
                    </button>
                  </div>
                  {aiSuggestion.summary && (
                    <div style={{ fontSize: 13, color: COLORS.textPrimary, lineHeight: 1.6, marginBottom: 10 }}>{aiSuggestion.summary}</div>
                  )}
                  {aiSuggestion.action_items?.length > 0 && (
                    <div style={{ marginBottom: aiSuggestion.awareness_items?.length ? 10 : 0 }}>
                      <div style={{ fontSize: 11, fontWeight: 700, color: '#9333ea', marginBottom: 4 }}>✅ What to do</div>
                      <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12.5, color: COLORS.textPrimary, lineHeight: 1.7 }}>
                        {aiSuggestion.action_items.map((s, i) => <li key={i}>{s}</li>)}
                      </ul>
                    </div>
                  )}
                  {aiSuggestion.awareness_items?.length > 0 && (
                    <div>
                      <div style={{ fontSize: 11, fontWeight: 700, color: '#9333ea', marginBottom: 4 }}>⚠️ Be aware of</div>
                      <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12.5, color: COLORS.textPrimary, lineHeight: 1.7 }}>
                        {aiSuggestion.awareness_items.map((s, i) => <li key={i}>{s}</li>)}
                      </ul>
                    </div>
                  )}
                  {!aiSuggestion.action_items?.length && !aiSuggestion.awareness_items?.length && (
                    <div style={{ fontSize: 12.5, color: COLORS.textMuted }}>No specific action needed — purely informational.</div>
                  )}
                </div>
              )}
            </div>

            <div style={{ marginBottom: 18 }}>
              <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 8 }}>
                Affected accounts ({detail.affected_accounts?.length ?? 0})
              </div>
              {(!detail.affected_accounts || detail.affected_accounts.length === 0) ? (
                <div style={{ fontSize: 13, color: COLORS.textMuted }}>No affected accounts reported for this event.</div>
              ) : (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  {detail.affected_accounts.map(acc => (
                    <span key={acc} style={{ background: '#f1f5f9', borderRadius: 5, padding: '3px 9px', fontSize: 12.5, fontFamily: 'monospace', color: COLORS.textPrimary }}>{acc}</span>
                  ))}
                </div>
              )}
            </div>

            <div>
              <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700, marginBottom: 8 }}>
                Affected resources ({detail.affected_entities?.length ?? 0})
              </div>
              {(!detail.affected_entities || detail.affected_entities.length === 0) ? (
                <div style={{ fontSize: 13, color: COLORS.textMuted }}>No affected resources reported for this event.</div>
              ) : (
                <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
                  <thead>
                    <tr style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                      <th style={{ textAlign: 'center', padding: '6px 8px', width: 28 }}></th>
                      <th style={{ textAlign: 'left', padding: '6px 8px', color: '#475569', fontWeight: 600 }}>Resource</th>
                      <th style={{ textAlign: 'left', padding: '6px 8px', color: '#475569', fontWeight: 600 }}>Account</th>
                      <th style={{ textAlign: 'left', padding: '6px 8px', color: '#475569', fontWeight: 600 }}>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.affected_entities.map((ent, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid #f1f5f9' }}>
                        <td style={{ padding: '6px 8px', textAlign: 'center' }}>
                          <input type="checkbox" checked={!!ent.checked}
                            disabled={savingResourceKeys.has(ent.resource_key)}
                            onChange={() => toggleResourceChecked(ent)}
                            style={{ cursor: savingResourceKeys.has(ent.resource_key) ? 'default' : 'pointer' }} />
                        </td>
                        <td style={{ padding: '6px 8px' }}>
                          <div style={{ fontFamily: 'monospace' }}>{ent.entity_value || '—'}</div>
                          {ent.checked && (
                            <div style={{ fontSize: 10, color: COLORS.success, marginTop: 2 }}>
                              ✅ {ent.checked_by_name || 'someone'}{ent.checked_at ? ` · ${formatDateTimeHK(ent.checked_at)}` : ''}
                            </div>
                          )}
                        </td>
                        <td style={{ padding: '6px 8px', fontFamily: 'monospace' }}>{ent.aws_account_id || '—'}</td>
                        <td style={{ padding: '6px 8px' }}>{ent.status_code || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// Debounced remark textbox — saves ~800ms after the user stops typing, so
// every keystroke doesn't fire a network request. Local `value` state
// keeps typing snappy; onCommit persists to the server.
function RemarkBox({ eventArn, initialValue, onCommit, disabled }) {
  const [value, setValue] = useState(initialValue || '')
  const [saved, setSaved] = useState(false)
  const timerRef = useRef(null)

  useEffect(() => { setValue(initialValue || '') }, [initialValue])

  const handleChange = (e) => {
    const v = e.target.value
    setValue(v)
    setSaved(false)
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(async () => {
      await onCommit(eventArn, v)
      setSaved(true)
    }, 800)
  }

  return (
    <input type="text" value={value} onChange={handleChange} disabled={disabled}
      placeholder="Add a remark…"
      style={{
        width: 160, padding: '5px 8px', borderRadius: 6, border: `1px solid ${COLORS.border}`,
        fontSize: 12, boxSizing: 'border-box',
      }} />
  )
}

export default function HealthDashboardPage() {
  const { user } = useAuth()
  // Drag-and-drop tab override is admin-only — same convention as the
  // Reseller Cost / Cost Analysis manual-adjustment features.
  const canManageCategory = !!user?.is_admin

  const [searchParams, setSearchParams] = useSearchParams()
  // Seed straight from the in-tab page cache (see utils/pageCache.js) so
  // re-visiting this page after navigating away renders the last-seen
  // data immediately instead of a blank "Loading…" screen — the actual
  // network refresh still happens below, just silently in the
  // background instead of blocking the first paint.
  const cached = getPageCache(CACHE_KEY)
  const [data, setData] = useState(cached || null)
  const [loading, setLoading] = useState(!cached)
  const [err, setErr] = useState('')
  const [tab, setTab] = useState('open_and_recent_issues')
  const [detailEvent, setDetailEvent] = useState(null)
  // ARNs currently mid-save — disables their checkbox to prevent
  // double-clicks racing the server round-trip.
  const [savingArns, setSavingArns] = useState(() => new Set())
  const [raisingSrArns, setRaisingSrArns] = useState(() => new Set())
  const [savingFinishedArns, setSavingFinishedArns] = useState(() => new Set())
  const [creatingJobArns, setCreatingJobArns] = useState(() => new Set())
  const [srErr, setSrErr] = useState('')
  const [jobErr, setJobErr] = useState('')
  // Hide/fold rows the engineer has already ticked "Mark as finished" on
  // (Task: filter to fold finished items). Defaults to hiding them since
  // that's the whole point of marking something finished — declutter the
  // list down to what still needs attention.
  const [hideFinished, setHideFinished] = useState(true)

  // refreshing (not loading) drives the button label for an explicit
  // "🔄 Refresh" click — kept separate from the initial-load spinner so a
  // background silent refresh (see the 5-min interval below) never shows
  // a jarring full-page "Loading…" once data is already on screen.
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback((refresh = false) => {
    // Only show the full-page "Loading…" state when there's nothing
    // cached to show yet — if we already seeded from the page cache,
    // this call runs invisibly in the background (loading stays false).
    if (refresh) setRefreshing(true)
    else if (!getPageCache(CACHE_KEY)) setLoading(true)
    setErr('')
    return api.getOrgHealthDashboard(refresh)
      .then(d => { setData(d); setPageCache(CACHE_KEY, d); return d })
      .catch(e => { setErr(e?.response?.data?.detail || 'Could not load AWS Health Dashboard'); return null })
      .finally(() => { setLoading(false); setRefreshing(false) })
  }, [])

  useEffect(() => { load(false) }, [load])

  // Server-side cache is 5 minutes (see cache_utils.py) — silently
  // re-fetch every 5 minutes in the background so the page's data never
  // sits stale for long without the user having to click Refresh, while
  // still not hammering describe_events_for_organization() on every page
  // visit. force_refresh=false here: this rides the same server cache
  // window rather than always bypassing it, so a tab left open in two
  // browsers doesn't double the AWS Health API call rate.
  useEffect(() => {
    const id = setInterval(() => { load(false) }, 5 * 60 * 1000)
    return () => clearInterval(id)
  }, [load])

  // Deep-link support (Task 12, sub-feature 4): the All Cases page links
  // here with ?event_arn=... — once the dashboard data loads, jump to the
  // right tab and auto-open that event's detail modal.
  const deepLinkArn = searchParams.get('event_arn')
  useEffect(() => {
    if (!data || !deepLinkArn) return
    const all = allEventsFrom(data)
    const match = all.find(e => e.arn === deepLinkArn)
    if (match) {
      setTab(tabForEvent(match))
      setDetailEvent(match)
    }
    // Clear the query param so a manual refresh/close doesn't re-trigger it.
    setSearchParams({}, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, deepLinkArn])

  const isFinishedTab = tab === FINISHED_TAB_KEY
  // The Finished tab pulls from event_log (the flat superset of every
  // event, across all categories) rather than a per-category list, so an
  // event finished under any category still shows up here.
  const allEventsInTab = data ? (isFinishedTab ? (data.event_log || []) : (data[tab] || [])) : []
  const finishedCountInTab = allEventsInTab.filter(e => e.finished).length
  const totalFinishedCount = data ? (data.event_log || []).filter(e => e.finished).length : 0
  const events = isFinishedTab
    ? allEventsInTab.filter(e => e.finished)
    : (hideFinished ? allEventsInTab.filter(e => !e.finished) : allEventsInTab)

  // Ticking/unticking is persisted server-side (who + when), so a toggle
  // is a real API call rather than local-only UI state. Re-fetches the
  // whole dashboard afterwards so every tab's copy of this event (an
  // event can appear in both its category tab and the flat Event log)
  // reflects the new checked state consistently.
  const toggleChecked = async (event) => {
    setSavingArns(prev => new Set(prev).add(event.arn))
    try {
      await api.setHealthEventCheck(event.arn, !event.checked)
      await load()
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not save checkbox state')
    } finally {
      setSavingArns(prev => {
        const next = new Set(prev)
        next.delete(event.arn)
        return next
      })
    }
  }

  const toggleAllInTab = async () => {
    const allChecked = events.length > 0 && events.every(e => e.checked)
    const targetArns = events.filter(e => e.checked === allChecked).map(e => e.arn)
    setSavingArns(prev => {
      const next = new Set(prev)
      targetArns.forEach(a => next.add(a))
      return next
    })
    try {
      await Promise.all(targetArns.map(arn => api.setHealthEventCheck(arn, !allChecked)))
      await load()
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not save checkbox state')
    } finally {
      setSavingArns(prev => {
        const next = new Set(prev)
        targetArns.forEach(a => next.delete(a))
        return next
      })
    }
  }

  // Remark box next to "Last Updated" — used by all 4 tabs (same table).
  const commitRemark = async (eventArn, remark) => {
    try {
      await api.setHealthEventRemark(eventArn, remark)
      // No full reload here — reloading on every debounced keystroke-save
      // would fight the input's own local state / cursor position. The
      // saved remark is picked up naturally on the next manual refresh or
      // tab switch.
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not save remark')
    }
  }

  // "Mark as finished" inside the event-detail popup.
  const toggleFinished = async (event) => {
    setSavingFinishedArns(prev => new Set(prev).add(event.arn))
    try {
      await api.setHealthEventFinished(event.arn, !event.finished)
      const fresh = await load()
      if (fresh) {
        const all = allEventsFrom(fresh)
        const updated = all.find(e => e.arn === event.arn)
        if (updated) setDetailEvent(updated)
      }
    } catch (e) {
      setErr(e?.response?.data?.detail || 'Could not save "finished" state')
    } finally {
      setSavingFinishedArns(prev => {
        const next = new Set(prev)
        next.delete(event.arn)
        return next
      })
    }
  }

  // Admin move-to-tab: click a "↔️ Move" button on a row to open a small
  // dropdown menu of the other tabs, pick one to set that event's manual
  // category override. `moveMenuArn` is the event whose menu is
  // currently open (null = closed); `moveErr` surfaces a failure without
  // needing a full page-level error banner takeover.
  const [moveMenuArn, setMoveMenuArn] = useState(null)
  const [movingArns, setMovingArns] = useState(() => new Set())
  const [moveErr, setMoveErr] = useState('')

  // Close the "Move to tab" dropdown when clicking anywhere outside it
  // (the menu itself stops propagation via its own onClick, so this only
  // fires for genuine outside clicks).
  useEffect(() => {
    if (!moveMenuArn) return
    const closeMenu = () => setMoveMenuArn(null)
    document.addEventListener('click', closeMenu)
    return () => document.removeEventListener('click', closeMenu)
  }, [moveMenuArn])

  const moveEventToCategory = async (event, targetTabKey) => {
    if (!canManageCategory) return
    setMoveMenuArn(null)
    // No-op if picking the tab it's already effectively in.
    if (tabForEvent(event) === targetTabKey) return
    setMoveErr('')
    setMovingArns(prev => new Set(prev).add(event.arn))
    try {
      await api.setHealthEventCategory(event.arn, targetTabKey)
      await load()
    } catch (e) {
      setMoveErr(e?.response?.data?.detail || 'Could not move event to that tab')
    } finally {
      setMovingArns(prev => {
        const next = new Set(prev)
        next.delete(event.arn)
        return next
      })
    }
  }

  // Clear a previously-set manual category override, reverting the event
  // back to automatic AWS eventTypeCategory/eventTypeCode classification.
  const undoCategoryOverride = async (event) => {
    if (!canManageCategory) return
    setMoveErr('')
    setMovingArns(prev => new Set(prev).add(event.arn))
    try {
      await api.setHealthEventCategory(event.arn, null)
      await load()
    } catch (e) {
      setMoveErr(e?.response?.data?.detail || 'Could not clear category override')
    } finally {
      setMovingArns(prev => {
        const next = new Set(prev)
        next.delete(event.arn)
        return next
      })
    }
  }

  // One-click "Raise SR to AMS" — from either the modal or a future row
  // button. Persists case_id onto the event so the badge/icon shows up
  // everywhere this event is rendered.
  const raiseSr = async (event) => {
    setSrErr('')
    setRaisingSrArns(prev => new Set(prev).add(event.arn))
    try {
      await api.raiseAmsSrForHealthEvent(event)
      const fresh = await load()
      if (fresh) {
        const all = allEventsFrom(fresh)
        const updated = all.find(e => e.arn === event.arn)
        if (updated) setDetailEvent(updated)
      }
    } catch (e) {
      setSrErr(e?.response?.data?.detail || 'Could not raise AMS Service Request')
    } finally {
      setRaisingSrArns(prev => {
        const next = new Set(prev)
        next.delete(event.arn)
        return next
      })
    }
  }

  // One-click "Create Job" — for events that need engineer follow-up,
  // whether or not an AMS SR was also raised for the same event. Persists
  // job_id onto the event so the badge/link shows up everywhere this
  // event is rendered.
  const createJob = async (event) => {
    setJobErr('')
    setCreatingJobArns(prev => new Set(prev).add(event.arn))
    try {
      await api.createJobFromHealthEvent(event)
      const fresh = await load()
      if (fresh) {
        const all = allEventsFrom(fresh)
        const updated = all.find(e => e.arn === event.arn)
        if (updated) setDetailEvent(updated)
      }
    } catch (e) {
      setJobErr(e?.response?.data?.detail || 'Could not create Job')
    } finally {
      setCreatingJobArns(prev => {
        const next = new Set(prev)
        next.delete(event.arn)
        return next
      })
    }
  }

  return (
    <div style={{ padding: '24px 28px', maxWidth: 1320 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 18 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>🩺 AWS Health Dashboard (Your organization health)</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            Organization-wide AWS Health events, fetched via the AWS AMS support account —
            the only account in this Organization with permission to view org-wide Health data.
          </div>
        </div>
        <button onClick={() => load(true)} disabled={loading || refreshing}
          style={{ padding: '8px 16px', borderRadius: 7, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontWeight: 600, fontSize: 13, cursor: (loading || refreshing) ? 'default' : 'pointer' }}>
          {refreshing ? '⏳ Refreshing…' : loading ? '⏳ Loading…' : '🔄 Refresh'}
        </button>
      </div>

      {data && !loading && (
        <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginTop: -10, marginBottom: 12 }}>
          ℹ️ {formatCacheAge(data.cache_age_seconds)}
        </div>
      )}

      {err && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {err}
        </div>
      )}
      {srErr && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {srErr}
        </div>
      )}
      {jobErr && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 16, color: COLORS.danger, fontSize: 13 }}>
          {jobErr}
        </div>
      )}

      {data && (
        <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
          {TABS.map(t => (
            <div key={t.key} style={{ ...CARD, padding: '12px 18px', minWidth: 160 }}>
              <div style={{ fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 700 }}>{t.icon} {t.label}</div>
              <div style={{ fontSize: 22, fontWeight: 700, color: COLORS.textPrimary, marginTop: 4 }}>{data.counts?.[t.key] ?? 0}</div>
            </div>
          ))}
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14, borderBottom: `2px solid ${COLORS.border}`, flexWrap: 'wrap', gap: 8 }}>
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          {TABS.map(t => (
            <button key={t.key} onClick={() => setTab(t.key)} style={tabButtonStyle(tab === t.key)}>
              {t.icon} {t.label}
            </button>
          ))}
          {/* Dedicated tab for reviewing everything already marked
              finished, across all categories (pulled from event_log). */}
          <button onClick={() => setTab(FINISHED_TAB_KEY)} style={tabButtonStyle(isFinishedTab, COLORS.success)}>
            ✅ Finished{totalFinishedCount > 0 ? ` (${totalFinishedCount})` : ''}
          </button>
        </div>
        {/* Fold/filter finished items — hides rows where the engineer has
            already ticked "Mark as finished" in the detail popup, so the
            table declutters down to what still needs attention. Not shown
            on the Finished tab itself (that tab IS the finished list). */}
        {!isFinishedTab && (
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5, color: COLORS.textSecondary, cursor: 'pointer', paddingBottom: 10, whiteSpace: 'nowrap' }}>
            <input type="checkbox" checked={hideFinished} onChange={() => setHideFinished(v => !v)}
              style={{ cursor: 'pointer' }} />
            🙈 Hide finished{finishedCountInTab > 0 ? ` (${finishedCountInTab})` : ''}
          </label>
        )}
      </div>

      {/* Admin-only hint + error surface for the move-to-tab feature —
          click the ↔️ Move button next to a row's category icon to open
          a dropdown of the other tabs and manually override that
          event's category. */}
      {canManageCategory && !isFinishedTab && (
        <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginTop: -10, marginBottom: 10 }}>
          ↔️ Admin tip: click "Move" next to a row's category icon to move that event to a different tab.
        </div>
      )}
      {moveErr && (
        <div style={{ background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', marginBottom: 14, color: COLORS.danger, fontSize: 13 }}>
          {moveErr}
        </div>
      )}

      {/* overflowX auto (not hidden) so the wide 11-column table — which
          no longer fits inside the page's 1320px maxWidth once the
          Remark/AMS SR/Job columns were added — scrolls horizontally
          inside the card instead of having its rightmost column
          (Job) clipped off. overflowY stays hidden so the card's
          rounded corners are still preserved against the table's
          square-cornered header row. */}
      <div style={{ ...CARD, overflowX: 'auto', overflowY: 'hidden' }}>
        <table data-cards style={{ width: '100%', minWidth: 1360, borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ background: '#f8fafc', borderBottom: `2px solid ${COLORS.border}` }}>
              <th style={{ padding: '10px 14px', textAlign: 'center', width: 36 }}>
                <input type="checkbox" style={{ cursor: 'pointer' }}
                  checked={events.length > 0 && events.every(e => e.checked)}
                  onChange={toggleAllInTab} />
              </th>
              {['Category', 'Service', 'Event Name', 'Region', 'AWS Status', 'Started', 'Last Updated', 'Remark', 'AMS SR', 'Job'].map(h => (
                <th key={h} style={{ padding: '10px 14px', textAlign: 'left', color: '#475569', fontWeight: 600, fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.3 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              Array.from({ length: 6 }, (_, i) => (
                <tr key={`sk-${i}`}>
                  <td colSpan={11} style={{ padding: '10px 16px' }}>
                    <Skeleton height={16} />
                  </td>
                </tr>
              ))
            ) : events.length === 0 ? (
              <tr><td colSpan={11} style={{ padding: 40, textAlign: 'center', color: COLORS.textMuted }}>No events in this category</td></tr>
            ) : events.map(e => (
              <tr key={e.arn} style={{ borderBottom: '1px solid #f1f5f9' }}>
                <td style={{ padding: '10px 14px', textAlign: 'center' }}>
                  <input type="checkbox" style={{ cursor: savingArns.has(e.arn) ? 'default' : 'pointer' }}
                    checked={!!e.checked}
                    disabled={savingArns.has(e.arn)}
                    onChange={() => toggleChecked(e)} />
                </td>
                <td style={{ padding: '10px 14px', position: 'relative' }}>
                  <span>{CATEGORY_ICON[e.event_type_category] || '•'}</span>
                  {e.category_override && (
                    <span title={`Manually moved to this tab by ${e.category_override_by_name || 'an admin'}${e.category_override_at ? ` on ${formatDateTimeHK(e.category_override_at)}` : ''}${canManageCategory ? ' — click to undo' : ''}`}
                      onClick={canManageCategory ? () => undoCategoryOverride(e) : undefined}
                      style={{ marginLeft: 4, fontSize: 10, cursor: canManageCategory ? 'pointer' : 'default' }}>✋</span>
                  )}
                  {movingArns.has(e.arn) && <span style={{ marginLeft: 4, fontSize: 10 }}>⏳</span>}
                  {/* Admin-only "Move to tab" button — opens a small
                      dropdown menu of the other tabs; clicking one sets
                      that event's manual category override. Replaces the
                      earlier drag-and-drop attempt, which didn't work
                      reliably across browsers/table layouts. */}
                  {canManageCategory && !isFinishedTab && (
                    <>
                      <button
                        onClick={(ev) => { ev.stopPropagation(); setMoveMenuArn(cur => (cur === e.arn ? null : e.arn)) }}
                        disabled={movingArns.has(e.arn)}
                        title="Move this event to a different tab"
                        style={{
                          marginLeft: 6, padding: '1px 6px', borderRadius: 5, border: `1px solid ${COLORS.border}`,
                          background: '#fff', color: COLORS.textSecondary, fontSize: 10, fontWeight: 600,
                          cursor: movingArns.has(e.arn) ? 'default' : 'pointer',
                        }}>
                        ↔️ Move
                      </button>
                      {moveMenuArn === e.arn && (
                        <div onClick={ev => ev.stopPropagation()}
                          style={{
                            position: 'absolute', top: '100%', left: 0, zIndex: 20, marginTop: 2,
                            background: '#fff', border: `1px solid ${COLORS.border}`, borderRadius: 8,
                            boxShadow: '0 6px 20px rgba(0,0,0,0.15)', minWidth: 190, padding: 4,
                          }}>
                          <div style={{ fontSize: 10.5, color: COLORS.textMuted, padding: '4px 8px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0.3 }}>
                            Move to tab
                          </div>
                          {TABS.filter(t => t.key !== 'event_log').map(t => (
                            <button key={t.key} onClick={() => moveEventToCategory(e, t.key)}
                              disabled={tabForEvent(e) === t.key}
                              style={{
                                display: 'flex', alignItems: 'center', gap: 6, width: '100%', textAlign: 'left',
                                padding: '7px 8px', border: 'none', background: 'none', borderRadius: 5, fontSize: 12.5,
                                color: tabForEvent(e) === t.key ? COLORS.textMuted : COLORS.textPrimary,
                                cursor: tabForEvent(e) === t.key ? 'default' : 'pointer',
                                fontWeight: tabForEvent(e) === t.key ? 400 : 500,
                              }}>
                              {t.icon} {t.label}{tabForEvent(e) === t.key ? ' (current)' : ''}
                            </button>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </td>
                <td style={{ padding: '10px 14px', color: COLORS.textPrimary, fontWeight: 600 }}>{e.service}</td>
                <td style={{ padding: '10px 14px', maxWidth: 260 }}>
                  <button onClick={() => setDetailEvent(e)}
                    style={{
                      background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                      color: COLORS.brand, fontFamily: 'monospace', fontSize: 11.5,
                      textDecoration: 'underline', textAlign: 'left', display: 'block',
                      overflowWrap: 'break-word', wordBreak: 'break-word', whiteSpace: 'normal',
                    }}>
                    {e.event_type_code}
                  </button>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 3 }}>
                    {e.checked && (
                      <div style={{ fontSize: 10.5, color: COLORS.textMuted }}>
                        ✅ Checked by {e.checked_by_name || e.checked_by_email || 'someone'}
                        {e.checked_at ? ` on ${formatDateTimeHK(e.checked_at)}` : ''}
                      </div>
                    )}
                    {e.finished && (
                      <div style={{ fontSize: 10.5, color: COLORS.success }}>
                        🏁 Finished by {e.finished_by_name || 'someone'}
                        {e.finished_at ? ` on ${formatDateTimeHK(e.finished_at)}` : ''}
                      </div>
                    )}
                  </div>
                </td>
                <td style={{ padding: '10px 14px', color: '#64748b' }}>{e.region || '—'}</td>
                <td style={{ padding: '10px 14px' }}>
                  <Badge text={statusLabel(e.status_code)} color={statusColor(e.status_code)} />
                </td>
                <td style={{ padding: '10px 14px', color: '#64748b' }}>{e.start_time ? formatDateTimeHK(e.start_time) : '—'}</td>
                <td style={{ padding: '10px 14px', color: '#64748b' }}>{e.last_updated_time ? formatDateTimeHK(e.last_updated_time) : '—'}</td>
                <td style={{ padding: '10px 14px' }}>
                  <RemarkBox eventArn={e.arn} initialValue={e.remark} onCommit={commitRemark} />
                </td>
                <td style={{ padding: '10px 14px' }}>
                  {e.ams_sr_case_id ? (
                    <span title={`AMS SR ${e.ams_sr_case_id} — status: ${e.ams_sr_case_status || 'unknown'}`}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11, fontFamily: 'monospace', color: '#9333ea', fontWeight: 700 }}>
                      🛡️ {e.ams_sr_case_id.length > 14 ? e.ams_sr_case_id.slice(0, 14) + '…' : e.ams_sr_case_id}
                    </span>
                  ) : (
                    <button onClick={() => raiseSr(e)} disabled={raisingSrArns.has(e.arn)}
                      title="Raise a Service Request to AMS for this event"
                      style={{
                        padding: '4px 9px', borderRadius: 6, border: '1px solid #9333ea40', background: '#9333ea18',
                        color: '#9333ea', fontWeight: 600, fontSize: 11, cursor: raisingSrArns.has(e.arn) ? 'default' : 'pointer',
                        opacity: raisingSrArns.has(e.arn) ? 0.6 : 1, whiteSpace: 'nowrap',
                      }}>
                      {raisingSrArns.has(e.arn) ? '⏳…' : '🛡️ Raise SR'}
                    </button>
                  )}
                </td>
                <td style={{ padding: '10px 14px' }}>
                  {e.job_id ? (
                    <Link to={`/jobs/${e.job_id}`}
                      title={`Job created by ${e.job_created_by_name || 'someone'}${e.job_created_at ? ` on ${formatDateTimeHK(e.job_created_at)}` : ''}`}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, fontWeight: 700, color: COLORS.brand, textDecoration: 'none', whiteSpace: 'nowrap' }}>
                      📋 {formatJobTicket(e.job_ticket_number, e.job_id)}
                    </Link>
                  ) : (
                    <button onClick={() => createJob(e)} disabled={creatingJobArns.has(e.arn)}
                      title="Create a portal Job for engineer follow-up on this event"
                      style={{
                        padding: '4px 9px', borderRadius: 6, border: `1px solid ${COLORS.brand}40`, background: COLORS.brand + '18',
                        color: COLORS.brand, fontWeight: 600, fontSize: 11, cursor: creatingJobArns.has(e.arn) ? 'default' : 'pointer',
                        opacity: creatingJobArns.has(e.arn) ? 0.6 : 1, whiteSpace: 'nowrap',
                      }}>
                      {creatingJobArns.has(e.arn) ? '⏳…' : '📋 Create Job'}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {detailEvent && (
        <EventDetailModal
          event={detailEvent}
          onClose={() => setDetailEvent(null)}
          onRaiseSr={raiseSr}
          raisingSr={raisingSrArns.has(detailEvent.arn)}
          savingFinished={savingFinishedArns.has(detailEvent.arn)}
          onToggleFinished={toggleFinished}
          onCreateJob={createJob}
          creatingJob={creatingJobArns.has(detailEvent.arn)}
        />
      )}
    </div>
  )
}
