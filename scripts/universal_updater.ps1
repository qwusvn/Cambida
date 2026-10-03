param(
    [int]$OldPid = 0,
    [Parameter(Mandatory=$true)][string]$Payload,
    [Parameter(Mandatory=$true)][string]$Target,
    [string]$CleanupRoot = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Payload = [IO.Path]::GetFullPath($Payload).TrimEnd('\')
$Target = [IO.Path]::GetFullPath($Target).TrimEnd('\')
$Protected = @(
    'config.json','analytics.db','device_id.key','tunnel_token.txt',
    'controlhub_machine_id.txt','controlhub_client_secret.txt','controlhub_bootstrap.json',
    'logs','cctv_videos','nvr_cache','.git','.project','.updates',
    '.update-backups','.pending_update_notification','native-update.log'
)

function Resolve-SafeFile([string]$Root, [string]$Relative) {
    if (-not $Relative -or $Relative.Contains('\') -or $Relative.Contains(':') -or $Relative.StartsWith('/')) {
        throw "Invalid update path: $Relative"
    }
    foreach ($part in $Relative.Split('/')) {
        $folded = $part.ToLowerInvariant()
        if ($part -in @('', '.', '..') -or $folded -in $Protected -or $part.EndsWith('.') -or $part.EndsWith(' ')) {
            throw "Protected or invalid update path: $Relative"
        }
    }
    $resolved = [IO.Path]::GetFullPath((Join-Path $Root $Relative))
    if (-not $resolved.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Path escapes update root'
    }
    $walk = $resolved
    while ($walk -and $walk.Length -ge $Root.Length) {
        if (Test-Path -LiteralPath $walk) {
            if ((Get-Item -LiteralPath $walk -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Reparse point is not allowed: $walk"
            }
        }
        $walk = Split-Path -Parent $walk
    }
    return $resolved
}

function File-Hash([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Read-Json([string]$Path) {
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-ManifestNames($Manifest) {
    if (-not $Manifest -or -not $Manifest.files) { return @() }
    return @($Manifest.files.PSObject.Properties.Name)
}

function Test-FileUnlocked([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $handle = [IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
    $handle.Dispose()
}

function Start-Release($Update, [string]$Root) {
    $startProp = $Update.PSObject.Properties['start']
    if (-not $startProp -or -not $startProp.Value) {
        throw 'Release start entrypoint is missing'
    }
    $start = $startProp.Value
    $pathProp = $start.PSObject.Properties['path']
    if (-not $pathProp -or -not [string]$pathProp.Value) {
        throw 'Release start entrypoint is missing'
    }
    $entry = Resolve-SafeFile $Root ([string]$pathProp.Value)
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw "Release start entrypoint is missing: $($pathProp.Value)"
    }
    $args = @()
    $argsProp = $start.PSObject.Properties['args']
    if ($argsProp -and $argsProp.Value) {
        $args = @($argsProp.Value | ForEach-Object { [string]$_ })
    }
    return Start-Process -FilePath $entry -ArgumentList $args -WorkingDirectory $Root -WindowStyle Hidden -PassThru
}

function Wait-Health($Update, [string]$Root) {
    $healthProp = $Update.PSObject.Properties['health']
    if (-not $healthProp -or -not $healthProp.Value) { return $true }
    $health = $healthProp.Value
    $urlProp = $health.PSObject.Properties['url']
    if (-not $urlProp -or -not [string]$urlProp.Value) { return $true }
    $url = [string]$urlProp.Value
    $configPath = Join-Path $Root 'config.json'
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        try {
            $runtimeConfig = Read-Json $configPath
            $runtimePort = [int]$runtimeConfig.server_port
            if ($runtimePort -ge 1 -and $runtimePort -le 65535) {
                $uri = [Uri]$url
                if ($uri.Host -in @('127.0.0.1','localhost','::1')) {
                    $builder = [UriBuilder]$uri
                    $builder.Port = $runtimePort
                    $url = $builder.Uri.AbsoluteUri
                }
            }
        } catch {}
    }
    $timeout = 60
    $timeoutProp = $health.PSObject.Properties['timeout_seconds']
    if ($timeoutProp -and $timeoutProp.Value) { $timeout = [Math]::Max(5, [int]$timeoutProp.Value) }
    $deadline = (Get-Date).AddSeconds($timeout)
    do {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3 -MaximumRedirection 0
            if ([int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 400) { return $true }
        } catch {}
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Stop-OwnedProcess([int]$PidToStop) {
    if ($PidToStop -le 0) { return }
    $process = Get-Process -Id $PidToStop -ErrorAction SilentlyContinue
    if (-not $process) { return }
    if (-not $process.WaitForExit(30000)) {
        Stop-Process -Id $PidToStop -Force -ErrorAction Stop
        Wait-Process -Id $PidToStop -Timeout 15 -ErrorAction SilentlyContinue
    }
}

$journal = [Collections.Generic.List[object]]::new()
$backup = $null
$newProcess = $null
$oldUpdate = $null
$oldManifest = $null
$didModify = $false

function Save-BeforeChange([string]$Relative) {
    $dest = Resolve-SafeFile $Target $Relative
    $saved = Join-Path $backup ($Relative.Replace('/', '\'))
    $existed = Test-Path -LiteralPath $dest -PathType Leaf
    if ($existed) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $saved) | Out-Null
        Copy-Item -LiteralPath $dest -Destination $saved -Force
    }
    $journal.Add([pscustomobject]@{Relative=$Relative; Destination=$dest; Backup=$saved; Existed=$existed})
}

try {
    if ($Target -eq $Payload -or $Payload.StartsWith($Target + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Payload must be outside the installed application tree'
    }

    $manifestPath = Join-Path $Payload 'release_manifest.json'
    $updatePath = Join-Path $Payload 'update.json'
    $manifest = Read-Json $manifestPath
    $update = Read-Json $updatePath
    if ($manifest.schema -ne 2 -or $update.schema -ne 2 -or $update.kind -ne 'full') {
        throw 'Unsupported update protocol; Cambida 2.3+ requires schema 2 full releases'
    }
    if ($manifest.version -ne $update.version) { throw 'Release version mismatch' }

    $newNames = Get-ManifestNames $manifest
    if ($newNames.Count -eq 0) { throw 'Release manifest contains no files' }
    $startName = [string]$update.start.path
    if (-not $startName -or $startName -notin $newNames) {
        throw 'Release start entrypoint must be listed in the manifest'
    }

    foreach ($property in $manifest.files.PSObject.Properties) {
        $source = Resolve-SafeFile $Payload $property.Name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Missing release file: $($property.Name)"
        }
        if ((File-Hash $source) -ne [string]$property.Value) {
            throw "Payload checksum mismatch: $($property.Name)"
        }
    }

    $oldManifestPath = Join-Path $Target 'release_manifest.json'
    $oldUpdatePath = Join-Path $Target 'update.json'
    if (Test-Path -LiteralPath $oldManifestPath -PathType Leaf) {
        try { $oldManifest = Read-Json $oldManifestPath } catch {}
    }
    if (Test-Path -LiteralPath $oldUpdatePath -PathType Leaf) {
        try { $oldUpdate = Read-Json $oldUpdatePath } catch {}
    }
    $oldNames = Get-ManifestNames $oldManifest
    $staleNames = @($oldNames | Where-Object { $_ -notin $newNames })

    Stop-OwnedProcess $OldPid

    $touchNames = @($newNames + $staleNames + @('release_manifest.json','update.json') | Select-Object -Unique)
    foreach ($name in $touchNames) {
        Test-FileUnlocked (Resolve-SafeFile $Target $name)
    }

    $backup = Join-Path $Target ('.update-backups\' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $backup | Out-Null

    foreach ($name in $staleNames) {
        Save-BeforeChange $name
        $dest = Resolve-SafeFile $Target $name
        if (Test-Path -LiteralPath $dest -PathType Leaf) {
            Remove-Item -LiteralPath $dest -Force
        }
        $didModify = $true
    }

    foreach ($name in $newNames) {
        Save-BeforeChange $name
        $source = Resolve-SafeFile $Payload $name
        $dest = Resolve-SafeFile $Target $name
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Copy-Item -LiteralPath $source -Destination $dest -Force
        if ((File-Hash $dest) -ne [string]$manifest.files.PSObject.Properties[$name].Value) {
            throw "Copy verification failed: $name"
        }
        $didModify = $true
    }

    foreach ($meta in @('release_manifest.json','update.json')) {
        Save-BeforeChange $meta
        Copy-Item -LiteralPath (Join-Path $Payload $meta) -Destination (Join-Path $Target $meta) -Force
        $didModify = $true
    }

    foreach ($property in $manifest.files.PSObject.Properties) {
        $dest = Resolve-SafeFile $Target $property.Name
        if ((File-Hash $dest) -ne [string]$property.Value) {
            throw "Installed checksum mismatch: $($property.Name)"
        }
    }

    "Installed $($manifest.version); backup: $backup" | Set-Content -LiteralPath (Join-Path $Target 'native-update.log') -Encoding UTF8
    $newProcess = Start-Release $update $Target
    if (-not (Wait-Health $update $Target)) {
        throw "Updated release did not become healthy within $($update.health.timeout_seconds) seconds"
    }

    if ($CleanupRoot) {
        try { Remove-Item -LiteralPath $CleanupRoot -Recurse -Force -ErrorAction Stop } catch {}
    }
    exit 0
}
catch {
    $failure = $_.Exception.Message
    if ($newProcess -and -not $newProcess.HasExited) {
        try { Stop-Process -Id $newProcess.Id -Force -ErrorAction SilentlyContinue } catch {}
    }

    $restoreErrors = [Collections.Generic.List[string]]::new()
    if ($didModify) {
        for ($i = $journal.Count - 1; $i -ge 0; $i--) {
            $entry = $journal[$i]
            try {
                if ($entry.Existed) {
                    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $entry.Destination) | Out-Null
                    Copy-Item -LiteralPath $entry.Backup -Destination $entry.Destination -Force
                } elseif (Test-Path -LiteralPath $entry.Destination) {
                    Remove-Item -LiteralPath $entry.Destination -Force
                }
            } catch {
                $restoreErrors.Add($_.Exception.Message)
            }
        }
    }

    $marker = Join-Path $Target '.pending_update_notification'
    if (Test-Path -LiteralPath $marker) {
        Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
    }
    "Update failed: $failure; rollback errors: $($restoreErrors -join '; '); backup: $backup" |
        Set-Content -LiteralPath (Join-Path $Target 'native-update.log') -Encoding UTF8

    $oldStart = $null
    if ($oldUpdate) { $oldStart = $oldUpdate.PSObject.Properties['start'] }
    if ($didModify -and $restoreErrors.Count -eq 0 -and $oldStart -and $oldStart.Value) {
        try { Start-Release $oldUpdate $Target | Out-Null } catch {}
    }
    Write-Error $failure
    exit 1
}
