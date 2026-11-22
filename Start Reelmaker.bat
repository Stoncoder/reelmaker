@echo off
rem Double-click to start Reelmaker (or open it, if it is already running). Keep the window open while you use it.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0reelmaker.ps1"
if errorlevel 1 pause
