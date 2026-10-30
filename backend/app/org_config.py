"""
Organisation-level configuration for the Cloud Infra Job Portal.

Everything that identifies *your* organisation (name, domains, admin users,
the AWS accounts the Portal depends on, third-party tenants) is read from
environment variables here, so the same code base can be deployed by any
organisation without editing source.

This module is DATA ONLY and must not import any other app module, because
it is imported very early by many modules.

Environment variables (all optional unless noted):

  ORG_NAME                    Display name of your organisation.
  PORTAL_DOMAIN               Public host of the web app, e.g. portal.example.com.
  API_DOMAIN                  Public host of the API (ALB), e.g. portal-api.example.com.
  ORG_EMAIL_DOMAIN            Your corporate e-mail domain, e.g. example.com. Also used to
                              allow any sub-domain of it for CORS.
  CORS_EXTRA_ORIGINS          Extra comma-separated allowed browser origins.
  SEED_ADMIN_EMAILS           REQUIRED for a first deployment: comma-separated e-mails of the
                              founding admins (they can never be locked out).
  PORTAL_FOUNDER_EMAILS       Optional e-mails shown with a "founder" badge.
  SES_SENDER_ADDRESS          Verified Amazon SES sender used for notifications.
  PORTAL_HOST_ACCOUNT_ID      AWS account that hosts the Portal itself.
  CORE_ACCOUNT_ID             AWS account whose IAM identity the Portal uses to reach the
                              other accounts (the "core" / hub account).
  KNOWN_BUSINESS_UNITS        Comma-separated business units offered on the Cloud Accounts page.
  CLOUD_ACCOUNT_SEED_FILE     Optional JSON file that pre-loads the account registry (see
                              docs / cloud_account_seed.py). Empty = start with no accounts.
  AMS_ACCOUNT_ID              Account shown on "Raise to AMS" buttons (defaults to CORE_ACCOUNT_ID).
  LICENSING_DASHBOARD_URL     External licensing dashboard linked from the Licensing page.
  FRESHSERVICE_DOMAIN         e.g. yourcompany.freshservice.com. Empty disables Freshservice.
  AZURE_AD_TENANT_ID          Microsoft Entra ID (Azure AD) tenant used for SSO.

  ENABLED_FEATURES            Comma-separated optional integrations to switch on (see
                              OPTIONAL_FEATURES below). All are OFF by default.

Feature switches live in `features()` below; the frontend hides the ones that are off.
"""

from __future__ import annotations

import os
import re


def _csv(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in os.environ.get(name, default).split(",") if p.strip()]


ORG_NAME = os.environ.get("ORG_NAME", "Your Organization")
PORTAL_DOMAIN = os.environ.get("PORTAL_DOMAIN", "")
API_DOMAIN = os.environ.get("API_DOMAIN", "")
ORG_EMAIL_DOMAIN = os.environ.get("ORG_EMAIL_DOMAIN", "").strip().lower().lstrip("@")

PORTAL_HOST_ACCOUNT_ID = os.environ.get("PORTAL_HOST_ACCOUNT_ID", "").strip()
CORE_ACCOUNT_ID = os.environ.get("CORE_ACCOUNT_ID", "").strip()

KNOWN_BUSINESS_UNITS = tuple(_csv("KNOWN_BUSINESS_UNITS"))

FRESHSERVICE_DOMAIN = os.environ.get("FRESHSERVICE_DOMAIN", "").strip()
SES_SENDER_ADDRESS = os.environ.get("SES_SENDER_ADDRESS", "").strip()


def cors_origins() -> list[str]:
    """Explicit browser origins allowed to call the API."""
    origins = ["http://localhost:5200", "http://127.0.0.1:5200"]
    for host in (PORTAL_DOMAIN, API_DOMAIN):
        if host:
            origins.append(f"https://{host}")
    origins.extend(_csv("CORS_EXTRA_ORIGINS"))
    return origins


def cors_origin_regex() -> str:
    """Any CloudFront distribution plus (when configured) any sub-domain of
    the organisation's e-mail/web domain. Both suffixes are controlled by AWS
    or by the organisation, not by an attacker."""
    parts = [r"[a-z0-9-]+\.cloudfront\.net"]
    if ORG_EMAIL_DOMAIN:
        parts.append(r"[a-z0-9-]+\." + re.escape(ORG_EMAIL_DOMAIN))
    return r"https://(" + "|".join(parts) + ")"


# Optional integrations that only make sense for an organisation that has the
# matching third-party tool / AWS set-up. They are OFF unless listed in the
# ENABLED_FEATURES env var (comma-separated), e.g.
#   ENABLED_FEATURES=aws_ams,s2s_vpn,vpn_lookup,meraki
# "freshservice" is switched on automatically when FRESHSERVICE_DOMAIN is set.
OPTIONAL_FEATURES = (
    "freshservice",      # ticket escalation / sync with Freshservice
    "aws_ams",           # raise AWS Managed Services (AMS) cases from the Portal
    "s2s_vpn",           # Site-to-Site VPN creation wizard
    "vpn_lookup",        # Site-to-Site VPN profile lookup
    "whitelist_lms",     # WAF IPSet / security-group IP whitelist tool
    "devops_agent",      # AWS DevOps Agent chat
    "security_portal",   # read-only view of an external security dashboard
    "screensaver",       # screensaver image upload to S3
    "licensing_dashboard",  # link to an external licensing dashboard
    "meraki",            # Cisco Meraki access-point monitor
    "fortigate",         # FortiManager Cloud monitor
    "sonicwall",         # SonicWall monitor
    "azure_monitor",     # Azure subscription monitoring
    "fs_calendar",       # Freshservice change calendar
    "sr_ticket",         # external service-request ticket system
    "resource_inventory",  # AWS Resources tab (S3 inventory scanner output)
)


def features() -> dict[str, bool]:
    """Which optional, organisation-specific integrations are switched on.

    The frontend reads this (GET /api/org-config) to hide menu entries whose
    integration is not set up, so a fresh deployment only shows what works.
    """
    enabled = {f.lower() for f in _csv("ENABLED_FEATURES")}
    out = {name: name in enabled for name in OPTIONAL_FEATURES}
    if FRESHSERVICE_DOMAIN:
        out["freshservice"] = True
    return out


def public_config(*, include_admin_info: bool = False) -> dict:
    """Non-secret values the frontend needs.

    `include_admin_info` adds the founding-admin / founder e-mails; only pass
    True for callers that are already admins.
    """
    core = CORE_ACCOUNT_ID
    cfg = {
        "org_name": ORG_NAME,
        "portal_domain": PORTAL_DOMAIN,
        "email_domain": ORG_EMAIL_DOMAIN,
        "portal_host_account_id": PORTAL_HOST_ACCOUNT_ID,
        "core_account_id": core,
        # Account used for AMS cases (defaults to the core account), the
        # network/VPN account, and the external licensing dashboard link.
        "ams_account_id": os.environ.get("AMS_ACCOUNT_ID", "").strip() or core,
        "vpn_account_id": os.environ.get("VPN_LOOKUP_ACCOUNT_ID", "").strip()
        or os.environ.get("S2S_VPN_ACCOUNT_ID", "").strip(),
        "licensing_dashboard_url": os.environ.get("LICENSING_DASHBOARD_URL", "").strip(),
        "security_portal_public_url": os.environ.get("SECURITY_PORTAL_PUBLIC_URL", "").strip(),
        "features": features(),
    }
    if include_admin_info:
        cfg["founding_admin_emails"] = sorted(_csv("SEED_ADMIN_EMAILS"))
        cfg["founder_emails"] = sorted(_csv("PORTAL_FOUNDER_EMAILS"))
    return cfg
