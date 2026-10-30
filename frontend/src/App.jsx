import React from 'react'
import { BrowserRouter, Routes, Route, Link, useLocation, useNavigate } from 'react-router-dom'
import { AuthProvider, useAuth, SESSION_MAX_AGE_MS, SESSION_STARTED_AT_KEY } from './auth/AuthContext.jsx'
import { ChatProvider } from './chat/ChatContext.jsx'
import { CallProvider } from './chat/CallContext.jsx'
import IncomingCallOverlay from './chat/IncomingCallOverlay.jsx'
import CallView from './chat/CallView.jsx'
import LoginPage from './pages/LoginPage.jsx'
import DashboardPage from './pages/DashboardPage.jsx'
import JobList from './pages/JobList.jsx'
import JobDetail from './pages/JobDetail.jsx'
import CreateJob from './pages/CreateJob.jsx'
import KBPage from './pages/KBPage.jsx'
import AdminPage from './pages/AdminPage.jsx'
import CloudAccountsPage from './pages/CloudAccountsPage.jsx'
import CalendarsPage from './pages/CalendarsPage.jsx'
import AwsResourcesDashboardPage from './pages/AwsResourcesDashboardPage.jsx'
import CasesPage from './pages/CasesPage.jsx'
import HealthDashboardPage from './pages/HealthDashboardPage.jsx'
import CostAnalysisPage from './pages/CostAnalysisPage.jsx'
import BillingByServicePage from './pages/BillingByServicePage.jsx'
import RiSpSavingsPage from './pages/RiSpSavingsPage.jsx'
import CmdbPage from './pages/CmdbPage.jsx'
import VpnLookupPage from './pages/VpnLookupPage.jsx'
import WhitelistLmsPage from './pages/WhitelistLmsPage.jsx'
import FeedbackPage from './pages/FeedbackPage.jsx'
import SecurityTeamPortalPage from './pages/SecurityTeamPortalPage.jsx'
import SecurityHubPage from './pages/SecurityHubPage.jsx'
import WafMonitoringPage from './pages/WafMonitoringPage.jsx'
import DnsRecordsPage from './pages/DnsRecordsPage.jsx'
import S2sVpnProfileCreationPage from './pages/S2sVpnProfileCreationPage.jsx'
import SrTicketPage from './pages/SrTicketPage.jsx'
import LicensingPage from './pages/LicensingPage.jsx'
import HelpdeskTeamPage from './pages/HelpdeskTeamPage.jsx'
import MerakiMonitorPage from './pages/MerakiMonitorPage.jsx'
import WebsiteMonitoringPage from './pages/WebsiteMonitoringPage.jsx'
import PatchingPage from './pages/PatchingPage.jsx'
import AzureResourcesPage from './pages/AzureResourcesPage.jsx'
import AiAssistantPage from './pages/AiAssistantPage.jsx'
import DevOpsAgentPage from './pages/DevOpsAgentPage.jsx'
import ChatPage from './chat/ChatPage.jsx'
import ChatBot from './components/ChatBot.jsx'
import ProductionFreezeNotice from './components/ProductionFreezeNotice.jsx'
import FestiveDecorations from './components/FestiveDecorations.jsx'
import Avatar from './components/Avatar.jsx'
import WelcomeBanner from './components/WelcomeBanner.jsx'
import ReleaseNotesModal from './components/ReleaseNotesModal.jsx'
import AppUpdater from './components/AppUpdater.jsx'
import { nowHK } from './utils/time.js'
import { getActiveFestival } from './utils/festivals.js'
import { COLORS } from './utils/theme.js'
import { api } from './api.js'
import { installExternalLinkHandler } from './utils/externalLinks.js'

// ── Freshservice-style left sidebar layout ──────────────────────────────────
// Replaces the previous top horizontal nav bar. Sidebar tokens come from
// theme.js (COLORS.sidebarBg etc.) so the whole portal shares one palette.

const SIDEBAR_WIDTH = 220
// Collapsed width shows just the icon column (labels hidden) — mirrors
// the reference Freshservice Change Calendar screenshot's collapsible
// left rail (the "‹" toggle). Persisted in localStorage (see
// COLLAPSE_KEY below) so a user's preference survives page reloads/
// navigation instead of resetting to expanded every time.
const SIDEBAR_WIDTH_COLLAPSED = 60
const COLLAPSE_KEY = 'job_portal_sidebar_collapsed'

const SIDEBAR_LOGO = {
  display: 'flex', alignItems: 'center', gap: 8, padding: '18px 18px 16px',
  color: '#fff', fontWeight: 700, fontSize: 15.5, letterSpacing: 0.2,
  borderBottom: `1px solid ${COLORS.sidebarBorder}`,
}
const SIDEBAR_SECTION_LABEL = {
  color: COLORS.sidebarText, fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase',
  letterSpacing: 0.6, padding: '16px 18px 6px',
}
const SIDEBAR_LINK_BASE = {
  display: 'flex', alignItems: 'center', gap: 10, padding: '9px 18px', margin: '1px 8px',
  borderRadius: 7, color: COLORS.sidebarText, textDecoration: 'none', fontSize: 13.5, fontWeight: 500,
  borderLeft: '3px solid transparent', boxSizing: 'border-box',
}
// Active tab gets a light-blue left accent bar (Freshservice-style) in
// addition to the background swap — the background-only version was easy
// to miss when scanning the sidebar quickly, especially at a glance/in
// peripheral vision. borderLeft (not a separate positioned element) so
// it participates in the same box without extra markup; margin/
// borderRadius stay identical to the inactive state so the accent bar
// doesn't shift the link's content horizontally.
const SIDEBAR_LINK_ACTIVE = {
  ...SIDEBAR_LINK_BASE, background: COLORS.sidebarBgActive, color: COLORS.sidebarTextActive, fontWeight: 600,
  borderLeft: '3px solid #60a5fa',
}

const TOPBAR_HEIGHT = 54

// Mobile breakpoint — below this viewport width, the sidebar becomes an
// off-canvas drawer (hidden by default, slides in over the content when
// opened via the TopBar's hamburger button) instead of a permanent
// left-hand column, and the content area reclaims the full width
// instead of leaving a 60-220px gutter. 768px matches the common
// tablet-portrait/phone-landscape cutoff used across most responsive
// frameworks, and comfortably separates "phone" from "the desktop
// sidebar still makes sense" widths for this app's own layout.
const MOBILE_BREAKPOINT = 768

// No existing responsive/viewport-width detection anywhere in this
// codebase (confirmed) — this is the first. Uses matchMedia + a resize
// listener rather than checking window.innerWidth once, so rotating a
// device or resizing a browser window updates the layout live instead
// of only taking effect on next page load.
function useIsMobile() {
  const [isMobile, setIsMobile] = React.useState(
    () => typeof window !== 'undefined' && window.innerWidth <= MOBILE_BREAKPOINT
  )
  React.useEffect(() => {
    const mql = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT}px)`)
    const handler = (e) => setIsMobile(e.matches)
    mql.addEventListener('change', handler)
    return () => mql.removeEventListener('change', handler)
  }, [])
  return isMobile
}

function HKClock() {
  const [now, setNow] = React.useState(nowHK())

  React.useEffect(() => {
    const timer = setInterval(() => setNow(nowHK()), 1000)
    return () => clearInterval(timer)
  }, [])

  const [datePart, timePart] = now.split(' ')

  return (
    <div style={{ textAlign: 'right', lineHeight: 1.2 }} title="Hong Kong Time (UTC+8)">
      <div style={{ color: COLORS.textPrimary, fontSize: 13, fontWeight: 700, fontFamily: "'SF Mono', Consolas, monospace", letterSpacing: 0.5 }}>
        {timePart}
      </div>
      <div style={{ color: COLORS.textMuted, fontSize: 10.5 }}>
        {datePart} · HKT (UTC+8)
      </div>
    </div>
  )
}

// Countdown to the same 8-hour absolute session deadline AuthContext.jsx
// enforces (auto sign-out — see its forceLogout()/checkSessionExpiry()).
// Reads session_started_at directly from localStorage rather than
// duplicating a "time remaining" value into AuthContext's own state,
// since this is purely a display concern — nothing here ever triggers
// the actual logout, that stays entirely owned by AuthContext.jsx's own
// timer, this just shows the same countdown visually. Ticks every
// second like HKClock beside it, for a smoothly counting-down "MM:SS"
// once under 1 hour remaining, or "Hh MMm" while there's more than an
// hour left (a second-by-second tick isn't meaningful information at
// that distance, and would just be visual noise).
//
// Renders nothing (null) in local dev mode (no ALB session, no 8-hour
// deadline exists at all) or before AuthContext has recorded a
// session_started_at yet (a brief window right after login, before
// whoami() resolves).
function SessionCountdown() {
  const { authMode } = useAuth()
  const [remainingMs, setRemainingMs] = React.useState(null)

  React.useEffect(() => {
    if (authMode !== 'oidc') return
    const tick = () => {
      const startedAt = Number(localStorage.getItem(SESSION_STARTED_AT_KEY) || 0)
      if (!startedAt) {
        setRemainingMs(null)
        return
      }
      const remaining = SESSION_MAX_AGE_MS - (Date.now() - startedAt)
      setRemainingMs(Math.max(remaining, 0))
    }
    tick()
    const timer = setInterval(tick, 1000)
    return () => clearInterval(timer)
  }, [authMode])

  if (remainingMs === null) return null

  const totalSeconds = Math.floor(remainingMs / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const display = hours > 0
    ? `${hours}h ${String(minutes).padStart(2, '0')}m`
    : `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  // Under 5 minutes remaining gets a warning colour so the imminent
  // auto-logout isn't a surprise — 5 minutes is enough time to notice
  // and finish/save whatever's in progress before being signed out.
  const isNearExpiry = remainingMs <= 5 * 60 * 1000

  return (
    <div style={{ textAlign: 'right', lineHeight: 1.2 }}
      title="Time remaining before this session's 8-hour limit signs you out automatically">
      <div style={{
        color: isNearExpiry ? COLORS.danger : COLORS.textPrimary,
        fontSize: 13, fontWeight: 700, fontFamily: "'SF Mono', Consolas, monospace", letterSpacing: 0.5,
      }}>
        {display}
      </div>
      <div style={{ color: COLORS.textMuted, fontSize: 10.5 }}>
        session
      </div>
    </div>
  )
}

function SidebarLink({ to, icon, label, active, collapsed }) {
  return (
    <Link to={to} title={collapsed ? label : undefined}
      style={{
        ...(active ? SIDEBAR_LINK_ACTIVE : SIDEBAR_LINK_BASE),
        ...(collapsed ? { justifyContent: 'center', padding: '9px 0', margin: '1px 8px' } : {}),
      }}
      onMouseEnter={e => { if (!active) e.currentTarget.style.background = 'rgba(255,255,255,0.06)' }}
      onMouseLeave={e => { if (!active) e.currentTarget.style.background = 'transparent' }}>
      <span style={{ fontSize: 15, width: 18, textAlign: 'center', flexShrink: 0 }}>{icon}</span>
      {!collapsed && label}
    </Link>
  )
}

// Reads/writes the collapsed preference to localStorage so it persists
// across reloads/navigation (a plain useState alone would reset to
// expanded on every hard refresh, which would be annoying for a user who
// deliberately collapsed it to get more content width).
function useSidebarCollapsed() {
  const [collapsed, setCollapsed] = React.useState(() => localStorage.getItem(COLLAPSE_KEY) === '1')
  const toggle = () => {
    setCollapsed(prev => {
      const next = !prev
      localStorage.setItem(COLLAPSE_KEY, next ? '1' : '0')
      return next
    })
  }
  return [collapsed, toggle]
}

function Sidebar({ collapsed: collapsedProp, onToggleCollapse, isMobile, mobileOpen, onCloseMobile }) {
  const { user, signOut, login, canSeeTab } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const at = (path) => location.pathname === path || location.pathname.startsWith(path + '/')
  const [uploadingPhoto, setUploadingPhoto] = React.useState(false)
  const [photoErr, setPhotoErr] = React.useState('')

  if (!user) return null
  const festival = getActiveFestival()
  // On mobile the sidebar is never "collapsed" (icon-only) — it's
  // either fully off-canvas (closed) or fully expanded (open), since a
  // 60px icon-only rail makes little sense as a touch-friendly overlay.
  // Shadows the incoming `collapsed` prop for the rest of this
  // function's body (every SidebarLink/sectionLabel call below already
  // reads `collapsed`), so no other line in this component needs to
  // change to account for the mobile case.
  const collapsed = isMobile ? false : collapsedProp
  const width = isMobile ? SIDEBAR_WIDTH : (collapsed ? SIDEBAR_WIDTH_COLLAPSED : SIDEBAR_WIDTH)
  // Close the drawer automatically after navigating — otherwise the
  // menu would stay open covering the newly-loaded page until the user
  // manually taps the backdrop/hamburger again.
  React.useEffect(() => {
    if (isMobile && mobileOpen) onCloseMobile()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname])

  const handlePhotoUpload = async (file) => {
    setPhotoErr('')
    setUploadingPhoto(true)
    try {
      const result = await api.uploadMyProfilePhoto(file)
      // Refresh the in-memory user object (and its localStorage/session
      // copy via login()) with the new photo, without a full page reload.
      login({ ...user, photo_data_url: result.photo_data_url })
    } catch (e) {
      setPhotoErr(e?.response?.data?.detail || 'Failed to upload photo')
    } finally {
      setUploadingPhoto(false)
    }
  }

  const sectionLabel = (text) => !collapsed && <div style={SIDEBAR_SECTION_LABEL}>{text}</div>

  return (
    <>
      {/* Backdrop — mobile only, dims/blocks the page behind the open
          drawer and closes it on tap, same convention as most mobile nav
          drawers. Rendered as a sibling (not a child of the sidebar's
          own fixed div) so it can sit at a lower z-index than the
          drawer but above everything else, and so tapping anywhere
          outside the drawer (not just an explicit close button) closes
          it. */}
      {isMobile && mobileOpen && (
        <div onClick={onCloseMobile}
          style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)', zIndex: 19 }} />
      )}
      <div style={{
        position: 'fixed', top: 0, left: 0, bottom: 0, width,
        background: COLORS.sidebarBg, display: 'flex', flexDirection: 'column',
        boxShadow: '2px 0 8px rgba(0,0,0,0.15)', zIndex: 20,
        transition: isMobile ? 'transform 0.22s ease' : 'width 0.18s ease',
        // Off-canvas on mobile: slid fully out of view to the left when
        // closed, slid into place when open. Desktop keeps its existing
        // "always in place, width animates for collapse" behaviour
        // untouched (transform stays at translateX(0) there).
        transform: isMobile ? (mobileOpen ? 'translateX(0)' : 'translateX(-100%)') : 'translateX(0)',
      }}>
      <div style={{ ...SIDEBAR_LOGO, justifyContent: collapsed ? 'center' : 'flex-start', padding: collapsed ? '18px 8px 16px' : SIDEBAR_LOGO.padding }}>
        {festival ? (
          <span style={{ fontSize: 20 }}>{festival.navIcon}</span>
        ) : (
          <img src="/logo.png" alt="Logo" style={{ width: 22, height: 22, objectFit: 'contain', flexShrink: 0 }} />
        )}
        {!collapsed && 'Cloud Infra Service Desk'}
        {/* Close button — mobile only, replaces the desktop collapse
            toggle (collapsing doesn't apply to an off-canvas drawer). */}
        {isMobile && (
          <button onClick={onCloseMobile} aria-label="Close menu"
            style={{ marginLeft: 'auto', background: 'none', border: 'none', color: '#fff', fontSize: 20, cursor: 'pointer', padding: '0 4px', lineHeight: 1 }}>
            ✕
          </button>
        )}
      </div>

      {/* Collapse/expand toggle — a small floating circular button
          straddling the sidebar's right edge (Notion/Linear-style),
          instead of the previous full-width bordered bar (which read as
          a heavy, separate block competing with the logo above it). No
          label text, no border-bottom — just an icon, so it feels like a
          lightweight affordance rather than another nav row. Desktop
          only — an off-canvas mobile drawer has no "collapsed" state,
          it's simply open or closed via the hamburger/backdrop/✕. */}
      {!isMobile && (
      <button onClick={onToggleCollapse} title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        style={{
          position: 'absolute', top: 22, left: width - 12,
          width: 22, height: 22, borderRadius: '50%',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: COLORS.sidebarBgActive, border: `1px solid ${COLORS.sidebarBorder}`,
          color: COLORS.sidebarText, cursor: 'pointer', fontSize: 11, lineHeight: 1,
          boxShadow: '0 1px 4px rgba(0,0,0,0.3)', transition: 'left 0.18s ease, color 0.15s, background 0.15s',
          zIndex: 21,
        }}
        onMouseEnter={e => { e.currentTarget.style.color = '#fff'; e.currentTarget.style.background = COLORS.brand }}
        onMouseLeave={e => { e.currentTarget.style.color = COLORS.sidebarText; e.currentTarget.style.background = COLORS.sidebarBgActive }}>
        {collapsed ? '›' : '‹'}
      </button>
      )}

      <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', paddingBottom: 12, paddingTop: 4 }}>
        {/* Tab Access Control (Admin page) — each link is conditionally
            rendered based on canSeeTab(), resolved per the current
            user's role (member/admin). A section label is only shown if
            at least one of its links is visible, so a fully-hidden
            section doesn't leave a dangling empty heading. */}
        {canSeeTab('dashboard') && (
          <>
            {sectionLabel('Overview')}
            <SidebarLink to="/dashboard" icon="📊" label="Dashboard" active={at('/dashboard')} collapsed={collapsed} />
          </>
        )}

        {(canSeeTab('jobs') || canSeeTab('jobs_new') || canSeeTab('cases')) && (
          <>
            {sectionLabel('Tickets')}
            {canSeeTab('jobs') && <SidebarLink to="/jobs" icon="🎫" label="All Tickets" active={at('/jobs') && !at('/jobs/new')} collapsed={collapsed} />}
            {canSeeTab('jobs_new') && <SidebarLink to="/jobs/new" icon="➕" label="New Ticket" active={at('/jobs/new')} collapsed={collapsed} />}
            {/* Renamed from "All Cases" and moved here from the
                Calendars section per user request — these are AMS
                Service Request cases, a ticket-like concept. */}
            {canSeeTab('cases') && <SidebarLink to="/cases" icon="🎟️" label="AWS Cases" active={at('/cases')} collapsed={collapsed} />}
          </>
        )}

        {/* "Resources" kept for the items not called out into their own
            named category below (AWS Resources / AWS Health Dashboard /
            CMDB) — Cost, Calendars, and Others were split out per user
            request into their own dedicated sidebar sections. */}
        {(canSeeTab('aws_resources') || canSeeTab('health_dashboard') || canSeeTab('cmdb') || canSeeTab('vpn_lookup') || canSeeTab('azure_resources')) && (
          <>
            {sectionLabel('Resources')}
            {canSeeTab('aws_resources') && <SidebarLink to="/aws-resources" icon="🏷️" label="AWS Resources" active={at('/aws-resources')} collapsed={collapsed} />}
            {canSeeTab('health_dashboard') && <SidebarLink to="/health-dashboard" icon="🩺" label="AWS Health Dashboard" active={at('/health-dashboard')} collapsed={collapsed} />}
            {canSeeTab('cmdb') && <SidebarLink to="/cmdb" icon="🗄️" label="CMDB" active={at('/cmdb')} collapsed={collapsed} />}
            {canSeeTab('vpn_lookup') && <SidebarLink to="/vpn-lookup" icon="🔐" label="VPN Profile Lookup" active={at('/vpn-lookup')} collapsed={collapsed} />}
            {/* Azure Resources — folded into this "Resources" category
                (per user's explicit follow-up request) rather than kept
                as its own standalone sidebar section. Inventories this
                org's single Azure subscription via ARM (see
                backend/app/azure_monitor.py) — VM/AVD/Resource
                Group/NIC/NSG are sub-tabs INSIDE the one page component,
                same as AWS Resources' own resource-type-tab-bar layout. */}
            {canSeeTab('azure_resources') && <SidebarLink to="/azure-resources" icon="☁️" label="Azure Resources" active={at('/azure-resources')} collapsed={collapsed} />}
          </>
        )}

        {/* "Cost" — Cost Analysis, Billing by Service, Savings Plan Cost. */}
        {(canSeeTab('cost_analysis') || canSeeTab('billing_by_service') || canSeeTab('ri_sp_savings')) && (
          <>
            {sectionLabel('Cost')}
            {canSeeTab('cost_analysis') && <SidebarLink to="/cost-analysis" icon="💰" label="Cost Analysis" active={at('/cost-analysis')} collapsed={collapsed} />}
            {canSeeTab('billing_by_service') && <SidebarLink to="/billing-by-service" icon="🧾" label="Billing by Service" active={at('/billing-by-service')} collapsed={collapsed} />}
            {canSeeTab('ri_sp_savings') && <SidebarLink to="/ri-sp-savings" icon="💵" label="Savings Plan Cost" active={at('/ri-sp-savings')} collapsed={collapsed} />}
          </>
        )}

        {/* "Calendars" — Calendars. (AWS Cases moved to Tickets above.) */}
        {canSeeTab('calendars') && (
          <>
            {sectionLabel('Calendars')}
            <SidebarLink to="/calendars" icon="📅" label="Calendars" active={at('/calendars')} collapsed={collapsed} />
          </>
        )}

        {/* "Others" — Feedback & Bug Report, Knowledge Base. */}
        {(canSeeTab('feedback') || canSeeTab('kb')) && (
          <>
            {sectionLabel('Others')}
            {canSeeTab('feedback') && <SidebarLink to="/feedback" icon="💬" label="Feedback & Bug Report" active={at('/feedback')} collapsed={collapsed} />}
            {canSeeTab('kb') && <SidebarLink to="/kb" icon="🗂️" label="Knowledge Base" active={at('/kb')} collapsed={collapsed} />}
          </>
        )}

        {/* "Security" — shortcut to the separate Security Scan Portal app,
            the AWS Security Hub findings/remediation tab, WAF rule
            monitoring, and Route 53 DNS records. Seeing the DNS tab only
            grants read-only lookup; editing a zone additionally requires
            admin or Cloud Infra/Security/Management Team membership,
            enforced server-side (main.py's require_dns_writer). */}
        {(canSeeTab('security_team_portal') || canSeeTab('security_hub') || canSeeTab('waf_monitoring') || canSeeTab('dns_records') || canSeeTab('s2s_vpn_profile_creation') || canSeeTab('sr_ticket_information') || canSeeTab('licensing')) && (
          <>
            {sectionLabel('Security')}
            {canSeeTab('security_team_portal') && <SidebarLink to="/security-team-portal" icon="🛡️" label="Security Team Portal" active={at('/security-team-portal')} collapsed={collapsed} />}
            {canSeeTab('security_hub') && <SidebarLink to="/security-hub" icon="🔐" label="AWS Security Hub" active={at('/security-hub')} collapsed={collapsed} />}
            {canSeeTab('waf_monitoring') && <SidebarLink to="/waf-monitoring" icon="🧱" label="WAF Rule Monitoring" active={at('/waf-monitoring')} collapsed={collapsed} />}
            {canSeeTab('dns_records') && <SidebarLink to="/dns-records" icon="🌐" label="DNS Record" active={at('/dns-records')} collapsed={collapsed} />}
            {canSeeTab('s2s_vpn_profile_creation') && <SidebarLink to="/s2s-vpn-profile-creation" icon="🔒" label="S2S VPN Profile Creation" active={at('/s2s-vpn-profile-creation')} collapsed={collapsed} />}
            {canSeeTab('sr_ticket_information') && <SidebarLink to="/sr-ticket-information" icon="🎫" label="SR Ticket Information" active={at('/sr-ticket-information')} collapsed={collapsed} />}
            {canSeeTab('licensing') && <SidebarLink to="/licensing" icon="📜" label="Licensing" active={at('/licensing')} collapsed={collapsed} />}
          </>
        )}

        {/* "Helpdesk Team" — a new service-catalogue-style section, so
            more Helpdesk Team tools can be added as additional tabs
            inside HelpdeskTeamPage.jsx later without needing a new
            sidebar entry each time. First tool: S3-ScreenSaver. */}
        {canSeeTab('helpdesk_team_s3_screensaver') && (
          <>
            {sectionLabel('Helpdesk Team')}
            <SidebarLink to="/helpdesk-team" icon="🛎️" label="S3-ScreenSaver" active={at('/helpdesk-team')} collapsed={collapsed} />
          </>
        )}

        {/* "Lifecycle" > Patching (OS tab inside the page) — see backend/app/patching.py.
            Visible to every logged-in user (settings_store.EVERYONE_TAB_IDS); only
            admins and the Cloud Infra / Security / Management Teams can edit. */}
        {canSeeTab('lifecycle_patching') && (
          <>
            {sectionLabel('Lifecycle')}
            <SidebarLink to="/patching" icon="🩹" label="Patching" active={at('/patching')} collapsed={collapsed} />
          </>
        )}

        {/* "Monitoring" — Website Monitoring (see backend/app/website_monitor.py).
            Visible to every logged-in user (settings_store.EVERYONE_TAB_IDS);
            only admins can add/remove websites inside the page. */}
        {canSeeTab('website_monitoring') && (
          <>
            {sectionLabel('Monitoring')}
            <SidebarLink to="/website-monitoring" icon="🌐" label="Website Monitoring" active={at('/website-monitoring')} collapsed={collapsed} />
          </>
        )}

        {/* "Local Infra Hardware Monitor" — Cisco Meraki Access Point
            monitoring dashboard (see backend/app/meraki_monitor.py).
            Its own standalone sidebar section (not folded into Helpdesk
            Team's catalogue) since it's one cohesive dashboard, not a
            growing family of unrelated tools. Read-only for everyone —
            no admin/group-gated write path to protect. */}
        {canSeeTab('meraki_monitor') && (
          <>
            {sectionLabel('Local Infra Hardware Monitor')}
            <SidebarLink to="/meraki-monitor" icon="📡" label="Network Devices" active={at('/meraki-monitor')} collapsed={collapsed} />
          </>
        )}

        {/* "AI Assistant" — standalone full-page chatbot tab, separate
            from the floating ChatBot widget mounted globally in Shell()
            below. Pure Q&A only: no ticket/KB/AWS resource access. */}
        {canSeeTab('whitelist_lms') && (
          <>
            {sectionLabel('Whitelist')}
            <SidebarLink to="/whitelist" icon="🔓" label="LMS" active={at('/whitelist')} collapsed={collapsed} />
          </>
        )}

        {canSeeTab('ai_assistant') && (
          <>
            {sectionLabel('AI Assistant')}
            <SidebarLink to="/ai-assistant" icon="🤖" label="AI Assistant" active={at('/ai-assistant')} collapsed={collapsed} />
          </>
        )}

        {canSeeTab('devops_agent') && (
          <>
            {sectionLabel('AWS DevOps Agent')}
            <SidebarLink to="/devops-agent" icon="☁️" label="AWS DevOps Agent" active={at('/devops-agent')} collapsed={collapsed} />
          </>
        )}

        {/* job-portal-chat-voice spec (Task 14.5) — company-wide 1-on-1/
            group text chat + voice/video calls, screen share, file/
            photo sharing. See TAB_REGISTRY's "chat" entry (settings_
            store.py) for why this is visible-by-default rather than
            gated behind a specific team's group access. */}
        {canSeeTab('chat') && (
          <>
            {sectionLabel('Chat')}
            <SidebarLink to="/chat" icon="💬" label="Chat" active={at('/chat')} collapsed={collapsed} />
          </>
        )}

        {(user.is_admin || user.can_manage_cloud_accounts) && (
          <>
            {sectionLabel('Administration')}
            {user.is_admin && (
              <SidebarLink to="/admin" icon="⚙️" label="Admin" active={at('/admin') && !at('/admin/cloud-accounts')} collapsed={collapsed} />
            )}
            {user.can_manage_cloud_accounts && (
              <SidebarLink to="/admin/cloud-accounts" icon="☁️" label="Cloud Accounts" active={at('/admin/cloud-accounts')} collapsed={collapsed} />
            )}
          </>
        )}
      </div>

      <div style={{ borderTop: `1px solid ${COLORS.sidebarBorder}`, padding: collapsed ? '12px 8px' : '12px 14px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, justifyContent: collapsed ? 'center' : 'flex-start' }}>
          <Avatar
            photoUrl={user.photo_data_url} name={user.name} email={user.email}
            size={30} fontSize={13} bg={COLORS.sidebarBgActive}
            onUpload={handlePhotoUpload} uploading={uploadingPhoto}
            title="Click to upload a profile photo"
          />
          {!collapsed && (
            <div style={{ overflow: 'hidden' }}>
              <div style={{ color: '#fff', fontSize: 12.5, fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{user.name}</div>
              <div style={{ color: COLORS.sidebarText, fontSize: 10.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{user.email}</div>
            </div>
          )}
        </div>
        {photoErr && !collapsed && (
          <div style={{ color: '#fca5a5', fontSize: 10.5, marginBottom: 8 }}>{photoErr}</div>
        )}
        <button onClick={() => { signOut(); navigate('/') }} title={collapsed ? 'Sign out' : undefined}
          style={{
            width: '100%', background: 'rgba(255,255,255,0.08)', color: '#fff',
            border: '1px solid rgba(255,255,255,0.15)', borderRadius: 6, padding: '6px 10px',
            fontSize: 12, cursor: 'pointer',
          }}>
          {collapsed ? '⏻' : 'Sign out'}
        </button>
        {!collapsed && <AppUpdater />}
      </div>
      </div>
    </>
  )
}

function TopBar({ sidebarWidth, isMobile, onOpenMobile }) {
  const { user } = useAuth()
  if (!user) return null
  // On mobile there's no permanent sidebar gutter to start after (the
  // sidebar is off-canvas), so the topbar spans the full width and gets
  // a hamburger button on its left edge instead.
  return (
    <div style={{
      position: 'fixed', top: 0, left: isMobile ? 0 : sidebarWidth, right: 0, height: TOPBAR_HEIGHT,
      background: '#fff', borderBottom: `1px solid ${COLORS.border}`,
      display: 'flex', alignItems: 'center', padding: isMobile ? '0 14px' : '0 24px', zIndex: 15,
      boxShadow: '0 1px 3px rgba(15,23,42,0.04)',
      transition: 'left 0.18s ease',
    }}>
      {isMobile && (
        <button onClick={onOpenMobile} aria-label="Open menu"
          style={{
            background: 'none', border: `1px solid ${COLORS.border}`, borderRadius: 7,
            width: 36, height: 36, display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 18, cursor: 'pointer', color: COLORS.textPrimary, marginRight: 10,
          }}>
          ☰
        </button>
      )}
      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 18 }}>
        <SessionCountdown />
        <HKClock />
      </div>
    </div>
  )
}

// Enforces Tab Access Control (Admin page) at the route level, not just
// the sidebar — so a user whose role can't see a tab also can't reach it
// by typing/bookmarking the URL directly. `tabId` is optional: routes not
// covered by TAB_REGISTRY (e.g. /jobs/:id detail pages, which are reached
// via the "jobs" tab rather than having their own entry) simply skip this
// check and fall back to the normal login-only guard.
//
// If blocked, redirects to "/dashboard" UNLESS "dashboard" is itself the
// blocked tab (an admin could technically hide it for a role) — in that
// case navigating to /dashboard would just bounce right back here in a
// loop, so this renders an inline "no access" message instead of
// redirecting anywhere.
function ProtectedRoute({ children, tabId }) {
  const { user, canSeeTab } = useAuth()
  const navigate = useNavigate()
  const blocked = !!(tabId && user && !canSeeTab(tabId))
  const blockedIsDashboard = blocked && tabId === 'dashboard'
  React.useEffect(() => {
    if (!user) navigate('/')
    else if (blocked && !blockedIsDashboard) navigate('/dashboard')
  }, [user, blocked, blockedIsDashboard])
  if (!user) return null
  if (blockedIsDashboard) {
    return (
      <div style={{ padding: 60, textAlign: 'center', color: COLORS.textMuted }}>
        🚫 You don't have access to any tab yet. Contact an admin to have your access configured.
      </div>
    )
  }
  if (blocked) return null
  return children
}

function Shell({ children }) {
  const { user } = useAuth()
  const [collapsed, toggleCollapsed] = useSidebarCollapsed()
  const isMobile = useIsMobile()
  // Mobile drawer open/closed — independent of the desktop `collapsed`
  // preference (they're mutually exclusive concerns: collapsed applies
  // only to the permanent desktop sidebar's width, mobileOpen applies
  // only to the off-canvas drawer's visibility). Always starts closed;
  // Sidebar itself auto-closes it on navigation (see its useEffect).
  const [mobileOpen, setMobileOpen] = React.useState(false)
  if (!user) return children // LoginPage — no sidebar/topbar chrome

  // On mobile, content never gets a sidebar-width gutter — the sidebar
  // is off-canvas, not a permanent column, so the page content should
  // use the full viewport width regardless of collapsed/expanded state.
  const sidebarWidth = collapsed ? SIDEBAR_WIDTH_COLLAPSED : SIDEBAR_WIDTH
  const contentMarginLeft = isMobile ? 0 : sidebarWidth

  return (
    <>
      <Sidebar
        collapsed={collapsed} onToggleCollapse={toggleCollapsed}
        isMobile={isMobile} mobileOpen={mobileOpen} onCloseMobile={() => setMobileOpen(false)}
      />
      <TopBar sidebarWidth={sidebarWidth} isMobile={isMobile} onOpenMobile={() => setMobileOpen(true)} />
      {/* FestiveDecorations/ProductionFreezeNotice render in normal flow
          (not fixed), so they must sit inside the offset content wrapper
          — otherwise they'd span the full viewport width underneath the
          fixed sidebar/topbar instead of starting after them. */}
      <div className="app-content" style={{ marginLeft: contentMarginLeft, paddingTop: TOPBAR_HEIGHT, minHeight: '100vh', background: COLORS.pageBg, transition: 'margin-left 0.18s ease' }}>
        <FestiveDecorations />
        <ProductionFreezeNotice />
        {children}
      </div>
      <ChatBot />
      {/* job-portal-chat-voice Task 15.1/15.2 — mounted app-shell-wide
          (Requirement 8.6/8.7) so a ringing/active Call surfaces
          regardless of which page/route is currently focused. Both
          render nothing on their own when there is no incoming/active
          Call for the current user (see each component's own guard). */}
      <IncomingCallOverlay />
      <CallView />
      <WelcomeBanner />
      <ReleaseNotesModal />
    </>
  )
}

export default function App() {
  // Installed once for the whole app's lifetime — see
  // externalLinks.js's docstring. No-ops entirely in the plain web
  // build (isTauri() is false there), so this has zero effect outside
  // the desktop app.
  React.useEffect(() => installExternalLinkHandler(), [])

  return (
    <AuthProvider>
      {/* job-portal-chat-voice Task 13.4: ChatProvider must be INSIDE
          AuthProvider (it reads useAuth() for the current user's email
          to open the WS connection) but wraps everything else so any
          page/route can consume useChat() — same nesting pattern as
          AuthProvider itself relative to BrowserRouter/Shell below.
          CallProvider (Task 15.1) is INSIDE ChatProvider — it reads
          ChatContext's incomingCall/activeCall/setIncomingCall/
          setActiveCall (see CallContext.jsx's own top-of-file comment
          on why the two are kept as separate contexts). */}
      <ChatProvider>
      <CallProvider>
      <BrowserRouter>
        <Shell>
          <Routes>
            <Route path="/" element={<LoginPage />} />
            <Route path="/dashboard" element={<ProtectedRoute tabId="dashboard"><DashboardPage /></ProtectedRoute>} />
            <Route path="/jobs" element={<ProtectedRoute tabId="jobs"><JobList /></ProtectedRoute>} />
            <Route path="/jobs/new" element={<ProtectedRoute tabId="jobs_new"><CreateJob /></ProtectedRoute>} />
            <Route path="/jobs/:id" element={<ProtectedRoute tabId="jobs"><JobDetail /></ProtectedRoute>} />
            <Route path="/kb" element={<ProtectedRoute tabId="kb"><KBPage /></ProtectedRoute>} />
            <Route path="/calendars" element={<ProtectedRoute tabId="calendars"><CalendarsPage /></ProtectedRoute>} />
            <Route path="/aws-resources" element={<ProtectedRoute tabId="aws_resources"><AwsResourcesDashboardPage /></ProtectedRoute>} />
            <Route path="/cases" element={<ProtectedRoute tabId="cases"><CasesPage /></ProtectedRoute>} />
            <Route path="/health-dashboard" element={<ProtectedRoute tabId="health_dashboard"><HealthDashboardPage /></ProtectedRoute>} />
            <Route path="/cost-analysis" element={<ProtectedRoute tabId="cost_analysis"><CostAnalysisPage /></ProtectedRoute>} />
            <Route path="/billing-by-service" element={<ProtectedRoute tabId="billing_by_service"><BillingByServicePage /></ProtectedRoute>} />
            <Route path="/ri-sp-savings" element={<ProtectedRoute tabId="ri_sp_savings"><RiSpSavingsPage /></ProtectedRoute>} />
            <Route path="/cmdb" element={<ProtectedRoute tabId="cmdb"><CmdbPage /></ProtectedRoute>} />
            <Route path="/vpn-lookup" element={<ProtectedRoute tabId="vpn_lookup"><VpnLookupPage /></ProtectedRoute>} />
            <Route path="/whitelist" element={<ProtectedRoute tabId="whitelist_lms"><WhitelistLmsPage /></ProtectedRoute>} />
            <Route path="/feedback" element={<ProtectedRoute tabId="feedback"><FeedbackPage /></ProtectedRoute>} />
            <Route path="/security-team-portal" element={<ProtectedRoute tabId="security_team_portal"><SecurityTeamPortalPage /></ProtectedRoute>} />
            <Route path="/security-hub" element={<ProtectedRoute tabId="security_hub"><SecurityHubPage /></ProtectedRoute>} />
            <Route path="/waf-monitoring" element={<ProtectedRoute tabId="waf_monitoring"><WafMonitoringPage /></ProtectedRoute>} />
            <Route path="/dns-records" element={<ProtectedRoute tabId="dns_records"><DnsRecordsPage /></ProtectedRoute>} />
            <Route path="/s2s-vpn-profile-creation" element={<ProtectedRoute tabId="s2s_vpn_profile_creation"><S2sVpnProfileCreationPage /></ProtectedRoute>} />
            <Route path="/sr-ticket-information" element={<ProtectedRoute tabId="sr_ticket_information"><SrTicketPage /></ProtectedRoute>} />
            <Route path="/licensing" element={<ProtectedRoute tabId="licensing"><LicensingPage /></ProtectedRoute>} />
            <Route path="/helpdesk-team" element={<ProtectedRoute tabId="helpdesk_team_s3_screensaver"><HelpdeskTeamPage /></ProtectedRoute>} />
            <Route path="/patching" element={<ProtectedRoute tabId="lifecycle_patching"><PatchingPage /></ProtectedRoute>} />
            <Route path="/website-monitoring" element={<ProtectedRoute tabId="website_monitoring"><WebsiteMonitoringPage /></ProtectedRoute>} />
            <Route path="/meraki-monitor" element={<ProtectedRoute tabId="meraki_monitor"><MerakiMonitorPage /></ProtectedRoute>} />
            <Route path="/azure-resources" element={<ProtectedRoute tabId="azure_resources"><AzureResourcesPage /></ProtectedRoute>} />
            <Route path="/ai-assistant" element={<ProtectedRoute tabId="ai_assistant"><AiAssistantPage /></ProtectedRoute>} />
            <Route path="/devops-agent" element={<ProtectedRoute tabId="devops_agent"><DevOpsAgentPage /></ProtectedRoute>} />
            <Route path="/chat" element={<ProtectedRoute tabId="chat"><ChatPage /></ProtectedRoute>} />
            <Route path="/admin" element={<ProtectedRoute><AdminPage /></ProtectedRoute>} />
            <Route path="/admin/cloud-accounts" element={<ProtectedRoute><CloudAccountsPage /></ProtectedRoute>} />
          </Routes>
        </Shell>
      </BrowserRouter>
      </CallProvider>
      </ChatProvider>
    </AuthProvider>
  )
}
