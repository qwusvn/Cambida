@echo off
setlocal
set "ROOT=%~dp0"
set "EXE=%ROOT%CCTV_2.1.61.exe"
if not exist "%EXE%" set "EXE=%ROOT%release\2.1.61\CCTV_2.1.61.exe"
if not exist "%EXE%" set "EXE=%ROOT%staging\2.1.61\CCTV_2.1.61.exe"
set "URL=http://127.0.0.1:8004/"

if not exist "%EXE%" (
  echo Khong tim thay CCTV_2.1.61.exe
  pause
  exit /b 1
)

for %%F in ("%EXE%") do set "EXE_DIR=%%~dpF"

call :port_ready
if errorlevel 1 start "Cambida CCTV" /D "%EXE_DIR%" "%EXE%"

for /l %%N in (1,1,30) do (
  call :port_ready
  if not errorlevel 1 goto open_browser
  >nul timeout /t 1 /nobreak
)

echo May chu Cambida chua san sang tai %URL%
pause
exit /b 2

:open_browser
start "" "%URL%"
exit /b 0

:port_ready
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$c=New-Object Net.Sockets.TcpClient; try {$c.Connect('127.0.0.1',8004); exit 0} catch {exit 1} finally {$c.Dispose()}" >nul 2>&1
exit /b %errorlevel%
