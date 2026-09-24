@echo off
setlocal enabledelayedexpansion

:: Updater script cho Cambida CCTV
:: Tham số: %1 = PID của ứng dụng cũ, %2 = Thư mục chứa bản mới đã giải nén, %3 = Thư mục ứng dụng đích

set "OLD_PID=%~1"
set "EXTRACT_DIR=%~2"
set "TARGET_DIR=%~3"

if "%TARGET_DIR%"=="" set "TARGET_DIR=%~dp0"
if "%TARGET_DIR:~-1%"=="\" set "TARGET_DIR=%TARGET_DIR:~0,-1%"

:: 1. Chờ ứng dụng chính cũ thoát hoàn toàn (tối đa 30 giây)
if not "%OLD_PID%"=="" (
    for /l %%i in (1,1,30) do (
        tasklist /fi "PID eq %OLD_PID%" 2>nul | find "%OLD_PID%" >nul
        if errorlevel 1 goto :proc_stopped
        timeout /t 1 /nobreak >nul
    )
)
:proc_stopped
timeout /t 2 /nobreak >nul

:: 2. Xác định thư mục nguồn bên trong extract_dir (phòng trường hợp zip chứa 1 thư mục cha)
set "COPY_SRC=%EXTRACT_DIR%"
for /d %%D in ("%EXTRACT_DIR%\*") do (
    if exist "%%D\1.py" set "COPY_SRC=%%D"
    if exist "%%D\*.exe" set "COPY_SRC=%%D"
)

:: 3. Sao chép và ghi đè file mới
:: TUYỆT ĐỐI KHÔNG GHI ĐÈ: config.json, analytics.db, cctv_videos, logs, .git, .project
robocopy "%COPY_SRC%" "%TARGET_DIR%" /E /R:3 /W:1 /XF config.json analytics.db *.log /XD cctv_videos logs .git .project >nul 2>&1

:: 4. Khởi động lại ứng dụng
cd /d "%TARGET_DIR%"
if exist "%TARGET_DIR%\Chay_CCTV.cmd" (
    start "" "%TARGET_DIR%\Chay_CCTV.cmd"
    set "LAUNCHED=1"
    goto :cleanup
)
for %%F in ("%TARGET_DIR%\CCTV_*.launcher.cmd") do (
    start "" "%%F"
    set "LAUNCHED=1"
    goto :cleanup
)
if "!LAUNCHED!"=="0" (
    for %%F in ("%TARGET_DIR%\CCTV_*.exe") do (
        start "" "%%F"
        set "LAUNCHED=1"
        goto :cleanup
    )
)
if "!LAUNCHED!"=="0" (
    start "" python 1.py
)

:cleanup
timeout /t 3 /nobreak >nul
if not "%EXTRACT_DIR%"=="" (
    rd /s /q "%EXTRACT_DIR%" >nul 2>&1
)
exit /b 0
