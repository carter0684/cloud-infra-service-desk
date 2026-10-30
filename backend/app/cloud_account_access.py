"""
AWS access path and connection test for registry accounts
(.kiro/specs/cloud-account-onboarding, task 3).

Two ways to reach a member account, chosen per registry entry
(`access_mode`):

  "legacy"   -> exactly as before the registry: Core account static keys ->
                sts:AssumeRole(PortalReadOnlyRole). Untouched; the client
                factories in cost_explorer / cmdb / security_hub /
                health_member_accounts only branch here for "infraops".

  "infraops" -> the ECS task's OWN identity (JobPortalTaskRole; no static
                keys) -> sts:AssumeRole(JobPortal-CrossAccount-InfraOps-XXXX)
                with that account's own External_Id. XXXX is the FIRST 4
                digits of the account ID (the convention vpn_lookup.py
                already uses). The role is created in the target account by
                the Admin from the CloudFormation template this module
                renders; the Portal never creates roles in other accounts.

vpn_lookup.py keeps its own session function and its shared External_Id;
the two mechanisms do not interact.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import boto3

from . import cloud_account_store

logger = logging.getLogger(__name__)

ROLE_NAME_PREFIX = "JobPortal-CrossAccount-InfraOps-"
ROLE_SESSION_NAME = "jobportal-infra-ops"
SESSION_DURATION_SECONDS = int(os.environ.get("CLOUD_ACCOUNT_SESSION_DURATION_SECONDS", "3600"))
# Refresh assumed credentials this long before they expire.
_REFRESH_MARGIN = timedelta(minutes=5)

TEMPLATE_PATH = Path(__file__).parent / "cloud_account_templates" / "infraops_role.yaml"

_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")
_ROLE_NAME_RE = re.compile(r"^JobPortal-CrossAccount-InfraOps-\d{4}$")
_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_TASK_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+$")


class AccessError(Exception):
    """The InfraOps session could not be obtained. `message` is safe to log and to show."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# ── Names and ARNs ───────────────────────────────────────────────────────

def role_name_for(account_id: str) -> str:
    """JobPortal-CrossAccount-InfraOps-XXXX, XXXX = first 4 digits of the account ID."""
    if not _ACCOUNT_ID_RE.match(account_id or ""):
        raise ValueError("AWS account ID must be exactly 12 digits")
    return f"{ROLE_NAME_PREFIX}{account_id[:4]}"


def role_arn_for(account_id: str, role_name: Optional[str] = None) -> str:
    return f"arn:aws:iam::{account_id}:role/{role_name or role_name_for(account_id)}"


_task_role_arn_cache: Optional[str] = None


def task_role_arn() -> str:
    """ARN of the Portal's task role (what the InfraOps role must trust).

    Env JOBPORTAL_TASK_ROLE_ARN wins; otherwise it is derived from
    sts:GetCallerIdentity, whose assumed-role ARN
    arn:aws:sts::<acct>:assumed-role/<Role>/<session> is converted to
    arn:aws:iam::<acct>:role/<Role>. Cached for the life of the process."""
    global _task_role_arn_cache
    if _task_role_arn_cache:
        return _task_role_arn_cache
    arn = os.environ.get("JOBPORTAL_TASK_ROLE_ARN", "").strip()
    if not arn:
        caller = _sts_client().get_caller_identity()["Arn"]
        m = re.match(r"^arn:aws:sts::(\d{12}):assumed-role/([^/]+)/", caller)
        if not m:
            raise AccessError("The Portal is not running under an IAM role, so its role ARN cannot be determined.")
        arn = f"arn:aws:iam::{m.group(1)}:role/{m.group(2)}"
    if not _TASK_ROLE_ARN_RE.match(arn):
        raise AccessError("The Portal role ARN is not in the expected format.")
    _task_role_arn_cache = arn
    return arn


def _sts_client():
    """STS client using the task's own credentials (container credential provider).
    A module-level function so tests can replace it."""
    return boto3.client("sts")


# ── InfraOps sessions ────────────────────────────────────────────────────

_sessions: dict[str, tuple[boto3.Session, datetime]] = {}
_sessions_lock = threading.Lock()


def invalidate(account_id: Optional[str] = None) -> None:
    """Forget cached assumed-role sessions (one account, or all)."""
    with _sessions_lock:
        if account_id is None:
            _sessions.clear()
        else:
            _sessions.pop(account_id, None)


def _assume(entry: dict) -> dict:
    """AssumeRole for an infraops entry; returns the STS Credentials dict."""
    account_id = entry["identifier"]
    role_arn = role_arn_for(account_id, entry.get("role_name"))
    external_id = entry.get("external_id")
    if not external_id:
        raise AccessError("This account has no External ID yet. Open its setup screen first.")
    resp = _sts_client().assume_role(
        RoleArn=role_arn,
        RoleSessionName=ROLE_SESSION_NAME,
        ExternalId=external_id,
        DurationSeconds=SESSION_DURATION_SECONDS,
    )
    return resp["Credentials"]


def session(account_id: str) -> boto3.Session:
    """boto3 Session for an onboarded (infraops) account, cached per account and
    refreshed shortly before expiry. Raises AccessError for an unknown,
    offboarded or non-infraops account, or if the role cannot be assumed."""
    now = datetime.now(timezone.utc)
    with _sessions_lock:
        cached = _sessions.get(account_id)
        if cached and cached[1] > now + _REFRESH_MARGIN:
            return cached[0]

    entry = cloud_account_store.find("aws", account_id)
    if not entry or entry.get("status") == "offboarded":
        raise AccessError(f"Account {account_id} is not onboarded.")
    if entry.get("access_mode") != "infraops":
        raise AccessError(f"Account {account_id} does not use the InfraOps role.")
    try:
        creds = _assume(entry)
    except AccessError:
        raise
    except Exception as e:
        code, message = classify_error(e, account_id, "infraops")
        logger.warning("cloud_account_access: assume failed for %s: %s", account_id, code)
        raise AccessError(message) from e

    sess = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    with _sessions_lock:
        _sessions[account_id] = (sess, creds["Expiration"])
    return sess


def client(account_id: str, service: str, region_name: str = "us-east-1", config=None):
    """boto3 client for `service` in an infraops account."""
    kwargs = {"region_name": region_name}
    if config is not None:
        kwargs["config"] = config
    return session(account_id).client(service, **kwargs)


# ── Connection test ──────────────────────────────────────────────────────

def classify_error(exc: Exception, account_id: str, mode: str) -> tuple[str, str]:
    """(code, message) for a failed connection attempt. The message is written by
    the Portal: raw AWS error text, credentials and External_Ids never reach it."""
    from botocore.exceptions import (
        ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError, ClientError,
    )

    if isinstance(exc, (ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError)):
        return "network", "Could not reach AWS STS. Try again in a moment."
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "")) or "Unknown"
        if code in ("AccessDenied", "AccessDeniedException"):
            if mode == "infraops":
                return "access_denied", (
                    "Role not found, or its trust policy or External ID does not match. Check that the "
                    f"CloudFormation stack was deployed in account {account_id} and that it uses the "
                    "External ID shown on this screen."
                )
            return "access_denied", (
                f"The Portal cannot assume the read-only role in account {account_id}. "
                "Check that the role exists and trusts the Portal's Core account identity."
            )
        if code in ("ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "SignatureDoesNotMatch"):
            return "portal_credentials", "The Portal's own AWS credentials were rejected. Contact a Portal admin."
        safe = re.sub(r"[^A-Za-z0-9_.-]", "", code)[:60] or "Unknown"
        return "aws_error", f"AWS returned an error ({safe})."
    from .cost_explorer import CostAnalysisError

    if isinstance(exc, CostAnalysisError):
        return "portal_credentials", "The Portal could not load its Core account credentials. Contact a Portal admin."
    if isinstance(exc, AccessError):
        return "access_error", exc.message
    return "unexpected", "Unexpected error while testing the connection."


def _caller_account(creds: dict) -> str:
    sts = boto3.client(
        "sts",
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    return sts.get_caller_identity()["Account"]


def test_aws_connection(entry: dict) -> dict:
    """Verify an AWS registry entry. Never raises.

    infraops: AssumeRole(InfraOps role, External_Id) -> GetCallerIdentity -> account matches
    legacy:   Core account keys -> AssumeRole(PortalReadOnlyRole) -> GetCallerIdentity -> account matches
              (the Core account itself is checked with its own keys directly)

    Returns {"ok": bool, "code": str, "message": str}."""
    account_id = entry.get("identifier", "")
    mode = entry.get("access_mode") or "legacy"
    try:
        if mode == "infraops":
            creds = _assume(entry)
            caller = _caller_account(creds)
        else:
            from . import cost_explorer

            core = cost_explorer._core_session()
            if account_id == cost_explorer.CORE_ACCOUNT_ID:
                caller = core.client("sts", region_name="us-east-1").get_caller_identity()["Account"]
            else:
                assumed = core.client("sts", region_name="us-east-1").assume_role(
                    RoleArn=f"arn:aws:iam::{account_id}:role/{cost_explorer.READONLY_ROLE_NAME}",
                    RoleSessionName="job-portal-connection-test",
                )["Credentials"]
                caller = _caller_account(assumed)
    except Exception as e:
        code, message = classify_error(e, account_id, mode)
        if code == "unexpected":
            logger.exception("cloud_account_access: unexpected error testing %s", account_id)
        else:
            logger.info("cloud_account_access: test for %s failed: %s", account_id, code)
        return {"ok": False, "code": code, "message": message}

    if caller != account_id:
        return {
            "ok": False,
            "code": "account_mismatch",
            "message": "The role answered from a different AWS account than the one entered. Check the account ID.",
        }
    return {"ok": True, "code": "ok", "message": "Connected."}


# ── CloudFormation template ──────────────────────────────────────────────

def render_template(entry: dict, task_role: Optional[str] = None) -> str:
    """The CloudFormation YAML an Admin deploys in the target account, with the
    role name, Portal role ARN and External_Id filled in. Values are validated
    against strict patterns first so nothing can break out of the YAML strings."""
    account_id = entry.get("identifier", "")
    role_name = entry.get("role_name") or role_name_for(account_id)
    external_id = entry.get("external_id") or ""
    trusted = task_role or task_role_arn()

    if not _ACCOUNT_ID_RE.match(account_id):
        raise ValueError("invalid AWS account ID")
    if not _ROLE_NAME_RE.match(role_name):
        raise ValueError("invalid role name")
    if not _EXTERNAL_ID_RE.match(external_id):
        raise ValueError("invalid External ID")
    if not _TASK_ROLE_ARN_RE.match(trusted):
        raise ValueError("invalid Portal role ARN")

    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    for marker, value in (
        ("{{ACCOUNT_ID}}", account_id),
        ("{{ROLE_NAME}}", role_name),
        ("{{TASK_ROLE_ARN}}", trusted),
        ("{{EXTERNAL_ID}}", external_id),
    ):
        text = text.replace(marker, value)
    return text


# ── What the assumed role can do ─────────────────────────────────────────

# IAM actions used to answer "can this role change anything?". They are run through
# iam:SimulatePrincipalPolicy against the role itself (nothing is executed). A role
# that only has ReadOnlyAccess must come back denied for every one of them.
WRITE_PROBES = (
    "s3:PutObject", "s3:DeleteObject", "s3:DeleteBucket", "s3:PutBucketPolicy",
    "ec2:RunInstances", "ec2:TerminateInstances", "ec2:AuthorizeSecurityGroupIngress",
    "ec2:ModifyInstanceAttribute", "ec2:DeleteVpc",
    "iam:CreateUser", "iam:CreateAccessKey", "iam:AttachRolePolicy", "iam:PutRolePolicy", "iam:PassRole",
    "dynamodb:PutItem", "dynamodb:DeleteTable", "rds:DeleteDBInstance", "rds:ModifyDBInstance",
    "lambda:CreateFunction", "lambda:UpdateFunctionCode", "lambda:InvokeFunction",
    "kms:ScheduleKeyDeletion", "kms:Decrypt", "cloudformation:DeleteStack",
    "route53:ChangeResourceRecordSets", "sqs:DeleteMessage", "sns:Publish",
    "secretsmanager:PutSecretValue", "organizations:LeaveOrganization",
)

# Actions that read DATA (not just resource details). They are allowed by ReadOnlyAccess;
# listing them tells the account owner what that policy really exposes.
DATA_READ_PROBES = (
    "s3:GetObject", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan",
    "ssm:GetParameter", "logs:FilterLogEvents", "sqs:ReceiveMessage",
    "secretsmanager:GetSecretValue", "lambda:GetFunction",
)

_SIMULATE_CHUNK = 25
_PERMISSIONS_UNAVAILABLE = "The role was assumed, but its policies could not be read ({code})."


def _paginate(fn, key, **kwargs) -> list:
    out: list = []
    marker = None
    for _ in range(20):  # hard bound; a role never has this many policies
        resp = fn(**kwargs, **({"Marker": marker} if marker else {}))
        out.extend(resp.get(key, []))
        if not resp.get("IsTruncated"):
            return out
        marker = resp.get("Marker")
    return out


def _error_code(exc: Exception) -> str:
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) or type(exc).__name__
    return re.sub(r"[^A-Za-z0-9_.-]", "", code)[:60] or "Unknown"


def _simulate(iam, role_arn: str, actions: tuple) -> dict:
    """{action: decision} for the given actions, run through SimulatePrincipalPolicy."""
    decisions: dict[str, str] = {}
    for i in range(0, len(actions), _SIMULATE_CHUNK):
        chunk = list(actions[i:i + _SIMULATE_CHUNK])
        marker = None
        for _ in range(10):
            resp = iam.simulate_principal_policy(
                PolicySourceArn=role_arn, ActionNames=chunk, ResourceArns=["*"],
                **({"Marker": marker} if marker else {}),
            )
            for r in resp.get("EvaluationResults", []):
                decisions[r["EvalActionName"]] = r.get("EvalDecision", "")
            if not resp.get("IsTruncated"):
                break
            marker = resp.get("Marker")
    return decisions


def describe_role_permissions(entry: dict) -> dict:
    """What can the role the Portal assumes in this AWS account do? Never raises.

    The role is assumed exactly as for a connection test; if that fails the result is
    {"ok": False, "code", "message"}. Otherwise it lists the role's attached managed
    policies, inline policy names and permissions boundary, and runs a write-action
    check with iam:SimulatePrincipalPolicy (nothing is executed). A role that was
    restricted so it cannot read its own policies still returns ok=True, with the part
    that could not be read marked unavailable."""
    from datetime import datetime, timezone

    account_id = entry.get("identifier", "")
    mode = entry.get("access_mode") or "legacy"
    try:
        if mode == "infraops":
            creds = _assume(entry)
            role_name = entry.get("role_name") or role_name_for(account_id)
        else:
            from . import cost_explorer

            core = cost_explorer._core_session()
            if account_id == cost_explorer.CORE_ACCOUNT_ID:
                ident = core.client("sts", region_name="us-east-1").get_caller_identity()
                return {
                    "ok": True,
                    "identity": {"account": ident["Account"], "kind": "iam_user", "arn": ident["Arn"]},
                    "managed_policies": [], "inline_policies": [],
                    "simulation": {"available": False, "reason": "direct_keys"},
                    "summary": ("The Portal reaches this account with an IAM user's access key, not an assumed role. "
                                "That user's permissions are managed in the account itself."),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
            creds = core.client("sts", region_name="us-east-1").assume_role(
                RoleArn=f"arn:aws:iam::{account_id}:role/{cost_explorer.READONLY_ROLE_NAME}",
                RoleSessionName="job-portal-permissions-view",
            )["Credentials"]
            role_name = cost_explorer.READONLY_ROLE_NAME
    except Exception as e:
        code, message = classify_error(e, account_id, mode)
        if code == "unexpected":
            logger.exception("cloud_account_access: unexpected error assuming %s for permissions", account_id)
        return {"ok": False, "code": code, "message": message}

    return _describe_assumed(creds, account_id, role_name, mode)


# The shared InfraOps role: JobPortal-CrossAccount-InfraOps-XXXX provisioned BY HAND in an
# account for the write-capable tools (VPN Profile Lookup, S2S VPN Profile Creation), assumed
# with the Portal task's own identity and this fixed External ID (vpn_lookup.py uses the same
# value, from the same env var). It is separate from the PortalReadOnlyRole the Portal uses for
# read-only reporting, and from the per-account roles onboarded on the Cloud Accounts page.
SHARED_INFRAOPS_EXTERNAL_ID = os.environ.get("VPN_LOOKUP_INFRA_OPS_EXTERNAL_ID", "jobportal-infra-ops")
SHARED_INFRAOPS_PURPOSE = "VPN Profile Lookup and S2S VPN Profile Creation (write-capable infrastructure operations)"
READONLY_ROLE_PURPOSE = "Read-only reporting: Cost Analysis, Billing, CMDB, Security Hub, AWS Health, WAF Monitoring, AWS Resources"


def describe_shared_infraops_role(entry: dict) -> dict:
    """Does this account ALSO have the JobPortal-CrossAccount-InfraOps-XXXX role, and what can it do?

    Same result shape as describe_role_permissions. If the Portal cannot assume the role the
    result is {"ok": False, "code": "not_provisioned", ...}: the usual reason is simply that the
    role was never created in that account. Never raises."""
    account_id = entry.get("identifier", "")
    try:
        role_name = role_name_for(account_id)
        resp = _sts_client().assume_role(
            RoleArn=role_arn_for(account_id, role_name),
            RoleSessionName=ROLE_SESSION_NAME,
            ExternalId=SHARED_INFRAOPS_EXTERNAL_ID,
            DurationSeconds=SESSION_DURATION_SECONDS,
        )
        creds = resp["Credentials"]
    except Exception as e:
        code, message = classify_error(e, account_id, "infraops")
        if code == "access_denied":
            return {
                "ok": False, "code": "not_provisioned",
                "message": (f"This account does not have the {ROLE_NAME_PREFIX}{account_id[:4]} role, or it does "
                            "not trust the Portal with the expected External ID. Only accounts where it was "
                            "created (for example network) have it."),
            }
        if code == "unexpected":
            logger.exception("cloud_account_access: unexpected error probing the InfraOps role in %s", account_id)
        return {"ok": False, "code": code, "message": message}
    return _describe_assumed(creds, account_id, role_name, "infraops")


def _describe_assumed(creds: dict, account_id: str, role_name: str, mode: str) -> dict:
    """Policies and write-access simulation for a role the Portal has just assumed. Never raises."""
    from datetime import datetime, timezone

    try:
        iam = boto3.client(
            "iam",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
    except Exception as e:
        code, message = classify_error(e, account_id, mode)
        return {"ok": False, "code": code, "message": message}

    result: dict = {
        "ok": True,
        "identity": {"account": account_id, "kind": "role", "role_name": role_name,
                     "arn": f"arn:aws:iam::{account_id}:role/{role_name}"},
        "managed_policies": [], "inline_policies": [],
        "simulation": {"available": False},
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    role_arn = result["identity"]["arn"]
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
        role_arn = role.get("Arn", role_arn)
        result["identity"]["arn"] = role_arn
        result["identity"]["max_session_duration"] = role.get("MaxSessionDuration")
        boundary = (role.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")
        result["identity"]["permissions_boundary"] = boundary
        attached = _paginate(iam.list_attached_role_policies, "AttachedPolicies", RoleName=role_name)
        result["managed_policies"] = [
            {"name": p.get("PolicyName"), "arn": p.get("PolicyArn"),
             "aws_managed": str(p.get("PolicyArn", "")).startswith("arn:aws:iam::aws:policy/")}
            for p in attached
        ]
        result["inline_policies"] = list(_paginate(iam.list_role_policies, "PolicyNames", RoleName=role_name))
    except Exception as e:
        result["policies_error"] = _PERMISSIONS_UNAVAILABLE.format(code=_error_code(e))

    try:
        writes = _simulate(iam, role_arn, WRITE_PROBES)
        reads = _simulate(iam, role_arn, DATA_READ_PROBES)
        write_allowed = sorted(a for a in WRITE_PROBES if writes.get(a) == "allowed")
        data_allowed = sorted(a for a in DATA_READ_PROBES if reads.get(a) == "allowed")
        result["simulation"] = {
            "available": True,
            "write_checked": len(WRITE_PROBES), "write_allowed": write_allowed,
            "data_read_checked": len(DATA_READ_PROBES), "data_read_allowed": data_allowed,
        }
        if write_allowed:
            result["summary"] = (f"This role can change things: {len(write_allowed)} of {len(WRITE_PROBES)} "
                                 "write actions checked are allowed. Review its policies.")
        else:
            result["summary"] = (f"Read-only: none of the {len(WRITE_PROBES)} write actions checked are allowed.")
    except Exception as e:
        result["simulation"] = {"available": False, "reason": _error_code(e)}
        result["summary"] = "The write-access check could not run, so the effective permissions are unknown."
    return result
