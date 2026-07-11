@echo off
title ter_go5 - Client
:: Relaunch elevated so the panic hotkey works while GTA5 is focused.
net session >nul 2>&1
if not %errorLevel% == 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

echo Checking for updates from GitHub...
where git >nul 2>&1 && git pull || echo (Git not installed, skipping update)
echo.

echo [OK] Running as Administrator. Starting client...
echo.

:: Prefer the py launcher, fall back to python.
where py >nul 2>&1 && ( py client.py ) || ( python client.py )
if %ERRORLEVEL% equ 99 exit /b

echo.
echo Client stopped.
pause
