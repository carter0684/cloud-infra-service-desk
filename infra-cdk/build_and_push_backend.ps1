<#
.SYNOPSIS
  Build the backend container image, push it to ECR (repo: job-portal-backend)
  and optionally roll the ECS service.

.DESCRIPTION
  The ECR repository is created if it does not exist. The ComputeStack imports it
  by name, so create/push BEFORE the first `deploy.ps1 -Action deploy` of
  JobPortalComputeStack (ECS needs the :latest image to become healthy).

.EXAMPLE
  .\build_and_push_backend.ps1 -AccountId 111111111111 -Region us-east-1
  .\build_and_push_backend.ps1 -AccountId 111111111111 -Region us-east-1 -Redeploy
#>
param(
  [Parameter(Mandatory)] [string] $AccountId,
  [string] $Region = 'us-east-1',
  [string] $Profile = '',
  [switch] $Redeploy
)

$ErrorActionPreference = 'Stop'
$repoName = 'job-portal-backend'
$backendDir = Join-Path $PSScriptRoot '..\backend'
$registry = "$AccountId.dkr.ecr.$Region.amazonaws.com"
$profileArgs = @(); if ($Profile) { $profileArgs = @('--profile', $Profile) }

aws ecr describe-repositories --repository-names $repoName --region $Region @profileArgs *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Creating ECR repository $repoName ..."
  aws ecr create-repository --repository-name $repoName --region $Region `
    --image-scanning-configuration scanOnPush=true --encryption-configuration encryptionType=KMS @profileArgs
  if ($LASTEXITCODE -ne 0) { throw 'Could not create the ECR repository.' }
}

aws ecr get-login-password --region $Region @profileArgs | docker login --username AWS --password-stdin $registry
if ($LASTEXITCODE -ne 0) { throw 'ECR login failed.' }

docker build -t "${registry}/${repoName}:latest" $backendDir
if ($LASTEXITCODE -ne 0) { throw 'docker build failed.' }
docker push "${registry}/${repoName}:latest"
if ($LASTEXITCODE -ne 0) { throw 'docker push failed.' }

if ($Redeploy) {
  $svc = aws ecs list-services --cluster job-portal-cluster --region $Region @profileArgs --query 'serviceArns[0]' --output text
  if (-not $svc -or $svc -eq 'None') { throw 'No ECS service found in cluster job-portal-cluster.' }
  aws ecs update-service --cluster job-portal-cluster --service $svc --force-new-deployment --region $Region @profileArgs | Out-Null
  Write-Host "Rolling $svc ..."
  aws ecs wait services-stable --cluster job-portal-cluster --services $svc --region $Region @profileArgs
}
Write-Host 'Done.'
