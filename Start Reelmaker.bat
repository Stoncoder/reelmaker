@echo off
rem Double-click to start Reelmaker (or open it, if it is already running). Keep the window open while you use it.
rem One ( ) block: Windows reads it whole, so an update may safely replace this file while Reelmaker runs.
(
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0reelmaker.ps1"
  if errorlevel 1 pause
  exit /b
)
