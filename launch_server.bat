@echo off
title ter_go5 - Server
cd /d "%~dp0"

echo Checking for updates from GitHub...
where git >nul 2>&1 && git pull || echo (Git not installed, skipping update)
echo.

echo Starting heist server... (type "help" in this window for commands)
echo.

where py >nul 2>&1 && ( py server.py ) || ( python server.py )

echo.
echo Server stopped.
pause
