@echo off
setlocal enabledelayedexpansion

:: Updater script cho Cambida CCTV
:: Tham số: %1 = PID của ứng dụng cũ, %2 = Thư mục chứa bản mới đã giải nén, %3 = Thư mục ứng dụng đích

set "OLD_PID=%~1"
set "EXTRACT_DIR=%~2"
set "TARGET_DIR=%~3"

if "%TARGET_DIR%"=="" set "TARGET_DIR=%~dp0"
if "%TARGET_DIR:~-1%"=="\" set "TARGET_DIR=%TARGET_DIR:~0,-1%"

:: Thiết lập thư mục và file log
set "LOG_DIR=%TARGET_DIR%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\updater.log"

echo ====================================================== >> "%LOG_FILE%"
echo [%DATE% %TIME%] [Updater] Bat dau tien trinh cap nhat... >> "%LOG_FILE%"
echo [%DATE% %TIME%] [Updater] OLD_PID=%OLD_PID% >> "%LOG_FILE%"
echo [%DATE% %TIME%] [Updater] EXTRACT_DIR=%EXTRACT_DIR% >> "%LOG_FILE%"
echo [%DATE% %TIME%] [Updater] TARGET_DIR=%TARGET_DIR% >> "%LOG_FILE%"

:: 1. Chờ ứng dụng chính cũ thoát hoàn toàn (tối đa 30 giây)
if not "%OLD_PID%"=="" (
    echo [%DATE% %TIME%] [Updater] Cho tien trinh cu PID %OLD_PID% thoat... >> "%LOG_FILE%"
    for /l %%i in (1,1,30) do (
        tasklist /fi "PID eq %OLD_PID%" 2>nul | find "%OLD_PID%" >nul
        if errorlevel 1 goto :proc_stopped
        timeout /t 1 /nobreak >nul
    )
    echo [%DATE% %TIME%] [Updater] Cuong buc dung PID %OLD_PID% sau 30s timeout >> "%LOG_FILE%"
    taskkill /f /pid %OLD_PID% >nul 2>&1
)
:proc_stopped
timeout /t 2 /nobreak >nul

:: 1.5. Cưỡng chế dừng mọi tiến trình con hoặc tiến trình ffmpeg/cloudflared có thể khóa file
echo [%DATE% %TIME%] [Updater] Giai phong file lock (ffmpeg, cloudflared, CCTV_*)... >> "%LOG_FILE%"
taskkill /f /im ffmpeg.exe >nul 2>&1
taskkill /f /im cloudflared.exe >nul 2>&1
taskkill /f /fi "IMAGENAME eq CCTV_*" >nul 2>&1

:: 2. Xác định thư mục nguồn bên trong extract_dir (phòng trường hợp zip chứa 1 thư mục cha)
set "COPY_SRC=%EXTRACT_DIR%"
for /d %%D in ("%EXTRACT_DIR%\*") do (
    if exist "%%D\1.py" set "COPY_SRC=%%D"
    if exist "%%D\*.exe" set "COPY_SRC=%%D"
)
echo [%DATE% %TIME%] [Updater] COPY_SRC=%COPY_SRC% >> "%LOG_FILE%"

:: 3. Sao chép và ghi đè file mới
:: TUYỆT ĐỐI KHÔNG GHI ĐÈ: config.json, analytics.db, cctv_videos, logs, .git, .project
:: Nếu cloudflared.exe đã tồn tại trong target_dir thì không ghi đè để tránh conflict với Windows Service
set "EXTRA_XF="
if exist "%TARGET_DIR%\cloudflared.exe" set "EXTRA_XF=cloudflared.exe"

echo [%DATE% %TIME%] [Updater] Dang chay robocopy de ghi de ban moi... >> "%LOG_FILE%"
robocopy "%COPY_SRC%" "%TARGET_DIR%" /E /R:2 /W:1 /XF config.json analytics.db *.log %EXTRA_XF% /XD cctv_videos logs .git .project >> "%LOG_FILE%" 2>&1
set "ROBO_EXIT=%ERRORLEVEL%"
echo [%DATE% %TIME%] [Updater] Robocopy hoan tat voi Exit Code: !ROBO_EXIT! >> "%LOG_FILE%"

:: 3.5. Dọn dẹp launcher và exe cũ để tránh xung đột phiên bản
if exist "%TARGET_DIR%\RELEASE_VERSION.txt" (
    set /p CUR_VER=<"%TARGET_DIR%\RELEASE_VERSION.txt"
    set "CUR_VER=!CUR_VER: =!"
    echo [%DATE% %TIME%] [Updater] Phien ban moi xac dinh: !CUR_VER! >> "%LOG_FILE%"
    for %%F in ("%TARGET_DIR%\CCTV_*.launcher.cmd") do (
        if /i not "%%~nxF"=="CCTV_!CUR_VER!.launcher.cmd" (
            del /f /q "%%F" >nul 2>&1
        )
    )
    for %%F in ("%TARGET_DIR%\CCTV_*.exe") do (
        if /i not "%%~nxF"=="CCTV_!CUR_VER!.exe" (
            del /f /q "%%F" >nul 2>&1
        )
    )
)

:: 4. Khởi động lại ứng dụng
cd /d "%TARGET_DIR%"
set "LAUNCHED=0"
echo [%DATE% %TIME%] [Updater] Khoi dong lai ung dung... >> "%LOG_FILE%"

if exist "%TARGET_DIR%\Chay_CCTV.cmd" (
    echo [%DATE% %TIME%] [Updater] Chay qua Chay_CCTV.cmd --no-pause >> "%LOG_FILE%"
    start "" "%TARGET_DIR%\Chay_CCTV.cmd" --no-pause
    set "LAUNCHED=1"
    goto :cleanup
)
if exist "%TARGET_DIR%\CCTV_!CUR_VER!.launcher.cmd" (
    echo [%DATE% %TIME%] [Updater] Chay qua CCTV_!CUR_VER!.launcher.cmd >> "%LOG_FILE%"
    start "" "%TARGET_DIR%\CCTV_!CUR_VER!.launcher.cmd"
    set "LAUNCHED=1"
    goto :cleanup
)
if exist "%TARGET_DIR%\CCTV_!CUR_VER!.exe" (
    echo [%DATE% %TIME%] [Updater] Chay truc tiep CCTV_!CUR_VER!.exe >> "%LOG_FILE%"
    start "" "%TARGET_DIR%\CCTV_!CUR_VER!.exe"
    set "LAUNCHED=1"
    goto :cleanup
)
for %%F in ("%TARGET_DIR%\CCTV_*.exe") do (
    if "!LAUNCHED!"=="0" (
        echo [%DATE% %TIME%] [Updater] Chay fallback %%F >> "%LOG_FILE%"
        start "" "%%F"
        set "LAUNCHED=1"
        goto :cleanup
    )
)
if "!LAUNCHED!"=="0" (
    echo [%DATE% %TIME%] [Updater] Chay fallback python 1.py >> "%LOG_FILE%"
    start "" python 1.py
)

:cleanup
timeout /t 3 /nobreak >nul
if not "%EXTRACT_DIR%"=="" (
    echo [%DATE% %TIME%] [Updater] Don dep thu muc tam %EXTRACT_DIR% >> "%LOG_FILE%"
    rd /s /q "%EXTRACT_DIR%" >nul 2>&1
)
echo [%DATE% %TIME%] [Updater] Ket thuc updater hoan tat. >> "%LOG_FILE%"
exit /b 0
