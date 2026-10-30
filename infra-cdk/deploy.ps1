<#
.SYNOPSIS
  Synth / diff / deploy the Job Portal CDK stacks.

.EXAMPLE
  .\deploy.ps1 -Action synth
  .\deploy.ps1 -Action diff   -Stacks JobPortalNetworkStack,JobPortalDataStack
  .\deploy.ps1 -Action deploy -Stacks JobPortalNetworkStack,JobPortalDataStack,JobPortalSecurityStack,JobPortalComputeStack
  .\deploy.ps1 -Action deploy -Stacks JobPortalFrontendStack

Uses your normal AWS credentials (AWS_PROFILE / SSO / environment variables).
Deployment settings come from infra-cdk/config.json (copy config.example.json).
Recommended order on a fresh account:
  Network, Data, Security, Compute -> build + push the backend image -> Frontend.
#>
param(
  [ValidateSet('synth', 'diff', 'deploy')] [string] $Action = 'synth',
  [string[]] $Stacks = @(),
  [string] $Profile = ''
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if ($Profile) { $env:AWS_PROFILE = $Profile }
if (-not (Test-Path 'config.json') -and -not $env:PORTAL_CONFIG_FILE) {
  throw 'Missing infra-cdk/config.json. Copy config.example.json to config.json and fill it in.'
}
if (Test-Path '.venv\Scripts\Activate.ps1') { & '.\.venv\Scripts\Activate.ps1' }

switch ($Action) {
  'synth'  { cdk synth @Stacks }
  'diff'   { cdk diff @Stacks }
  'deploy' { cdk deploy @Stacks --require-approval broadening }
}
exit $LASTEXITCODE
