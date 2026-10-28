# Compatibility handoff for installed 2.3.x clients. All update work is in updater.exe.
# Kept signature-compatible with the bridge shipped in 2.3.4/2.3.6:
#   -OldPid <pid> -Payload <dir> -Target <dir> [-CleanupRoot <dir>]
param(
    [int]$OldPid = 0,
    [Parameter(Mandatory=$true)][string]$Payload,
    [Parameter(Mandatory=$true)][string]$Target,
    [string]$CleanupRoot = ''
)
$ErrorActionPreference = 'Stop'

# "D:\" must stay a drive root; everything else is stored without a trailing
# separator so parent lookups never produce "D:".
function ConvertTo-NormalizedDirectory([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -match '^[A-Za-z]:\\?$') { return $full.Substring(0, 2) + '\' }
    return $full.TrimEnd('\')
}

# A drive root has no parent: Split-Path/GetDirectoryName return an empty
# string and Join-Path then throws before updater.exe is ever started. Fall
# back to the system temp directory for those installs.
function Get-RunnerParent([string]$Directory) {
    $parent = $null
    try { $parent = [IO.Path]::GetDirectoryName($Directory) } catch { $parent = $null }
    if ($parent -and $parent -notmatch '^[A-Za-z]:$') { return $parent }
    return $null
}

# Encode one argument exactly like CommandLineToArgvW decodes it. A quoted
# path ending in a backslash ("D:\") would otherwise escape the closing quote
# and reach updater.exe as a broken value (argparse exit code 2).
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

$Target = ConvertTo-NormalizedDirectory $Target
$Payload = ConvertTo-NormalizedDirectory $Payload

function Write-BridgeLog([string]$Message) {
    try {
        $folder = Join-Path $Target 'logs'
        if (-not (Test-Path -LiteralPath $folder)) {
            New-Item -ItemType Directory -Path $folder -Force | Out-Null
        }
        $line = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss') + ' [bridge] ' + $Message
        Add-Content -LiteralPath (Join-Path $folder 'updater.log') -Value $line -Encoding UTF8
    } catch { }
}

function Test-InstalledAppRunning {
    $root = $Target.TrimEnd('\') + '\'
    foreach ($name in @('Cambida', 'camhl')) {
        foreach ($process in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
            try {
                if ($process.Path) {
                    $full = [IO.Path]::GetFullPath($process.Path)
                    if ($full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { return $true }
                }
            } catch { }
        }
    }
    return $false
}

function Start-InstalledApp {
    $entry = $null
    $arguments = @()
    $updateFile = Join-Path $Target 'update.json'
    if (Test-Path -LiteralPath $updateFile -PathType Leaf) {
        try {
            $update = Get-Content -LiteralPath $updateFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $relative = [string]$update.start.path
            if ($relative) {
                $candidate = Join-Path $Target ($relative -replace '/', '\')
                if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                    $entry = $candidate
                    if ($update.start.args) {
                        $arguments = @($update.start.args | ForEach-Object { [string]$_ })
                    }
                }
            }
        } catch { }
    }
    if (-not $entry) {
        foreach ($name in @('Cambida.exe', 'camhl.exe')) {
            $candidate = Join-Path $Target $name
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $entry = $candidate
                break
            }
        }
    }
    if (-not $entry) { throw 'Application executable not found' }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $entry
    $startInfo.WorkingDirectory = $Target
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    if ($arguments.Count -gt 0) {
        $startInfo.Arguments = ($arguments | ForEach-Object { ConvertTo-CommandLineArgument $_ }) -join ' '
    }
    [void][Diagnostics.Process]::Start($startInfo)
}

function Restart-InstalledAppIfNeeded {
    # updater.exe may have restarted the application itself; give it a moment
    # before starting a second copy.
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        if (Test-InstalledAppRunning) { return $false }
        Start-Sleep -Milliseconds 500
    }
    Start-InstalledApp
    Start-Sleep -Seconds 2
    if (Test-InstalledAppRunning) { return $true }
    throw 'Application did not start'
}

$runnerDir = $null
$updaterProcess = $null
try {
    $updater = Join-Path $Payload 'updater.exe'
    if (-not (Test-Path -LiteralPath $updater -PathType Leaf)) { throw 'Missing standalone updater.exe in payload' }
    $metadataPath = Join-Path $Payload 'update.json'
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) { throw 'Missing update.json in payload' }
    $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $version = [string]$metadata.version
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Release metadata has no valid version' }

    $runnerParent = Get-RunnerParent $Target
    foreach ($candidate in @($runnerParent, [IO.Path]::GetTempPath())) {
        if (-not $candidate) { continue }
        $path = Join-Path $candidate ('cambida-updater-' + [Guid]::NewGuid().ToString('N'))
        try {
            New-Item -ItemType Directory -Path $path -ErrorAction Stop | Out-Null
            $runnerDir = $path
            break
        } catch {
            Write-BridgeLog ("Cannot create a runner directory in {0}: {1}" -f $candidate, $_.Exception.Message)
        }
    }
    if (-not $runnerDir) { throw 'Cannot create a temporary updater directory' }

    $runner = Join-Path $runnerDir 'updater.exe'
    Copy-Item -LiteralPath $updater -Destination $runner -Force
    Write-BridgeLog ("Starting updater {0} for target {1}" -f $version, $Target)

    $arguments = @(
        '--target', (ConvertTo-CommandLineArgument $Target),
        '--version', $version,
        '--old-pid', ([string]$OldPid),
        '--payload', (ConvertTo-CommandLineArgument $Payload)
    ) -join ' '
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $runner
    $startInfo.Arguments = $arguments
    $startInfo.WorkingDirectory = $runnerDir
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.EnvironmentVariables['TEMP'] = $runnerDir
    $startInfo.EnvironmentVariables['TMP'] = $runnerDir
    $startInfo.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    $process = [Diagnostics.Process]::Start($startInfo)
    $updaterProcess = $process
    if (-not $process.WaitForExit(1800000)) { throw 'Updater did not finish within 30 minutes' }
    if ($process.ExitCode -ne 0) { throw ('Updater exited with code ' + $process.ExitCode) }

    Write-BridgeLog ("Update {0} completed" -f $version)
    # The caller's staging root is mkdtemp("cambida-update-", dir=%TEMP%).
    # Delete only a directory with exactly that shape; never a directory that
    # could contain the installation or unrelated user data.
    if ($CleanupRoot) {
        $cleanup = ConvertTo-NormalizedDirectory $CleanupRoot
        $leaf = Split-Path -Leaf $cleanup
        $cleanupRoot = $cleanup.TrimEnd('\') + '\'
        $installRoot = $Target.TrimEnd('\') + '\'
        $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        $isStaging = $leaf -match '^cambida-update-[0-9A-Za-z_]{6,}$'
        $insideTemp = $cleanupRoot.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)
        $holdsInstall = $installRoot.StartsWith($cleanupRoot, [StringComparison]::OrdinalIgnoreCase)
        if ($isStaging -and $insideTemp -and -not $holdsInstall) {
            try { Remove-Item -LiteralPath $cleanup -Recurse -Force -ErrorAction Stop } catch { }
        }
    }
    try { Remove-Item -LiteralPath $runnerDir -Recurse -Force -ErrorAction Stop } catch { }
    exit 0
}
catch {
    $failure = $_.Exception.Message
    Write-BridgeLog ('Update failed: ' + $failure)
    $stillRunning = $false
    if ($updaterProcess) {
        try { $stillRunning = -not $updaterProcess.HasExited } catch { }
    }
    if ($stillRunning) {
        Write-BridgeLog 'Updater is still running; the application is not restarted by the bridge'
        Write-Error $failure
        exit 1
    }
    $restarted = $false
    try { $restarted = Restart-InstalledAppIfNeeded } catch { Write-BridgeLog ('Restart failed: ' + $_.Exception.Message) }
    if ($restarted) { Write-BridgeLog 'Original application restarted by the bridge' }
    Write-Error $failure
    exit 1
}
