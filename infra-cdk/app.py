#!/usr/bin/env python3
"""
CDK app entry point for the Cloud Infra Job Portal.

Account/region come from infra-cdk/config.json (see config.example.json).
"""

import aws_cdk as cdk

from infra_cdk.config import CONFIG
from infra_cdk.stacks.network_stack import NetworkStack
from infra_cdk.stacks.data_stack import DataStack
from infra_cdk.stacks.security_stack import SecurityStack
from infra_cdk.stacks.compute_stack import ComputeStack
from infra_cdk.stacks.frontend_stack import FrontendStack
from infra_cdk.stacks.observability_stack import ObservabilityStack

app = cdk.App()

env = cdk.Environment(account=CONFIG["account_id"], region=CONFIG["region"])

network_stack = NetworkStack(app, "JobPortalNetworkStack", env=env)
data_stack = DataStack(app, "JobPortalDataStack", env=env)
security_stack = SecurityStack(app, "JobPortalSecurityStack", env=env)

compute_stack = ComputeStack(
    app,
    "JobPortalComputeStack",
    env=env,
    vpc=network_stack.vpc,
    alb_security_group=network_stack.alb_security_group,
    ecs_task_security_group=network_stack.ecs_task_security_group,
    alb_web_acl=security_stack.alb_web_acl,
    jobs_table_arn=data_stack.jobs_table.table_arn,
    kb_documents_table_arn=data_stack.kb_documents_table.table_arn,
    mappings_table_arn=data_stack.mappings_table.table_arn,
    sync_log_table_arn=data_stack.sync_log_table.table_arn,
    webhook_idempotency_table_arn=data_stack.webhook_idempotency_table.table_arn,
    notification_dedup_table_arn=data_stack.notification_dedup_table.table_arn,
    users_table_arn=data_stack.users_table.table_arn,
    health_event_checks_table_arn=data_stack.health_event_checks_table.table_arn,
    feedback_reports_table_arn=data_stack.feedback_reports_table.table_arn,
    cost_adjustments_table_arn=data_stack.cost_adjustments_table.table_arn,
    cmdb_snapshots_table_arn=data_stack.cmdb_snapshots_table.table_arn,
    cmdb_history_table_arn=data_stack.cmdb_history_table.table_arn,
    settings_table_arn=data_stack.settings_table.table_arn,
    standalone_cases_table_arn=data_stack.standalone_cases_table.table_arn,
    whitelist_audit_log_table_arn=data_stack.whitelist_audit_log_table.table_arn,
    dns_audit_log_table_arn=data_stack.dns_audit_log_table.table_arn,
    s2s_vpn_audit_log_table_arn=data_stack.s2s_vpn_audit_log_table.table_arn,
    cloud_accounts_table_arn=data_stack.cloud_accounts_table.table_arn,
    cloud_account_audit_table_arn=data_stack.cloud_account_audit_table.table_arn,
    cmk_key_arn=data_stack.cmk.key_arn,
    note_attachments_bucket_arn=data_stack.note_attachments_bucket.bucket_arn,
    note_attachments_bucket_name=data_stack.note_attachments_bucket.bucket_name,
    chat_conversations_table_arn=data_stack.chat_conversations_table.table_arn,
    chat_participants_table_arn=data_stack.chat_participants_table.table_arn,
    chat_messages_table_arn=data_stack.chat_messages_table.table_arn,
    call_sessions_table_arn=data_stack.call_sessions_table.table_arn,
    ws_connections_table_arn=data_stack.ws_connections_table.table_arn,
    chat_attachments_bucket_arn=data_stack.chat_attachments_bucket.bucket_arn,
    chat_attachments_bucket_name=data_stack.chat_attachments_bucket.bucket_name,
)
compute_stack.add_dependency(network_stack)
compute_stack.add_dependency(data_stack)
compute_stack.add_dependency(security_stack)

frontend_stack = FrontendStack(
    app,
    "JobPortalFrontendStack",
    env=env,
    cloudfront_web_acl=security_stack.cloudfront_web_acl,
    # Route /api/* through CloudFront → ALB to eliminate CORS issues.
    # ALB DNS name is a known stable output of ComputeStack.
    alb_dns_name=compute_stack.alb.load_balancer_dns_name,
)
frontend_stack.add_dependency(security_stack)
frontend_stack.add_dependency(compute_stack)

observability_stack = ObservabilityStack(app, "JobPortalObservabilityStack", env=env)
observability_stack.add_dependency(compute_stack)

app.synth()
