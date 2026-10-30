import React, { useState, useRef, useEffect, useCallback } from 'react'
import { COLORS, CARD, TAB_BAR, tabButtonStyle } from '../utils/theme.js'
import { api } from '../api.js'

// ── "Helpdesk Team" catalogue ───────────────────────────────────────────────
// A new top-level sidebar section, structured as an internal-tab page (same
// pattern as CasesPage/WafMonitoringPage — see theme.js's TAB_BAR/
// tabButtonStyle docstring) so more Helpdesk Team tools can be added as
// additional tabs later without needing a new sidebar entry each time.
//
// First (and currently only) tab: S3-ScreenSaver — lets any logged-in user
// upload a file to a dedicated S3 bucket (screensaver-100000000001,
// account 100000000001, ap-southeast-1) via a single multipart POST to
// the portal's OWN backend, which then writes the bytes to S3 (see
// app/screensaver.py's module docstring for why this is backend-proxy
// rather than the original direct-browser-to-S3 presigned-PUT design —
// that was replaced after a real user's corporate network/proxy/
// firewall kept blocking direct connections to S3).
//
// Every successful upload is also republished to a fixed, permanent public
// URL — https://screensaver.example-site-a.com/screensaver/latest/current
// (served via CloudFront, see app/screensaver.py's module docstring) — so
// AD scripts / store location configs can hardcode ONE URL that always
// resolves to whichever file was most recently uploaded here, with zero
// per-store config changes needed when the screensaver is swapped out.

const TABS = [
  { key: 's3_screensaver', label: 'S3-ScreenSaver', icon: '🖼️' },
]

function formatBytes(bytes) {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

function S3ScreenSaverTab() {
  const fileInputRef = useRef(null)
  const [selectedFile, setSelectedFile] = useState(null)
  const [phase, setPhase] = useState('idle') // idle | uploading | done | error
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [copiedField, setCopiedField] = useState(null) // 'public_url' | 's3_path' | null

  // Uploaded-files list — lets the user see every historical upload
  // sitting in the bucket and manually delete old ones. See
  // app/screensaver.py's list_uploaded_files()/delete_uploaded_file()
  // docstrings for WHY this exists: old dated-key uploads are kept
  // forever as an audit trail and never auto-deleted, so the bucket
  // accumulates one object per upload. A stale/cached consumer pointed
  // directly at one of those old keys — rather than the fixed
  // latest/current URL the "Permanent URL" box above always shows the
  // newest upload at — is what actually causes the "link didn't update
  // until I manually deleted the old file from S3" symptom reported.
  const [files, setFiles] = useState([])
  const [filesLoading, setFilesLoading] = useState(false)
  const [filesError, setFilesError] = useState('')
  const [deletingKey, setDeletingKey] = useState(null)

  const loadFiles = useCallback(async () => {
    setFilesLoading(true)
    setFilesError('')
    try {
      const res = await api.listScreensaverFiles()
      setFiles(res.files || [])
    } catch (e) {
      setFilesError(e?.message || 'Could not load the uploaded files list.')
    } finally {
      setFilesLoading(false)
    }
  }, [])

  useEffect(() => { loadFiles() }, [loadFiles])

  function reset() {
    setSelectedFile(null)
    setPhase('idle')
    setResult(null)
    setError('')
    setCopiedField(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  async function handleUpload(file) {
    if (!file) return
    setSelectedFile(file)
    setResult(null)
    setError('')
    setCopiedField(null)
    setPhase('uploading')

    try {
      // Single multipart POST straight to the portal's own backend —
      // the browser never talks to S3 directly anymore (see
      // api.uploadScreensaverFile's comment for why), so a failure here
      // means either the backend rejected the file (a real HTTP error
      // with a message) or the portal itself is unreachable (session
      // expired, portal down) — never a "your network is blocking S3"
      // scenario, since S3 is never contacted from this browser.
      const uploaded = await api.uploadScreensaverFile(file)
      setResult(uploaded)
      setPhase('done')
      loadFiles() // refresh the uploaded-files list so the new key shows up right away
    } catch (e) {
      const message = e?.message ||
        'Upload failed. This usually means your portal login session expired — please refresh the page (or log out and back in) and try again.'
      // eslint-disable-next-line no-console
      console.error('[S3-ScreenSaver] upload failed', e)
      setError(message)
      setPhase('error')
    }
  }

  async function handleDelete(f) {
    if (!window.confirm(`Delete "${f.filename}" (${formatBytes(f.size_bytes)})? This cannot be undone.`)) return
    setDeletingKey(f.s3_key)
    try {
      await api.deleteScreensaverFile(f.s3_key)
      setFiles(prev => prev.filter(x => x.s3_key !== f.s3_key))
    } catch (e) {
      window.alert(e?.message || 'Could not delete this file.')
    } finally {
      setDeletingKey(null)
    }
  }

  function handleCopy(value, field) {
    if (!value) return
    navigator.clipboard?.writeText(value)
    setCopiedField(field)
    setTimeout(() => setCopiedField(null), 2000)
  }

  const busy = phase === 'uploading'
  const oldFilesCount = files.filter(f => !f.is_current).length

  return (
    <div style={{ maxWidth: 640 }}>
      <div style={{ ...CARD, padding: 24 }}>
        <h3 style={{ margin: '0 0 6px', fontSize: 16, color: COLORS.textPrimary }}>🖼️ S3-ScreenSaver — Direct File Upload</h3>
        <div style={{ fontSize: 12.5, color: COLORS.textSecondary, marginBottom: 12 }}>
          Upload a file directly to the ScreenSaver S3 bucket (CORE, Singapore region).
          Once uploaded, you'll get back a permanent public URL — hardcode it once in AD scripts / location
          configs, and every new upload here will automatically update what that URL shows.
        </div>
        <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginBottom: 12 }}>
          Supported file types: <b>JPG, PNG</b> (other file types are not blocked, but are not guaranteed to
          display correctly as a screensaver).
        </div>
        <div style={{
          fontSize: 11.5, color: COLORS.warning || '#8a6d00', background: COLORS.warningBg || '#fff8e1',
          border: `1px solid ${COLORS.warning || '#e0c46b'}60`, borderRadius: 6, padding: '8px 12px', marginBottom: 16,
        }}>
          ⚠️ Uploading a new file replaces what the permanent URL shows for <b>everyone</b> — there's only one
          "current" screensaver at a time.
        </div>

        <input
          ref={fileInputRef}
          type="file"
          onChange={e => handleUpload(e.target.files?.[0])}
          disabled={busy}
          style={{ display: 'none' }}
          id="screensaver-file-input"
        />
        <label
          htmlFor="screensaver-file-input"
          style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
            gap: 8, padding: '36px 20px', border: `2px dashed ${busy ? COLORS.border : COLORS.brand}40`,
            borderRadius: 10, background: busy ? COLORS.pageBg : COLORS.brandLight,
            cursor: busy ? 'default' : 'pointer', textAlign: 'center',
          }}
        >
          <span style={{ fontSize: 28 }}>📤</span>
          <span style={{ fontSize: 13.5, fontWeight: 600, color: COLORS.textPrimary }}>
            {busy ? 'Uploading…' : 'Click to choose a file to upload'}
          </span>
          {selectedFile && (
            <span style={{ fontSize: 11.5, color: COLORS.textSecondary }}>
              {selectedFile.name} ({formatBytes(selectedFile.size)})
            </span>
          )}
        </label>

        {busy && (
          <div style={{ marginTop: 16 }}>
            <div style={{ fontSize: 12, color: COLORS.textSecondary, marginBottom: 6 }}>
              ⬆️ Uploading…
            </div>
            <div style={{ height: 6, background: COLORS.border, borderRadius: 4, overflow: 'hidden' }}>
              <div style={{
                height: '100%', borderRadius: 4, background: COLORS.brand,
                width: '60%',
                transition: 'width 0.4s ease',
              }} />
            </div>
          </div>
        )}

        {phase === 'error' && (
          <div style={{ marginTop: 16, background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, borderRadius: 7, padding: '10px 14px', color: COLORS.danger, fontSize: 13 }}>
            ❌ {error}
          </div>
        )}

        {phase === 'done' && result && (
          <div style={{ marginTop: 16, background: COLORS.successBg, border: `1px solid ${COLORS.success}40`, borderRadius: 7, padding: '14px 16px' }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: COLORS.success, marginBottom: 10 }}>
              ✅ Upload complete
            </div>

            {result.public_url && (
              <>
                <div style={{ fontSize: 11.5, fontWeight: 700, color: COLORS.textPrimary, marginBottom: 4 }}>
                  🔗 Permanent URL — use this in AD scripts / location configs:
                </div>
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 8, background: '#fff', border: `1.5px solid ${COLORS.brand}`,
                  borderRadius: 6, padding: '8px 10px', fontFamily: 'monospace', fontSize: 12.5, color: COLORS.brand,
                  fontWeight: 600, wordBreak: 'break-all',
                }}>
                  <span style={{ flex: 1 }}>{result.public_url}</span>
                  <button onClick={() => handleCopy(result.public_url, 'public_url')}
                    style={{ padding: '4px 10px', borderRadius: 5, border: `1px solid ${COLORS.brand}`, background: COLORS.brand, color: '#fff', fontSize: 11, fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap' }}>
                    {copiedField === 'public_url' ? '✓ Copied' : '📋 Copy'}
                  </button>
                </div>
                <div style={{ marginTop: 4, marginBottom: 12, fontSize: 10.5, color: COLORS.textMuted }}>
                  This URL never changes — it always shows whichever file was most recently uploaded here.
                </div>
              </>
            )}

            <div style={{ fontSize: 11.5, fontWeight: 700, color: COLORS.textSecondary, marginBottom: 4 }}>
              This upload's own S3 path (audit record):
            </div>
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8, background: '#fff', border: `1px solid ${COLORS.border}`,
              borderRadius: 6, padding: '8px 10px', fontFamily: 'monospace', fontSize: 12.5, color: COLORS.textPrimary,
              wordBreak: 'break-all',
            }}>
              <span style={{ flex: 1 }}>{result.s3_path}</span>
              <button onClick={() => handleCopy(result.s3_path, 's3_path')}
                style={{ padding: '4px 10px', borderRadius: 5, border: `1px solid ${COLORS.brand}`, background: '#fff', color: COLORS.brand, fontSize: 11, fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap' }}>
                {copiedField === 's3_path' ? '✓ Copied' : '📋 Copy'}
              </button>
            </div>
            <div style={{ marginTop: 8, fontSize: 11, color: COLORS.textMuted }}>
              Size: {formatBytes(result.size_bytes)} · Region: {result.region} · Bucket: {result.bucket}
            </div>
            <button onClick={reset}
              style={{ marginTop: 12, padding: '6px 14px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontSize: 12, fontWeight: 600, cursor: 'pointer' }}>
              Upload another file
            </button>
          </div>
        )}

        {phase === 'error' && (
          <button onClick={reset}
            style={{ marginTop: 12, padding: '6px 14px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontSize: 12, fontWeight: 600, cursor: 'pointer' }}>
            Try again
          </button>
        )}
      </div>

      {/* Uploaded files list + cleanup reminder — see the useState/
          useCallback block above for why this exists: old dated-key
          uploads are never auto-deleted, and a stale/cached consumer
          pointed directly at an old key (rather than the permanent
          latest/current URL above) is what actually causes a "the link
          didn't update" symptom. */}
      <div style={{ ...CARD, padding: 24, marginTop: 20 }}>
        <h3 style={{ margin: '0 0 6px', fontSize: 15, color: COLORS.textPrimary }}>🗂️ Uploaded files in this bucket</h3>
        <div style={{ fontSize: 12, color: COLORS.textSecondary, marginBottom: 12 }}>
          Every past upload is kept here as an audit trail — the permanent URL above always shows the
          <b> newest</b> one. If a device/screen still shows an old image, it's usually a local cache on
          that device, not this list — but please delete old files below once you've confirmed the newest
          upload is showing correctly, so the bucket doesn't just keep accumulating.
        </div>

        {oldFilesCount > 0 && (
          <div style={{
            fontSize: 12.5, color: COLORS.warning || '#8a6d00', background: COLORS.warningBg || '#fff8e1',
            border: `1px solid ${COLORS.warning || '#e0c46b'}60`, borderRadius: 6, padding: '9px 12px', marginBottom: 14,
          }}>
            ⚠️ You have <b>{oldFilesCount}</b> old upload{oldFilesCount === 1 ? '' : 's'} still sitting in the
            bucket. Please remove {oldFilesCount === 1 ? 'it' : 'them'} below once you've confirmed the latest
            screensaver is displaying correctly.
          </div>
        )}

        {filesLoading && <div style={{ fontSize: 12.5, color: COLORS.textSecondary }}>Loading…</div>}
        {filesError && (
          <div style={{ fontSize: 12.5, color: COLORS.danger, marginBottom: 10 }}>⚠️ {filesError}</div>
        )}

        {!filesLoading && !filesError && files.length === 0 && (
          <div style={{ fontSize: 12.5, color: COLORS.textMuted }}>No uploads yet.</div>
        )}

        {!filesLoading && files.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {files.map(f => (
              <div key={f.s3_key} style={{
                display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px',
                borderRadius: 7, border: `1px solid ${f.is_current ? COLORS.brand + '60' : COLORS.border}`,
                background: f.is_current ? COLORS.brandLight : COLORS.pageBg,
              }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12.5, fontWeight: 600, color: COLORS.textPrimary, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {f.is_current && <span title="This is the file the permanent URL currently shows">🟢 </span>}
                    {f.filename}
                  </div>
                  <div style={{ fontSize: 10.5, color: COLORS.textMuted }}>
                    {formatBytes(f.size_bytes)} · {new Date(f.last_modified).toLocaleString()}
                  </div>
                </div>
                <button
                  onClick={() => handleDelete(f)}
                  disabled={deletingKey === f.s3_key}
                  title={f.is_current ? 'This is the currently-live file — deleting it just removes this audit-trail copy, not the live latest/current URL' : 'Delete this old upload'}
                  style={{
                    padding: '5px 10px', borderRadius: 5, border: `1px solid ${COLORS.danger}60`,
                    background: '#fff', color: COLORS.danger, fontSize: 11.5, fontWeight: 600,
                    cursor: deletingKey === f.s3_key ? 'default' : 'pointer', whiteSpace: 'nowrap', flexShrink: 0,
                  }}>
                  {deletingKey === f.s3_key ? 'Deleting…' : '🗑️ Delete'}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default function HelpdeskTeamPage() {
  const [tab, setTab] = useState('s3_screensaver')

  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ margin: '0 0 4px', color: COLORS.textPrimary, fontSize: 20 }}>🛎️ Helpdesk Team</h2>
      <div style={{ color: COLORS.textSecondary, fontSize: 13, marginBottom: 16 }}>
        Tools maintained by the Helpdesk Team for everyday requests.
      </div>

      <div style={{ ...TAB_BAR, marginBottom: 20 }}>
        {TABS.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)} style={tabButtonStyle(tab === t.key)}>
            {t.icon} {t.label}
          </button>
        ))}
      </div>

      {tab === 's3_screensaver' && <S3ScreenSaverTab />}
    </div>
  )
}
