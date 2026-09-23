$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Tìm config.json
$ConfigPath = ""
if (Test-Path "$ScriptDir\config.json") {
    $ConfigPath = "$ScriptDir\config.json"
} elseif (Test-Path "$ScriptDir\..\config.json") {
    $ConfigPath = (Resolve-Path "$ScriptDir\..\config.json").Path
} elseif (Test-Path "D:\1\cambida\config.json") {
    $ConfigPath = "D:\1\cambida\config.json"
}

$port = 8004
if ($ConfigPath -and (Test-Path $ConfigPath)) {
    try {
        $cfg = (Get-Content $ConfigPath -Raw -Encoding UTF8) | ConvertFrom-Json
        if ($cfg.server_port) { $port = $cfg.server_port }
    } catch {}
}

# Tắt tiến trình cũ
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

$CloudflaredExe = "$ScriptDir\cloudflared.exe"
if (-not (Test-Path $CloudflaredExe)) { exit 1 }

$LogPath = "$ScriptDir\cloudflared.log"
if (Test-Path $LogPath) { Remove-Item $LogPath -Force }

$procInfo = New-Object System.Diagnostics.ProcessStartInfo
$procInfo.FileName = $CloudflaredExe
$procInfo.Arguments = "tunnel --url http://127.0.0.1:$port --no-autoupdate"
$procInfo.RedirectStandardError = $true
$procInfo.RedirectStandardOutput = $true
$procInfo.UseShellExecute = $false
$procInfo.CreateNoWindow = $true

$process = New-Object System.Diagnostics.Process
$process.StartInfo = $procInfo

$stdErrHandler = {
    if (-not [String]::IsNullOrEmpty($EventArgs.Data)) {
        Add-Content -Path $Event.MessageData -Value $EventArgs.Data -Encoding UTF8
    }
}
Register-ObjectEvent -InputObject $process -EventName "ErrorDataReceived" -Action $stdErrHandler -MessageData $LogPath | Out-Null
Register-ObjectEvent -InputObject $process -EventName "OutputDataReceived" -Action $stdErrHandler -MessageData $LogPath | Out-Null

$process.Start() | Out-Null
$process.BeginOutputReadLine()
$process.BeginErrorReadLine()

# Bắt URL và cập nhật vào config.json
$tunnelUrl = ""
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $LogPath) {
        $logContent = Get-Content $LogPath -Raw -ErrorAction SilentlyContinue
        if ($logContent -match "https://[-a-zA-Z0-9]+\.trycloudflare\.com") {
            $tunnelUrl = $matches[0]
            break
        }
    }
}

if ($tunnelUrl -and $ConfigPath -and (Test-Path $ConfigPath)) {
    try {
        $config = (Get-Content $ConfigPath -Raw -Encoding UTF8) | ConvertFrom-Json
        $config.public_base_url = $tunnelUrl
        $newJson = $config | ConvertTo-Json -Depth 10
        [System.IO.File]::WriteAllText($ConfigPath, $newJson, [System.Text.Encoding]::UTF8)
    } catch {}
}

# Giữ tiến trình chạy ngầm
$process.WaitForExit()
