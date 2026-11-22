# Reelmaker one-line installer for Windows 10/11. Paste this into PowerShell (Start menu > "PowerShell"):
#
#   irm https://raw.githubusercontent.com/Stoncoder/reelmaker/main/install.ps1 | iex
#
# Downloads Reelmaker into Documents\Reelmaker (no git needed) and runs its setup. If Reelmaker is already
# installed there, it updates it instead. Options (set before running): $env:REELMAKER_DIR = "D:\Reelmaker"
# $env:REELMAKER_REPO = "user/repo"  $env:REELMAKER_BRANCH = "main"  $env:REELMAKER_SKIP_SETUP = "1" (download only)

function Install-Reelmaker {
  $ErrorActionPreference = "Stop"
  $ProgressPreference = "SilentlyContinue"
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  $repo = if ($env:REELMAKER_REPO) { $env:REELMAKER_REPO } else { "Stoncoder/reelmaker" }
  $branch = if ($env:REELMAKER_BRANCH) { $env:REELMAKER_BRANCH } else { "main" }
  $dest = if ($env:REELMAKER_DIR) { $env:REELMAKER_DIR } else { Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Reelmaker" }
  Write-Host "Reelmaker installer  ($repo, $branch -> $dest)" -ForegroundColor White

  if (-not [Environment]::Is64BitOperatingSystem) { Write-Host "Reelmaker needs 64-bit Windows 10 or 11." -ForegroundColor Red; return }

  $overlay = $false
  $vpy = Join-Path $dest ".venv\Scripts\python.exe"
  if ((Test-Path (Join-Path $dest "setup.ps1")) -and (Test-Path (Join-Path $dest "app"))) {
    Write-Host "Reelmaker is already installed in $dest - updating it instead."
    if ((Test-Path $vpy) -and (Test-Path (Join-Path $dest "app\updater.py"))) {
      Push-Location $dest
      try { & $vpy -m app.updater --apply } finally { Pop-Location }
      return
    }
    $overlay = $true   # a copy from before the updater existed (or never set up): download the new version over it
  }
  elseif ((Test-Path $dest) -and (Get-ChildItem $dest -Force | Select-Object -First 1)) {
    Write-Host "$dest already exists and isn't a Reelmaker folder. Move it away (or set `$env:REELMAKER_DIR) and run this again." -ForegroundColor Red
    return
  }

  Write-Host "Finding the latest version..."
  try { $sha = (Invoke-RestMethod "https://api.github.com/repos/$repo/commits/$branch" -UseBasicParsing).sha }
  catch { Write-Host "Couldn't reach GitHub. Check your internet connection and try again." -ForegroundColor Red; return }

  $tmp = Join-Path ([IO.Path]::GetTempPath()) ("reelmaker-" + [guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $tmp | Out-Null
  Write-Host ("Downloading Reelmaker " + $sha.Substring(0, 7) + "...")
  Invoke-WebRequest "https://codeload.github.com/$repo/zip/$sha" -OutFile (Join-Path $tmp "reelmaker.zip") -UseBasicParsing
  Expand-Archive (Join-Path $tmp "reelmaker.zip") -DestinationPath (Join-Path $tmp "x") -Force
  $src = Get-ChildItem (Join-Path $tmp "x") -Directory | Select-Object -First 1
  New-Item -ItemType Directory -Force -Path $dest | Out-Null
  Copy-Item -Path (Join-Path $src.FullName "*") -Destination $dest -Recurse -Force
  Get-ChildItem $src.FullName -Force -Filter ".*" | ForEach-Object { Copy-Item $_.FullName -Destination $dest -Recurse -Force }
  Set-Content -Path (Join-Path $dest ".reelmaker-version") -Value $sha -Encoding ASCII
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  Get-ChildItem $dest -Recurse -File | Unblock-File -ErrorAction SilentlyContinue   # no "downloaded from the internet" warnings
  Write-Host "Downloaded to $dest"

  if ($env:REELMAKER_SKIP_SETUP) { Write-Host "(setup skipped)"; return }
  if ($overlay -and (Test-Path $vpy)) { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "setup.ps1") -Update }
  else { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "setup.ps1") }
}

Install-Reelmaker
