"""
Helpdesk Team catalogue -> S3-ScreenSaver tool: lets an end user upload a
file from the browser to a dedicated S3 bucket in the Core account
account (100000000001, ap-southeast-1 / Singapore), then hands back the
resulting S3 path so the user knows exactly where their file landed.

Why a NEW dedicated bucket/credential instead of reusing an existing
path into 100000000001: the two existing credential paths into that
account are scoped for other purposes only —
  - cost_explorer.py's `_core_session()` (raw "portal-core-admin"
    keys) is used for Cost Explorer / Security Hub / WAF / IAM
    READ-ONLY queries across the whole org.
  - escalation.py's AMS support credentials are scoped to the AWS
    Support API only (raising/reading support cases).
Neither is intended or scoped for S3 writes, so this module uses its OWN
narrowly-scoped IAM user (job-portal-screensaver-s3, provisioned directly
in 100000000001 with `s3:PutObject`/`s3:GetObject`/`s3:ListBucket`
permissions on ONLY the `screensaver-100000000001` bucket — see
infra-cdk/README or ops notes for the exact IAM policy), whose keys are
stored in Secrets Manager the same way every other cross-account
credential in this codebase is (see cost_explorer.py's
`_get_core_credentials()` / escalation.py's `_get_ams_credentials()`
for the identical pattern this mirrors).

Upload flow is BACKEND-PROXY (browser -> this backend -> S3), the same
pattern note_attachments.py uses — NOT the original direct-browser-to-S3
presigned-PUT design. The original design minted a presigned PUT URL and
had the browser PUT the file bytes straight to S3, bypassing this
backend/ALB entirely. That was changed after a real user's upload
consistently failed with a browser-side "network error" on exactly that
direct-to-S3 step — their corporate network/proxy/firewall was blocking
outbound connections to `*.s3.ap-southeast-1.amazonaws.com`, which this
team has no ability to fix (can't force arbitrary IT departments to
allowlist an AWS domain). Proxying the bytes through this backend means
the browser only ever talks to the portal's own domain (already proven
reachable, since the user is logged in), trading away the original
"keeps large files off the backend's own network path/memory" rationale
for actually-working uploads — worth it for this low-volume internal
tool. upload_screensaver_file() takes the already-read file bytes
(read from the FastAPI UploadFile by the route handler, same as
note_attachments.upload_attachment()), does one `put_object()` call, and
then immediately republishes the object as the site-wide latest/current
file (see _publish_as_latest()) before returning — no separate
"confirm" step is needed since put_object() either succeeds or raises.

STABLE PUBLIC URL (design "B" — fixed URL always serves the latest
upload): a CloudFront distribution (id ECTB50DKBFCG2, custom domain
`screensaver.example-site-a.com`, ACM cert on the existing
`*.example-site-a.com` wildcard, origin = this same S3 bucket via
Origin Access Control) fronts the bucket over HTTPS. AD scripts / store
location configs are meant to hardcode ONE fixed path —
`https://screensaver.example-site-a.com/screensaver/latest/current`
— that never changes, rather than each upload's own dated/uuid'd key.
To make that possible, _publish_as_latest() copies the just-uploaded
object on top of that fixed key (S3 CopyObject, preserving Content-Type)
and then fires a CloudFront invalidation for that one path so the CDN
edge caches don't keep serving the previous file for its TTL. The
original per-upload dated key is also kept (never deleted) purely as an
audit trail of every historical upload — only the `latest/current` copy
is what AD scripts should ever reference.

FILE TYPE: no hard restriction is enforced here (any content-type/
extension is accepted and uploaded as-is) — but JPG and PNG are the
only officially SUPPORTED types for the screensaver use case, since
that's what AD/location screensaver mechanisms are expected to render.
The frontend surfaces this as guidance text only, not an <input accept>
block, so this stays a soft recommendation rather than a hard gate.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.config import Config

from . import org_config

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")  # region for Secrets Manager (portal's own account)

# The screensaver bucket itself lives in a DIFFERENT account/region than
# everything else this backend talks to — 100000000001 (Core account
# Server), ap-southeast-1 (Singapore), per the user's explicit request.
# This is intentionally its own constant, never reusing AWS_REGION.
SCREENSAVER_ACCOUNT_ID = os.environ.get("SCREENSAVER_ACCOUNT_ID", "") or org_config.CORE_ACCOUNT_ID
SCREENSAVER_BUCKET_REGION = os.environ.get("SCREENSAVER_BUCKET_REGION", "ap-southeast-1")
# Empty until configured (also requires ENABLED_FEATURES=screensaver).
SCREENSAVER_BUCKET_NAME = os.environ.get("SCREENSAVER_BUCKET_NAME", "")

SCREENSAVER_CREDENTIALS_SECRET_NAME = os.environ.get(
    "SCREENSAVER_CREDENTIALS_SECRET_NAME", "job-portal/s3-screensaver-credentials"
)

# Per-file size cap. Now that the upload transits this backend's own
# memory (browser -> backend -> S3, see module docstring for why this
# changed from presigned-PUT), 500MB is kept as the ceiling for now —
# generous for screensaver video/image packages — but this DOES mean a
# 500MB upload is briefly held in the ECS task's memory as `bytes`
# before put_object() ships it to S3. Revisit downward if task memory
# pressure becomes an issue in practice; the real-world files uploaded
# through this tool so far have all been small JPG/PNG images anyway.
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB

# CloudFront distribution fronting this same bucket over HTTPS via OAC,
# giving every upload a stable public URL. Distribution lives in the
# SAME account as the bucket (100000000001) — created directly via AWS
# CLI (not this repo's CDK, which is scoped to the portal's own account
# 100000000002 only and cannot deploy cross-account resources).
SCREENSAVER_CLOUDFRONT_DOMAIN = os.environ.get("SCREENSAVER_CLOUDFRONT_DOMAIN", "")
SCREENSAVER_CLOUDFRONT_DISTRIBUTION_ID = os.environ.get("SCREENSAVER_CLOUDFRONT_DISTRIBUTION_ID", "")

# Fixed, never-changing key that AD scripts / store location configs
# should hardcode. Every successful upload gets copied on top of this
# key (see _publish_as_latest()) so the SAME public URL always resolves
# to whichever file was most recently uploaded — no per-store config
# changes needed when the screensaver image/video is swapped out.
SCREENSAVER_LATEST_KEY = "screensaver/latest/current"


class ScreenSaverError(ValueError):
    """Raised for any invalid request — message is safe to show the user."""


def _get_secret(secret_name: str) -> str:
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]


def _get_screensaver_credentials() -> dict:
    """Static IAM keys for the narrowly-scoped `job-portal-screensaver-s3`
    user in account 100000000001. Same local-dev-env-var-override
    convenience as cost_explorer._get_core_credentials() /
    escalation._get_ams_credentials(): if
    SCREENSAVER_ACCESS_KEY_ID/SCREENSAVER_SECRET_ACCESS_KEY are set,
    skip the Secrets Manager round trip (handy on a developer laptop)."""
    env_key = os.environ.get("SCREENSAVER_ACCESS_KEY_ID")
    env_secret = os.environ.get("SCREENSAVER_SECRET_ACCESS_KEY")
    if env_key and env_secret:
        return {"aws_access_key_id": env_key, "aws_secret_access_key": env_secret}

    try:
        secret_str = _get_secret(SCREENSAVER_CREDENTIALS_SECRET_NAME)
        creds = json.loads(secret_str)
    except Exception as e:
        raise ScreenSaverError(f"Could not load ScreenSaver upload credentials: {e}") from e
    if "aws_access_key_id" not in creds or "aws_secret_access_key" not in creds:
        raise ScreenSaverError("ScreenSaver credentials secret is missing required keys")
    return creds


_s3_client = None
_cloudfront_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        creds = _get_screensaver_credentials()
        _s3_client = boto3.client(
            "s3",
            region_name=SCREENSAVER_BUCKET_REGION,
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            config=Config(signature_version="s3v4"),
        )
    return _s3_client


def _cloudfront():
    """CloudFront's control-plane API is global/region-less (unlike the
    S3 bucket, which is deliberately pinned to ap-southeast-1) — pass no
    region_name here rather than reusing SCREENSAVER_BUCKET_REGION, which
    would be misleading since CloudFront calls always route to us-east-1
    regardless of what's specified."""
    global _cloudfront_client
    if _cloudfront_client is None:
        creds = _get_screensaver_credentials()
        _cloudfront_client = boto3.client(
            "cloudfront",
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
        )
    return _cloudfront_client


def _safe_filename(filename: str) -> str:
    """Strip path separators and any character that isn't safe in an S3
    key, so a malicious/odd filename can't traverse the key namespace or
    break URL encoding — same sanitization note_attachments.py uses."""
    base = os.path.basename(filename or "file")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return cleaned[:150] or "file"


def upload_screensaver_file(
    *, filename: str, content_type: Optional[str], content: bytes, uploaded_by_email: str
) -> dict:
    """Backend-proxy upload: the route handler has already read the raw
    bytes off the incoming multipart UploadFile (same as
    note_attachments.upload_attachment()) — this function validates
    them, writes them straight to S3 via put_object(), republishes the
    object as the site-wide `latest/current` file (see
    _publish_as_latest()), and returns the final metadata for the user.

    No separate "confirm" step is needed here (unlike the old presigned-
    PUT flow's confirm_upload()) — put_object() either succeeds or
    raises, so there's no gap where the backend wouldn't know the upload
    failed.

    The key is namespaced by upload date + a random id + the sanitized
    original filename (`screensaver/YYYY/MM/DD/<uuid>-<filename>`) so
    concurrent uploads never collide and files are still browsable/
    organized by date directly in the S3 console if needed."""
    if not filename or not filename.strip():
        raise ScreenSaverError("A filename is required")
    if not content:
        raise ScreenSaverError("Uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ScreenSaverError(
            f"'{filename}' is too large — maximum {MAX_UPLOAD_BYTES // (1024 * 1024)} MB per file"
        )

    safe_name = _safe_filename(filename)
    today = datetime.now(timezone.utc)
    upload_id = str(uuid.uuid4())
    s3_key = f"screensaver/{today:%Y/%m/%d}/{upload_id}-{safe_name}"
    resolved_content_type = content_type or "application/octet-stream"

    try:
        _s3().put_object(
            Bucket=SCREENSAVER_BUCKET_NAME,
            Key=s3_key,
            Body=content,
            ContentType=resolved_content_type,
        )
    except Exception as e:
        logger.exception("screensaver: failed to upload %s to S3", s3_key)
        raise ScreenSaverError(f"Could not upload file: {e}") from e

    # Best-effort: a failure here must not fail the whole upload — the
    # file is already safely in S3 under its own dated key regardless
    # (see _publish_as_latest()'s own docstring for the same guarantee
    # it previously gave confirm_upload()).
    _publish_as_latest(s3_key, resolved_content_type)

    return {
        "s3_key": s3_key,
        "s3_path": f"s3://{SCREENSAVER_BUCKET_NAME}/{s3_key}",
        "bucket": SCREENSAVER_BUCKET_NAME,
        "region": SCREENSAVER_BUCKET_REGION,
        "content_type": resolved_content_type,
        "size_bytes": len(content),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "filename": filename,
        "uploaded_by_email": uploaded_by_email,
        # Stable public URL AD scripts / location configs should
        # hardcode — always resolves to whichever file was most recently
        # uploaded, since _publish_as_latest() just refreshed it above.
        "public_url": f"https://{SCREENSAVER_CLOUDFRONT_DOMAIN}/{SCREENSAVER_LATEST_KEY}",
    }


def _publish_as_latest(s3_key: str, content_type: Optional[str]) -> None:
    """Design "B": copy the just-uploaded object on top of the fixed
    `screensaver/latest/current` key, then invalidate that one CloudFront
    path so edge caches don't keep serving whatever was there before.
    Best-effort: a failure here must NOT fail the whole
    upload_screensaver_file() call — the user's file is already safely
    in S3 under its own dated key regardless, since put_object() already
    succeeded before this is called. Losing the "publish as latest" step
    just means AD scripts keep seeing the previous screensaver until the
    next successful upload, not that any data was lost."""
    try:
        _s3().copy_object(
            Bucket=SCREENSAVER_BUCKET_NAME,
            Key=SCREENSAVER_LATEST_KEY,
            CopySource={"Bucket": SCREENSAVER_BUCKET_NAME, "Key": s3_key},
            ContentType=content_type or "application/octet-stream",
            # Explicit no-cache directive on the object itself — belt-
            # and-suspenders alongside the CloudFront invalidation just
            # below. If the invalidation call fails (e.g. the cross-
            # account `job-portal-screensaver-s3` IAM user's policy
            # doesn't include cloudfront:CreateInvalidation — that user
            # is documented at the top of this module as scoped to only
            # PutObject/GetObject/ListBucket, so this is a real
            # possibility, not hypothetical), a CloudFront distribution
            # whose cache behavior is configured to respect origin
            # headers will still stop serving the stale cached response
            # immediately instead of for its full default TTL. This has
            # NO effect if the distribution's cache policy is one of the
            # AWS-managed "ignore origin headers" policies (e.g.
            # CachingOptimized) — that setting lives in the CloudFront
            # distribution's own config, in the other AWS account, and
            # can't be changed from this repo.
            CacheControl="no-cache, max-age=0, must-revalidate",
            MetadataDirective="REPLACE",
        )
    except Exception:
        logger.exception("screensaver: failed to publish %s as latest/current", s3_key)
        return

    try:
        _cloudfront().create_invalidation(
            DistributionId=SCREENSAVER_CLOUDFRONT_DISTRIBUTION_ID,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": [f"/{SCREENSAVER_LATEST_KEY}"]},
                "CallerReference": str(uuid.uuid4()),
            },
        )
    except Exception:
        # Non-fatal: the object is already updated, so even a stale CDN
        # edge cache resolves within its normal TTL without any
        # invalidation at all — this just makes the update visible sooner.
        logger.exception("screensaver: failed to invalidate CloudFront cache for %s", SCREENSAVER_LATEST_KEY)


def generate_download_url(s3_key: str) -> str:
    """Short-lived presigned GET URL, in case the user (or a teammate)
    wants to re-download/view a previously uploaded file from its path.
    Not currently wired to a route, but kept alongside upload for
    symmetry with note_attachments.py and to support a future
    "recent uploads" list without further backend changes."""
    if not s3_key or not s3_key.startswith("screensaver/"):
        raise ScreenSaverError("Invalid file reference")
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": SCREENSAVER_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=300,
    )


# ── Why "the link didn't update" happens, and why listing + reminding to
# delete old uploads is the fix ─────────────────────────────────────────────
#
# _publish_as_latest() above already copies every new upload on top of
# the fixed `screensaver/latest/current` key and fires a CloudFront
# invalidation for that one path — so the PERMANENT URL
# (`.../screensaver/latest/current`) itself should always reflect the
# newest upload within a few seconds. A user who sees the old image keep
# showing there almost always has one of these two things going on
# instead, neither of which this module can fix on its own:
#   1. The AD script / kiosk device is caching the downloaded image file
#      on local disk rather than re-fetching the URL every time, so it
#      keeps rendering its own local copy until the device restarts or
#      its own cache expires — this is client-side behavior, outside
#      this backend's control.
#   2. A CDN edge location the invalidation didn't (yet) reach, or whose
#      own cache policy ignores origin Cache-Control headers, keeps
#      serving a stale cached response for the remainder of its TTL —
#      partially mitigated above by the extra CacheControl header on
#      the `latest/current` object itself.
#
# What the user actually reported ("removing the OLD file from the S3
# bucket made the link switch to the newest one") describes something
# else entirely: multiple dated upload keys sitting in the bucket at
# once, with whatever CDN/device cache was already serving one of the
# non-`latest/current` objects directly (or a manual workflow that
# doesn't go through this tool at all) rather than the fixed
# `latest/current` key this tool publishes to. Since every upload here
# is intentionally KEPT under its own dated key (screensaver/YYYY/MM/DD/
# <uuid>-<filename>) as an audit trail (see upload_screensaver_file()'s
# docstring) and never auto-deleted, the bucket accumulates one object
# per upload forever. list_uploaded_files() / delete_uploaded_file()
# below let the user see every historical upload and manually clean up
# old ones, so nothing is left behind for a misconfigured/cached
# consumer to accidentally keep pointing at.

def list_uploaded_files(max_keys: int = 200) -> list[dict]:
    """List every historical ScreenSaver upload (everything under the
    `screensaver/` prefix EXCEPT the fixed `latest/current` publish
    target itself), newest first, so the user can see what's
    accumulated in the bucket and manually delete old ones they no
    longer need. Also flags which entry's content currently matches
    `latest/current` (via ETag — S3's CopyObject recomputes the ETag
    from the copied bytes, so an unchanged single-part object's ETag is
    stable across the copy) as `is_current`, purely as a hint — it is
    NOT re-derived from any stored pointer/metadata, since none is kept.
    Best-effort: any failure to look up `latest/current`'s ETag
    downgrades to `is_current: False` for every entry rather than
    failing the whole listing."""
    current_etag = None
    try:
        head = _s3().head_object(Bucket=SCREENSAVER_BUCKET_NAME, Key=SCREENSAVER_LATEST_KEY)
        current_etag = head.get("ETag")
    except Exception:
        logger.info("screensaver: could not head latest/current object (may not exist yet)")

    files: list[dict] = []
    continuation_token = None
    while True:
        kwargs = {"Bucket": SCREENSAVER_BUCKET_NAME, "Prefix": "screensaver/", "MaxKeys": min(max_keys, 1000)}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        try:
            resp = _s3().list_objects_v2(**kwargs)
        except Exception as e:
            logger.exception("screensaver: failed to list objects")
            raise ScreenSaverError(f"Could not list uploaded files: {e}") from e

        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key == SCREENSAVER_LATEST_KEY:
                continue  # not a historical upload — the publish target itself
            files.append({
                "s3_key": key,
                "filename": key.rsplit("/", 1)[-1],
                "size_bytes": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
                "is_current": bool(current_etag) and obj.get("ETag") == current_etag,
            })
            if len(files) >= max_keys:
                break

        if len(files) >= max_keys or not resp.get("IsTruncated"):
            break
        continuation_token = resp.get("NextContinuationToken")

    files.sort(key=lambda f: f["last_modified"], reverse=True)
    return files


def delete_uploaded_file(s3_key: str) -> None:
    """Delete one historical dated-key upload. Refuses to delete the
    fixed `latest/current` publish key itself (that key must only ever
    be overwritten by the next upload's _publish_as_latest() call, never
    deleted directly — deleting it would make the permanent public URL
    404 for everyone until the next upload) and refuses any key outside
    the `screensaver/` prefix, same guard generate_download_url() uses.

    NOTE: this call needs `s3:DeleteObject` on the ScreenSaver bucket.
    The cross-account `job-portal-screensaver-s3` IAM user this module
    authenticates as is documented (see module docstring) as scoped to
    ONLY PutObject/GetObject/ListBucket — if DeleteObject was never
    added to that user's policy in account 100000000001, this call fails
    with an AccessDenied error surfaced as a ScreenSaverError below,
    and that policy must be updated directly in that other account
    (outside this repo's CDK, which only manages account 100000000002)."""
    if not s3_key or not s3_key.startswith("screensaver/"):
        raise ScreenSaverError("Invalid file reference")
    if s3_key == SCREENSAVER_LATEST_KEY:
        raise ScreenSaverError(
            "Cannot delete the live 'latest/current' file directly — upload a replacement instead."
        )
    try:
        _s3().delete_object(Bucket=SCREENSAVER_BUCKET_NAME, Key=s3_key)
    except Exception as e:
        logger.exception("screensaver: failed to delete %s", s3_key)
        raise ScreenSaverError(f"Could not delete '{s3_key}': {e}") from e
