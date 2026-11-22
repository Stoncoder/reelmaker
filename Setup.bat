@echo off
rem Double-click to install everything Reelmaker needs on this PC (safe to run again).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
pause
