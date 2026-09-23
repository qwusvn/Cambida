@echo off
chcp 65001 >nul
title Cai dat Cloudflared Tunnel - Cambida
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_tunnel.ps1"
