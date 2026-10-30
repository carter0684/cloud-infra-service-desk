import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { api } from '../api.js'
import { COLORS, CARD, InfoBanner, Badge } from '../utils/theme.js'

// ── DNS Record (Security) ─────────────────────────────────────────────────
// Browse every Route 53 hosted zone the organization owns (all in the Core account,
// 100000000001) and its records — type, value, TTL — and add, modify or
// delete them. See app/dns_records.py for the backend design.
//
// THIS IS THE ONLY PAGE IN THE PORTAL THAT CAN CHANGE PRODUCTION DNS, so
// three things here differ from every other AWS-facing tab:
//
//  * Write controls are hidden unless the backend says this user may
//    write (getDnsPermissions -> can_write: admins + Cloud Infra/Security/
//    Management Team). That's a UI convenience only — every write route
//    re-checks server-side, so a user who forces the buttons visible
//    still gets a 403.
//  * Every write demands a Service Request number, and the form will
//    not submit without one.
//  * NS and SOA records come back with editable:false and are rendered
//    without Edit/Delete buttons, because a mistake in either takes the
//    whole domain offline rather than one hostname.
//
// The audit log panel at the bottom follows the same convention as
// WhitelistLmsPage: a 403 from getDnsAuditLog() leaves auditLog === null
// and the panel simply isn't rendered, which is the correct outcome for
// users outside the three privileged groups, not an error to surface.

const INPUT = { width: '100%', padding: '9px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 14, boxSizing: 'border-box' }
const LABEL = { fontSize: 12, fontWeight: 600, color: COLORS.textSecondary, display: 'block', marginBottom: 5 }
const MONO = { fontFamily: 'monospace' }
const TH = { padding: '7px 8px', color: COLORS.textSecondary, fontWeight: 600, whiteSpace: 'nowrap' }
const TD = { padding: '7px 8px', verticalAlign: 'top' }

// Offerable in the "add" form. Mirrors dns_records.SUPPORTED_RECORD_TYPES
// — the backend rejects anything outside that list, so keep the two in
// step. NS/SOA are deliberately absent: they're refused server-side.
const RECORD_TYPES = ['A', 'AAAA', 'CNAME', 'MX', 'TXT', 'SRV', 'CAA', 'PTR', 'SPF', 'NAPTR', 'DS']

// Type badge colors — chosen so the two types people most often confuse
// at a glance (A vs CNAME) don't share one, and TXT/MX (the two whose
// breakage means "mail is down") stand out.
const TYPE_COLOR = {
  A: '#2563eb', AAAA: '#1d4ed8', CNAME: '#7c3aed', MX: '#d97706',
  TXT: '#0891b2', NS: '#6b7280', SOA: '#6b7280', SRV: '#16a34a', CAA: '#db2777',
}
const typeColor = t => TYPE_COLOR[t] || '#64748b'

function btn(bg, disabled) {
  return {
    padding: '8px 16px', borderRadius: 7, border: 'none', background: bg, color: '#fff',
    fontSize: 13, fontWeight: 600, whiteSpace: 'nowrap',
    cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.6 : 1,
  }
}

function smallBtn(color) {
  return {
    padding: '3px 9px', borderRadius: 5, border: `1px solid ${color}50`,
    background: color + '12', color, fontSize: 11.5, fontWeight: 600, cursor: 'pointer',
  }
}

function Banner({ tone, children }) {
  if (!children) return null
  const c = tone === 'error' ? COLORS.danger : COLORS.success
  const bg = tone === 'error' ? COLORS.dangerBg : COLORS.successBg
  return (
    <div style={{ ...CARD, padding: 13, marginBottom: 16, background: bg, border: `1px solid ${c}40`, color: c, fontSize: 13, fontWeight: 600 }}>
      {children}
    </div>
  )
}

/** The value column. ALIAS records keep their target in a different
 * structure from ordinary records (no TTL, an AliasTarget instead of
 * ResourceRecords), and a multi-value record has several lines, so both
 * cases are rendered explicitly rather than stringified. */
function RecordValue({ record }) {
  if (record.is_alias) {
    const t = record.alias_target || {}
    return (
      <div style={{ ...MONO, fontSize: 11.5 }}>
        <span style={{ color: COLORS.textMuted }}>ALIAS → </span>{t.dns_name}
        <div style={{ color: COLORS.textMuted, fontSize: 10.5 }}>target zone {t.hosted_zone_id}</div>
      </div>
    )
  }
  if (!record.values?.length) return <span style={{ color: COLORS.textMuted }}>—</span>
  return (
    <div style={{ ...MONO, fontSize: 11.5, display: 'flex', flexDirection: 'column', gap: 2 }}>
      {record.values.map((v, i) => <div key={i} style={{ wordBreak: 'break-all' }}>{v}</div>)}
    </div>
  )
}

/** Weighted / latency / failover / geolocation siblings share a name and
 * type, so the routing policy is what distinguishes them — shown as a
 * separate column, since without it two rows look like duplicates. */
function RoutingCell({ record }) {
  const bits = []
  if (record.set_identifier) bits.push(`id: ${record.set_identifier}`)
  if (record.weight !== null && record.weight !== undefined) bits.push(`weight ${record.weight}`)
  if (record.aws_region) bits.push(`region ${record.aws_region}`)
  if (record.failover) bits.push(`failover ${record.failover}`)
  if (record.multi_value_answer) bits.push('multivalue')
  if (record.geo_location) bits.push('geo')
  if (!bits.length) return <span style={{ color: COLORS.textMuted }}>simple</span>
  return <span style={{ fontSize: 11, color: COLORS.textSecondary }}>{bits.join(' · ')}</span>
}

// Bulk import action -> badge color. Kept close to the record-type
// palette in spirit: green for something new, orange for something
// changing, grey for a no-op, red for something that won't be applied.
const BULK_ACTION_COLOR = {
  CREATE: '#16a34a', MODIFY: '#d97706', UNCHANGED: '#6b7280', ERROR: '#dc2626',
}

const EMPTY_FORM = {
  mode: 'create',       // 'create' | 'modify'
  name: '',
  record_type: 'A',
  kind: 'standard',     // 'standard' | 'alias'
  ttl: '300',
  valuesText: '',
  alias_dns_name: '',
  alias_hosted_zone_id: '',
  alias_evaluate_target_health: false,
  set_identifier: '',
  sr_number: '',
}

export default function DnsRecordsPage() {
  const [zones, setZones] = useState([])
  const [zoneId, setZoneId] = useState('')
  const [zone, setZone] = useState(null)
  const [records, setRecords] = useState([])
  const [loadingZones, setLoadingZones] = useState(true)
  const [loadingRecords, setLoadingRecords] = useState(false)
  const [filter, setFilter] = useState('')

  const [canWrite, setCanWrite] = useState(false)
  const [form, setForm] = useState(null)          // null = form closed
  const [saving, setSaving] = useState(false)
  const [pendingDelete, setPendingDelete] = useState(null)  // { record, sr_number }
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(null)

  const [auditLog, setAuditLog] = useState(null)  // null = unauthorized / not loaded, [] = empty
  const [auditLoading, setAuditLoading] = useState(false)

  // ── Bulk import (create/modify only) ──
  // bulkFileName/bulkFileContent hold what FileReader read from the
  // picked file — sent to the backend as a plain JSON string, never as
  // multipart/form-data (see api.js's comment on previewDnsBulkImport).
  // bulkPreview is null until Preview succeeds; apply is only offered
  // once a preview exists, and picking a new file clears it so a stale
  // preview can never be applied against a file the user has since
  // changed.
  const [bulkOpen, setBulkOpen] = useState(false)
  const [bulkFileName, setBulkFileName] = useState('')
  const [bulkFileContent, setBulkFileContent] = useState('')
  // Only set for an .xlsx pick — base64-encoded workbook bytes, since
  // .xlsx is binary and can't travel in bulkFileContent as text. Exactly
  // one of bulkFileContent/bulkFileContentB64 is non-empty at a time —
  // see handleBulkFilePick().
  const [bulkFileContentB64, setBulkFileContentB64] = useState(null)
  const [bulkPreview, setBulkPreview] = useState(null)
  const [bulkPreviewLoading, setBulkPreviewLoading] = useState(false)
  const [bulkSrNumber, setBulkSrNumber] = useState('')
  const [bulkApplying, setBulkApplying] = useState(false)
  const [bulkApplyResult, setBulkApplyResult] = useState(null)
  const [bulkError, setBulkError] = useState(null)

  useEffect(() => {
    // can_write only decides whether the buttons render — the routes
    // enforce it again regardless, so a failure here degrades to
    // read-only rather than to a broken page.
    api.getDnsPermissions().then(r => setCanWrite(!!r.can_write)).catch(() => setCanWrite(false))
  }, [])

  useEffect(() => {
    setLoadingZones(true)
    api.getDnsZones()
      .then(res => {
        const list = res.zones || []
        setZones(list)
        if (list.length) setZoneId(list[0].id)
      })
      .catch(e => setError(e?.response?.data?.detail || 'Could not load the hosted zones.'))
      .finally(() => setLoadingZones(false))
  }, [])

  const loadRecords = useCallback((id) => {
    if (!id) return
    setLoadingRecords(true)
    api.getDnsZoneRecords(id)
      .then(res => { setZone(res.zone); setRecords(res.records || []) })
      .catch(e => {
        setZone(null); setRecords([])
        setError(e?.response?.data?.detail || 'Could not load the records for this zone.')
      })
      .finally(() => setLoadingRecords(false))
  }, [])

  useEffect(() => { loadRecords(zoneId) }, [zoneId, loadRecords])

  const loadAuditLog = useCallback(() => {
    setAuditLoading(true)
    api.getDnsAuditLog(200)
      .then(res => setAuditLog(res.records || []))
      .catch(() => setAuditLog(null))  // 403 — leave the panel hidden
      .finally(() => setAuditLoading(false))
  }, [])

  useEffect(() => { loadAuditLog() }, [loadAuditLog])

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return records
    return records.filter(r =>
      r.display_name.toLowerCase().includes(q)
      || r.type.toLowerCase().includes(q)
      || (r.values || []).some(v => v.toLowerCase().includes(q))
      || (r.alias_target?.dns_name || '').toLowerCase().includes(q)
    )
  }, [records, filter])

  function clearBanners() { setError(null); setSuccess(null) }

  function openCreate() {
    clearBanners()
    setPendingDelete(null)
    // Prefill the name with a trailing dot-suffix hint by leaving it
    // blank: the backend validates the name is inside the zone and its
    // error names both domains, which is clearer than silently appending.
    setForm({ ...EMPTY_FORM, mode: 'create' })
  }

  function openModify(record) {
    clearBanners()
    setPendingDelete(null)
    setForm({
      mode: 'modify',
      name: record.display_name.replace(/\.$/, ''),
      record_type: record.type,
      kind: record.is_alias ? 'alias' : 'standard',
      ttl: record.ttl !== null && record.ttl !== undefined ? String(record.ttl) : '',
      valuesText: (record.values || []).join('\n'),
      alias_dns_name: record.alias_target?.dns_name || '',
      alias_hosted_zone_id: record.alias_target?.hosted_zone_id || '',
      alias_evaluate_target_health: !!record.alias_target?.evaluate_target_health,
      set_identifier: record.set_identifier || '',
      sr_number: '',
    })
  }

  function buildPayload(f) {
    const payload = {
      zone_id: zoneId,
      name: f.name.trim(),
      record_type: f.record_type,
      sr_number: f.sr_number.trim(),
    }
    if (f.set_identifier.trim()) payload.set_identifier = f.set_identifier.trim()
    if (f.kind === 'alias') {
      payload.alias_target = {
        dns_name: f.alias_dns_name.trim(),
        hosted_zone_id: f.alias_hosted_zone_id.trim(),
        evaluate_target_health: !!f.alias_evaluate_target_health,
      }
    } else {
      payload.ttl = Number(f.ttl)
      // One value per line — the natural shape for a multi-value A record
      // or a split TXT record, and avoids guessing where to split on a
      // comma inside a quoted TXT string.
      payload.values = f.valuesText.split('\n').map(s => s.trim()).filter(Boolean)
    }
    return payload
  }

  async function handleSave(e) {
    e.preventDefault()
    if (!form || !form.sr_number.trim()) return
    clearBanners()
    setSaving(true)
    try {
      const payload = buildPayload(form)
      const res = form.mode === 'create'
        ? await api.createDnsRecord(payload)
        : await api.modifyDnsRecord(payload)
      setSuccess(
        `✅ ${form.mode === 'create' ? 'Added' : 'Updated'} ${res.record.type} `
        + `${res.record.display_name.replace(/\.$/, '')} — Route 53 change ${res.change.status || 'submitted'}.`
      )
      setForm(null)
      loadRecords(zoneId)
      loadAuditLog()
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || 'Could not save this record.')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(e) {
    e.preventDefault()
    if (!pendingDelete?.sr_number.trim()) return
    clearBanners()
    setDeleting(true)
    try {
      const r = pendingDelete.record
      const payload = {
        zone_id: zoneId,
        name: r.display_name,
        record_type: r.type,
        sr_number: pendingDelete.sr_number.trim(),
      }
      if (r.set_identifier) payload.set_identifier = r.set_identifier
      const res = await api.deleteDnsRecord(payload)
      setSuccess(
        `✅ Deleted ${res.record.type} ${res.record.display_name.replace(/\.$/, '')} — `
        + `Route 53 change ${res.change.status || 'submitted'}.`
      )
      setPendingDelete(null)
      loadRecords(zoneId)
      loadAuditLog()
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || 'Could not delete this record.')
    } finally {
      setDeleting(false)
    }
  }

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  function openBulk() {
    clearBanners()
    setForm(null)
    setPendingDelete(null)
    setBulkFileName('')
    setBulkFileContent('')
    setBulkFileContentB64(null)
    setBulkPreview(null)
    setBulkApplyResult(null)
    setBulkSrNumber('')
    setBulkError(null)
    setBulkOpen(true)
  }

  function closeBulk() {
    setBulkOpen(false)
    setBulkFileName('')
    setBulkFileContent('')
    setBulkFileContentB64(null)
    setBulkPreview(null)
    setBulkApplyResult(null)
    setBulkSrNumber('')
    setBulkError(null)
  }

  function handleBulkFilePick(e) {
    const file = e.target.files?.[0]
    if (!file) return
    // Picking a new file always invalidates any earlier preview/apply
    // result — a stale preview must never be applied against content
    // the user has since replaced.
    setBulkPreview(null)
    setBulkApplyResult(null)
    setBulkError(null)
    setBulkFileName(file.name)
    setBulkFileContent('')
    setBulkFileContentB64(null)

    // .xlsx is a binary ZIP-based format — readAsText() would just
    // produce garbage, so it needs to travel as base64 bytes instead
    // (see api.js's previewDnsBulkImport/applyDnsBulkImport comment).
    // Everything else (.csv/.tsv/.txt, and a BIND zone-file which has no
    // fixed extension convention) is read as plain text as before.
    const isXlsx = /\.xlsx$/i.test(file.name)
    const reader = new FileReader()
    reader.onerror = () => setBulkError('Could not read this file.')
    if (isXlsx) {
      reader.onload = () => {
        const buf = reader.result
        let binary = ''
        const bytes = new Uint8Array(buf)
        for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
        setBulkFileContentB64(btoa(binary))
      }
      reader.readAsArrayBuffer(file)
    } else {
      reader.onload = () => setBulkFileContent(String(reader.result || ''))
      reader.readAsText(file)
    }
  }

  async function handleBulkPreview() {
    if (!zoneId || !(bulkFileContent || bulkFileContentB64)) return
    setBulkError(null)
    setBulkApplyResult(null)
    setBulkPreviewLoading(true)
    try {
      const res = await api.previewDnsBulkImport(zoneId, bulkFileContent, bulkFileContentB64)
      setBulkPreview(res)
    } catch (err) {
      setBulkPreview(null)
      setBulkError(err?.response?.data?.detail || err?.message || 'Could not preview this file.')
    } finally {
      setBulkPreviewLoading(false)
    }
  }

  async function handleBulkApply() {
    if (!zoneId || !(bulkFileContent || bulkFileContentB64) || !bulkSrNumber.trim()) return
    setBulkError(null)
    setBulkApplying(true)
    try {
      const res = await api.applyDnsBulkImport(zoneId, bulkFileContent, bulkSrNumber.trim(), bulkFileContentB64)
      setBulkApplyResult(res)
      setBulkPreview(null)
      loadRecords(zoneId)
      loadAuditLog()
    } catch (err) {
      setBulkError(err?.response?.data?.detail || err?.message || 'Could not apply this bulk import.')
    } finally {
      setBulkApplying(false)
    }
  }

  return (
    <div style={{ padding: 24, maxWidth: 1240 }}>
      <h2 style={{ margin: '0 0 4px', fontSize: 19, color: COLORS.textPrimary }}>🌐 DNS Record</h2>
      <div style={{ fontSize: 13, color: COLORS.textSecondary, marginBottom: 16 }}>
        Every DNS record for the organization's domains, hosted in Amazon Route 53 (account 100000000001 — CORE).
        Pick a domain to see its records, their type, value and TTL.
      </div>

      <InfoBanner
        text={canWrite
          ? 'You can add, modify and delete records here. Every change needs a Service Request (SR) number and is written to the audit log below. NS and SOA records are read-only in the portal — a mistake in either takes the whole domain offline.'
          : 'Read-only view. Adding, modifying or deleting DNS records is limited to admins and the Cloud Infra, Security and Management Teams.'}
        right={zones.length ? `${zones.length} hosted zones` : null}
      />

      <Banner tone="error">{error}</Banner>
      <Banner tone="success">{success}</Banner>

      <div style={{ ...CARD, padding: 16, marginBottom: 18, display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <div style={{ flex: '1 1 320px', minWidth: 260 }}>
          <label style={LABEL}>Domain (hosted zone)</label>
          <select
            value={zoneId}
            onChange={e => { setZoneId(e.target.value); setFilter(''); setForm(null); setPendingDelete(null); clearBanners() }}
            disabled={loadingZones || !zones.length}
            style={{ ...INPUT, background: '#fff' }}
          >
            {loadingZones && <option>Loading…</option>}
            {zones.map(z => (
              <option key={z.id} value={z.id}>
                {z.display_name} ({z.record_count ?? '?'} records){z.private ? ' — private' : ''}
              </option>
            ))}
          </select>
          {zone && (
            <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 4, ...MONO }}>
              zone {zone.id}{zone.comment ? ` · ${zone.comment}` : ''}
            </div>
          )}
        </div>
        <div style={{ flex: '1 1 240px', minWidth: 200 }}>
          <label style={LABEL}>Filter records</label>
          <input
            value={filter}
            onChange={e => setFilter(e.target.value)}
            placeholder="name, type or value"
            style={INPUT}
          />
        </div>
        {canWrite && (
          <button type="button" onClick={openCreate} disabled={!zoneId} style={btn(COLORS.success, !zoneId)}>
            ➕ Add Record
          </button>
        )}
        {canWrite && (
          <button type="button" onClick={openBulk} disabled={!zoneId} style={btn(COLORS.brand, !zoneId)}>
            📄 Bulk Import
          </button>
        )}
      </div>

      {/* ── Add / modify form ── */}
      {form && (
        <form onSubmit={handleSave} style={{ ...CARD, padding: 18, marginBottom: 18, border: `1px solid ${COLORS.brand}40` }}>
          <h3 style={{ margin: '0 0 14px', fontSize: 15, color: COLORS.textPrimary }}>
            {form.mode === 'create' ? '➕ Add a record' : '✏️ Modify record'}
            {zone && <span style={{ fontWeight: 400, color: COLORS.textMuted, fontSize: 13 }}> in {zone.display_name}</span>}
          </h3>

          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', marginBottom: 14 }}>
            <div style={{ flex: '2 1 320px', minWidth: 240 }}>
              <label style={LABEL}>Record name</label>
              <input
                value={form.name}
                onChange={e => set('name', e.target.value)}
                placeholder={zone ? `e.g. www.${zone.display_name}` : 'e.g. www.example.com'}
                style={{ ...INPUT, ...MONO }}
                disabled={form.mode === 'modify'}
              />
              <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 4 }}>
                {form.mode === 'modify'
                  ? 'Name and type identify the record and cannot be changed — delete and re-add to rename.'
                  : `Must be inside ${zone ? zone.display_name : 'the selected zone'}. Use * for a wildcard.`}
              </div>
            </div>
            <div style={{ flex: '0 1 140px', minWidth: 120 }}>
              <label style={LABEL}>Type</label>
              <select
                value={form.record_type}
                onChange={e => set('record_type', e.target.value)}
                style={{ ...INPUT, background: '#fff' }}
                disabled={form.mode === 'modify'}
              >
                {RECORD_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div style={{ flex: '0 1 180px', minWidth: 160 }}>
              <label style={LABEL}>Record kind</label>
              <select
                value={form.kind}
                onChange={e => set('kind', e.target.value)}
                style={{ ...INPUT, background: '#fff' }}
              >
                <option value="standard">Standard (TTL + value)</option>
                <option value="alias">ALIAS (AWS endpoint)</option>
              </select>
            </div>
          </div>

          {form.kind === 'standard' ? (
            <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', marginBottom: 14 }}>
              <div style={{ flex: '0 1 140px', minWidth: 120 }}>
                <label style={LABEL}>TTL (seconds)</label>
                <input value={form.ttl} onChange={e => set('ttl', e.target.value)} placeholder="300" style={{ ...INPUT, ...MONO }} />
              </div>
              <div style={{ flex: '1 1 420px', minWidth: 280 }}>
                <label style={LABEL}>Value — one per line</label>
                <textarea
                  value={form.valuesText}
                  onChange={e => set('valuesText', e.target.value)}
                  rows={4}
                  placeholder={'203.0.113.10\n203.0.113.11'}
                  style={{ ...INPUT, ...MONO, resize: 'vertical' }}
                />
                <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 4 }}>
                  TXT and SPF values are quoted for you if you don't quote them yourself.
                  MX needs a priority, e.g. <code>10 mail.example.com</code>.
                </div>
              </div>
            </div>
          ) : (
            <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', marginBottom: 14 }}>
              <div style={{ flex: '2 1 320px', minWidth: 240 }}>
                <label style={LABEL}>Alias target DNS name</label>
                <input
                  value={form.alias_dns_name}
                  onChange={e => set('alias_dns_name', e.target.value)}
                  placeholder="e.g. d123abc.cloudfront.net"
                  style={{ ...INPUT, ...MONO }}
                />
              </div>
              <div style={{ flex: '1 1 220px', minWidth: 180 }}>
                <label style={LABEL}>Alias target hosted zone ID</label>
                <input
                  value={form.alias_hosted_zone_id}
                  onChange={e => set('alias_hosted_zone_id', e.target.value)}
                  placeholder="e.g. Z2FDTNDATAQYW2"
                  style={{ ...INPUT, ...MONO }}
                />
                <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 4 }}>
                  Identifies the AWS endpoint — CloudFront, an ELB, an S3 website, or this same zone.
                </div>
              </div>
              <div style={{ flex: '0 1 200px', minWidth: 180, paddingBottom: 8 }}>
                <label style={{ ...LABEL, marginBottom: 8 }}>Health check</label>
                <label style={{ fontSize: 12.5, color: COLORS.textPrimary, display: 'flex', gap: 7, alignItems: 'center' }}>
                  <input
                    type="checkbox"
                    checked={form.alias_evaluate_target_health}
                    onChange={e => set('alias_evaluate_target_health', e.target.checked)}
                  />
                  Evaluate target health
                </label>
              </div>
            </div>
          )}

          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'flex-end', borderTop: `1px solid ${COLORS.border}`, paddingTop: 14 }}>
            <div style={{ flex: '1 1 260px', minWidth: 220 }}>
              <label style={LABEL}>SR Number (required)</label>
              <input
                value={form.sr_number}
                onChange={e => set('sr_number', e.target.value)}
                placeholder="e.g. SR-000123456"
                style={INPUT}
              />
              <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 4 }}>
                Recorded in the audit log below and stamped into the Route 53 change history.
              </div>
            </div>
            <button type="submit" disabled={saving || !form.sr_number.trim()} style={btn(COLORS.brand, saving || !form.sr_number.trim())}>
              {saving ? 'Saving…' : (form.mode === 'create' ? 'Create record' : 'Save changes')}
            </button>
            <button type="button" onClick={() => { setForm(null); clearBanners() }} style={{ ...btn('#94a3b8', false) }}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* ── Delete confirmation ── */}
      {pendingDelete && (
        <form onSubmit={handleDelete} style={{ ...CARD, padding: 18, marginBottom: 18, border: `1px solid ${COLORS.danger}60`, background: COLORS.dangerBg }}>
          <h3 style={{ margin: '0 0 8px', fontSize: 15, color: COLORS.danger }}>🗑️ Delete this record?</h3>
          <div style={{ fontSize: 13, color: COLORS.textPrimary, marginBottom: 12 }}>
            <b style={MONO}>{pendingDelete.record.display_name.replace(/\.$/, '')}</b>{' '}
            <Badge text={pendingDelete.record.type} color={typeColor(pendingDelete.record.type)} />{' '}
            in <b>{zone?.display_name}</b>. This takes effect in Route 53 immediately and cannot be undone
            from the portal — the record's current value is kept in the audit log so it can be re-created by hand.
          </div>
          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <div style={{ flex: '1 1 260px', minWidth: 220 }}>
              <label style={LABEL}>SR Number (required)</label>
              <input
                value={pendingDelete.sr_number}
                onChange={e => setPendingDelete(p => ({ ...p, sr_number: e.target.value }))}
                placeholder="e.g. SR-000123456"
                style={INPUT}
              />
            </div>
            <button type="submit" disabled={deleting || !pendingDelete.sr_number.trim()} style={btn(COLORS.danger, deleting || !pendingDelete.sr_number.trim())}>
              {deleting ? 'Deleting…' : 'Delete record'}
            </button>
            <button type="button" onClick={() => { setPendingDelete(null); clearBanners() }} style={btn('#94a3b8', false)}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* ── Bulk import ── */}
      {bulkOpen && (
        <div style={{ ...CARD, padding: 18, marginBottom: 18, border: `1px solid ${COLORS.brand}40` }}>
          <h3 style={{ margin: '0 0 6px', fontSize: 15, color: COLORS.textPrimary }}>
            📄 Bulk import
            {zone && <span style={{ fontWeight: 400, color: COLORS.textMuted, fontSize: 13 }}> into {zone.display_name}</span>}
          </h3>
          <div style={{ fontSize: 12, color: COLORS.textMuted, marginBottom: 14 }}>
            Upload a CSV/TSV/Excel (.xlsx) file (columns: name, type, ttl, value) or a BIND zone-file. Only
            <b> create</b> and <b>modify</b> are supported here — delete a record individually above.
            Nothing is written until you Preview, review every row, and then Apply.
          </div>

          <Banner tone="error">{bulkError}</Banner>

          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'flex-end', marginBottom: 14 }}>
            <div style={{ flex: '1 1 320px', minWidth: 260 }}>
              <label style={LABEL}>File (.csv, .tsv, .txt, .xlsx)</label>
              <input type="file" accept=".csv,.tsv,.txt,.xlsx" onChange={handleBulkFilePick} style={{ ...INPUT, padding: '6px 8px' }} />
              {bulkFileName && (
                <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 4 }}>
                  {bulkFileName}
                  {bulkFileContentB64
                    ? ' · Excel workbook (rows shown after Preview)'
                    : ` · ${bulkFileContent.split(/\r?\n/).filter(l => l.trim()).length} non-blank lines`}
                </div>
              )}
            </div>
            <button
              type="button"
              onClick={handleBulkPreview}
              disabled={!bulkFileContent || bulkPreviewLoading}
              style={btn(COLORS.brand, !bulkFileContent || bulkPreviewLoading)}
            >
              {bulkPreviewLoading ? 'Previewing…' : 'Preview'}
            </button>
            <button type="button" onClick={closeBulk} style={btn('#94a3b8', false)}>Cancel</button>
          </div>

          {bulkPreview && (
            <>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 10, fontSize: 12 }}>
                <Badge text={`${bulkPreview.counts.create} to create`} color={BULK_ACTION_COLOR.CREATE} />
                <Badge text={`${bulkPreview.counts.modify} to modify`} color={BULK_ACTION_COLOR.MODIFY} />
                <Badge text={`${bulkPreview.counts.unchanged} unchanged`} color={BULK_ACTION_COLOR.UNCHANGED} />
                <Badge text={`${bulkPreview.counts.error} errors`} color={BULK_ACTION_COLOR.ERROR} />
              </div>

              <div style={{ overflowX: 'auto', marginBottom: 14, maxHeight: 360, overflowY: 'auto', border: `1px solid ${COLORS.border}`, borderRadius: 8 }}>
                <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                  <thead>
                    <tr style={{ textAlign: 'left', borderBottom: `1px solid ${COLORS.border}`, position: 'sticky', top: 0, background: '#fff' }}>
                      <th style={TH}>Line</th>
                      <th style={TH}>Action</th>
                      <th style={TH}>Name</th>
                      <th style={TH}>Type</th>
                      <th style={TH}>TTL</th>
                      <th style={TH}>New value</th>
                      <th style={TH}>Previous value</th>
                    </tr>
                  </thead>
                  <tbody>
                    {bulkPreview.rows.map((r, i) => {
                      const isModify = r.action === 'MODIFY'
                      const ttlChanged = isModify && r.previous_ttl != null && String(r.previous_ttl) !== String(r.ttl)
                      const valuesChanged = isModify && JSON.stringify([...(r.previous_values || [])].sort()) !== JSON.stringify([...(r.values || [])].sort())
                      return (
                        <tr key={i} style={{ borderBottom: `1px solid ${COLORS.border}`, background: r.action === 'ERROR' ? COLORS.dangerBg : (isModify ? '#fff7ed' : undefined) }}>
                          <td style={{ ...TD, color: COLORS.textMuted }}>{r.line_no ?? '—'}</td>
                          <td style={TD}><Badge text={r.action} color={BULK_ACTION_COLOR[r.action] || '#64748b'} /></td>
                          <td style={{ ...TD, ...MONO, wordBreak: 'break-all' }}>{(r.name || '').replace(/\.$/, '')}</td>
                          <td style={TD}>{r.type ? <Badge text={r.type} color={typeColor(r.type)} /> : '—'}</td>
                          <td style={{ ...TD, ...MONO, color: ttlChanged ? COLORS.danger : COLORS.textPrimary, fontWeight: ttlChanged ? 700 : 400 }}>
                            {r.ttl ?? '—'}
                          </td>
                          <td style={{ ...TD, ...MONO, fontSize: 11, wordBreak: 'break-all', color: valuesChanged ? COLORS.success : COLORS.textPrimary, fontWeight: valuesChanged ? 700 : 400 }}>
                            {r.action === 'ERROR' ? <span style={{ color: COLORS.danger, fontWeight: 600 }}>{r.error}</span> : (r.values || []).join(', ') || '—'}
                          </td>
                          <td style={{ ...TD, ...MONO, fontSize: 11, wordBreak: 'break-all', color: valuesChanged ? COLORS.danger : COLORS.textMuted, textDecoration: valuesChanged ? 'line-through' : 'none' }}>
                            {(r.previous_values || []).join(', ') || '—'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>

              {(bulkPreview.counts.create > 0 || bulkPreview.counts.modify > 0) && (
                <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'flex-end', borderTop: `1px solid ${COLORS.border}`, paddingTop: 14 }}>
                  <div style={{ flex: '1 1 260px', minWidth: 220 }}>
                    <label style={LABEL}>SR Number (required)</label>
                    <input value={bulkSrNumber} onChange={e => setBulkSrNumber(e.target.value)} placeholder="e.g. SR-000123456" style={INPUT} />
                    <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 4 }}>
                      Applying re-checks every row against Route 53's current state before writing —
                      if something changed since Preview, the result may differ from what's shown above.
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={handleBulkApply}
                    disabled={bulkApplying || !bulkSrNumber.trim()}
                    style={btn(COLORS.success, bulkApplying || !bulkSrNumber.trim())}
                  >
                    {bulkApplying ? 'Applying…' : `Apply ${bulkPreview.counts.create + bulkPreview.counts.modify} changes`}
                  </button>
                </div>
              )}
            </>
          )}

          {bulkApplyResult && (
            <div style={{ marginTop: 14, borderTop: `1px solid ${COLORS.border}`, paddingTop: 14 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: COLORS.success, marginBottom: 8 }}>
                ✅ Bulk import applied — batch {bulkApplyResult.batch_id}
              </div>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 10, fontSize: 12 }}>
                <Badge text={`${bulkApplyResult.counts.created} created`} color={BULK_ACTION_COLOR.CREATE} />
                <Badge text={`${bulkApplyResult.counts.modified} modified`} color={BULK_ACTION_COLOR.MODIFY} />
                <Badge text={`${bulkApplyResult.counts.unchanged} unchanged`} color={BULK_ACTION_COLOR.UNCHANGED} />
                <Badge text={`${bulkApplyResult.counts.error} errors`} color={BULK_ACTION_COLOR.ERROR} />
              </div>
              {bulkApplyResult.counts.error > 0 && (
                <div style={{ overflowX: 'auto', maxHeight: 240, overflowY: 'auto', border: `1px solid ${COLORS.border}`, borderRadius: 8 }}>
                  <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                    <thead>
                      <tr style={{ textAlign: 'left', borderBottom: `1px solid ${COLORS.border}` }}>
                        <th style={TH}>Line</th><th style={TH}>Name</th><th style={TH}>Type</th><th style={TH}>Error</th>
                      </tr>
                    </thead>
                    <tbody>
                      {bulkApplyResult.rows.filter(r => r.result === 'error').map((r, i) => (
                        <tr key={i} style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                          <td style={{ ...TD, color: COLORS.textMuted }}>{r.line_no ?? '—'}</td>
                          <td style={{ ...TD, ...MONO, wordBreak: 'break-all' }}>{(r.name || '').replace(/\.$/, '')}</td>
                          <td style={TD}>{r.type || '—'}</td>
                          <td style={{ ...TD, color: COLORS.danger }}>{r.error || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── Records table ── */}
      <div style={{ ...CARD, padding: 18 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 12 }}>
          <h3 style={{ margin: 0, fontSize: 14.5, color: COLORS.textPrimary }}>
            {zone ? zone.display_name : 'Records'}
          </h3>
          <span style={{ fontSize: 11.5, color: COLORS.textMuted }}>
            {loadingRecords ? 'Loading…' : `${filtered.length}${filter ? ` of ${records.length}` : ''} records`}
          </span>
        </div>

        {!loadingRecords && !filtered.length && (
          <div style={{ fontSize: 12.5, color: COLORS.textMuted }}>
            {records.length ? 'No records match that filter.' : 'No records in this zone.'}
          </div>
        )}

        {!!filtered.length && (
          <div style={{ overflowX: 'auto' }}>
            <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ textAlign: 'left', borderBottom: `1px solid ${COLORS.border}` }}>
                  <th style={TH}>Name</th>
                  <th style={TH}>Type</th>
                  <th style={TH}>TTL</th>
                  <th style={TH}>Value</th>
                  <th style={TH}>Routing</th>
                  {canWrite && <th style={TH}></th>}
                </tr>
              </thead>
              <tbody>
                {filtered.map(r => (
                  <tr key={`${r.name}|${r.type}|${r.set_identifier || ''}`} style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                    <td style={{ ...TD, ...MONO, wordBreak: 'break-all' }}>{r.display_name.replace(/\.$/, '')}</td>
                    <td style={TD}><Badge text={r.type} color={typeColor(r.type)} /></td>
                    <td style={{ ...TD, ...MONO, color: r.ttl == null ? COLORS.textMuted : COLORS.textPrimary }}>
                      {r.ttl == null ? '—' : r.ttl}
                    </td>
                    <td style={TD}><RecordValue record={r} /></td>
                    <td style={TD}><RoutingCell record={r} /></td>
                    {canWrite && (
                      <td style={{ ...TD, whiteSpace: 'nowrap' }}>
                        {r.editable ? (
                          <span style={{ display: 'inline-flex', gap: 6 }}>
                            <button type="button" onClick={() => openModify(r)} style={smallBtn(COLORS.brand)}>Edit</button>
                            <button type="button" onClick={() => { clearBanners(); setForm(null); setPendingDelete({ record: r, sr_number: '' }) }} style={smallBtn(COLORS.danger)}>Delete</button>
                          </span>
                        ) : (
                          <span title={r.protected_reason} style={{ fontSize: 11, color: COLORS.textMuted }}>🔒 protected</span>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Audit log — only renders if the backend actually returned data
          for this user (admins + Cloud Infra/Security/Management Team).
          Silently absent for everyone else — see the module comment above
          and main.py's require_dns_audit_viewer(). */}
      {auditLog !== null && (
        <div style={{ ...CARD, padding: 18, marginTop: 28 }}>
          <h3 style={{ margin: '0 0 4px', fontSize: 14.5, color: COLORS.textPrimary }}>📋 Audit Log</h3>
          <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginBottom: 12 }}>
            Every DNS change attempt across all domains, successful or not.
            Visible only to Cloud Infra Team, Security Team, Management Team, and admins.
          </div>
          {auditLoading && <div style={{ fontSize: 12.5, color: COLORS.textSecondary }}>Loading…</div>}
          {!auditLoading && auditLog.length === 0 && (
            <div style={{ fontSize: 12.5, color: COLORS.textMuted }}>No records yet.</div>
          )}
          {!auditLoading && auditLog.length > 0 && (
            <div style={{ overflowX: 'auto' }}>
              <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead>
                  <tr style={{ textAlign: 'left', borderBottom: `1px solid ${COLORS.border}` }}>
                    {['When', 'Action', 'Domain', 'Record', 'Type', 'TTL', 'New Value', 'Previous Value', 'SR Number', 'Result', 'User'].map(h => (
                      <th key={h} style={TH}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {auditLog.map(r => (
                    <tr key={r.id} style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                      <td style={{ ...TD, whiteSpace: 'nowrap', color: COLORS.textMuted }}>{r.created_at ? new Date(r.created_at).toLocaleString() : '—'}</td>
                      <td style={TD}>{r.action}</td>
                      <td style={TD}>{r.zone_name}</td>
                      <td style={{ ...TD, ...MONO, wordBreak: 'break-all' }}>{(r.record_name || '').replace(/\.$/, '')}</td>
                      <td style={TD}>{r.record_type}</td>
                      <td style={{ ...TD, ...MONO }}>{r.ttl || '—'}</td>
                      <td style={{ ...TD, ...MONO, fontSize: 11, wordBreak: 'break-all' }}>{r.value || '—'}</td>
                      <td style={{ ...TD, ...MONO, fontSize: 11, wordBreak: 'break-all' }}>{r.previous_value || '—'}</td>
                      <td style={{ ...TD, ...MONO }}>{r.sr_number || '—'}</td>
                      <td style={TD}>
                        {r.result === 'error'
                          ? <span title={r.detail || ''} style={{ color: COLORS.danger, fontWeight: 600 }}>error</span>
                          : r.result}
                      </td>
                      <td style={TD}>{r.actor_name || r.actor_email}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
