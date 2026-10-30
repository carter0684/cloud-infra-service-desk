import React, { useState, useEffect, useCallback } from 'react'
import { api } from '../api.js'
import { COLORS, CARD, InfoBanner } from '../utils/theme.js'

// ── S2S VPN Profile Creation (Security) ─────────────────────────────────
// A 3-step wizard that CREATES real AWS Site-to-Site VPN infrastructure
// in the network account (100000000003): a Customer Gateway, a VPN
// connection (attached to one of four fixed Transit Gateways), and a
// static route on that Transit Gateway's route table. See
// app/s2s_vpn_creation.py for the backend design.
//
// THIS PAGE CAN CREATE REAL, BILLABLE, HARD-TO-FULLY-UNDO AWS NETWORK
// RESOURCES, so — mirroring DnsRecordsPage.jsx's precedent exactly —
// write controls are hidden unless the backend says this user may write
// (getS2sVpnPermissions -> can_write: admins + Cloud Infra/Security
// Team), and every create action demands a Service Request (SR)
// number before it will submit. That's a UI convenience only — every
// write route re-checks server-side.
//
// Unlike DNS (a single flat form), this is a genuine multi-step wizard
// because the three AWS resources are created in sequence and each
// later step needs an id produced by the step before it (the VPN
// connection needs a Customer Gateway id; the route needs the VPN
// connection id).

const INPUT = { width: '100%', padding: '9px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 14, boxSizing: 'border-box' }
const LABEL = { fontSize: 12, fontWeight: 600, color: COLORS.textSecondary, display: 'block', marginBottom: 5 }
const MONO = { fontFamily: 'monospace' }

function btn(bg, disabled) {
  return {
    padding: '9px 18px', borderRadius: 7, border: 'none', background: bg, color: '#fff',
    fontSize: 13.5, fontWeight: 600, whiteSpace: 'nowrap',
    cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.6 : 1,
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

const STEPS = [
  { key: 'region', label: '1. Region' },
  { key: 'cgw', label: '2. Customer Gateway' },
  { key: 'vpn', label: '3. VPN Connection' },
  { key: 'route', label: '4. Route Table' },
]

function StepBar({ step, cgwId, vpnId }) {
  const idx = STEPS.findIndex(s => s.key === step)
  return (
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 18 }}>
      {STEPS.map((s, i) => {
        const active = i === idx
        const done = i < idx
        return (
          <div key={s.key} style={{
            display: 'flex', alignItems: 'center', gap: 6, padding: '6px 12px', borderRadius: 20,
            background: active ? COLORS.brand : done ? COLORS.successBg : '#f1f4f9',
            color: active ? '#fff' : done ? COLORS.success : COLORS.textMuted,
            fontSize: 12, fontWeight: 700,
            border: done ? `1px solid ${COLORS.success}40` : 'none',
          }}>
            {done ? '✓' : i + 1} {s.label.replace(/^\d+\.\s*/, '')}
          </div>
        )
      })}
    </div>
  )
}

const EMPTY_CGW_FORM = { name_tag: '', ip_address: '', bgp_asn: '65000', sr_number: '' }
const EMPTY_VPN_FORM = { name_tag: '', target_gateway_type: 'Transit gateway', virtual_private_gateway_id: '', routing: 'static', sr_number: '' }
const EMPTY_ROUTE_FORM = { cidr: '', attachment_ref: '', sr_number: '' }

export default function S2sVpnProfileCreationPage() {
  const [config, setConfig] = useState(null)
  const [loadingConfig, setLoadingConfig] = useState(true)
  const [canWrite, setCanWrite] = useState(false)

  const [step, setStep] = useState('region')
  const [region, setRegion] = useState('')

  const [error, setError] = useState(null)
  const [success, setSuccess] = useState(null)

  // Step 2: Customer Gateway
  const [existingCgws, setExistingCgws] = useState([])
  const [loadingCgws, setLoadingCgws] = useState(false)
  const [cgwPopupOpen, setCgwPopupOpen] = useState(false)
  const [cgwForm, setCgwForm] = useState(EMPTY_CGW_FORM)
  const [creatingCgw, setCreatingCgw] = useState(false)
  const [selectedCgw, setSelectedCgw] = useState(null) // {customer_gateway_id, name, ...}

  // Step 3: VPN connection
  const [vpnForm, setVpnForm] = useState(EMPTY_VPN_FORM)
  const [creatingVpn, setCreatingVpn] = useState(false)
  const [createdVpn, setCreatedVpn] = useState(null)

  // Step 4: Route
  const [routeForm, setRouteForm] = useState(EMPTY_ROUTE_FORM)
  const [creatingRoute, setCreatingRoute] = useState(false)
  const [createdRoute, setCreatedRoute] = useState(null)

  useEffect(() => {
    api.getS2sVpnPermissions().then(r => setCanWrite(!!r.can_write)).catch(() => setCanWrite(false))
  }, [])

  useEffect(() => {
    setLoadingConfig(true)
    api.getS2sVpnConfig()
      .then(cfg => setConfig(cfg))
      .catch(e => setError(e?.response?.data?.detail || 'Could not load the S2S VPN configuration.'))
      .finally(() => setLoadingConfig(false))
  }, [])

  function clearBanners() { setError(null); setSuccess(null) }

  function resetWizard() {
    setStep('region')
    setRegion('')
    setExistingCgws([])
    setCgwPopupOpen(false)
    setCgwForm(EMPTY_CGW_FORM)
    setSelectedCgw(null)
    setVpnForm(EMPTY_VPN_FORM)
    setCreatedVpn(null)
    setRouteForm(EMPTY_ROUTE_FORM)
    setCreatedRoute(null)
    clearBanners()
  }

  const loadExistingCgws = useCallback((r) => {
    if (!r) return
    setLoadingCgws(true)
    api.listS2sVpnCustomerGateways(r)
      .then(res => setExistingCgws(res.customer_gateways || []))
      .catch(() => setExistingCgws([]))
      .finally(() => setLoadingCgws(false))
  }, [])

  function handleRegionContinue() {
    if (!region) return
    clearBanners()
    loadExistingCgws(region)
    setCgwPopupOpen(true)  // show "reuse an existing CGW?" popup first, per spec
    setStep('cgw')
  }

  function pickExistingCgw(cgw) {
    setSelectedCgw(cgw)
    setCgwPopupOpen(false)
    clearBanners()
  }

  function startNewCgw() {
    setSelectedCgw(null)
    setCgwPopupOpen(false)
    setCgwForm(EMPTY_CGW_FORM)
    clearBanners()
  }

  async function handleCreateCgw(e) {
    e.preventDefault()
    if (!cgwForm.sr_number.trim()) return
    clearBanners()
    setCreatingCgw(true)
    try {
      const result = await api.createS2sVpnCustomerGateway({
        region,
        name_tag: cgwForm.name_tag.trim(),
        ip_address: cgwForm.ip_address.trim(),
        bgp_asn: Number(cgwForm.bgp_asn),
        sr_number: cgwForm.sr_number.trim(),
      })
      setSelectedCgw(result)
      setSuccess(`✅ Created Customer Gateway ${result.customer_gateway_id} (${result.name}).`)
      setStep('vpn')
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || 'Could not create this Customer Gateway.')
    } finally {
      setCreatingCgw(false)
    }
  }

  function confirmCgwAndContinue() {
    if (!selectedCgw) return
    clearBanners()
    setStep('vpn')
  }

  async function handleCreateVpn(e) {
    e.preventDefault()
    if (!vpnForm.sr_number.trim() || !selectedCgw) return
    clearBanners()
    setCreatingVpn(true)
    try {
      const result = await api.createS2sVpnConnection({
        region,
        name_tag: vpnForm.name_tag.trim(),
        customer_gateway_id: selectedCgw.customer_gateway_id,
        target_gateway_type: vpnForm.target_gateway_type,
        virtual_private_gateway_id: vpnForm.virtual_private_gateway_id.trim() || null,
        static_routes_only: vpnForm.routing === 'static',
        sr_number: vpnForm.sr_number.trim(),
      })
      setCreatedVpn(result)
      setSuccess(`✅ Created Site-to-Site VPN connection ${result.vpn_connection_id} (${result.name}).`)
      setRouteForm(f => ({ ...f, attachment_ref: result.vpn_connection_id }))
      setStep('route')
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || 'Could not create this VPN connection.')
    } finally {
      setCreatingVpn(false)
    }
  }

  async function handleCreateRoute(e) {
    e.preventDefault()
    if (!routeForm.sr_number.trim()) return
    clearBanners()
    setCreatingRoute(true)
    try {
      const result = await api.createS2sVpnRoute({
        region,
        cidr: routeForm.cidr.trim(),
        attachment_ref: routeForm.attachment_ref.trim(),
        sr_number: routeForm.sr_number.trim(),
      })
      setCreatedRoute(result)
      setSuccess(`✅ Added route ${result.destination_cidr_block} to ${result.route_table_name} (${result.route_table_id}).`)
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || 'Could not add this route.')
    } finally {
      setCreatingRoute(false)
    }
  }

  if (loadingConfig) {
    return <div style={{ padding: 24 }}>Loading…</div>
  }

  if (!config) {
    return (
      <div style={{ padding: 24, maxWidth: 900 }}>
        <h2 style={{ margin: '0 0 4px', fontSize: 19, color: COLORS.textPrimary }}>🔒 S2S VPN Profile Creation</h2>
        <Banner tone="error">{error || 'Could not load configuration.'}</Banner>
      </div>
    )
  }

  const tgw = region ? config.transit_gateways[region] : null
  const rt = region ? config.tgw_route_tables[region] : null
  const tunnel = config.tunnel_config_standard

  return (
    <div style={{ padding: 24, maxWidth: 1000 }}>
      <h2 style={{ margin: '0 0 4px', fontSize: 19, color: COLORS.textPrimary }}>🔒 S2S VPN Profile Creation</h2>
      <div style={{ fontSize: 13, color: COLORS.textSecondary, marginBottom: 14 }}>
        Create a Customer Gateway, a Site-to-Site VPN connection, and a static route on a Transit Gateway
        route table in the <b>network</b> account ({config.account_id}) — {config.allowed_regions.join(', ')} only.
      </div>

      <InfoBanner
        icon="⚠️"
        text={canWrite
          ? 'This wizard creates REAL AWS network infrastructure (billable, hard to fully undo). Every step needs a Service Request (SR) number and is written to the audit log.'
          : 'Read-only view. Creating S2S VPN infrastructure is limited to admins and the Cloud Infra and Security Teams.'}
      />

      <Banner tone="error">{error}</Banner>
      <Banner tone="success">{success}</Banner>

      {!canWrite ? (
        <div style={{ ...CARD, padding: 24, textAlign: 'center', color: COLORS.textMuted, fontSize: 13.5 }}>
          You do not have permission to create S2S VPN infrastructure.
        </div>
      ) : (
        <>
          <StepBar step={step} />

          {/* ── Step 1: Region ── */}
          {step === 'region' && (
            <div style={{ ...CARD, padding: 18 }}>
              <h3 style={{ margin: '0 0 12px', fontSize: 15, color: COLORS.textPrimary }}>1. Select a region</h3>
              <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'flex-end' }}>
                <div style={{ flex: '1 1 260px', minWidth: 220 }}>
                  <label style={LABEL}>Region</label>
                  <select value={region} onChange={e => setRegion(e.target.value)} style={{ ...INPUT, background: '#fff' }}>
                    <option value="">Select a region…</option>
                    {config.allowed_regions.map(r => (
                      <option key={r} value={r}>{r} — {config.transit_gateways[r]?.name}</option>
                    ))}
                  </select>
                </div>
                <button type="button" onClick={handleRegionContinue} disabled={!region} style={btn(COLORS.brand, !region)}>
                  Continue →
                </button>
              </div>
            </div>
          )}

          {/* ── Step 2: Customer Gateway ── */}
          {step === 'cgw' && (
            <div>
              {cgwPopupOpen && (
                <div style={{ ...CARD, padding: 18, marginBottom: 18, border: `1px solid ${COLORS.brand}40` }}>
                  <h3 style={{ margin: '0 0 6px', fontSize: 15, color: COLORS.textPrimary }}>
                    Existing Customer Gateways in {region}
                  </h3>
                  <div style={{ fontSize: 12, color: COLORS.textMuted, marginBottom: 12 }}>
                    The device may already have a Customer Gateway registered — reuse it below instead of creating a
                    duplicate, or create a new one.
                  </div>
                  {loadingCgws ? (
                    <div style={{ color: COLORS.textMuted, fontSize: 13 }}>Loading…</div>
                  ) : existingCgws.length === 0 ? (
                    <div style={{ color: COLORS.textMuted, fontSize: 13, marginBottom: 14 }}>
                      No existing Customer Gateways found in {region}.
                    </div>
                  ) : (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 14, maxHeight: 260, overflowY: 'auto' }}>
                      {existingCgws.map(cgw => (
                        <button
                          key={cgw.customer_gateway_id}
                          type="button"
                          onClick={() => pickExistingCgw(cgw)}
                          style={{
                            textAlign: 'left', padding: '10px 14px', borderRadius: 8,
                            border: `1px solid ${COLORS.border}`, background: '#fff', cursor: 'pointer',
                          }}
                        >
                          <div style={{ fontSize: 13.5, fontWeight: 700, color: COLORS.textPrimary }}>{cgw.name}</div>
                          <div style={{ fontSize: 11.5, color: COLORS.textMuted, ...MONO }}>
                            {cgw.customer_gateway_id} · {cgw.ip_address} · ASN {cgw.bgp_asn} · {cgw.state}
                          </div>
                        </button>
                      ))}
                    </div>
                  )}
                  <button type="button" onClick={startNewCgw} style={btn(COLORS.success, false)}>
                    ➕ Create a new Customer Gateway instead
                  </button>
                </div>
              )}

              {!cgwPopupOpen && selectedCgw && (
                <div style={{ ...CARD, padding: 18, marginBottom: 18, border: `1px solid ${COLORS.success}40`, background: COLORS.successBg }}>
                  <h3 style={{ margin: '0 0 8px', fontSize: 15, color: COLORS.textPrimary }}>Selected Customer Gateway</h3>
                  <div style={{ fontSize: 13, color: COLORS.textPrimary, marginBottom: 12 }}>
                    <b>{selectedCgw.name}</b> — <span style={MONO}>{selectedCgw.customer_gateway_id}</span>
                    {selectedCgw.ip_address && <> · {selectedCgw.ip_address}</>}
                    {selectedCgw.bgp_asn && <> · ASN {selectedCgw.bgp_asn}</>}
                  </div>
                  <div style={{ display: 'flex', gap: 10 }}>
                    <button type="button" onClick={confirmCgwAndContinue} style={btn(COLORS.brand, false)}>Continue →</button>
                    <button type="button" onClick={() => { setSelectedCgw(null); setCgwPopupOpen(true) }} style={btn('#94a3b8', false)}>
                      Choose a different one
                    </button>
                  </div>
                </div>
              )}

              {!cgwPopupOpen && !selectedCgw && (
                <form onSubmit={handleCreateCgw} style={{ ...CARD, padding: 18, border: `1px solid ${COLORS.brand}40` }}>
                  <h3 style={{ margin: '0 0 14px', fontSize: 15, color: COLORS.textPrimary }}>2. Create a new Customer Gateway in {region}</h3>
                  <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', marginBottom: 14 }}>
                    <div style={{ flex: '1 1 220px', minWidth: 200 }}>
                      <label style={LABEL}>Name tag</label>
                      <input value={cgwForm.name_tag} onChange={e => setCgwForm(f => ({ ...f, name_tag: e.target.value }))}
                        placeholder="e.g. Site-A-CGW" style={INPUT} />
                    </div>
                    <div style={{ flex: '1 1 200px', minWidth: 180 }}>
                      <label style={LABEL}>IP address</label>
                      <input value={cgwForm.ip_address} onChange={e => setCgwForm(f => ({ ...f, ip_address: e.target.value }))}
                        placeholder="e.g. 203.0.113.10" style={{ ...INPUT, ...MONO }} />
                    </div>
                    <div style={{ flex: '0 1 160px', minWidth: 140 }}>
                      <label style={LABEL}>BGP ASN</label>
                      <input value={cgwForm.bgp_asn} onChange={e => setCgwForm(f => ({ ...f, bgp_asn: e.target.value }))}
                        placeholder="65000" style={{ ...INPUT, ...MONO }} />
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'flex-end', borderTop: `1px solid ${COLORS.border}`, paddingTop: 14 }}>
                    <div style={{ flex: '1 1 260px', minWidth: 220 }}>
                      <label style={LABEL}>SR Number (required)</label>
                      <input value={cgwForm.sr_number} onChange={e => setCgwForm(f => ({ ...f, sr_number: e.target.value }))}
                        placeholder="e.g. SR-000123456" style={INPUT} />
                    </div>
                    <button type="submit" disabled={creatingCgw || !cgwForm.sr_number.trim() || !cgwForm.name_tag.trim() || !cgwForm.ip_address.trim()}
                      style={btn(COLORS.brand, creatingCgw || !cgwForm.sr_number.trim() || !cgwForm.name_tag.trim() || !cgwForm.ip_address.trim())}>
                      {creatingCgw ? 'Creating…' : 'Create Customer Gateway'}
                    </button>
                    <button type="button" onClick={() => setCgwPopupOpen(true)} style={btn('#94a3b8', false)}>← Back to list</button>
                  </div>
                </form>
              )}
            </div>
          )}

          {/* ── Step 3: VPN connection ── */}
          {step === 'vpn' && (
            <form onSubmit={handleCreateVpn} style={{ ...CARD, padding: 18, border: `1px solid ${COLORS.brand}40` }}>
              <h3 style={{ margin: '0 0 4px', fontSize: 15, color: COLORS.textPrimary }}>3. Site-to-Site VPN connection in {region}</h3>
              <div style={{ fontSize: 12, color: COLORS.textMuted, marginBottom: 14 }}>
                Customer Gateway: <b>{selectedCgw?.name}</b> (<span style={MONO}>{selectedCgw?.customer_gateway_id}</span>)
              </div>

              <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', marginBottom: 14 }}>
                <div style={{ flex: '1 1 260px', minWidth: 220 }}>
                  <label style={LABEL}>Name tag</label>
                  <input value={vpnForm.name_tag} onChange={e => setVpnForm(f => ({ ...f, name_tag: e.target.value }))}
                    placeholder="e.g. Site-A-VPN" style={INPUT} />
                </div>
                <div style={{ flex: '1 1 220px', minWidth: 200 }}>
                  <label style={LABEL}>Target gateway type</label>
                  <select value={vpnForm.target_gateway_type} onChange={e => setVpnForm(f => ({ ...f, target_gateway_type: e.target.value }))}
                    style={{ ...INPUT, background: '#fff' }}>
                    {config.target_gateway_types.map(t => <option key={t} value={t}>{t}</option>)}
                  </select>
                </div>
              </div>

              {vpnForm.target_gateway_type === 'Transit gateway' && tgw && (
                <div style={{ ...CARD, padding: 12, marginBottom: 14, background: '#f8fafc' }}>
                  <div style={{ fontSize: 12.5, color: COLORS.textPrimary }}>
                    Transit Gateway (fixed for {region}): <b>{tgw.name}</b> — <span style={MONO}>{tgw.id}</span>
                  </div>
                </div>
              )}

              {vpnForm.target_gateway_type === 'Virtual private gateway' && (
                <div style={{ marginBottom: 14 }}>
                  <label style={LABEL}>Virtual Private Gateway ID</label>
                  <input value={vpnForm.virtual_private_gateway_id}
                    onChange={e => setVpnForm(f => ({ ...f, virtual_private_gateway_id: e.target.value }))}
                    placeholder="e.g. vgw-0123456789abcdef0" style={{ ...INPUT, ...MONO, maxWidth: 320 }} />
                </div>
              )}

              <div style={{ marginBottom: 14 }}>
                <label style={LABEL}>Routing options</label>
                <div style={{ display: 'flex', gap: 18, fontSize: 13, color: COLORS.textPrimary }}>
                  <label style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                    <input type="radio" name="routing" checked={vpnForm.routing === 'dynamic'}
                      onChange={() => setVpnForm(f => ({ ...f, routing: 'dynamic' }))} />
                    Dynamic (requires BGP)
                  </label>
                  <label style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                    <input type="radio" name="routing" checked={vpnForm.routing === 'static'}
                      onChange={() => setVpnForm(f => ({ ...f, routing: 'static' }))} />
                    Static — Customer Gateway <span style={MONO}>{selectedCgw?.customer_gateway_id}</span>
                  </label>
                </div>
              </div>

              <div style={{ marginBottom: 14 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: COLORS.textSecondary, textTransform: 'uppercase', letterSpacing: 0.3, marginBottom: 8 }}>
                  Tunnel Configuration Standard (fixed, applies to both tunnels)
                </div>
                <div style={{ ...CARD, padding: 14, background: '#f8fafc', display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 8, fontSize: 12 }}>
                  <div>Phase 1 Encryption: <b>{tunnel.phase1_encryption}</b></div>
                  <div>Phase 2 Encryption: <b>{tunnel.phase2_encryption}</b></div>
                  <div>IKE Version: <b>{tunnel.ike_version}</b></div>
                  <div>Phase 1 Integrity: <b>{tunnel.phase1_integrity}</b></div>
                  <div>Phase 2 Integrity: <b>{tunnel.phase2_integrity}</b></div>
                  <div>Phase 1 DH Group: <b>{tunnel.phase1_dh_group}</b></div>
                  <div>Phase 2 DH Group: <b>{tunnel.phase2_dh_group}</b></div>
                  <div>DPD Timeout: <b>{tunnel.dpd_timeout_seconds}s</b></div>
                  <div>DPD Action: <b>{tunnel.dpd_action}</b></div>
                  <div>Phase 1 Lifetime: <b>{tunnel.phase1_lifetime_seconds}s</b></div>
                  <div>Phase 2 Lifetime: <b>{tunnel.phase2_lifetime_seconds}s</b></div>
                  <div>CloudWatch Logging: <b>{tunnel.cloudwatch_logging}</b></div>
                </div>
              </div>

              <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'flex-end', borderTop: `1px solid ${COLORS.border}`, paddingTop: 14 }}>
                <div style={{ flex: '1 1 260px', minWidth: 220 }}>
                  <label style={LABEL}>SR Number (required)</label>
                  <input value={vpnForm.sr_number} onChange={e => setVpnForm(f => ({ ...f, sr_number: e.target.value }))}
                    placeholder="e.g. SR-000123456" style={INPUT} />
                </div>
                <button type="submit" disabled={creatingVpn || !vpnForm.sr_number.trim() || !vpnForm.name_tag.trim()}
                  style={btn(COLORS.brand, creatingVpn || !vpnForm.sr_number.trim() || !vpnForm.name_tag.trim())}>
                  {creatingVpn ? 'Creating…' : 'Create VPN connection'}
                </button>
                <button type="button" onClick={() => setStep('cgw')} style={btn('#94a3b8', false)}>← Back</button>
              </div>
            </form>
          )}

          {/* ── Step 4: Route table ── */}
          {step === 'route' && (
            <div>
              {createdVpn && (
                <div style={{ ...CARD, padding: 14, marginBottom: 14, background: '#f8fafc', fontSize: 12.5 }}>
                  VPN connection created: <b>{createdVpn.name}</b> — <span style={MONO}>{createdVpn.vpn_connection_id}</span>
                  {' '}({createdVpn.state})
                </div>
              )}
              <form onSubmit={handleCreateRoute} style={{ ...CARD, padding: 18, border: `1px solid ${COLORS.brand}40` }}>
                <h3 style={{ margin: '0 0 4px', fontSize: 15, color: COLORS.textPrimary }}>4. Add a static route to the Transit Gateway route table</h3>
                <div style={{ fontSize: 12, color: COLORS.textMuted, marginBottom: 14 }}>
                  Route table (fixed for {region}): <b>{rt?.name}</b> — <span style={MONO}>{rt?.id}</span>
                </div>

                <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', marginBottom: 14 }}>
                  <div style={{ flex: '1 1 220px', minWidth: 200 }}>
                    <label style={LABEL}>Destination CIDR</label>
                    <input value={routeForm.cidr} onChange={e => setRouteForm(f => ({ ...f, cidr: e.target.value }))}
                      placeholder="e.g. 10.20.0.0/16" style={{ ...INPUT, ...MONO }} />
                  </div>
                  <div style={{ flex: '1 1 260px', minWidth: 220 }}>
                    <label style={LABEL}>Transit gateway attachment (VPN connection ID or attachment ID)</label>
                    <input value={routeForm.attachment_ref} onChange={e => setRouteForm(f => ({ ...f, attachment_ref: e.target.value }))}
                      placeholder="e.g. vpn-0ab9ab4ce4f1e66ed or tgw-attach-..." style={{ ...INPUT, ...MONO }} />
                  </div>
                </div>

                <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'flex-end', borderTop: `1px solid ${COLORS.border}`, paddingTop: 14 }}>
                  <div style={{ flex: '1 1 260px', minWidth: 220 }}>
                    <label style={LABEL}>SR Number (required)</label>
                    <input value={routeForm.sr_number} onChange={e => setRouteForm(f => ({ ...f, sr_number: e.target.value }))}
                      placeholder="e.g. SR-000123456" style={INPUT} />
                  </div>
                  <button type="submit" disabled={creatingRoute || !routeForm.sr_number.trim() || !routeForm.cidr.trim() || !routeForm.attachment_ref.trim()}
                    style={btn(COLORS.brand, creatingRoute || !routeForm.sr_number.trim() || !routeForm.cidr.trim() || !routeForm.attachment_ref.trim())}>
                    {creatingRoute ? 'Adding…' : 'Add route'}
                  </button>
                  <button type="button" onClick={() => setStep('vpn')} style={btn('#94a3b8', false)}>← Back</button>
                </div>
              </form>

              {createdRoute && (
                <div style={{ ...CARD, padding: 18, marginTop: 18, border: `1px solid ${COLORS.success}40`, background: COLORS.successBg }}>
                  <h3 style={{ margin: '0 0 8px', fontSize: 15, color: COLORS.textPrimary }}>🎉 VPN profile complete</h3>
                  <div style={{ fontSize: 13, color: COLORS.textPrimary, marginBottom: 12 }}>
                    Customer Gateway <span style={MONO}>{selectedCgw?.customer_gateway_id}</span> → VPN connection{' '}
                    <span style={MONO}>{createdVpn?.vpn_connection_id}</span> → route{' '}
                    <span style={MONO}>{createdRoute.destination_cidr_block}</span> on{' '}
                    <span style={MONO}>{createdRoute.route_table_id}</span>.
                  </div>
                  <button type="button" onClick={resetWizard} style={btn(COLORS.brand, false)}>Create another profile</button>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
