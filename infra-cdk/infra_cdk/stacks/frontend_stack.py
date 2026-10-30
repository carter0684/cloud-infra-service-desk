"""
FrontendStack — S3 static site + CloudFront distribution for the
Cloud Infra Job Portal React SPA.

Design:
- Private S3 bucket, CloudFront serves static SPA assets.
- API calls go directly to the ALB (VITE_API_BASE_URL in .env.production).
  ALB handles Azure AD OIDC auth; CORS on the backend allows *.cloudfront.net.
- /api/* CloudFront behavior proxies to ALB as a fallback alternative,
  but login flow (OIDC) must go directly to ALB so the session cookie is
  scoped to the ALB domain (the Azure AD redirect_uri is always the ALB URL).
"""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    aws_s3 as s3,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3_deployment as s3deploy,
    aws_wafv2 as wafv2,
    CfnOutput,
)
from constructs import Construct
import os


class FrontendStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cloudfront_web_acl: wafv2.CfnWebACL,
        alb_dns_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Private bucket — CloudFront (via OAC) is the only reader.
        self.bucket = s3.Bucket(
            self,
            "FrontendBucket",
            bucket_name=None,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            versioned=True,
        )

        s3_origin = origins.S3BucketOrigin.with_origin_access_control(self.bucket)

        self.distribution = cloudfront.Distribution(
            self,
            "FrontendDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=s3_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
            ],
            web_acl_id=cloudfront_web_acl.attr_arn,
        )

        # Deploy the pre-built static assets (see module docstring — the
        # actual `npm run build` happens outside CDK, in the deploy
        # tooling script, before this stack synthesizes/deploys).
        #
        # FRONTEND_DIST_DIR env var override: on a machine where this repo
        # lives inside a OneDrive-synced folder, OneDrive's background
        # sync can dehydrate freshly-built files into cloud placeholders
        # (FILE_ATTRIBUTE_REPARSE_POINT) within seconds of `npm run build`
        # finishing — which breaks CDK's asset-fingerprinting step
        # (`fs.readlinkSync` throws EINVAL on those placeholders, since
        # they aren't real symlinks). Deploy tooling can work around this
        # by copying frontend/dist to a non-OneDrive-synced temp
        # directory and pointing this env var at that copy instead; this
        # defaults to the normal in-repo path when unset, so it's a no-op
        # everywhere else.
        dist_dir = os.environ.get("FRONTEND_DIST_DIR") or os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "frontend", "dist"
        )
        if os.path.isdir(dist_dir):
            s3deploy.BucketDeployment(
                self,
                "DeployFrontendAssets",
                sources=[s3deploy.Source.asset(dist_dir)],
                destination_bucket=self.bucket,
                # Scoping prune to this key prefix means the web-app
                # deploy only ever adds/removes files under the site
                # root, and never touches (or deletes) anything under
                # desktop-updates/ — that prefix is managed exclusively
                # by DeployDesktopUpdate below, on its own release
                # cadence, so publishing a new web build never
                # accidentally wipes the desktop updater manifest/
                # installer that's currently live for existing Windows
                # app installs.
                exclude=["desktop-updates/*"],
                distribution=self.distribution,
                distribution_paths=["/*"],
                retain_on_delete=False,
            )

        # Desktop app auto-updater manifest + installer + signature.
        #
        # Tauri's updater plugin (see src-tauri/tauri.conf.json's
        # `plugins.updater.endpoints`) polls
        # https://<CloudFrontDomain>/desktop-updates/latest.json when a
        # user clicks "Check for Updates" in the sidebar. If that file's
        # "version" is newer than the running app, the plugin downloads
        # the installer at the URL inside it, verifies it against the
        # signature, and installs + relaunches automatically — no admin
        # needs to email/resend an installer to anyone ever again.
        #
        # To ship a new desktop release:
        #   1. Bump "version" in src-tauri/tauri.conf.json.
        #   2. `npm run tauri build` (from job-portal/frontend) — this
        #      produces target/release/bundle/nsis/*-setup.exe and a
        #      matching *.exe.sig, signed with the private key generated
        #      via `tauri signer generate` (kept OUTSIDE the repo, in
        #      TAURI_SIGNING_PRIVATE_KEY / _PATH env vars — never
        #      committed; see .gitignore's *.key rule).
        #   3. Drop the new .exe + .sig + a hand-written/generated
        #      latest.json (matching the Tauri static-JSON schema: version,
        #      pub_date, platforms."windows-x86_64".{url,signature}) into
        #      DESKTOP_UPDATES_DIR below.
        #   4. Re-run this CDK deploy (or just this stack) — it uploads
        #      those 3 files to desktop-updates/ and invalidates just that
        #      path, leaving the rest of the CloudFront cache untouched.
        #
        # DESKTOP_UPDATES_DIR defaults to a repo-local staging folder that
        # is empty/absent by default (nothing to deploy until a release
        # is actually staged there), so this is a no-op on every machine/
        # deploy that hasn't prepared a new desktop release.
        desktop_updates_dir = os.environ.get("DESKTOP_UPDATES_DIR") or os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "frontend", "desktop-updates-staging"
        )
        if os.path.isdir(desktop_updates_dir) and os.listdir(desktop_updates_dir):
            s3deploy.BucketDeployment(
                self,
                "DeployDesktopUpdate",
                sources=[s3deploy.Source.asset(desktop_updates_dir)],
                destination_bucket=self.bucket,
                destination_key_prefix="desktop-updates",
                # prune=True (default) is safe here because destination_key_prefix
                # scopes it to only this prefix — old installers/signatures
                # for previous versions get cleaned up automatically, but
                # nothing outside desktop-updates/ is ever touched.
                distribution=self.distribution,
                distribution_paths=["/desktop-updates/*"],
                retain_on_delete=False,
            )

        CfnOutput(self, "CloudFrontDomainName", value=self.distribution.distribution_domain_name)
        CfnOutput(self, "FrontendBucketName", value=self.bucket.bucket_name)
