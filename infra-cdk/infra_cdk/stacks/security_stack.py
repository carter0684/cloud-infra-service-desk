"""
SecurityStack — WAF WebACLs for the ALB (regional) and CloudFront (global scope).

Note: this stack must be deployed in us-east-1 for the CLOUDFRONT-scope WebACL
to be valid (a hard AWS requirement, independent of the app's primary region).
Secrets (Azure AD client id/secret/tenant, Freshservice API key, webhook HMAC
secret) were created out-of-band via the AWS CLI and are referenced by name/ARN
elsewhere — this stack does not create or manage their values.
"""

from aws_cdk import (
    Stack,
    aws_wafv2 as wafv2,
)
from constructs import Construct


def _managed_rule_group(
    name: str,
    priority: int,
    vendor: str = "AWS",
    rule_action_overrides: list[wafv2.CfnWebACL.RuleActionOverrideProperty] | None = None,
) -> wafv2.CfnWebACL.RuleProperty:
    return wafv2.CfnWebACL.RuleProperty(
        name=name,
        priority=priority,
        override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
        statement=wafv2.CfnWebACL.StatementProperty(
            managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                vendor_name=vendor,
                name=name,
                rule_action_overrides=rule_action_overrides,
            )
        ),
        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            sampled_requests_enabled=True,
            cloud_watch_metrics_enabled=True,
            metric_name=name,
        ),
    )


class SecurityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # AWSManagedRulesCommonRuleSet's SizeRestrictions_BODY sub-rule
        # blocks any request body over 8KB by default. That breaks the
        # profile-photo upload endpoints (POST /api/profile/photo and
        # POST /api/admin/users/{email}/photo), whose multipart bodies
        # can legitimately be several hundred KB (an original phone/
        # camera photo before this app's own server-side resize/compress
        # step in app/profile_photo.py, which already caps + validates
        # uploads at 8MB and rejects non-images). Override that one
        # sub-rule to COUNT (log-only) instead of BLOCK so it stops
        # rejecting legitimate uploads, while every other sub-rule in
        # the Common Rule Set (XSS, LFI/RFI, etc.) still blocks as-is.
        #
        # CrossSiteScripting_BODY is a second sub-rule that also needs a
        # COUNT override. Confirmed via GetSampledRequests + a direct
        # test upload (2026-08-17) that this rule false-positives on
        # legitimate Outlook .msg attachments — the OLE2/compound-file
        # binary format (and its embedded RTF/HTML body) can contain
        # byte sequences that pattern-match as script-injection payloads
        # even though the content is never rendered back as executable
        # HTML: POST /api/extract/file, /api/cases/attachments, and
        # /api/kb/upload all parse the upload into plain structured
        # text server-side (app/file_parser.py) before it is ever shown
        # in the UI, and React escapes all rendered text by default —
        # so this sub-rule was blocking safe binary uploads without
        # protecting anything that wasn't already safe.
        common_rule_set_overrides = [
            wafv2.CfnWebACL.RuleActionOverrideProperty(
                name="SizeRestrictions_BODY",
                action_to_use=wafv2.CfnWebACL.RuleActionProperty(count={}),
            ),
            wafv2.CfnWebACL.RuleActionOverrideProperty(
                name="CrossSiteScripting_BODY",
                action_to_use=wafv2.CfnWebACL.RuleActionProperty(count={}),
            ),
        ]

        common_rules = [
            _managed_rule_group(
                "AWSManagedRulesCommonRuleSet",
                priority=1,
                rule_action_overrides=common_rule_set_overrides,
            ),
            _managed_rule_group("AWSManagedRulesKnownBadInputsRuleSet", priority=2),
        ]

        # Regional WebACL — associated with the ALB in ComputeStack
        self.alb_web_acl = wafv2.CfnWebACL(
            self,
            "AlbWebAcl",
            name="job-portal-alb-waf",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            rules=common_rules,
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name="JobPortalAlbWaf",
            ),
        )

        # CLOUDFRONT-scope WebACL — must be created in us-east-1;
        # associated with the CloudFront distribution in FrontendStack.
        self.cloudfront_web_acl = wafv2.CfnWebACL(
            self,
            "CloudFrontWebAcl",
            name="job-portal-cloudfront-waf",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            rules=common_rules,
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name="JobPortalCloudFrontWaf",
            ),
        )
