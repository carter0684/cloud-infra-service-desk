"""
Mock AWS Bedrock AI analysis.
In production, replace analyse_job() with a real boto3 Bedrock InvokeModel call.
"""

import random
from datetime import datetime

CATEGORY_MAP = {
    "Networking": "Network Connectivity",
    "Compute": "Compute / EC2",
    "Storage": "Storage / S3",
    "Security": "Security / IAM",
    "AD/Identity": "Directory Services",
    "Other": "General Cloud",
}

SEVERITY_MAP = {
    "ASAP": ("critical", "Critical — business-critical system down or inaccessible"),
    "High": ("high", "High — important functions impaired, workaround exists"),
    "Medium": ("normal", "Normal — system impaired, non-critical functions affected"),
    "Low": ("low", "Low — general guidance or feature request"),
}

REMEDIATION_TEMPLATES = {
    "Networking": [
        "Verify security group inbound/outbound rules allow required traffic on relevant ports.",
        "Check VPC route tables for correct routing entries to target subnets.",
        "Confirm NAT Gateway or Internet Gateway is attached and route is propagated.",
        "Review Network ACLs — stateless rules may be blocking return traffic.",
        "Use VPC Reachability Analyser to trace the exact path blocking connectivity.",
    ],
    "Compute": [
        "Check EC2 instance status checks (System Status / Instance Status) in the console.",
        "Review CloudWatch metrics for CPU, memory, and network I/O anomalies.",
        "Inspect instance system log and screenshot via EC2 console for boot errors.",
        "Verify IAM instance profile has required permissions for the workload.",
        "Check Auto Scaling lifecycle hooks — instance may be stuck in Pending:Wait.",
    ],
    "Storage": [
        "Verify S3 bucket policy and ACL allow the calling principal's actions.",
        "Check bucket versioning and lifecycle rules — objects may be in a delete marker state.",
        "Confirm KMS key policy grants the IAM role kms:GenerateDataKey and kms:Decrypt.",
        "Review S3 access logs or CloudTrail for recent AccessDenied events.",
        "Check EBS volume attachment state and file system integrity with fsck.",
    ],
    "Security": [
        "Review IAM policy attached to the role — use IAM Access Analyser for policy validation.",
        "Check SCPs (Service Control Policies) in AWS Organizations for blanket denies.",
        "Confirm MFA is enforced correctly and not blocking programmatic access.",
        "Review CloudTrail for recent unauthorised API calls or access pattern anomalies.",
        "Enable AWS Security Hub and run a compliance check against AWS Foundational Best Practices.",
    ],
    "AD/Identity": [
        "Verify AWS Managed Microsoft AD domain join configuration and DNS resolution.",
        "Check trust relationship status between on-premises AD and AWS Directory Service.",
        "Confirm AD Connector is healthy and on-premises DCs are reachable from VPC.",
        "Review CloudWatch Logs for AWS Managed AD controller events and errors.",
        "Validate security group rules allow LDAP (389), LDAPS (636), Kerberos (88), DNS (53) traffic.",
    ],
    "Other": [
        "Review recent CloudTrail events for API call errors related to this service.",
        "Check AWS Service Health Dashboard for any active incidents in your region.",
        "Verify IAM permissions are sufficient for the operation being performed.",
        "Check resource limits in Service Quotas and request an increase if needed.",
        "Enable AWS Config and review compliance status for related resources.",
    ],
}


def analyse_job(title: str, description: str, category: str, priority: str, audit_log: list) -> dict:
    """
    Simulate Bedrock analysis of a job.
    Returns a structured draft suitable for an AWS Support ticket.
    """
    effective_category = category if category != "Other" else "Other"
    issue_type = CATEGORY_MAP.get(effective_category, "General Cloud")
    severity_code, severity_label = SEVERITY_MAP.get(priority, ("normal", "Normal"))
    remediations = REMEDIATION_TEMPLATES.get(effective_category, REMEDIATION_TEMPLATES["Other"])

    # Pick 3 most relevant remediation steps (mock: first 3)
    recommended_steps = remediations[:3]

    ticket_subject = f"[{issue_type}] {title[:80]}"
    ticket_description = (
        f"**Issue Summary**\n{description}\n\n"
        f"**Steps to Reproduce**\n"
        f"1. Identify the affected resource in the AWS console.\n"
        f"2. Attempt the operation described above.\n"
        f"3. Observe the error or unexpected behaviour.\n\n"
        f"**Expected Behaviour**\nThe resource should operate normally without errors.\n\n"
        f"**Actual Behaviour**\n{description}\n\n"
        f"**Additional Context**\n"
        f"Priority as assessed internally: {priority}\n"
        f"Job last updated: {audit_log[-1]['timestamp'] if audit_log else 'N/A'}"
    )

    return {
        "issue_summary": f"AWS {issue_type} issue detected: {title}",
        "issue_type": issue_type,
        "severity": severity_code,
        "severity_label": severity_label,
        "recommended_steps": recommended_steps,
        "ticket_subject": ticket_subject,
        "ticket_description": ticket_description,
        "generated_at": datetime.utcnow().isoformat(),
    }
