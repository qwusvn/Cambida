param([int]$OldPid, [Parameter(Mandatory=$true)][string]$Payload,
      [Parameter(Mandatory=$true)][string]$Target)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Payload = [IO.Path]::GetFullPath($Payload).TrimEnd('\')
$Target = [IO.Path]::GetFullPath($Target).TrimEnd('\')
$Protected = @('config.json','analytics.db','device_id.key','logs','cctv_videos','nvr_cache','.git','.project','.update-backups')

function Resolve-SafeFile([string]$Root, [string]$Relative) {
    if (-not $Relative -or $Relative.Contains('\') -or $Relative.Contains(':') -or $Relative.StartsWith('/')) {
        throw "Invalid update path: $Relative"
    }
    foreach ($part in $Relative.Split('/')) {
        if ($part -in @('', '.', '..') -or $part -in $Protected -or $part.EndsWith('.') -or $part.EndsWith(' ')) {
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

$journal = [Collections.Generic.List[object]]::new()
$backup = $null
$didStop = $false
try {
    if ($Target -eq $Payload -or $Payload.StartsWith($Target + '\modules\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Payload directory overlaps application modules'
    }
    $manifest = Get-Content -LiteralPath "$Payload\release_manifest.json" -Raw | ConvertFrom-Json
    $update = Get-Content -LiteralPath "$Payload\update.json" -Raw | ConvertFrom-Json
    if ($manifest.schema -ne 1 -or $update.schema -ne 1 -or $update.kind -notin @('full','delta')) { throw 'Unsupported update' }
    if ($manifest.version -ne $update.version) { throw 'Version mismatch' }
    if ($update.kind -eq 'delta') {
        if ((File-Hash "$Target\release_manifest.json") -ne $update.base_manifest_sha256) { throw 'Wrong delta base' }
        $previous = Get-Content -LiteralPath "$Target\release_manifest.json" -Raw | ConvertFrom-Json
        if ($previous.version -ne $update.base_version -or $previous.runtime_id -ne $manifest.runtime_id -or $previous.abi -ne $manifest.abi) {
            throw 'Delta runtime/version mismatch'
        }
    }
    $names = @($manifest.files.PSObject.Properties.Name)
    foreach ($required in @('Cambida.exe','modules/modules.json','RELEASE_VERSION.txt')) {
        if ($required -notin $names) { throw "Missing required release file: $required" }
    }
    $copy = [Collections.Generic.List[string]]::new()
    foreach ($property in $manifest.files.PSObject.Properties) {
        $source = Resolve-SafeFile $Payload $property.Name
        $dest = Resolve-SafeFile $Target $property.Name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            if ((File-Hash $source) -ne $property.Value) { throw "Payload checksum mismatch: $($property.Name)" }
            if (-not (Test-Path -LiteralPath $dest -PathType Leaf) -or (File-Hash $dest) -ne $property.Value) {
                $copy.Add($property.Name)
            }
        } elseif ($update.kind -ne 'delta' -or -not (Test-Path -LiteralPath $dest -PathType Leaf) -or (File-Hash $dest) -ne $property.Value) {
            throw "Missing or mismatched file: $($property.Name)"
        }
    }
    foreach ($file in Get-ChildItem -LiteralPath $Payload -File -Recurse) {
        $relative = $file.FullName.Substring($Payload.Length + 1).Replace('\','/')
        if ($relative -notin $names -and $relative -notin @('release_manifest.json','update.json')) { throw "Unlisted file: $relative" }
    }
    # Only wait for the application that requested this update. Never kill other
    # CCTV, FFmpeg or Cloudflared processes, including services on this machine.
    if ($OldPid -gt 0) {
        $old = Get-Process -Id $OldPid -ErrorAction SilentlyContinue
        if ($old) {
            if (-not $old.WaitForExit(60000)) { throw 'Application did not exit; update cancelled' }
        }
    }
    $didStop = $true
    $backup = Join-Path $Target ('.update-backups\' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    # Check file locks before changing any file; leave active tunnel services alone.
    foreach ($name in $copy) {
        $dest = Resolve-SafeFile $Target $name
        if (Test-Path -LiteralPath $dest -PathType Leaf) {
            $handle = [IO.File]::Open($dest, 'Open', 'ReadWrite', 'None')
            $handle.Dispose()
        }
    }
    # Manifest/version metadata is committed last. A backup remains for recovery.
    $copy = @($copy | Where-Object { $_ -notin @('RELEASE_VERSION.txt','VERSION.txt') }) + @('RELEASE_VERSION.txt','VERSION.txt','release_manifest.json','update.json')
    foreach ($name in $copy) {
        $source = Resolve-SafeFile $Payload $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
        $dest = Resolve-SafeFile $Target $name
        $saved = Join-Path $backup $name
        $existed = Test-Path -LiteralPath $dest -PathType Leaf
        if ($existed) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $saved) | Out-Null
            Copy-Item -LiteralPath $dest -Destination $saved
        }
        $journal.Add([pscustomobject]@{Destination=$dest; Backup=$saved; Existed=$existed})
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Copy-Item -LiteralPath $source -Destination $dest -Force
        if ((File-Hash $source) -ne (File-Hash $dest)) { throw "Copy verification failed: $name" }
    }
    "Installed $($manifest.version); backup: $backup" | Set-Content -LiteralPath "$Target\native-update.log"
    Start-Process -FilePath "$Target\Cambida.exe" -WorkingDirectory $Target -WindowStyle Hidden
    exit 0
} catch {
    $failure = $_.Exception.Message
    $restoreErrors = [Collections.Generic.List[string]]::new()
    for ($i = $journal.Count - 1; $i -ge 0; $i--) {
        $entry = $journal[$i]
        try {
            if ($entry.Existed) { Copy-Item -LiteralPath $entry.Backup -Destination $entry.Destination -Force }
            elseif (Test-Path -LiteralPath $entry.Destination) { Remove-Item -LiteralPath $entry.Destination -Force }
        } catch { $restoreErrors.Add($_.Exception.Message) }
    }
    $marker = Join-Path $Target '.pending_update_notification'
    if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker -Force }
    "Update failed: $failure; rollback errors: $($restoreErrors -join '; '); backup: $backup" | Set-Content -LiteralPath "$Target\native-update.log"
    if ($didStop -and $restoreErrors.Count -eq 0 -and (Test-Path -LiteralPath "$Target\Cambida.exe")) {
        Start-Process -FilePath "$Target\Cambida.exe" -WorkingDirectory $Target -WindowStyle Hidden
    }
    Write-Error $failure
    exit 1
}
