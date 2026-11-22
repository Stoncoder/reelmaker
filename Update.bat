@echo off
rem Double-click to update Reelmaker to the newest version on GitHub. Your reels, edits and settings are kept.
rem (Reelmaker also has an Update button at the top of its page when an update is available.)
rem Everything is in one ( ) block: Windows reads it whole, so the update may safely replace this very file.
(
  cd /d "%~dp0"
  if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m app.updater --apply
  ) else (
    echo Reelmaker is not set up yet - running Setup first.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
  )
  echo.
  pause
  exit /b
)
