import React, { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'
import useOrg from '../utils/useOrg.js'
import { api } from '../api.js'
import { Badge } from '../utils/theme.js'
import Avatar from '../components/Avatar.jsx'
import { formatDateTimeHK } from '../utils/time.js'

const CARD = {
  maxWidth: 820,
  margin: '32px auto',
  background: '#fff',
  borderRadius: 12,
  padding: '28px 32px',
  boxShadow: '0 2px 10px rgba(0,0,0,0.08)',
}
const TH = { textAlign: 'left', padding: '10px 12px', fontSize: 12, color: '#64748b', fontWeight: 600, borderBottom: '2px solid #e5e7eb' }
const TD = { padding: '10px 12px', fontSize: 14, borderBottom: '1px solid #f1f5f9' }
// Role badge colors — brand blue for admin, teal for the narrower User
// Admin role, neutral grey for member — rendered through the shared
// outlined-pill <Badge> (theme.js) so this matches every other status/
// role badge in the portal.
const ROLE_BADGE_COLOR = { admin: '#1d4ed8', user_admin: '#0f766e', member: '#64748b' }
// Display label per role value — "User Admin" (with a space) is shown
// instead of the raw "user_admin" store value.
const ROLE_LABEL = { admin: 'ADMIN', user_admin: 'USER ADMIN', member: 'MEMBER' }
const BTN = { padding: '5px 10px', borderRadius: 5, fontSize: 12, fontWeight: 600, cursor: 'pointer', border: 'none' }

const EXPORT_DATASETS = [
  { key: 'all', label: 'Full Export (Jobs + Users + KB)', formats: ['json'] },
  { key: 'jobs', label: 'Jobs', formats: ['json', 'csv'] },
  { key: 'users', label: 'Users', formats: ['json', 'csv'] },
  { key: 'kb', label: 'Knowledge Base Documents', formats: ['json', 'csv'] },
]

export default function AdminPage() {
  const { user, login, refreshTabAccess } = useAuth()
  const { foundingAdminEmails, founderEmails } = useOrg()
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  // Emails currently mid-photo-upload — disables that row's avatar click
  // and shows the ⏳ overlay while the request is in flight.
  const [uploadingPhotoFor, setUploadingPhotoFor] = useState(() => new Set())

  // Raw data export state
  const [exporting, setExporting] = useState('')   // e.g. "jobs-csv" while in flight

  // Add-user form state
  const [newEmail, setNewEmail] = useState('')
  const [newName, setNewName] = useState('')
  const [newRole, setNewRole] = useState('member')
  const [adding, setAdding] = useState(false)

  // Site Notice (yellow "Production Freeze" banner shown to all users)
  const [notice, setNotice] = useState(null)          // { enabled, message, updated_by_name, updated_at } | null while loading
  const [noticeDraft, setNoticeDraft] = useState('')
  const [noticeEnabledDraft, setNoticeEnabledDraft] = useState(true)
  const [noticeLoading, setNoticeLoading] = useState(true)
  const [noticeSaving, setNoticeSaving] = useState(false)
  const [noticeErr, setNoticeErr] = useState('')
  const [noticeDirty, setNoticeDirty] = useState(false)

  // User Groups (Security Team / Infra Team / Cloud Infra Team /
  // Management Team / Billing Team / Helpdesk Team) — the SOLE
  // access-control layer for sidebar tab visibility (besides being an
  // admin). Each group is granted whole sidebar categories.
  const [groups, setGroups] = useState([])           // [{id, label}, ...] — fixed registry from the backend
  const [categories, setCategories] = useState([])    // [{id, label, tab_ids}, ...] — fixed registry
  const [tabs, setTabs] = useState([])                // [{id, label}, ...] — fixed registry (settings_store.TAB_REGISTRY), used for the tab_write "Can Do" checklist below
  const [groupAccessDraft, setGroupAccessDraft] = useState({})  // {group_id: [category_id, ...]} — editable copy
  const [groupAccessLoading, setGroupAccessLoading] = useState(true)
  const [groupAccessSaving, setGroupAccessSaving] = useState(false)
  const [groupAccessErr, setGroupAccessErr] = useState('')
  const [groupAccessDirty, setGroupAccessDirty] = useState(false)
  // Per-user group-membership editor state — which user's dropdown is
  // currently open, keyed by email (only one open at a time).
  const [groupPickerFor, setGroupPickerFor] = useState('')
  const [savingGroupsFor, setSavingGroupsFor] = useState('')
  // Per-user tab WRITE ("Can Do") editor state — a THIRD, independent
  // layer from role and group membership above (see database.py's
  // PortalUser.tab_write docstring): which user's checklist is
  // currently open, keyed by email (only one open at a time). Applies
  // to every tab_id in `tabs` (per the user's explicit choice, since
  // it's unknown which tab will gain a write action next), even though
  // most tabs have no write action gated today.
  const [tabWritePickerFor, setTabWritePickerFor] = useState('')
  const [savingTabWriteFor, setSavingTabWriteFor] = useState('')

  // Connected Portal Password Updates (Freshservice Change Calendar, SR
  // Ticket Information) — each entry: { id, label, username, configured }.
  // Password fields are never pre-filled (write-only from this page's
  // perspective; the backend never returns a password).
  const [connectedPortals, setConnectedPortals] = useState([])
  const [connectedPortalsLoading, setConnectedPortalsLoading] = useState(true)
  const [connectedPortalsErr, setConnectedPortalsErr] = useState('')
  const [connectedPortalDrafts, setConnectedPortalDrafts] = useState({})   // { [id]: { username, password } }
  const [connectedPortalSaving, setConnectedPortalSaving] = useState('')   // system id currently saving, or ''

  const load = () => {
    setLoading(true)
    api.adminListUsers()
      .then(d => { setUsers(d.users || []); setError('') })
      .catch(e => setError(e?.response?.data?.detail || 'Failed to load users — admin access required'))
      .finally(() => setLoading(false))
  }

  const loadNotice = () => {
    setNoticeLoading(true)
    api.adminGetFreezeNotice()
      .then(d => {
        setNotice(d)
        setNoticeDraft(d.message || '')
        setNoticeEnabledDraft(!!d.enabled)
        setNoticeDirty(false)
        setNoticeErr('')
      })
      .catch(e => setNoticeErr(e?.response?.data?.detail || 'Failed to load site notice'))
      .finally(() => setNoticeLoading(false))
  }

  const loadGroups = () => {
    setGroupAccessLoading(true)
    api.adminGetGroups()
      .then(d => {
        setGroups(d.groups || [])
        setCategories(d.categories || [])
        setTabs(d.tabs || [])
        setGroupAccessDraft(d.access || {})
        setGroupAccessDirty(false)
        setGroupAccessErr('')
      })
      .catch(e => setGroupAccessErr(e?.response?.data?.detail || 'Failed to load user groups'))
      .finally(() => setGroupAccessLoading(false))
  }

  const loadConnectedPortals = () => {
    setConnectedPortalsLoading(true)
    api.adminListConnectedPortals()
      .then(d => {
        const systems = d.systems || []
        setConnectedPortals(systems)
        // Seed each draft's username from the current value; password
        // always starts blank (never returned by the backend).
        setConnectedPortalDrafts(prev => {
          const next = { ...prev }
          for (const s of systems) {
            if (!next[s.id]) next[s.id] = { username: s.username || '', password: '', apiKey: '' }
          }
          return next
        })
        setConnectedPortalsErr('')
      })
      .catch(e => setConnectedPortalsErr(e?.response?.data?.detail || 'Failed to load connected portal settings'))
      .finally(() => setConnectedPortalsLoading(false))
  }

  useEffect(() => { load(); loadNotice(); loadGroups(); loadConnectedPortals() }, [])

  const handleSaveNotice = async () => {
    if (!noticeDraft.trim()) {
      setNoticeErr('Message must not be empty')
      return
    }
    setNoticeSaving(true)
    setNoticeErr('')
    try {
      const updated = await api.adminSetFreezeNotice(noticeEnabledDraft, noticeDraft.trim())
      setNotice(updated)
      setNoticeDraft(updated.message || '')
      setNoticeEnabledDraft(!!updated.enabled)
      setNoticeDirty(false)
      flash('Site notice updated')
    } catch (e) {
      setNoticeErr(e?.response?.data?.detail || 'Failed to save site notice')
    } finally {
      setNoticeSaving(false)
    }
  }

  const handleToggleNoticeEnabled = async () => {
    const nextEnabled = !noticeEnabledDraft
    setNoticeEnabledDraft(nextEnabled)
    // Toggling on/off is a one-click action independent of the message
    // textarea's "Save" button — saves immediately using whatever message
    // is currently stored (not the possibly-unsaved draft), so a quick
    // "just turn it off" doesn't accidentally also commit half-edited text.
    setNoticeSaving(true)
    setNoticeErr('')
    try {
      const updated = await api.adminSetFreezeNotice(nextEnabled, notice?.message || noticeDraft)
      setNotice(updated)
      flash(nextEnabled ? 'Site notice is now visible to all users' : 'Site notice is now hidden')
    } catch (e) {
      setNoticeErr(e?.response?.data?.detail || 'Failed to update site notice')
      setNoticeEnabledDraft(!nextEnabled) // revert the toggle on failure
    } finally {
      setNoticeSaving(false)
    }
  }

  const flash = (msg) => { setSuccess(msg); setTimeout(() => setSuccess(''), 3000) }

  const setConnectedPortalDraft = (systemId, field, value) => {
    setConnectedPortalDrafts(prev => ({ ...prev, [systemId]: { ...prev[systemId], [field]: value } }))
  }

  // What a connected portal needs depends on its auth_type: an "api_key"
  // system (Freshservice Change Calendar) just needs the key pasted; a
  // "password" system (SR Ticket Information) needs username + password
  // (username is pre-filled, so it just needs the password typed).
  const connectedPortalDraftIsSavable = (system, draft) => {
    if (!system || !draft) return false
    if (system.auth_type === 'api_key') return !!draft.apiKey?.trim()
    return !!draft.password && !!draft.username?.trim()
  }

  const handleSaveConnectedPortal = async (systemId, label) => {
    const draft = connectedPortalDrafts[systemId] || {}
    const system = connectedPortals.find(s => s.id === systemId)
    if (!connectedPortalDraftIsSavable(system, draft)) {
      setConnectedPortalsErr(system?.auth_type === 'api_key'
        ? `${label}: paste the API key`
        : `${label}: enter the username and the new password`)
      return
    }
    setConnectedPortalSaving(systemId)
    setConnectedPortalsErr('')
    try {
      const updated = await api.adminUpdateConnectedPortal(systemId,
        system.auth_type === 'api_key'
          ? { apiKey: draft.apiKey.trim() }
          : { username: draft.username.trim(), password: draft.password })
      setConnectedPortals(prev => prev.map(s => s.id === systemId
        ? { ...s, username: updated.username, configured: true, api_key_configured: !!updated.api_key_configured }
        : s))
      // Clear the password and API key fields after a successful save —
      // both are write-only and now stale (the real values live only in
      // Secrets Manager), so leaving them on screen would be misleading.
      setConnectedPortalDrafts(prev => ({ ...prev, [systemId]: { username: updated.username || '', password: '', apiKey: '' } }))
      flash(`Updated ${label} credentials`)
    } catch (e) {
      setConnectedPortalsErr(e?.response?.data?.detail || `Failed to update ${label} credentials`)
    } finally {
      setConnectedPortalSaving('')
    }
  }

  // Flip one group's checkbox for one category in the editable draft —
  // same "edit freely, then commit" UX as Tab Access Control above.
  const toggleGroupAccessDraft = (groupId, categoryId) => {
    setGroupAccessDraft(prev => {
      const current = prev[groupId] || []
      const next = current.includes(categoryId) ? current.filter(c => c !== categoryId) : [...current, categoryId]
      return { ...prev, [groupId]: next }
    })
    setGroupAccessDirty(true)
  }

  const handleSaveGroupAccess = async () => {
    setGroupAccessSaving(true)
    setGroupAccessErr('')
    try {
      const updated = await api.adminSetGroupCategoryAccess(groupAccessDraft)
      setGroupAccessDraft(updated)
      setGroupAccessDirty(false)
      flash('User group category access updated')
      // Refresh the current admin's own sidebar in case they just
      // granted/revoked a category for a group they themselves belong to.
      refreshTabAccess()
    } catch (e) {
      setGroupAccessErr(e?.response?.data?.detail || 'Failed to save user group access')
    } finally {
      setGroupAccessSaving(false)
    }
  }

  // Toggle one group membership for one user directly from the User
  // Management table — saves immediately (no separate draft/save step,
  // unlike the category-access matrix below) since this is a simple
  // per-user membership toggle, not a matrix edit.
  const handleToggleUserGroup = async (u, groupId) => {
    const current = u.groups || []
    const next = current.includes(groupId) ? current.filter(g => g !== groupId) : [...current, groupId]
    setSavingGroupsFor(u.email)
    setError('')
    try {
      const updated = await api.adminUpdateUserGroups(u.email, next)
      setUsers(prev => prev.map(x => x.email === u.email ? { ...x, groups: updated.groups } : x))
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to update user groups')
    } finally {
      setSavingGroupsFor('')
    }
  }

  // Toggle one tab's WRITE grant for one user — same immediate-save UX
  // as handleToggleUserGroup above, but a completely independent field
  // (tab_write, not groups). Restricted server-side to portal
  // authorities (require_portal_authority); the button that opens this
  // picker is itself gated the same way in the JSX below.
  const handleToggleUserTabWrite = async (u, tabId) => {
    const current = u.tab_write || []
    const next = current.includes(tabId) ? current.filter(t => t !== tabId) : [...current, tabId]
    setSavingTabWriteFor(u.email)
    setError('')
    try {
      const updated = await api.adminUpdateUserTabWrite(u.email, next)
      setUsers(prev => prev.map(x => x.email === u.email ? { ...x, tab_write: updated.tab_write } : x))
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to update write access')
    } finally {
      setSavingTabWriteFor('')
    }
  }

  const handleAdd = async (e) => {
    e.preventDefault()
    if (!newEmail.trim()) return
    setAdding(true)
    setError('')
    try {
      await api.adminAddUser(newEmail.trim().toLowerCase(), newName.trim() || newEmail.trim(), newRole)
      setNewEmail(''); setNewName(''); setNewRole('member')
      flash(`Added ${newEmail}`)
      load()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to add user')
    } finally {
      setAdding(false)
    }
  }

  const handleRoleToggle = async (u) => {
    const newRole = u.role === 'admin' ? 'member' : 'admin'
    try {
      await api.adminUpdateUserRole(u.email, newRole)
      flash(`${u.name} is now ${newRole}`)
      load()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to update role')
    }
  }

  // Grant/revoke the narrower "User Admin" role — separate action from
  // the full admin/member toggle above, gated server-side to founding
  // admins / Management Team only (require_user_admin_manager).
  const handleUserAdminRoleToggle = async (u) => {
    const grant = u.role !== 'user_admin'
    try {
      await api.adminUpdateUserUserAdminRole(u.email, grant)
      flash(`${u.name} is ${grant ? 'now a User Admin' : 'no longer a User Admin'}`)
      load()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to update User Admin role')
    }
  }

  const handleRemove = async (u) => {
    if (!window.confirm(`Remove portal access for ${u.name} (${u.email})?`)) return
    try {
      await api.adminRemoveUser(u.email)
      flash(`Removed ${u.email}`)
      load()
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to remove user')
    }
  }

  // Admin-managed photo upload — sets/replaces ANY user's profile photo
  // (e.g. uploading a headshot on behalf of a teammate who hasn't done
  // it themselves). If the uploaded photo happens to be for the admin's
  // own account, also refreshes the sidebar avatar immediately via
  // login() — same as the self-service upload in App.jsx's Sidebar.
  const handlePhotoUpload = async (targetEmail, file) => {
    setError('')
    setUploadingPhotoFor(prev => new Set(prev).add(targetEmail))
    try {
      const result = await api.adminUploadUserPhoto(targetEmail, file)
      setUsers(prev => prev.map(u => u.email === targetEmail ? { ...u, photo_data_url: result.photo_data_url } : u))
      if (targetEmail === user.email) login({ ...user, photo_data_url: result.photo_data_url })
      flash(`Updated photo for ${targetEmail}`)
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to upload photo')
    } finally {
      setUploadingPhotoFor(prev => {
        const next = new Set(prev)
        next.delete(targetEmail)
        return next
      })
    }
  }

  const isFoundingAdmin = (email) => foundingAdminEmails.includes((email || '').toLowerCase())
  // Portal founder: optional display-only badge (PORTAL_FOUNDER_EMAILS on the backend).
  const isPortalFounder = (email) => founderEmails.includes((email || '').toLowerCase())

  const handleExportJson = async (dataset) => {
    setExporting(`${dataset}-json`)
    setError('')
    try {
      const data = await api.adminExportJson(dataset)
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = window.URL.createObjectURL(blob)
      const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
      const link = document.createElement('a')
      link.href = url
      link.download = `${dataset}-export-${stamp}.json`
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.URL.revokeObjectURL(url)
      flash(`Exported ${dataset} as JSON`)
    } catch (e) {
      setError(e?.response?.data?.detail || `Failed to export ${dataset}`)
    } finally {
      setExporting('')
    }
  }

  const handleExportCsv = async (dataset) => {
    setExporting(`${dataset}-csv`)
    setError('')
    try {
      await api.adminExportCsv(dataset)
      flash(`Exported ${dataset} as CSV`)
    } catch (e) {
      setError(e?.response?.data?.detail || `Failed to export ${dataset}`)
    } finally {
      setExporting('')
    }
  }

  // Full admins see the whole page; a plain "User Admin" (not also a
  // full admin) is let in too, but the JSX below hides every section
  // except the add-user form + their own filtered user list — see the
  // isFullAdmin checks sprinkled through the sections below.
  if (!user?.is_admin && !user?.is_user_admin) {
    return (
      <div style={CARD}>
        <h2 style={{ color: '#dc2626' }}>🚫 Access Denied</h2>
        <p style={{ color: '#64748b' }}>Admin access is required to view this page.</p>
      </div>
    )
  }
  const isFullAdmin = !!user?.is_admin
  // "Portal authority" — founding admin, Management Team, or Security
  // Team (backend: user_store.is_portal_authority()). Gates every
  // authorization/delegation decision: Group membership, group-
  // >category access, per-user tab WRITE grants, and promoting/
  // demoting Admin/User Admin. Named canManageUserAdminRole in an
  // earlier iteration of this feature when it only gated Admin/User
  // Admin role changes — kept this variable name to avoid a large,
  // purely-cosmetic diff below; its meaning is now broader.
  const canManageUserAdminRole = !!user?.is_portal_authority

  return (
    <div style={CARD}>
      <h2 style={{ color: '#0f2e5a', marginBottom: 4 }}>👤 User Management</h2>
      <p style={{ color: '#64748b', fontSize: 13, marginBottom: 24 }}>
        {isFullAdmin
          ? 'Manage who can access the Cloud Infra Service Desk and their permission level.'
          : "Add new team members to the Cloud Infra Service Desk. You're shown only yourself and the users you've added."}
      </p>

      {error && <div style={{ background: '#fef2f2', color: '#dc2626', padding: '10px 14px', borderRadius: 6, fontSize: 13, marginBottom: 16 }}>{error}</div>}
      {success && <div style={{ background: '#f0fdf4', color: '#16a34a', padding: '10px 14px', borderRadius: 6, fontSize: 13, marginBottom: 16 }}>✓ {success}</div>}

      {/* Add user form */}
      <form onSubmit={handleAdd} style={{ background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 8, padding: '16px 18px', marginBottom: 24, display: 'flex', gap: 10, alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <div style={{ flex: '1 1 220px' }}>
          <label style={{ fontSize: 12, color: '#475569', fontWeight: 600, display: 'block', marginBottom: 4 }}>Email</label>
          <input
            type="email" required value={newEmail} onChange={e => setNewEmail(e.target.value)}
            placeholder="name@example.com"
            style={{ width: '100%', padding: '8px 10px', borderRadius: 6, border: '1px solid #cbd5e1', fontSize: 13 }}
          />
        </div>
        <div style={{ flex: '1 1 180px' }}>
          <label style={{ fontSize: 12, color: '#475569', fontWeight: 600, display: 'block', marginBottom: 4 }}>Display name</label>
          <input
            type="text" value={newName} onChange={e => setNewName(e.target.value)}
            placeholder="Full name"
            style={{ width: '100%', padding: '8px 10px', borderRadius: 6, border: '1px solid #cbd5e1', fontSize: 13 }}
          />
        </div>
        <div>
          <label style={{ fontSize: 12, color: '#475569', fontWeight: 600, display: 'block', marginBottom: 4 }}>Role</label>
          <select value={newRole} onChange={e => setNewRole(e.target.value)}
            style={{ padding: '8px 10px', borderRadius: 6, border: '1px solid #cbd5e1', fontSize: 13, background: '#fff' }}>
            <option value="member">Member</option>
            {/* Assigning "User Admin" or "Admin" directly when adding a
                new user is restricted to founding admins / Management
                Team (canManageUserAdminRole) — same rule as the role-
                toggle buttons in the table below. Anyone else's add-user
                form only offers Member; the backend enforces this too
                (see main.py's _UNRESTRICTED_ASSIGNABLE_ROLES), this just
                keeps the dropdown from offering an option that would 403. */}
            {canManageUserAdminRole && <option value="user_admin">User Admin</option>}
            {canManageUserAdminRole && <option value="admin">Admin</option>}
          </select>
        </div>
        <button type="submit" disabled={adding || !newEmail.trim()}
          style={{ ...BTN, padding: '9px 16px', background: '#1a4080', color: '#fff', fontSize: 13 }}>
          {adding ? 'Adding…' : '+ Add User'}
        </button>
      </form>

      {loading ? (
        <p style={{ color: '#64748b' }}>Loading users…</p>
      ) : (
        <div style={{ overflowX: 'auto' }}>
        <table data-cards style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ ...TH, width: 44 }}></th>
              <th style={TH}>Name</th>
              <th style={TH}>Email</th>
              <th style={TH}>Role</th>
              <th style={TH}>Groups</th>
              {canManageUserAdminRole && <th style={TH}>Write Access</th>}
              <th style={TH}>Added by</th>
              <th style={TH}>Last Login (HKT)</th>
              <th style={TH}></th>
            </tr>
          </thead>
          <tbody>
            {users.map(u => {
              const userGroups = u.groups || []
              return (
              <tr key={u.email}>
                <td style={{ ...TD, padding: '8px 12px' }}>
                  <Avatar
                    photoUrl={u.photo_data_url} name={u.name} email={u.email}
                    size={32} fontSize={13}
                    // Photo upload stays a full-admin-only action
                    // (backend: admin_upload_user_photo is still
                    // require_admin) — omitting onUpload for a plain
                    // User Admin makes Avatar render read-only instead
                    // of offering a click target that would just 403.
                    onUpload={isFullAdmin ? (file) => handlePhotoUpload(u.email, file) : undefined}
                    uploading={uploadingPhotoFor.has(u.email)}
                    title={isFullAdmin ? `Click to upload a photo for ${u.name}` : u.name}
                  />
                </td>
                <td style={TD}>{u.name}</td>
                <td style={TD}>{u.email}</td>
                <td style={TD}>
                  <Badge text={ROLE_LABEL[u.role] || u.role.toUpperCase()} color={ROLE_BADGE_COLOR[u.role] || '#64748b'} />
                  {isFoundingAdmin(u.email) && (
                    <span style={{ marginLeft: 6, fontSize: 11, color: '#94a3b8' }}>(founding admin)</span>
                  )}
                  {isPortalFounder(u.email) && (
                    <span style={{ marginLeft: 6, fontSize: 11, color: '#b45309', fontWeight: 600 }}>(portal founder)</span>
                  )}
                </td>
                <td style={{ ...TD, position: 'relative' }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
                    {userGroups.length === 0 && <span style={{ color: '#94a3b8', fontSize: 12 }}>—</span>}
                    {userGroups.map(gid => {
                      const g = groups.find(x => x.id === gid)
                      return (
                        // whiteSpace: 'nowrap' keeps each pill's own label on
                        // a single line — without it, a long label (e.g.
                        // "Cloud Infra Team") wraps INSIDE the pill onto two
                        // lines, but the pill's rounded background doesn't
                        // reshape to fit both lines, so the text visually
                        // spills outside the pill's rounded box. The parent
                        // flex container's flexWrap still wraps whole pills
                        // onto the next line as needed, so nothing overflows
                        // the table cell itself.
                        <span key={gid} style={{ padding: '2px 8px', borderRadius: 12, background: '#f1f5f9', color: '#334155', fontSize: 11, fontWeight: 600, whiteSpace: 'nowrap', display: 'inline-block' }}>
                          {g?.label || gid}
                        </span>
                      )
                    })}
                    {/* Editing group membership (READ/visibility) is a
                        portal-authority action — founding admin /
                        Management Team / Security Team — NOT every
                        plain full admin, matching main.py's
                        admin_update_user_groups (require_portal_authority). */}
                    {canManageUserAdminRole && (
                      <button
                        onClick={() => setGroupPickerFor(prev => prev === u.email ? '' : u.email)}
                        disabled={savingGroupsFor === u.email}
                        style={{ border: 'none', background: 'none', color: '#1d4ed8', fontSize: 12, fontWeight: 700, cursor: 'pointer', padding: '2px 4px' }}>
                        {savingGroupsFor === u.email ? '…' : (userGroups.length === 0 ? '+ Assign' : '✏️')}
                      </button>
                    )}
                  </div>
                  {canManageUserAdminRole && groupPickerFor === u.email && (
                    <div style={{ position: 'absolute', top: '100%', left: 0, marginTop: 4, background: '#fff', border: '1px solid #e2e8f0', borderRadius: 8, boxShadow: '0 6px 20px rgba(0,0,0,0.12)', zIndex: 30, minWidth: 200, padding: 10 }}>
                      <div style={{ fontSize: 11, color: '#94a3b8', fontWeight: 700, textTransform: 'uppercase', marginBottom: 6 }}>Assign to groups</div>
                      {groups.map(g => (
                        <label key={g.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 2px', fontSize: 12.5, color: '#1e293b', cursor: 'pointer' }}>
                          <input type="checkbox" checked={userGroups.includes(g.id)}
                            onChange={() => handleToggleUserGroup(u, g.id)} />
                          {g.label}
                        </label>
                      ))}
                      <button onClick={() => setGroupPickerFor('')}
                        style={{ marginTop: 6, border: 'none', background: 'none', color: '#64748b', fontSize: 11.5, cursor: 'pointer', padding: 0 }}>
                        Close
                      </button>
                    </div>
                  )}
                </td>
                {/* Write Access ("Can Do") — a THIRD, independent layer
                    from role and group membership: which tabs this user
                    may perform the write action for (e.g. create a
                    ticket, edit DNS). NEVER inherited from group
                    membership — always an individual grant, editable
                    only by a portal authority. Applies to every tab in
                    `tabs`, per the user's explicit choice.

                    The ENTIRE cell (not just the edit button) is hidden
                    for a non-portal-authority viewer — per the user's
                    explicit follow-up request, even the plain "N
                    granted" count summary must not be visible to a
                    plain full admin/User Admin, not just the ability to
                    edit it. Paired with the header cell above also
                    being conditional, so the whole column disappears
                    rather than showing an empty header with no cells
                    under it. */}
                {canManageUserAdminRole && (
                <td style={{ ...TD, position: 'relative' }}>
                  <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                    {/* Collapsed to a plain count instead of listing every
                        granted tab's pill — with TAB_REGISTRY now over a
                        dozen entries, a user granted several tabs (or one
                        whose group already covers dns_records/
                        s2s_vpn_profile_creation) made this cell balloon
                        to many lines and pushed the whole row's height
                        out of proportion. Click either the count or the
                        edit button to see/change the actual list in the
                        picker below — same picker as before, just not
                        rendered inline anymore. */}
                    <button
                      onClick={() => setTabWritePickerFor(prev => prev === u.email ? '' : u.email)}
                      disabled={savingTabWriteFor === u.email}
                      style={{ border: 'none', background: 'none', fontSize: 12.5, fontWeight: 600, padding: '2px 4px', color: (u.tab_write || []).length > 0 ? '#9a3412' : '#1d4ed8', cursor: 'pointer' }}>
                      {savingTabWriteFor === u.email ? '…' : (
                        (u.tab_write || []).length === 0 ? '+ Grant' : `${(u.tab_write || []).length} granted ✏️`
                      )}
                    </button>
                  </div>
                  {tabWritePickerFor === u.email && (
                    <div style={{ position: 'absolute', top: '100%', left: 0, marginTop: 4, background: '#fff', border: '1px solid #e2e8f0', borderRadius: 8, boxShadow: '0 6px 20px rgba(0,0,0,0.12)', zIndex: 30, minWidth: 220, maxHeight: 280, overflowY: 'auto', padding: 10 }}>
                      <div style={{ fontSize: 11, color: '#94a3b8', fontWeight: 700, textTransform: 'uppercase', marginBottom: 6 }}>Grant write access</div>
                      {tabs.map(t => (
                        <label key={t.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 2px', fontSize: 12.5, color: '#1e293b', cursor: 'pointer' }}>
                          <input type="checkbox" checked={(u.tab_write || []).includes(t.id)}
                            onChange={() => handleToggleUserTabWrite(u, t.id)} />
                          {t.label}
                        </label>
                      ))}
                      <button onClick={() => setTabWritePickerFor('')}
                        style={{ marginTop: 6, border: 'none', color: '#64748b', fontSize: 11.5, cursor: 'pointer', padding: 0, position: 'sticky', bottom: 0, background: '#fff' }}>
                        Close
                      </button>
                    </div>
                  )}
                </td>
                )}
                <td style={{ ...TD, color: '#94a3b8', fontSize: 12 }}>{u.added_by || '—'}</td>
                <td style={{ ...TD, color: '#64748b', fontSize: 12.5, whiteSpace: 'nowrap' }}>
                  {u.last_login ? formatDateTimeHK(u.last_login) : <span style={{ color: '#94a3b8' }}>Never</span>}
                </td>
                <td style={{ ...TD, textAlign: 'right', whiteSpace: 'nowrap' }}>
                  {!isFoundingAdmin(u.email) && (
                    <>
                      {/* Full admin <-> member toggle — founding admins
                          or Management Team only (backend:
                          require_user_admin_manager), same restriction
                          as the User Admin toggle below. A plain full
                          admin who is neither a founding admin nor on
                          the Management Team no longer sees this
                          button, per the user's explicit follow-up
                          request. */}
                      {canManageUserAdminRole && (
                        <button onClick={() => handleRoleToggle(u)}
                          style={{ ...BTN, background: '#eff6ff', color: '#1d4ed8', marginRight: 6 }}>
                          {u.role === 'admin' ? 'Make Member' : 'Make Admin'}
                        </button>
                      )}
                      {/* User Admin <-> member toggle — founding admins
                          or Management Team only (backend:
                          require_user_admin_manager), never shown for
                          someone who is already a full admin. */}
                      {canManageUserAdminRole && u.role !== 'admin' && (
                        <button onClick={() => handleUserAdminRoleToggle(u)}
                          style={{ ...BTN, background: '#f0fdfa', color: '#0f766e', marginRight: 6 }}>
                          {u.role === 'user_admin' ? 'Remove User Admin' : 'Make User Admin'}
                        </button>
                      )}
                      {isFullAdmin && (
                        <button onClick={() => handleRemove(u)}
                          style={{ ...BTN, background: '#fef2f2', color: '#dc2626' }}
                          disabled={u.email === user.email}>
                          Remove
                        </button>
                      )}
                    </>
                  )}
                </td>
              </tr>
              )
            })}
          </tbody>
        </table>
        </div>
      )}

      {/* Everything below (Site Notice, Connected Portal credentials,
          User Groups category-access matrix, Raw Data Export) stays
          exclusively for full admins — a plain "User Admin" only ever
          sees the user table + add-user form above this point. */}
      {isFullAdmin && (
      <>
      <p style={{ marginTop: 20, fontSize: 12, color: '#94a3b8' }}>
        Note: users must also be assigned to the Job Portal application in Azure AD
        (Enterprise Applications → Job Portal → Users and groups) before they can sign in,
        if the app registration requires assignment.
      </p>

      {/* Site Notice — the yellow "Production Freeze" banner shown site-
          wide (see components/ProductionFreezeNotice.jsx). Lets an admin
          edit the message text and show/hide it without a code deploy. */}
      <div style={{ marginTop: 32, paddingTop: 24, borderTop: '1px solid #e5e7eb' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
          <h2 style={{ color: '#0f2e5a', marginBottom: 0, fontSize: 18 }}>❄️ Site Notice (yellow banner)</h2>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, fontWeight: 600, color: noticeEnabledDraft ? '#15803d' : '#64748b', cursor: noticeSaving ? 'default' : 'pointer' }}>
            <input type="checkbox" checked={noticeEnabledDraft} disabled={noticeSaving || noticeLoading}
              onChange={handleToggleNoticeEnabled}
              style={{ cursor: noticeSaving ? 'default' : 'pointer', width: 16, height: 16 }} />
            {noticeEnabledDraft ? 'Visible to all users' : 'Hidden'}
          </label>
        </div>
        <p style={{ color: '#64748b', fontSize: 13, marginBottom: 14 }}>
          Controls the yellow notice banner shown at the top of every page for every portal user
          (e.g. Production Freeze period announcements). Use <code>**text**</code> for bold. Changes
          take effect immediately — no code deploy needed.
        </p>

        {noticeLoading ? (
          <p style={{ color: '#64748b', fontSize: 13 }}>Loading…</p>
        ) : (
          <>
            <textarea
              value={noticeDraft}
              onChange={e => { setNoticeDraft(e.target.value); setNoticeDirty(true) }}
              placeholder="Enter the banner message… use **text** for bold, and a blank line to start a new paragraph."
              maxLength={4000}
              style={{
                width: '100%', minHeight: 130, padding: '10px 12px', borderRadius: 7,
                border: '1px solid #cbd5e1', fontSize: 13, resize: 'vertical',
                boxSizing: 'border-box', fontFamily: 'inherit',
              }}
            />
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 8 }}>
              <button onClick={handleSaveNotice} disabled={noticeSaving || !noticeDirty}
                style={{ ...BTN, padding: '8px 16px', background: (noticeSaving || !noticeDirty) ? '#e2e8f0' : '#1a4080', color: (noticeSaving || !noticeDirty) ? '#94a3b8' : '#fff', fontSize: 13, cursor: (noticeSaving || !noticeDirty) ? 'default' : 'pointer' }}>
                {noticeSaving ? 'Saving…' : 'Save Message'}
              </button>
              <span style={{ fontSize: 11.5, color: '#94a3b8' }}>{noticeDraft.length} / 4000</span>
              {notice?.updated_by_name && (
                <span style={{ fontSize: 11.5, color: '#94a3b8', marginLeft: 'auto' }}>
                  Last updated by {notice.updated_by_name}{notice.updated_at ? ` on ${new Date(notice.updated_at).toLocaleString()}` : ''}
                </span>
              )}
            </div>
            {/* Live preview — same rendering (bold + line breaks) as the
                actual banner, so an admin can sanity-check formatting
                before saving. */}
            <div style={{ marginTop: 12, background: '#fef9c3', border: '1px solid #fde047', borderRadius: 8, padding: '10px 14px', fontSize: 13, color: '#713f12' }}>
              <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0.4, color: '#92400e', marginBottom: 6 }}>Preview</div>
              {noticeDraft.split('\n').map((line, li) => (
                <React.Fragment key={li}>
                  {li > 0 && <br />}
                  {line.split(/(\*\*[^*]+\*\*)/g).filter(Boolean).map((part, pi) => (
                    part.startsWith('**') && part.endsWith('**') && part.length > 4
                      ? <strong key={pi}>{part.slice(2, -2)}</strong>
                      : <span key={pi}>{part}</span>
                  ))}
                </React.Fragment>
              ))}
            </div>
          </>
        )}
        {noticeErr && <div style={{ marginTop: 10, background: '#fef2f2', color: '#dc2626', padding: '8px 12px', borderRadius: 6, fontSize: 13 }}>{noticeErr}</div>}
      </div>

      {/* Connected Portal Password Updates — Freshservice Change Calendar
          and SR Ticket Information both proxy through this backend using
          their own separate login (see app/fs_calendar.py,
          app/sr_ticket.py), so their passwords occasionally need
          updating here rather than via an out-of-band AWS CLI call. */}
      {user?.can_manage_cloud_accounts && (
        <div style={{ marginTop: 32, paddingTop: 24, borderTop: '1px solid #e5e7eb' }}>
          <h2 style={{ color: '#0f2e5a', marginBottom: 4, fontSize: 18 }}>☁️ Cloud Accounts</h2>
          <p style={{ color: '#64748b', fontSize: 13, marginBottom: 12 }}>
            Add, check, rename or offboard the AWS accounts and Azure subscriptions this portal reports on.
          </p>
          <Link to="/admin/cloud-accounts" style={{ color: '#1a4080', fontWeight: 600, fontSize: 14 }}>Open Cloud Accounts →</Link>
        </div>
      )}

      <div style={{ marginTop: 32, paddingTop: 24, borderTop: '1px solid #e5e7eb' }}>
        <h2 style={{ color: '#0f2e5a', marginBottom: 4, fontSize: 18 }}>🔑 Connected Portal Credential Updates</h2>
        <p style={{ color: '#64748b', fontSize: 13, marginBottom: 16 }}>
          These two systems have their own separate authentication (not Azure AD SSO) that this
          portal uses on your behalf. Freshservice Change Calendar uses an API key; SR Ticket
          Information uses a username and password. Update them here whenever they change or
          expire — takes effect immediately, no code deploy needed. Secrets are stored only in
          AWS Secrets Manager and are never shown here once saved.
        </p>

        {connectedPortalsErr && <div style={{ marginBottom: 14, background: '#fef2f2', color: '#dc2626', padding: '8px 12px', borderRadius: 6, fontSize: 13 }}>{connectedPortalsErr}</div>}

        {connectedPortalsLoading ? (
          <p style={{ color: '#64748b', fontSize: 13 }}>Loading…</p>
        ) : (
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            {connectedPortals.map(s => {
              const draft = connectedPortalDrafts[s.id] || { username: '', password: '', apiKey: '' }
              const saving = connectedPortalSaving === s.id
              return (
                <div key={s.id} style={{ flex: '1 1 300px', minWidth: 280, background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 8, padding: '16px 18px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
                    <h3 style={{ margin: 0, fontSize: 14, color: '#0f2e5a' }}>{s.label}</h3>
                    {!s.configured && (
                      <span style={{ fontSize: 11, color: '#b45309', fontWeight: 600 }}>⚠️ Not configured</span>
                    )}
                  </div>
                  {s.auth_type === 'api_key' ? (
                    <>
                      <label style={{ fontSize: 12, color: '#475569', fontWeight: 600, display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
                        <span>API Key</span>
                        <span style={{ fontWeight: 500, color: s.api_key_configured ? '#15803d' : '#b45309' }}>
                          {s.api_key_configured ? '✓ Configured' : 'Not set'}
                        </span>
                      </label>
                      <input
                        type="password" value={draft.apiKey || ''} disabled={saving}
                        autoComplete="off"
                        placeholder={s.api_key_configured ? 'Paste a new API key to replace it' : 'Paste the API key'}
                        onChange={e => setConnectedPortalDraft(s.id, 'apiKey', e.target.value)}
                        style={{ width: '100%', padding: '8px 10px', borderRadius: 6, border: '1px solid #cbd5e1', fontSize: 13, marginBottom: 12, boxSizing: 'border-box' }}
                      />
                    </>
                  ) : (
                    <>
                      <label style={{ fontSize: 12, color: '#475569', fontWeight: 600, display: 'block', marginBottom: 4 }}>Username</label>
                      <input
                        type="text" value={draft.username || ''} disabled={saving}
                        onChange={e => setConnectedPortalDraft(s.id, 'username', e.target.value)}
                        style={{ width: '100%', padding: '8px 10px', borderRadius: 6, border: '1px solid #cbd5e1', fontSize: 13, marginBottom: 10, boxSizing: 'border-box' }}
                      />
                      <label style={{ fontSize: 12, color: '#475569', fontWeight: 600, display: 'block', marginBottom: 4 }}>New password</label>
                      <input
                        type="password" value={draft.password || ''} disabled={saving}
                        placeholder="Enter the new password"
                        onChange={e => setConnectedPortalDraft(s.id, 'password', e.target.value)}
                        style={{ width: '100%', padding: '8px 10px', borderRadius: 6, border: '1px solid #cbd5e1', fontSize: 13, marginBottom: 12, boxSizing: 'border-box' }}
                      />
                    </>
                  )}
                  <button
                    onClick={() => handleSaveConnectedPortal(s.id, s.label)}
                    disabled={saving || !connectedPortalDraftIsSavable(s, draft)}
                    style={{ ...BTN, padding: '8px 16px', width: '100%', background: (saving || !connectedPortalDraftIsSavable(s, draft)) ? '#e2e8f0' : '#1a4080', color: (saving || !connectedPortalDraftIsSavable(s, draft)) ? '#94a3b8' : '#fff', fontSize: 13, cursor: (saving || !connectedPortalDraftIsSavable(s, draft)) ? 'default' : 'pointer' }}>
                    {saving ? 'Saving…' : s.auth_type === 'api_key' ? 'Update API Key' : 'Update Credentials'}
                  </button>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* User Groups — Security Team / Infra Team / Cloud Infra Team /
          Management Team / Billing Team / Helpdesk Team, each grantable
          whole sidebar CATEGORIES of access. This is now the SOLE
          access-control layer for sidebar tab visibility (besides being
          an admin, who always sees everything). Which users belong to
          which group is managed inline in the User Management table
          above (the "Groups" column). */}
      <div style={{ marginTop: 32, paddingTop: 24, borderTop: '1px solid #e5e7eb' }}>
        <h2 style={{ color: '#0f2e5a', marginBottom: 4, fontSize: 18 }}>🧑‍🤝‍🧑 User Groups (category access)</h2>
        <p style={{ color: '#64748b', fontSize: 13, marginBottom: 14 }}>
          Grant each team access to whole sidebar categories (Cost, Security, etc.). This is the
          <strong> only</strong> way non-admin users get sidebar tab access — a user in no groups sees
          no tabs at all. Admins always see every tab regardless of group membership. Assign users
          to groups from the Groups column in the User Management table above.
        </p>

        {groupAccessLoading ? (
          <p style={{ color: '#64748b', fontSize: 13 }}>Loading…</p>
        ) : (
          <>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 640 }}>
                <thead>
                  <tr>
                    <th style={TH}>Group</th>
                    {categories.map(c => (
                      <th key={c.id} style={{ ...TH, textAlign: 'center', width: 90 }}>{c.label}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {groups.map(g => {
                    const granted = groupAccessDraft[g.id] || []
                    return (
                      <tr key={g.id}>
                        <td style={TD}>{g.label}</td>
                        {categories.map(c => (
                          <td key={c.id} style={{ ...TD, textAlign: 'center' }}>
                            <input
                              type="checkbox" checked={granted.includes(c.id)} disabled={groupAccessSaving}
                              onChange={() => toggleGroupAccessDraft(g.id, c.id)}
                              style={{ width: 16, height: 16, cursor: groupAccessSaving ? 'default' : 'pointer' }}
                            />
                          </td>
                        ))}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12 }}>
              <button onClick={handleSaveGroupAccess} disabled={groupAccessSaving || !groupAccessDirty}
                style={{ ...BTN, padding: '8px 16px', background: (groupAccessSaving || !groupAccessDirty) ? '#e2e8f0' : '#1a4080', color: (groupAccessSaving || !groupAccessDirty) ? '#94a3b8' : '#fff', fontSize: 13, cursor: (groupAccessSaving || !groupAccessDirty) ? 'default' : 'pointer' }}>
                {groupAccessSaving ? 'Saving…' : 'Save Group Access'}
              </button>
              {groupAccessDirty && !groupAccessSaving && (
                <span style={{ fontSize: 11.5, color: '#d97706' }}>Unsaved changes</span>
              )}
            </div>
          </>
        )}
        {groupAccessErr && <div style={{ marginTop: 10, background: '#fef2f2', color: '#dc2626', padding: '8px 12px', borderRadius: 6, fontSize: 13 }}>{groupAccessErr}</div>}
      </div>

      {/* Raw data export */}
      <div style={{ marginTop: 32, paddingTop: 24, borderTop: '1px solid #e5e7eb' }}>
        <h2 style={{ color: '#0f2e5a', marginBottom: 4, fontSize: 18 }}>📤 Raw Data Export</h2>
        <p style={{ color: '#64748b', fontSize: 13, marginBottom: 16 }}>
          Download the portal's underlying data for backup, reporting, or offline analysis.
          JSON exports include full raw fields (audit log, notes, AI draft); CSV exports are
          flattened for spreadsheet use.
        </p>
        <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={TH}>Dataset</th>
              <th style={TH}></th>
            </tr>
          </thead>
          <tbody>
            {EXPORT_DATASETS.map(ds => (
              <tr key={ds.key}>
                <td style={TD}>{ds.label}</td>
                <td style={{ ...TD, textAlign: 'right', whiteSpace: 'nowrap' }}>
                  <button onClick={() => handleExportJson(ds.key)}
                    disabled={exporting === `${ds.key}-json`}
                    style={{ ...BTN, background: '#eff6ff', color: '#1d4ed8', marginRight: 6 }}>
                    {exporting === `${ds.key}-json` ? 'Exporting…' : '⬇ JSON'}
                  </button>
                  {ds.formats.includes('csv') && (
                    <button onClick={() => handleExportCsv(ds.key)}
                      disabled={exporting === `${ds.key}-csv`}
                      style={{ ...BTN, background: '#f0fdf4', color: '#16a34a' }}>
                      {exporting === `${ds.key}-csv` ? 'Exporting…' : '⬇ CSV'}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </div>
      </>
      )}
    </div>
  )
}
