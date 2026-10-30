"""
Deployment configuration for the Cloud Infra Job Portal CDK app.

Everything that is specific to *your* organisation / AWS environment is read
from a JSON file instead of being hard-coded in the stacks:

    infra-cdk/config.json          (git-ignored; copy config.example.json)

or from the file named by the PORTAL_CONFIG_FILE environment variable. Any key
can also be overridden by an environment variable named PORTAL_<KEY IN UPPER
CASE>, e.g. PORTAL_ACCOUNT_ID, PORTAL_CUSTOM_DOMAIN.

Required keys: account_id, custom_domain, acm_certificate_arn, azure_tenant_id,
azure_client_id, azure_client_secret_arn, seed_admin_emails.
Everything else has a safe default. See config.example.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_REQUIRED = (
    "account_id",
    "custom_domain",
    "acm_certificate_arn",
    "azure_tenant_id",
    "azure_client_id",
    "azure_client_secret_arn",
    "seed_admin_emails",
)

_DEFAULTS: dict[str, Any] = {
    "region": "us-east-1",
    "org_name": "Your Organization",
    "org_email_domain": "",
    "api_domain": "",
    "ses_sender_address": "",
    "freshservice_domain": "",
    "seed_member_emails": "",
    "bedrock_account_id": "",          # empty = use this account for Bedrock
    "bedrock_role_name": "JobPortal-Bedrock-Invoke",
    "core_account_id": "",
    "enabled_features": "",
    "website_monitor_region": "ap-southeast-1",
    # Extra environment variables passed straight to the backend container, e.g.
    # S2S_VPN_TGW_CONFIG, SECURITY_PORTAL_API_BASE, KNOWN_BUSINESS_UNITS, ...
    "extra_env": {},
}


def _load() -> dict[str, Any]:
    path = Path(os.environ.get("PORTAL_CONFIG_FILE") or Path(__file__).resolve().parents[1] / "config.json")
    data: dict[str, Any] = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    cfg = {**_DEFAULTS, **data}
    for key in list(_DEFAULTS) + list(_REQUIRED):
        env_val = os.environ.get(f"PORTAL_{key.upper()}")
        if env_val not in (None, ""):
            cfg[key] = env_val
    missing = [k for k in _REQUIRED if not cfg.get(k)]
    if missing:
        raise RuntimeError(
            "Missing deployment configuration: " + ", ".join(missing)
            + f". Copy infra-cdk/config.example.json to {path} and fill it in."
        )
    return cfg


CONFIG = _load()
