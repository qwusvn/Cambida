@echo off
setlocal enabledelayedexpansion
set "ROOT=%~dp0"
set "URL=http://127.0.0.1:8004/"

:: 1. Xác định file thực thi CCTV mới nhất
set "EXE="
if exist "%ROOT%RELEASE_VERSION.txt" (
  set /p CUR_VER=<"%ROOT%RELEASE_VERSION.txt"
  set "CUR_VER=!CUR_VER: =!"
  if exist "%ROOT%CCTV_!CUR_VER!.exe" set "EXE=%ROOT%CCTV_!CUR_VER!.exe"
)
if "%EXE%"=="" (
  for /f "delims=" %%F in ('dir /b /o-d "%ROOT%CCTV_*.exe" 2^>nul') do (
    if "%EXE%"=="" set "EXE=%ROOT%%%F"
  )
)
if "%EXE%"=="" (
  if exist "%ROOT%release\2.1.63\CCTV_2.1.63.exe" set "EXE=%ROOT%release\2.1.63\CCTV_2.1.63.exe"
  if exist "%ROOT%staging\2.1.63\CCTV_2.1.63.exe" set "EXE=%ROOT%staging\2.1.63\CCTV_2.1.63.exe"
)

if "%EXE%"=="" (
  echo Khong tim thay file thuc thi CCTV_*.exe
  if not "%~1"=="--no-pause" if not "%~1"=="--headless" pause
  exit /b 1
)

for %%F in ("%EXE%") do set "EXE_DIR=%%~dpF"

call :port_ready
if errorlevel 1 (
  start "Cambida CCTV" /D "%EXE_DIR%" "%EXE%"
)

for /l %%N in (1,1,30) do (
  call :port_ready
  if not errorlevel 1 goto open_browser
  >nul timeout /t 1 /nobreak
)

echo May chu Cambida chua san sang tai %URL%
if not "%~1"=="--no-pause" if not "%~1"=="--headless" pause
exit /b 2

:open_browser
if not "%~1"=="--no-browser" if not "%~1"=="--headless" (
  start "" "%URL%"
)
exit /b 0

:port_ready
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$c=New-Object Net.Sockets.TcpClient; try {$c.Connect('127.0.0.1',8004); exit 0} catch {exit 1} finally {$c.Dispose()}" >nul 2>&1
exit /b %errorlevel%
