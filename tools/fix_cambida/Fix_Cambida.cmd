@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
rem Cambida manual repair launcher. All logic lives in Fix_Cambida.ps1.
set "SCRIPT=%~dp0Fix_Cambida.ps1"
if not exist "%SCRIPT%" (
    echo [x] Fix_Cambida.ps1 not found next to this file.
    if "%~1"=="" pause
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" (
    echo [x] Fix tool exited with code %CODE%. Open logs\fix-tool.log inside the Cambida folder.
) else (
    echo [+] Done.
)
if "%~1"=="" pause
exit /b %CODE%
