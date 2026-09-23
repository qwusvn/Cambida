@echo off
chcp 65001 >nul
echo Đang tắt toàn bộ tiến trình Cloudflared...
taskkill /F /IM cloudflared.exe >nul 2>&1
echo Đã tắt hoàn tất.
pause
