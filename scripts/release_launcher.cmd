@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0Cambida.exe" (
  echo Khong tim thay Cambida.exe.
  exit /b 1
)
if exist "%~dp0CambidaWatchdog.exe" (
  "%~dp0CambidaWatchdog.exe" --install --target "%~dp0" >nul 2>&1
)
start "" "%~dp0Cambida.exe"
exit /b 0
