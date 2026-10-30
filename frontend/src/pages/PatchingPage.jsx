import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { api } from '../api.js'
import {
  COLORS, CARD, Badge, InfoBanner, SkeletonLines, TAB_BAR, tabButtonStyle,
} from '../utils/theme.js'

// ── Lifecycle > Patching > OS ─────────────────────────────────────────────
// Plan which EC2 instances are in scope for OS patching and when (UTC+8),
// with the Freshservice Change number, the AMS SR number and the MS Teams
// link for the patching window. See backend/app/patching.py.
//
//   * Schedules are filed under GROUPS (a group / task name) that the user
//     creates and picks - nothing is grouped automatically by date or time.
//   * The MS Teams links are maintained by the teams themselves (name + URL).
//   * Everyone can read this page. Only admins and the Cloud Infra / Security /
//     Management Teams (or users individually granted write) see the editing
//     controls. The backend reports that as `can_edit` and enforces it again on
//     every write route.
//
// The instance list is the same inventory as AWS Resources > EC2 Instances.

const SUB_TABS = [
  { id: 'os', label: 'OS' },
]

const STATUS_META = {
  planned: { label: 'Planned', color: '#2563eb' },
  completed: { label: 'Completed', color: COLORS.success },
  cancelled: { label: 'Cancelled', color: COLORS.textSecondary },
}

const MAX_ROWS_SHOWN = 300

const input = {
  padding: '7px 10px', border: `1px solid ${COLORS.border}`, borderRadius: 6, fontSize: 13,
  color: COLORS.textPrimary, background: '#fff',
}
const th = { textAlign: 'left', padding: '6px 8px', fontWeight: 600, color: COLORS.textSecondary, whiteSpace: 'nowrap' }
const td = { padding: '6px 8px', color: COLORS.textPrimary, whiteSpace: 'nowrap' }
const linkBtn = { background: 'none', border: 'none', padding: 0, color: COLORS.brand, fontWeight: 600, cursor: 'pointer', fontSize: 12.5 }
const smallBtn = { padding: '4px 12px', borderRadius: 5, border: `1px solid ${COLORS.border}`, background: '#fff', cursor: 'pointer', fontSize: 12, fontWeight: 600 }
const dangerBtn = { ...smallBtn, color: COLORS.danger, borderColor: `${COLORS.danger}60`, background: COLORS.dangerBg }
const primaryBtn = { padding: '6px 16px', borderRadius: 6, border: 'none', background: COLORS.brand, color: '#fff', fontWeight: 600, fontSize: 13, cursor: 'pointer' }

function todayHkt() {
  // en-CA formats as YYYY-MM-DD
  return new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Hong_Kong' })
}

function errorMessage(e, fallback) {
  const status = e?.response?.status
  if (status === 403) return 'You do not have permission to change patching data.'
  return e?.response?.data?.detail || fallback
}

function instanceKey(i) {
  return `${i.account_id}|${i.instance_id}`
}

function formatWhen(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString('en-HK', { timeZone: 'Asia/Hong_Kong', hour12: false }) + ' HKT'
}

function Field({ label, children, hint, style }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 12, color: COLORS.textSecondary, ...style }}>
      {label}
      {children}
      {hint && <span style={{ fontSize: 11, color: COLORS.textMuted }}>{hint}</span>}
    </label>
  )
}

function Modal({ label, onClose, busy, width = 'min(980px, 100%)', children }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !busy) onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, busy])
  return (
    <div role="dialog" aria-modal="true" aria-label={label}
      style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.6)', zIndex: 1000, display: 'flex', alignItems: 'flex-start', justifyContent: 'center', padding: 24, overflow: 'auto' }}>
      <div style={{ ...CARD, width, padding: 18 }}>{children}</div>
    </div>
  )
}

function ModalHeader({ title, onClose, busy }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
      <h3 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 17 }}>{title}</h3>
      <button type="button" onClick={onClose} disabled={busy} aria-label="Close"
        style={{ border: `1px solid ${COLORS.border}`, background: '#fff', borderRadius: 6, padding: '4px 10px', cursor: 'pointer' }}>✕</button>
    </div>
  )
}

// ── Small "name" dialog (create / rename a group) ────────────────────────

function NameDialog({ title, label, initial, submitLabel, onSubmit, onClose }) {
  const [name, setName] = useState(initial || '')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      await onSubmit(name.trim())
    } catch (ex) {
      setErr(errorMessage(ex, 'Could not save'))
      setBusy(false)
    }
  }
  return (
    <Modal label={title} onClose={onClose} busy={busy} width="min(460px, 100%)">
      <form onSubmit={submit}>
        <ModalHeader title={title} onClose={onClose} busy={busy} />
        <Field label={label}>
          <input value={name} onChange={e => setName(e.target.value)} maxLength={60} required autoFocus
            placeholder="e.g. Nov 2026 monthly patching" style={input} />
        </Field>
        {err && <div role="alert" style={{ color: COLORS.danger, fontSize: 13, marginTop: 10 }}>{err}</div>}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 14 }}>
          <button type="button" onClick={onClose} disabled={busy} style={smallBtn}>Cancel</button>
          <button type="submit" disabled={busy || !name.trim()} style={{ ...primaryBtn, opacity: busy ? 0.7 : 1 }}>
            {busy ? 'Saving…' : submitLabel}
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ── Teams links manager ──────────────────────────────────────────────────

function LinkRow({ link, canEdit, onChanged }) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(link.name)
  const [url, setUrl] = useState(link.url)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const save = async (e) => {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      await api.updatePatchingOsMeeting(link.id, name.trim(), url.trim())
      setEditing(false)
      onChanged()
    } catch (ex) {
      setErr(errorMessage(ex, 'Could not update the link'))
    } finally {
      setBusy(false)
    }
  }
  const remove = async () => {
    if (!window.confirm(`Delete the Teams link "${link.name}"?`)) return
    setErr('')
    setBusy(true)
    try {
      await api.deletePatchingOsMeeting(link.id)
      onChanged()
    } catch (ex) {
      setErr(errorMessage(ex, 'Could not delete the link'))
      setBusy(false)
    }
  }

  if (editing) {
    return (
      <form onSubmit={save} style={{ borderTop: `1px solid ${COLORS.border}`, padding: '10px 0', display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <Field label="Name" style={{ flex: '0 1 200px' }}>
            <input value={name} onChange={e => setName(e.target.value)} maxLength={60} required style={input} />
          </Field>
          <Field label="Teams meeting URL" style={{ flex: '1 1 320px' }}>
            <input value={url} onChange={e => setUrl(e.target.value)} maxLength={2000} required style={input} />
          </Field>
        </div>
        {err && <div role="alert" style={{ color: COLORS.danger, fontSize: 12.5 }}>{err}</div>}
        <div style={{ display: 'flex', gap: 8 }}>
          <button type="submit" disabled={busy} style={{ ...primaryBtn, padding: '4px 14px', fontSize: 12.5 }}>{busy ? 'Saving…' : 'Save'}</button>
          <button type="button" disabled={busy} style={smallBtn}
            onClick={() => { setEditing(false); setName(link.name); setUrl(link.url); setErr('') }}>Cancel</button>
        </div>
      </form>
    )
  }
  return (
    <div style={{ borderTop: `1px solid ${COLORS.border}`, padding: '10px 0' }}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <div style={{ fontWeight: 600, color: COLORS.textPrimary, minWidth: 140 }}>{link.name}</div>
        <a href={link.url} target="_blank" rel="noopener noreferrer" style={{ color: COLORS.brand, fontWeight: 600, fontSize: 12.5 }}>Open ↗</a>
        <span title={link.url} style={{ color: COLORS.textMuted, fontSize: 11.5, flex: 1, minWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {link.url}
        </span>
        {canEdit && (
          <>
            <button type="button" onClick={() => setEditing(true)} disabled={busy} style={smallBtn}>Edit</button>
            <button type="button" onClick={remove} disabled={busy} style={dangerBtn}>Delete</button>
          </>
        )}
      </div>
      {err && <div role="alert" style={{ color: COLORS.danger, fontSize: 12.5, marginTop: 6 }}>{err}</div>}
    </div>
  )
}

function LinksManager({ links, canEdit, onChanged, onClose }) {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const add = async (e) => {
    e.preventDefault()
    setErr('')
    setBusy(true)
    try {
      await api.createPatchingOsMeeting(name.trim(), url.trim())
      setName('')
      setUrl('')
      onChanged()
    } catch (ex) {
      setErr(errorMessage(ex, 'Could not add the link'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal label="MS Teams links" onClose={onClose} width="min(820px, 100%)">
      <ModalHeader title="MS Teams meeting links" onClose={onClose} />
      <div style={{ color: COLORS.textSecondary, fontSize: 12.5, marginBottom: 10 }}>
        {canEdit
          ? 'Add, rename or change the URL of a link. Schedules that use a link follow any change automatically. A link that a schedule still uses cannot be deleted.'
          : 'These are the Teams links that can be attached to a patching schedule.'}
      </div>

      {canEdit && (
        <form onSubmit={add} style={{ ...CARD, padding: 12, marginBottom: 10 }}>
          <div style={{ fontWeight: 600, fontSize: 13, color: COLORS.textPrimary, marginBottom: 8 }}>Add a link</div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <Field label="Name" style={{ flex: '0 1 200px' }}>
              <input value={name} onChange={e => setName(e.target.value)} maxLength={60} required placeholder="e.g. Project Falcon" style={input} />
            </Field>
            <Field label="Teams meeting URL" style={{ flex: '1 1 320px' }}>
              <input value={url} onChange={e => setUrl(e.target.value)} maxLength={2000} required placeholder="https://teams.microsoft.com/l/meetup-join/…" style={input} />
            </Field>
            <button type="submit" disabled={busy} style={{ ...primaryBtn, opacity: busy ? 0.7 : 1 }}>{busy ? 'Adding…' : 'Add link'}</button>
          </div>
          {err && <div role="alert" style={{ color: COLORS.danger, fontSize: 12.5, marginTop: 8 }}>{err}</div>}
          <div style={{ color: COLORS.textMuted, fontSize: 11.5, marginTop: 8 }}>Only https links on Microsoft Teams (teams.microsoft.com) are accepted.</div>
        </form>
      )}

      {links.length === 0 && <div style={{ color: COLORS.textMuted, padding: 12 }}>No Teams links yet.</div>}
      {links.map(l => <LinkRow key={`${l.id}-${l.updated_at}`} link={l} canEdit={canEdit} onChanged={onChanged} />)}
    </Modal>
  )
}

// ── Instance picker ──────────────────────────────────────────────────────

function InstancePicker({ instances, selected, onChange, loading, error, onReload }) {
  const [search, setSearch] = useState('')
  const [account, setAccount] = useState('')
  const [region, setRegion] = useState('')
  const [state, setState] = useState('')

  const accounts = useMemo(() => {
    const m = new Map()
    instances.forEach(i => m.set(i.account_id, i.account_name))
    return Array.from(m, ([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name))
  }, [instances])
  const regions = useMemo(() => Array.from(new Set(instances.map(i => i.region))).sort(), [instances])
  const states = useMemo(() => Array.from(new Set(instances.map(i => i.state))).filter(Boolean).sort(), [instances])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return instances.filter(i => {
      if (account && i.account_id !== account) return false
      if (region && i.region !== region) return false
      if (state && i.state !== state) return false
      if (!q) return true
      return [i.name, i.instance_id, i.private_ip, i.account_name, i.instance_type]
        .some(v => String(v || '').toLowerCase().includes(q))
    })
  }, [instances, search, account, region, state])

  const shown = filtered.slice(0, MAX_ROWS_SHOWN)

  const toggle = (i) => {
    const next = new Set(selected)
    const k = instanceKey(i)
    if (next.has(k)) next.delete(k); else next.add(k)
    onChange(next)
  }
  const selectShown = () => {
    const next = new Set(selected)
    filtered.forEach(i => next.add(instanceKey(i)))
    onChange(next)
  }
  const clearShown = () => {
    const next = new Set(selected)
    filtered.forEach(i => next.delete(instanceKey(i)))
    onChange(next)
  }

  return (
    <div style={{ border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: 10 }}>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 8 }}>
        <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search name, ID, IP…"
          aria-label="Search instances" style={{ ...input, flex: '1 1 200px' }} />
        <select value={account} onChange={e => setAccount(e.target.value)} aria-label="Filter by account" style={input}>
          <option value="">All accounts</option>
          {accounts.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <select value={region} onChange={e => setRegion(e.target.value)} aria-label="Filter by region" style={input}>
          <option value="">All regions</option>
          {regions.map(r => <option key={r} value={r}>{r}</option>)}
        </select>
        <select value={state} onChange={e => setState(e.target.value)} aria-label="Filter by state" style={input}>
          <option value="">All states</option>
          {states.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 8, fontSize: 12.5 }}>
        <strong style={{ color: COLORS.textPrimary }}>{selected.size} selected</strong>
        <span style={{ color: COLORS.textMuted }}>· {filtered.length} match the filters</span>
        <button type="button" onClick={selectShown} disabled={!filtered.length} style={linkBtn}>Select all matching</button>
        <button type="button" onClick={clearShown} disabled={!filtered.length} style={linkBtn}>Clear matching</button>
        <button type="button" onClick={() => onChange(new Set())} disabled={!selected.size} style={linkBtn}>Clear all</button>
        <button type="button" onClick={onReload} style={{ ...linkBtn, marginLeft: 'auto' }}>↻ Reload inventory</button>
      </div>

      {loading && <SkeletonLines lines={4} />}
      {error && <div role="alert" style={{ color: COLORS.danger, fontSize: 12.5 }}>{error}</div>}

      {!loading && !error && (
        <div style={{ maxHeight: 300, overflow: 'auto', border: `1px solid ${COLORS.border}`, borderRadius: 6 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
            <thead style={{ position: 'sticky', top: 0, background: COLORS.pageBg }}>
              <tr>
                <th style={th} scope="col"><span style={{ position: 'absolute', left: -9999 }}>Select</span></th>
                {['Name', 'Instance ID', 'Account', 'Region', 'Private IP', 'State', 'Type'].map(h => (
                  <th key={h} style={th} scope="col">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map(i => {
                const on = selected.has(instanceKey(i))
                return (
                  <tr key={instanceKey(i)} onClick={() => toggle(i)}
                    style={{ cursor: 'pointer', background: on ? COLORS.brandLight : 'transparent', borderTop: `1px solid ${COLORS.border}` }}>
                    <td style={td}>
                      <input type="checkbox" checked={on} onChange={() => toggle(i)} onClick={e => e.stopPropagation()}
                        aria-label={`Select ${i.name || i.instance_id}`} />
                    </td>
                    <td style={{ ...td, fontWeight: 600 }}>{i.name || <span style={{ color: COLORS.textMuted }}>—</span>}</td>
                    <td style={{ ...td, fontFamily: 'monospace' }}>{i.instance_id}</td>
                    <td style={td}>{i.account_name}</td>
                    <td style={td}>{i.region}</td>
                    <td style={{ ...td, fontFamily: 'monospace' }}>{i.private_ip}</td>
                    <td style={td}>{i.state}</td>
                    <td style={td}>{i.instance_type}</td>
                  </tr>
                )
              })}
              {!shown.length && (
                <tr><td colSpan={8} style={{ ...td, color: COLORS.textMuted, textAlign: 'center', padding: 16 }}>No instances match.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      {filtered.length > MAX_ROWS_SHOWN && (
        <div style={{ color: COLORS.textMuted, fontSize: 11.5, marginTop: 6 }}>
          Showing the first {MAX_ROWS_SHOWN} of {filtered.length}. Narrow the filters to see the rest (“Select all matching” still applies to all {filtered.length}).
        </div>
      )}
    </div>
  )
}

// ── Schedule editor ──────────────────────────────────────────────────────

function PlanEditor({ plan, presetGroupId, groups: initialGroups, meetings, onClose, onSaved }) {
  const editing = !!plan
  const [groups, setGroups] = useState(initialGroups)
  const [groupId, setGroupId] = useState(plan?.group_id || presetGroupId || '')
  const [newGroup, setNewGroup] = useState(null) // null = closed, string = typing
  const [creatingGroup, setCreatingGroup] = useState(false)
  const [groupErr, setGroupErr] = useState('')
  const [title, setTitle] = useState(plan?.title || '')
  const [date, setDate] = useState(plan?.schedule_date || '')
  const [time, setTime] = useState(plan?.schedule_time || '22:00')
  const [meeting, setMeeting] = useState(plan?.teams_meeting || '')
  const [change, setChange] = useState(plan?.freshservice_change || '')
  const [sr, setSr] = useState(plan?.ams_sr || '')
  const [notes, setNotes] = useState(plan?.notes || '')
  const [status, setStatus] = useState(plan?.status || 'planned')
  const [selected, setSelected] = useState(() => new Set((plan?.instances || []).map(instanceKey)))
  const [instances, setInstances] = useState([])
  const [loadingInv, setLoadingInv] = useState(true)
  const [invErr, setInvErr] = useState('')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState('')
  const [dropped, setDropped] = useState([])

  const loadInventory = useCallback((refresh = false) => {
    setLoadingInv(true)
    setInvErr('')
    api.getPatchingOsInstances(refresh)
      .then(d => {
        setInstances(d.instances || [])
        if (!refresh) {
          // Instances saved in the plan that vanished from the inventory (e.g.
          // terminated) can't be re-saved; drop them and say so.
          const known = new Set((d.instances || []).map(instanceKey))
          const missing = (plan?.instances || []).filter(i => !known.has(instanceKey(i)))
          if (missing.length) {
            setDropped(missing)
            setSelected(prev => {
              const next = new Set(prev)
              missing.forEach(i => next.delete(instanceKey(i)))
              return next
            })
          }
        }
      })
      .catch(e => setInvErr(errorMessage(e, 'Could not load the EC2 inventory')))
      .finally(() => setLoadingInv(false))
  }, [plan])

  useEffect(() => { loadInventory(false) }, [loadInventory])

  const createGroup = async () => {
    setGroupErr('')
    setCreatingGroup(true)
    try {
      const g = await api.createPatchingOsGroup((newGroup || '').trim())
      setGroups(prev => [...prev, g].sort((a, b) => a.name.localeCompare(b.name)))
      setGroupId(g.id)
      setNewGroup(null)
    } catch (ex) {
      setGroupErr(errorMessage(ex, 'Could not create the group'))
    } finally {
      setCreatingGroup(false)
    }
  }

  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    if (!selected.size) { setErr('Select at least one EC2 instance'); return }
    const body = {
      title: title.trim(),
      group_id: groupId || null,
      schedule_date: date,
      schedule_time: time,
      teams_meeting: meeting || null,
      freshservice_change: change.trim(),
      ams_sr: sr.trim(),
      notes: notes.trim(),
      status,
      instances: Array.from(selected).map(k => {
        const [account_id, instance_id] = k.split('|')
        return { account_id, instance_id }
      }),
    }
    setSaving(true)
    try {
      if (editing) await api.updatePatchingOsPlan(plan.id, body)
      else await api.createPatchingOsPlan(body)
      onSaved()
    } catch (ex) {
      setErr(errorMessage(ex, 'Could not save the patching schedule'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal label={editing ? 'Edit patching schedule' : 'New patching schedule'} onClose={onClose} busy={saving}>
      <form onSubmit={submit}>
        <ModalHeader title={editing ? 'Edit patching schedule' : 'New patching schedule'} onClose={onClose} busy={saving} />

        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 12, alignItems: 'flex-end' }}>
          <Field label="Group / task name" style={{ flex: '1 1 260px' }}>
            <select value={groupId} onChange={e => setGroupId(e.target.value)} style={input}>
              <option value="">No group</option>
              {groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}
            </select>
          </Field>
          {newGroup === null ? (
            <button type="button" onClick={() => { setNewGroup(''); setGroupErr('') }} style={smallBtn}>+ New group</button>
          ) : (
            <div style={{ display: 'flex', gap: 6, alignItems: 'flex-end' }}>
              <Field label="New group name">
                <input value={newGroup} onChange={e => setNewGroup(e.target.value)} maxLength={60} autoFocus
                  placeholder="e.g. Nov 2026 monthly patching" style={input}
                  onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); if (newGroup.trim()) createGroup() } }} />
              </Field>
              <button type="button" onClick={createGroup} disabled={creatingGroup || !newGroup.trim()} style={primaryBtn}>
                {creatingGroup ? 'Creating…' : 'Create'}
              </button>
              <button type="button" onClick={() => setNewGroup(null)} disabled={creatingGroup} style={smallBtn}>Cancel</button>
            </div>
          )}
        </div>
        {groupErr && <div role="alert" style={{ color: COLORS.danger, fontSize: 12.5, marginBottom: 10 }}>{groupErr}</div>}

        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 12 }}>
          <Field label="Title (optional)" style={{ flex: '1 1 260px' }}>
            <input value={title} onChange={e => setTitle(e.target.value)} maxLength={80} placeholder="e.g. Web servers" style={input} />
          </Field>
          <Field label="Date (UTC+8)">
            <input type="date" value={date} onChange={e => setDate(e.target.value)} required style={input} />
          </Field>
          <Field label="Time (UTC+8)">
            <input type="time" value={time} onChange={e => setTime(e.target.value)} required style={input} />
          </Field>
          {editing && (
            <Field label="Status">
              <select value={status} onChange={e => setStatus(e.target.value)} style={input}>
                {Object.entries(STATUS_META).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
              </select>
            </Field>
          )}
        </div>

        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 12 }}>
          <Field label="Freshservice Change number">
            <input value={change} onChange={e => setChange(e.target.value)} maxLength={40} placeholder="e.g. CHG-1234" style={input} />
          </Field>
          <Field label="AMS SR number">
            <input value={sr} onChange={e => setSr(e.target.value)} maxLength={40} placeholder="e.g. 1234567890" style={input} />
          </Field>
          <Field label="MS Teams meeting" hint={meetings.length ? undefined : 'No links yet - add one under “Teams links”'}>
            <select value={meeting} onChange={e => setMeeting(e.target.value)} style={input}>
              <option value="">None</option>
              {meetings.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
          </Field>
          <Field label="Notes (optional)" style={{ flex: '1 1 240px' }}>
            <input value={notes} onChange={e => setNotes(e.target.value)} maxLength={500} style={input} />
          </Field>
        </div>

        <div style={{ color: COLORS.textPrimary, fontSize: 13.5, fontWeight: 600, marginBottom: 6 }}>
          Instances in scope <span style={{ color: COLORS.textMuted, fontWeight: 400 }}>— from AWS Resources › EC2 Instances; click a row to select</span>
        </div>
        {dropped.length > 0 && (
          <div role="status" style={{ color: COLORS.warning, fontSize: 12.5, marginBottom: 6 }}>
            {dropped.length} instance(s) from this schedule are no longer in the inventory and will be removed when you save:{' '}
            {dropped.map(i => i.name || i.instance_id).join(', ')}
          </div>
        )}
        <InstancePicker instances={instances} selected={selected} onChange={setSelected}
          loading={loadingInv} error={invErr} onReload={() => loadInventory(true)} />

        {err && <div role="alert" style={{ color: COLORS.danger, fontSize: 13, marginTop: 10 }}>{err}</div>}

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 14 }}>
          <button type="button" onClick={onClose} disabled={saving}
            style={{ padding: '8px 16px', borderRadius: 6, border: `1px solid ${COLORS.border}`, background: '#fff', cursor: 'pointer' }}>Cancel</button>
          <button type="submit" disabled={saving || loadingInv}
            style={{ ...primaryBtn, padding: '8px 18px', cursor: saving ? 'wait' : 'pointer', opacity: saving ? 0.7 : 1 }}>
            {saving ? 'Saving…' : editing ? 'Save changes' : 'Create schedule'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ── Schedule card ────────────────────────────────────────────────────────

function Info({ label, value }) {
  return (
    <div>
      <div style={{ color: COLORS.textSecondary }}>{label}</div>
      <div style={{ color: COLORS.textPrimary, fontWeight: 600 }}>{value}</div>
    </div>
  )
}

function PlanCard({ plan, canEdit, onEdit, onDelete, deleting }) {
  const [open, setOpen] = useState(false)
  const meta = STATUS_META[plan.status] || STATUS_META.planned
  const isToday = plan.status === 'planned' && plan.schedule_date === todayHkt()
  return (
    <div style={{ ...CARD, padding: 14, borderLeft: `4px solid ${meta.color}` }}>
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ color: COLORS.textPrimary, fontSize: 15, fontWeight: 700 }}>
          {plan.schedule_date} {plan.schedule_time} <span style={{ color: COLORS.textSecondary, fontWeight: 500, fontSize: 12.5 }}>UTC+8</span>
          {plan.title ? <span style={{ fontWeight: 500 }}> · {plan.title}</span> : null}
        </div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          {isToday && <Badge text="Today" color="#d97706" />}
          <Badge text={meta.label} color={meta.color} />
          {canEdit && (
            <>
              <button type="button" onClick={() => onEdit(plan)} style={smallBtn}>Edit</button>
              <button type="button" onClick={() => onDelete(plan)} disabled={deleting} style={dangerBtn}>
                {deleting ? 'Deleting…' : 'Delete'}
              </button>
            </>
          )}
        </div>
      </div>

      <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', marginTop: 10, fontSize: 12.5 }}>
        <Info label="Freshservice Change" value={plan.freshservice_change || '—'} />
        <Info label="AMS SR" value={plan.ams_sr || '—'} />
        <Info label="Instances" value={String(plan.instances.length)} />
        <div>
          <div style={{ color: COLORS.textSecondary }}>MS Teams meeting</div>
          {plan.teams_meeting_url ? (
            <a href={plan.teams_meeting_url} target="_blank" rel="noopener noreferrer"
              style={{ color: COLORS.brand, fontWeight: 600 }}>Join {plan.teams_meeting_label} ↗</a>
          ) : plan.teams_meeting ? (
            <span style={{ color: COLORS.warning, fontWeight: 600 }}>Link no longer available</span>
          ) : <span style={{ color: COLORS.textPrimary, fontWeight: 600 }}>—</span>}
        </div>
      </div>

      {plan.notes && <div style={{ marginTop: 8, fontSize: 12.5, color: COLORS.textSecondary }}>{plan.notes}</div>}

      <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open} style={{ ...linkBtn, marginTop: 10 }}>
        {open ? '▾ Hide' : '▸ Show'} {plan.instances.length} instance{plan.instances.length === 1 ? '' : 's'} in scope
      </button>
      {open && (
        <div style={{ overflowX: 'auto', marginTop: 6 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
            <thead>
              <tr>{['Name', 'Instance ID', 'Account', 'Region', 'Private IP'].map(h => <th key={h} style={th} scope="col">{h}</th>)}</tr>
            </thead>
            <tbody>
              {plan.instances.map(i => (
                <tr key={instanceKey(i)} style={{ borderTop: `1px solid ${COLORS.border}` }}>
                  <td style={{ ...td, fontWeight: 600 }}>{i.name || '—'}</td>
                  <td style={{ ...td, fontFamily: 'monospace' }}>{i.instance_id}</td>
                  <td style={td}>{i.account_name || i.account_id}</td>
                  <td style={td}>{i.region}</td>
                  <td style={{ ...td, fontFamily: 'monospace' }}>{i.private_ip}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ color: COLORS.textMuted, fontSize: 11, marginTop: 8 }}>
        Created by {plan.created_by} · last updated by {plan.updated_by} on {formatWhen(plan.updated_at)}
      </div>
    </div>
  )
}

// ── OS tab ───────────────────────────────────────────────────────────────

function OsPatching() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [actionErr, setActionErr] = useState('')
  const [view, setView] = useState('upcoming') // upcoming | past | all
  const [editor, setEditor] = useState(null)   // null | { plan, presetGroupId }
  const [groupDialog, setGroupDialog] = useState(null) // null | { group: null | group }
  const [linksOpen, setLinksOpen] = useState(false)
  const [deletingId, setDeletingId] = useState('')
  const mountedRef = useRef(true)

  const load = useCallback(() => {
    setLoading(true)
    api.getPatchingOsPlans()
      .then(d => { if (mountedRef.current) { setData(d); setErr('') } })
      .catch(e => { if (mountedRef.current) setErr(errorMessage(e, 'Could not load patching schedules')) })
      .finally(() => { if (mountedRef.current) setLoading(false) })
  }, [])

  useEffect(() => {
    mountedRef.current = true
    load()
    return () => { mountedRef.current = false }
  }, [load])

  const canEdit = !!data?.can_edit

  const removePlan = async (plan) => {
    if (!window.confirm(`Delete the patching schedule on ${plan.schedule_date} ${plan.schedule_time} (UTC+8)?\n\nThis cannot be undone.`)) return
    setActionErr('')
    setDeletingId(plan.id)
    try {
      await api.deletePatchingOsPlan(plan.id)
      load()
    } catch (e) {
      setActionErr(errorMessage(e, 'Could not delete the schedule'))
    } finally {
      setDeletingId('')
    }
  }

  const removeGroup = async (group, count) => {
    const extra = count ? `\n\nIts ${count} schedule${count === 1 ? '' : 's'} will be kept and moved to “Ungrouped”.` : ''
    if (!window.confirm(`Delete the group "${group.name}"?${extra}`)) return
    setActionErr('')
    try {
      await api.deletePatchingOsGroup(group.id)
      load()
    } catch (e) {
      setActionErr(errorMessage(e, 'Could not delete the group'))
    }
  }

  const today = todayHkt()
  const plans = data?.plans || []
  const groups = data?.groups || []
  const isUpcoming = (p) => p.status === 'planned' && p.schedule_date >= today
  const upcomingCount = plans.filter(isUpcoming).length
  const pastCount = plans.length - upcomingCount

  // Sections: one per user-created group (alphabetical, from the API), then
  // "Ungrouped". Empty groups only show in "All" so they can still be managed.
  const sections = useMemo(() => {
    const matches = (p) => view === 'all' || (view === 'upcoming' ? isUpcoming(p) : !isUpcoming(p))
    const ordered = (list) => (view === 'past' ? [...list].reverse() : list)
    const out = groups.map(g => {
      const all = plans.filter(p => p.group_id === g.id)
      return { key: g.id, group: g, total: all.length, plans: ordered(all.filter(matches)) }
    })
    const ungrouped = plans.filter(p => !p.group_id || !groups.some(g => g.id === p.group_id))
    out.push({ key: '_none', group: null, total: ungrouped.length, plans: ordered(ungrouped.filter(matches)) })
    return out.filter(s => s.plans.length > 0 || (s.group && view === 'all'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plans, groups, view, today])

  return (
    <div>
      <InfoBanner
        text={canEdit
          ? 'You can create groups, schedules and Teams links. All times are UTC+8. Everyone else has read-only access.'
          : 'Read-only view. Only admins and the Cloud Infra, Security and Management Teams can change patching schedules. All times are UTC+8.'}
      />

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 12 }}>
        {[['upcoming', `Upcoming (${upcomingCount})`], ['past', `Past / closed (${pastCount})`], ['all', `All (${plans.length})`]].map(([id, label]) => (
          <button key={id} type="button" onClick={() => setView(id)} aria-pressed={view === id}
            style={{
              padding: '5px 14px', borderRadius: 999, fontSize: 12.5, fontWeight: 600, cursor: 'pointer',
              border: `1px solid ${view === id ? COLORS.brand : COLORS.border}`,
              background: view === id ? COLORS.brandLight : '#fff', color: view === id ? COLORS.brand : COLORS.textSecondary,
            }}>{label}</button>
        ))}
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button type="button" onClick={load} style={smallBtn}>↻ Refresh</button>
          <button type="button" onClick={() => setLinksOpen(true)} style={smallBtn}>🔗 Teams links ({data?.teams_meetings?.length ?? 0})</button>
          {canEdit && (
            <>
              <button type="button" onClick={() => setGroupDialog({ group: null })} style={smallBtn}>+ New group</button>
              <button type="button" onClick={() => setEditor({ plan: null, presetGroupId: '' })} style={primaryBtn}>+ New patching schedule</button>
            </>
          )}
        </div>
      </div>

      {err && <div role="alert" style={{ ...CARD, padding: 14, color: COLORS.danger, marginBottom: 12 }}>{err}</div>}
      {actionErr && <div role="alert" style={{ ...CARD, padding: 14, color: COLORS.danger, marginBottom: 12 }}>{actionErr}</div>}
      {loading && !data && <div style={{ ...CARD, padding: 20 }}><SkeletonLines lines={5} /></div>}

      {data && sections.length === 0 && (
        <div style={{ ...CARD, padding: 24, textAlign: 'center', color: COLORS.textSecondary }}>
          {view === 'upcoming' ? 'No upcoming patching schedules.' : view === 'past' ? 'No past schedules.' : 'No patching schedules yet.'}
          {canEdit && view !== 'past' ? ' Use “New patching schedule” to add one.' : ''}
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        {sections.map(s => {
          const instanceCount = s.plans.reduce((n, p) => n + p.instances.length, 0)
          const next = s.plans.filter(isUpcoming).map(p => `${p.schedule_date} ${p.schedule_time}`).sort()[0]
          return (
            <section key={s.key} aria-label={s.group ? `Group ${s.group.name}` : 'Ungrouped schedules'}
              style={{ border: `1px solid ${COLORS.border}`, borderRadius: 12, background: COLORS.pageBg, padding: 12 }}>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginBottom: 10 }}>
                <div style={{ color: COLORS.textPrimary, fontSize: 15.5, fontWeight: 700 }}>
                  {s.group ? s.group.name : 'Ungrouped'}
                </div>
                <Badge text={`${s.plans.length} schedule${s.plans.length === 1 ? '' : 's'}`} color={COLORS.brand} />
                <span style={{ color: COLORS.textSecondary, fontSize: 12.5 }}>
                  {instanceCount} instance{instanceCount === 1 ? '' : 's'}
                  {next ? ` · next ${next} UTC+8` : ''}
                </span>
                {canEdit && s.group && (
                  <div style={{ marginLeft: 'auto', display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    <button type="button" style={smallBtn} onClick={() => setEditor({ plan: null, presetGroupId: s.group.id })}>+ Add schedule</button>
                    <button type="button" style={smallBtn} onClick={() => setGroupDialog({ group: s.group })}>Rename</button>
                    <button type="button" style={dangerBtn} onClick={() => removeGroup(s.group, s.total)}>Delete group</button>
                  </div>
                )}
              </div>
              {s.plans.length === 0 ? (
                <div style={{ color: COLORS.textMuted, fontSize: 12.5, padding: '4px 2px' }}>No schedules in this group yet.</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {s.plans.map(p => (
                    <PlanCard key={p.id} plan={p} canEdit={canEdit}
                      onEdit={pl => setEditor({ plan: pl, presetGroupId: '' })} onDelete={removePlan} deleting={deletingId === p.id} />
                  ))}
                </div>
              )}
            </section>
          )
        })}
      </div>

      {editor && (
        <PlanEditor
          plan={editor.plan}
          presetGroupId={editor.presetGroupId}
          groups={groups}
          meetings={data?.teams_meetings || []}
          onClose={() => setEditor(null)}
          onSaved={() => { setEditor(null); load() }}
        />
      )}

      {groupDialog && (
        <NameDialog
          title={groupDialog.group ? 'Rename group' : 'New group'}
          label="Group / task name"
          initial={groupDialog.group?.name}
          submitLabel={groupDialog.group ? 'Rename' : 'Create group'}
          onClose={() => setGroupDialog(null)}
          onSubmit={async (name) => {
            if (groupDialog.group) await api.renamePatchingOsGroup(groupDialog.group.id, name)
            else await api.createPatchingOsGroup(name)
            setGroupDialog(null)
            load()
          }}
        />
      )}

      {linksOpen && (
        <LinksManager
          links={data?.teams_meetings || []}
          canEdit={canEdit}
          onChanged={load}
          onClose={() => setLinksOpen(false)}
        />
      )}
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────

export default function PatchingPage() {
  const [tab, setTab] = useState('os')
  return (
    <div style={{ padding: '16px 20px 24px' }}>
      <div style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0, color: COLORS.textPrimary, fontSize: 20 }}>🩹 Lifecycle · Patching</h2>
        <div style={{ color: COLORS.textSecondary, fontSize: 13, marginTop: 4 }}>
          Plan which resources are patched and when, with the change and SR references and the Teams link.
        </div>
      </div>

      <div style={{ ...TAB_BAR, marginBottom: 14 }} role="tablist" aria-label="Patching type">
        {SUB_TABS.map(t => (
          <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}
            style={tabButtonStyle(tab === t.id)}>{t.label}</button>
        ))}
      </div>

      {tab === 'os' && <OsPatching />}
    </div>
  )
}
