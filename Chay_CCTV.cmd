@echo off
title Camera Highlight 2.2.3
setlocal enabledelayedexpansion
set "ROOT=%~dp0"
set "URL=http://127.0.0.1:8004/"

echo ========================================================
echo         KHOI DONG HE THONG CAMERA HIGHLIGHT v2.2.3
echo ========================================================

:: 1. Kiem tra va khoi dong camhl.exe neu chua chay
call :port_ready
if errorlevel 1 (
    echo [*] Dang khoi chay may chu camhl.exe...
    if exist "%ROOT%camhl.exe" (
        start "" "%ROOT%camhl.exe"
    ) else if exist "%ROOT%1.py" (
        start "" python "%ROOT%1.py"
    )
) else (
    echo [+] May chu Camera Highlight dang hoat dong san tren cong 8004.
)

:: 2. Doi may chu san sang va tu dong mo trinh duyet
echo [*] Dang ket noi may chu va mo trinh duyet...
for /l %%N in (1,1,30) do (
    call :port_ready
    if not errorlevel 1 goto :open_browser
    >nul timeout /t 1 /nobreak
)

echo [!] May chu chua san sang tai %URL%
if not "%~1"=="--no-pause" pause
exit /b 2

:open_browser
echo [OK] May chu da san sang tai %URL%
if not "%~1"=="--no-browser" (
    start "" "%URL%"
)
exit /b 0

:port_ready
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$c=New-Object Net.Sockets.TcpClient; try {$c.Connect('127.0.0.1',8004); exit 0} catch {exit 1} finally {$c.Dispose()}" >nul 2>&1
exit /b %errorlevel%
