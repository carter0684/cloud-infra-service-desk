import React, { useState } from 'react'
import { api } from '../api.js'
import { COLORS, CARD, badgeStyle, InfoBanner } from '../utils/theme.js'

// ── VPN Profile Lookup (Resources) ───────────────────────────────────────
// Given a site's WAN IP (the public IP of its Customer Gateway device),
// looks up the matching AWS Site-to-Site VPN connection in the network
// account (100000000003) across all regions, and shows the full profile
// a network engineer needs to (re)configure that site's router/firewall:
// both tunnels' outside/inside IPs, IKE (phase 1) and IPsec (phase 2)
// parameters, Dead Peer Detection settings, firewall rules to open, and
// each tunnel's pre-shared key (masked by default — see PreSharedKeyRow
// below — since this is genuinely sensitive data unlike everything else
// in the Resources section).

const STATUS_COLOR = { UP: COLORS.success, DOWN: COLORS.danger }

function CopyButton({ value, small }) {
  const [copied, setCopied] = useState(false)
  if (!value) return null
  return (
    <button
      onClick={() => { navigator.clipboard?.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1500) }}
      title="Copy to clipboard"
      style={{
        padding: small ? '2px 7px' : '4px 10px', borderRadius: 5, border: `1px solid ${COLORS.border}`,
        background: '#fff', color: COLORS.textSecondary, fontSize: small ? 10.5 : 11.5, fontWeight: 600,
        cursor: 'pointer', flexShrink: 0,
      }}>
      {copied ? '✓ Copied' : '📋 Copy'}
    </button>
  )
}

function Field({ label, value, mono = true, copyable = false }) {
  if (value === null || value === undefined || value === '') return null
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, padding: '5px 0', borderBottom: `1px solid ${COLORS.border}` }}>
      <span style={{ fontSize: 12, color: COLORS.textSecondary, flexShrink: 0 }}>{label}</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
        <span style={{ fontSize: 12.5, color: COLORS.textPrimary, fontFamily: mono ? 'monospace' : 'inherit', textAlign: 'right', wordBreak: 'break-all' }}>
          {value}
        </span>
        {copyable && <CopyButton value={String(value)} small />}
      </span>
    </div>
  )
}

function PreSharedKeyRow({ value }) {
  const [revealed, setRevealed] = useState(false)
  if (!value) return null
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, padding: '5px 0', borderBottom: `1px solid ${COLORS.border}` }}>
      <span style={{ fontSize: 12, color: COLORS.textSecondary, flexShrink: 0 }}>Pre-Shared Key</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
        <span style={{ fontSize: 12.5, color: COLORS.textPrimary, fontFamily: 'monospace', textAlign: 'right', wordBreak: 'break-all' }}>
          {revealed ? value : '•'.repeat(Math.min(value.length, 24))}
        </span>
        <button onClick={() => setRevealed(r => !r)}
          style={{ padding: '2px 7px', borderRadius: 5, border: `1px solid ${COLORS.border}`, background: '#fff', color: COLORS.textSecondary, fontSize: 10.5, fontWeight: 600, cursor: 'pointer', flexShrink: 0 }}>
          {revealed ? '🙈 Hide' : '👁️ Reveal'}
        </button>
        {revealed && <CopyButton value={value} small />}
      </span>
    </div>
  )
}

function TunnelCard({ tunnel }) {
  const statusColor = STATUS_COLOR[tunnel.status] || COLORS.textMuted
  return (
    <div style={{ ...CARD, padding: 18 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <h4 style={{ margin: 0, fontSize: 14, color: COLORS.textPrimary }}>Tunnel {tunnel.tunnel_number}</h4>
        {tunnel.status && <span style={badgeStyle(statusColor)}>{tunnel.status}</span>}
      </div>

      <Field label="AWS Outside IP" value={tunnel.aws_outside_ip} copyable />
      <Field label="AWS Inside IP" value={tunnel.aws_inside_ip} copyable />
      <Field label="Local Outside IP" value={tunnel.local_outside_ip} copyable />
      <Field label="Local Inside IP" value={tunnel.local_inside_ip} copyable />
      <PreSharedKeyRow value={tunnel.pre_shared_key} />

      {tunnel.ike && (
        <>
          <div style={{ marginTop: 14, marginBottom: 4, fontSize: 11.5, fontWeight: 700, color: COLORS.textSecondary, textTransform: 'uppercase', letterSpacing: 0.3 }}>IKE (Phase 1)</div>
          <Field label="IKE Version" value={tunnel.ike.version} mono={false} />
          <Field label="Encryption" value={tunnel.ike.encryption} mono={false} />
          <Field label="Integrity" value={tunnel.ike.integrity} mono={false} />
          <Field label="DH Group" value={tunnel.ike.dh_group} mono={false} />
          <Field label="Lifetime" value={tunnel.ike.lifetime_seconds != null ? `${tunnel.ike.lifetime_seconds} seconds` : null} mono={false} />
          <Field label="Authentication" value={tunnel.ike.authentication} mono={false} />
        </>
      )}

      {tunnel.ipsec && (
        <>
          <div style={{ marginTop: 14, marginBottom: 4, fontSize: 11.5, fontWeight: 700, color: COLORS.textSecondary, textTransform: 'uppercase', letterSpacing: 0.3 }}>IPsec (Phase 2)</div>
          <Field label="Encryption" value={tunnel.ipsec.encryption} mono={false} />
          <Field label="Integrity" value={tunnel.ipsec.integrity} mono={false} />
          <Field label="DH Group (PFS)" value={tunnel.ipsec.dh_group} mono={false} />
          <Field label="Lifetime" value={tunnel.ipsec.lifetime_seconds != null ? `${tunnel.ipsec.lifetime_seconds} seconds` : null} mono={false} />
          <Field label="Mode" value={tunnel.ipsec.mode} mono={false} />
        </>
      )}

      {tunnel.dpd && (tunnel.dpd.timeout_seconds != null || tunnel.dpd.action) && (
        <>
          <div style={{ marginTop: 14, marginBottom: 4, fontSize: 11.5, fontWeight: 700, color: COLORS.textSecondary, textTransform: 'uppercase', letterSpacing: 0.3 }}>Dead Peer Detection</div>
          <Field label="Timeout" value={tunnel.dpd.timeout_seconds != null ? `${tunnel.dpd.timeout_seconds} seconds` : null} mono={false} />
          <Field label="Action" value={tunnel.dpd.action} mono={false} />
        </>
      )}
    </div>
  )
}

function ConnectionResult({ conn }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <div style={{ ...CARD, padding: '14px 18px', marginBottom: 14, display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: COLORS.textPrimary }}>
            {conn.name} <span style={{ fontFamily: 'monospace', fontWeight: 400, fontSize: 12.5, color: COLORS.textSecondary }}>({conn.vpn_connection_id})</span>
          </div>
          <div style={{ fontSize: 12, color: COLORS.textSecondary, marginTop: 2 }}>
            Region: <b>{conn.region}</b> · Customer Gateway: <span style={{ fontFamily: 'monospace' }}>{conn.customer_gateway_id}</span> · Routing: {conn.routing_type}
          </div>
        </div>
        {conn.state && <span style={{ ...badgeStyle(conn.state === 'available' ? COLORS.success : COLORS.warning), marginLeft: 'auto' }}>{conn.state}</span>}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 16, marginBottom: 14 }}>
        {(conn.tunnels || []).map(t => <TunnelCard key={t.tunnel_number} tunnel={t} />)}
      </div>

      {conn.firewall_rules && conn.firewall_rules.length > 0 && (
        <div style={{ ...CARD, padding: 18 }}>
          <h4 style={{ margin: '0 0 10px', fontSize: 13.5, color: COLORS.textPrimary }}>
            🔥 Firewall Rules <span style={{ fontWeight: 400, color: COLORS.textSecondary }}>(allow inbound/outbound on {conn.customer_gateway_ip})</span>
          </h4>
          <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12.5, color: COLORS.textPrimary, fontFamily: 'monospace', lineHeight: 1.8 }}>
            {conn.firewall_rules.map((rule, i) => <li key={i}>{rule}</li>)}
          </ul>
        </div>
      )}
    </div>
  )
}

export default function VpnLookupPage() {
  const [wanIp, setWanIp] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)

  async function handleSearch(e) {
    e.preventDefault()
    if (!wanIp.trim()) return
    setLoading(true)
    setError('')
    setResult(null)
    try {
      const res = await api.vpnLookupSearch(wanIp.trim())
      setResult(res)
    } catch (e) {
      setError(e?.response?.data?.detail || e?.message || 'Could not complete the VPN lookup.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ padding: 24, maxWidth: 1100 }}>
      <h2 style={{ margin: '0 0 4px', fontSize: 19, color: COLORS.textPrimary }}>🔐 VPN Profile Lookup</h2>
      <div style={{ fontSize: 13, color: COLORS.textSecondary, marginBottom: 14 }}>
        Enter a site's WAN IP (the public IP of its Customer Gateway device) to look up the matching
        AWS Site-to-Site VPN connection in the <b>network</b> account (100000000003) — tunnel IPs, IKE/IPsec
        parameters, Dead Peer Detection settings, firewall rules, and pre-shared keys.
      </div>
      <InfoBanner icon="🔒" text="Read-only view — data is fetched live from AWS and cannot be modified here." right="network · all regions" />

      <form onSubmit={handleSearch} style={{ ...CARD, padding: 18, marginBottom: 20, display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <label style={{ fontSize: 12, fontWeight: 600, color: COLORS.textSecondary, display: 'block', marginBottom: 5 }}>WAN IP (Customer Gateway)</label>
          <input
            value={wanIp} onChange={e => setWanIp(e.target.value)}
            placeholder="e.g. 203.0.113.18"
            style={{ width: '100%', padding: '9px 12px', borderRadius: 7, border: `1px solid ${COLORS.border}`, fontSize: 14, boxSizing: 'border-box', fontFamily: 'monospace' }}
          />
        </div>
        <button type="submit" disabled={loading || !wanIp.trim()}
          style={{
            padding: '9px 20px', borderRadius: 7, border: 'none', background: COLORS.brand, color: '#fff',
            fontSize: 13.5, fontWeight: 600, cursor: loading || !wanIp.trim() ? 'default' : 'pointer',
            opacity: loading || !wanIp.trim() ? 0.6 : 1,
          }}>
          {loading ? 'Searching…' : '🔍 Search'}
        </button>
      </form>

      {error && (
        <div style={{ ...CARD, padding: 14, marginBottom: 20, background: COLORS.dangerBg, border: `1px solid ${COLORS.danger}40`, color: COLORS.danger, fontSize: 13 }}>
          ⚠️ {error}
        </div>
      )}

      {result && (
        <>
          <div style={{ fontSize: 12, color: COLORS.textMuted, marginBottom: 14 }}>
            Scanned {result.regions_scanned?.length || 0} region{result.regions_scanned?.length === 1 ? '' : 's'} in
            {' '}<b>{result.account_name}</b> ({result.account_id})
            {result.is_mock && <span style={{ color: COLORS.warning, fontWeight: 600 }}> · mock data (local dev mode)</span>}
            {result.errors && result.errors.length > 0 && (
              <span style={{ color: COLORS.warning }}> · {result.errors.length} region{result.errors.length === 1 ? '' : 's'} could not be scanned</span>
            )}
          </div>

          {(!result.connections || result.connections.length === 0) && (
            <div style={{ ...CARD, padding: 24, textAlign: 'center', color: COLORS.textMuted, fontSize: 13.5 }}>
              No VPN connection found for WAN IP <b style={{ fontFamily: 'monospace' }}>{result.wan_ip}</b> in this account.
            </div>
          )}

          {result.connections?.map(conn => <ConnectionResult key={conn.vpn_connection_id} conn={conn} />)}
        </>
      )}
    </div>
  )
}
