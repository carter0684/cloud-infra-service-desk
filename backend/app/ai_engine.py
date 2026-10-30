"""
AWS Bedrock AI engine for the Cloud Infra Job Portal.

Chat model  : openai.gpt-6.1-terra          (GPT-6.1 Terra via the bedrock-mantle
              Responses API — reasoning + Bedrock built-in Web Search)
Analysis    : us.anthropic.claude-sonnet-5-5 (Claude Sonnet 5.5, Converse API,
              US cross-Region inference profile)
Fallback    : same model (a second attempt — see MODEL_ANALYSIS_FALLBACK)

Cross-account access
--------------------
Bedrock is used from a different AWS account than the one the portal runs
in.  When the BEDROCK_ROLE_ARN environment variable is set, every Bedrock
call (Converse *and* the signed mantle request) uses credentials obtained by
assuming that role; they are refreshed automatically before they expire.
When it is not set (local development, or a deployment that has Bedrock in
its own account) the default credential chain is used.

Web Search
----------
Claude has no server-side web search on Bedrock, so the two chat endpoints
(floating ChatBot and the AI Assistant tab) go through GPT-6.1 Terra's
`web_search` tool on the bedrock-mantle endpoint instead.  The tool is
invoked with external_web_access=False, so lookups are served only from the
Amazon Bedrock web index/cache and no request data leaves the AWS boundary.
mantle is the *only* endpoint that offers this tool — bedrock-runtime
rejects it.  Citations come back as `url_citation` annotations.
"""

import boto3
import httpx
import json
import math
import os
import re
import logging
import threading
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Model IDs ────────────────────────────────────────────────────────────────
BEDROCK_REGION = "us-east-1"

# GPT-6.1 Terra on bedrock-mantle (in-Region, us-east-1): the chat models use
# Bedrock Web Search, which only exists on that endpoint.
MODEL_CHAT    = "openai.gpt-6.1-terra"
MANTLE_RESPONSES_URL = f"https://bedrock-mantle.{BEDROCK_REGION}.api.aws/openai/v1/responses"
MANTLE_SIGNING_NAME = "bedrock-mantle"

# Claude Sonnet 5.5 through the US cross-Region inference profile (keeps
# inference inside US regions).  Bare model IDs are not invocable.
MODEL_ANALYSIS         = "us.anthropic.claude-sonnet-5-5"
# Nothing else is approved for these workloads, so the "fallback" is simply
# a second attempt with the same model (it still recovers from a transient
# error or a malformed JSON answer).
MODEL_ANALYSIS_FALLBACK = MODEL_ANALYSIS

# Keep these names exported so main.py imports stay valid
MODEL_PRIMARY = MODEL_ANALYSIS
MODEL_FALLBACK = MODEL_ANALYSIS_FALLBACK

SEVERITY_DEFINITIONS = {
    "critical": "Critical — business-critical system completely down, no workaround",
    "high":     "High — important functions impaired, partial workaround exists",
    "normal":   "Normal — system impaired, non-critical functions affected",
    "low":      "Low — general guidance, feature request, or minor issue",
}

# Extended read timeout — web search + reasoning can take up to ~60 s
_BEDROCK_CONFIG = Config(read_timeout=120, connect_timeout=10)

_session_lock = threading.Lock()
_session_cache: dict = {}   # role_arn ("" = default chain) -> boto3.Session


def _get_session() -> boto3.Session:
    """boto3 session used for every Bedrock call.

    If BEDROCK_ROLE_ARN is set, the session's credentials come from
    sts:AssumeRole on that (cross-account) role and refresh themselves
    shortly before expiry, so a long-running ECS task never ends up holding
    expired credentials.  Otherwise the default credential chain is used.
    The session is cached per role ARN.
    """
    role_arn = os.environ.get("BEDROCK_ROLE_ARN", "").strip()
    with _session_lock:
        cached = _session_cache.get(role_arn)
        if cached is not None:
            return cached

        if not role_arn:
            session = boto3.Session(region_name=BEDROCK_REGION)
        else:
            from botocore.credentials import (
                AssumeRoleCredentialFetcher,
                DeferredRefreshableCredentials,
            )
            from botocore.session import get_session as _botocore_session

            base = _botocore_session()
            fetcher = AssumeRoleCredentialFetcher(
                client_creator=base.create_client,
                source_credentials=base.get_credentials(),
                role_arn=role_arn,
                extra_args={
                    "RoleSessionName": "job-portal-bedrock",
                    "DurationSeconds": 3600,
                },
            )
            bc_session = _botocore_session()
            bc_session._credentials = DeferredRefreshableCredentials(
                method="assume-role",
                refresh_using=fetcher.fetch_credentials,
            )
            bc_session.set_config_variable("region", BEDROCK_REGION)
            session = boto3.Session(botocore_session=bc_session, region_name=BEDROCK_REGION)
            logger.info("Bedrock calls will assume role %s", role_arn)

        _session_cache[role_arn] = session
        return session


def _get_client():
    return _get_session().client(
        "bedrock-runtime",
        region_name=BEDROCK_REGION,
        config=_BEDROCK_CONFIG,
    )


def _converse_first_text(response: dict) -> str:
    """First non-empty text block of a Converse response (skips any
    reasoning block that may precede the answer)."""
    for block in response.get("output", {}).get("message", {}).get("content", []):
        if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip():
            return block["text"]
    raise ValueError("model returned no text")


# Claude Sonnet 5.5 "thinks" before answering and those hidden reasoning tokens
# are counted against `maxTokens`, together with the answer.  With the old
# Nova-sized limits (400-1500) the model regularly used the whole budget on
# reasoning/answer, stopped with stopReason=max_tokens and returned truncated
# JSON ("No JSON object found in model response").  So:
#   * reasoning effort is "low" (these are short structured-JSON tasks; at low
#     effort the model emits no reasoning block at all), and
#   * the caller's `max_tokens` is treated as the expected ANSWER size and
#     this much headroom is added on top as a safety margin.
CLAUDE_REASONING_EFFORT = "low"
CLAUDE_TOKEN_HEADROOM = 3000


def claude_converse_params(max_tokens: int) -> dict:
    """Converse keyword arguments (token limit + reasoning effort) shared by
    every Claude call in the portal.  No `temperature`: Sonnet 5.5 rejects it
    as deprecated."""
    return {
        "inferenceConfig": {"maxTokens": max_tokens + CLAUDE_TOKEN_HEADROOM},
        "additionalModelRequestFields": {"output_config": {"effort": CLAUDE_REASONING_EFFORT}},
    }


def _invoke(client, model_id: str, prompt: str, max_tokens: int = 1200) -> str:
    """Simple single-turn call via the Converse API (used for analysis/extraction).

    `max_tokens` is the expected answer size; see claude_converse_params().
    """
    response = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        **claude_converse_params(max_tokens),
    )
    if response.get("stopReason") == "max_tokens":
        logger.warning("%s stopped at the token limit — the answer is probably truncated", model_id)
    return _converse_first_text(response)


def _converse_to_responses_input(messages: list) -> list[dict]:
    """Converse-style messages ([{"role", "content": [{"text": ...}]}]) →
    Responses API input items ([{"role", "content": "<text>"}])."""
    items: list[dict] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = "".join(
                b.get("text", "") for b in (content or []) if isinstance(b, dict)
            )
        if text.strip():
            items.append({"role": m.get("role", "user"), "content": text})
    return items


def _mantle_post(body: dict, timeout: float = 120.0) -> dict:
    """SigV4-signed POST to the bedrock-mantle Responses endpoint using the
    same (possibly assumed-role) credentials as every other Bedrock call."""
    creds = _get_session().get_credentials()
    if creds is None:
        raise RuntimeError("No AWS credentials available for Bedrock")
    frozen = creds.get_frozen_credentials()

    payload = json.dumps(body)
    request = AWSRequest(
        method="POST",
        url=MANTLE_RESPONSES_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(frozen, MANTLE_SIGNING_NAME, BEDROCK_REGION).add_auth(request)

    resp = httpx.post(
        MANTLE_RESPONSES_URL,
        content=payload,
        headers=dict(request.headers),
        timeout=httpx.Timeout(timeout, connect=10.0),
    )
    if resp.status_code != 200:
        # Keep the upstream error text short; it can echo request details.
        raise RuntimeError(f"Bedrock mantle returned HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def chat_with_grounding(
    client,
    messages: list,
    system_prompt: str,
    max_tokens: int = 1500,
    enable_thinking: bool = True,
) -> tuple[str, list[dict]]:
    """
    Multi-turn chat using GPT-6.1 Terra (bedrock-mantle Responses API) with:
      - Web Search  (`web_search` tool — the model searches autonomously; served
                     from the Bedrock web index only, external_web_access=False)
      - Reasoning   (effort "medium", or "low" when enable_thinking is False)
      - Conversation history support

    `client` is accepted for backward compatibility with existing callers but
    is not used: the web_search tool is not available on bedrock-runtime, so
    the request is signed and sent to bedrock-mantle directly.

    Returns (answer_text, citations_list).
    citations_list is a list of {"url": ..., "domain": ...} dicts.
    """
    body = {
        "model": MODEL_CHAT,
        "instructions": system_prompt,
        "input": _converse_to_responses_input(messages),
        # Reasoning tokens count against this limit, so leave headroom for
        # them on top of the answer length the caller asked for.
        "max_output_tokens": max_tokens + 1500,
        "reasoning": {"effort": "medium" if enable_thinking else "low"},
        "tools": [{"type": "web_search", "external_web_access": False}],
        # Do not keep the conversation server-side.
        "store": False,
    }
    response = _mantle_post(body)

    answer_parts: list[str] = []
    citations: list[dict] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if part.get("type") != "output_text":
                continue
            answer_parts.append(part.get("text", ""))
            for ann in part.get("annotations", []) or []:
                if ann.get("type") == "url_citation" and ann.get("url"):
                    url = ann["url"]
                    citations.append({
                        "url": url,
                        "domain": urlparse(url).netloc,
                    })

    answer = "".join(answer_parts).strip()

    if not answer:
        reason = (response.get("incomplete_details") or {}).get("reason") or response.get("status")
        raise RuntimeError(f"The chat model returned no answer ({reason})")

    # Defensive: strip any reasoning markup that leaked into the answer text.
    answer = re.sub(r"<thinking>.*?</thinking>", "", answer, flags=re.DOTALL).strip()

    return answer, citations


def _extract_json(text: str) -> dict:
    """Strip markdown fences and parse a JSON object from the model's
    response using an incremental decoder (json.JSONDecoder.raw_decode)
    rather than a greedy `\\{.*\\}` regex — the regex approach can span
    across multiple unrelated braces/objects in the same response and
    silently produce garbage. Scans each '{' position left-to-right and
    returns the first substring that parses as valid JSON *and* is a
    dict (a bare list/string/number is not an acceptable root here)."""
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip().strip("`").strip()
    decoder = json.JSONDecoder()
    start = 0
    while True:
        brace_pos = cleaned.find("{", start)
        if brace_pos == -1:
            break
        try:
            obj, _end = decoder.raw_decode(cleaned, brace_pos)
        except (json.JSONDecodeError, ValueError):
            start = brace_pos + 1
            continue
        if isinstance(obj, dict):
            return obj
        start = brace_pos + 1
    raise ValueError("No JSON object found in model response")


# ── Job analysis (unchanged API, upgraded fallback model) ────────────────────

def analyse_job(
    title: str,
    description: str,
    category: str,
    priority: str,
    audit_log: list,
    platform_hint: Optional[str] = None,
    kb_context: str = "",
) -> dict:
    """
    Analyse a cloud infrastructure job using Claude Sonnet 5.5 (structured JSON output).
    Retries once (MODEL_ANALYSIS_FALLBACK) if the first call fails.
    """
    combined = (title + " " + description).lower()
    if platform_hint:
        platform_context = f"The issue is on: {platform_hint}."
    elif any(k in combined for k in ["azure", "microsoft", "entra", "intune", "aad", "m365", "office 365", "teams", "aks"]):
        platform_context = "The issue appears to be on Microsoft Azure."
    elif any(k in combined for k in ["aliyun", "alibaba", "alicloud", "oss"]):
        platform_context = "The issue appears to be on Alibaba Cloud (Aliyun)."
    elif any(k in combined for k in ["aws", "ec2", "s3", "rds", "iam", "vpc", "eks", "lambda", "cloudwatch", "route53", "elb", "alb", "nlb"]):
        platform_context = "The issue appears to be on Amazon Web Services (AWS)."
    else:
        platform_context = "The cloud platform is not clearly specified — provide general multi-cloud guidance."

    last_events = ""
    if audit_log:
        last_events = "\n".join(
            f"- {e.get('timestamp', '')[:16]}: {e.get('event', '')}"
            for e in audit_log[-5:]
        )

    prompt = f"""You are a senior cloud infrastructure engineer with deep expertise in AWS, Microsoft Azure, and Alibaba Cloud (Aliyun).

{kb_context + chr(10) if kb_context else ""}A support job has been raised:
- Title: {title}
- Description: {description}
- Category: {category}
- Priority: {priority}
- Recent activity:
{last_events or '  (no prior activity)'}
- Platform context: {platform_context}

Tasks:
1. Identify root cause and summarise clearly.
2. Determine cloud platform (AWS/Azure/Aliyun/Multi-cloud/General).
3. Suggest severity: critical/high/normal/low.
4. Provide exactly 4 actionable remediation steps with platform-specific commands.
5. Write a concise support ticket subject (max 100 chars).
6. Write a structured ticket description: Problem Summary, Steps to Reproduce, Expected Behaviour, Actual Behaviour, Impact.

IMPORTANT: Reply with valid JSON ONLY, no extra text:
{{
  "issue_summary": "...",
  "issue_type": "...",
  "cloud_platform": "AWS" | "Azure" | "Aliyun" | "Multi-cloud" | "General",
  "severity": "critical" | "high" | "normal" | "low",
  "recommended_steps": ["step 1", "step 2", "step 3", "step 4"],
  "ticket_subject": "...",
  "ticket_description": "..."
}}"""

    client = _get_client()
    used_model = MODEL_ANALYSIS
    try:
        raw_text = _invoke(client, MODEL_ANALYSIS, prompt)
    except Exception as e:
        logger.warning(f"{MODEL_ANALYSIS} failed ({e}), retrying with {MODEL_ANALYSIS_FALLBACK}")
        used_model = MODEL_ANALYSIS_FALLBACK
        raw_text = _invoke(client, MODEL_ANALYSIS_FALLBACK, prompt)

    try:
        data = _extract_json(raw_text)
    except Exception:
        data = {
            "issue_summary": title,
            "issue_type": category,
            "cloud_platform": "General",
            "severity": _priority_to_severity(priority),
            "recommended_steps": [raw_text[:500]],
            "ticket_subject": title[:100],
            "ticket_description": description,
        }

    severity = data.get("severity", _priority_to_severity(priority))
    return {
        "issue_summary":      data.get("issue_summary", title),
        "issue_type":         data.get("issue_type", category),
        "cloud_platform":     data.get("cloud_platform", "General"),
        "severity":           severity,
        "severity_label":     SEVERITY_DEFINITIONS.get(severity, severity),
        "recommended_steps":  data.get("recommended_steps", [])[:5],
        "ticket_subject":     data.get("ticket_subject", title)[:100],
        "ticket_description": data.get("ticket_description", description),
        "generated_at":       datetime.utcnow().isoformat(),
        "model_used":         used_model,
        "bedrock_region":     BEDROCK_REGION,
    }


def _priority_to_severity(priority: str) -> str:
    return {"ASAP": "critical", "High": "high", "Medium": "normal", "Low": "low"}.get(priority, "normal")


# ── AWS Health event AI suggestion ────────────────────────────────────────────

def analyse_health_event(
    event_type_code: str,
    service: str,
    region: str,
    status_code: str,
    event_description: Optional[str],
    affected_accounts: list,
    affected_entities: list,
) -> dict:
    """
    Analyse an AWS Health Dashboard event using Claude Sonnet 5.5 (structured
    JSON output) and produce a short "what an engineer should do or be aware
    of" suggestion. Retries once if the first call fails.

    Kept intentionally lightweight/read-only — this only summarises and
    advises, it never takes any action on the user's behalf.
    """
    accounts_str = ", ".join(affected_accounts[:10]) if affected_accounts else "(none reported)"
    entities_str = ""
    if affected_entities:
        entities_str = "\n".join(
            f"- {e.get('entity_value', '?')} (account {e.get('aws_account_id', '?')}, status {e.get('status_code', '?')})"
            for e in affected_entities[:15]
        )

    prompt = f"""You are a senior cloud infrastructure engineer specialising in AWS operations and incident response.

An AWS Health Dashboard event was raised for this organization:
- Event type: {event_type_code}
- Service: {service}
- Region: {region}
- Current AWS status: {status_code}
- AWS's own description of the event:
{event_description or '(AWS did not provide a description for this event)'}
- Affected AWS accounts: {accounts_str}
- Affected resources:
{entities_str or '  (none reported)'}

Tasks:
1. In plain language, summarise what is actually happening and why it matters (1-2 sentences).
2. List up to 5 concrete things an engineer should DO (concrete actions — check X, patch Y, fail over Z, etc). Empty list is fine if this event is purely informational and needs no action.
3. List up to 4 things an engineer should be AWARE of / watch out for (risks, side effects, deadlines, things that could silently break). Empty list is fine if there's nothing notable.
4. Suggest an urgency level for how quickly this needs attention.

IMPORTANT: Reply with valid JSON ONLY, no extra text:
{{
  "summary": "...",
  "action_items": ["do this", "do that"],
  "awareness_items": ["watch out for this"],
  "urgency": "immediate" | "soon" | "monitor" | "informational"
}}"""

    client = _get_client()
    used_model = MODEL_ANALYSIS
    try:
        raw_text = _invoke(client, MODEL_ANALYSIS, prompt, max_tokens=900)
    except Exception as e:
        logger.warning(f"{MODEL_ANALYSIS} failed for health event analysis ({e}), retrying with {MODEL_ANALYSIS_FALLBACK}")
        used_model = MODEL_ANALYSIS_FALLBACK
        raw_text = _invoke(client, MODEL_ANALYSIS_FALLBACK, prompt, max_tokens=900)

    try:
        data = _extract_json(raw_text)
    except Exception:
        data = {
            "summary": raw_text[:500],
            "action_items": [],
            "awareness_items": [],
            "urgency": "monitor",
        }

    urgency = data.get("urgency") if data.get("urgency") in ("immediate", "soon", "monitor", "informational") else "monitor"
    return {
        "summary":          data.get("summary", ""),
        "action_items":     [str(x) for x in (data.get("action_items") or [])][:5],
        "awareness_items":  [str(x) for x in (data.get("awareness_items") or [])][:4],
        "urgency":          urgency,
        "generated_at":     datetime.utcnow().isoformat(),
        "model_used":       used_model,
        "bedrock_region":   BEDROCK_REGION,
    }


# ── AWS Security Hub — per-finding AI analysis ────────────────────────────────

def analyse_security_finding(finding: dict, account_alias: str, account_id: str, impacted_principals: Optional[dict] = None) -> dict:
    """
    Analyse a SINGLE AWS Security Hub finding (Claude Sonnet 5.5, retried
    once on failure) and produce a step-by-step "what to do about this"
    remediation plan — this is what runs automatically when a user clicks
    a finding row on the AWS Security Hub tab (no separate "AI Analysis"
    button needed for the per-finding case; the click IS the trigger).

    `impacted_principals` (from security_hub.get_impacted_principals(),
    optional) lists which IAM users/groups/roles are actually attached to
    the offending resource for IAM-policy findings — when present, the
    remediation plan explicitly names them (e.g. "remove this policy from
    group X, which will affect users Y and Z") instead of speaking only
    in the abstract about "the policy".

    Read-only / advisory only — never changes the finding's workflow
    status or any AWS resource.
    """
    remediation_hint = finding.get("remediation_text") or "(AWS did not provide remediation guidance for this control)"
    remediation_url = finding.get("remediation_url") or ""

    impacted_block = ""
    if impacted_principals and impacted_principals.get("supported"):
        lines = []
        groups = impacted_principals.get("directly_attached_groups") or []
        users = impacted_principals.get("directly_attached_users") or []
        roles = impacted_principals.get("directly_attached_roles") or []
        users_via_groups = impacted_principals.get("users_via_groups") or []
        if groups:
            lines.append("Directly attached IAM groups: " + ", ".join(g.get("group_name", "?") for g in groups))
        if users:
            lines.append("Directly attached IAM users: " + ", ".join(u.get("user_name", "?") for u in users))
        if roles:
            lines.append("Directly attached IAM roles: " + ", ".join(r.get("role_name", "?") for r in roles))
        if users_via_groups:
            lines.append("Users impacted via group membership: " + ", ".join(
                f"{u.get('user_name', '?')} (via group {u.get('via_group', '?')})" for u in users_via_groups
            ))
        if not lines:
            lines.append("This policy is not currently attached to any user, group, or role.")
        impacted_block = "\nImpacted IAM principals (from AWS IAM directly, live data):\n" + "\n".join(f"- {l}" for l in lines)

    prompt = f"""You are a senior cloud security engineer. A member of the security team just clicked on this AWS Security Hub finding and needs to know exactly what to do about it.

Finding details:
- Title: {finding.get('title') or 'Unknown'}
- Severity: {finding.get('severity') or 'Unknown'}
- AWS account: {account_alias} ({account_id})
- Security control ID: {finding.get('security_control_id') or finding.get('generator_id') or 'n/a'}
- Compliance status: {finding.get('compliance_status') or 'Unknown'}
- Resource type: {finding.get('resource_type') or 'Unknown'}
- Resource ID: {finding.get('resource_id') or 'Unknown'}
- Region: {finding.get('region') or 'Unknown'}
- AWS's own description: {finding.get('description') or '(no description provided)'}
- AWS's own remediation hint: {remediation_hint}
{f'- AWS remediation guide URL: {remediation_url}' if remediation_url else ''}
{impacted_block}

Tasks:
1. In 1-2 plain-language sentences, explain what this finding actually means and why it matters (avoid restating the title verbatim). If impacted IAM users/groups/roles are listed above, name them specifically in this explanation (e.g. "this over-privileged policy is attached to group X, giving users Y and Z full admin access").
2. Give a specific, ordered, step-by-step remediation plan (concrete AWS console/CLI actions — e.g. exact settings to change, exact commands, not vague advice like "review your configuration"). If specific users/groups/roles are impacted, name them explicitly in the relevant steps (e.g. "detach policy X from group Y" rather than just "detach the policy"). Assume the reader has AWS console/CLI access to this account.
3. List up to 3 things to be aware of / watch out for when applying the fix (side effects, downtime risk, dependent resources, cost impact, or specific users who will lose access).
4. Suggest an urgency level for fixing this.

IMPORTANT: Reply with valid JSON ONLY, no extra text:
{{
  "summary": "...",
  "remediation_steps": ["step 1", "step 2", "step 3"],
  "awareness_items": ["thing to watch out for"],
  "urgency": "immediate" | "soon" | "monitor" | "informational"
}}"""

    client = _get_client()
    used_model = MODEL_ANALYSIS
    data = None
    last_error: Optional[Exception] = None

    for model_id in (MODEL_ANALYSIS, MODEL_ANALYSIS_FALLBACK):
        try:
            raw_text = _invoke(client, model_id, prompt, max_tokens=1000)
        except Exception as e:
            logger.warning(f"Bedrock invocation failed for finding analysis with {model_id}: {e}")
            last_error = e
            continue
        try:
            data = _extract_json(raw_text)
        except Exception as e:
            logger.warning(f"Could not parse finding-analysis response from {model_id}: {e}")
            last_error = e
            continue
        if not isinstance(data.get("summary"), str) or not data.get("summary", "").strip():
            logger.warning(f"Finding-analysis response from {model_id} missing/empty summary")
            data = None
            continue
        used_model = model_id
        break

    if data is None:
        logger.error(f"All models failed to produce a usable finding analysis: {last_error}")
        data = {
            "summary": "Automated AI analysis is temporarily unavailable for this finding.",
            "remediation_steps": [remediation_hint] if remediation_hint else [],
            "awareness_items": [],
            "urgency": "monitor",
        }
        used_model = "fallback-static"

    urgency = data.get("urgency") if data.get("urgency") in ("immediate", "soon", "monitor", "informational") else "monitor"
    remediation_steps = [_clip(x, 400) for x in (data.get("remediation_steps") or [])][:8]
    awareness_items = [_clip(x, 300) for x in (data.get("awareness_items") or [])][:3]
    return {
        "summary": _clip(data.get("summary", ""), 800),
        "remediation_steps": remediation_steps,
        "awareness_items": awareness_items,
        "urgency": urgency,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_used": used_model,
        "bedrock_region": BEDROCK_REGION,
    }


# ── Field extraction (unchanged API) ─────────────────────────────────────────

def ai_extract_job_fields_bedrock(raw_text: str) -> dict:
    """Extract job fields from pasted email/file text using Claude Sonnet 5.5."""
    prompt = f"""You are a cloud infrastructure triage assistant. Extract key job information from the following text.

Text:
{raw_text[:4000]}

Return ONLY valid JSON:
{{
  "title": "short descriptive title (max 100 chars)",
  "description": "clean summary of the issue (max 1000 chars)",
  "category": "Networking" | "Compute" | "Storage" | "Security" | "AD/Identity" | "Other",
  "priority": "ASAP" | "High" | "Medium" | "Low",
  "cloud_platform": "AWS" | "Azure" | "Aliyun" | "Multi-cloud" | "General"
}}"""

    client = _get_client()
    try:
        raw = _invoke(client, MODEL_ANALYSIS, prompt, max_tokens=400)
    except Exception:
        raw = _invoke(client, MODEL_ANALYSIS_FALLBACK, prompt, max_tokens=400)

    try:
        data = _extract_json(raw)
        return {
            "title":          data.get("title", "")[:200],
            "description":    data.get("description", raw_text[:3000]),
            "category":       data.get("category", "Other"),
            "priority":       data.get("priority", "Medium"),
            "cloud_platform": data.get("cloud_platform", "General"),
            "raw_char_count": len(raw_text),
            "model_used":     MODEL_ANALYSIS,
        }
    except Exception:
        from .file_parser import ai_extract_job_fields as local_extract
        return local_extract(raw_text)


# ── AWS Cost Analysis — executive summary for senior management ──────────────

def _fmt_money(currency: str, amount) -> str:
    try:
        return f"{currency} {float(amount):,.2f}"
    except (TypeError, ValueError):
        return f"{currency} N/A"


def _fmt_pct(pct) -> str:
    """Render a percentage the way this dashboard expects: 'N/A' when the
    value isn't comparable (None), otherwise a signed one-decimal number.
    Never lets a raw None/NaN reach the prompt as the literal text
    'None%'."""
    if pct is None:
        return "N/A"
    try:
        f = float(pct)
    except (TypeError, ValueError):
        return "N/A"
    if math.isnan(f) or math.isinf(f):
        return "N/A"
    sign = "+" if f >= 0 else ""
    return f"{sign}{f:.1f}%"


def _clip(text, max_len: int) -> str:
    s = str(text) if text is not None else ""
    return s if len(s) <= max_len else s[: max_len - 1].rstrip() + "…"


def analyse_cost_summary(cost_summary: dict) -> dict:
    """
    Summarise the org-wide AWS Cost Analysis payload (from
    cost_explorer.get_organization_cost_summary()) into an executive-level
    narrative a Cloud Manager can hand straight to senior management:
    what we're spending, why it moved, and what to do about it.

    Uses Claude Sonnet 5.5 (structured JSON output), retried once —
    the fallback model is retried for Bedrock invocation failures AND for
    response-parsing/schema failures (a model that returns unparseable
    text is treated the same as one that failed to respond at all).
    Read-only / advisory only — never changes any AWS resource.
    """
    currency = cost_summary.get("currency", "USD")
    total_mtd = cost_summary.get("total_mtd_cost", 0) or 0
    total_last_month = cost_summary.get("total_last_month_cost", 0) or 0
    total_two_months_ago = cost_summary.get("total_two_months_ago_cost", 0) or 0
    mtd_pct = cost_summary.get("mtd_vs_last_month_pct")
    last_month_pct = cost_summary.get("last_month_vs_prior_pct")
    account_count = cost_summary.get("account_count", 0) or 0
    error_count = cost_summary.get("error_count", 0) or 0
    as_of = cost_summary.get("as_of", "")
    is_stale = bool(cost_summary.get("stale_fallback"))

    bu_lines = "\n".join(
        f"- {b.get('bu', 'Unknown')}: {_fmt_money(currency, b.get('mtd_cost'))} MTD across "
        f"{b.get('account_count', 0)} account(s) (was {_fmt_money(currency, b.get('last_month_cost'))} last month)"
        for b in (cost_summary.get("business_units") or [])[:10]
    )

    top_account_lines = "\n".join(
        f"- {a.get('account_name', a.get('account_id', 'unknown'))} ({a.get('account_id', '?')}): "
        f"{_fmt_money(currency, a.get('mtd_cost'))} MTD, {_fmt_pct(a.get('mtd_vs_last_month_pct'))} vs last month"
        for a in sorted(cost_summary.get("accounts") or [], key=lambda x: x.get("mtd_cost") or 0, reverse=True)[:8]
        if not a.get("error")
    )

    top_service_lines = "\n".join(
        f"- {s.get('service', 'unknown')}: {_fmt_money(currency, s.get('amount'))}"
        for s in (cost_summary.get("top_services_mtd") or [])[:8]
    )

    warnings = []
    if error_count > 0:
        warnings.append(
            f"{error_count} of {account_count} account(s) failed to fetch cost data this refresh — "
            "totals below EXCLUDE those accounts and may understate true org-wide spend."
        )
    if is_stale:
        warnings.append(
            "All accounts failed on the most recent refresh attempt — the figures below are the last "
            "known-good data, which may be older than the normal cache window."
        )
    warnings_block = ("\nData-quality warnings:\n" + "\n".join(f"- {w}" for w in warnings)) if warnings else ""

    prompt = f"""You are the Cloud Cost Manager preparing a short AWS cost briefing for senior management (CIO/CFO level). Be concise, business-focused, and avoid engineering jargon where possible.

Data as of: {as_of or 'unknown'}
Note: the "Month-to-date total" below is a partial-month figure (from the 1st of the current month through today), NOT a full-month projection — do not describe it as if it were a complete month's spend.
{warnings_block}

Organization-wide AWS spend:
- Month-to-date total (partial month): {_fmt_money(currency, total_mtd)}
- Last full month total: {_fmt_money(currency, total_last_month)}
- Month before that total: {_fmt_money(currency, total_two_months_ago)}
- MTD vs last month change: {_fmt_pct(mtd_pct)}
- Last month vs prior month change: {_fmt_pct(last_month_pct)}
- Accounts included: {account_count} ({error_count} failed to fetch)

Spend by Business Unit (MTD):
{bu_lines or '(no data)'}

Top spending AWS accounts (MTD):
{top_account_lines or '(no data)'}

Top AWS services by spend (MTD, org-wide):
{top_service_lines or '(no data)'}

Tasks:
1. Write a 2-3 sentence executive summary of current AWS spend and the overall trend (up/down/stable). Explicitly note that MTD is a partial month, not a full-month total.
2. Identify up to 3 key cost drivers (which BU/account/service is driving spend, and why if inferable from the service names).
3. Give up to 4 concrete cost-control recommendations a Cloud Manager could action this month (e.g. rightsizing, reserved instances/savings plans, unused resource cleanup, budget alerts, tagging for cost allocation). Be specific to AWS.
4. Give a one-line "bottom line" recommendation for management (approve as-is / needs review / needs immediate cost action).

IMPORTANT: Reply with valid JSON ONLY, no extra text:
{{
  "executive_summary": "...",
  "trend": "increasing" | "decreasing" | "stable",
  "key_cost_drivers": ["driver 1", "driver 2", "driver 3"],
  "recommendations": ["recommendation 1", "recommendation 2", "recommendation 3", "recommendation 4"],
  "bottom_line": "..."
}}"""

    client = _get_client()
    used_model = MODEL_ANALYSIS
    data = None
    last_error: Optional[Exception] = None

    for model_id in (MODEL_ANALYSIS, MODEL_ANALYSIS_FALLBACK):
        try:
            raw_text = _invoke(client, model_id, prompt, max_tokens=900)
        except Exception as e:
            logger.warning(f"Bedrock invocation failed for cost analysis with {model_id}: {e}")
            last_error = e
            continue
        try:
            data = _extract_json(raw_text)
        except Exception as e:
            logger.warning(f"Could not parse cost-analysis response from {model_id}: {e}")
            last_error = e
            continue
        if not isinstance(data.get("executive_summary"), str) or not data.get("executive_summary", "").strip():
            logger.warning(f"Cost-analysis response from {model_id} missing/empty executive_summary")
            data = None
            continue
        used_model = model_id
        break

    if data is None:
        logger.error(f"All models failed to produce a usable cost analysis: {last_error}")
        data = {
            "executive_summary": (
                f"Automated AI summary is temporarily unavailable. MTD spend is {_fmt_money(currency, total_mtd)} "
                f"({_fmt_pct(mtd_pct)} vs last month); last full month was {_fmt_money(currency, total_last_month)}."
            ),
            "trend": "stable",
            "key_cost_drivers": [],
            "recommendations": [],
            "bottom_line": "AI summary generation failed — review the cost table directly.",
        }
        used_model = "fallback-static"

    trend = data.get("trend") if data.get("trend") in ("increasing", "decreasing", "stable") else "stable"
    key_cost_drivers = [_clip(x, 300) for x in (data.get("key_cost_drivers") or [])][:3]
    recommendations = [_clip(x, 300) for x in (data.get("recommendations") or [])][:4]
    return {
        "executive_summary": _clip(data.get("executive_summary", ""), 1500),
        "trend": trend,
        "key_cost_drivers": key_cost_drivers,
        "recommendations": recommendations,
        "bottom_line": _clip(data.get("bottom_line", ""), 300),
        "as_of": as_of,
        "account_count": account_count,
        "error_count": error_count,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_used": used_model,
        "bedrock_region": BEDROCK_REGION,
    }


# ── AWS Security Hub — AI analysis + remediation plan ────────────────────────

def analyse_security_hub_summary(summary: dict) -> dict:
    """
    Summarise the org-wide AWS Security Hub findings payload (from
    security_hub.get_security_hub_summary()) into an AI executive summary
    plus a prioritized remediation plan — the "AI Analysis" button on the
    AWS Security Hub tab.

    Uses Claude Sonnet 5.5 (structured JSON output), retried once on failure.
    Read-only / advisory only — never changes any AWS resource, and never
    calls Security Hub's own BatchUpdateFindings (workflow status changes
    stay a manual, deliberate action for the security team in the AWS
    console or Security Hub itself).
    """
    totals = summary.get("totals") or {}
    severity_counts = totals.get("severity_counts") or {}
    total_findings = totals.get("total_findings", 0) or 0
    account_count = totals.get("account_count", 0) or 0
    enabled_count = totals.get("enabled_count", 0) or 0
    not_enabled_count = totals.get("not_enabled_count", 0) or 0
    error_count = totals.get("error_count", 0) or 0
    as_of = summary.get("as_of", "")

    severity_lines = "\n".join(
        f"- {label}: {severity_counts.get(label, 0)}"
        for label in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL")
        if severity_counts.get(label, 0) > 0
    )

    # Collect the highest-severity findings across all accounts (already
    # sorted per-account by severity in security_hub.py) for the model to
    # reason over concretely, capped so the prompt stays a reasonable size.
    finding_lines = []
    for acct in sorted(summary.get("accounts") or [], key=lambda a: (
        -(a.get("severity_counts", {}).get("CRITICAL", 0)),
        -(a.get("severity_counts", {}).get("HIGH", 0)),
    )):
        if acct.get("error") or not acct.get("enabled"):
            continue
        for f in (acct.get("findings") or [])[:6]:
            if f.get("severity") not in ("CRITICAL", "HIGH", "MEDIUM"):
                continue
            finding_lines.append(
                f"- [{f.get('severity')}] {acct.get('alias', acct.get('account_id'))}: "
                f"{f.get('title')} (control {f.get('security_control_id') or f.get('generator_id') or 'n/a'}, "
                f"resource type {f.get('resource_type') or 'unknown'})"
            )
            if len(finding_lines) >= 40:
                break
        if len(finding_lines) >= 40:
            break
    findings_block = "\n".join(finding_lines) or "(no CRITICAL/HIGH/MEDIUM findings)"

    warnings = []
    if error_count > 0:
        warnings.append(f"{error_count} of {account_count} account(s) failed to fetch — findings below may be incomplete.")
    if not_enabled_count > 0:
        warnings.append(f"{not_enabled_count} account(s) do not have AWS Security Hub enabled at all (no visibility into those accounts' security posture).")
    warnings_block = ("\nData-quality notes:\n" + "\n".join(f"- {w}" for w in warnings)) if warnings else ""

    prompt = f"""You are a Cloud Security Engineer preparing a findings summary and remediation plan for the security team, based on AWS Security Hub data across the organization's AWS accounts. Be concise and actionable.

Data as of: {as_of or 'unknown'}
{warnings_block}

Org-wide Security Hub coverage: {enabled_count} of {account_count} accounts have Security Hub enabled.

Active findings by severity (org-wide):
{severity_lines or '(none)'}
Total active findings: {total_findings}

Highest-priority findings observed (sample, up to 40, CRITICAL/HIGH/MEDIUM only):
{findings_block}

Tasks:
1. Write a 2-3 sentence executive summary of the current security posture and the most urgent risk area.
2. List up to 5 top issues to prioritize (most severe / most impactful first), each as a short, specific finding description.
3. For each top issue listed, give a concrete, actionable remediation step (what to actually do in AWS to fix it — be specific, e.g. "enable default encryption on S3 bucket X" not "review S3 settings").
4. Give a one-line "bottom line" risk rating for management (e.g. "Low risk, routine hygiene items only" / "Moderate risk, address within 30 days" / "High risk, immediate action required").

IMPORTANT: Reply with valid JSON ONLY, no extra text:
{{
  "executive_summary": "...",
  "risk_level": "low" | "moderate" | "high" | "critical",
  "top_issues": ["issue 1", "issue 2", "issue 3", "issue 4", "issue 5"],
  "remediation_plan": ["step for issue 1", "step for issue 2", "step for issue 3", "step for issue 4", "step for issue 5"],
  "bottom_line": "..."
}}"""

    client = _get_client()
    used_model = MODEL_ANALYSIS
    data = None
    last_error: Optional[Exception] = None

    for model_id in (MODEL_ANALYSIS, MODEL_ANALYSIS_FALLBACK):
        try:
            raw_text = _invoke(client, model_id, prompt, max_tokens=1400)
        except Exception as e:
            logger.warning(f"Bedrock invocation failed for Security Hub analysis with {model_id}: {e}")
            last_error = e
            continue
        try:
            data = _extract_json(raw_text)
        except Exception as e:
            logger.warning(f"Could not parse Security Hub analysis response from {model_id}: {e}")
            last_error = e
            continue
        if not isinstance(data.get("executive_summary"), str) or not data.get("executive_summary", "").strip():
            logger.warning(f"Security Hub analysis response from {model_id} missing/empty executive_summary")
            data = None
            continue
        used_model = model_id
        break

    if data is None:
        logger.error(f"All models failed to produce a usable Security Hub analysis: {last_error}")
        data = {
            "executive_summary": (
                f"Automated AI summary is temporarily unavailable. {total_findings} active findings across "
                f"{enabled_count} account(s) with Security Hub enabled — review the findings table directly."
            ),
            "risk_level": "moderate",
            "top_issues": [],
            "remediation_plan": [],
            "bottom_line": "AI summary generation failed — review the findings table directly.",
        }
        used_model = "fallback-static"

    risk_level = data.get("risk_level") if data.get("risk_level") in ("low", "moderate", "high", "critical") else "moderate"
    top_issues = [_clip(x, 300) for x in (data.get("top_issues") or [])][:5]
    remediation_plan = [_clip(x, 400) for x in (data.get("remediation_plan") or [])][:5]
    return {
        "executive_summary": _clip(data.get("executive_summary", ""), 1500),
        "risk_level": risk_level,
        "top_issues": top_issues,
        "remediation_plan": remediation_plan,
        "bottom_line": _clip(data.get("bottom_line", ""), 300),
        "as_of": as_of,
        "account_count": account_count,
        "enabled_count": enabled_count,
        "total_findings": total_findings,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_used": used_model,
        "bedrock_region": BEDROCK_REGION,
    }


# ── AWS Billing by Service — executive summary for senior management ─────────

def analyse_billing_summary(billing_report: dict) -> dict:
    """
    Summarise a Billing by Service payload (from
    billing_by_service.get_billing_report()) into an executive-level
    narrative — the per-account/per-service counterpart to
    analyse_cost_summary() above. Same model fallback strategy and the
    same JSON response contract (executive_summary/trend/
    key_cost_drivers/recommendations/bottom_line), so both AI summaries
    render with identical UI (see the Dashboard's Executive Summary
    section and the Billing by Service page's own AI Analysis button).
    Read-only / advisory only — never changes any AWS resource.
    """
    currency = "USD"
    months = billing_report.get("months") or []
    month_labels = billing_report.get("month_labels") or months
    accounts = billing_report.get("accounts") or []
    valid_accounts = [a for a in accounts if not a.get("error")]
    error_count = len(accounts) - len(valid_accounts)
    account_count = len(accounts)
    as_of = billing_report.get("as_of", "")
    is_mock = bool(billing_report.get("is_mock"))

    latest_month = months[-1] if months else None
    prev_month = months[-2] if len(months) >= 2 else None
    latest_label = month_labels[-1] if month_labels else "the latest month"
    prev_label = month_labels[-2] if len(month_labels) >= 2 else None

    def _month_total(month):
        if not month:
            return 0.0
        return sum((a.get("totals", {}).get(month, 0) or 0) for a in valid_accounts)

    total_latest = _month_total(latest_month)
    total_prev = _month_total(prev_month)
    pct_change = None
    if prev_month and total_prev:
        pct_change = (total_latest - total_prev) / total_prev * 100

    top_account_lines = "\n".join(
        f"- {a.get('alias', a.get('account_id', 'unknown'))} ({a.get('account_id', '?')}): "
        f"{_fmt_money(currency, a.get('totals', {}).get(latest_month, 0))} in {latest_label}"
        for a in sorted(valid_accounts, key=lambda x: x.get("totals", {}).get(latest_month, 0) or 0, reverse=True)[:8]
    )

    service_totals: dict[str, float] = {}
    for a in valid_accounts:
        for row in a.get("rows") or []:
            amt = (row.get("amounts") or {}).get(latest_month, 0) or 0
            if amt:
                svc = row.get("service", "Unknown")
                service_totals[svc] = service_totals.get(svc, 0.0) + amt
    top_service_lines = "\n".join(
        f"- {svc}: {_fmt_money(currency, amt)}"
        for svc, amt in sorted(service_totals.items(), key=lambda kv: kv[1], reverse=True)[:8]
    )

    warnings = []
    if error_count > 0:
        warnings.append(
            f"{error_count} of {account_count} account(s) failed to fetch billing data this refresh — "
            "totals below EXCLUDE those accounts and may understate true spend."
        )
    if is_mock:
        warnings.append("This is local mock data, not real AWS billing data.")
    warnings_block = ("\nData-quality warnings:\n" + "\n".join(f"- {w}" for w in warnings)) if warnings else ""

    prompt = f"""You are the Cloud Cost Manager preparing a short AWS Billing-by-Service briefing for senior management (CIO/CFO level). Be concise, business-focused, and avoid engineering jargon where possible.

Data as of: {as_of or 'unknown'}
Months covered: {', '.join(month_labels) if month_labels else 'unknown'}
{warnings_block}

Total AWS spend across all accounts, by service, for {latest_label}: {_fmt_money(currency, total_latest)}
{f"Previous month ({prev_label}) total: " + _fmt_money(currency, total_prev) if prev_month else ""}
{f"Change vs previous month: " + _fmt_pct(pct_change) if prev_month else ""}
Accounts included: {account_count} ({error_count} failed to fetch)

Top spending AWS accounts ({latest_label}):
{top_account_lines or '(no data)'}

Top AWS services by spend ({latest_label}, across all accounts):
{top_service_lines or '(no data)'}

Tasks:
1. Write a 2-3 sentence executive summary of current AWS spend-by-service and the overall trend (up/down/stable) versus the prior month if available.
2. Identify up to 3 key cost drivers (which account/service is driving spend, and why if inferable from the service name — e.g. compute, storage, data transfer, marketplace add-ons).
3. Give up to 4 concrete cost-control recommendations a Cloud Manager could action this month (e.g. rightsizing, reserved instances/savings plans, unused resource cleanup, marketplace subscription review, data-transfer optimization). Be specific to AWS.
4. Give a one-line "bottom line" recommendation for management (approve as-is / needs review / needs immediate cost action).

IMPORTANT: Reply with valid JSON ONLY, no extra text:
{{
  "executive_summary": "...",
  "trend": "increasing" | "decreasing" | "stable",
  "key_cost_drivers": ["driver 1", "driver 2", "driver 3"],
  "recommendations": ["recommendation 1", "recommendation 2", "recommendation 3", "recommendation 4"],
  "bottom_line": "..."
}}"""

    client = _get_client()
    used_model = MODEL_ANALYSIS
    data = None
    last_error: Optional[Exception] = None

    for model_id in (MODEL_ANALYSIS, MODEL_ANALYSIS_FALLBACK):
        try:
            raw_text = _invoke(client, model_id, prompt, max_tokens=900)
        except Exception as e:
            logger.warning(f"Bedrock invocation failed for billing-by-service analysis with {model_id}: {e}")
            last_error = e
            continue
        try:
            data = _extract_json(raw_text)
        except Exception as e:
            logger.warning(f"Could not parse billing-by-service response from {model_id}: {e}")
            last_error = e
            continue
        if not isinstance(data.get("executive_summary"), str) or not data.get("executive_summary", "").strip():
            logger.warning(f"Billing-by-service response from {model_id} missing/empty executive_summary")
            data = None
            continue
        used_model = model_id
        break

    if data is None:
        logger.error(f"All models failed to produce a usable billing-by-service analysis: {last_error}")
        data = {
            "executive_summary": (
                f"Automated AI summary is temporarily unavailable. {latest_label} spend across all services is "
                f"{_fmt_money(currency, total_latest)}"
                + (f" ({_fmt_pct(pct_change)} vs {prev_label})." if prev_month else ".")
            ),
            "trend": "stable",
            "key_cost_drivers": [],
            "recommendations": [],
            "bottom_line": "AI summary generation failed — review the billing-by-service table directly.",
        }
        used_model = "fallback-static"

    trend = data.get("trend") if data.get("trend") in ("increasing", "decreasing", "stable") else "stable"
    key_cost_drivers = [_clip(x, 300) for x in (data.get("key_cost_drivers") or [])][:3]
    recommendations = [_clip(x, 300) for x in (data.get("recommendations") or [])][:4]
    return {
        "executive_summary": _clip(data.get("executive_summary", ""), 1500),
        "trend": trend,
        "key_cost_drivers": key_cost_drivers,
        "recommendations": recommendations,
        "bottom_line": _clip(data.get("bottom_line", ""), 300),
        "as_of": as_of,
        "account_count": account_count,
        "error_count": error_count,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_used": used_model,
        "bedrock_region": BEDROCK_REGION,
    }
