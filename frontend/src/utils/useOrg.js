// Organisation-level settings for the deployment, with neutral fallbacks used
// until GET /api/org-config has loaded (or if it fails). Nothing here is
// hard-coded for a particular company: the values come from the backend's
// environment (backend/app/org_config.py).
import { useAuth } from '../auth/AuthContext.jsx'

export default function useOrg() {
  const { orgConfig } = useAuth()
  const c = orgConfig || {}
  return {
    orgName: c.org_name || 'Your Organization',
    coreAccountId: c.core_account_id || '',
    portalHostAccountId: c.portal_host_account_id || '',
    amsAccountId: c.ams_account_id || c.core_account_id || '',
    vpnAccountId: c.vpn_account_id || '',
    licensingUrl: c.licensing_dashboard_url || '',
    securityPortalUrl: c.security_portal_public_url || '',
    foundingAdminEmails: (c.founding_admin_emails || []).map(e => e.toLowerCase()),
    founderEmails: (c.founder_emails || []).map(e => e.toLowerCase()),
    emailDomain: c.email_domain || '',
    features: c.features || {},
  }
}
