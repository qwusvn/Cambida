@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0Cambida.exe" (
  echo Khong tim thay Cambida.exe.
  exit /b 1
)
start "" "%~dp0Cambida.exe"
