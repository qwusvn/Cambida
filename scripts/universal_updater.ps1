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
    '.update-backups','.pending_update_notification','.watchdog-update.json','native-update.log'
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
    if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $bytes = $sha.ComputeHash($stream)
        return [System.BitConverter]::ToString($bytes).Replace("-", "").ToLowerInvariant()
    } finally {
        $stream.Close()
    }
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
    $deadline = (Get-Date).AddSeconds(5)
    while ($true) {
        try {
            $handle = [IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
            $handle.Dispose()
            return
        } catch {
            if ((Get-Date) -ge $deadline) {
                throw
            }
            Start-Sleep -Milliseconds 250
        }
    }
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
    if ($args.Count -gt 0) {
        return Start-Process -FilePath $entry -ArgumentList $args -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    }
    return Start-Process -FilePath $entry -WorkingDirectory $Root -WindowStyle Hidden -PassThru
}

function Test-HttpEndpoint([string]$ProbeUrl) {
    if (-not $ProbeUrl) { return $false }
    try {
        $request = [System.Net.HttpWebRequest]::Create($ProbeUrl)
        $request.Timeout = 2500
        $request.ReadWriteTimeout = 2500
        $request.AllowAutoRedirect = $true
        $request.MaximumAutomaticRedirections = 5
        $request.Proxy = $null
        $request.UserAgent = 'Cambida-Health/1'
        $response = $request.GetResponse()
        $code = [int]$response.StatusCode
        $response.Close()
        if ($code -ge 200 -and $code -lt 500) { return $true }
    } catch {
        try {
            $ex = $_.Exception
            if ($ex -is [System.Net.WebException] -and $ex.Response) {
                $code = [int]$ex.Response.StatusCode
                $ex.Response.Close()
                if ($code -ge 200 -and $code -lt 500) { return $true }
            }
        } catch {}
    }
    return $false
}

function Test-TcpPort([string]$HostName, [int]$Port) {
    if ($Port -le 0 -or $Port -gt 65535) { return $false }
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $iar = $client.BeginConnect($HostName, $Port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(1000, $false)) {
            $client.EndConnect($iar)
            $client.Close()
            return $true
        }
        $client.Close()
    } catch {}
    return $false
}

function Wait-Health($Update, [string]$Root) {
    $healthProp = $Update.PSObject.Properties['health_v2']
    if (-not $healthProp -or -not $healthProp.Value) {
        $healthProp = $Update.PSObject.Properties['health']
    }
    if (-not $healthProp -or -not $healthProp.Value) { return $true }
    $health = $healthProp.Value
    $modeProp = $health.PSObject.Properties['mode']
    $mode = if ($modeProp -and $modeProp.Value) { [string]$modeProp.Value } else { '' }
    $urlProp = $health.PSObject.Properties['url']
    $url = if ($urlProp -and $urlProp.Value) { [string]$urlProp.Value } else { '' }
    $configPath = Join-Path $Root 'config.json'
    $runtimePort = 0
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        try {
            $runtimeConfig = Read-Json $configPath
            $runtimePort = [int]$runtimeConfig.server_port
            if ($runtimePort -lt 1 -or $runtimePort -gt 65535) { $runtimePort = 0 }
        } catch { $runtimePort = 0 }
    }
    if ($runtimePort -le 0) { $runtimePort = 8000 }

    $urlsToProbe = [Collections.Generic.List[string]]::new()
    if ($mode -eq 'config_port') {
        $pathProp = $health.PSObject.Properties['path']
        $healthPath = if ($pathProp -and $pathProp.Value) { [string]$pathProp.Value } else { '/' }
        if (-not $healthPath.StartsWith('/')) { $healthPath = '/' + $healthPath }
        $url = "http://127.0.0.1:$runtimePort$healthPath"
        $urlsToProbe.Add($url)
        if ($healthPath -ne '/api/ping') {
            $urlsToProbe.Add("http://127.0.0.1:$runtimePort/api/ping")
        }
    } elseif ($url) {
        if ($runtimePort -gt 0) {
            try {
                $uri = [Uri]$url
                if ($uri.Host -in @('127.0.0.1','localhost','::1')) {
                    $builder = [UriBuilder]$uri
                    $builder.Port = $runtimePort
                    $urlsToProbe.Add($builder.Uri.AbsoluteUri)
                } else {
                    $urlsToProbe.Add($url)
                }
            } catch {
                $urlsToProbe.Add($url)
            }
        } else {
            $urlsToProbe.Add($url)
        }
        $urlsToProbe.Add("http://127.0.0.1:$runtimePort/api/ping")
    } else {
        return $true
    }

    $timeout = 90
    $timeoutProp = $health.PSObject.Properties['timeout_seconds']
    if ($timeoutProp -and $timeoutProp.Value) { $timeout = [Math]::Max(10, [int]$timeoutProp.Value) }
    $deadline = (Get-Date).AddSeconds($timeout)
    $tcpOpenCount = 0

    do {
        foreach ($probeUrl in $urlsToProbe) {
            if (Test-HttpEndpoint $probeUrl) { return $true }
            try {
                $response = Invoke-WebRequest -Uri $probeUrl -UseBasicParsing -TimeoutSec 3 -MaximumRedirection 5
                if ([int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 500) { return $true }
            } catch {}
        }
        if (Test-TcpPort '127.0.0.1' $runtimePort) {
            $tcpOpenCount++
            if ($tcpOpenCount -ge 6) { return $true }
        } else {
            $tcpOpenCount = 0
        }
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

function Unix-Now {
    return [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}

function Write-JsonAtomic([string]$Path, $Value) {
    $temp = $Path + '.tmp'
    $json = $Value | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText($temp, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $Path -Force
}

function Install-Watchdog {
    $watchdog = Join-Path $Payload 'CambidaWatchdog.exe'
    if (-not (Test-Path -LiteralPath $watchdog -PathType Leaf)) {
        return
    }
    $targetClean = $Target.Replace('"','')
    $psi = [Diagnostics.ProcessStartInfo]::new($watchdog, "--install `"--target=$targetClean`"")
    $psi.UseShellExecute = $true
    $psi.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $p = [Diagnostics.Process]::Start($psi)
    if (-not $p.WaitForExit(30000)) {
        try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
        throw 'Cambida watchdog installation timed out'
    }
    if ($p.ExitCode -ne 0) {
        throw "Cambida watchdog installation failed with code $($p.ExitCode)"
    }
}

$journal = [Collections.Generic.List[object]]::new()
$backup = $null
$newProcess = $null
$oldUpdate = $null
$oldManifest = $null
$didModify = $false
$oldProcessStopped = $false
$transactionPath = Join-Path $Target '.watchdog-update.json'
$transaction = $null

function Write-Transaction {
    if (-not $script:transaction) { return }
    $items = @()
    foreach ($entry in $script:journal) {
        $items += [ordered]@{
            relative = [string]$entry.Relative
            existed = [bool]$entry.Existed
        }
    }
    $script:transaction['journal'] = $items
    $script:transaction['updated_at'] = Unix-Now
    Write-JsonAtomic $script:transactionPath $script:transaction
}

function Save-BeforeChange([string]$Relative) {
    $dest = Resolve-SafeFile $Target $Relative
    $saved = Join-Path $backup ($Relative.Replace('/', '\'))
    $existed = Test-Path -LiteralPath $dest -PathType Leaf
    if ($existed) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $saved) | Out-Null
        Copy-Item -LiteralPath $dest -Destination $saved -Force
    }
    $journal.Add([pscustomobject]@{Relative=$Relative; Destination=$dest; Backup=$saved; Existed=$existed})
    Write-Transaction
}

function Clean-DiskArtifacts([string]$TargetRoot, [string]$Cleanup) {
    if ($Cleanup -and (Test-Path -LiteralPath $Cleanup)) {
        try { Remove-Item -LiteralPath $Cleanup -Recurse -Force -ErrorAction SilentlyContinue } catch {}
    }
    try {
        $now = Get-Date
        Get-ChildItem -Path $env:TEMP -Directory -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -like 'cambida-update-*' -or $_.Name -like 'cambida-updater-*'
        } | ForEach-Object {
            if (($now - $_.LastWriteTime).TotalMinutes -gt 15) {
                try { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue } catch {}
            }
        }
    } catch {}

    try {
        Get-ChildItem -Path $env:TEMP -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue | ForEach-Object {
            try { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction Stop } catch {}
        }
    } catch {}

    if ($TargetRoot -and (Test-Path -LiteralPath (Join-Path $TargetRoot '.update-backups'))) {
        try {
            $backups = @(Get-ChildItem -Path (Join-Path $TargetRoot '.update-backups') -Directory -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending)
            if ($backups.Count -gt 1) {
                $backups | Select-Object -Skip 1 | ForEach-Object {
                    try { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue } catch {}
                }
            }
        } catch {}
    }
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

    Clean-DiskArtifacts $Target ''
    $backupId = [Guid]::NewGuid().ToString('N')
    $backupRel = '.update-backups/' + $backupId
    $backup = Join-Path $Target ('.update-backups\' + $backupId)
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    $now = Unix-Now
    $transaction = [ordered]@{
        schema = 1
        stage = 'applying'
        new_version = [string]$manifest.version
        old_version = if ($oldManifest) { [string]$oldManifest.version } else { '' }
        backup_rel = $backupRel
        updater_pid = $PID
        watchdog_failures = 0
        started_at = $now
        updated_at = $now
        journal = @()
    }
    Write-Transaction

    Stop-OwnedProcess $OldPid
    if ($OldPid -gt 0) { $oldProcessStopped = $true }

    $targetExe = (Join-Path $Target 'Cambida.exe').ToLowerInvariant()
    $targetWatchdog = (Join-Path $Target 'CambidaWatchdog.exe').ToLowerInvariant()
    Get-Process | Where-Object {
        try {
            if (-not $_.Path) { return $false }
            $p = $_.Path.ToLowerInvariant()
            return ($p -eq $targetExe -or $p -eq $targetWatchdog)
        } catch { $false }
    } | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }


    $touchNames = @($newNames + $staleNames + @('release_manifest.json','update.json') | Select-Object -Unique)
    foreach ($name in $touchNames) {
        Test-FileUnlocked (Resolve-SafeFile $Target $name)
    }


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
    Install-Watchdog
    $transaction['stage'] = 'starting'
    Write-Transaction
    $newProcess = Start-Release $update $Target
    $transaction['new_pid'] = $newProcess.Id
    Write-Transaction
    if (-not (Wait-Health $update $Target)) {
        throw "Updated release did not become healthy within the configured timeout"
    }
    $transaction['stage'] = 'monitoring'
    $transaction['watchdog_failures'] = 0
    $transaction['stabilize_seconds'] = 120
    Write-Transaction

    Clean-DiskArtifacts $Target $CleanupRoot
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

    if ($transaction) {
        $transaction['failure'] = $failure
        $transaction['stage'] = if ($restoreErrors.Count -eq 0) { 'rolled_back' } else { 'rollback_failed' }
        try { Write-Transaction } catch {}
    }

    $oldStart = $null
    if ($oldUpdate) { $oldStart = $oldUpdate.PSObject.Properties['start'] }
    if (($didModify -or $oldProcessStopped) -and $restoreErrors.Count -eq 0 -and $oldStart -and $oldStart.Value) {
        try { Start-Release $oldUpdate $Target | Out-Null } catch {}
    }
    Clean-DiskArtifacts $Target $CleanupRoot
    Write-Error $failure
    exit 1
}
