<#
.SYNOPSIS
  Builds the Cloud Infra Service Desk Android app (Capacitor) -> debug + signed release APK.

.DESCRIPTION
  npm ci -> vite build -> cap sync android -> gradle assembleDebug/assembleRelease
  -> copies outputs to frontend/android-release/ and verifies them (apksigner, aapt2).

  Toolchain is resolved from (in order): $env:JAVA_HOME / $env:ANDROID_HOME, then the
  Android Studio bundled JBR and %LOCALAPPDATA%\Android\Sdk, then C:\android-sdk.
  Nothing is installed machine-wide and no environment variable is persisted.

  Release signing config comes from $env:ANDROID_KEYSTORE_PROPERTIES, else
  C:\android-keys\keystore.properties, else frontend/android/keystore.properties
  (see android/app/build.gradle). The keystore must stay OUTSIDE the repo.

.PARAMETER WorkDir
  Build in a copy of frontend/ at this path (use a non-OneDrive folder, e.g.
  C:\build\job-portal-android) if Gradle trips over OneDrive file placeholders.

.PARAMETER Aab
  Also build an .aab (bundleRelease).

.EXAMPLE
  .\build-android.ps1
  .\build-android.ps1 -WorkDir C:\build\job-portal-android
#>
[CmdletBinding()]
param(
  [string]$WorkDir,
  [switch]$Aab
)

$ErrorActionPreference = 'Stop'
$SourceDir = $PSScriptRoot

function Invoke-Step([string]$Title, [scriptblock]$Block) {
  Write-Host "`n==> $Title" -ForegroundColor Cyan
  & $Block
  if ($LASTEXITCODE -ne 0) { throw "$Title failed (exit code $LASTEXITCODE)" }
}

# ── Toolchain ───────────────────────────────────────────────────────────────
if (-not $env:JAVA_HOME -or -not (Test-Path "$env:JAVA_HOME\bin\java.exe")) {
  $jdk = @(
    'C:\Program Files\Android\Android Studio\jbr',
    (Get-ChildItem 'C:\Program Files\Eclipse Adoptium' -Directory -Filter 'jdk-*' -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty FullName)
  ) | Where-Object { $_ -and (Test-Path "$_\bin\java.exe") } | Select-Object -First 1
  if (-not $jdk) { throw 'JDK 17+ not found. Install Temurin 17+ (winget install EclipseAdoptium.Temurin.21.JDK) or set JAVA_HOME.' }
  $env:JAVA_HOME = $jdk
}
if (-not $env:ANDROID_HOME -or -not (Test-Path $env:ANDROID_HOME)) {
  $sdk = @("$env:LOCALAPPDATA\Android\Sdk", 'C:\android-sdk') | Where-Object { Test-Path "$_\platforms" } | Select-Object -First 1
  if (-not $sdk) { throw 'Android SDK not found. Install the command-line tools outside OneDrive (e.g. C:\android-sdk) or set ANDROID_HOME.' }
  $env:ANDROID_HOME = $sdk
}
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$env:Path = "$env:JAVA_HOME\bin;$env:Path"

$buildTools = Get-ChildItem "$env:ANDROID_HOME\build-tools" -Directory | Sort-Object { [version]($_.Name -replace '[^0-9.]','') } -Descending | Select-Object -First 1
if (-not $buildTools) { throw "No build-tools in $env:ANDROID_HOME" }
$apksigner = Join-Path $buildTools.FullName 'apksigner.bat'
$aapt2 = Join-Path $buildTools.FullName 'aapt2.exe'
Write-Host "JAVA_HOME    = $env:JAVA_HOME"
Write-Host "ANDROID_HOME = $env:ANDROID_HOME (build-tools $($buildTools.Name))"

if (-not $env:ANDROID_KEYSTORE_PROPERTIES) {
  foreach ($c in 'C:\android-keys\keystore.properties', (Join-Path $SourceDir 'android\keystore.properties')) {
    if (Test-Path $c) { $env:ANDROID_KEYSTORE_PROPERTIES = $c; break }
  }
}
if (-not $env:ANDROID_KEYSTORE_PROPERTIES -and -not $env:ANDROID_KEYSTORE_FILE) {
  throw 'No release signing config. Create C:\android-keys\keystore.properties (see README "Android app") or set ANDROID_KEYSTORE_PROPERTIES.'
}

# ── Optional out-of-OneDrive build copy ─────────────────────────────────────
$BuildDir = $SourceDir
if ($WorkDir) {
  Invoke-Step "Mirror frontend/ to $WorkDir" {
    New-Item -ItemType Directory -Force $WorkDir | Out-Null
    robocopy $SourceDir $WorkDir /MIR /XD node_modules dist build .gradle android-release /XF local.properties _out_tmp.txt /NFL /NDL /NJH /NJS /NP | Out-Null
    # robocopy exit codes 0-7 are success
    $global:LASTEXITCODE = if ($LASTEXITCODE -lt 8) { 0 } else { $LASTEXITCODE }
  }
  $BuildDir = $WorkDir
}

Push-Location $BuildDir
try {
  # ── Web bundle + Capacitor sync ───────────────────────────────────────────
  if (Test-Path package-lock.json) { Invoke-Step 'npm ci' { npm ci } } else { Invoke-Step 'npm install' { npm install } }
  Invoke-Step 'vite build' { npm run build }
  Invoke-Step 'cap sync android' { npx cap sync android }

  # ── Gradle ────────────────────────────────────────────────────────────────
  Push-Location android
  try {
    $tasks = @('assembleDebug', 'assembleRelease')
    if ($Aab) { $tasks += 'bundleRelease' }
    Invoke-Step "gradlew $($tasks -join ' ')" { & .\gradlew.bat --no-daemon @tasks }
  } finally { Pop-Location }

  # ── Collect outputs ───────────────────────────────────────────────────────
  $out = Join-Path $SourceDir 'android-release'
  New-Item -ItemType Directory -Force $out | Out-Null
  $apkDir = Join-Path $BuildDir 'android\app\build\outputs\apk'
  $verName = (Select-String -Path (Join-Path $BuildDir 'android\app\build.gradle') -Pattern 'versionName\s+"([^"]+)"').Matches[0].Groups[1].Value
  $debugApk = Join-Path $out "cloud-infra-service-desk-$verName-debug.apk"
  $releaseApk = Join-Path $out "cloud-infra-service-desk-$verName-release.apk"
  Copy-Item "$apkDir\debug\app-debug.apk" $debugApk -Force
  if (-not (Test-Path "$apkDir\release\app-release.apk")) { throw 'app-release.apk missing — release APK was not signed (check keystore.properties).' }
  Copy-Item "$apkDir\release\app-release.apk" $releaseApk -Force
  if ($Aab) { Copy-Item (Join-Path $BuildDir 'android\app\build\outputs\bundle\release\app-release.aab') (Join-Path $out "cloud-infra-service-desk-$verName-release.aab") -Force }

  # ── Verify ────────────────────────────────────────────────────────────────
  Write-Host "`n==> Verify release APK signature" -ForegroundColor Cyan
  & $apksigner verify --verbose --print-certs $releaseApk
  if ($LASTEXITCODE -ne 0) { throw 'apksigner verify failed' }
  Write-Host "`n==> aapt2 badging (release)" -ForegroundColor Cyan
  & $aapt2 dump badging $releaseApk | Select-String -Pattern '^package:|^application-label:|^uses-permission:|^launchable-activity:|^sdkVersion|^targetSdkVersion' | ForEach-Object { $_.Line }

  Write-Host "`n==> Artifacts" -ForegroundColor Green
  Get-ChildItem $out -File | ForEach-Object {
    '{0}  {1,12:N0} bytes  sha256={2}' -f $_.FullName, $_.Length, (Get-FileHash $_.FullName -Algorithm SHA256).Hash
  }
} finally {
  Pop-Location
}
