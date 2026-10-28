<#
    Cambida manual repair tool (schema-2 full releases).

    Downloads <version>.zip from GitHub, verifies every file against the
    release manifest, runs the packaged updater.exe directly (bypassing the
    updater.ps1 bridge that broke for installations at a drive root such as
    D:\) and confirms /api/ping before reporting success.

    Runtime data (config.json, database, videos, logs, tokens) is never
    touched: updater.exe only overlays program files listed in the release.
#>
param(
    [string]$InstallDir = '',
    [string]$Version = '',
    [string]$ZipPath = '',
    [string]$Repo = 'qwusvn/Cambida',
    [string]$Token = '',
    [int]$HealthTimeout = 120,
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Write-Step([string]$Message) { Write-Host ('[*] ' + $Message) }
function Write-Ok([string]$Message) { Write-Host ('[+] ' + $Message) -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host ('[!] ' + $Message) -ForegroundColor Yellow }
function Stop-WithError([string]$Message) { Write-Host ('[-] ' + $Message) -ForegroundColor Red; exit 1 }

function ConvertTo-NormalizedDirectory([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -match '^[A-Za-z]:\\?$') { return $full.Substring(0, 2) + '\' }
    return $full.TrimEnd('\')
}

# CommandLineToArgvW-compatible quoting: "D:\" must become "D:\\" or the
# trailing backslash escapes the closing quote (updater exits with code 2).
function ConvertTo-CommandLineArgument([string]$Value) {
    if ([string]::IsNullOrEmpty($Value)) { return '""' }
    if (-not ($Value -match '[\s"]') -and -not $Value.EndsWith('\')) { return $Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * ($backslashes * 2 + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) { [void]$builder.Append(('\' * ($backslashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Get-Sha256([string]$Path) {
    if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $stream.Close()
    }
}

# ZipFile.ExtractToDirectory does not reject every traversal spelling on older
# .NET Framework builds, so extract entry by entry and verify each target path.
function Expand-SafeArchive([string]$Archive, [string]$Destination) {
    $root = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    $bundle = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $bundle.Entries) {
            $name = $entry.FullName
            if (-not $name) { continue }
            if ($name.Contains(':') -or $name.Contains('\') -or $name.StartsWith('/')) {
                throw ('Đường dẫn không hợp lệ trong ZIP: ' + $name)
            }
            $target = [IO.Path]::GetFullPath((Join-Path $Destination ($name.Replace('/', '\'))))
            if (-not $target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
                throw ('Đường dẫn thoát khỏi thư mục giải nén: ' + $name)
            }
            if ($name.EndsWith('/')) {
                if (-not (Test-Path -LiteralPath $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null }
                continue
            }
            $parent = Split-Path -Parent $target
            if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        }
    } finally {
        $bundle.Dispose()
    }
}

function Find-RunningInstall {
    foreach ($name in @('Cambida', 'camhl')) {
        foreach ($process in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
            try {
                if ($process.Path) { return (Split-Path -Parent $process.Path) }
            } catch { }
        }
    }
    return $null
}

function Test-InstallDirectory([string]$Path) {
    foreach ($name in @('Cambida.exe', 'camhl.exe', 'update.json')) {
        if (Test-Path -LiteralPath (Join-Path $Path $name) -PathType Leaf) { return $true }
    }
    return $false
}

function Start-InstalledApp([string]$Root) {
    $entry = $null
    $arguments = @()
    $updateFile = Join-Path $Root 'update.json'
    if (Test-Path -LiteralPath $updateFile -PathType Leaf) {
        try {
            $update = Get-Content -LiteralPath $updateFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $relative = [string]$update.start.path
            if ($relative) {
                $candidate = Join-Path $Root ($relative -replace '/', '\')
                if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                    $entry = $candidate
                    if ($update.start.args) { $arguments = @($update.start.args | ForEach-Object { [string]$_ }) }
                }
            }
        } catch { }
    }
    if (-not $entry) {
        foreach ($name in @('Cambida.exe', 'camhl.exe')) {
            $candidate = Join-Path $Root $name
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $entry = $candidate
                break
            }
        }
    }
    if (-not $entry) { throw 'Không tìm thấy Cambida.exe để khởi động lại.' }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $entry
    $startInfo.WorkingDirectory = $Root
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    if ($arguments.Count -gt 0) {
        $startInfo.Arguments = ($arguments | ForEach-Object { ConvertTo-CommandLineArgument $_ }) -join ' '
    }
    [void][Diagnostics.Process]::Start($startInfo)
}

function Get-ConfiguredPort([string]$Root) {
    $port = 8000
    $configFile = Join-Path $Root 'config.json'
    if (Test-Path -LiteralPath $configFile -PathType Leaf) {
        try {
            $config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $candidate = 0
            if ([int]::TryParse([string]$config.server_port, [ref]$candidate) -and $candidate -ge 1 -and $candidate -le 65535) {
                $port = $candidate
            }
        } catch { }
    }
    return $port
}

function Wait-Health([string]$Root, [int]$TimeoutSeconds = 120) {
    $url = 'http://127.0.0.1:' + (Get-ConfiguredPort $Root) + '/api/ping'
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3 -MaximumRedirection 0
            if ([int]$response.StatusCode -eq 200) { return $url }
        } catch { }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $null
}

function Test-ApplicationRunning([string]$Root) {
    $prefix = $Root.TrimEnd('\') + '\'
    foreach ($name in @('Cambida', 'camhl')) {
        foreach ($process in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
            try {
                if ($process.Path -and [IO.Path]::GetFullPath($process.Path).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                    return $true
                }
            } catch { }
        }
    }
    return $false
}

# ---------------------------------------------------------------- install dir
if (-not $InstallDir -and $ZipPath -and $ZipPath -match '\.zip$') {
    $ZipPath, $InstallDir = $InstallDir, $ZipPath
}
if (-not $InstallDir) {
    $detected = Find-RunningInstall
    if ($detected) {
        $InstallDir = $detected
        Write-Step ('Tự phát hiện thư mục cài đặt: ' + $InstallDir)
    } else {
        $InstallDir = Read-Host 'Nhập thư mục cài Cambida (ví dụ D:\ hoặc D:\Cambida)'
    }
}
if (-not $InstallDir) { Stop-WithError 'Chưa có thư mục cài đặt.' }
$InstallDir = ConvertTo-NormalizedDirectory $InstallDir
if (-not (Test-Path -LiteralPath $InstallDir -PathType Container)) { Stop-WithError ('Không tồn tại thư mục: ' + $InstallDir) }
if (-not (Test-InstallDirectory $InstallDir)) {
    Stop-WithError ('Thư mục không giống bản cài Cambida (thiếu Cambida.exe/update.json): ' + $InstallDir)
}

$logDir = Join-Path $InstallDir 'logs'
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logFile = Join-Path $logDir 'fix-tool.log'
function Write-Log([string]$Message) {
    try {
        $line = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss') + ' [fix-tool] ' + $Message
        Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    } catch { }
}

Write-Host ''
Write-Step ('Thư mục cài đặt: ' + $InstallDir)
$installedVersion = ''
$versionFile = Join-Path $InstallDir 'RELEASE_VERSION.txt'
if (Test-Path -LiteralPath $versionFile -PathType Leaf) {
    $installedVersion = ([string](Get-Content -LiteralPath $versionFile -Raw)).Trim()
}
if ($installedVersion) { Write-Step ('Phiên bản đang cài: ' + $installedVersion) }
Write-Log ('Start: install=' + $InstallDir + ' current=' + $installedVersion + ' dryrun=' + [bool]$DryRun)

# --------------------------------------------------------------- get release
$assetUrl = $null
if ($ZipPath) {
    if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) { Stop-WithError ('Không thấy file ZIP: ' + $ZipPath) }
    Write-Step ('Dùng ZIP có sẵn: ' + $ZipPath)
} else {
    $headers = @{ 'User-Agent' = 'Cambida-FixTool'; 'Accept' = 'application/vnd.github+json' }
    if ($Token) { $headers['Authorization'] = 'Bearer ' + $Token }
    try {
        if ($Version) {
            $release = Invoke-RestMethod -Uri ('https://api.github.com/repos/' + $Repo + '/releases/tags/v' + $Version) -Headers $headers -TimeoutSec 30
        } else {
            $release = Invoke-RestMethod -Uri ('https://api.github.com/repos/' + $Repo + '/releases/latest') -Headers $headers -TimeoutSec 30
        }
    } catch {
        Stop-WithError ('Không truy vấn được GitHub Releases: ' + $_.Exception.Message)
    }
    $releasedVersion = ([string]$release.tag_name) -replace '^[vV]', ''
    if ($release.prerelease) { Write-Warn ('Bản phát hành ' + $releasedVersion + ' đang là prerelease.') }
    if (-not $Version) { $Version = $releasedVersion }
    if ($Version -notmatch '^\d+\.\d+\.\d+$') { Stop-WithError ('Phiên bản không hợp lệ: ' + $Version) }
    $asset = @($release.assets | Where-Object { $_.name -eq ($Version + '.zip') }) | Select-Object -First 1
    if (-not $asset) { Stop-WithError ('Release v' + $Version + ' không có asset ' + $Version + '.zip') }
    $assetUrl = $asset.browser_download_url
}

if ($installedVersion -and $installedVersion -eq $Version -and -not $Force -and -not $DryRun) {
    $healthy = Wait-Health $InstallDir 10
    if ($healthy) {
        Write-Ok ('Máy đã ở phiên bản ' + $Version + ' và /api/ping OK (' + $healthy + '). Không cần sửa.')
        exit 0
    }
    Write-Warn ('Máy báo phiên bản ' + $Version + ' nhưng /api/ping chưa OK; tiếp tục cài lại.')
}

# ------------------------------------------------------------- download/extract
$work = Join-Path $env:TEMP ('cambida-fix-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
$archive = Join-Path $work ($Version + '.zip')
try {
    if ($ZipPath) {
        $archive = [IO.Path]::GetFullPath($ZipPath)
    } else {
        Write-Step ('Đang tải ' + $Version + '.zip ...')
        $attempt = 0
        while ($true) {
            $attempt++
            try {
                $client = New-Object Net.WebClient
                $client.Headers['User-Agent'] = 'Cambida-FixTool'
                if ($Token) { $client.Headers['Authorization'] = 'Bearer ' + $Token }
                $client.DownloadFile($assetUrl, $archive)
                break
            } catch {
                if ($attempt -ge 2) { Stop-WithError ('Tải ZIP thất bại: ' + $_.Exception.Message) }
                Write-Warn 'Tải lỗi, thử lại một lần...'
                Start-Sleep -Seconds 2
            } finally {
                if ($client) { $client.Dispose() }
            }
        }
    }
    $sizeMb = [math]::Round((Get-Item -LiteralPath $archive).Length / 1MB, 1)
    Write-Step ('ZIP: ' + $archive + ' (' + $sizeMb + ' MB)')

    $extract = Join-Path $work 'payload'
    Write-Step 'Đang giải nén...'
    Expand-SafeArchive $archive $extract
    $payload = $extract
    if (-not (Test-Path -LiteralPath (Join-Path $extract 'update.json') -PathType Leaf)) {
        $children = @(Get-ChildItem -LiteralPath $extract -Directory)
        if ($children.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $children[0].FullName 'update.json') -PathType Leaf)) {
            $payload = $children[0].FullName
        }
    }

    # ------------------------------------------------------------- validation
    $manifestFile = Join-Path $payload 'release_manifest.json'
    $updateFile = Join-Path $payload 'update.json'
    if (-not (Test-Path -LiteralPath $manifestFile -PathType Leaf) -or -not (Test-Path -LiteralPath $updateFile -PathType Leaf)) {
        Stop-WithError 'ZIP không phải bản phát hành Cambida 2.3+ (thiếu manifest/update.json).'
    }
    $manifest = Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $update = Get-Content -LiteralPath $updateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 2 -or $update.schema -ne 2 -or $update.kind -ne 'full') {
        Stop-WithError 'Gói cập nhật không đúng schema 2 / kind full.'
    }
    if ($manifest.version -ne $update.version) { Stop-WithError 'Phiên bản trong manifest và update.json không khớp.' }
    if ($Version -and $manifest.version -ne $Version) { Stop-WithError ('ZIP là phiên bản ' + $manifest.version + ', không phải ' + $Version) }
    $Version = [string]$manifest.version
    if (-not (Test-Path -LiteralPath (Join-Path $payload 'updater.exe') -PathType Leaf)) {
        Stop-WithError 'ZIP thiếu updater.exe.'
    }

    $entries = @($manifest.files.PSObject.Properties)
    $manifestNames = @($entries | ForEach-Object { [string]$_.Name })
    if ('updater.exe' -notin $manifestNames) {
        Stop-WithError 'Manifest không liệt kê updater.exe; không thể tin cậy file thực thi này.'
    }
    $entryPoint = [string]$update.start.path
    if (-not $entryPoint -or $entryPoint -notin $manifestNames) {
        Stop-WithError 'update.json thiếu entrypoint hợp lệ trong manifest.'
    }
    Write-Step ('Đang kiểm tra SHA-256 ' + $entries.Count + ' file...')
    $checked = 0
    foreach ($entry in $entries) {
        $checked++
        $relative = ([string]$entry.Name).Replace('/', '\')
        $file = Join-Path $payload $relative
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { Stop-WithError ('Thiếu file trong ZIP: ' + $entry.Name) }
        if ((Get-Sha256 $file) -ne ([string]$entry.Value).ToLowerInvariant()) { Stop-WithError ('Sai SHA-256: ' + $entry.Name) }
        if ($checked % 25 -eq 0) { Write-Step ('  đã kiểm tra ' + $checked + '/' + $entries.Count) }
    }

    # Nothing except the manifest, update.json and the listed files may exist
    # in the payload: updater.exe rejects unlisted files and so do we.
    $payloadRoot = [IO.Path]::GetFullPath($payload).TrimEnd('\') + '\'
    $allowedNames = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($name in $manifestNames) { [void]$allowedNames.Add($name) }
    [void]$allowedNames.Add('release_manifest.json')
    [void]$allowedNames.Add('update.json')
    foreach ($file in @(Get-ChildItem -LiteralPath $payload -Recurse -File -Force)) {
        if (-not $file.FullName.StartsWith($payloadRoot, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-WithError ('File nằm ngoài payload: ' + $file.FullName)
        }
        $relative = $file.FullName.Substring($payloadRoot.Length).Replace('\', '/')
        if (-not $allowedNames.Contains($relative)) { Stop-WithError ('File không có trong manifest: ' + $relative) }
    }
    Write-Ok ('Toàn bộ ' + $entries.Count + ' file khớp manifest (phiên bản ' + $Version + ').')
    Write-Log ('Validated ' + $Version + ' (' + $entries.Count + ' files) from ' + $archive)

    if ($DryRun) {
        Write-Ok 'DryRun: đã tải và xác minh xong, không thay đổi gì trên máy.'
        exit 0
    }

    # ------------------------------------------------------------------ apply
    $oldPid = 0
    $running = @(Get-Process -Name 'Cambida', 'camhl' -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and [IO.Path]::GetFullPath($_.Path).StartsWith($InstallDir.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    })
    if ($running.Count -gt 0) { $oldPid = [int]$running[0].Id }
    Write-Step ('Đang chạy updater.exe --target "' + $InstallDir + '" --version ' + $Version + ' --old-pid ' + $oldPid)

    $arguments = @(
        '--target', (ConvertTo-CommandLineArgument $InstallDir),
        '--version', $Version,
        '--old-pid', ([string]$oldPid),
        '--payload', (ConvertTo-CommandLineArgument $payload)
    ) -join ' '
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = Join-Path $payload 'updater.exe'
    $startInfo.Arguments = $arguments
    $startInfo.WorkingDirectory = $payload
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $process = [Diagnostics.Process]::Start($startInfo)
    if (-not $process.WaitForExit(1800000)) { Stop-WithError 'updater.exe chạy quá 30 phút, dừng theo dõi.' }
    $updaterExit = $process.ExitCode
    Write-Log ('updater.exe exit=' + $updaterExit)
    if ($updaterExit -ne 0) { Write-Warn ('updater.exe trả mã ' + $updaterExit + '; kiểm tra ' + (Join-Path $logDir 'updater.log')) }

    # ---------------------------------------------------------------- health
    Write-Step ('Đang chờ /api/ping trả 200 (tối đa ' + $HealthTimeout + 's)...')
    $healthUrl = Wait-Health $InstallDir $HealthTimeout
    if ($healthUrl) {
        Write-Ok ('/api/ping OK: ' + $healthUrl)
    } else {
        Write-Warn '/api/ping chưa trả 200; đang khởi động lại ứng dụng...'
        if (-not (Test-ApplicationRunning $InstallDir)) {
            try { Start-InstalledApp $InstallDir } catch { Write-Warn ('Không khởi động lại được: ' + $_.Exception.Message) }
        }
        Start-Sleep -Seconds 5
        $retryTimeout = [Math]::Max(10, [int]($HealthTimeout / 2))
        $healthUrl = Wait-Health $InstallDir $retryTimeout
        if (-not $healthUrl) {
            Write-Log 'Health check failed after update; application restart attempted.'
            Stop-WithError 'Cập nhật chưa healthy. Xem logs\updater.log và logs\fix-tool.log để xử lý tiếp.'
        }
        Write-Ok ('/api/ping OK sau khi khởi động lại: ' + $healthUrl)
    }

    $finalVersion = ''
    if (Test-Path -LiteralPath $versionFile -PathType Leaf) { $finalVersion = ([string](Get-Content -LiteralPath $versionFile -Raw)).Trim() }
    $markerVersion = ''
    $installedUpdateFile = Join-Path $InstallDir 'update.json'
    if (Test-Path -LiteralPath $installedUpdateFile -PathType Leaf) {
        try { $markerVersion = ([string](Get-Content -LiteralPath $installedUpdateFile -Raw -Encoding UTF8 | ConvertFrom-Json).version).Trim() } catch { }
    }
    $installedNow = if ($finalVersion) { $finalVersion } else { $markerVersion }
    Write-Log ('Complete: installed=' + $installedNow + ' target=' + $Version + ' updater_exit=' + $updaterExit)
    if ($installedNow -ne $Version) {
        Stop-WithError ('Cập nhật chưa hoàn tất: trên đĩa là ' + ($(if ($installedNow) { $installedNow } else { 'không rõ' })) + ', đích là ' + $Version + '. Xem logs\updater.log.')
    }
    if ($updaterExit -ne 0) {
        Stop-WithError ('updater.exe báo lỗi (mã ' + $updaterExit + ') dù file đã là ' + $installedNow + ' và ping OK. Đọc logs\updater.log trước khi kết luận.')
    }
    Write-Ok ('Hoàn tất. Cambida hiện chạy phiên bản ' + $installedNow + '.')
    Write-Ok ('Xem log: ' + (Join-Path $logDir 'updater.log'))
    exit 0
}
finally {
    $installPrefix = $InstallDir.TrimEnd('\') + '\'
    $workPrefix = ([IO.Path]::GetFullPath($work)).TrimEnd('\') + '\'
    if (-not $installPrefix.StartsWith($workPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        try { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction Stop } catch { }
    }
}
