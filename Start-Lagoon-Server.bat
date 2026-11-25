@echo off
title Lagoon Open LAN Server
cd /d "%~dp0"
rem Prefer the cross-platform Python server; fall back to the built-in PowerShell one if Python isn't installed.
py -3 --version >nul 2>&1 && (py -3 lagoon-server.py & goto end)
python --version >nul 2>&1 && (python lagoon-server.py & goto end)
echo Python 3 not found - using the built-in Windows PowerShell server instead.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0lagoon-server.ps1"
:end
pause
