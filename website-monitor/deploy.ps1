<#
.SYNOPSIS
  Deploys the website monitor probes: the central stack (private results bucket + SNS alert topic),
  the initial site list, and one CloudWatch Synthetics canary stack per probe region in sites.json.

  The UI is the Cloud Infra Service Desk portal (Monitoring > Website Monitoring). That portal's
  ECS task needs the matching IAM grant and env vars (infra-cdk compute_stack.py,
  WEBSITE_MONITOR_BUCKET / WEBSITE_MONITOR_REGION) - deploy the portal's ComputeStack after this.

.EXAMPLE
  .\deploy.ps1 -AlertEmail ops@example.com,secops@example.com -AwsProfile myprofile   # up to 3 recipients

  After the first deploy, confirm the SNS subscription email, otherwise no alerts are delivered.
  The site list lives in s3://<central bucket>/config/sites.json and is managed by admins in the portal;
  sites.json in this folder is only the initial seed and never overwrites an existing list.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][ValidateCount(1, 3)][string[]]$AlertEmail,
  [string]$AwsProfile,
  [string]$Owner = $env:USERNAME,
  [string]$CostCenter = 'IT',
  [string]$Environment = 'prod',
  [int]$FailThreshold = 3
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$cfg = Get-Content sites.json -Raw | ConvertFrom-Json
$centralRegion = $cfg.centralRegion
$stackPrefix = 'webmon'
$tags = @("Project=website-monitor", "Environment=$Environment", "Owner=$Owner", "CostCenter=$CostCenter")

$profileArgs = @()
if ($AwsProfile) { $profileArgs = @('--profile', $AwsProfile) }

function Invoke-Aws {
  & aws @args @profileArgs
  if ($LASTEXITCODE -ne 0) { throw "aws $($args[0..1] -join ' ') failed (exit $LASTEXITCODE)" }
}

Write-Host '==> Building artifacts' -ForegroundColor Cyan
& python build.py
if ($LASTEXITCODE -ne 0) { throw 'build.py failed' }

Write-Host "==> Deploying central stack in $centralRegion" -ForegroundColor Cyan
Invoke-Aws cloudformation deploy `
  --region $centralRegion `
  --stack-name "$stackPrefix-central" `
  --template-file cfn/central.yaml `
  --parameter-overrides "AlertEmail=$($AlertEmail[0])" "AlertEmail2=$(if ($AlertEmail.Count -gt 1) { $AlertEmail[1] })" "AlertEmail3=$(if ($AlertEmail.Count -gt 2) { $AlertEmail[2] })" `
  --no-fail-on-empty-changeset `
  --tags @tags

function Get-CentralOutput([string]$key) {
  $v = & aws cloudformation describe-stacks --region $centralRegion --stack-name "$stackPrefix-central" `
    --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" --output text @profileArgs
  if ($LASTEXITCODE -ne 0 -or -not $v) { throw "Could not read output $key" }
  return $v.Trim()
}
$bucket = Get-CentralOutput 'CentralBucketName'
$topicArn = Get-CentralOutput 'AlertTopicArn'

# Seed the site list only when it does not exist yet - after that the portal's admins own it.
& aws s3api head-object --bucket $bucket --key config/sites.json --region $centralRegion @profileArgs *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "==> Seeding s3://$bucket/config/sites.json" -ForegroundColor Cyan
  Invoke-Aws s3 cp dist/sites-seed.json "s3://$bucket/config/sites.json" --region $centralRegion `
    --content-type 'application/json' --cache-control 'no-cache'
} else {
  Write-Host '==> Site list already exists - leaving it untouched' -ForegroundColor Cyan
}

foreach ($r in $cfg.regions) {
  Write-Host "==> Deploying probe '$($r.key)' in $($r.awsRegion)" -ForegroundColor Cyan
  Invoke-Aws cloudformation deploy `
    --region $r.awsRegion `
    --stack-name "$stackPrefix-probe-$($r.key)" `
    --template-file dist/regional.yaml `
    --parameter-overrides "RegionKey=$($r.key)" "RegionLabel=$($r.label)" "CentralBucketName=$bucket" `
      "CentralRegion=$centralRegion" "AlertTopicArn=$topicArn" "ScheduleMinutes=$($cfg.intervalMinutes)" `
      "FailThreshold=$FailThreshold" `
    --capabilities CAPABILITY_IAM `
    --no-fail-on-empty-changeset `
    --tags @tags
}

Write-Host ''
Write-Host "Central bucket: $bucket" -ForegroundColor Green
Write-Host "Each recipient (up to 3) must confirm the SNS subscription email, otherwise alerts are not delivered: $($AlertEmail -join ', ')" -ForegroundColor Yellow
Write-Host 'First results appear within one check interval and show up in the portal under Monitoring > Website Monitoring.'
