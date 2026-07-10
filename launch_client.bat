@echo off
title GTA Heist Sync - Client
:: Relaunch elevated so the panic hotkey works while GTA5 is focused.
net session >nul 2>&1
if not %errorLevel% == 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
echo [OK] Running as Administrator. Starting client...
echo.

:: Prefer the py launcher, fall back to python.
where py >nul 2>&1 && ( py client.py ) || ( python client.py )

echo.
echo Client stopped.
pause
