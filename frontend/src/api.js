import axios from 'axios'

const API_BASE = import.meta.env.VITE_API_BASE_URL || ''
const BASE = `${API_BASE}/api`

// Cross-origin calls to the ALB must carry the ALB session cookie.
axios.defaults.withCredentials = true

// ── Session-expired (401) handling ─────────────────────────────────────────
//
// The ALB's Azure AD session cookie has an 8-hour absolute lifetime
// (session_timeout=Duration.hours(8) in compute_stack.py) — once it
// expires, EVERY API call this app makes starts getting a 401 back from
// get_actor() (auth_alb.py), since the ALB can no longer forward a valid
// x-amzn-oidc-data header. Before this interceptor, the app had no
// global handling for that: whichever page happened to be open would
// just show its own generic fetch-error state (e.g. "Could not load
// data") while `user` in AuthContext stayed populated — a stale,
// confusing "looks logged in but nothing works" screen.
//
// AuthContext.jsx registers a callback here (via
// registerSessionExpiredHandler()) that clears local auth state and
// sends the user back through the OIDC-gated /login-landing path,
// forcing a fresh Azure AD challenge. This is a UX improvement, NOT a
// security boundary — the real enforcement is the ALB's own
// session_timeout and get_actor()'s per-request JWT verification, which
// already reject the request regardless of whether this interceptor
// runs.
let _sessionExpiredHandler = null
export function registerSessionExpiredHandler(handler) {
  _sessionExpiredHandler = handler
}

axios.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401 && _sessionExpiredHandler) {
      _sessionExpiredHandler()
    }
    return Promise.reject(error)
  },
)

function getActor() {
  // OIDC mode: email saved to sessionStorage by AuthContext after whoami()
  const sessionEmail = sessionStorage.getItem('job_portal_email')
  if (sessionEmail) return sessionEmail
  // Local mode: user object saved to localStorage by login()
  try {
    const u = JSON.parse(localStorage.getItem('job_portal_user') || 'null')
    return u?.email || ''
  } catch { return '' }
}

function headers() {
  return { 'X-Actor-Email': getActor() }
}

export const api = {
  // Auth
  getTeam: () => axios.get(`${BASE}/auth/team`).then(r => r.data),
  login: (email) => axios.post(`${BASE}/auth/login`, { email }).then(r => r.data),
  whoami: () => axios.get(`${BASE}/auth/whoami`).then(r => r.data).catch(() => null),

  // Admin — manage portal users/permissions (admin role only)
  adminListUsers: () => axios.get(`${BASE}/admin/users`, { headers: headers() }).then(r => r.data),
  adminAddUser: (email, name, role) =>
    axios.post(`${BASE}/admin/users`, { email, name, role }, { headers: headers() }).then(r => r.data),
  adminUpdateUserRole: (email, role) =>
    axios.patch(`${BASE}/admin/users/${encodeURIComponent(email)}`, { role }, { headers: headers() }).then(r => r.data),
  // Grant/revoke the narrower "User Admin" role — separate endpoint,
  // separate backend gate (founding admin / Management Team only), see
  // main.py's require_user_admin_manager().
  adminUpdateUserUserAdminRole: (email, isUserAdmin) =>
    axios.patch(`${BASE}/admin/users/${encodeURIComponent(email)}/user-admin-role`, { is_user_admin: isUserAdmin }, { headers: headers() }).then(r => r.data),
  adminRemoveUser: (email) =>
    axios.delete(`${BASE}/admin/users/${encodeURIComponent(email)}`, { headers: headers() }).then(r => r.data),

  // User Groups (Security Team / Infra Team / Cloud Infra Team /
  // Management Team / Billing Team) — a second, additive layer of access
  // on top of role-based Tab Access Control: groups are granted whole
  // sidebar categories, and users are placed into zero or more groups.
  adminGetGroups: () => axios.get(`${BASE}/admin/settings/groups`, { headers: headers() }).then(r => r.data),
  adminSetGroupCategoryAccess: (access) =>
    axios.put(`${BASE}/admin/settings/groups`, { access }, { headers: headers() }).then(r => r.data),
  adminUpdateUserGroups: (email, groups) =>
    axios.patch(`${BASE}/admin/users/${encodeURIComponent(email)}/groups`, { groups }, { headers: headers() }).then(r => r.data),
  // Per-user tab WRITE grants — independent of User Group membership
  // (which only ever grants READ/visibility). Restricted server-side to
  // portal authorities (founding admin / Management Team / Security
  // Team), see main.py's require_portal_authority().
  adminUpdateUserTabWrite: (email, tabWrite) =>
    axios.patch(`${BASE}/admin/users/${encodeURIComponent(email)}/tab-write`, { tab_write: tabWrite }, { headers: headers() }).then(r => r.data),

  // Profile photos — self-service (own account) + admin-managed (any user)
  uploadMyProfilePhoto: (file) => {
    const form = new FormData()
    form.append('file', file)
    return axios.post(`${BASE}/profile/photo`, form, { headers: headers() }).then(r => r.data)
  },
  removeMyProfilePhoto: () =>
    axios.delete(`${BASE}/profile/photo`, { headers: headers() }).then(r => r.data),
  adminUploadUserPhoto: (email, file) => {
    const form = new FormData()
    form.append('file', file)
    return axios.post(`${BASE}/admin/users/${encodeURIComponent(email)}/photo`, form, { headers: headers() }).then(r => r.data)
  },
  adminRemoveUserPhoto: (email) =>
    axios.delete(`${BASE}/admin/users/${encodeURIComponent(email)}/photo`, { headers: headers() }).then(r => r.data),

  // Site settings — Production Freeze yellow banner (message + on/off)
  getFreezeNotice: () => axios.get(`${BASE}/settings/freeze-notice`, { headers: headers() }).then(r => r.data),
  adminGetFreezeNotice: () => axios.get(`${BASE}/admin/settings/freeze-notice`, { headers: headers() }).then(r => r.data),
  adminSetFreezeNotice: (enabled, message) =>
    axios.put(`${BASE}/admin/settings/freeze-notice`, { enabled, message }, { headers: headers() }).then(r => r.data),

  // Connected Portal Password Updates — lets an admin rotate the login
  // credentials for the Freshservice Change Calendar and SR Ticket
  // Information systems (see backend/app/connected_portal_credentials.py).
  // The password is write-only: adminListConnectedPortals() only ever
  // returns the current username, never the password.
  adminListConnectedPortals: () => axios.get(`${BASE}/admin/connected-portals`, { headers: headers() }).then(r => r.data),
  // Send the fields that match the system's auth_type: { apiKey } for an
  // "api_key" system (Freshservice Change Calendar), { username, password }
  // for a "password" system (SR Ticket Information). The backend rejects
  // the wrong combination. Passwords/keys are write-only.
  adminUpdateConnectedPortal: (systemId, { username, password, apiKey } = {}) =>
    axios.put(`${BASE}/admin/connected-portals/${encodeURIComponent(systemId)}`, {
      ...(username ? { username } : {}),
      ...(password ? { password } : {}),
      ...(apiKey ? { api_key: apiKey } : {}),
    }, { headers: headers() }).then(r => r.data),

  // Cloud Accounts (Admin > Cloud Accounts) — onboard / verify / rename /
  // offboard AWS accounts and Azure subscriptions (see
  // backend/app/cloud_account_service.py). Admin only. An AWS account's
  // External ID is only ever returned by prepare / setup / the template
  // download; nothing else carries it.
  cloudAccountsList: () => axios.get(`${BASE}/admin/cloud-accounts`, { headers: headers() }).then(r => r.data),
  cloudAccountsDetail: (provider, identifier) =>
    axios.get(`${BASE}/admin/cloud-accounts/${provider}/${encodeURIComponent(identifier)}`, { headers: headers() }).then(r => r.data),
  cloudAccountsPrepareAws: (accountId) =>
    axios.post(`${BASE}/admin/cloud-accounts/aws/prepare`, { account_id: accountId }, { headers: headers() }).then(r => r.data),
  cloudAccountsAwsSetup: (accountId) =>
    axios.get(`${BASE}/admin/cloud-accounts/aws/${encodeURIComponent(accountId)}/setup`, { headers: headers() }).then(r => r.data),
  // Returns a Blob of the CloudFormation YAML (sent with the auth header, so it
  // cannot be a plain <a href>).
  cloudAccountsTemplate: (accountId) =>
    axios.get(`${BASE}/admin/cloud-accounts/aws/${encodeURIComponent(accountId)}/template`, {
      headers: headers(), responseType: 'blob',
    }).then(r => r.data),
  cloudAccountsConnectAws: (accountId, { displayName, businessUnit, confirmNewBusinessUnit } = {}) =>
    axios.post(`${BASE}/admin/cloud-accounts/aws/${encodeURIComponent(accountId)}/connect`, {
      display_name: displayName,
      ...(businessUnit ? { business_unit: businessUnit } : {}),
      confirm_new_business_unit: !!confirmNewBusinessUnit,
    }, { headers: headers() }).then(r => r.data),
  cloudAccountsAddAzure: ({ subscriptionId, displayName, businessUnit, confirmNewBusinessUnit } = {}) =>
    axios.post(`${BASE}/admin/cloud-accounts/azure`, {
      subscription_id: subscriptionId,
      display_name: displayName,
      ...(businessUnit ? { business_unit: businessUnit } : {}),
      confirm_new_business_unit: !!confirmNewBusinessUnit,
    }, { headers: headers() }).then(r => r.data),
  cloudAccountsAzureGuide: (subscriptionId) =>
    axios.get(`${BASE}/admin/cloud-accounts/azure/guide`, {
      headers: headers(), params: subscriptionId ? { subscription_id: subscriptionId } : {},
    }).then(r => r.data),
  // changes: any of { display_name, business_unit, scope_core, scope_billing, confirm_new_business_unit }
  cloudAccountsUpdate: (provider, identifier, changes) =>
    axios.patch(`${BASE}/admin/cloud-accounts/${provider}/${encodeURIComponent(identifier)}`, changes, { headers: headers() }).then(r => r.data),
  cloudAccountsTest: (provider, identifier) =>
    axios.post(`${BASE}/admin/cloud-accounts/${provider}/${encodeURIComponent(identifier)}/test`, {}, { headers: headers() }).then(r => r.data),
  cloudAccountsTestAll: () =>
    axios.post(`${BASE}/admin/cloud-accounts/test-all`, {}, { headers: headers() }).then(r => r.data),
  // riskConfirmation is required (exact phrase) only for the Portal's own
  // account and the Core account; the server checks it.
  cloudAccountsOffboard: (provider, identifier, riskConfirmation) =>
    axios.post(`${BASE}/admin/cloud-accounts/${provider}/${encodeURIComponent(identifier)}/offboard`,
      riskConfirmation ? { risk_confirmation: riskConfirmation } : {}, { headers: headers() }).then(r => r.data),
  cloudAccountsOnboard: (provider, identifier) =>
    axios.post(`${BASE}/admin/cloud-accounts/${provider}/${encodeURIComponent(identifier)}/onboard`, {}, { headers: headers() }).then(r => r.data),
  cloudAccountsDiscard: (provider, identifier) =>
    axios.delete(`${BASE}/admin/cloud-accounts/${provider}/${encodeURIComponent(identifier)}`, { headers: headers() }).then(r => r.data),
  // AWS only: assumes the account's role and returns what it is allowed to do.
  cloudAccountsPermissions: (provider, identifier) =>
    axios.get(`${BASE}/admin/cloud-accounts/${provider}/${encodeURIComponent(identifier)}/permissions`, { headers: headers() }).then(r => r.data),

  // Sidebar tab visibility — SOLELY User-Group-based (plus admins always
  // see everything). `getMyTabAccess` is called once per session by any
  // logged-in user to resolve which tabs THEY can see (sidebar + route
  // guard in App.jsx).
  getMyTabAccess: () => axios.get(`${BASE}/settings/my-tab-access`, { headers: headers() }).then(r => r.data),

  // Organisation-level settings (name, key AWS account IDs, which optional
  // integrations are switched on). See backend/app/org_config.py.
  getOrgConfig: () => axios.get(`${BASE}/org-config`, { headers: headers() }).then(r => r.data),

  // Admin — raw data export (jobs / users / KB / full dump), JSON or CSV
  adminExportJson: (dataset) =>
    axios.get(`${BASE}/admin/export/${dataset}`, { headers: headers() }).then(r => r.data),
  // CSV comes back as a file download — request as a blob so the browser
  // gets the raw bytes (not axios trying to JSON.parse a CSV string), then
  // trigger a save via a temporary <a download> link.
  adminExportCsv: async (dataset) => {
    const resp = await axios.get(`${BASE}/admin/export/${dataset}`, {
      params: { format: 'csv' }, headers: headers(), responseType: 'blob',
    })
    const disposition = resp.headers['content-disposition'] || ''
    const match = disposition.match(/filename="([^"]+)"/)
    const filename = match ? match[1] : `${dataset}-export.csv`
    const url = window.URL.createObjectURL(new Blob([resp.data], { type: 'text/csv' }))
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.URL.revokeObjectURL(url)
  },

  // Meta
  getOptions: () => axios.get(`${BASE}/meta/options`, { headers: headers() }).then(r => r.data),
  dashboardStats: () => axios.get(`${BASE}/dashboard/stats`, { headers: headers() }).then(r => r.data),

  // Jobs
  listJobs: (params) => axios.get(`${BASE}/jobs`, { params, headers: headers() }).then(r => r.data),
  getJob: (id) => axios.get(`${BASE}/jobs/${id}`, { headers: headers() }).then(r => r.data),
  createJob: (body) => axios.post(`${BASE}/jobs`, body, { headers: headers() }).then(r => r.data),
  updateJob: (id, body) => axios.patch(`${BASE}/jobs/${id}`, body, { headers: headers() }).then(r => r.data),
  // Creator-or-admin only (enforced server-side) — for jobs raised by mistake.
  deleteJob: (id) => axios.delete(`${BASE}/jobs/${id}`, { headers: headers() }).then(r => r.data),
  changeStatus: (id, new_status) =>
    axios.post(`${BASE}/jobs/${id}/status`, { new_status }, { headers: headers() }).then(r => r.data),
  getTransitions: (id) =>
    axios.get(`${BASE}/jobs/${id}/transitions`, { headers: headers() }).then(r => r.data),

  // AI
  analyseJob: (id) => axios.post(`${BASE}/jobs/${id}/analyse`, {}, { headers: headers() }).then(r => r.data),

  // Escalation
  escalate: (id, action, ai_draft) =>
    axios.post(`${BASE}/jobs/${id}/escalate`, { action, ai_draft }, { headers: headers() }).then(r => r.data),
  refreshAmsStatus: (id) =>
    axios.post(`${BASE}/jobs/${id}/refresh-ams-status`, {}, { headers: headers() }).then(r => r.data),

  // AWS Support / AMS Cases (All Cases page) — lists cases across both
  // accounts, shows Job link if any, and lets the user reply to a case.
  listCases: () => axios.get(`${BASE}/cases`, { headers: headers() }).then(r => r.data),
  // Create a new AWS case directly (no Job needed) — case_type is
  // "aws_support" | "aws_ams_sr" | "aws_ams_incident".
  // attachmentSetId (optional) comes from uploadCaseAttachments() below.
  createCase: (caseType, subject, description, category, priority, attachmentSetId) =>
    axios.post(`${BASE}/cases`, {
      case_type: caseType, subject, description, category, priority,
      ...(attachmentSetId ? { attachment_set_id: attachmentSetId } : {}),
    }, { headers: headers() }).then(r => r.data),
  // `source` is only set for native Security Incident Response cases
  // ('security_ir'), which the backend reads from the security-ir API
  // instead of the Support API.
  getCase: (caseId, account, source) =>
    axios.get(`${BASE}/cases/${caseId}`, { params: { account, ...(source ? { source } : {}) }, headers: headers() }).then(r => r.data),
  // Comment on a native Security Incident Response case (text only). The
  // backend requires the "cases" tab write grant and prefixes the Portal
  // user's email into the comment, since AWS only sees the shared IAM user.
  replyToSecurityIrCase: (caseId, body) =>
    axios.post(`${BASE}/cases/${caseId}/security-ir-reply`, { body }, { headers: headers() }).then(r => r.data),
  replyToCase: (caseId, account, body, attachmentSetId) =>
    axios.post(`${BASE}/cases/${caseId}/reply`, {
      account, body, ...(attachmentSetId ? { attachment_set_id: attachmentSetId } : {}),
    }, { headers: headers() }).then(r => r.data),
  // Upload up to 3 files (5 MB each — AWS Support's own limit) to a new
  // or existing attachment set. `account` is "own" | "ams" and must match
  // the account the resulting case/reply will be created against. Pass
  // the returned attachment_set_id into createCase()/replyToCase().
  // Uses fetch() with credentials:'omit' — same reason as extractFromFile.
  uploadCaseAttachments: async (account, fileList, attachmentSetId) => {
    const form = new FormData()
    form.append('account', account)
    if (attachmentSetId) form.append('attachment_set_id', attachmentSetId)
    for (const file of fileList) form.append('files', file)
    const resp = await fetch(`${BASE}/cases/attachments`, {
      method: 'POST',
      headers: { 'X-Actor-Email': getActor() },
      body: form,
      credentials: 'omit',
    })
    if (!resp.ok) {
      const text = await resp.text().catch(() => resp.statusText)
      let detail = text
      try { detail = JSON.parse(text)?.detail || text } catch { /* not JSON */ }
      throw new Error(detail || `Upload failed (${resp.status})`)
    }
    return resp.json()
  },

  // Cost Analysis tab — org-wide MTD + previous-month AWS spend, one row
  // per account (Core account direct + all others via AssumeRole readonly).
  // `signal` (optional AbortSignal) lets the caller cancel an in-flight
  // request — e.g. when a newer refresh supersedes it, or the component
  // unmounts before the response arrives.
  getOrgCostAnalysis: (refresh = false, signal) =>
    axios.get(`${BASE}/cost-analysis/organization`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  // On-demand AI executive summary ("AI Analysis" button) — trend, key
  // cost drivers, and cost-control recommendations for senior management.
  getCostAnalysisAiSummary: (refresh = false, signal) =>
    axios.get(`${BASE}/cost-analysis/ai-summary`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),

  // Manual cost adjustments (extra fee/cost line items) — viewable by
  // any logged-in user, but create/update/delete are admin-only
  // (enforced server-side too; the buttons are just hidden client-side
  // for non-admins). These are layered on top of live Cost Explorer data
  // as a separate "adjustments" bucket — never mixed into the real
  // AWS-reported usage/marketplace/shield/support/credits numbers.
  listCostAdjustments: (accountId) =>
    axios.get(`${BASE}/cost-analysis/adjustments`, {
      params: accountId ? { account_id: accountId } : {}, headers: headers(),
    }).then(r => r.data),
  createCostAdjustment: (body) =>
    axios.post(`${BASE}/cost-analysis/adjustments`, body, { headers: headers() }).then(r => r.data),
  updateCostAdjustment: (id, body) =>
    axios.patch(`${BASE}/cost-analysis/adjustments/${id}`, body, { headers: headers() }).then(r => r.data),
  deleteCostAdjustment: (id) =>
    axios.delete(`${BASE}/cost-analysis/adjustments/${id}`, { headers: headers() }).then(r => r.data),

  // Billing by Service — per-account, per-month AWS spend by service,
  // matching the Billing Console's "Charges by service" view (ported from
  // the standalone billing_by_service.py script). Read-only for everyone.
  getBillingByServiceAccounts: () =>
    axios.get(`${BASE}/billing-by-service/accounts`, { headers: headers() }).then(r => r.data),
  getBillingByServiceReport: (months, accountIds, refresh = false, signal) =>
    axios.get(`${BASE}/billing-by-service/report`, {
      params: {
        months: months.join(','),
        ...(accountIds && accountIds.length ? { accounts: accountIds.join(',') } : {}),
        ...(refresh ? { refresh: true } : {}),
      },
      headers: headers(), signal,
    }).then(r => r.data),
  // On-demand AI executive summary — the Billing by Service counterpart
  // to getCostAnalysisAiSummary above. Used by the Dashboard's automatic
  // "Executive Summary" section (called with no params = current month,
  // all accounts) and can also be wired to an on-page "AI Analysis"
  // button the same way Cost Analysis does.
  getBillingByServiceAiSummary: (months, accountIds, refresh = false, signal) =>
    axios.get(`${BASE}/billing-by-service/ai-summary`, {
      params: {
        ...(months && months.length ? { months: months.join(',') } : {}),
        ...(accountIds && accountIds.length ? { accounts: accountIds.join(',') } : {}),
        ...(refresh ? { refresh: true } : {}),
      },
      headers: headers(), signal,
    }).then(r => r.data),

  // Excel export — comes back as a binary file download; request as a
  // blob (same pattern as adminExportCsv above) then trigger a save via a
  // temporary <a download> link.
  exportBillingByServiceXlsx: async (months, accountIds, refresh = false) => {
    const resp = await axios.get(`${BASE}/billing-by-service/export`, {
      params: {
        months: months.join(','),
        ...(accountIds && accountIds.length ? { accounts: accountIds.join(',') } : {}),
        ...(refresh ? { refresh: true } : {}),
      },
      headers: headers(), responseType: 'blob',
    })
    const disposition = resp.headers['content-disposition'] || ''
    const match = disposition.match(/filename="([^"]+)"/)
    const filename = match ? match[1] : 'billing-by-service.xlsx'
    const url = window.URL.createObjectURL(new Blob([resp.data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }))
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.URL.revokeObjectURL(url)
  },

  // RI & Savings Plan Saved Cost — per-account, per-month realized savings
  // from Reserved Instances and Savings Plans, straight from Cost
  // Explorer's own commitment-utilization APIs. Read-only for everyone.
  getRiSpSavingsAccounts: () =>
    axios.get(`${BASE}/ri-sp-savings/accounts`, { headers: headers() }).then(r => r.data),
  getRiSpSavingsReport: (months, accountIds, refresh = false, signal) =>
    axios.get(`${BASE}/ri-sp-savings/report`, {
      params: {
        months: months.join(','),
        ...(accountIds && accountIds.length ? { accounts: accountIds.join(',') } : {}),
        ...(refresh ? { refresh: true } : {}),
      },
      headers: headers(), signal,
    }).then(r => r.data),
  // Reseller On-Demand Cost comparison — an admin manually types in what
  // the reseller invoiced for on-demand cost for one AWS account for a
  // given month (per-account, since different accounts in this org can
  // be on different reseller contracts). Everyone can view the resulting
  // diff/discount-% figures (attached server-side to each account row —
  // account.reseller_comparison — plus an aggregate on the month's
  // totals.reseller_comparison), but only an admin can set/clear the
  // underlying figure (enforced server-side too).
  getRiSpSavingsResellerCost: (accountId, month) =>
    axios.get(`${BASE}/ri-sp-savings/reseller-cost`, { params: { account_id: accountId, month }, headers: headers() }).then(r => r.data),
  setRiSpSavingsResellerCost: (accountId, month, amount, currency = 'USD') =>
    axios.put(`${BASE}/ri-sp-savings/reseller-cost`, { amount, currency }, { params: { account_id: accountId, month }, headers: headers() }).then(r => r.data),
  deleteRiSpSavingsResellerCost: (accountId, month) =>
    axios.delete(`${BASE}/ri-sp-savings/reseller-cost`, { params: { account_id: accountId, month }, headers: headers() }).then(r => r.data),

  // AWS Health Dashboard (Your organization health) — Open/recent issues,
  // Scheduled changes, Other notifications, Event log — fetched via the
  // AMS cross-account credentials (only that account has org-wide access).
  getOrgHealthDashboard: (refresh = false) =>
    axios.get(`${BASE}/health-dashboard/organization`, {
      params: refresh ? { refresh: true } : {}, headers: headers(),
    }).then(r => r.data),
  // Drill-down for one event (Affected accounts / Affected resources),
  // shown in a modal when the user clicks the event name.
  getHealthEventDetail: (eventArn) =>
    axios.get(`${BASE}/health-dashboard/organization/event-detail`, {
      params: { event_arn: eventArn }, headers: headers(),
    }).then(r => r.data),
  // On-demand AI suggestion ("what to do / be aware of") for one event —
  // only called when the engineer explicitly asks for it in the popup.
  getHealthEventAiSuggestion: (eventArn) =>
    axios.get(`${BASE}/health-dashboard/organization/event-ai-suggestion`, {
      params: { event_arn: eventArn }, headers: headers(),
    }).then(r => r.data),
  // Tick/untick one row of the "Affected resources" table inside the
  // event-detail popup — one by one, independent of "finished".
  setHealthEventResourceCheck: (eventArn, resourceKey, checked) =>
    axios.post(`${BASE}/health-dashboard/organization/resource-check`,
      { event_arn: eventArn, resource_key: resourceKey, checked }, { headers: headers() }).then(r => r.data),
  // Tick/untick the checkbox for an event — records who + when.
  setHealthEventCheck: (eventArn, checked) =>
    axios.post(`${BASE}/health-dashboard/organization/check`,
      { event_arn: eventArn, checked }, { headers: headers() }).then(r => r.data),
  // Free-text remark shown next to "Last Updated" in all 4 tabs.
  setHealthEventRemark: (eventArn, remark) =>
    axios.post(`${BASE}/health-dashboard/organization/remark`,
      { event_arn: eventArn, remark }, { headers: headers() }).then(r => r.data),
  // "Mark as finished" checkbox inside the event-detail popup.
  setHealthEventFinished: (eventArn, finished) =>
    axios.post(`${BASE}/health-dashboard/organization/finish`,
      { event_arn: eventArn, finished }, { headers: headers() }).then(r => r.data),
  // Admin-only: drag-and-drop override — force this event into a
  // different tab than its automatic AWS-classified category. Pass
  // category=null to clear the override.
  setHealthEventCategory: (eventArn, category) =>
    axios.post(`${BASE}/health-dashboard/organization/category-override`,
      { event_arn: eventArn, category }, { headers: headers() }).then(r => r.data),
  // One-click "Raise SR to AMS" directly from a Health event row.
  raiseAmsSrForHealthEvent: (event) =>
    axios.post(`${BASE}/health-dashboard/organization/raise-sr`, {
      event_arn: event.arn,
      service: event.service,
      event_type_code: event.event_type_code,
      event_type_category: event.event_type_category,
      region: event.region,
      status_code: event.status_code,
    }, { headers: headers() }).then(r => r.data),
  // One-click "Create Job" directly from a Health event row — for
  // events that need engineer follow-up, independent of whether an AMS
  // SR was also raised for the same event.
  createJobFromHealthEvent: (event) =>
    axios.post(`${BASE}/health-dashboard/organization/create-job`, {
      event_arn: event.arn,
      service: event.service,
      event_type_code: event.event_type_code,
      event_type_category: event.event_type_category,
      region: event.region,
      status_code: event.status_code,
    }, { headers: headers() }).then(r => r.data),

  // AI Extract from file upload
  // Uses fetch() with credentials:'omit' to ensure the ALB session cookie
  // is NOT sent. This forces the ALB priority-8 bypass rule path (forward
  // without OIDC). Auth is handled by X-Actor-Email header in auth_alb.py.
  extractFromFile: async (file) => {
    const form = new FormData()
    form.append('file', file)
    const resp = await fetch(`${BASE}/extract/file`, {
      method: 'POST',
      headers: { 'X-Actor-Email': getActor() },
      body: form,
      credentials: 'omit',
    })
    if (!resp.ok) {
      const text = await resp.text().catch(() => resp.statusText)
      throw new Error(`Upload failed (${resp.status}): ${text}`)
    }
    return resp.json()
  },

  // AI Extract from pasted email text
  extractFromEmail: (text) =>
    axios.post(`${BASE}/extract/email`, { text }, { headers: headers() }).then(r => r.data),

  // AI Analysis preview before job submission
  analysePreview: (title, description, category, priority) =>
    axios.post(`${BASE}/analyse/preview`, { title, description, category, priority }, { headers: headers() }).then(r => r.data),

  // Knowledge Base (RAG)
  kbUpload: async (file, description = '') => {
    const form = new FormData()
    form.append('file', file)
    if (description) form.append('description', description)
    // fetch() with credentials:'omit' — same reason as extractFromFile.
    const resp = await fetch(`${BASE}/kb/upload`, {
      method: 'POST',
      headers: { 'X-Actor-Email': getActor() },
      body: form,
      credentials: 'omit',
    })
    if (!resp.ok) {
      const text = await resp.text().catch(() => resp.statusText)
      throw new Error(`Upload failed (${resp.status}): ${text}`)
    }
    return resp.json()
  },
  kbList: () => axios.get(`${BASE}/kb/documents`, { headers: headers() }).then(r => r.data),
  kbDelete: (id) => axios.delete(`${BASE}/kb/documents/${id}`, { headers: headers() }).then(r => r.data),

  // Job notes
  addNote: (jobId, text) =>
    axios.post(`${BASE}/jobs/${jobId}/notes`, { text }, { headers: headers() }).then(r => r.data),
  deleteNote: (jobId, noteId) =>
    axios.delete(`${BASE}/jobs/${jobId}/notes/${noteId}`, { headers: headers() }).then(r => r.data),
  // Note attachments (photos pasted/uploaded + arbitrary files) — stored
  // in S3, never inline base64. Uses fetch() with credentials:'omit',
  // same reason as extractFromFile/kbUpload/uploadCaseAttachments — this
  // route is on the ALB's OIDC-bypass rule (compute_stack.py's
  // CasesBypassRule) because of the cross-site SameSite cookie issue on
  // multipart POST.
  addNoteAttachments: async (jobId, noteId, fileList) => {
    const form = new FormData()
    for (const file of fileList) form.append('files', file)
    const resp = await fetch(`${BASE}/jobs/${jobId}/notes/${noteId}/attachments`, {
      method: 'POST',
      headers: { 'X-Actor-Email': getActor() },
      body: form,
      credentials: 'omit',
    })
    if (!resp.ok) {
      const text = await resp.text().catch(() => resp.statusText)
      let detail = text
      try { detail = JSON.parse(text)?.detail || text } catch { /* not JSON */ }
      throw new Error(detail || `Upload failed (${resp.status})`)
    }
    return resp.json()
  },
  deleteNoteAttachment: (jobId, noteId, attachmentId) =>
    axios.delete(`${BASE}/jobs/${jobId}/notes/${noteId}/attachments/${attachmentId}`, { headers: headers() }).then(r => r.data),
  // Fresh short-lived presigned S3 URL for viewing (inline <img>) or
  // downloading one attachment — fetched on demand, never cached, since
  // the URL expires after a few minutes.
  getNoteAttachmentUrl: (jobId, noteId, attachmentId) =>
    axios.get(`${BASE}/jobs/${jobId}/notes/${noteId}/attachments/${attachmentId}/url`, { headers: headers() }).then(r => r.data),

  // Chatbot
  chat: (message, history = []) =>
    axios.post(`${BASE}/chat`, { message, history }, { headers: headers() }).then(r => r.data),

  // AI Assistant — standalone sidebar tab, general Q&A only (no KB/job
  // context, no AWS resource access). Separate backend route from the
  // floating widget's chat() above.
  aiAssistantChat: (message, history = []) =>
    axios.post(`${BASE}/ai-assistant/chat`, { message, history }, { headers: headers() }).then(r => r.data),

  // AWS DevOps Agent — forwards to a dedicated AWS DevOps Agent Agent
  // Space via A2A (see app/devops_agent.py). No conversation history is
  // sent — the Agent Space's own chat/task history is server-side and
  // tied to the access token, not to this request.
  //
  // Async task model (2026-09-14 redesign — fixes the earlier "Network
  // Error" AND adds support for the "investigate" skill, which AWS
  // documents as taking 5-8 minutes): this call itself is now always
  // fast, whether the underlying A2A task finished immediately or not.
  // Response shape: {done, task_id, answer}.
  //   - done=true: `answer` is the final text, show it right away.
  //   - done=false: no answer yet (e.g. a long "investigate" run just
  //     started) — the caller must poll getDevOpsAgentTaskStatus(task_id)
  //     on a timer until it returns done=true. See DevOpsAgentPage.jsx
  //     for the polling loop.
  // A short 30s timeout is enough headroom for this single fast call —
  // there's no long-lived request left in this design to protect
  // against the ALB's idle_timeout.
  devopsAgentChat: (message) =>
    axios.post(`${BASE}/devops-agent/chat`, { message }, { headers: headers(), timeout: 30000 }).then(r => r.data),

  // Poll for completion of a DevOps Agent task started by
  // devopsAgentChat() (used when that call returns done=false). Same
  // response shape: {done, task_id, answer}.
  getDevOpsAgentTaskStatus: (taskId) =>
    axios.get(`${BASE}/devops-agent/tasks/${taskId}`, { headers: headers(), timeout: 30000 }).then(r => r.data),

  // AWS Security Hub — active findings across all accounts + AI analysis
  // and remediation plan. Read-only for everyone.
  getSecurityHubAccounts: () =>
    axios.get(`${BASE}/security-hub/accounts`, { headers: headers() }).then(r => r.data),
  getSecurityHubSummary: (accountIds, refresh = false, signal) =>
    axios.get(`${BASE}/security-hub/summary`, {
      params: {
        ...(accountIds && accountIds.length ? { accounts: accountIds.join(',') } : {}),
        ...(refresh ? { refresh: true } : {}),
      },
      headers: headers(), signal,
    }).then(r => r.data),
  getSecurityHubAiSummary: (accountIds, refresh = false, signal) =>
    axios.get(`${BASE}/security-hub/ai-summary`, {
      params: {
        ...(accountIds && accountIds.length ? { accounts: accountIds.join(',') } : {}),
        ...(refresh ? { refresh: true } : {}),
      },
      headers: headers(), signal,
    }).then(r => r.data),
  // Per-finding AI analysis — auto-triggered when the user clicks a
  // specific finding row (no separate button for this one).
  getSecurityHubFindingAiAnalysis: (accountId, findingId, signal) =>
    axios.get(`${BASE}/security-hub/finding-ai-analysis`, {
      params: { account_id: accountId, finding_id: findingId },
      headers: headers(), signal,
    }).then(r => r.data),

  // AWS WAF Rule Monitoring — per-account Web ACLs, associated resources,
  // and per-rule Blocked/Counted/Allowed request counts (CloudWatch
  // AWS/WAFV2 metrics). Same account superset + cross-account pattern as
  // Security Hub / Billing by Service. Read-only for everyone.
  getWafMonitoringAccounts: () =>
    axios.get(`${BASE}/waf-monitoring/accounts`, { headers: headers() }).then(r => r.data),
  getWafMonitoringSummary: (accountIds, refresh = false, signal) =>
    axios.get(`${BASE}/waf-monitoring/summary`, {
      params: {
        ...(accountIds && accountIds.length ? { accounts: accountIds.join(',') } : {}),
        ...(refresh ? { refresh: true } : {}),
      },
      headers: headers(), signal,
    }).then(r => r.data),
  // On-demand Web-ACL-level aggregate ("Rule=ALL") Blocked/Counted/
  // Allowed CloudWatch time series — fetched only when a user expands a
  // Web ACL card's Metrics chart (never eagerly / on page load).
  getWafMonitoringWebAclMetrics: (accountId, webAclName, webAclId, scope, timeRange, signal) =>
    axios.get(`${BASE}/waf-monitoring/web-acl-metrics`, {
      params: { account_id: accountId, web_acl_name: webAclName, web_acl_id: webAclId, scope, time_range: timeRange },
      headers: headers(), signal,
    }).then(r => r.data),
  // Same time series scoped to one specific rule — fetched only when a
  // user clicks into that rule's own detail view.
  getWafMonitoringRuleMetrics: (accountId, webAclName, webAclId, scope, ruleName, timeRange, signal) =>
    axios.get(`${BASE}/waf-monitoring/rule-metrics`, {
      params: { account_id: accountId, web_acl_name: webAclName, web_acl_id: webAclId, scope, rule_name: ruleName, time_range: timeRange },
      headers: headers(), signal,
    }).then(r => r.data),
  // "By Action" tab — ACCOUNT-LEVEL: pick one account + one action
  // (Block/Allow/Count/CAPTCHA/Challenge), no Web ACL picker. Returns
  // one account-wide total for that action plus a combined "impacted
  // rules" list (each tagged with which Web ACL it came from). Fetched
  // only once BOTH an account and an action have been explicitly
  // selected — never eagerly, never for all five actions at once.
  getWafMonitoringActionBreakdown: (accountId, action, timeRange, signal) =>
    axios.get(`${BASE}/waf-monitoring/action-breakdown`, {
      params: { account_id: accountId, action, time_range: timeRange },
      headers: headers(), signal,
    }).then(r => r.data),
  // Live sample of recently-matched requests for one rule inside one Web
  // ACL — every action (Block/Allow/Count/CAPTCHA/Challenge), not just
  // blocked. The heaviest/most "log-like" call; fetched automatically as
  // soon as a rule row is expanded (no extra click). AWS's
  // GetSampledRequests only ever covers roughly the last 3 hours,
  // regardless of any time_range picked elsewhere on the page.
  getWafMonitoringSampledRequests: (accountId, webAclName, webAclId, scope, ruleName, signal) =>
    axios.get(`${BASE}/waf-monitoring/sampled-requests`, {
      params: { account_id: accountId, web_acl_name: webAclName, web_acl_id: webAclId, scope, rule_name: ruleName },
      headers: headers(), signal,
    }).then(r => r.data),

  // Lifecycle > Patching > OS — see backend/app/patching.py. Everyone can read;
  // create / edit / delete are limited to admins + Cloud Infra / Security /
  // Management Team (403 otherwise).
  getPatchingOsPlans: (signal) =>
    axios.get(`${BASE}/patching/os/plans`, { headers: headers(), signal }).then(r => r.data),
  getPatchingOsInstances: (refresh = false, signal) =>
    axios.get(`${BASE}/patching/os/instances`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  createPatchingOsPlan: (body) =>
    axios.post(`${BASE}/patching/os/plans`, body, { headers: headers() }).then(r => r.data),
  updatePatchingOsPlan: (id, body) =>
    axios.put(`${BASE}/patching/os/plans/${encodeURIComponent(id)}`, body, { headers: headers() }).then(r => r.data),
  deletePatchingOsPlan: (id) =>
    axios.delete(`${BASE}/patching/os/plans/${encodeURIComponent(id)}`, { headers: headers() }).then(r => r.data),
  createPatchingOsGroup: (name) =>
    axios.post(`${BASE}/patching/os/groups`, { name }, { headers: headers() }).then(r => r.data),
  renamePatchingOsGroup: (id, name) =>
    axios.put(`${BASE}/patching/os/groups/${encodeURIComponent(id)}`, { name }, { headers: headers() }).then(r => r.data),
  deletePatchingOsGroup: (id) =>
    axios.delete(`${BASE}/patching/os/groups/${encodeURIComponent(id)}`, { headers: headers() }).then(r => r.data),
  createPatchingOsMeeting: (name, url) =>
    axios.post(`${BASE}/patching/os/meetings`, { name, url }, { headers: headers() }).then(r => r.data),
  updatePatchingOsMeeting: (id, name, url) =>
    axios.put(`${BASE}/patching/os/meetings/${encodeURIComponent(id)}`, { name, url }, { headers: headers() }).then(r => r.data),
  deletePatchingOsMeeting: (id) =>
    axios.delete(`${BASE}/patching/os/meetings/${encodeURIComponent(id)}`, { headers: headers() }).then(r => r.data),

  // Website Monitoring (Monitoring section) — see backend/app/website_monitor.py.
  // Status is readable by everyone; add/remove are admin-only (403 otherwise).
  getWebsiteMonitoringStatus: (refresh = false, signal) =>
    axios.get(`${BASE}/website-monitoring/status`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  addWebsiteMonitoringSite: (name, url) =>
    axios.post(`${BASE}/website-monitoring/sites`, { name, url }, { headers: headers() }).then(r => r.data),
  removeWebsiteMonitoringSite: (siteId) =>
    axios.delete(`${BASE}/website-monitoring/sites/${encodeURIComponent(siteId)}`, { headers: headers() }).then(r => r.data),

  // Local Infra Hardware Monitor — Cisco Meraki AP monitoring dashboard.
  // See backend/app/meraki_monitor.py. Read-only for everyone.
  getMerakiAccessPoints: (refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/access-points`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  getMerakiOverview: (refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/overview`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  getMerakiAvailabilityHistory: (timespan = 30 * 24 * 3600, refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/availability-history`, {
      params: { timespan, ...(refresh ? { refresh: true } : {}) }, headers: headers(), signal,
    }).then(r => r.data),
  // AP Network Monitoring tab's period-select download — same
  // blob-then-<a download> pattern as exportBillingByServiceXlsx above.
  // `start`/`end` are ISO8601 timestamps; `format` is 'xlsx' or 'csv'.
  exportMerakiAvailabilityHistory: async (start, end, format = 'xlsx', site = '', status = '') => {
    const resp = await axios.get(`${BASE}/meraki-monitor/availability-history/export`, {
      params: {
        start, end, format,
        ...(site ? { site } : {}),
        ...(status ? { status } : {}),
      },
      headers: headers(), responseType: 'blob',
    })
    const disposition = resp.headers['content-disposition'] || ''
    const match = disposition.match(/filename="([^"]+)"/)
    const filename = match ? match[1] : `ap-network-monitoring.${format}`
    const mimeType = format === 'csv' ? 'text/csv' : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    const url = window.URL.createObjectURL(new Blob([resp.data], { type: mimeType }))
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.URL.revokeObjectURL(url)
  },
  getMerakiChannelUtilizationByNetwork: (timespan = 3600, refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/channel-utilization/by-network`, {
      params: { timespan, ...(refresh ? { refresh: true } : {}) }, headers: headers(), signal,
    }).then(r => r.data),
  getMerakiChannelUtilizationByDevice: (timespan = 3600, refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/channel-utilization/by-device`, {
      params: { timespan, ...(refresh ? { refresh: true } : {}) }, headers: headers(), signal,
    }).then(r => r.data),
  getMerakiChannelUtilizationChangeByNetwork: (timespan = 7200, interval = 3600, refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/channel-utilization/change/by-network`, {
      params: { timespan, interval, ...(refresh ? { refresh: true } : {}) }, headers: headers(), signal,
    }).then(r => r.data),
  getMerakiChannelUtilizationChangeByDevice: (timespan = 7200, interval = 3600, refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/channel-utilization/change/by-device`, {
      params: { timespan, interval, ...(refresh ? { refresh: true } : {}) }, headers: headers(), signal,
    }).then(r => r.data),
  getMerakiPacketLossByNetwork: (timespan = 3600, refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/packet-loss/by-network`, {
      params: { timespan, ...(refresh ? { refresh: true } : {}) }, headers: headers(), signal,
    }).then(r => r.data),
  getMerakiTopNetworksByClients: (refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/top-networks-by-clients`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  getMerakiTopDevicesByClients: (refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/top-devices-by-clients`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  getMerakiTopLatencyNetworks: (timespan = 3600, refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/latency/top-networks`, {
      params: { timespan, ...(refresh ? { refresh: true } : {}) }, headers: headers(), signal,
    }).then(r => r.data),
  getMerakiEvents: (networkId = null, perPage = 100, refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/events`, {
      params: { ...(networkId ? { network_id: networkId } : {}), per_page: perPage, ...(refresh ? { refresh: true } : {}) },
      headers: headers(), signal,
    }).then(r => r.data),
  getMerakiNetworks: (refresh = false, signal) =>
    axios.get(`${BASE}/meraki-monitor/networks`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),

  // SonicWall sub-tab (still part of "Local Infra Hardware Monitor" —
  // see backend/app/sonicwall_monitor.py). Read-only for everyone.
  getSonicWallOverview: (refresh = false, signal) =>
    axios.get(`${BASE}/sonicwall-monitor/overview`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  getSonicWallTenants: (refresh = false, signal) =>
    axios.get(`${BASE}/sonicwall-monitor/tenants`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),

  // Fortigate Firewall sub-tab (still part of "Local Infra Hardware
  // Monitor" — see backend/app/fortigate_monitor.py). Read-only for
  // everyone, same as the Meraki/SonicWall sub-tabs above.
  getFortigateOverview: (refresh = false, signal) =>
    axios.get(`${BASE}/fortigate-monitor/overview`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  getFortigateDevices: (refresh = false, signal) =>
    axios.get(`${BASE}/fortigate-monitor/devices`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  // Per-device live detail (CPU/Memory, Interface Bandwidth, VPN Tunnel
  // status) — fetched on demand only when a device row is expanded, NOT
  // for every device on page load (see backend/app/fortigate_monitor.py's
  // "Per-device live detail" section docstring).
  getFortigateDeviceDetail: (deviceName, refresh = false, signal) =>
    axios.get(`${BASE}/fortigate-monitor/devices/${encodeURIComponent(deviceName)}/detail`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  // FortiSwitch sub-tab (separate from Fortigate Firewall, per user
  // request) — switches attached via FortiLink through a parent
  // FortiGate, see backend/app/fortigate_monitor.py's "FortiSwitches"
  // section docstring.
  getFortigateSwitchOverview: (refresh = false, signal) =>
    axios.get(`${BASE}/fortigate-monitor/switches/overview`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  getFortigateSwitches: (refresh = false, signal) =>
    axios.get(`${BASE}/fortigate-monitor/switches`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),

  // AWS Resources — inventory sourced from the resource-inventory-scanner
  // S3 bucket (account 100000000001 / Core account). Read-only for everyone.
  getAwsResourcesAccounts: () =>
    axios.get(`${BASE}/aws-resources/accounts`, { headers: headers() }).then(r => r.data),
  getAwsResourcesSummary: (accountIds, refresh = false, signal) =>
    axios.get(`${BASE}/aws-resources/summary`, {
      params: {
        ...(accountIds && accountIds.length ? { accounts: accountIds.join(',') } : {}),
        ...(refresh ? { refresh: true } : {}),
      },
      headers: headers(), signal,
    }).then(r => r.data),
  getAwsResourcesData: (resourceType, accountIds, refresh = false, signal) =>
    axios.get(`${BASE}/aws-resources/data`, {
      params: {
        resource_type: resourceType,
        ...(accountIds && accountIds.length ? { accounts: accountIds.join(',') } : {}),
        ...(refresh ? { refresh: true } : {}),
      },
      headers: headers(), signal,
    }).then(r => r.data),

  // Azure Resources — read-only inventory + cost dashboard for this org's
  // Azure subscription (see backend/app/azure_monitor.py). Its own
  // independent sidebar category, mirroring AWS Resources' single-
  // subscription (no multi-account picker needed — there's only one
  // Azure subscription configured) resource-type-tab shape.
  getAzureResourceTypes: () =>
    axios.get(`${BASE}/azure-resources/resource-types`, { headers: headers() }).then(r => r.data),
  getAzureResourcesData: (resourceType, refresh = false, signal) =>
    axios.get(`${BASE}/azure-resources/data`, {
      params: { resource_type: resourceType, ...(refresh ? { refresh: true } : {}) },
      headers: headers(), signal,
    }).then(r => r.data),

  // Freshservice Change Calendar (external tool) — proxied through our own
  // backend so the calendar's separate login credentials never reach the
  // browser. See backend/app/fs_calendar.py.
  fsCalendarDashboard: (year, month, refresh = false) =>
    axios.get(`${BASE}/fs-calendar/dashboard`, {
      params: refresh ? { year, month, refresh: true } : { year, month }, headers: headers(),
    }).then(r => r.data),
  // Security Team Portal (external tool) — read-only data proxied through
  // our own backend so its API key never reaches the browser. `resource` is
  // one of the allow-listed names in backend/app/security_portal.py, e.g.
  // 'aws/alarms' or 'securityhub/report'. Returns { ok, data, cachedAt,
  // fromCache, cache_age_seconds }.
  securityPortal: (resource, refresh = false, signal) =>
    axios.get(`${BASE}/security-portal/${resource}`, {
      params: refresh ? { refresh: true } : {}, headers: headers(), signal,
    }).then(r => r.data),
  // Worldwide Public Holidays tab — see backend/app/world_holidays.py.
  worldHolidayRegions: () =>
    axios.get(`${BASE}/world-holidays/regions`, { headers: headers() }).then(r => r.data),
  worldHolidays: (year, regionKeys, refresh = false) =>
    axios.get(`${BASE}/world-holidays`, {
      params: refresh ? { year, regions: regionKeys.join(','), refresh: true } : { year, regions: regionKeys.join(',') },
      headers: headers(),
    }).then(r => r.data),
  fsCalendarChanges: (year, month, refresh = false) =>
    axios.get(`${BASE}/fs-calendar/changes`, {
      params: refresh ? { year, month, refresh: true } : { year, month }, headers: headers(),
    }).then(r => r.data),

  // SR Ticket Information (external tool) — proxied through our own
  // backend so its separate login credentials never reach the browser.
  // See backend/app/sr_ticket.py. `filters` mirrors that module's
  // get_tickets() shape: { start, end, serviceItem, workspace, requester,
  // approvalStatus, requestStatus, outstandingOnly }.
  srTicketTickets: (filters = {}, refresh = false) =>
    axios.get(`${BASE}/sr-ticket/tickets`, {
      params: {
        ...(refresh ? { refresh: true } : {}),
        ...(filters.start ? { start: filters.start } : {}),
        ...(filters.end ? { end: filters.end } : {}),
        ...(filters.serviceItem ? { service_item: filters.serviceItem } : {}),
        ...(filters.workspace ? { workspace: filters.workspace } : {}),
        ...(filters.requester ? { requester: filters.requester } : {}),
        ...(filters.approvalStatus ? { approval_status: filters.approvalStatus } : {}),
        ...(filters.requestStatus ? { request_status: filters.requestStatus } : {}),
        ...(filters.outstandingOnly ? { outstanding_only: true } : {}),
      },
      headers: headers(),
    }).then(r => r.data),

  // Feedback & Bug Report (sidebar tab) — any logged-in user can submit
  // either type. Visibility/reply is enforced server-side: "feedback" is
  // a private submitter<->admin thread, "bug" is admin-only (even the
  // reporter loses visibility after submitting). Status change is
  // always admin-only.
  listFeedback: () => axios.get(`${BASE}/feedback`, { headers: headers() }).then(r => r.data),
  createFeedback: (body) => axios.post(`${BASE}/feedback`, body, { headers: headers() }).then(r => r.data),
  updateFeedbackStatus: (id, status) =>
    axios.patch(`${BASE}/feedback/${id}/status`, { status }, { headers: headers() }).then(r => r.data),
  addFeedbackReply: (id, text) =>
    axios.post(`${BASE}/feedback/${id}/replies`, { text }, { headers: headers() }).then(r => r.data),
  deleteFeedback: (id) => axios.delete(`${BASE}/feedback/${id}`, { headers: headers() }).then(r => r.data),
  // AI triage note on a BUG report (admin-only, like bug reports
  // themselves). request = (re)generate in the background, optionally with
  // a chosen model key; get = current note, polled while it is "pending".
  requestFeedbackAiAnalysis: (id, model) =>
    axios.post(`${BASE}/feedback/${id}/ai-analysis`, model ? { model } : {}, { headers: headers() }).then(r => r.data),
  getFeedbackAiAnalysis: (id) =>
    axios.get(`${BASE}/feedback/${id}/ai-analysis`, { headers: headers() }).then(r => r.data),

  // CMDB — live AWS resource inventory (EC2/RDS/Lambda/S3/DynamoDB/Load
  // Balancers) across the whole org, via the same AssumeRole pattern as
  // Cost Analysis / Billing by Service. Read-only for everyone.
  getCmdbInventory: (refresh = false, signal) =>
    axios.get(`${BASE}/cmdb/inventory`, { params: { refresh }, headers: headers(), signal }).then(r => r.data),
  getCmdbHistory: (accountId, limit = 200) =>
    axios.get(`${BASE}/cmdb/history`, { params: { account: accountId, limit }, headers: headers() }).then(r => r.data),
  analyzeCmdbImpact: (resource, accountId, accountName) =>
    axios.post(`${BASE}/cmdb/analyze-impact`, { resource, account_id: accountId, account_name: accountName }, { headers: headers() }).then(r => r.data),
  // Illumio-style per-EC2 network topology diagram — real traffic peers
  // from VPC Flow Logs when available, else a generic animated activity
  // indicator from CloudWatch NetworkIn/NetworkOut. On-demand (no cache).
  getCmdbEc2Topology: (accountId, instanceId) =>
    axios.get(`${BASE}/cmdb/ec2-topology`, { params: { account_id: accountId, instance_id: instanceId }, headers: headers() }).then(r => r.data),
  // Same Illumio-style diagram, extended to RDS database instances.
  getCmdbRdsTopology: (accountId, dbInstanceId) =>
    axios.get(`${BASE}/cmdb/rds-topology`, { params: { account_id: accountId, db_instance_id: dbInstanceId }, headers: headers() }).then(r => r.data),
  // Same Illumio-style diagram, extended to Load Balancers — also
  // includes the LB → TargetGroup → Target service chain.
  getCmdbLbTopology: (accountId, lbName) =>
    axios.get(`${BASE}/cmdb/lb-topology`, { params: { account_id: accountId, lb_name: lbName }, headers: headers() }).then(r => r.data),

  // VPN Profile Lookup (Resources) — given a site's WAN IP, scans the
  // network account (100000000003) for a matching Site-to-Site VPN
  // connection and returns its tunnel/IKE/IPsec/DPD profile, including
  // plaintext pre-shared keys. See app/vpn_lookup.py. No cache — a live
  // on-demand lookup per search.
  vpnLookupSearch: (wanIp) =>
    axios.get(`${BASE}/vpn-lookup/search`, { params: { wan_ip: wanIp }, headers: headers() }).then(r => r.data),

  // Whitelist (LMS) — pick a fixed WAF IPSet / Security Group target,
  // check/add an IP. The AWS region is NOT a parameter here — the backend
  // auto-detects which region the target lives in and returns it as part
  // of the response, purely for display. Every check/add is recorded
  // server-side to an audit log only admins + Cloud Infra/Security/
  // Management Team can view (getWhitelistLmsAuditLog below, 403 for
  // everyone else). See app/whitelist_lms.py.
  getWhitelistLmsTargets: () =>
    axios.get(`${BASE}/whitelist/lms/targets`, { headers: headers() }).then(r => r.data),
  checkWhitelistLmsIp: (targetId, ipAddress) =>
    axios.post(`${BASE}/whitelist/lms/check`, { target_id: targetId, ip_address: ipAddress }, { headers: headers() }).then(r => r.data),
  addWhitelistLmsIp: (targetId, ipAddress, srNumber, port, protocol) =>
    axios.post(`${BASE}/whitelist/lms/add`, {
      target_id: targetId, ip_address: ipAddress, sr_number: srNumber, port, protocol,
    }, { headers: headers() }).then(r => r.data),
  getWhitelistLmsAuditLog: (limit = 500) =>
    axios.get(`${BASE}/whitelist/lms/audit-log`, { params: { limit }, headers: headers() }).then(r => r.data),

  // Whitelist (LMS) — Admin target registry management (add/edit/remove
  // the WAF IPSet / Security Group targets the picker above offers, and
  // resolve-by-name lookups used by the "Add target" forms). Every call
  // below is 403 for non-admins. See app/whitelist_lms.py's
  // "Admin-editable target registry" docstring section.
  getWhitelistLmsTargetsAdmin: () =>
    axios.get(`${BASE}/whitelist/lms/targets/admin`, { headers: headers() }).then(r => r.data),
  resolveWhitelistLmsWafIpset: (ipSetName) =>
    axios.post(`${BASE}/whitelist/lms/targets/resolve-waf-ipset`, { ip_set_name: ipSetName }, { headers: headers() }).then(r => r.data),
  resolveWhitelistLmsSecurityGroup: (groupId) =>
    axios.post(`${BASE}/whitelist/lms/targets/resolve-security-group`, { group_id: groupId }, { headers: headers() }).then(r => r.data),
  addWhitelistLmsWafTarget: (label, ipSetName, wafScope, linkedDomain) =>
    axios.post(`${BASE}/whitelist/lms/targets/waf`, {
      label, ip_set_name: ipSetName, waf_scope: wafScope, linked_domain: linkedDomain || null,
    }, { headers: headers() }).then(r => r.data),
  addWhitelistLmsSgTarget: (label, groupId, linkedDomain, port, protocol) =>
    axios.post(`${BASE}/whitelist/lms/targets/security-group`, {
      label, group_id: groupId, linked_domain: linkedDomain || null, port: port || null, protocol: protocol || null,
    }, { headers: headers() }).then(r => r.data),
  updateWhitelistLmsTarget: (targetId, patch) =>
    axios.patch(`${BASE}/whitelist/lms/targets/${targetId}`, patch, { headers: headers() }).then(r => r.data),
  removeWhitelistLmsTarget: (targetId) =>
    axios.delete(`${BASE}/whitelist/lms/targets/${targetId}`, { headers: headers() }).then(r => r.data),
  restoreWhitelistLmsTarget: (targetId) =>
    axios.post(`${BASE}/whitelist/lms/targets/${targetId}/restore`, {}, { headers: headers() }).then(r => r.data),

  // DNS Record (Security) — browse and edit Route 53 hosted zones in the
  // Core account (100000000001). See app/dns_records.py.
  //
  // The two GETs are open to anyone who can see the tab; all three write
  // calls are 403 for anyone outside admins + Cloud Infra/Security/
  // Management Team (main.py's require_dns_writer), and every write
  // requires an SR number. getDnsAuditLog is 403 for the same set as the
  // Whitelist audit log, so the page hides that panel on failure rather
  // than showing an error — same pattern as WhitelistLmsPage.
  //
  // deleteDnsRecord is a POST, not axios.delete: the request needs a body
  // (record identity + SR number), and a DELETE body is exactly what
  // intermediaries may legally drop. It also takes no ttl/values — the
  // backend re-reads the record's live contents before removing it, so a
  // stale tab can't delete a version other than what was displayed.
  // UI hint only — tells the page whether to render the write controls.
  // Server-side enforcement is independent of this (require_dns_writer).
  getDnsPermissions: () =>
    axios.get(`${BASE}/dns/permissions`, { headers: headers() }).then(r => r.data),
  getDnsZones: () =>
    axios.get(`${BASE}/dns/zones`, { headers: headers() }).then(r => r.data),
  getDnsZoneRecords: (zoneId) =>
    axios.get(`${BASE}/dns/zones/${encodeURIComponent(zoneId)}/records`, { headers: headers() }).then(r => r.data),
  createDnsRecord: (payload) =>
    axios.post(`${BASE}/dns/records`, payload, { headers: headers() }).then(r => r.data),
  modifyDnsRecord: (payload) =>
    axios.put(`${BASE}/dns/records`, payload, { headers: headers() }).then(r => r.data),
  deleteDnsRecord: (payload) =>
    axios.post(`${BASE}/dns/records/delete`, payload, { headers: headers() }).then(r => r.data),
  getDnsAuditLog: (limit = 500) =>
    axios.get(`${BASE}/dns/audit-log`, { params: { limit }, headers: headers() }).then(r => r.data),

  // DNS bulk import — create/modify only, from an uploaded file (CSV/TSV
  // or BIND zone-file syntax, auto-detected server-side; or an .xlsx
  // Excel workbook). The caller reads a CSV/TSV/zone-file pick with
  // FileReader.readAsText() and sends the text as a plain JSON string
  // field, NOT multipart/form-data — see main.py's route comment for why
  // a DNS-mutating endpoint stays off the ALB's multipart-upload bypass
  // rule. .xlsx is binary, so it travels as a SEPARATE base64 field
  // (fileContentB64) instead — exactly one of fileContent/fileContentB64
  // is expected to carry real content per call. Preview makes zero
  // writes; apply re-validates the same content against Route 53's
  // current state before writing, so it never trusts a stale preview.
  previewDnsBulkImport: (zoneId, fileContent, fileContentB64 = null) =>
    axios.post(`${BASE}/dns/bulk/preview`, {
      zone_id: zoneId, file_content: fileContent || '', file_content_b64: fileContentB64 || null,
    }, { headers: headers() }).then(r => r.data),
  applyDnsBulkImport: (zoneId, fileContent, srNumber, fileContentB64 = null) =>
    axios.post(`${BASE}/dns/bulk/apply`, {
      zone_id: zoneId, file_content: fileContent || '', file_content_b64: fileContentB64 || null, sr_number: srNumber,
    }, { headers: headers() }).then(r => r.data),

  // S2S VPN Profile Creation (Security) — wizard that CREATES real
  // Customer Gateways, Site-to-Site VPN connections and Transit Gateway
  // static routes in the network account (100000000003), via
  // sts:AssumeRole(JobPortal-CrossAccount-InfraOps-1000). See
  // app/s2s_vpn_creation.py. Same read/write asymmetry as DNS: the three
  // GETs (config, permissions, listing existing Customer Gateways) are
  // open to anyone who can see the tab; every write call is 403 for
  // anyone outside admins + Cloud Infra/Security Team
  // (main.py's require_vpn_writer), and every write requires an SR
  // number, mirroring DNS's mandatory-change-ticket convention exactly.
  getS2sVpnConfig: () =>
    axios.get(`${BASE}/s2s-vpn/config`, { headers: headers() }).then(r => r.data),
  getS2sVpnPermissions: () =>
    axios.get(`${BASE}/s2s-vpn/permissions`, { headers: headers() }).then(r => r.data),
  listS2sVpnCustomerGateways: (region) =>
    axios.get(`${BASE}/s2s-vpn/customer-gateways`, { params: { region }, headers: headers() }).then(r => r.data),
  createS2sVpnCustomerGateway: (payload) =>
    axios.post(`${BASE}/s2s-vpn/customer-gateways`, payload, { headers: headers() }).then(r => r.data),
  createS2sVpnConnection: (payload) =>
    axios.post(`${BASE}/s2s-vpn/vpn-connections`, payload, { headers: headers() }).then(r => r.data),
  createS2sVpnRoute: (payload) =>
    axios.post(`${BASE}/s2s-vpn/routes`, payload, { headers: headers() }).then(r => r.data),
  getS2sVpnAuditLog: (limit = 500) =>
    axios.get(`${BASE}/s2s-vpn/audit-log`, { params: { limit }, headers: headers() }).then(r => r.data),

  // Helpdesk Team catalogue — S3-ScreenSaver: backend-proxy upload.
  // Single multipart POST straight to the portal's own backend (not
  // directly to S3 — see app/screensaver.py's module docstring for why
  // this replaced the original direct-browser-to-S3 presigned-PUT
  // design: a real user's network/proxy/firewall was blocking outbound
  // connections to *.s3.ap-southeast-1.amazonaws.com, which is outside
  // this team's control to fix). Uses fetch() with credentials:'omit'
  // — same reason as addNoteAttachments/extractFromFile/kbUpload/
  // uploadCaseAttachments above: this route is on the ALB's OIDC-bypass
  // rule (compute_stack.py's CasesBypassRule, `/api/screensaver/*`)
  // because of the cross-site SameSite cookie issue on multipart POST.
  // Auth is via the X-Actor-Email header only, not the ALB session
  // cookie, for every route on that bypass rule.
  uploadScreensaverFile: async (file) => {
    const form = new FormData()
    form.append('file', file)
    const resp = await fetch(`${BASE}/screensaver/upload`, {
      method: 'POST',
      headers: { 'X-Actor-Email': getActor() },
      body: form,
      credentials: 'omit',
    })
    if (!resp.ok) {
      const text = await resp.text().catch(() => resp.statusText)
      let detail = text
      try { detail = JSON.parse(text)?.detail || text } catch { /* not JSON */ }
      throw new Error(detail || `Upload failed (${resp.status})`)
    }
    return resp.json()
  },

  // List every historical upload sitting in the ScreenSaver bucket, so
  // the user can see what's accumulated and manually delete old files —
  // see app/screensaver.py's list_uploaded_files() docstring for why
  // this exists (old dated-key uploads are never auto-deleted, and a
  // stale/cached consumer pointed at one of them — rather than the
  // fixed latest/current URL — is what actually causes the "link
  // didn't update until I deleted the old file" symptom). Same
  // fetch()+credentials:'omit' pattern as uploadScreensaverFile above,
  // for consistency (this route sits on the same `/api/screensaver/*`
  // ALB bypass path pattern).
  listScreensaverFiles: async () => {
    const resp = await fetch(`${BASE}/screensaver/files`, {
      method: 'GET',
      headers: { 'X-Actor-Email': getActor() },
      credentials: 'omit',
    })
    if (!resp.ok) {
      const text = await resp.text().catch(() => resp.statusText)
      let detail = text
      try { detail = JSON.parse(text)?.detail || text } catch { /* not JSON */ }
      throw new Error(detail || `Could not list files (${resp.status})`)
    }
    return resp.json()
  },
  deleteScreensaverFile: async (s3Key) => {
    const resp = await fetch(`${BASE}/screensaver/files?s3_key=${encodeURIComponent(s3Key)}`, {
      method: 'DELETE',
      headers: { 'X-Actor-Email': getActor() },
      credentials: 'omit',
    })
    if (!resp.ok) {
      const text = await resp.text().catch(() => resp.statusText)
      let detail = text
      try { detail = JSON.parse(text)?.detail || text } catch { /* not JSON */ }
      throw new Error(detail || `Could not delete file (${resp.status})`)
    }
    return resp.json()
  },

  // ── Chat / Voice+Video Call (job-portal-chat-voice spec) ─────────────────
  // User directory — "new conversation" / "invite to call" picker. Each
  // entry also carries a `presence` snapshot (Requirement 8.4);
  // ChatContext.jsx treats this as the initial presence map and layers
  // live WS `presence.update` events + its own periodic re-fetch on top.
  listUsersDirectory: () => axios.get(`${BASE}/users/directory`, { headers: headers() }).then(r => r.data),

  // Conversations
  listChatConversations: () => axios.get(`${BASE}/chat/conversations`, { headers: headers() }).then(r => r.data),
  createDirectConversation: (participantEmail) =>
    axios.post(`${BASE}/chat/conversations`, { type: 'direct', participant_email: participantEmail }, { headers: headers() }).then(r => r.data),
  createGroupConversation: (name, participantEmails) =>
    axios.post(`${BASE}/chat/conversations`, { type: 'group', name, participant_emails: participantEmails }, { headers: headers() }).then(r => r.data),
  renameGroupConversation: (conversationId, name) =>
    axios.patch(`${BASE}/chat/conversations/${conversationId}`, { name }, { headers: headers() }).then(r => r.data),
  addConversationParticipants: (conversationId, emails) =>
    axios.patch(`${BASE}/chat/conversations/${conversationId}`, { add_participant_emails: emails }, { headers: headers() }).then(r => r.data),
  removeConversationParticipants: (conversationId, emails) =>
    axios.patch(`${BASE}/chat/conversations/${conversationId}`, { remove_participant_emails: emails }, { headers: headers() }).then(r => r.data),

  // Messages
  listChatMessages: (conversationId, { limit, beforeMessageId } = {}) =>
    axios.get(`${BASE}/chat/conversations/${conversationId}/messages`, {
      params: { ...(limit ? { limit } : {}), ...(beforeMessageId ? { before_message_id: beforeMessageId } : {}) },
      headers: headers(),
    }).then(r => r.data),
  sendChatMessage: (conversationId, content, attachment) =>
    axios.post(`${BASE}/chat/conversations/${conversationId}/messages`, { content, attachment }, { headers: headers() }).then(r => r.data),
  // Requirement 3.1/3.4-3.7: server-enforced 10-minute Edit_Window —
  // dayjs is used by the composer (Task 14.3) to compute/display the
  // remaining time client-side, but the real enforcement is here on the
  // backend (this call fails with 400 once the window has expired).
  editChatMessage: (messageId, conversationId, content) =>
    axios.patch(`${BASE}/chat/messages/${messageId}`, { content }, { params: { conversation_id: conversationId }, headers: headers() }).then(r => r.data),
  // Requirement 2.5: read receipts — call this when a Conversation is
  // opened/its messages become visible, for whichever message_ids
  // aren't already read by the caller. Silently no-ops on already-read
  // or unknown message_ids (see main.py's mark_chat_messages_read()).
  markMessagesRead: (conversationId, messageIds) =>
    axios.post(`${BASE}/chat/conversations/${conversationId}/messages/read`, { message_ids: messageIds }, { headers: headers() }).then(r => r.data),

  // Attachments — presigned PUT for upload, fresh presigned GET per view
  // (never cached, per main.py's get_chat_attachment_url() docstring).
  presignChatAttachment: (conversationId, messageId, filename, contentType, sizeBytes) =>
    axios.post(`${BASE}/chat/conversations/${conversationId}/attachments/presign`, {
      message_id: messageId, filename, content_type: contentType, size_bytes: sizeBytes,
    }, { headers: headers() }).then(r => r.data),
  uploadChatAttachmentToS3: (presignedUrl, file, contentType, onProgress) =>
    // Direct PUT to S3 using the presigned URL — no Portal auth headers
    // (S3 authenticates via the URL's own signature), matching every
    // other presigned-PUT flow in this app (note attachments, etc.).
    // onProgress(percent:number), if given, drives AttachmentUpload.jsx's
    // progress indicator (Task 14.4/Requirement 4.4).
    axios.put(presignedUrl, file, {
      headers: { 'Content-Type': contentType || file.type || 'application/octet-stream' },
      onUploadProgress: onProgress
        ? (evt) => { if (evt.total) onProgress(Math.round((evt.loaded / evt.total) * 100)) }
        : undefined,
    }),
  getChatAttachmentUrl: (s3Key, conversationId) =>
    axios.get(`${BASE}/chat/attachments/${encodeURIComponent(s3Key)}/url`, { params: { conversation_id: conversationId }, headers: headers() }).then(r => r.data),

  // Calls — signaling only; actual media join uses the chime_join_info
  // fields on the accept response with amazon-chime-sdk-js (Task 15).
  createCall: (conversationId, calleeEmails) =>
    axios.post(`${BASE}/calls`, { conversation_id: conversationId, ...(calleeEmails ? { callee_emails: calleeEmails } : {}) }, { headers: headers() }).then(r => r.data),
  acceptCall: (callId) => axios.post(`${BASE}/calls/${callId}/accept`, {}, { headers: headers() }).then(r => r.data),
  declineCall: (callId) => axios.post(`${BASE}/calls/${callId}/decline`, {}, { headers: headers() }).then(r => r.data),
  cancelCall: (callId) => axios.post(`${BASE}/calls/${callId}/cancel`, {}, { headers: headers() }).then(r => r.data),
  leaveCall: (callId) => axios.post(`${BASE}/calls/${callId}/leave`, {}, { headers: headers() }).then(r => r.data),
  inviteToCall: (callId, email) => axios.post(`${BASE}/calls/${callId}/invite`, { email }, { headers: headers() }).then(r => r.data),
  // Requirement 9.3/11.3/11.4: 30-day retention is enforced entirely
  // server-side (see call_store.py) — this always just returns whatever
  // the backend still has.
  listConversationCallHistory: (conversationId) =>
    axios.get(`${BASE}/chat/conversations/${conversationId}/calls`, { headers: headers() }).then(r => r.data),
}
