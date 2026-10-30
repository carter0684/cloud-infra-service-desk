import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth/AuthContext.jsx'
import { COLORS, CARD } from '../utils/theme.js'

// ── Whitelist (LMS) ───────────────────────────────────────────────────────
// Lets a user pick one of a fixed set of WAF IPSets / a Security Group,
// type an IP, and check whether it's already whitelisted. The AWS region
// is NOT something the user picks here — the backend auto-detects which
// region the selected target actually lives in (see
// app/whitelist_lms.py's "Region — AUTO-DETECTED" docstring section) and
// returns it as part of the check/add result, purely for display. If the
// IP isn't whitelisted yet, the user must enter a Service Request
// (SR) number before the portal will actually add it — see
// app/whitelist_lms.py. Every check/add is recorded server-side; the
// audit log panel at the bottom of this page only renders data if the
// backend allows it (admins + Cloud Infra/Security/Management Team) — a
// 403 from getWhitelistLmsAuditLog() just means the panel silently shows
// nothing for this user, which is the expected/correct behavior for
// everyone else.

const INPUT = { width: '100%', padding: '9px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 14, boxSizing: 'border-box' }
const LABEL = { fontSize: 12, fontWeight: 600, color: COLORS.textSecondary, display: 'block', marginBottom: 5 }

function ResultBanner({ result, region }) {
  if (!result) return null
  const isGood = result.type === 'already' || result.type === 'added'
  const regionSuffix = region ? ` (${region})` : ''
  return (
    <div style={{
      ...CARD, padding: 14, marginTop: 14,
      background: isGood ? COLORS.successBg || '#eafaf0' : COLORS.dangerBg,
      border: `1px solid ${isGood ? COLORS.success : COLORS.danger}40`,
      color: isGood ? (COLORS.successDark || COLORS.success) : COLORS.danger,
      fontSize: 13.5, fontWeight: 600,
    }}>
      {result.type === 'already' && `⚠️ IP already in the list${regionSuffix}.`}
      {result.type === 'added' && `✅ IP have been added${regionSuffix}.`}
      {result.type === 'error' && `⚠️ ${result.message}`}
    </div>
  )
}

// ── "Linked Resources" — reference table of which domain/URL each WAF
// IPSet / Security Group target protects. Visible to anyone who can see
// this tab (same visibility as the check/add form above) — it's purely
// informational, sourced from each target's admin-curated linked_domain
// field (see app/whitelist_lms.py's "linked_domain" docstring section).
function LinkedResourcesPanel({ targets }) {
  return (
    <div style={{ ...CARD, padding: 18, marginTop: 20 }}>
      <h3 style={{ margin: '0 0 4px', fontSize: 14.5, color: COLORS.textPrimary }}>🔗 Linked Resources</h3>
      <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginBottom: 12 }}>
        Which domain/URL each WAF IPSet or Security Group target protects. This is a reference list
        maintained by admins — not something AWS itself reports automatically.
      </div>
      {targets.length === 0 && <div style={{ fontSize: 12.5, color: COLORS.textMuted }}>No targets configured.</div>}
      {targets.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
            <thead>
              <tr style={{ textAlign: 'left', borderBottom: `1px solid ${COLORS.border}` }}>
                {['Target', 'Type', 'Linked Domain / URL'].map(h => (
                  <th key={h} style={{ padding: '6px 8px', color: COLORS.textSecondary, fontWeight: 600 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {targets.map(t => (
                <tr key={t.id} style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                  <td style={{ padding: '6px 8px' }}>{t.label}</td>
                  <td style={{ padding: '6px 8px' }}>{t.resource_type === 'security_group' ? 'Security Group' : 'WAF IPSet'}</td>
                  <td style={{ padding: '6px 8px', fontFamily: t.linked_domain ? 'monospace' : 'inherit', color: t.linked_domain ? COLORS.textPrimary : COLORS.textMuted }}>
                    {t.linked_domain || '— not set —'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Admin: manage whitelist targets ────────────────────────────────────────
// Lets an admin add a new WAF IPSet (typing only its Name — the backend
// auto-resolves scope/region) or Security Group (typing its group_id +
// display label) target, edit an existing target's label/linked_domain/
// port/protocol, and remove/restore targets — all without a code deploy.
// See app/whitelist_lms.py's "Admin-editable target registry" docstring.
function ManageTargetsPanel({ onTargetsChanged }) {
  const [targets, setTargets] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const [showAddWaf, setShowAddWaf] = useState(false)
  const [showAddSg, setShowAddSg] = useState(false)
  const [editingId, setEditingId] = useState('')
  const [editDraft, setEditDraft] = useState({})

  // Add WAF IPSet target form state
  const [wafLabel, setWafLabel] = useState('')
  const [wafIpSetName, setWafIpSetName] = useState('')
  const [wafDomain, setWafDomain] = useState('')
  const [wafMatches, setWafMatches] = useState(null)  // null = not resolved yet, [] = resolved but no match
  const [wafScope, setWafScope] = useState('')
  const [wafResolving, setWafResolving] = useState(false)
  const [wafSubmitting, setWafSubmitting] = useState(false)

  // Add Security Group target form state
  const [sgLabel, setSgLabel] = useState('')
  const [sgGroupId, setSgGroupId] = useState('')
  const [sgDomain, setSgDomain] = useState('')
  const [sgPort, setSgPort] = useState('')
  const [sgProtocol, setSgProtocol] = useState('tcp')
  const [sgResolved, setSgResolved] = useState(null)   // null = not resolved yet, undefined-ish = not found
  const [sgResolving, setSgResolving] = useState(false)
  const [sgSubmitting, setSgSubmitting] = useState(false)

  const flash = (msg) => { setSuccess(msg); setTimeout(() => setSuccess(''), 3000) }

  const load = useCallback(() => {
    setLoading(true)
    api.getWhitelistLmsTargetsAdmin()
      .then(res => { setTargets(res.targets || []); setError('') })
      .catch(e => setError(e?.response?.data?.detail || 'Failed to load targets'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  async function handleResolveWaf() {
    if (!wafIpSetName.trim()) return
    setWafResolving(true)
    setError('')
    setWafMatches(null)
    setWafScope('')
    try {
      const res = await api.resolveWhitelistLmsWafIpset(wafIpSetName.trim())
      setWafMatches(res.matches || [])
      if (res.matches?.length === 1) setWafScope(res.matches[0].waf_scope)
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to look up this WAF IPSet')
    } finally {
      setWafResolving(false)
    }
  }

  async function handleAddWaf(e) {
    e.preventDefault()
    if (!wafLabel.trim() || !wafIpSetName.trim() || !wafScope) return
    setWafSubmitting(true)
    setError('')
    try {
      await api.addWhitelistLmsWafTarget(wafLabel.trim(), wafIpSetName.trim(), wafScope, wafDomain.trim())
      flash(`Added WAF IPSet target "${wafLabel.trim()}"`)
      setWafLabel(''); setWafIpSetName(''); setWafDomain(''); setWafMatches(null); setWafScope(''); setShowAddWaf(false)
      load(); onTargetsChanged?.()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to add WAF IPSet target')
    } finally {
      setWafSubmitting(false)
    }
  }

  async function handleResolveSg() {
    if (!sgGroupId.trim()) return
    setSgResolving(true)
    setError('')
    setSgResolved(null)
    try {
      const res = await api.resolveWhitelistLmsSecurityGroup(sgGroupId.trim())
      setSgResolved(res.found || false)
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to look up this security group')
    } finally {
      setSgResolving(false)
    }
  }

  async function handleAddSg(e) {
    e.preventDefault()
    if (!sgLabel.trim() || !sgGroupId.trim()) return
    setSgSubmitting(true)
    setError('')
    try {
      await api.addWhitelistLmsSgTarget(
        sgLabel.trim(), sgGroupId.trim(), sgDomain.trim(),
        sgPort ? parseInt(sgPort, 10) : null, sgProtocol,
      )
      flash(`Added Security Group target "${sgLabel.trim()}"`)
      setSgLabel(''); setSgGroupId(''); setSgDomain(''); setSgPort(''); setSgProtocol('tcp'); setSgResolved(null); setShowAddSg(false)
      load(); onTargetsChanged?.()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to add security group target')
    } finally {
      setSgSubmitting(false)
    }
  }

  function startEdit(t) {
    setEditingId(t.id)
    setEditDraft({ label: t.label, linked_domain: t.linked_domain || '', port: t.port || '', protocol: t.protocol || '' })
  }

  async function handleSaveEdit(t) {
    setError('')
    try {
      const patch = { label: editDraft.label, linked_domain: editDraft.linked_domain || null }
      if (t.resource_type === 'security_group') {
        patch.port = editDraft.port ? parseInt(editDraft.port, 10) : null
        patch.protocol = editDraft.protocol || null
      }
      await api.updateWhitelistLmsTarget(t.id, patch)
      flash(`Updated "${editDraft.label}"`)
      setEditingId('')
      load(); onTargetsChanged?.()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to update target')
    }
  }

  async function handleRemove(t) {
    if (!window.confirm(`Remove "${t.label}" from the Whitelist target list? This only affects the portal's picker — it does NOT change anything in AWS itself.`)) return
    setError('')
    try {
      await api.removeWhitelistLmsTarget(t.id)
      flash(`Removed "${t.label}"`)
      load(); onTargetsChanged?.()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to remove target')
    }
  }

  async function handleRestore(t) {
    setError('')
    try {
      await api.restoreWhitelistLmsTarget(t.id)
      flash(`Restored "${t.label}"`)
      load(); onTargetsChanged?.()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to restore target')
    }
  }

  const SMALL_INPUT = { padding: '7px 9px', borderRadius: 6, border: `1px solid ${COLORS.border}`, fontSize: 12.5, boxSizing: 'border-box' }
  const SMALL_BTN = { padding: '6px 12px', borderRadius: 6, border: 'none', fontSize: 12, fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap' }

  return (
    <div style={{ ...CARD, padding: 18, marginTop: 28, border: `1px solid ${COLORS.brand}30` }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
        <h3 style={{ margin: 0, fontSize: 14.5, color: COLORS.textPrimary }}>⚙️ Manage Whitelist Targets (Admin)</h3>
      </div>
      <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginBottom: 14 }}>
        Add/edit/remove which WAF IPSets and Security Groups appear in the picker above. The security team must
        already have created the underlying resource in AWS — this only registers it with the portal. A newly
        added target's <b>Add</b> action may still fail with an AWS permission error until the
        job-portal-whitelist-lms credentials are also granted access to that specific resource.
      </div>

      {error && <div style={{ background: COLORS.dangerBg, color: COLORS.danger, padding: '8px 12px', borderRadius: 6, fontSize: 12.5, marginBottom: 12 }}>{error}</div>}
      {success && <div style={{ background: COLORS.successBg || '#eafaf0', color: COLORS.success, padding: '8px 12px', borderRadius: 6, fontSize: 12.5, marginBottom: 12 }}>✓ {success}</div>}

      <div style={{ display: 'flex', gap: 10, marginBottom: 16 }}>
        <button onClick={() => { setShowAddWaf(v => !v); setShowAddSg(false) }} style={{ ...SMALL_BTN, background: COLORS.brand, color: '#fff' }}>
          {showAddWaf ? 'Cancel' : '+ Add WAF IPSet'}
        </button>
        <button onClick={() => { setShowAddSg(v => !v); setShowAddWaf(false) }} style={{ ...SMALL_BTN, background: COLORS.brand, color: '#fff' }}>
          {showAddSg ? 'Cancel' : '+ Add Security Group'}
        </button>
      </div>

      {showAddWaf && (
        <form onSubmit={handleAddWaf} style={{ background: '#f8fafc', border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: 14, marginBottom: 16 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 10 }}>
            <div>
              <label style={LABEL}>Display Label</label>
              <input value={wafLabel} onChange={e => setWafLabel(e.target.value)} placeholder="e.g. LMS Booking Portal Whitelist" style={{ ...SMALL_INPUT, width: '100%' }} />
            </div>
            <div>
              <label style={LABEL}>WAF IPSet Name (must already exist in AWS)</label>
              <div style={{ display: 'flex', gap: 6 }}>
                <input value={wafIpSetName} onChange={e => { setWafIpSetName(e.target.value); setWafMatches(null); setWafScope('') }}
                  placeholder="exact IPSet Name in AWS WAF console" style={{ ...SMALL_INPUT, flex: 1 }} />
                <button type="button" onClick={handleResolveWaf} disabled={wafResolving || !wafIpSetName.trim()}
                  style={{ ...SMALL_BTN, background: COLORS.info, color: '#fff', opacity: wafResolving ? 0.6 : 1 }}>
                  {wafResolving ? '…' : 'Find'}
                </button>
              </div>
            </div>
          </div>

          {wafMatches !== null && wafMatches.length === 0 && (
            <div style={{ fontSize: 12, color: COLORS.danger, marginBottom: 10 }}>
              ⚠️ Not found under REGIONAL (either region) or CLOUDFRONT scope. Double-check the exact Name in the AWS WAF console.
            </div>
          )}
          {wafMatches !== null && wafMatches.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <label style={LABEL}>Found {wafMatches.length > 1 ? `${wafMatches.length} matches — pick the right one` : 'match'}</label>
              {wafMatches.map(m => (
                <label key={`${m.waf_scope}-${m.region}`} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, padding: '4px 0', cursor: 'pointer' }}>
                  <input type="radio" name="waf-scope-match" checked={wafScope === m.waf_scope} onChange={() => setWafScope(m.waf_scope)} />
                  {m.waf_scope.toUpperCase()} scope, {m.region} — {m.address_count} address{m.address_count === 1 ? '' : 'es'} currently in the set
                </label>
              ))}
            </div>
          )}

          <label style={LABEL}>Linked Domain / URL (optional, for the reference list below)</label>
          <input value={wafDomain} onChange={e => setWafDomain(e.target.value)} placeholder="e.g. lms.example.com" style={{ ...SMALL_INPUT, width: '100%', marginBottom: 12 }} />

          <button type="submit" disabled={wafSubmitting || !wafLabel.trim() || !wafIpSetName.trim() || !wafScope}
            style={{ ...SMALL_BTN, background: COLORS.success, color: '#fff', opacity: (wafSubmitting || !wafScope) ? 0.6 : 1 }}>
            {wafSubmitting ? 'Adding…' : '✅ Register this target'}
          </button>
        </form>
      )}

      {showAddSg && (
        <form onSubmit={handleAddSg} style={{ background: '#f8fafc', border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: 14, marginBottom: 16 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 10 }}>
            <div>
              <label style={LABEL}>Display Label</label>
              <input value={sgLabel} onChange={e => setSgLabel(e.target.value)} placeholder="e.g. AVPC-SFTP-Client-SG-04" style={{ ...SMALL_INPUT, width: '100%' }} />
            </div>
            <div>
              <label style={LABEL}>Security Group ID (must already exist in AWS)</label>
              <div style={{ display: 'flex', gap: 6 }}>
                <input value={sgGroupId} onChange={e => { setSgGroupId(e.target.value); setSgResolved(null) }}
                  placeholder="sg-xxxxxxxxxxxxxxxxx" style={{ ...SMALL_INPUT, flex: 1, fontFamily: 'monospace' }} />
                <button type="button" onClick={handleResolveSg} disabled={sgResolving || !sgGroupId.trim()}
                  style={{ ...SMALL_BTN, background: COLORS.info, color: '#fff', opacity: sgResolving ? 0.6 : 1 }}>
                  {sgResolving ? '…' : 'Find'}
                </button>
              </div>
            </div>
          </div>

          {sgResolved === false && (
            <div style={{ fontSize: 12, color: COLORS.danger, marginBottom: 10 }}>
              ⚠️ Not found in either us-east-1 or ap-southeast-1. Double-check the group ID.
            </div>
          )}
          {sgResolved && (
            <div style={{ fontSize: 12, color: COLORS.success, marginBottom: 10 }}>
              ✅ Found: {sgResolved.group_name || '(unnamed)'} in {sgResolved.region} (VPC {sgResolved.vpc_id})
            </div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 1fr', gap: 10, marginBottom: 10 }}>
            <div>
              <label style={LABEL}>Linked Domain / URL (optional)</label>
              <input value={sgDomain} onChange={e => setSgDomain(e.target.value)} placeholder="e.g. sftp.example.com" style={{ ...SMALL_INPUT, width: '100%' }} />
            </div>
            <div>
              <label style={LABEL}>Port (default 22)</label>
              <input value={sgPort} onChange={e => setSgPort(e.target.value)} placeholder="22" type="number" style={{ ...SMALL_INPUT, width: '100%' }} />
            </div>
            <div>
              <label style={LABEL}>Protocol</label>
              <select value={sgProtocol} onChange={e => setSgProtocol(e.target.value)} style={{ ...SMALL_INPUT, width: '100%', background: '#fff' }}>
                <option value="tcp">tcp</option>
                <option value="udp">udp</option>
              </select>
            </div>
          </div>

          <button type="submit" disabled={sgSubmitting || !sgLabel.trim() || !sgGroupId.trim() || sgResolved === false}
            style={{ ...SMALL_BTN, background: COLORS.success, color: '#fff', opacity: sgSubmitting ? 0.6 : 1 }}>
            {sgSubmitting ? 'Adding…' : '✅ Register this target'}
          </button>
        </form>
      )}

      {loading ? (
        <div style={{ fontSize: 12.5, color: COLORS.textSecondary }}>Loading targets…</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table data-cards style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
            <thead>
              <tr style={{ textAlign: 'left', borderBottom: `1px solid ${COLORS.border}` }}>
                {['Target', 'Type', 'Linked Domain', 'Source', ''].map(h => (
                  <th key={h} style={{ padding: '6px 8px', color: COLORS.textSecondary, fontWeight: 600 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(targets || []).map(t => {
                const isEditing = editingId === t.id
                return (
                  <tr key={t.id} style={{ borderBottom: `1px solid ${COLORS.border}`, opacity: t.removed ? 0.5 : 1 }}>
                    {isEditing ? (
                      <>
                        <td style={{ padding: '6px 8px' }}>
                          <input value={editDraft.label} onChange={e => setEditDraft(d => ({ ...d, label: e.target.value }))} style={{ ...SMALL_INPUT, width: '100%' }} />
                        </td>
                        <td style={{ padding: '6px 8px' }}>{t.resource_type === 'security_group' ? 'Security Group' : 'WAF IPSet'}</td>
                        <td style={{ padding: '6px 8px' }}>
                          <input value={editDraft.linked_domain} onChange={e => setEditDraft(d => ({ ...d, linked_domain: e.target.value }))}
                            placeholder="domain / URL" style={{ ...SMALL_INPUT, width: '100%' }} />
                          {t.resource_type === 'security_group' && (
                            <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                              <input value={editDraft.port} onChange={e => setEditDraft(d => ({ ...d, port: e.target.value }))}
                                placeholder="port" type="number" style={{ ...SMALL_INPUT, width: 70 }} />
                              <select value={editDraft.protocol} onChange={e => setEditDraft(d => ({ ...d, protocol: e.target.value }))} style={{ ...SMALL_INPUT, background: '#fff' }}>
                                <option value="tcp">tcp</option>
                                <option value="udp">udp</option>
                              </select>
                            </div>
                          )}
                        </td>
                        <td style={{ padding: '6px 8px' }}>{t.is_custom ? 'Custom' : 'Default'}</td>
                        <td style={{ padding: '6px 8px', whiteSpace: 'nowrap' }}>
                          <button onClick={() => handleSaveEdit(t)} style={{ ...SMALL_BTN, background: COLORS.success, color: '#fff', marginRight: 6 }}>Save</button>
                          <button onClick={() => setEditingId('')} style={{ ...SMALL_BTN, background: '#e2e8f0', color: COLORS.textSecondary }}>Cancel</button>
                        </td>
                      </>
                    ) : (
                      <>
                        <td style={{ padding: '6px 8px' }}>{t.label}</td>
                        <td style={{ padding: '6px 8px' }}>{t.resource_type === 'security_group' ? 'Security Group' : 'WAF IPSet'}</td>
                        <td style={{ padding: '6px 8px', fontFamily: t.linked_domain ? 'monospace' : 'inherit', color: t.linked_domain ? COLORS.textPrimary : COLORS.textMuted }}>
                          {t.linked_domain || '— not set —'}
                        </td>
                        <td style={{ padding: '6px 8px' }}>{t.is_custom ? 'Custom' : 'Default'}</td>
                        <td style={{ padding: '6px 8px', whiteSpace: 'nowrap' }}>
                          <button onClick={() => startEdit(t)} style={{ ...SMALL_BTN, background: '#eff6ff', color: COLORS.brand, marginRight: 6 }}>Edit</button>
                          <button onClick={() => handleRemove(t)} style={{ ...SMALL_BTN, background: COLORS.dangerBg, color: COLORS.danger }}>Remove</button>
                        </td>
                      </>
                    )}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function WhitelistLmsPage() {
  const { user } = useAuth()
  const [targets, setTargets] = useState([])
  const [targetId, setTargetId] = useState('')
  const [ipAddress, setIpAddress] = useState('')
  const [srNumber, setSrNumber] = useState('')

  const [checking, setChecking] = useState(false)
  const [adding, setAdding] = useState(false)
  const [needsSr, setNeedsSr] = useState(false)   // true once a check confirms the IP is NOT whitelisted
  const [result, setResult] = useState(null)
  // The region is never chosen by the user — it's auto-detected
  // server-side per target and echoed back on every check/add response,
  // purely for display (e.g. "not yet whitelisted on X (us-east-1)").
  const [detectedRegion, setDetectedRegion] = useState(null)

  const [auditLog, setAuditLog] = useState(null)   // null = not authorized / not loaded, [] = authorized but empty
  const [auditLoading, setAuditLoading] = useState(false)

  // "Linked Resources" — always-visible reference table showing every
  // target's domain/URL, purely from targets' linked_domain field (see
  // app/whitelist_lms.py's "linked_domain" docstring section — this is
  // admin-curated display info, not something AWS itself exposes).
  const [showLinkedResources, setShowLinkedResources] = useState(false)

  const loadTargets = useCallback(() => {
    api.getWhitelistLmsTargets().then(res => {
      setTargets(res.targets || [])
      setTargetId(prev => prev || (res.targets?.length ? res.targets[0].id : ''))
    }).catch(() => {})
  }, [])

  useEffect(() => { loadTargets() }, [loadTargets])

  const loadAuditLog = useCallback(() => {
    setAuditLoading(true)
    api.getWhitelistLmsAuditLog(200)
      .then(res => setAuditLog(res.records || []))
      .catch(() => setAuditLog(null))  // 403 (not in Cloud Infra/Security/Management Team) — leave panel hidden
      .finally(() => setAuditLoading(false))
  }, [])

  useEffect(() => { loadAuditLog() }, [loadAuditLog])

  function resetForNewSearch() {
    setResult(null)
    setNeedsSr(false)
    setSrNumber('')
    setDetectedRegion(null)
  }

  async function handleCheck(e) {
    e.preventDefault()
    if (!targetId || !ipAddress.trim()) return
    resetForNewSearch()
    setChecking(true)
    try {
      const res = await api.checkWhitelistLmsIp(targetId, ipAddress.trim())
      setDetectedRegion(res.region)
      if (res.already_whitelisted) {
        setResult({ type: 'already' })
        setNeedsSr(false)
      } else {
        setNeedsSr(true)  // not whitelisted yet — reveal the SR number box + "Add" button
      }
    } catch (e) {
      setResult({ type: 'error', message: e?.response?.data?.detail || e?.message || 'Could not check this IP.' })
    } finally {
      setChecking(false)
    }
  }

  async function handleAdd(e) {
    e.preventDefault()
    if (!srNumber.trim()) return
    setAdding(true)
    try {
      const res = await api.addWhitelistLmsIp(targetId, ipAddress.trim(), srNumber.trim())
      setDetectedRegion(res.region)
      setResult({ type: res.already_whitelisted ? 'already' : 'added' })
      setNeedsSr(false)
      loadAuditLog()
    } catch (e) {
      setResult({ type: 'error', message: e?.response?.data?.detail || e?.message || 'Could not add this IP.' })
    } finally {
      setAdding(false)
    }
  }

  const selectedTarget = targets.find(t => t.id === targetId)

  return (
    <div style={{ padding: 24, maxWidth: 900 }}>
      <h2 style={{ margin: '0 0 4px', fontSize: 19, color: COLORS.textPrimary }}>🔓 Whitelist — LMS</h2>
      <div style={{ fontSize: 13, color: COLORS.textSecondary, marginBottom: 18 }}>
        Select a target WAF IPSet or Security Group, then check whether an IP is already whitelisted.
        If not, you'll need to provide your Service Request (SR) number before it can be added —
        please make sure you've already submitted the SR for this change.
      </div>

      <form onSubmit={handleCheck} style={{ ...CARD, padding: 18, marginBottom: 20 }}>
        <div style={{ marginBottom: 14 }}>
          <label style={LABEL}>Whitelist Target (WAF IPSet / Security Group)</label>
          <select value={targetId} onChange={e => { setTargetId(e.target.value); resetForNewSearch() }} style={{ ...INPUT, background: '#fff' }}>
            {targets.map(t => (
              <option key={t.id} value={t.id}>
                {t.label} {t.resource_type === 'security_group' ? '(Security Group)' : '(WAF IPSet)'}
                {t.linked_domain ? ` — ${t.linked_domain}` : ''}
              </option>
            ))}
          </select>
          <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 4 }}>
            The AWS region (us-east-1 / ap-southeast-1) is detected automatically for this target — no need to select it.
          </div>
        </div>

        <label style={LABEL}>IP Address</label>
        <div style={{ display: 'flex', gap: 10 }}>
          <input value={ipAddress} onChange={e => { setIpAddress(e.target.value); resetForNewSearch() }}
            placeholder="e.g. 203.0.113.10" style={{ ...INPUT, fontFamily: 'monospace' }} />
          <button type="submit" disabled={checking || !ipAddress.trim() || !targetId}
            style={{
              padding: '9px 20px', borderRadius: 7, border: 'none', background: COLORS.brand, color: '#fff',
              fontSize: 13.5, fontWeight: 600, whiteSpace: 'nowrap',
              cursor: checking || !ipAddress.trim() ? 'default' : 'pointer',
              opacity: checking || !ipAddress.trim() ? 0.6 : 1,
            }}>
            {checking ? 'Checking…' : '🔍 Check'}
          </button>
        </div>
      </form>

      <ResultBanner result={result} region={detectedRegion} />

      {needsSr && (
        <form onSubmit={handleAdd} style={{ ...CARD, padding: 18, marginTop: 14, border: `1px solid ${COLORS.warning}60` }}>
          <div style={{ fontSize: 13, color: COLORS.textPrimary, marginBottom: 12 }}>
            <b>{ipAddress}</b> is not yet whitelisted on <b>{selectedTarget?.label}</b>{detectedRegion ? ` (${detectedRegion})` : ''}.
            Enter the Service Request (SR) number you've already submitted for this change to proceed.
          </div>
          <label style={LABEL}>SR Number</label>
          <div style={{ display: 'flex', gap: 10 }}>
            <input value={srNumber} onChange={e => setSrNumber(e.target.value)}
              placeholder="e.g. SR-000123456" style={INPUT} />
            <button type="submit" disabled={adding || !srNumber.trim()}
              style={{
                padding: '9px 20px', borderRadius: 7, border: 'none', background: COLORS.success, color: '#fff',
                fontSize: 13.5, fontWeight: 600, whiteSpace: 'nowrap',
                cursor: adding || !srNumber.trim() ? 'default' : 'pointer',
                opacity: adding || !srNumber.trim() ? 0.6 : 1,
              }}>
              {adding ? 'Adding…' : '➕ Add to Whitelist'}
            </button>
          </div>
        </form>
      )}

      {/* Audit log — only renders if the backend actually returned data
          for this user (admins + Cloud Infra/Security/Management Team).
          Silently absent for everyone else — see the module docstring
          above and main.py's require_whitelist_audit_viewer(). */}
      {auditLog !== null && (
        <div style={{ ...CARD, padding: 18, marginTop: 28 }}>
          <h3 style={{ margin: '0 0 4px', fontSize: 14.5, color: COLORS.textPrimary }}>📋 Audit Log</h3>
          <div style={{ fontSize: 11.5, color: COLORS.textMuted, marginBottom: 12 }}>
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
                    {['When', 'Action', 'Target', 'Region', 'IP', 'SR Number', 'Result', 'User'].map(h => (
                      <th key={h} style={{ padding: '6px 8px', color: COLORS.textSecondary, fontWeight: 600 }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {auditLog.map(r => (
                    <tr key={r.id} style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                      <td style={{ padding: '6px 8px', whiteSpace: 'nowrap', color: COLORS.textMuted }}>{r.created_at ? new Date(r.created_at).toLocaleString() : '—'}</td>
                      <td style={{ padding: '6px 8px' }}>{r.action}</td>
                      <td style={{ padding: '6px 8px' }}>{r.target_label}</td>
                      <td style={{ padding: '6px 8px' }}>{r.region}</td>
                      <td style={{ padding: '6px 8px', fontFamily: 'monospace' }}>{r.ip_address}</td>
                      <td style={{ padding: '6px 8px', fontFamily: 'monospace' }}>{r.sr_number || '—'}</td>
                      <td style={{ padding: '6px 8px' }}>{r.result}</td>
                      <td style={{ padding: '6px 8px' }}>{r.actor_name || r.actor_email}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Linked Resources — always visible reference list (see
          LinkedResourcesPanel above), collapsed by default to keep the
          page compact. */}
      <div style={{ marginTop: 20 }}>
        <button onClick={() => setShowLinkedResources(v => !v)}
          style={{ border: 'none', background: 'none', color: COLORS.brand, fontSize: 12.5, fontWeight: 600, cursor: 'pointer', padding: 0 }}>
          {showLinkedResources ? '▾' : '▸'} 🔗 Show linked domains/URLs for all targets
        </button>
        {showLinkedResources && <LinkedResourcesPanel targets={targets} />}
      </div>

      {/* Admin-only target registry management — see ManageTargetsPanel
          above. Hidden entirely for non-admins (the backend also
          enforces this via require_admin on every underlying endpoint,
          so this is UX convenience, not the actual access control). */}
      {user?.is_admin && (
        <ManageTargetsPanel onTargetsChanged={loadTargets} />
      )}
    </div>
  )
}
