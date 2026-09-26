@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0native_updater.ps1" -OldPid "%~1" -Payload "%~2" -Target "%~3"
exit /b %errorlevel%
