@echo off
setlocal enabledelayedexpansion

:: =============================================================================
:: CAMERA HIGHLIGHT - 1-CLICK LAUNCHER & AUTO-INSTALLER
:: Tu dong cai dat khoi dong cung Windows va chay an toan bo
:: =============================================================================

:: Chay an hoan toan khong hien cua so den CMD
if "%~1"=="--hidden" goto :worker
mshta vbscript:CreateObject("Wscript.Shell").Run("""%~f0"" --hidden %*",0,False)(window.close)&exit /b

:worker
set "ROOT=%~dp0"
set "URL=http://127.0.0.1:8004/"

cd /d "%ROOT%"

:: -----------------------------------------------------------------------------
:: 1. TU DONG CAI DAT KHOI DONG CUNG WINDOWS
:: -----------------------------------------------------------------------------
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "CameraHighlight" /t REG_SZ /d "\"%~f0\" --hidden --no-browser" /f >nul 2>&1

:: Don dep cac khoa registry cu
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "CCTV_System" /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "CambidaCCTV" /f >nul 2>&1

:: -----------------------------------------------------------------------------
:: 2. KHOI DONG SERVER CAMERA HIGHLIGHT (PORT 8004)
:: -----------------------------------------------------------------------------
call :port_ready
if errorlevel 1 (
    set "EXE="
    if exist "%ROOT%camhl.exe" set "EXE=%ROOT%camhl.exe"
    if "!EXE!"=="" if exist "%ROOT%RELEASE_VERSION.txt" (
        set /p CUR_VER=<"%ROOT%RELEASE_VERSION.txt"
        set "CUR_VER=!CUR_VER: =!"
        if exist "%ROOT%CCTV_!CUR_VER!.exe" set "EXE=%ROOT%CCTV_!CUR_VER!.exe"
    )
    if "!EXE!"=="" (
        for /f "delims=" %%F in ('dir /b /o-d "%ROOT%CCTV_*.exe" 2^>nul') do (
            if "!EXE!"=="" set "EXE=%ROOT%%%F"
        )
    )
    if "!EXE!"=="" (
        if exist "%ROOT%1.py" (
            set "EXE=python.exe"
            start "Camera Highlight" /B python "%ROOT%1.py"
        )
    ) else (
        for %%F in ("!EXE!") do set "EXE_DIR=%%~dpF"
        start "Camera Highlight" /D "!EXE_DIR!" "!EXE!"
    )
)

:: -----------------------------------------------------------------------------
:: 3. KHOI DONG CLOUDFLARED TUNNEL CHAY NGAM
:: -----------------------------------------------------------------------------
tasklist /FI "IMAGENAME eq cloudflared.exe" 2>nul | findstr /i "cloudflared.exe" >nul 2>&1
if errorlevel 1 (
    set "CF_BIN="
    if exist "%ROOT%cloudflared.exe" set "CF_BIN=%ROOT%cloudflared.exe"
    if "!CF_BIN!"=="" if exist "%ROOT%cloudflared-windows-amd64.exe" set "CF_BIN=%ROOT%cloudflared-windows-amd64.exe"
    if "!CF_BIN!"=="" if exist "%ROOT%cloudflared_setup\cloudflared.exe" set "CF_BIN=%ROOT%cloudflared_setup\cloudflared.exe"
    if "!CF_BIN!"=="" (
        where cloudflared.exe >nul 2>&1
        if !errorlevel! equ 0 set "CF_BIN=cloudflared.exe"
    )

    if not "!CF_BIN!"=="" (
        set "CF_TOKEN="
        if exist "%ROOT%cloudflared_setup\tunnel_token.txt" (
            set /p CF_TOKEN=<"%ROOT%cloudflared_setup\tunnel_token.txt"
        )
        if "!CF_TOKEN!"=="" if exist "%ROOT%tunnel_token.txt" (
            set /p CF_TOKEN=<"%ROOT%tunnel_token.txt"
        )
        if "!CF_TOKEN!"=="" if exist "%ROOT%config.json" (
            for /f "usebackq delims=" %%T in (`powershell -NoLogo -NoProfile -Command "(Get-Content '%ROOT%config.json' -Raw | ConvertFrom-Json).cloudflare_tunnel.token" 2^>nul`) do (
                set "CF_TOKEN=%%T"
            )
        )

        if not "!CF_TOKEN!"=="" (
            start "" /B "!CF_BIN!" tunnel run --token !CF_TOKEN!
        ) else (
            start "" /B "!CF_BIN!" tunnel --url http://127.0.0.1:8004
        )
    )
)

:: -----------------------------------------------------------------------------
:: 4. CHO MAY CHU SAN SANG & MO TRINH DUYET
:: -----------------------------------------------------------------------------
for /l %%N in (1,1,30) do (
    call :port_ready
    if not errorlevel 1 goto :open_browser
    >nul timeout /t 1 /nobreak
)
exit /b 2

:open_browser
echo %* | findstr /i "\--no-browser \--headless" >nul 2>&1
if errorlevel 1 (
    start "" "%URL%"
)
exit /b 0

:port_ready
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$c=New-Object Net.Sockets.TcpClient; try {$c.Connect('127.0.0.1',8004); exit 0} catch {exit 1} finally {$c.Dispose()}" >nul 2>&1
exit /b %errorlevel%
