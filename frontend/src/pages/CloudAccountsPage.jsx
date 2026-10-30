// Administration > Cloud Accounts
// Onboard, verify, rename and offboard the AWS accounts and Azure subscriptions
// the Portal reports on. Founding admins and the Management Team only (the server
// enforces it on every route; the check here only decides what to render). Spec:
// .kiro/specs/cloud-account-onboarding. All text coming back from the server is
// rendered as plain text.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useAuth } from '../auth/AuthContext.jsx'
import { api } from '../api.js'
import { Badge } from '../utils/theme.js'
import { formatDateTimeHK } from '../utils/time.js'

const RISK_PHRASE = 'I understand the risk and agree to offboard.'
const AWS_ID_RE = /^\d{12}$/
const GUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/
const NEW_BU = '__new__'

const PAGE = { maxWidth: 1240, margin: '24px auto', padding: '0 20px' }
const CARD = { background: '#fff', borderRadius: 12, padding: '22px 26px', boxShadow: '0 2px 10px rgba(0,0,0,0.08)' }
const TH = { textAlign: 'left', padding: '9px 10px', fontSize: 12, color: '#64748b', fontWeight: 600, borderBottom: '2px solid #e5e7eb', whiteSpace: 'nowrap' }
const TD = { padding: '9px 10px', fontSize: 13, borderBottom: '1px solid #f1f5f9', verticalAlign: 'top' }
const BTN = { padding: '5px 10px', borderRadius: 5, fontSize: 12, fontWeight: 600, cursor: 'pointer', border: 'none' }
const BTN_PRIMARY = { ...BTN, background: '#1a4080', color: '#fff' }
const BTN_PLAIN = { ...BTN, background: '#eef2f7', color: '#1e2a3a' }
const BTN_DANGER = { ...BTN, background: '#dc2626', color: '#fff' }
const INPUT = { padding: '8px 10px', border: '1px solid #cbd5e1', borderRadius: 6, fontSize: 14, width: '100%', boxSizing: 'border-box' }
const LABEL = { display: 'block', fontSize: 12, fontWeight: 600, color: '#475569', margin: '12px 0 4px' }

const STATUS_COLOR = { connected: '#16a34a', pending: '#d97706', failed: '#dc2626', offboarded: '#64748b' }
const PROVIDER_LABEL = { aws: 'AWS', azure: 'Azure' }
const MODE_LABEL = { legacy: 'Legacy (PortalReadOnlyRole)', infraops: 'InfraOps role', arm: 'Azure Reader' }

function errorText(e) {
  const d = e?.response?.data?.detail
  if (typeof d === 'string') return d
  if (Array.isArray(d)) return 'Please check the values entered.'
  return e?.message ? 'The request failed. Try again.' : 'Something went wrong.'
}

function Field({ id, label, children }) {
  return (
    <div>
      <label htmlFor={id} style={LABEL}>{label}</label>
      {children}
    </div>
  )
}

function Modal({ title, onClose, children, width = 640 }) {
  const ref = useRef(null)
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    ref.current?.focus()
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div
      style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.5)', zIndex: 1000, display: 'flex', alignItems: 'flex-start', justifyContent: 'center', overflowY: 'auto', padding: '40px 16px' }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={ref}
        style={{ ...CARD, width: '100%', maxWidth: width, outline: 'none' }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
          <h3 style={{ margin: 0, color: '#0f2e5a', fontSize: 18 }}>{title}</h3>
          <button type="button" onClick={onClose} aria-label="Close" style={{ ...BTN_PLAIN, fontSize: 14 }}>✕</button>
        </div>
        {children}
      </div>
    </div>
  )
}

function CopyBox({ label, value }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard not available: the value is selectable below */ }
  }
  return (
    <div>
      <span style={LABEL}>{label}</span>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <code style={{ flex: 1, background: '#f1f5f9', padding: '8px 10px', borderRadius: 6, fontSize: 12, wordBreak: 'break-all', userSelect: 'all' }}>{value}</code>
        <button type="button" style={BTN_PLAIN} onClick={copy} aria-label={`Copy ${label}`}>{copied ? 'Copied' : 'Copy'}</button>
      </div>
    </div>
  )
}

// Business unit picker: the known list plus "new business unit…".
function BusinessUnitField({ idPrefix, units, value, onChange, newValue, onNewChange }) {
  return (
    <div>
      <Field id={`${idPrefix}-bu`} label="Business unit">
        <select id={`${idPrefix}-bu`} style={INPUT} value={value} onChange={(e) => onChange(e.target.value)}>
          {units.map((u) => <option key={u} value={u}>{u}</option>)}
          <option value={NEW_BU}>+ New business unit…</option>
        </select>
      </Field>
      {value === NEW_BU && (
        <Field id={`${idPrefix}-newbu`} label="New business unit name">
          <input id={`${idPrefix}-newbu`} style={INPUT} maxLength={30} value={newValue} onChange={(e) => onNewChange(e.target.value)} />
        </Field>
      )}
    </div>
  )
}

function resolveBu(value, newValue) {
  if (value === NEW_BU) return { businessUnit: (newValue || '').trim(), confirmNew: true }
  return { businessUnit: value, confirmNew: false }
}

// ── Role permissions (AWS) ─────────────────────────────────────────────────

function PermissionsPanel({ permissions }) {
  const p = permissions
  if (!p) return null
  if (!p.ok && p.code === 'not_provisioned') {
    // Not an error: most accounts simply do not have this extra role.
    return <p role="status" style={{ color: '#64748b', fontSize: 13 }}>ℹ️ {p.message}</p>
  }
  if (!p.ok) {
    return <p role="alert" style={{ color: '#dc2626', fontSize: 13, fontWeight: 600 }}>❌ {p.message || 'The role could not be assumed.'}</p>
  }
  const id = p.identity || {}
  const sim = p.simulation || {}
  const writes = sim.write_allowed || []
  const reads = sim.data_read_allowed || []
  const readOnly = sim.available && writes.length === 0
  const SECTION = { fontSize: 12, fontWeight: 700, color: '#475569', margin: '14px 0 6px' }
  const CHIP = { display: 'inline-block', fontFamily: 'monospace', fontSize: 11, padding: '2px 6px', borderRadius: 4, margin: '0 4px 4px 0' }
  return (
    <div>
      <p role="status" style={{ fontSize: 13, fontWeight: 600, color: readOnly ? '#16a34a' : sim.available ? '#dc2626' : '#d97706' }}>
        {readOnly ? '✅' : sim.available ? '⚠️' : 'ℹ️'} {p.summary}
      </p>
      <p style={SECTION}>Identity</p>
      <p style={{ fontSize: 12, margin: 0, wordBreak: 'break-all' }}><code>{id.arn}</code></p>
      {id.max_session_duration ? (
        <p style={{ fontSize: 12, color: '#64748b', margin: '4px 0 0' }}>Maximum session: {Math.round(id.max_session_duration / 60)} minutes</p>
      ) : null}
      {id.permissions_boundary ? (
        <p style={{ fontSize: 12, color: '#64748b', margin: '4px 0 0', wordBreak: 'break-all' }}>Permissions boundary: <code>{id.permissions_boundary}</code></p>
      ) : null}

      <p style={SECTION}>Policies attached to the role</p>
      {p.policies_error && <p style={{ fontSize: 12, color: '#d97706' }}>{p.policies_error}</p>}
      {!p.policies_error && (p.managed_policies || []).length === 0 && (p.inline_policies || []).length === 0 && (
        <p style={{ fontSize: 12, color: '#64748b' }}>None.</p>
      )}
      {(p.managed_policies || []).map((m) => (
        <div key={m.arn} style={{ fontSize: 12, marginBottom: 4 }}>
          <strong>{m.name}</strong>{' '}
          <span style={{ ...CHIP, background: m.aws_managed ? '#e0f2fe' : '#fef3c7', color: '#334155', fontFamily: 'inherit' }}>
            {m.aws_managed ? 'AWS managed' : 'Customer managed'}
          </span>
          <div style={{ color: '#94a3b8', wordBreak: 'break-all' }}>{m.arn}</div>
        </div>
      ))}
      {(p.inline_policies || []).map((n) => (
        <div key={n} style={{ fontSize: 12, marginBottom: 4 }}>
          <strong>{n}</strong> <span style={{ ...CHIP, background: '#fef3c7', color: '#334155', fontFamily: 'inherit' }}>Inline</span>
        </div>
      ))}

      <p style={SECTION}>Write access check</p>
      {!sim.available && (
        <p style={{ fontSize: 12, color: '#d97706' }}>
          Not checked{sim.reason === 'direct_keys' ? ' (this account uses an access key, not a role).' : sim.reason ? ` (${sim.reason}).` : '.'}
        </p>
      )}
      {sim.available && writes.length === 0 && (
        <p style={{ fontSize: 12, color: '#16a34a' }}>
          None of the {sim.write_checked} write actions checked is allowed (nothing was run; this is a policy simulation).
        </p>
      )}
      {sim.available && writes.length > 0 && (
        <div>
          <p style={{ fontSize: 12, color: '#dc2626' }}>These write actions are allowed:</p>
          {writes.map((a) => <span key={a} style={{ ...CHIP, background: '#fee2e2', color: '#991b1b' }}>{a}</span>)}
        </div>
      )}
      {sim.available && reads.length > 0 && (
        <div>
          <p style={SECTION}>Data the role can read</p>
          <p style={{ fontSize: 12, color: '#64748b', marginTop: 0 }}>
            These read actions return data content, not only resource details.
          </p>
          {reads.map((a) => <span key={a} style={{ ...CHIP, background: '#f1f5f9', color: '#334155' }}>{a}</span>)}
        </div>
      )}
      {p.checked_at && <p style={{ fontSize: 11, color: '#94a3b8', marginTop: 14 }}>Checked {formatDateTimeHK(p.checked_at)}</p>}
    </div>
  )
}

// An AWS account can be reached through more than one role: the read-only role used for
// reporting (PortalReadOnlyRole) and, in some accounts, the JobPortal-CrossAccount-InfraOps-XXXX
// role used by the write-capable tools. The dialog shows every role found, each with its own
// policies and write-access check.
function PermissionsDialog({ account, onClose }) {
  const [roles, setRoles] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    let live = true
    api.cloudAccountsPermissions(account.provider, account.identifier)
      .then((d) => {
        if (!live) return
        setRoles(d.roles && d.roles.length
          ? d.roles
          : [{ key: 'primary', label: 'Role', role_name: null, purpose: '', permissions: d.permissions }])
      })
      .catch((e) => { if (live) setErr(errorText(e)) })
    return () => { live = false }
  }, [account])
  return (
    <Modal title={`Role permissions: ${account.display_name}`} onClose={onClose} width={760}>
      {err && <p role="alert" style={{ color: '#dc2626', fontSize: 13 }}>{err}</p>}
      {!roles && !err && <p style={{ fontSize: 13 }}>Assuming the roles and reading their policies…</p>}
      {roles && (
        <p style={{ fontSize: 12, color: '#64748b', margin: '0 0 8px' }}>
          Roles the Portal uses in this account ({roles.filter((r) => r.permissions?.ok).length} of {roles.length} found).
        </p>
      )}
      {(roles || []).map((r) => (
        <section key={r.key} aria-label={`${r.label} ${r.role_name || ''}`}
          style={{ border: '1px solid #e5e7eb', borderRadius: 8, padding: '10px 14px', marginBottom: 12 }}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'baseline' }}>
            <strong style={{ fontSize: 14 }}>{r.label}</strong>
            {r.role_name && <code style={{ fontSize: 12 }}>{r.role_name}</code>}
            {r.permissions && r.permissions.ok === false && r.permissions.code === 'not_provisioned' && (
              <span style={{ fontSize: 11, color: '#64748b', background: '#f1f5f9', borderRadius: 4, padding: '1px 6px' }}>not in this account</span>
            )}
          </div>
          {r.purpose && <p style={{ fontSize: 12, color: '#64748b', margin: '4px 0 0' }}>Used for: {r.purpose}</p>}
          <PermissionsPanel permissions={r.permissions} />
        </section>
      ))}
      <div style={{ marginTop: 16 }}><button type="button" style={BTN_PLAIN} onClick={onClose}>Close</button></div>
    </Modal>
  )
}

// ── Add account ────────────────────────────────────────────────────────────

function AwsAddTab({ units, initialAccountId, onDone }) {
  const [accountId, setAccountId] = useState(initialAccountId || '')
  const [setup, setSetup] = useState(null)
  const [name, setName] = useState('')
  const [bu, setBu] = useState('Other')
  const [newBu, setNewBu] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [result, setResult] = useState(null)
  const [downloaded, setDownloaded] = useState(false)
  const [perms, setPerms] = useState(null)
  const [permsBusy, setPermsBusy] = useState(false)
  const [permsErr, setPermsErr] = useState('')

  useEffect(() => {
    if (!initialAccountId) return
    let live = true
    api.cloudAccountsPrepareAws(initialAccountId)
      .then((s) => { if (live) setSetup(s) })
      .catch((e) => { if (live) setErr(errorText(e)) })
    return () => { live = false }
  }, [initialAccountId])

  const prepare = async () => {
    setBusy(true); setErr('')
    try { setSetup(await api.cloudAccountsPrepareAws(accountId.trim())) } catch (e) { setErr(errorText(e)) } finally { setBusy(false) }
  }

  const download = async () => {
    setErr('')
    try {
      const blob = await api.cloudAccountsTemplate(setup.account_id)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = setup.template_filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      setDownloaded(true)
    } catch (e) { setErr(errorText(e)) }
  }

  const showPerms = async () => {
    setPermsBusy(true); setPermsErr('')
    try { setPerms((await api.cloudAccountsPermissions('aws', setup.account_id)).permissions) } catch (e) { setPermsErr(errorText(e)) } finally { setPermsBusy(false) }
  }

  const connect = async () => {
    setBusy(true); setErr(''); setResult(null); setPerms(null); setPermsErr('')
    const { businessUnit, confirmNew } = resolveBu(bu, newBu)
    try {
      const r = await api.cloudAccountsConnectAws(setup.account_id, { displayName: name, businessUnit, confirmNewBusinessUnit: confirmNew })
      setResult(r)
      onDone()
    } catch (e) { setErr(errorText(e)) } finally { setBusy(false) }
  }

  if (!setup) {
    return (
      <div>
        <p style={{ fontSize: 13, color: '#475569' }}>
          Enter the 12-digit AWS account ID. The Portal will create a draft with its own External ID, which
          you then use to create a read-only role in that account.
        </p>
        <Field id="aws-id" label="AWS account ID">
          <input id="aws-id" style={INPUT} inputMode="numeric" maxLength={12} value={accountId} onChange={(e) => setAccountId(e.target.value.replace(/\D/g, ''))} />
        </Field>
        {err && <p role="alert" style={{ color: '#dc2626', fontSize: 13 }}>{err}</p>}
        <div style={{ marginTop: 14 }}>
          <button type="button" style={BTN_PRIMARY} disabled={busy || !AWS_ID_RE.test(accountId)} onClick={prepare}>
            {busy ? 'Preparing…' : 'Continue'}
          </button>
        </div>
      </div>
    )
  }

  const connected = result?.result?.ok
  return (
    <div>
      <ol style={{ fontSize: 13, color: '#334155', paddingLeft: 18, lineHeight: 1.6 }}>
        <li>Download the CloudFormation template below.</li>
        <li>
          Sign in to AWS account <strong>{setup.account_id}</strong> and create a stack from it
          (CloudFormation console: Create stack, Upload a template file; or
          {' '}<code>aws cloudformation deploy --template-file &lt;file&gt; --stack-name JobPortal-InfraOps-Role --capabilities CAPABILITY_NAMED_IAM</code>).
        </li>
        <li>Come back here, enter a display name and press Connect.</li>
      </ol>
      <CopyBox label="Role name" value={setup.role_name} />
      <CopyBox label="External ID (this account only)" value={setup.external_id} />
      <CopyBox label="Portal role allowed to assume it" value={setup.portal_role_arn} />
      <div style={{ marginTop: 12 }}>
        <button type="button" style={BTN_PRIMARY} onClick={download}>⬇ Download CloudFormation template</button>
        {downloaded && <span style={{ marginLeft: 10, fontSize: 12, color: '#16a34a' }}>Downloaded</span>}
      </div>
      <ul style={{ fontSize: 12, color: '#64748b', paddingLeft: 18, marginTop: 12, lineHeight: 1.5 }}>
        <li>The role is read-only (AWS managed ReadOnlyAccess) and has no write access. The account's own administrator can adjust it in their IAM console.</li>
        <li>ReadOnlyAccess can read data, not only resource details (for example S3 objects and DynamoDB items). It does not include reading secret values.</li>
        <li>AWS Health shows events only for accounts with a Business or Enterprise Support plan.</li>
        <li>If the Portal's own role is ever deleted and recreated, redeploy the stack in every onboarded account.</li>
      </ul>

      <Field id="aws-name" label="Display name">
        <input id="aws-name" style={INPUT} maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <BusinessUnitField idPrefix="aws" units={units} value={bu} onChange={setBu} newValue={newBu} onNewChange={setNewBu} />
      {err && <p role="alert" style={{ color: '#dc2626', fontSize: 13 }}>{err}</p>}
      {result && (
        <p role="status" style={{ fontSize: 13, color: connected ? '#16a34a' : '#dc2626', fontWeight: 600 }}>
          {connected ? '✅ Connected. The account now appears on the Portal pages.' : `❌ ${result.result.message}`}
        </p>
      )}
      {result?.warning && <p style={{ fontSize: 12, color: '#d97706' }}>{result.warning}</p>}
      <div style={{ marginTop: 14, display: 'flex', gap: 8 }}>
        <button type="button" style={BTN_PRIMARY} disabled={busy || !name.trim() || connected} onClick={connect}>
          {busy ? 'Connecting…' : result && !connected ? 'Try again' : 'Connect'}
        </button>
        {connected && !perms && (
          <button type="button" style={BTN_PLAIN} disabled={permsBusy} onClick={showPerms}>
            {permsBusy ? 'Checking…' : 'Show role permissions'}
          </button>
        )}
      </div>
      {permsErr && <p role="alert" style={{ color: '#dc2626', fontSize: 13 }}>{permsErr}</p>}
      {perms && <div style={{ marginTop: 12, borderTop: '1px solid #e5e7eb', paddingTop: 8 }}><PermissionsPanel permissions={perms} /></div>}
    </div>
  )
}

function AzureAddTab({ units, onDone }) {
  const [sub, setSub] = useState('')
  const [name, setName] = useState('')
  const [bu, setBu] = useState('Other')
  const [newBu, setNewBu] = useState('')
  const [guide, setGuide] = useState(null)
  const [guideErr, setGuideErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [result, setResult] = useState(null)

  const validSub = GUID_RE.test(sub.trim())
  useEffect(() => {
    let live = true
    setGuideErr('')
    api.cloudAccountsAzureGuide(validSub ? sub.trim() : undefined)
      .then((g) => { if (live) setGuide(g) })
      .catch((e) => { if (live) { setGuide(null); setGuideErr(errorText(e)) } })
    return () => { live = false }
  }, [sub, validSub])

  const connect = async () => {
    setBusy(true); setErr(''); setResult(null)
    const { businessUnit, confirmNew } = resolveBu(bu, newBu)
    try {
      const r = await api.cloudAccountsAddAzure({ subscriptionId: sub.trim(), displayName: name, businessUnit, confirmNewBusinessUnit: confirmNew })
      setResult(r)
      onDone()
    } catch (e) { setErr(errorText(e)) } finally { setBusy(false) }
  }

  const connected = result?.result?.ok
  return (
    <div>
      <p style={{ fontSize: 13, color: '#475569' }}>
        The Portal reads Azure with its existing read-only Service Principal, so there is no secret to enter.
        The subscription owner only has to give that Service Principal the <strong>Reader</strong> role on the subscription.
      </p>
      {guide && (
        <>
          <CopyBox label="Service Principal application (client) ID" value={guide.application_id} />
          <CopyBox label={validSub ? 'Command to run (Azure CLI)' : 'Command to run (replace <SUBSCRIPTION_ID>)'} value={guide.command} />
        </>
      )}
      {guideErr && <p style={{ fontSize: 12, color: '#d97706' }}>{guideErr}</p>}
      <Field id="az-sub" label="Subscription ID">
        <input id="az-sub" style={INPUT} value={sub} onChange={(e) => setSub(e.target.value)} placeholder="00000000-0000-0000-0000-000000000000" />
      </Field>
      <Field id="az-name" label="Display name">
        <input id="az-name" style={INPUT} maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <BusinessUnitField idPrefix="az" units={units} value={bu} onChange={setBu} newValue={newBu} onNewChange={setNewBu} />
      {err && <p role="alert" style={{ color: '#dc2626', fontSize: 13 }}>{err}</p>}
      {result && (
        <p role="status" style={{ fontSize: 13, color: connected ? '#16a34a' : '#dc2626', fontWeight: 600 }}>
          {connected ? '✅ Connected.' : `❌ ${result.result.message}`}
        </p>
      )}
      <div style={{ marginTop: 14 }}>
        <button type="button" style={BTN_PRIMARY} disabled={busy || !validSub || !name.trim() || connected} onClick={connect}>
          {busy ? 'Connecting…' : 'Connect'}
        </button>
        {result && !connected && <span style={{ fontSize: 12, color: '#64748b', marginLeft: 10 }}>After granting Reader, use Re-test on the account's row.</span>}
      </div>
    </div>
  )
}

function AddAccountDialog({ units, initialAwsId, onClose, onChanged }) {
  const [tab, setTab] = useState('aws')
  const tabBtn = (id, label, disabled) => (
    <button
      type="button" role="tab" aria-selected={tab === id} disabled={disabled}
      onClick={() => setTab(id)}
      style={{ ...BTN, padding: '8px 14px', background: tab === id ? '#1a4080' : '#eef2f7', color: tab === id ? '#fff' : disabled ? '#94a3b8' : '#1e2a3a', cursor: disabled ? 'not-allowed' : 'pointer' }}
    >
      {label}
    </button>
  )
  return (
    <Modal title={initialAwsId ? `Set up AWS account ${initialAwsId}` : 'Add cloud account'} onClose={onClose} width={720}>
      <div role="tablist" style={{ display: 'flex', gap: 6, margin: '10px 0 8px' }}>
        {tabBtn('aws', 'AWS')}
        {tabBtn('azure', 'Azure')}
        {tabBtn('aliyun', 'Aliyun · coming soon', true)}
      </div>
      {tab === 'aws' && <AwsAddTab units={units} initialAccountId={initialAwsId} onDone={onChanged} />}
      {tab === 'azure' && <AzureAddTab units={units} onDone={onChanged} />}
    </Modal>
  )
}

// ── Edit / offboard / history ──────────────────────────────────────────────

function EditDialog({ account, units, onClose, onSaved }) {
  const [name, setName] = useState(account.display_name)
  const [bu, setBu] = useState(units.includes(account.business_unit) ? account.business_unit : NEW_BU)
  const [newBu, setNewBu] = useState(units.includes(account.business_unit) ? '' : account.business_unit || '')
  const [core, setCore] = useState(!!account.scope_core)
  const [billing, setBilling] = useState(!!account.scope_billing)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [warning, setWarning] = useState('')

  const save = async () => {
    const { businessUnit, confirmNew } = resolveBu(bu, newBu)
    const changes = {}
    if (name.trim() !== account.display_name) changes.display_name = name
    if (businessUnit !== account.business_unit) { changes.business_unit = businessUnit; changes.confirm_new_business_unit = confirmNew }
    if (account.provider === 'aws') {
      if (core !== !!account.scope_core) changes.scope_core = core
      if (billing !== !!account.scope_billing) changes.scope_billing = billing
    }
    if (Object.keys(changes).length === 0) { onClose(); return }
    setBusy(true); setErr('')
    try {
      const r = await api.cloudAccountsUpdate(account.provider, account.identifier, changes)
      if (r.warning) { setWarning(r.warning) }
      onSaved(r.warning ? r.warning : '')
      if (!r.warning) onClose()
    } catch (e) { setErr(errorText(e)) } finally { setBusy(false) }
  }

  return (
    <Modal title={`Edit ${PROVIDER_LABEL[account.provider]} account`} onClose={onClose}>
      <p style={{ fontSize: 12, color: '#64748b' }}>
        {account.identifier}. These settings only change how the account appears in this Portal. Nothing is changed in {PROVIDER_LABEL[account.provider]}.
      </p>
      <Field id="ed-name" label="Display name">
        <input id="ed-name" style={INPUT} maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <BusinessUnitField idPrefix="ed" units={units} value={bu} onChange={setBu} newValue={newBu} onNewChange={setNewBu} />
      {account.provider === 'aws' && (
        <fieldset style={{ border: '1px solid #e5e7eb', borderRadius: 8, marginTop: 14, padding: '8px 12px' }}>
          <legend style={{ fontSize: 12, fontWeight: 600, color: '#475569' }}>Shown on these pages</legend>
          <label style={{ display: 'block', fontSize: 13, margin: '4px 0' }}>
            <input type="checkbox" checked={core} onChange={(e) => setCore(e.target.checked)} /> Cost Analysis, CMDB, AWS Health, VPN Lookup
          </label>
          <label style={{ display: 'block', fontSize: 13, margin: '4px 0' }}>
            <input type="checkbox" checked={billing} onChange={(e) => setBilling(e.target.checked)} /> Billing by Service, RI/SP Savings, Security Hub, WAF, AWS Resources
          </label>
        </fieldset>
      )}
      {err && <p role="alert" style={{ color: '#dc2626', fontSize: 13 }}>{err}</p>}
      {warning && <p role="status" style={{ color: '#d97706', fontSize: 13 }}>Saved. {warning}</p>}
      <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
        <button type="button" style={BTN_PRIMARY} disabled={busy || !name.trim() || (bu === NEW_BU && !newBu.trim())} onClick={save}>{busy ? 'Saving…' : 'Save'}</button>
        <button type="button" style={BTN_PLAIN} onClick={onClose}>{warning ? 'Close' : 'Cancel'}</button>
      </div>
    </Modal>
  )
}

function OffboardDialog({ account, onClose, onDone }) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const protectedAcct = !!account.protected
  const ready = !protectedAcct || typed === RISK_PHRASE

  const go = async () => {
    setBusy(true); setErr('')
    try {
      await api.cloudAccountsOffboard(account.provider, account.identifier, protectedAcct ? typed : undefined)
      onDone(`${account.display_name} was offboarded.`)
      onClose()
    } catch (e) { setErr(errorText(e)) } finally { setBusy(false) }
  }

  return (
    <Modal title={`Offboard ${account.display_name}?`} onClose={onClose}>
      <p style={{ fontSize: 13, color: '#334155' }}>
        <strong>{PROVIDER_LABEL[account.provider]} {account.identifier}</strong> will disappear from every Portal page and
        the Portal will stop calling it. Its history is kept and you can onboard it again later.
      </p>
      <p style={{ fontSize: 12, color: '#64748b' }}>
        Nothing is removed in the cloud account. If the InfraOps role or Reader assignment is no longer needed, remove it there yourself.
      </p>
      {protectedAcct && (
        <div style={{ background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 8, padding: '10px 12px', marginTop: 10 }}>
          <p style={{ margin: 0, fontSize: 13, color: '#991b1b', fontWeight: 600 }}>
            ⚠ Other Portal functions depend on this account (the Portal's own hosting account or the Core account).
          </p>
          <p style={{ margin: '8px 0 4px', fontSize: 12, color: '#7f1d1d' }}>To continue, type exactly:</p>
          <code style={{ display: 'block', fontSize: 12, userSelect: 'all', marginBottom: 6 }}>{RISK_PHRASE}</code>
          <label htmlFor="off-phrase" style={{ ...LABEL, marginTop: 4 }}>Confirmation</label>
          <input id="off-phrase" style={INPUT} autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} />
        </div>
      )}
      {err && <p role="alert" style={{ color: '#dc2626', fontSize: 13 }}>{err}</p>}
      <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
        <button type="button" style={BTN_DANGER} disabled={busy || !ready} onClick={go}>{busy ? 'Offboarding…' : 'Offboard'}</button>
        <button type="button" style={BTN_PLAIN} onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  )
}

function changeText(a) {
  const show = (o) => (o ? Object.entries(o).map(([k, v]) => `${k}: ${v === null ? '—' : String(v)}`).join(', ') : '')
  const parts = []
  if (a.before) parts.push(show(a.before))
  if (a.after) parts.push(`→ ${show(a.after)}`)
  if (a.note) parts.push(`(${a.note})`)
  return parts.join(' ')
}

function HistoryDialog({ account, onClose }) {
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    let live = true
    api.cloudAccountsDetail(account.provider, account.identifier)
      .then((d) => { if (live) setRows(d.audit || []) })
      .catch((e) => { if (live) setErr(errorText(e)) })
    return () => { live = false }
  }, [account])
  return (
    <Modal title={`History: ${account.display_name}`} onClose={onClose} width={760}>
      {err && <p role="alert" style={{ color: '#dc2626' }}>{err}</p>}
      {!rows && !err && <p style={{ fontSize: 13 }}>Loading…</p>}
      {rows && rows.length === 0 && <p style={{ fontSize: 13, color: '#64748b' }}>No changes recorded yet.</p>}
      {rows && rows.length > 0 && (
        <table data-cards style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead><tr><th style={TH}>When</th><th style={TH}>Who</th><th style={TH}>Action</th><th style={TH}>Details</th></tr></thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.sk}>
                <td style={TD}>{formatDateTimeHK(a.sk.split('#')[0])}</td>
                <td style={TD}>{a.actor}</td>
                <td style={TD}>{a.action}</td>
                <td style={{ ...TD, fontSize: 12, color: '#475569' }}>{changeText(a)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Modal>
  )
}

// ── Page ───────────────────────────────────────────────────────────────────

export default function CloudAccountsPage() {
  const { user } = useAuth()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [notice, setNotice] = useState('')
  const [search, setSearch] = useState('')
  const [provider, setProvider] = useState('all')
  const [status, setStatus] = useState('all')
  const [dialog, setDialog] = useState(null) // {type, account?, awsId?}
  const [busyKey, setBusyKey] = useState('')
  const [testingAll, setTestingAll] = useState(false)

  const load = useCallback(async () => {
    try {
      setData(await api.cloudAccountsList())
      setErr('')
    } catch (e) { setErr(errorText(e)) } finally { setLoading(false) }
  }, [])

  const allowed = !!user?.can_manage_cloud_accounts

  useEffect(() => { if (allowed) load() }, [allowed, load])

  const accounts = data?.accounts || []
  const units = data?.business_units || ['Other']

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase()
    return accounts.filter((a) =>
      (provider === 'all' || a.provider === provider) &&
      (status === 'all' || a.status === status) &&
      (!q || a.display_name.toLowerCase().includes(q) || a.identifier.toLowerCase().includes(q)))
  }, [accounts, search, provider, status])

  if (!allowed) {
    return (
      <div style={{ ...PAGE, maxWidth: 820 }}>
        <div style={CARD}>
          <h2 style={{ color: '#dc2626' }}>🚫 Access Denied</h2>
          <p style={{ color: '#64748b' }}>Cloud Accounts is open to founding admins and the Management Team only.</p>
        </div>
      </div>
    )
  }

  const act = async (key, fn, okMessage) => {
    setBusyKey(key); setNotice('')
    try {
      const r = await fn()
      const res = r?.result
      setNotice(res ? (res.ok ? `${okMessage}` : `Not connected: ${res.message}`) : okMessage)
      await load()
    } catch (e) { setNotice(errorText(e)) } finally { setBusyKey('') }
  }

  const testAll = async () => {
    setTestingAll(true); setNotice('')
    try {
      const r = await api.cloudAccountsTestAll()
      setNotice(`Tested ${r.tested} accounts: ${r.connected} connected, ${r.failed} failed.`)
      await load()
    } catch (e) { setNotice(errorText(e)) } finally { setTestingAll(false) }
  }

  const counts = data?.counts || {}
  const chip = (p) => {
    const c = counts[p]
    if (!c) return null
    return (
      <div key={p} style={{ background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 8, padding: '8px 12px', fontSize: 12 }}>
        <strong style={{ fontSize: 13 }}>{PROVIDER_LABEL[p]}</strong> · {c.total} total
        {['connected', 'pending', 'failed', 'offboarded'].map((s) => (
          <span key={s} style={{ marginLeft: 10, color: STATUS_COLOR[s] }}>{c[s] || 0} {s}</span>
        ))}
      </div>
    )
  }

  return (
    <div style={PAGE}>
      <div style={CARD}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12, flexWrap: 'wrap' }}>
          <div>
            <h2 style={{ color: '#0f2e5a', margin: 0 }}>☁️ Cloud Accounts</h2>
            <p style={{ color: '#64748b', fontSize: 13, margin: '4px 0 0', maxWidth: 720 }}>
              The AWS accounts and Azure subscriptions the Portal reports on. Add an account, check its connection, rename it,
              or offboard it. Renaming only changes how it appears here; nothing is changed in the cloud account.
            </p>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button type="button" style={BTN_PLAIN} disabled={testingAll} onClick={testAll}>{testingAll ? 'Testing…' : '🔄 Re-test all'}</button>
            <button type="button" style={BTN_PRIMARY} onClick={() => setDialog({ type: 'add' })}>＋ Add account</button>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', margin: '16px 0 10px' }}>{['aws', 'azure'].map(chip)}</div>

        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginBottom: 10 }}>
          <label htmlFor="ca-search" style={{ position: 'absolute', left: -9999 }}>Search accounts</label>
          <input id="ca-search" style={{ ...INPUT, width: 260 }} placeholder="Search name or ID" value={search} onChange={(e) => setSearch(e.target.value)} />
          <label htmlFor="ca-provider" style={{ position: 'absolute', left: -9999 }}>Provider</label>
          <select id="ca-provider" style={{ ...INPUT, width: 140 }} value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="all">All providers</option><option value="aws">AWS</option><option value="azure">Azure</option>
          </select>
          <label htmlFor="ca-status" style={{ position: 'absolute', left: -9999 }}>Status</label>
          <select id="ca-status" style={{ ...INPUT, width: 150 }} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="all">All statuses</option>
            {['connected', 'pending', 'failed', 'offboarded'].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <span style={{ fontSize: 12, color: '#64748b' }}>{shown.length} of {accounts.length} shown</span>
        </div>

        {notice && <p role="status" style={{ background: '#f0f9ff', border: '1px solid #bae6fd', borderRadius: 6, padding: '8px 12px', fontSize: 13 }}>{notice}</p>}
        {err && <p role="alert" style={{ color: '#dc2626', fontSize: 13 }}>{err}</p>}
        {loading && <p style={{ fontSize: 13 }}>Loading…</p>}

        {!loading && (
          <div style={{ overflowX: 'auto' }}>
            <table data-cards style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={TH}>Provider</th><th style={TH}>ID</th><th style={TH}>Name</th><th style={TH}>Business unit</th>
                  <th style={TH}>Status</th><th style={TH}>Access</th><th style={TH}>Last verified</th><th style={TH}>Shown on</th><th style={TH}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((a) => {
                  const key = a.account_key
                  const busy = busyKey === key
                  const draft = a.never_connected && !a.seeded
                  return (
                    <tr key={key} style={{ opacity: a.status === 'offboarded' ? 0.6 : 1 }}>
                      <td style={TD}>{PROVIDER_LABEL[a.provider]}</td>
                      <td style={{ ...TD, fontFamily: 'monospace', fontSize: 12 }}>{a.identifier}</td>
                      <td style={TD}>
                        {a.display_name}
                        {a.protected && <span title="Other Portal functions depend on this account" style={{ marginLeft: 6 }}>🔒</span>}
                      </td>
                      <td style={TD}>{a.business_unit}</td>
                      <td style={TD}>
                        <Badge text={a.status.toUpperCase()} color={STATUS_COLOR[a.status]} />
                        {a.last_error && <div style={{ fontSize: 11, color: '#991b1b', marginTop: 4, maxWidth: 260 }}>{a.last_error}</div>}
                        {draft && a.status !== 'offboarded' && <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>Draft: not connected yet</div>}
                      </td>
                      <td style={{ ...TD, fontSize: 12 }}>{MODE_LABEL[a.access_mode] || a.access_mode}</td>
                      <td style={{ ...TD, fontSize: 12 }}>{a.last_verified_at ? formatDateTimeHK(a.last_verified_at) : '—'}</td>
                      <td style={{ ...TD, fontSize: 11 }}>
                        {a.provider === 'aws'
                          ? [a.scope_core && 'Cost · CMDB · Health', a.scope_billing && 'Billing · Security · WAF'].filter(Boolean).join(' / ') || 'none'
                          : 'Azure Resources'}
                      </td>
                      <td style={{ ...TD, whiteSpace: 'nowrap' }}>
                        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                          {a.status !== 'offboarded' && (
                            <>
                              {draft && a.provider === 'aws' && (
                                <button type="button" style={BTN_PRIMARY} onClick={() => setDialog({ type: 'add', awsId: a.identifier })}>Set up</button>
                              )}
                              <button type="button" style={BTN_PLAIN} onClick={() => setDialog({ type: 'edit', account: a })}>Edit</button>
                              {a.provider === 'aws' && !draft && (
                                <button type="button" style={BTN_PLAIN} onClick={() => setDialog({ type: 'permissions', account: a })}>Permissions</button>
                              )}
                              <button type="button" style={BTN_PLAIN} disabled={busy}
                                onClick={() => act(key, () => api.cloudAccountsTest(a.provider, a.identifier), `${a.display_name} is connected.`)}>
                                {busy ? 'Testing…' : 'Re-test'}
                              </button>
                              {draft
                                ? <button type="button" style={BTN_PLAIN} disabled={busy}
                                    onClick={() => { if (window.confirm(`Discard the draft for ${a.identifier}?`)) act(key, () => api.cloudAccountsDiscard(a.provider, a.identifier), 'Draft discarded.') }}>Discard</button>
                                : <button type="button" style={BTN_DANGER} onClick={() => setDialog({ type: 'offboard', account: a })}>Offboard</button>}
                            </>
                          )}
                          {a.status === 'offboarded' && (
                            <button type="button" style={BTN_PRIMARY} disabled={busy}
                              onClick={() => act(key, () => api.cloudAccountsOnboard(a.provider, a.identifier), `${a.display_name} is onboarded again.`)}>
                              {busy ? 'Testing…' : 'Onboard again'}
                            </button>
                          )}
                          <button type="button" style={BTN_PLAIN} onClick={() => setDialog({ type: 'history', account: a })}>History</button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
                {shown.length === 0 && (
                  <tr><td colSpan={9} style={{ ...TD, textAlign: 'center', color: '#64748b' }}>No accounts match.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {dialog?.type === 'add' && (
        <AddAccountDialog units={units} initialAwsId={dialog.awsId} onClose={() => setDialog(null)} onChanged={load} />
      )}
      {dialog?.type === 'edit' && (
        <EditDialog account={dialog.account} units={units} onClose={() => setDialog(null)} onSaved={() => load()} />
      )}
      {dialog?.type === 'offboard' && (
        <OffboardDialog account={dialog.account} onClose={() => setDialog(null)} onDone={(m) => { setNotice(m); load() }} />
      )}
      {dialog?.type === 'history' && <HistoryDialog account={dialog.account} onClose={() => setDialog(null)} />}
      {dialog?.type === 'permissions' && <PermissionsDialog account={dialog.account} onClose={() => setDialog(null)} />}
    </div>
  )
}
