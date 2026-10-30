import React, { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../api.js'
import { COLORS, CARD, Badge, InfoBanner, SkeletonLines } from '../utils/theme.js'

// ── Website Monitoring (Monitoring) ───────────────────────────────────────
// UP/DOWN, HTTP status code, response time and the last successful
// screenshot of each monitored website, as seen by CloudWatch Synthetics
// probes in US / London / Singapore / Mumbai / Tokyo (see
// backend/app/website_monitor.py).
//
//   * Everyone can read this page.
//   * Only admins get the "Add website" form and the Remove buttons. The
//     backend decides (`can_manage` in the status response) and enforces it
//     again on the write routes, so hiding the controls is only UX.
//
// State per site x Region is computed server-side:
//   up | down | stale (probe has been silent too long) | nodata (never reported)

const REFRESH_MS = 60 * 1000

const STATE = {
  up: { label: 'UP', color: COLORS.success },
  down: { label: 'DOWN', color: COLORS.danger },
  stale: { label: 'STALE', color: COLORS.textSecondary },
  nodata: { label: 'NO DATA', color: COLORS.textMuted },
}

function formatAge(seconds) {
  if (seconds == null) return ''
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86400)} d ago`
}

// Everything else in this portal shows HKT (UTC+8).
function formatHkt(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString('en-HK', { timeZone: 'Asia/Hong_Kong', hour12: false }) + ' HKT'
}

function errorMessage(e, fallback) {
  const status = e?.response?.status
  if (status === 503) return 'Website Monitoring is not configured for this environment yet.'
  if (status === 403) return 'Only admins can change the monitored websites.'
  return e?.response?.data?.detail || fallback
}

function Row({ label, value }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, marginTop: 4, fontSize: 12 }}>
      <span style={{ color: COLORS.textSecondary }}>{label}</span>
      <span style={{ color: COLORS.textPrimary, fontWeight: 600 }}>{value}</span>
    </div>
  )
}

function ResultCell({ site, region, result, onOpenShot }) {
  const state = result?.state || 'nodata'
  const meta = STATE[state]
  return (
    <div style={{
      border: `1px solid ${COLORS.border}`, borderLeft: `4px solid ${meta.color}`,
      borderRadius: 8, padding: 10, background: COLORS.cardBg, minWidth: 180,
    }}>
      <Badge text={meta.label} color={meta.color} />
      {result?.slow && <span style={{ marginLeft: 6 }}><Badge text="SLOW" color={COLORS.warning} /></span>}
      {state === 'nodata' ? (
        <div style={{ color: COLORS.textMuted, fontSize: 12, marginTop: 6 }}>No result reported yet</div>
      ) : (
        <>
          <div style={{ color: COLORS.textMuted, fontSize: 11, marginTop: 4 }} title={formatHkt(result.checked_at)}>
            Checked {formatAge(result.age_seconds)}
          </div>
          <Row label="Status code" value={result.status_code ?? 'n/a'} />
          <Row label="Response" value={result.response_time_ms != null ? `${result.response_time_ms} ms${result.slow ? ' (slow)' : ''}` : 'n/a'} />
          {state === 'down' && (
            <>
              {result.error && (
                <div style={{ color: COLORS.danger, fontSize: 11.5, marginTop: 6, wordBreak: 'break-word' }}>{result.error}</div>
              )}
              <Row label="Failed runs" value={result.consecutive_failures || 1} />
            </>
          )}
          {result.screenshot_url && (
            <button
              type="button"
              onClick={() => onOpenShot({
                url: result.screenshot_url, site: site.name, region: region.label, at: result.screenshot_at,
              })}
              aria-label={`View latest successful screenshot of ${site.name} from ${region.label}`}
              style={{ display: 'block', width: '100%', padding: 0, marginTop: 8, border: 'none', background: 'none', cursor: 'zoom-in' }}
            >
              <img
                src={result.screenshot_url}
                alt={`Latest successful screenshot of ${site.name} from ${region.label}`}
                loading="lazy"
                style={{
                  width: '100%', aspectRatio: '16 / 9', objectFit: 'cover', objectPosition: 'top',
                  border: `1px solid ${COLORS.border}`, borderRadius: 4, display: 'block', background: COLORS.pageBg,
                }}
              />
              <div style={{ color: COLORS.textMuted, fontSize: 10.5, marginTop: 2, textAlign: 'left' }}>
                Last successful access {formatAge(Math.max(0, Math.round((Date.now() - new Date(result.screenshot_at).getTime()) / 1000)))}
              </div>
            </button>
          )}
        </>
      )}
    </div>
  )
}

function ScreenshotModal({ shot, onClose }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  if (!shot) return null
  return (
    <div
      role="dialog" aria-modal="true" aria-label="Screenshot"
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.75)', zIndex: 1000,
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24,
      }}
    >
      <div onClick={e => e.stopPropagation()} style={{ ...CARD, padding: 12, maxWidth: '92vw', maxHeight: '92vh', overflow: 'auto' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 8 }}>
          <div style={{ color: COLORS.textPrimary, fontSize: 13, fontWeight: 600 }}>
            {shot.site} · {shot.region}
            <span style={{ color: COLORS.textSecondary, fontWeight: 400 }}> · {formatHkt(shot.at)}</span>
          </div>
          <button type="button" onClick={onClose} autoFocus aria-label="Close screenshot"
            style={{ border: `1px solid ${COLORS.border}`, background: '#fff', borderRadius: 6, padding: '4px 10px', cursor: 'pointer' }}>
            Close
          </button>
        </div>
        <img src={shot.url} alt={`Screenshot of ${shot.site} from ${shot.region}`} style={{ maxWidth: '100%', height: 'auto', display: 'block' }} />
      </div>
    </div>
  )
}

function AddSiteForm({ maxSites, count, onAdded }) {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      await api.addWebsiteMonitoringSite(name.trim(), url.trim())
      setName('')
      setUrl('')
      onAdded()
    } catch (ex) {
      setErr(errorMessage(ex, 'Could not add the website'))
    } finally {
      setBusy(false)
    }
  }

  const input = {
    padding: '7px 10px', border: `1px solid ${COLORS.border}`, borderRadius: 6, fontSize: 13,
    color: COLORS.textPrimary, background: '#fff',
  }
  const full = count >= maxSites
  return (
    <form onSubmit={submit} style={{ ...CARD, padding: 14, marginBottom: 16 }}>
      <div style={{ color: COLORS.textPrimary, fontSize: 13.5, fontWeight: 600, marginBottom: 8 }}>Add website (admin)</div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 12, color: COLORS.textSecondary }}>
          Name
          <input value={name} onChange={e => setName(e.target.value)} maxLength={80} required placeholder="Example Site A" style={{ ...input, width: 220 }} />
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 12, color: COLORS.textSecondary, flex: '1 1 280px' }}>
          URL
          <input value={url} onChange={e => setUrl(e.target.value)} maxLength={500} required type="text" inputMode="url" placeholder="https://www.example.com/" style={input} />
        </label>
        <button type="submit" disabled={busy || full}
          style={{
            padding: '8px 18px', borderRadius: 6, border: 'none', background: COLORS.brand, color: '#fff',
            fontWeight: 600, fontSize: 13, cursor: busy || full ? 'not-allowed' : 'pointer', opacity: busy || full ? 0.6 : 1,
          }}>
          {busy ? 'Adding…' : 'Add website'}
        </button>
      </div>
      {full && <div style={{ color: COLORS.warning, fontSize: 12, marginTop: 8 }}>The limit of {maxSites} websites has been reached.</div>}
      {err && <div role="alert" style={{ color: COLORS.danger, fontSize: 12.5, marginTop: 8 }}>{err}</div>}
      <div style={{ color: COLORS.textMuted, fontSize: 11.5, marginTop: 8 }}>
        Only public http(s) websites. The probes pick up the change on their next run, so the first result appears within one check interval.
      </div>
    </form>
  )
}

export default function WebsiteMonitoringPage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [shot, setShot] = useState(null)
  const [removing, setRemoving] = useState('')
  const [actionErr, setActionErr] = useState('')
  const mountedRef = useRef(true)
  const seqRef = useRef(0)

  const load = useCallback((refresh = false) => {
    const mySeq = ++seqRef.current
    const controller = new AbortController()
    api.getWebsiteMonitoringStatus(refresh, controller.signal)
      .then(d => {
        if (!mountedRef.current || mySeq !== seqRef.current) return
        setData(d)
        setErr('')
      })
      .catch(e => {
        if (!mountedRef.current || mySeq !== seqRef.current || e?.code === 'ERR_CANCELED') return
        setErr(errorMessage(e, 'Could not load website status'))
      })
      .finally(() => { if (mountedRef.current && mySeq === seqRef.current) setLoading(false) })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    mountedRef.current = true
    const abort = load()
    const timer = setInterval(() => load(), REFRESH_MS)
    return () => {
      mountedRef.current = false
      clearInterval(timer)
      abort()
    }
  }, [load])

  const remove = async (site) => {
    if (!window.confirm(`Stop monitoring "${site.name}" (${site.url})?\n\nIts stored results and screenshots will be deleted.`)) return
    setActionErr('')
    setRemoving(site.id)
    try {
      await api.removeWebsiteMonitoringSite(site.id)
      load(true)
    } catch (ex) {
      setActionErr(errorMessage(ex, 'Could not remove the website'))
    } finally {
      setRemoving('')
    }
  }

  const counts = { up: 0, down: 0, stale: 0, nodata: 0, slow: 0 }
  if (data) {
    data.sites.forEach(s => data.regions.forEach(r => { counts[s.results[r.key]?.state || 'nodata']++; if (s.results[r.key]?.slow) counts.slow++ }))
  }

  return (
    <div style={{ padding: '16px 20px 24px' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
        <div>
          <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>🌐 Website Monitoring</h2>
          <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
            Availability, status code, response time and latest successful screenshot of each website, probed from
            {' '}{data ? data.regions.map(r => r.label).join(', ') : 'several AWS Regions'}.
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          {data && (
            <>
              <Badge text={`${counts.up} up`} color={COLORS.success} />
              {counts.slow > 0 && <Badge text={`${counts.slow} slow`} color={COLORS.warning} />}
              <Badge text={`${counts.down} down`} color={COLORS.danger} />
              <Badge text={`${counts.stale + counts.nodata} stale / no data`} color={COLORS.textSecondary} />
            </>
          )}
          <button type="button" onClick={() => { setLoading(true); load(true) }}
            style={{ padding: '6px 14px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', cursor: 'pointer', fontSize: 12.5 }}>
            ↻ Refresh
          </button>
        </div>
      </div>

      <InfoBanner
        text={data?.can_manage
          ? 'You are an admin: you can add and remove monitored websites. Everyone else has read-only access.'
          : 'Read-only view. Only admins can add or remove monitored websites.'}
        right={data ? `Probes run every ${data.interval_minutes} min · page refreshes every 60 s` : null}
      />

      {err && <div role="alert" style={{ ...CARD, padding: 14, color: COLORS.danger, marginBottom: 16 }}>{err}</div>}
      {actionErr && <div role="alert" style={{ ...CARD, padding: 14, color: COLORS.danger, marginBottom: 16 }}>{actionErr}</div>}

      {loading && !data && <div style={{ ...CARD, padding: 20 }}><SkeletonLines lines={5} /></div>}

      {data?.can_manage && (
        <AddSiteForm maxSites={data.max_sites} count={data.sites.length} onAdded={() => load(true)} />
      )}

      {data && data.sites.length === 0 && (
        <div style={{ ...CARD, padding: 24, color: COLORS.textSecondary, textAlign: 'center' }}>
          No websites are being monitored yet.{data.can_manage ? ' Use "Add website" above.' : ' An admin can add some.'}
        </div>
      )}

      {data && data.sites.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ borderCollapse: 'separate', borderSpacing: 8, minWidth: '100%' }}>
            <thead>
              <tr>
                <th scope="col" style={{ textAlign: 'left', color: COLORS.textPrimary, fontSize: 13, padding: '4px 6px' }}>Website</th>
                {data.regions.map(r => (
                  <th key={r.key} scope="col" style={{ textAlign: 'left', color: COLORS.textPrimary, fontSize: 13, padding: '4px 6px', whiteSpace: 'nowrap' }}>{r.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.sites.map(site => (
                <tr key={site.id}>
                  <th scope="row" style={{ textAlign: 'left', verticalAlign: 'top', padding: '4px 6px', minWidth: 170 }}>
                    <div style={{ color: COLORS.textPrimary, fontSize: 13.5 }}>{site.name}</div>
                    <a href={site.url} target="_blank" rel="noopener noreferrer"
                      style={{ color: COLORS.brand, fontSize: 11.5, fontWeight: 400, wordBreak: 'break-all' }}>{site.url}</a>
                    {data.can_manage && (
                      <div style={{ marginTop: 6 }}>
                        <button type="button" onClick={() => remove(site)} disabled={removing === site.id}
                          aria-label={`Remove ${site.name} from monitoring`}
                          style={{
                            padding: '3px 10px', fontSize: 11.5, borderRadius: 5, cursor: removing === site.id ? 'wait' : 'pointer',
                            border: `1px solid ${COLORS.danger}60`, background: COLORS.dangerBg, color: COLORS.danger, fontWeight: 600,
                          }}>
                          {removing === site.id ? 'Removing…' : 'Remove'}
                        </button>
                      </div>
                    )}
                  </th>
                  {data.regions.map(r => (
                    <td key={r.key} style={{ verticalAlign: 'top', padding: 0 }}>
                      <ResultCell site={site} region={r} result={site.results[r.key]} onOpenShot={setShot} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ color: COLORS.textMuted, fontSize: 11.5, marginTop: 12 }}>
        Response time is measured to DOMContentLoaded. A site counts as UP when the final HTTP status is 200–399.
        STALE means the probe in that Region has not reported for more than 2.5 check intervals.
      </div>

      <ScreenshotModal shot={shot} onClose={() => setShot(null)} />
    </div>
  )
}
