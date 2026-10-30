"""
ComputeStack — ECR, ECS Fargate service, and ALB (with Azure AD OIDC auth
listener action) for the Cloud Infra Job Portal.

Account: 100000000002
Region:  us-east-1

Notes on the TLS certificate:
    UPDATED (2026-08-03): a custom domain now exists — the company's DNS has
    a CNAME `portal.example.com` -> `<AlbDnsName>`, and a real,
    publicly-trusted, DNS-validated ACM certificate for that domain was
    issued in us-east-1 (matching the ALB's region — ACM certs cannot cross
    regions). `IMPORTED_CERT_ARN` now points at that cert, and
    `CUSTOM_DOMAIN` is used for the Azure AD `redirect_uri` and the
    post-login `FRONTEND_URL` bounce, so browsers/WebViews (including the
    Android/iOS/Windows app shells) no longer hit the old self-signed-cert
    "untrusted certificate" warning. The previous self-signed cert
    (`*.us-east-1.elb.amazonaws.com`) is no longer referenced by this stack.
"""

from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
    aws_wafv2 as wafv2,
    aws_certificatemanager as acm,
    CfnOutput,
)
from constructs import Construct

from ..config import CONFIG

# NOTE: aws_elasticloadbalancingv2_actions.AuthenticateOidcAction does not
# exist in this CDK version — the OIDC action is a static factory method on
# elbv2.ListenerAction itself (elbv2.ListenerAction.authenticate_oidc(...)).

# ── Deployment configuration (see infra_cdk/config.py and config.example.json) ──
# Real, publicly-trusted ACM certificate for the portal's custom domain
# (DNS-validated, in the SAME region as the ALB — ACM certs cannot cross regions).
IMPORTED_CERT_ARN = CONFIG["acm_certificate_arn"]

# Custom domain that DNS points at the ALB via CNAME. Used for the Azure AD
# OIDC redirect_uri and for the FRONTEND_URL post-login bounce so
# browsers/WebViews see a real, trusted TLS certificate.
CUSTOM_DOMAIN = CONFIG["custom_domain"]

AWS_ACCOUNT_ID = CONFIG["account_id"]
AWS_REGION = CONFIG["region"]

# Bedrock may live in a separate account; the backend assumes this role there
# (BEDROCK_ROLE_ARN env var, see backend/app/ai_engine.py). When
# bedrock_account_id is empty the portal's own account is used.
BEDROCK_ACCOUNT_ID = CONFIG["bedrock_account_id"] or AWS_ACCOUNT_ID
BEDROCK_ROLE_NAME = CONFIG["bedrock_role_name"]
BEDROCK_ROLE_ARN = f"arn:aws:iam::{BEDROCK_ACCOUNT_ID}:role/{BEDROCK_ROLE_NAME}"

# Central bucket of the Website Monitoring probes (website-monitor CloudFormation
# project, central stack: webmon-central-<account>-<region>).
WEBSITE_MONITOR_REGION = CONFIG["website_monitor_region"]
WEBSITE_MONITOR_BUCKET = f"webmon-central-{AWS_ACCOUNT_ID}-{WEBSITE_MONITOR_REGION}"

# Azure AD (Entra ID) tenant/client IDs — non-secret public identifiers (they
# appear in browser-visible OAuth redirect URLs), unlike the client *secret*
# which stays in Secrets Manager and is referenced by ARN below.
AZURE_TENANT_ID = CONFIG["azure_tenant_id"]
AZURE_CLIENT_ID = CONFIG["azure_client_id"]

class ComputeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        alb_security_group: ec2.ISecurityGroup,
        ecs_task_security_group: ec2.ISecurityGroup,
        alb_web_acl: wafv2.CfnWebACL,
        jobs_table_arn: str,
        kb_documents_table_arn: str,
        mappings_table_arn: str,
        sync_log_table_arn: str,
        webhook_idempotency_table_arn: str,
        notification_dedup_table_arn: str,
        users_table_arn: str,
        health_event_checks_table_arn: str,
        feedback_reports_table_arn: str,
        cost_adjustments_table_arn: str,
        cmdb_snapshots_table_arn: str,
        cmdb_history_table_arn: str,
        settings_table_arn: str,
        standalone_cases_table_arn: str,
        whitelist_audit_log_table_arn: str,
        dns_audit_log_table_arn: str,
        s2s_vpn_audit_log_table_arn: str,
        # Cloud account registry + audit (Admin > Cloud Accounts).
        cloud_accounts_table_arn: str,
        cloud_account_audit_table_arn: str,
        cmk_key_arn: str,
        note_attachments_bucket_arn: str,
        note_attachments_bucket_name: str,
        # Chat/Call feature (job-portal-chat-voice spec) — new tables +
        # bucket added in DataStack. Threaded through the same way every
        # other table/bucket above is: as a constructor kwarg from
        # app.py, never re-derived here.
        chat_conversations_table_arn: str,
        chat_participants_table_arn: str,
        chat_messages_table_arn: str,
        call_sessions_table_arn: str,
        ws_connections_table_arn: str,
        chat_attachments_bucket_arn: str,
        chat_attachments_bucket_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── ECR repository ───────────────────────────────────────────────
        # Created out-of-band via `aws ecr create-repository` before this
        # stack's first deploy, specifically to break the chicken-and-egg
        # problem where the ECS service needs a real image to exist in ECR
        # before it can reach a healthy steady state, but CDK would normally
        # create the repo in the same deploy as the service. Imported here
        # (not `ecr.Repository(...)`) so CDK manages the lifecycle rule going
        # forward without trying to re-create the repo.
        self.repository = ecr.Repository.from_repository_name(
            self, "JobPortalRepo", "job-portal-backend"
        )

        # ── CloudWatch log group (explicit retention, Req 10.3) ──────────
        self.log_group = logs.LogGroup(
            self,
            "JobPortalLogGroup",
            log_group_name="/ecs/job-portal-backend",
            retention=logs.RetentionDays.SIX_MONTHS,  # 180 days, within 90-365 window
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ── ECS cluster ───────────────────────────────────────────────────
        self.cluster = ecs.Cluster(
            self,
            "JobPortalCluster",
            vpc=vpc,
            cluster_name="job-portal-cluster",
            container_insights=True,
        )

        # ── Task execution role (pulls image, writes first log lines) ────
        execution_role = iam.Role(
            self,
            "JobPortalTaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )

        # ── Task role — least privilege, scoped exactly per design.md §2 ─
        task_role = iam.Role(
            self,
            "JobPortalTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )

        portal_table_arns = [
            jobs_table_arn,
            f"{jobs_table_arn}/index/*",
            kb_documents_table_arn,
            mappings_table_arn,
            sync_log_table_arn,
            webhook_idempotency_table_arn,
            notification_dedup_table_arn,
            users_table_arn,
            health_event_checks_table_arn,
            feedback_reports_table_arn,
            cost_adjustments_table_arn,
            cmdb_snapshots_table_arn,
            cmdb_history_table_arn,
            settings_table_arn,
            standalone_cases_table_arn,
            # Whitelist (LMS) tool's audit trail (see
            # app/whitelist_store.py, data_stack.py's
            # WhitelistAuditLogTable). This ARN was missing here entirely
            # until now — its absence is what caused every
            # /api/whitelist/lms/* call to 500 with AccessDeniedException
            # (PutItem/Scan on a table the task role was never granted
            # access to), surfacing to the user as a frontend
            # "Network Error".
            whitelist_audit_log_table_arn,
            # DNS Record tool's audit trail (see app/dns_audit_store.py,
            # data_stack.py's DnsAuditLogTable). Needed for exactly the
            # same reason as the whitelist ARN above — without it every
            # /api/dns/records write and /api/dns/audit-log read 500s on
            # AccessDeniedException, which the frontend can only show as
            # a generic failure.
            dns_audit_log_table_arn,
            # S2S VPN Profile Creation tool's audit trail (see
            # app/s2s_vpn_audit_store.py, data_stack.py's
            # S2sVpnAuditLogTable). Needed for exactly the same reason as
            # the DNS ARN above — without it every S2S VPN write/
            # audit-log read 500s on AccessDeniedException.
            s2s_vpn_audit_log_table_arn,
            # Cloud account registry + its audit trail (app/cloud_account_store.py,
            # data_stack.py's CloudAccountsTable / CloudAccountAuditTable). Without
            # these every /api/admin/cloud-accounts call 500s on AccessDeniedException
            # and the registry silently falls back to the built-in account list.
            cloud_accounts_table_arn,
            cloud_account_audit_table_arn,
        ]
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="DynamoDbPortalTables",
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                ],
                resources=portal_table_arns,
            )
        )
        # KMS permissions for the CMK backing the tables above, plus the
        # NoteAttachmentsBucket (same shared CMK — see data_stack.py).
        # kms:Encrypt added alongside GenerateDataKey/Decrypt since S3
        # SSE-KMS PutObject calls can invoke either depending on object
        # size/multipart behavior.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="KmsForDynamoDbCmk",
                actions=["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                resources=[cmk_key_arn],
            )
        )

        portal_secret_arns = [
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/freshservice-api-key-*",
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/freshservice-webhook-hmac-secret-*",
            # Freshservice CHANGE CALENDAR app credentials (a separate
            # internal tool, not the main Freshservice ticketing API above).
            # Read server-side only by app/fs_calendar.py — never exposed
            # to the frontend bundle or Git.
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/fs-calendar-credentials-*",
            # SR Ticket Information system credentials (yet another
            # separate internal tool). Read server-side only by
            # app/sr_ticket.py — never exposed to the frontend bundle or
            # Git. Secret created directly via `aws secretsmanager
            # create-secret` (not modeled as a CDK resource here, since
            # the JSON payload contains the raw password).
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/sr-ticket-credentials-*",
            # Security Team Portal read-only API key ({"api_key": ...}).
            # Read server-side only by app/security_portal.py — never
            # exposed to the frontend bundle or Git. Created directly via
            # `aws secretsmanager create-secret` (not a CDK resource, since
            # the payload is the raw key).
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/security-portal-credentials-*",
            # Cross-account IAM user access keys for the AWS Managed
            # Services (AMS) support account (100000000001). Used by
            # escalation.py to raise AWS Support SRs/Incidents directly
            # against that account instead of this one. Long-lived raw
            # keys (not a cross-account role) per explicit user choice —
            # revisit if AMS ever exposes role-based delegation.
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/aws-ams-support-credentials-*",
            # Cross-account IAM user access keys for the Core account
            # account (100000000001, portal-core-admin). Used by
            # cost_explorer.py to query Cost Explorer directly in that
            # account and to sts:AssumeRole(PortalReadOnlyRole) into every
            # other the organization member account for the Cost Analysis tab.
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/core-admin-credentials-*",
            # Cross-account IAM user access keys for a NARROWLY SCOPED IAM
            # user (job-portal-screensaver-s3) provisioned directly in the
            # Core account (100000000001). Unlike the two
            # entries above, this identity has ONLY
            # s3:PutObject/GetObject/ListBucket on the single
            # `screensaver-100000000001` bucket (ap-southeast-1) — it must
            # NOT be reused for anything else (Cost Explorer, AMS support,
            # etc.), and neither of those other two credentials should be
            # reused for S3 writes here, since neither is scoped for that.
            # Used by app/screensaver.py (Helpdesk Team catalogue ->
            # S3-ScreenSaver tool) to mint presigned PUT URLs for direct
            # browser-to-S3 uploads.
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/s3-screensaver-credentials-*",
            # Cross-account IAM user access keys for a NARROWLY SCOPED
            # write-capable identity for the "Whitelist (LMS)" tool (see
            # app/whitelist_lms.py's module docstring). PROVISIONED
            # 2026-09-08: dedicated IAM user job-portal-whitelist-lms in
            # account 100000000004 ("was-prod" — confirmed live, not just
            # naming-convention inference), with an inline policy scoped
            # to ONLY wafv2:GetIPSet/UpdateIPSet on the 7 named IPSet ARNs
            # (plus account-wide wafv2:ListIPSets, required since IPSets
            # are looked up by Name -> Id and there's no ARN-conditioned
            # way to scope List itself) + ec2:AuthorizeSecurityGroupIngress/
            # RevokeSecurityGroupIngress restricted to just
            # sg-021238ff0c0e522cc (plus account-wide
            # ec2:DescribeSecurityGroups, same ARN-conditioning limitation)
            # — never a broader wafv2:*/ec2:* grant, and never reusing
            # PortalReadOnlyRole (that role is read-only across the whole org
            # by design). Secret stored in this portal's OWN account
            # (100000000002), region us-east-1, exactly as named below.
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/whitelist-lms-credentials-*",
            # AWS DevOps Agent A2A access token (Bearer, scope="operate",
            # client type="agent"). Generated manually in the AWS DevOps
            # Agent web app — Settings > Access Tokens — for Agent Space
            # 00000000-0000-0000-0000-0000000000a4 in account
            # 100000000001 (us-east-1); AWS provides no API/CLI to create
            # or list these tokens by design (the value is shown only
            # once). Read server-side only by app/devops_agent.py — never
            # exposed to the frontend bundle or Git. Max 60-day
            # expiration; must be rotated manually before it expires
            # (create-secret above did not rotate it despite the token
            # having briefly appeared in this session's chat transcript —
            # do that rotation separately, then update the secret value
            # with put-secret-value, not create-secret).
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/devops-agent-access-token-*",
            # Cisco Meraki Dashboard API key (single plain-string bearer
            # key, sent as the X-Cisco-Meraki-API-Key header) for the
            # "Local Infra Hardware Monitor" tool (see
            # app/meraki_monitor.py's module docstring). Meraki is a
            # SaaS platform entirely outside AWS IAM, so this is a single
            # static credential rather than a cross-account role/user
            # pair like the entries above. Read server-side only by
            # app/meraki_monitor.py — never exposed to the frontend
            # bundle or Git.
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/meraki-api-key-*",
            # MySonicWall MSSP API key (single plain-string bearer key,
            # sent as the X-API-KEY header) for the SonicWall sub-tab
            # inside the "Local Infra Hardware Monitor" tool (see
            # app/sonicwall_monitor.py's module docstring). Same rationale
            # as the Meraki entry above: a SaaS platform outside AWS IAM,
            # read server-side only by app/sonicwall_monitor.py.
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/sonicwall-api-key-*",
            # FortiManager Cloud API user credentials (JSON object:
            # {"api_id": "...", "password": "..."}) for the Fortigate
            # Firewall sub-tab inside the "Local Infra Hardware Monitor"
            # tool (see app/fortigate_monitor.py's module docstring for
            # the 3-step OAuth2 -> session -> jsonrpc auth flow this
            # unlocks). Same rationale as the Meraki/SonicWall entries
            # above: a SaaS platform outside AWS IAM, read server-side
            # only by app/fortigate_monitor.py.
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/fortigate-api-credentials-*",
            # Azure AD Service Principal (App Registration
            # "job-portal-azure-reader", single-tenant, example.com)
            # client-credentials (JSON object: {"tenant_id": "...",
            # "client_id": "...", "client_secret": "...",
            # "subscription_id": "..."}) for the "Azure Resources" sidebar
            # category (see app/azure_monitor.py's module docstring for
            # the OAuth2 client-credentials -> ARM/Cost Management API
            # flow this unlocks). This App is assigned ONLY the "Reader"
            # role on the target subscription — v1 scope is deliberately
            # read-only, no write/control capability exists anywhere.
            # Read server-side only by app/azure_monitor.py — never
            # exposed to the frontend bundle or Git. Deliberately a
            # SEPARATE secret from job-portal/azure-ad-client-secret (the
            # existing SSO login app's own client secret, used by the ALB
            # listener) — two entirely different Azure AD App
            # Registrations, mixing them would let a rotation of one
            # silently break the other's unrelated flow.
            f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/azure-reader-credentials-*",
        ]
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SecretsManagerPortalSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=portal_secret_arns,
            )
        )

        # Write access, scoped ONLY to the two "connected portal" secrets
        # that admins can rotate from the Admin page's Password Updates
        # section (see app/connected_portal_credentials.py) — deliberately
        # NOT granted for the other secrets above (AMS/the organization-core/etc admin
        # credentials, Freshservice API key, webhook HMAC secret), which
        # must still only ever be rotated out-of-band via the AWS CLI.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SecretsManagerConnectedPortalWrite",
                actions=["secretsmanager:PutSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/fs-calendar-credentials-*",
                    f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/sr-ticket-credentials-*",
                    f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:job-portal/security-portal-credentials-*",
                ],
            )
        )

        # Bedrock is used from a different AWS account (BEDROCK_ACCOUNT_ID), so
        # the task role holds NO Bedrock permissions of its own any more: it may
        # only assume the invoke role there. That role (see
        # infra-cdk/bedrock-account/bedrock-invoke-role.yaml, deployed in the
        # Bedrock account) grants Claude Sonnet 5.5 (Converse) and GPT-6.1 Terra
        # (bedrock-mantle + Web Search). The previous Nova-specific statements
        # (BedrockInvokeModels / BedrockWebGrounding) were removed with the
        # move off Amazon Nova.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockCrossAccountRole",
                actions=["sts:AssumeRole"],
                resources=[BEDROCK_ROLE_ARN],
            )
        )

        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SesSend",
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=["*"],  # SES identity ARNs are per-verified-identity; broadened until a verified identity ARN is confirmed (Task 9.3)
            )
        )

        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AwsSupportCases",
                actions=[
                    "support:CreateCase",
                    "support:DescribeCases",
                    "support:AddCommunicationToCase",
                    "support:DescribeSeverityLevels",
                    "support:DescribeServices",
                    # Needed by the case-attachments feature (POST
                    # /api/cases/attachments -> escalation.add_attachments_to_set())
                    "support:AddAttachmentsToSet",
                ],
                resources=["*"],  # AWS Support API does not support resource-level scoping
            )
        )

        # AWS Security Hub tab — read-only findings access for Core account
        # Server itself (this task's own identity, called directly). Every
        # other account is reached via sts:AssumeRole(PortalReadOnlyRole)
        # using the Core account credentials (see app/security_hub.py) — that
        # role in each member account already grants whatever Security
        # Hub read access it has; this statement only covers the Core account
        # account's own direct securityhub:* read calls.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SecurityHubReadOnly",
                actions=[
                    "securityhub:GetFindings",
                    "securityhub:DescribeHub",
                    "securityhub:GetInsights",
                ],
                resources=["*"],  # Security Hub API does not support resource-level scoping
            )
        )

        # AWS Security Hub tab — "impacted users/groups/roles" drill-down
        # for IAM-policy findings (e.g. IAM.1 "IAM policies should not
        # allow full '*' administrative privileges"). Covers Core account
        # Server's own direct IAM read calls only; every other account is
        # reached via sts:AssumeRole(PortalReadOnlyRole), same as Security
        # Hub itself (see app/security_hub.py's get_impacted_principals()).
        # Read-only — never creates/modifies/deletes any IAM entity.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SecurityHubIamPrincipalLookupReadOnly",
                actions=[
                    "iam:ListEntitiesForPolicy",
                    "iam:GetGroup",
                    "iam:GetPolicy",
                    "iam:GetPolicyVersion",
                ],
                resources=["*"],  # IAM read APIs used here don't support useful resource-level scoping for this task's cross-account pattern
            )
        )

        # WAF Rule Monitoring tab (Security section) — read-only WAFv2 +
        # CloudWatch + CloudFront access for Core account itself (this
        # task's own identity, called directly). Every other account is
        # reached via sts:AssumeRole(PortalReadOnlyRole) using the Core account
        # credentials (see app/waf_monitoring.py) — that role in each
        # member account already grants whatever WAF/CloudWatch read
        # access it has; this statement only covers Core account's
        # own direct wafv2:*/cloudwatch:*/cloudfront:* read calls.
        # cloudfront:ListDistributions/GetDistribution are needed to
        # discover which CloudFront distribution a CLOUDFRONT-scope Web
        # ACL is attached to (wafv2 has no reverse ACL->resource lookup
        # for that scope). Read-only — never creates/modifies/deletes any
        # Web ACL, rule, or association.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="WafMonitoringReadOnly",
                actions=[
                    "wafv2:ListWebACLs",
                    "wafv2:GetWebACL",
                    "wafv2:ListResourcesForWebACL",
                    "wafv2:GetSampledRequests",
                    "cloudwatch:GetMetricStatistics",
                    "cloudwatch:GetMetricData",
                    "cloudfront:ListDistributions",
                    "cloudfront:GetDistribution",
                ],
                resources=["*"],  # WAFv2/CloudWatch/CloudFront read APIs used here don't support useful resource-level scoping for this task's cross-account pattern
            )
        )

        # AWS Resources tab — read-only access to the S3 bucket the
        # "Resource Inventory Scanner" writes its per-account/per-region/
        # per-resource-type JSON snapshots to. This bucket lives directly
        # in Core account's own account (100000000001), so — unlike
        # Cost Explorer/Billing/Security Hub, which need AssumeRole into
        # every OTHER account's own API — this is a plain, direct,
        # same-account S3 read. No cross-account role is involved.
        # Read-only: ListBucket (to discover which account/region/
        # resource-type files exist) + GetObject (to read their content).
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AwsResourcesInventoryS3ReadOnly",
                actions=["s3:ListBucket"],
                resources=["arn:aws:s3:::itinfra-compliance-reports"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AwsResourcesInventoryS3ObjectReadOnly",
                actions=["s3:GetObject"],
                resources=["arn:aws:s3:::itinfra-compliance-reports/*"],
            )
        )

        # ── Website Monitoring (Monitoring > Website Monitoring) ─────────
        # app/website_monitor.py reads the probe results/screenshots that
        # the CloudWatch Synthetics canaries write to the private central
        # bucket (created by the separate `website_monitor` CloudFormation
        # project, ap-southeast-1, same account) and edits its
        # config/sites.json (admin-only in the app, see main.py). Scoped to
        # that one bucket and its three prefixes - nothing else in S3.
        #   config/*       read + write (site list, admin edits)
        #   results/*      read + delete (delete when a site is removed)
        #   screenshots/*  read (presigned GET for the browser) + delete
        # ListBucket is needed so a missing key is NoSuchKey rather than
        # AccessDenied, and for the delete-on-remove cleanup.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="WebsiteMonitoringConfigReadWrite",
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[f"arn:aws:s3:::{WEBSITE_MONITOR_BUCKET}/config/*"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="WebsiteMonitoringResultsReadDelete",
                actions=["s3:GetObject", "s3:DeleteObject"],
                resources=[
                    f"arn:aws:s3:::{WEBSITE_MONITOR_BUCKET}/results/*",
                    f"arn:aws:s3:::{WEBSITE_MONITOR_BUCKET}/screenshots/*",
                ],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="WebsiteMonitoringListBucket",
                actions=["s3:ListBucket"],
                resources=[f"arn:aws:s3:::{WEBSITE_MONITOR_BUCKET}"],
                conditions={"StringLike": {"s3:prefix": ["config/*", "results/*", "screenshots/*"]}},
            )
        )

        # ── Engineer Notes attachments (Task 11) ─────────────────────────
        # Full read/write/delete on the NoteAttachmentsBucket only (a
        # dedicated, private bucket this stack owns — see
        # data_stack.py's NoteAttachmentsBucket) — scoped to that one
        # bucket's ARN, never a wildcard across all S3 buckets in the
        # account. Presigned GET/PUT URLs generated by the backend also
        # rely on this same task role's credentials, so no extra grant
        # is needed for the presigned-URL flow itself.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="NoteAttachmentsS3ReadWrite",
                actions=["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
                resources=[f"{note_attachments_bucket_arn}/*"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="NoteAttachmentsS3ListBucket",
                actions=["s3:ListBucket"],
                resources=[note_attachments_bucket_arn],
            )
        )

        # ── Chat/Call feature: DynamoDB tables (job-portal-chat-voice spec) ──
        # Same shape as DynamoDbPortalTables above — GetItem/PutItem/
        # UpdateItem/DeleteItem/Query/Scan across the 4 new tables plus
        # their GSI ARNs (CallSessionsTable's ConversationCallHistoryIndex,
        # WsConnectionsTable's UserEmailIndex). A separate policy statement
        # (not merged into DynamoDbPortalTables) so this feature's
        # permission footprint stays independently auditable/removable.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="DynamoDbChatTables",
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                ],
                resources=[
                    chat_conversations_table_arn,
                    chat_participants_table_arn,
                    chat_messages_table_arn,
                    call_sessions_table_arn,
                    f"{call_sessions_table_arn}/index/*",
                    ws_connections_table_arn,
                    f"{ws_connections_table_arn}/index/*",
                ],
            )
        )

        # ── Chat/Call feature: DynamoDB Streams read access ──────────────
        # Required by app/ws_manager.py's cross-task fan-out poller,
        # which subscribes to ChatMessagesTable's and CallSessionsTable's
        # Streams so every ECS task can observe new/edited messages and
        # call-signaling events and forward them only to the WebSocket
        # connections it personally holds (see the chat-voice design
        # doc's "cross-task message and signaling fan-out" section — no
        # target-group stickiness exists on this ALB, so this is the
        # mechanism that makes delivery work regardless of which task a
        # recipient's WS connection landed on).
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="DynamoDbChatStreamsReadOnly",
                actions=[
                    "dynamodb:DescribeStream",
                    "dynamodb:GetRecords",
                    "dynamodb:GetShardIterator",
                    "dynamodb:ListStreams",
                ],
                resources=[
                    f"{chat_messages_table_arn}/stream/*",
                    f"{call_sessions_table_arn}/stream/*",
                ],
            )
        )
        # dynamodb:DescribeTable is required as a separate statement because
        # its resource is the table ARN itself (not the "/stream/*" suffix
        # used above) — ws_manager.py's _poll_table_stream() calls
        # DescribeTable first to discover each table's LatestStreamArn
        # before it can use the four stream-read actions above.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="DynamoDbChatStreamsDescribeTable",
                actions=["dynamodb:DescribeTable"],
                resources=[
                    chat_messages_table_arn,
                    call_sessions_table_arn,
                ],
            )
        )

        # ── Chat/Call feature: S3 chat attachments ───────────────────────
        # Same shape as NoteAttachmentsS3ReadWrite/ListBucket above, but
        # for the presigned-PUT attachments bucket (see
        # data_stack.py's ChatAttachmentsBucket and
        # app/chat_attachments.py) — scoped to that one bucket's ARN only.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="ChatAttachmentsS3ReadWrite",
                actions=["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
                resources=[f"{chat_attachments_bucket_arn}/*"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="ChatAttachmentsS3ListBucket",
                actions=["s3:ListBucket"],
                resources=[chat_attachments_bucket_arn],
            )
        )

        # ── Chat/Call feature: Amazon Chime SDK Meetings API ─────────────
        # resources=["*"] because the Chime SDK Meetings API does not
        # support resource-level ARN scoping for these actions — same
        # rationale already documented for SesSend/AwsSupportCases/
        # SecurityHubReadOnly/WafMonitoringReadOnly above. Used by
        # app/chime_client.py to create/join/delete the per-Call meeting
        # resource; meetings are created only at Call-initiation time and
        # deleted the moment a Call ends, so no idle Chime SDK cost
        # accrues (see chat-voice spec Requirement 10).
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="ChimeSdkMeetings",
                actions=[
                    "chime:CreateMeeting",
                    "chime:CreateMeetingWithAttendees",
                    "chime:CreateAttendee",
                    "chime:BatchCreateAttendee",
                    "chime:DeleteMeeting",
                    "chime:GetMeeting",
                    "chime:GetAttendee",
                    "chime:DeleteAttendee",
                    "chime:ListAttendees",
                ],
                resources=["*"],  # Chime SDK Meetings API does not support resource-level scoping
            )
        )

        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="LogsForPortalGroupOnly",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{self.log_group.log_group_arn}:*"],
            )
        )

        # Allows this task role to assume a dedicated cross-account role
        # (JobPortal-CrossAccount-InfraOps-*) provisioned in target
        # accounts, for a new infra-ops feature that needs write-capable
        # (not just PortalReadOnlyRole read-only) cross-account access. The
        # account ID is wildcarded because the role is meant to be
        # assumable in any account it's provisioned into — the actual
        # security boundary is the target account's own trust policy on
        # that role (it must only trust THIS task role's ARN back), plus
        # whatever permissions are attached to the role itself in each
        # target account.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeCrossAccountInfraOpsRole",
                actions=["sts:AssumeRole"],
                resources=["arn:aws:iam::*:role/JobPortal-CrossAccount-InfraOps-*"],
            )
        )

        # ── Fargate task definition ──────────────────────────────────────
        # CPU/memory doubled (512->1024, 1024->2048) as a second safety-net
        # layer on top of the CMDB background-fetch architecture fix
        # (app/cmdb.py: the org-wide scan now always runs on a background
        # thread with lower fan-out concurrency, so it no longer pegs the
        # single request thread/CPU for ~15 minutes). This bump gives every
        # task more headroom to absorb bursts (multiple concurrent users,
        # the 2 uvicorn workers below, Bedrock calls, etc.) without the
        # whole container starving, even though the primary fix already
        # addresses the CMDB-specific root cause on its own.
        task_definition = ecs.FargateTaskDefinition(
            self,
            "JobPortalTaskDef",
            cpu=1024,
            memory_limit_mib=2048,
            execution_role=execution_role,
            task_role=task_role,
        )

        container = task_definition.add_container(
            "backend",
            image=ecs.ContainerImage.from_ecr_repository(self.repository, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="job-portal",
                log_group=self.log_group,
            ),
            environment={
                "ENV": "production",
                "AWS_REGION": AWS_REGION,
                # Cross-account Bedrock (Claude Sonnet 5.5 + GPT-6.1 Terra)
                "BEDROCK_ROLE_ARN": BEDROCK_ROLE_ARN,
                "FRESHSERVICE_DOMAIN": CONFIG["freshservice_domain"],
                "SES_SENDER_ADDRESS": CONFIG["ses_sender_address"],
                # Organisation identity (see backend/app/org_config.py)
                "ORG_NAME": CONFIG["org_name"],
                "PORTAL_DOMAIN": CUSTOM_DOMAIN,
                "API_DOMAIN": CONFIG["api_domain"],
                "ORG_EMAIL_DOMAIN": CONFIG["org_email_domain"],
                "PORTAL_HOST_ACCOUNT_ID": AWS_ACCOUNT_ID,
                "CORE_ACCOUNT_ID": CONFIG["core_account_id"],
                "ENABLED_FEATURES": CONFIG["enabled_features"],
                # Task 12: after the Azure AD OIDC round trip completes at
                # the ALB, GET /login-landing bounces the browser back to
                # the deployed SPA rather than leaving it on a bare
                # backend JSON response. Uses the custom domain
                # (CUSTOM_DOMAIN), NOT the raw CloudFront distribution
                # domain, so users stay on the custom domain after login.
                "FRONTEND_URL": f"https://{CUSTOM_DOMAIN}",
                # Non-secret public identifier (same one used above to
                # build the ALB's own authorize/token endpoints) — passed
                # through to the backend so GET /api/auth/logout can
                # build the matching Azure AD v2.0 end-session (logout)
                # redirect URL without hardcoding the tenant ID a second
                # time in application code.
                "AZURE_AD_TENANT_ID": AZURE_TENANT_ID,
                "USERS_TABLE_NAME": "job-portal-users",
                "HEALTH_EVENT_CHECKS_TABLE_NAME": "job-portal-health-event-checks",
                "COST_ADJUSTMENTS_TABLE_NAME": "job-portal-cost-adjustments",
                "CMDB_SNAPSHOTS_TABLE_NAME": "job-portal-cmdb-snapshots",
                "CMDB_HISTORY_TABLE_NAME": "job-portal-cmdb-history",
                "SETTINGS_TABLE_NAME": "job-portal-settings",
                "STANDALONE_CASES_TABLE_NAME": "job-portal-standalone-cases",
                "WHITELIST_AUDIT_TABLE_NAME": "job-portal-whitelist-audit-log",
                "DNS_AUDIT_TABLE_NAME": "job-portal-dns-audit-log",
                "S2S_VPN_AUDIT_TABLE_NAME": "job-portal-s2s-vpn-audit-log",
                "CLOUD_ACCOUNTS_TABLE_NAME": "job-portal-cloud-accounts",
                "CLOUD_ACCOUNT_AUDIT_TABLE_NAME": "job-portal-cloud-account-audit",
                "NOTE_ATTACHMENTS_BUCKET_NAME": note_attachments_bucket_name,
                # Website Monitoring - read by app/website_monitor.py. Needs
                # the WebsiteMonitoring* IAM grants above.
                "WEBSITE_MONITOR_BUCKET": WEBSITE_MONITOR_BUCKET,
                "WEBSITE_MONITOR_REGION": WEBSITE_MONITOR_REGION,
                # Chat/Call feature (job-portal-chat-voice spec) — table
                # names read by app/chat_store.py, app/call_store.py, and
                # app/ws_connections.py. Also requires the DynamoDbChatTables
                # IAM grant and DynamoDbChatStreamsReadOnly IAM grant above —
                # omitting either one silently 500s on AccessDeniedException,
                # same failure mode already documented for
                # WhitelistAuditLogTable/DnsAuditLogTable elsewhere in this
                # file.
                "CHAT_CONVERSATIONS_TABLE_NAME": "job-portal-chat-conversations",
                "CHAT_PARTICIPANTS_TABLE_NAME": "job-portal-chat-participants",
                "CHAT_MESSAGES_TABLE_NAME": "job-portal-chat-messages",
                "CALL_SESSIONS_TABLE_NAME": "job-portal-call-sessions",
                "WS_CONNECTIONS_TABLE_NAME": "job-portal-ws-connections",
                "CHAT_ATTACHMENTS_BUCKET_NAME": chat_attachments_bucket_name,
                # Seed admins — always treated as admin + always allowed,
                # even if the DynamoDB users table is empty (first boot) or
                # someone accidentally removes themselves. Comma-separated.
                "SEED_ADMIN_EMAILS": CONFIG["seed_admin_emails"],
                # Seed members — only applied once, when the users table is
                # first created (empty). After that, manage users via the
                # Admin page; this list is ignored.
                "SEED_MEMBER_EMAILS": CONFIG["seed_member_emails"],
                # Freshservice Change Calendar (external tool) proxy config
                # — see app/fs_calendar.py. The API base URL is not secret;
                # the API key ({"api_key": ...}) is read from Secrets Manager
                # at request time (FS_CALENDAR_CREDENTIALS_SECRET_NAME below),
                # never placed in an env var here.
                "FS_CALENDAR_API_BASE": CONFIG["extra_env"].get("FS_CALENDAR_API_BASE", ""),
                "FS_CALENDAR_CREDENTIALS_SECRET_NAME": "job-portal/fs-calendar-credentials",
                # SR Ticket Information (external tool) proxy config —
                # see app/sr_ticket.py. The API base URL is not secret;
                # the username/password are read from Secrets Manager at
                # request time (SR_TICKET_CREDENTIALS_SECRET_NAME below),
                # never placed in an env var here.
                "SR_TICKET_API_BASE": CONFIG["extra_env"].get("SR_TICKET_API_BASE", ""),
                "SR_TICKET_CREDENTIALS_SECRET_NAME": "job-portal/sr-ticket-credentials",
                # Security Team Portal (external tool) proxy config — see
                # app/security_portal.py. Only the read-only X-API-Key
                # surface (/external-api) is used. The base URL is not
                # secret; the key is read from Secrets Manager at request
                # time, never placed in an env var here.
                "SECURITY_PORTAL_API_BASE": CONFIG["extra_env"].get("SECURITY_PORTAL_API_BASE", ""),
                "SECURITY_PORTAL_CREDENTIALS_SECRET_NAME": "job-portal/security-portal-credentials",
                # AWS DevOps Agent A2A integration (app/devops_agent.py).
                # DEVOPS_AGENT_REGION defaults to "us-east-1" in code —
                # only needed here if the Agent Space ever moves region.
                # The access token itself is read from Secrets Manager at
                # request time, never placed in an env var (see the
                # SecretsManagerPortalSecrets policy above).
                "DEVOPS_AGENT_TOKEN_SECRET_NAME": "job-portal/devops-agent-access-token",
                # Any extra backend settings from config.json ("extra_env"),
                # e.g. KNOWN_BUSINESS_UNITS, S2S_VPN_TGW_CONFIG, MERAKI_ORG_ID,
                # RESOURCE_INVENTORY_BUCKET, SCREENSAVER_BUCKET_NAME ...
                **CONFIG["extra_env"],
            },
            port_mappings=[ecs.PortMapping(container_port=8100, protocol=ecs.Protocol.TCP)],
        )

        # ── Fargate service — 2 tasks across 2 private subnets ───────────
        self.service = ecs.FargateService(
            self,
            "JobPortalService",
            cluster=self.cluster,
            task_definition=task_definition,
            desired_count=2,
            security_groups=[ecs_task_security_group],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            assign_public_ip=False,
            min_healthy_percent=50,
            max_healthy_percent=200,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )

        scaling = self.service.auto_scale_task_count(min_capacity=2, max_capacity=6)
        scaling.scale_on_cpu_utilization(
            "CpuScaling", target_utilization_percent=60, scale_in_cooldown=Duration.seconds(60), scale_out_cooldown=Duration.seconds(60)
        )
        scaling.scale_on_memory_utilization(
            "MemScaling", target_utilization_percent=70, scale_in_cooldown=Duration.seconds(60), scale_out_cooldown=Duration.seconds(60)
        )

        # ── ALB ───────────────────────────────────────────────────────────
        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "JobPortalAlb",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_security_group,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            # Default is 60s. Was raised to 200s (2026-09-14) to work
            # around a production "Network Error" caused by the AWS
            # DevOps Agent A2A integration (app/devops_agent.py) making a
            # single long-lived blocking call that could exceed 60s.
            #
            # That integration was redesigned the same day to an async
            # task-polling model (see app/devops_agent.py's "Async task
            # model" docstring section) specifically so it no longer
            # needs any long-lived request: message:send and GetTask both
            # return promptly now, and the frontend polls GetTask on a
            # timer instead of holding one connection open for minutes.
            # 90s restores comfortable headroom above every remaining
            # call's own timeout in this app (devops_agent.py's httpx
            # read: 45s; frontend/src/api.js's devopsAgentChat /
            # getDevOpsAgentTaskStatus axios timeouts: 30s each) without
            # reverting all the way to the 60s default, in case some
            # other slow-but-legitimate request elsewhere in the app
            # needs the extra margin.
            idle_timeout=Duration.seconds(90),
        )

        target_group = elbv2.ApplicationTargetGroup(
            self,
            "JobPortalTargetGroup",
            vpc=vpc,
            port=8100,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[self.service],
            health_check=elbv2.HealthCheck(
                path="/api/health",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
            ),
        )

        # HTTP :80 -> 301 redirect to HTTPS (Req 9.3).
        # CloudFront uses HTTPS:443 to reach the ALB origin, not HTTP:80,
        # so browser direct-HTTP requests are still properly redirected.
        self.alb.add_listener(
            "HttpListener",
            port=80,
            default_action=elbv2.ListenerAction.redirect(
                protocol="HTTPS", port="443", permanent=True
            ),
        )

        # Azure AD OIDC client secret, referenced by full ARN (including the
        # random suffix Secrets Manager appends) — ALB's authenticate-oidc
        # action requires the complete ARN to resolve the secret; the
        # name-only lookup (from_secret_name_v2) builds a suffix-less ARN
        # that ALB cannot find, which is what caused the first deploy
        # attempt's CREATE_FAILED (ResourceNotFoundException) on the HTTPS
        # listener. The secret value itself never enters app code or this
        # stack's synthesized template in plaintext — ALB reads it natively
        # at request time.
        azure_client_secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "AzureClientSecretSecret",
            # Full ARN of the secret job-portal/azure-ad-client-secret (with
            # Secrets Manager's 6-character suffix): `aws secretsmanager
            # describe-secret --secret-id job-portal/azure-ad-client-secret`.
            CONFIG["azure_client_secret_arn"],
        )

        default_oidc_action = elbv2.ListenerAction.authenticate_oidc(
            authorization_endpoint=(
                f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/authorize"
            ),
            client_id=AZURE_CLIENT_ID,
            client_secret=azure_client_secret.secret_value,
            issuer=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/v2.0",
            token_endpoint=(
                f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/token"
            ),
            user_info_endpoint="https://graph.microsoft.com/oidc/userinfo",
            next=elbv2.ListenerAction.forward([target_group]),
            scope="openid email profile",
            session_timeout=Duration.hours(8),
        )

        # HTTPS :443 listener, TLS1.2+ policy (Req 9.4). Default action is the
        # Azure AD OIDC challenge (Req 1.1); the webhook path below is carved
        # out as a higher-priority rule that bypasses OIDC entirely (Req 7.2).
        certificate = acm.Certificate.from_certificate_arn(self, "AlbCertificate", IMPORTED_CERT_ARN)
        https_listener = self.alb.add_listener(
            "HttpsListener",
            port=443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[certificate],
            ssl_policy=elbv2.SslPolicy.TLS13_RES,  # ELBSecurityPolicy-TLS13-1-2-2021-06 equivalent
            default_action=default_oidc_action,
        )

        # Freshservice webhook path — NO OIDC, auth is HMAC inside the app (Req 7.2)
        https_listener.add_action(
            "FreshserviceWebhookRule",
            priority=10,
            conditions=[elbv2.ListenerCondition.path_patterns(["/api/webhooks/freshservice*"])],
            action=elbv2.ListenerAction.forward([target_group]),
        )

        # File upload + escalation bypass — NO OIDC (2026-07-16 fix).
        #
        # These paths send multipart/form-data POST or JSON POST with a
        # session cookie from a cross-site origin (CloudFront → ALB).
        # Chrome's SameSite=None enforcement means the ALB session cookie
        # is dropped on some cross-site POST requests, causing ALB to
        # 302-redirect to Azure AD instead of forwarding to the backend.
        # The backend's get_actor() dependency still validates X-Actor-Email
        # against the allowlist, so these routes are NOT unauthenticated —
        # they just bypass the ALB-level OIDC check and rely on app-level auth.
        # NOTE: an ALB listener rule's path_patterns condition is capped at 5
        # values, so this bypass is split across two rules (priority 8 and
        # 9) rather than one — both forward to the same target_group with
        # the same "no OIDC, app-level X-Actor-Email auth" behavior above.
        https_listener.add_action(
            "UploadAndEscalateBypassRule",
            priority=8,
            conditions=[elbv2.ListenerCondition.path_patterns([
                "/api/extract/*",
                "/api/kb/*",
                "/api/jobs/*/escalate",
                "/api/jobs/*/analyse",
                "/api/jobs/*/refresh-ams-status",
            ])],
            action=elbv2.ListenerAction.forward([target_group]),
        )
        https_listener.add_action(
            "CasesBypassRule",
            priority=9,
            conditions=[elbv2.ListenerCondition.path_patterns([
                "/api/cases/*/reply",
                "/api/cases/attachments",
                "/api/health-dashboard/organization/check",
                # Engineer Notes photo/file attachment upload (Task 11) —
                # same multipart-POST-with-cookie SameSite issue as the
                # other bypassed routes above.
                "/api/jobs/*/notes/*/attachments",
                # Helpdesk Team catalogue -> S3-ScreenSaver: multipart-
                # POST upload call (backend-proxy upload, see
                # app/screensaver.py's module docstring), same multipart-
                # POST-with-cookie SameSite issue as the other bypassed
                # routes in this rule.
                "/api/screensaver/*",
            ])],
            action=elbv2.ListenerAction.forward([target_group]),
        )

        # Chat/Call WebSocket path — deliberately NOT a bypass rule (see
        # the chat-voice design doc's "WebSocket authentication through
        # the ALB" section). Forwards to the same target group with the
        # listener's DEFAULT authenticate-oidc behaviour, exactly the
        # same as any other unmatched path — the SPA always completes
        # the OIDC login over plain HTTPS before opening this WebSocket,
        # so the ALB's `AWSELBAuthSessionCookie-*` session cookie already
        # exists by the time the WS upgrade handshake fires, and the ALB
        # is expected to inject `x-amzn-oidc-data` on that handshake the
        # same way it does on any authenticated HTTPS request (verified
        # empirically as part of implementing app/main.py's `/ws` route
        # — see that task's notes for the outcome of this verification).
        # This rule exists ONLY to route the path to the target group;
        # explicitly listing it here (rather than relying on the
        # listener's own default_action) keeps its priority visible
        # alongside the other path-based rules in this file, and keeps
        # it easy to audit that this path was never added to either
        # bypass rule below.
        https_listener.add_action(
            "ChatWebSocketRule",
            priority=11,
            conditions=[elbv2.ListenerCondition.path_patterns(["/ws/*"])],
            action=elbv2.ListenerAction.forward([target_group]),
        )

        # CORS preflight bypass — NO OIDC (Task 12 fix, 2026-07-15).
        #
        # Bug: the frontend (CloudFront domain) is a different origin from
        # the backend (ALB domain), so every browser request that carries a
        # custom header (X-Actor-Email) or a non-"simple" method/content-type
        # (POST with a JSON body, PATCH, DELETE, etc.) triggers a CORS
        # preflight OPTIONS request first. Per the Fetch spec, a preflight
        # request NEVER carries credentials/cookies, even when the real
        # request that follows sets `withCredentials: true`. Without this
        # rule, that credential-less OPTIONS request hit the default
        # authenticate-oidc action below, which saw no valid ALB session
        # cookie and responded with a 302 redirect to the Azure AD login
        # page instead of a proper 200/204 CORS preflight response. Browsers
        # treat any non-2xx preflight response as a hard CORS failure and
        # never send the real request at all — this is what surfaced as
        # "Network Error" in the chatbot (and would have broken every other
        # cross-origin call using a custom header or JSON body, not just
        # chat).
        #
        # Fix: forward ALL OPTIONS requests straight to the target group,
        # bypassing OIDC entirely. This is safe — an OPTIONS preflight never
        # reaches application business logic; FastAPI's CORSMiddleware
        # (main.py) answers it directly with the correct
        # Access-Control-Allow-* headers and nothing else.
        https_listener.add_action(
            "CorsPreflightBypassRule",
            priority=5,
            conditions=[elbv2.ListenerCondition.http_request_methods(["OPTIONS"])],
            action=elbv2.ListenerAction.forward([target_group]),
        )

        # Associate the regional WAF WebACL with the ALB (Req 9.10)
        wafv2.CfnWebACLAssociation(
            self,
            "AlbWafAssociation",
            resource_arn=self.alb.load_balancer_arn,
            web_acl_arn=alb_web_acl.attr_arn,
        )

        CfnOutput(self, "AlbDnsName", value=self.alb.load_balancer_dns_name)
        CfnOutput(self, "CustomDomain", value=CUSTOM_DOMAIN)
        CfnOutput(self, "EcrRepositoryUri", value=self.repository.repository_uri)
        CfnOutput(
            self,
            "TlsCertificateNote",
            value=(
                f"ALB HTTPS listener now uses a real, publicly-trusted ACM "
                f"certificate for {CUSTOM_DOMAIN} (DNS-validated, us-east-1). "
                f"DNS: {CUSTOM_DOMAIN} CNAME -> <AlbDnsName>. Redirect URI for "
                f"Azure AD app registration: https://{CUSTOM_DOMAIN}/oauth2/idpresponse "
                f"(update the app registration's Redirect URIs to this value; "
                f"the old https://<AlbDnsName>/oauth2/idpresponse entry can be "
                f"removed once traffic has moved to the custom domain)."
            ),
        )
