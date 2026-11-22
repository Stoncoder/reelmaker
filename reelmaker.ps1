# Start Reelmaker on Windows. Easiest: double-click "Start Reelmaker" (the .bat, or the Desktop shortcut setup made).
# Already running? It just opens it (and restarts it first if it's running an older version).
# The Restart button in Reelmaker restarts it right here. Close this window (or Ctrl+C) to stop it.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$port = if ($env:REELMAKER_PORT) { $env:REELMAKER_PORT } else { "8765" }
$url = "http://127.0.0.1:$port"
$vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) {
  Write-Host "Reelmaker isn't set up on this PC yet. Double-click Setup.bat first (see README.md)." -ForegroundColor Yellow
  Read-Host "Press Enter to close"; exit 1
}
function Get-Json($path) { try { return Invoke-RestMethod "$url$path" -TimeoutSec 2 } catch { return $null } }

if (Get-Json "/api/state") {
  $v = Get-Json "/api/ui-version"
  if (-not $v -or -not $v.api -or $v.restart_needed) {
    Write-Host "Reelmaker is running an older version - restarting it..."
    $ok = $false
    try { $r = Invoke-RestMethod -Method Post "$url/api/restart" -ContentType "application/json" -Body '{"force":false}' -TimeoutSec 5; $ok = $r.restarting } catch { }
    if ($ok) {
      for ($i = 0; $i -lt 40; $i++) { Start-Sleep -Milliseconds 500; $n = Get-Json "/api/ui-version"; if ($n -and -not $n.restart_needed) { break } }
      Write-Host "Restarted."
    } else {
      Write-Host "It couldn't restart by itself (it's busy, or it was started another way)."
      Write-Host "Close the window where it runs, then double-click 'Start Reelmaker' again."
    }
  } else { Write-Host "Reelmaker is already running." }
  Start-Process $url; exit 0
}

# the local hook AI: Ollama usually runs in the system tray already; start it if it doesn't
try { Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null } catch {
  $ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
  if (-not $ollama) { $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
  if (Test-Path $ollama) { Write-Host "Starting Ollama (local hook AI)..."; Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden }
}

$env:REELMAKER_LAUNCHER = "1"   # lets the Restart button work: exit code 75 means "start me again"
$env:PYTHONIOENCODING = "utf-8"
$first = $true
while ($true) {
  if ($first) { Write-Host "Reelmaker running at $url  (close this window or press Ctrl+C to stop)" -ForegroundColor Green }
  $p = Start-Process -FilePath $vpy -ArgumentList @("-m", "uvicorn", "app.server:app", "--host", "127.0.0.1", "--port", $port) -NoNewWindow -PassThru
  if ($first) {   # open the browser once the server answers
    for ($i = 0; $i -lt 60 -and -not $p.HasExited; $i++) { Start-Sleep -Milliseconds 500; if (Get-Json "/api/state") { Start-Process $url; break } }
  }
  $p.WaitForExit()
  if ($p.ExitCode -ne 75) { break }
  Write-Host "Restarting Reelmaker (new version loading)..."
  $first = $false
}
Write-Host "Reelmaker stopped."
