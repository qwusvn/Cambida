# Compatibility handoff for installed 2.3.x clients. All update work is in updater.exe.
param(
    [int]$OldPid = 0,
    [Parameter(Mandatory=$true)][string]$Payload,
    [Parameter(Mandatory=$true)][string]$Target,
    [string]$CleanupRoot = ''
)
$ErrorActionPreference = 'Stop'
$Target = [IO.Path]::GetFullPath($Target)
$Payload = [IO.Path]::GetFullPath($Payload)
$updater = Join-Path $Payload 'updater.exe'
if (-not (Test-Path -LiteralPath $updater -PathType Leaf)) { throw 'Missing standalone updater.exe' }
$runnerDir = Join-Path (Split-Path -Parent $Target) ('cambida-updater-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $runnerDir | Out-Null
$runner = Join-Path $runnerDir 'updater.exe'
Copy-Item -LiteralPath $updater -Destination $runner -Force
$env:TEMP = $runnerDir
$env:TMP = $runnerDir
$metadata = Get-Content -LiteralPath (Join-Path $Payload 'update.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$arguments = @('--target', ('"' + $Target + '"'), '--version', ([string]$metadata.version),
               '--old-pid', ([string]$OldPid), '--payload', ('"' + $Payload + '"'))
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $runner
$start.Arguments = $arguments -join ' '
$start.WorkingDirectory = $runnerDir
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$process = [Diagnostics.Process]::Start($start)
$process.WaitForExit()
exit $process.ExitCode
