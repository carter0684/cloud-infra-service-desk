"""
NetworkStack — VPC, subnets, NAT, VPC endpoints, and security groups
for the Cloud Infra Job Portal.

Account: 100000000002
Region:  us-east-1
"""

from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
)
from constructs import Construct


class NetworkStack(Stack):
    """
    Purpose-built VPC for the Job Portal (the account's default VPC has
    public-only subnets across 6 AZs and is not suitable for a 3-tier design).

    - 2 AZs (sufficient for a 3-person internal tool; keeps NAT cost to one GW)
    - Public subnets: ALB, NAT Gateway
    - Private subnets: ECS Fargate tasks
    - Gateway endpoint for DynamoDB (free, keeps DB traffic off the NAT path)
    - Interface endpoints for Secrets Manager, Bedrock-runtime, ECR, CloudWatch
      Logs, and SES to minimize reliance on the NAT Gateway (Well-Architected:
      prefer PrivateLink over internet routing for AWS service traffic).
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self,
            "JobPortalVpc",
            ip_addresses=ec2.IpAddresses.cidr("10.20.0.0/16"),
            # Pinned explicitly (rather than max_azs) because not every AWS
            # PrivateLink interface endpoint is available in every AZ. The
            # SES VPC endpoint (com.amazonaws.<region>.email-smtp) in
            # us-east-1 is only available in 1a/1c/1d, not 1b — using
            # max_azs=2 non-deterministically picked 1a+1b and broke the
            # SES endpoint deployment. 1a+1c are safe for all endpoints
            # this stack creates (Secrets Manager, Bedrock-runtime, ECR,
            # CloudWatch Logs, SES).
            availability_zones=["us-east-1a", "us-east-1c"],
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # Gateway endpoint — DynamoDB (no hourly cost, no data transfer cost)
        self.vpc.add_gateway_endpoint(
            "DynamoDbEndpoint",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
        )

        # Interface endpoints — keep AWS-service traffic on the AWS backbone
        interface_services = {
            "SecretsManagerEndpoint": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            "BedrockRuntimeEndpoint": ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            "EcrApiEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDkrEndpoint": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "CloudWatchLogsEndpoint": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            "SesEndpoint": ec2.InterfaceVpcEndpointAwsService.SES,
        }
        for name, service in interface_services.items():
            self.vpc.add_interface_endpoint(name, service=service)

        # Security groups
        self.alb_security_group = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=self.vpc,
            description="Job Portal ALB - public HTTPS/HTTP ingress",
            allow_all_outbound=True,
        )
        self.alb_security_group.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS from anywhere"
        )
        self.alb_security_group.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP from anywhere (redirected to HTTPS)"
        )

        self.ecs_task_security_group = ec2.SecurityGroup(
            self,
            "EcsTaskSecurityGroup",
            vpc=self.vpc,
            description="Job Portal ECS Fargate tasks - ingress only from ALB",
            allow_all_outbound=True,
        )
        self.ecs_task_security_group.add_ingress_rule(
            ec2.Peer.security_group_id(self.alb_security_group.security_group_id),
            ec2.Port.tcp(8100),
            "Backend API port from ALB only",
        )
