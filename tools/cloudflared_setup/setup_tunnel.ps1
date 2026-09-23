[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "Cài đặt Cloudflared Tunnel cho Cambida"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "   BỘ CÀI ĐẶT TỰ ĐỘNG CLOUDFLARED TUNNEL - CAMBIDA CCTV   " -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Tìm tệp config.json
$ConfigPath = ""
if (Test-Path "$ScriptDir\config.json") {
    $ConfigPath = "$ScriptDir\config.json"
} elseif (Test-Path "$ScriptDir\..\config.json") {
    $ConfigPath = (Resolve-Path "$ScriptDir\..\config.json").Path
} elseif (Test-Path "D:\1\cambida\config.json") {
    $ConfigPath = "D:\1\cambida\config.json"
}

if (-not $ConfigPath) {
    Write-Host "[!] Không tìm thấy file config.json ở thư mục hiện tại hoặc thư mục cha." -ForegroundColor Red
    $ConfigPath = Read-Host "Nhập đường dẫn đầy đủ đến file config.json"
    if (-not (Test-Path $ConfigPath)) {
        Write-Host "Đường dẫn không hợp lệ. Đang thoát..." -ForegroundColor Red
        pause
        exit 1
    }
}

Write-Host "[+] Đã tìm thấy config: $ConfigPath" -ForegroundColor Green

# 2. Đọc cấu hình
try {
    $rawJson = Get-Content $ConfigPath -Raw -Encoding UTF8
    $config = $rawJson | ConvertFrom-Json
} catch {
    Write-Host "[!] Lỗi đọc file config.json: $_" -ForegroundColor Red
    pause
    exit 1
}

$port = 8004
if ($config.server_port) {
    $port = $config.server_port
}
Write-Host "[+] Cổng web server Cambida: $port" -ForegroundColor Green

# 3. Kiểm tra cloudflared.exe
$CloudflaredExe = "$ScriptDir\cloudflared.exe"
if (-not (Test-Path $CloudflaredExe)) {
    Write-Host "[!] Không tìm thấy cloudflared.exe trong thư mục $ScriptDir" -ForegroundColor Red
    pause
    exit 1
}

# 4. Tắt cloudflared cũ nếu đang chạy
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

# 5. Khởi động cloudflared tunnel
$LogPath = "$ScriptDir\cloudflared.log"
if (Test-Path $LogPath) { Remove-Item $LogPath -Force }

Write-Host "[*] Đang khởi tạo Cloudflare Quick Tunnel cho cổng $port..." -ForegroundColor Yellow

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

# 6. Đợi bắt URL
$tunnelUrl = ""
Write-Host "[*] Đang đợi Cloudflare cấp tên miền HTTPS..." -NoNewline
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    Write-Host "." -NoNewline
    if (Test-Path $LogPath) {
        $logContent = Get-Content $LogPath -Raw -ErrorAction SilentlyContinue
        if ($logContent -match "https://[-a-zA-Z0-9]+\.trycloudflare\.com") {
            $tunnelUrl = $matches[0]
            break
        }
    }
}
Write-Host ""

if (-not $tunnelUrl) {
    Write-Host "[!] Không bắt được URL Cloudflare sau 30 giây. Vui lòng kiểm tra kết nối mạng hoặc file cloudflared.log" -ForegroundColor Red
    pause
    exit 1
}

Write-Host "==========================================================" -ForegroundColor Green
Write-Host "[THÀNH CÔNG] URL Cloudflare HTTPS đã cấp:" -ForegroundColor Green
Write-Host "   $tunnelUrl" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Green

# 7. Ghi vào config.json
Write-Host "[*] Đang ghi URL vào file config.json..." -ForegroundColor Yellow
$config.public_base_url = $tunnelUrl
$newJson = $config | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText($ConfigPath, $newJson, [System.Text.Encoding]::UTF8)
Write-Host "[OK] Đã cập nhật public_base_url trong config.json thành công!" -ForegroundColor Green

# 8. Hỏi cài đặt khởi động cùng Windows
Write-Host ""
$choice = Read-Host "Bạn có muốn cài đặt để Cloudflared tự chạy ngầm mỗi khi khởi động máy không? (Y/N)"
if ($choice -match "^[yY]") {
    $StartupDir = [Environment]::GetFolderPath("Startup")
    $ShortcutPath = "$StartupDir\Cambida_Cloudflared_Tunnel.lnk"
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = "wscript.exe"
    $Shortcut.Arguments = "`"$ScriptDir\run_tunnel_silent.vbs`""
    $Shortcut.WorkingDirectory = $ScriptDir
    $Shortcut.Description = "Auto start Cloudflared Tunnel for Cambida"
    $Shortcut.Save()
    Write-Host "[OK] Đã tạo shortcut khởi động cùng Windows tại thư mục Startup!" -ForegroundColor Green
}

Write-Host ""
Write-Host "Hoàn tất cài đặt! Tunnel đang chạy ngầm trong máy." -ForegroundColor Green
Write-Host "Khách dùng iPhone khi quét mã QR sẽ tự động chuyển tới link HTTPS để Lưu vào Thư viện Ảnh." -ForegroundColor Yellow
Write-Host "Nhấn phím bất kỳ để đóng cửa sổ này."
pause
