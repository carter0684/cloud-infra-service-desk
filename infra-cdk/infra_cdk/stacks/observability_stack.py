"""
ObservabilityStack — CloudWatch dashboard/log retention and CloudTrail
for the Cloud Infra Job Portal.

Placeholder in Task 1 — implemented fully in Task 13.
"""

from aws_cdk import Stack
from constructs import Construct


class ObservabilityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # Implemented in Task 13: CloudWatch dashboard, log group retention,
        # CloudTrail trail + versioned/MFA-delete S3 bucket.
