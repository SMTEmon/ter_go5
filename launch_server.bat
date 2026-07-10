@echo off
title GTA Heist Sync - Server
cd /d "%~dp0"
echo Starting heist server... (type "help" in this window for commands)
echo.

where py >nul 2>&1 && ( py server.py ) || ( python server.py )

echo.
echo Server stopped.
pause
