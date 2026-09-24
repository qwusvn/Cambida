$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Version = "2.1.52"
$Name = "CCTV_$Version"
$Spec = Join-Path $Root "$Name.spec"
$StageParent = Join-Path $Root "staging"
$Stage = Join-Path $StageParent $Version
$ReleaseParent = Join-Path $Root "release"
$Release = Join-Path $ReleaseParent $Version
$Dist = Join-Path $StageParent ".pyinstaller-$Version-dist"
$Work = Join-Path $StageParent ".pyinstaller-$Version-work"

foreach ($path in @($Stage, $Dist, $Work)) {
    if (Test-Path -LiteralPath $path) {
        Write-Warning "Removing pre-existing task output: $path"
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path $StageParent | Out-Null
New-Item -ItemType Directory -Force -Path $ReleaseParent | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root "config.release.json"))) {
    throw "Missing safe release seed config.release.json"
}
if (Test-Path -LiteralPath (Join-Path $Root "staging\$Version\config.json")) {
    throw "Refusing to package operational config.json"
}

Push-Location $Root
try {
    Write-Host "[1/6] Running PyInstaller for $Name..." -ForegroundColor Cyan
    & python -m PyInstaller --distpath $Dist --workpath $Work $Spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    $Built = Join-Path $Dist $Name
    if (-not (Test-Path -LiteralPath (Join-Path $Built "$Name.exe"))) {
        throw "Expected onedir executable not found: $Built"
    }

    Write-Host "[2/6] Staging build artifacts & Cloudflared..." -ForegroundColor Cyan
    Copy-Item -LiteralPath $Built -Destination $Stage -Recurse

    $Sidecars = @(
        "index.html",
        "admin.html",
        "admin_login.html",
        "home.html",
        "live_all.html",
        "stats.html",
        "timeline.html",
        "ffmpeg.exe",
        "cloudflared.exe",
        "updater.cmd",
        "RELEASE_VERSION.txt",
        "$Name.launcher.cmd"
    )
    foreach ($file in $Sidecars) {
        $src = Join-Path $Root $file
        if (-not (Test-Path -LiteralPath $src)) {
            throw "Missing required release sidecar: $file"
        }
        Copy-Item -LiteralPath $src -Destination (Join-Path $Stage $file)
    }

    $CloudflaredSetupSrc = Join-Path $Root "tools\cloudflared_setup"
    if (Test-Path -LiteralPath $CloudflaredSetupSrc) {
        Copy-Item -LiteralPath $CloudflaredSetupSrc -Destination (Join-Path $Stage "cloudflared_setup") -Recurse -Force
    }

    Copy-Item -LiteralPath (Join-Path $Root "$Name.launcher.cmd") -Destination (Join-Path $Stage "Chay_CCTV.cmd")
    Copy-Item -LiteralPath (Join-Path $Root "RELEASE_VERSION.txt") -Destination (Join-Path $Stage "VERSION.txt")

    $head = (git rev-parse HEAD).Trim()
    $branch = (git branch --show-current).Trim()
    $status = (git status --short) -join [Environment]::NewLine
    $hashFiles = @("1.py","index.html","admin.html","home.html","$Name.spec","config.release.json","RELEASE_VERSION.txt","updater.cmd","cloudflared.exe")
    $hashLines = foreach ($file in $hashFiles) {
        $p = Join-Path $Root $file
        if (Test-Path -LiteralPath $p) {
            $h = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash
            "$file SHA256=$h"
        }
    }
    @"
Cambida CCTV release staging
Version: $Version
Built from branch: $branch
Git HEAD: $head
Working tree was dirty at packaging time: $([bool]$status)
Working-tree paths at packaging time:
$status

Source hashes:
$($hashLines -join [Environment]::NewLine)

Tests: NOT RUN - not requested.
This package intentionally contains no operational config.json.
On first run only, if config.json is absent beside the EXE, the app seeds it from _internal\config.release.json.
Existing config.json beside the EXE is never overwritten by application startup.
"@ | Set-Content -LiteralPath (Join-Path $Stage "BUILD_INFO.txt") -Encoding UTF8

    @"
CAMBIDA CCTV $Version
- This is a staging onedir build with Cloudflared included.
- No production config, database, logs, media, or cache are bundled.
- First run with no config.json creates a safe config.json from the embedded release template.
- The seed has no cameras, no Telegram/GitHub token, and no usable admin password.
- Configure your own admin password and cameras before operational use.
- If config.json already exists beside the EXE, startup preserves it unchanged.
"@ | Set-Content -LiteralPath (Join-Path $Stage "README_FIRST_RUN.txt") -Encoding UTF8

    Write-Host "[3/6] Enforcing Zero Config Pollution..." -ForegroundColor Cyan
    $Forbidden = @("config.json","analytics.db","cctv_videos","logs","nvr_cache")
    foreach ($forbiddenName in $Forbidden) {
        if (Test-Path -LiteralPath (Join-Path $Stage $forbiddenName)) {
            throw "Forbidden runtime/production item found in staging root: $forbiddenName"
        }
    }
    $Required = @(
        "$Name.exe","index.html","admin.html","admin_login.html","home.html",
        "live_all.html","stats.html","timeline.html","ffmpeg.exe","cloudflared.exe","updater.cmd",
        "RELEASE_VERSION.txt","VERSION.txt","$Name.launcher.cmd","Chay_CCTV.cmd",
        "_internal\config.release.json","_internal\vendor\dahua_netsdk\dhnetsdk.dll",
        "cloudflared_setup\setup_tunnel.ps1"
    )
    foreach ($rel in $Required) {
        if (-not (Test-Path -LiteralPath (Join-Path $Stage $rel))) {
            throw "Required release item missing: $rel"
        }
    }

    Write-Host "[4/6] Copying to release directory: $Release..." -ForegroundColor Cyan
    if (Test-Path -LiteralPath $Release) {
        Remove-Item -LiteralPath $Release -Recurse -Force
    }
    Copy-Item -LiteralPath $Stage -Destination $Release -Recurse

    Write-Host "[5/6] Creating ZIP archive for $Version..." -ForegroundColor Cyan
    $ZipRoot = Join-Path $ReleaseParent "$Version.zip"
    if (Test-Path -LiteralPath $ZipRoot) {
        Remove-Item -LiteralPath $ZipRoot -Force
    }
    Compress-Archive -Path "$Stage\*" -DestinationPath $ZipRoot -CompressionLevel Optimal

    Copy-Item -LiteralPath $ZipRoot -Destination (Join-Path $Release "$Version.zip") -Force
    Copy-Item -LiteralPath $ZipRoot -Destination (Join-Path $Stage "$Version.zip") -Force

    Write-Host "[6/6] Packaging completed successfully!" -ForegroundColor Green

    $ExeHash = (Get-FileHash -LiteralPath (Join-Path $Release "$Name.exe") -Algorithm SHA256).Hash
    $ZipHash = (Get-FileHash -LiteralPath $ZipRoot -Algorithm SHA256).Hash

    Write-Output "RELEASE_FOLDER=$Release"
    Write-Output "RELEASE_ZIP=$ZipRoot"
    Write-Output "EXE_SHA256=$ExeHash"
    Write-Output "ZIP_SHA256=$ZipHash"
}
finally {
    Pop-Location
    if (Test-Path -LiteralPath $Dist) { Remove-Item -LiteralPath $Dist -Recurse -Force }
    if (Test-Path -LiteralPath $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }
}
