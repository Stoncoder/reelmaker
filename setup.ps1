# Reelmaker setup for Windows 10/11 (64-bit). Easiest: double-click Setup.bat
# Installs the tools, the Python environment and the AI models Reelmaker needs. Safe to run again: it skips
# whatever is already done. Options: -Yes (accept the defaults)  -Voice  -NoVoice  -NoShortcut
#   -Update (used by Reelmaker's Update: no questions, keeps your choices, installs only what's new or missing)
param([switch]$Yes, [switch]$Voice, [switch]$NoVoice, [switch]$NoShortcut, [switch]$Update)
if ($Update) { $Yes = $true; $NoShortcut = $true }
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with its progress bar on
Set-Location $PSScriptRoot
$Here = $PSScriptRoot

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor White }
function Ok($t)   { Write-Host "    OK  $t" -ForegroundColor Green }
function Warn($t) { Write-Host "    !   $t" -ForegroundColor Yellow }
function Fail($t) { Write-Host "    X   $t" -ForegroundColor Red; exit 1 }
function Ask($q, $def) {
  if ($Yes) { return $def }
  $hint = if ($def) { "Y/n" } else { "y/N" }
  $r = Read-Host "    $q [$hint]"
  if ([string]::IsNullOrWhiteSpace($r)) { return $def }
  return ($r -match '^[yY]')
}
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Probe($exe, [string[]]$argv) {   # run a program just to check something: never stops setup, returns its output
  # (in Windows PowerShell 5.1, hiding a program's error output while errors stop the script turns any error text
  #  into a crash, so checks run with errors allowed)
  $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"; $global:LASTEXITCODE = 1
  try { $out = & $exe @argv 2>&1 | ForEach-Object { "$_" } } catch { $out = @() }
  $code = $LASTEXITCODE; $ErrorActionPreference = $old
  return [pscustomobject]@{ Code = $code; Out = ($out -join "`n") }
}
function Run($exe, [string[]]$argv) {   # run a program, stop setup if it fails
  & $exe @argv
  if ($LASTEXITCODE -ne 0) { Fail "$exe $($argv -join ' ') failed (exit $LASTEXITCODE)" }
}

# ---------------------------------------------------------------- this PC
Step "Checking this PC"
if (-not [Environment]::Is64BitOperatingSystem) { Fail "Reelmaker needs 64-bit Windows 10 or 11." }
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$drive = (Get-Item $Here).PSDrive.Name
$freeGB = [math]::Round((Get-PSDrive $drive).Free / 1GB)
$gpus = (Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }) -join ", "
Ok "$ramGB GB memory, $freeGB GB free on $($drive):, GPU: $gpus"
if ($freeGB -lt 14) { Warn "Setup downloads about 9 GB (12 GB with the expressive voice). Free up space if it fails." }
if ($gpus -notmatch "NVIDIA") { Warn "No NVIDIA graphics card: transcription runs on the CPU (works, just slower)." }

# ---------------------------------------------------------------- tools (winget)
Step "Tools"
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
  Fail "winget is missing. Install 'App Installer' from the Microsoft Store, then run Setup.bat again."
}
# Gyan.FFmpeg is the 'full' build: it has libass, needed to burn in captions. Deno lets yt-dlp read YouTube.
$tools = @(
  @{ id = "Python.Python.3.12";  name = "Python 3.12" },
  @{ id = "Gyan.FFmpeg";         name = "FFmpeg (full build)" },
  @{ id = "DenoLand.Deno";       name = "Deno" },
  @{ id = "Ollama.Ollama";       name = "Ollama (local hook AI)" },
  @{ id = "OliverBetz.ExifTool"; name = "ExifTool (removes metadata from exports)" }
)
foreach ($t in $tools) {
  if ((Probe "winget" @("list", "--id", $t.id, "-e", "--accept-source-agreements")).Code -eq 0) { Ok "$($t.name)"; continue }
  Write-Host "    Installing $($t.name)..."
  winget install --id $t.id -e --silent --accept-source-agreements --accept-package-agreements
  if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) { Fail "Couldn't install $($t.name) (winget exit $LASTEXITCODE)" }
  Ok "$($t.name)"
}
Refresh-Path

$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
  if ((Probe "py" @("-3.12", "-c", "import sys")).Code -eq 0) { $py = "py"; $pyArgs = @("-3.12") }
}
if (-not $py) {
  $cand = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
  if (Test-Path $cand) { $py = $cand; $pyArgs = @() } else { Fail "Python 3.12 not found. Close this window and run Setup.bat again." }
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { Warn "ffmpeg isn't on PATH yet - if Reelmaker can't find it, sign out and back in." }

# ---------------------------------------------------------------- Python environment
Step "Python environment (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) { Run $py ($pyArgs + @("-m", "venv", ".venv")) }
$vpy = Join-Path $Here ".venv\Scripts\python.exe"
Run $vpy @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
Run $vpy @("-m", "pip", "install", "--quiet", "-r", "requirements.txt")
Run $vpy @("-m", "pip", "install", "--quiet", "--upgrade", "yt-dlp")   # YouTube changes often
Run $vpy @("-c", "import fastapi, faster_whisper, cv2, kokoro_onnx, soundfile, psutil, keyring, send2trash")
Ok "Packages installed"

# ---------------------------------------------------------------- models
Step "AI models (models\)"
New-Item -ItemType Directory -Force -Path models | Out-Null
function Fetch($url, $file, $sha) {
  $out = Join-Path $Here "models\$file"
  if ((Test-Path $out) -and ((Get-FileHash $out -Algorithm SHA256).Hash -eq $sha.ToUpper())) { Ok "$file (already here)"; return }
  Write-Host "    Downloading $file..."
  Invoke-WebRequest -Uri $url -OutFile "$out.part" -UseBasicParsing
  if ((Get-FileHash "$out.part" -Algorithm SHA256).Hash -ne $sha.ToUpper()) {
    Remove-Item "$out.part" -Force; Fail "$file didn't download correctly (checksum mismatch). Run Setup.bat again."
  }
  Move-Item "$out.part" $out -Force; Ok $file
}
$kokoro = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
Fetch "$kokoro/kokoro-v1.0.onnx" "kokoro-v1.0.onnx" "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
Fetch "$kokoro/voices-v1.0.bin" "voices-v1.0.bin" "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"
if (Test-Path "models\faster-whisper-large-v3-turbo\model.bin") {
  Ok "Whisper large-v3-turbo (already here)"
} else {
  Write-Host "    Downloading Whisper large-v3-turbo (speech to text, 1.6 GB)..."
  $env:HF_HUB_DISABLE_XET = "1"
  Run $vpy @("-c", "from huggingface_hub import snapshot_download as d; d('mobiuslabsgmbh/faster-whisper-large-v3-turbo', local_dir='models/faster-whisper-large-v3-turbo')")
  Ok "Whisper large-v3-turbo"
}

# ---------------------------------------------------------------- local hook AI (Ollama)
Step "Local hook AI (Ollama)"
$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) { $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
function OllamaUp { try { Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null; return $true } catch { return $false } }
if (-not (OllamaUp)) {
  Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden
  for ($i = 0; $i -lt 30 -and -not (OllamaUp); $i++) { Start-Sleep -Milliseconds 500 }
}
$have = (Probe $ollama @("list")).Out -match '(?m)^qwen2\.5:7b'
if ($have) { Ok "qwen2.5:7b (already here)" } else {
  Write-Host "    Downloading qwen2.5:7b (writes hooks and picks the story, 4.7 GB)..."
  Run $ollama @("pull", "qwen2.5:7b"); Ok "qwen2.5:7b"
}
if ($ramGB -ge 24) { Write-Host "    Tip: with $ramGB GB memory you can use the bigger, better model:  ollama pull qwen2.5:14b" }

# ---------------------------------------------------------------- optional expressive voice
Step "Expressive voice (optional)"
$tpy = Join-Path $Here ".venv-tts\Scripts\python.exe"
$haveVoice = $false
if (Test-Path $tpy) { $haveVoice = ((Probe $tpy @("-c", "import chatterbox")).Code -eq 0) }
if ($haveVoice) {
  if ($Update) { Run $tpy @("-m", "pip", "install", "--quiet", "-r", "requirements-voice.txt") }
  Ok "Hype narrator voice (already installed)"
} else {
  $want = if ($Voice) { $true } elseif ($NoVoice -or $Update) { $false } else {
    Write-Host "    The 'Hype narrator' voice sounds the most natural for hooks. Without it, Reelmaker uses its 8 built-in voices."
    Write-Host "    (On a PC without an NVIDIA card it runs on the CPU and takes a while per hook.)"
    Ask "Install it? (about 3 GB more)" $false
  }
  if ($want) {
    if (-not (Test-Path $tpy)) { Run $py ($pyArgs + @("-m", "venv", ".venv-tts")) }
    Run $tpy @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
    Run $tpy @("-m", "pip", "install", "--quiet", "-r", "requirements-voice.txt")
    Write-Host "    Downloading the voice model (first time only)..."
    $env:HF_HUB_DISABLE_XET = "1"
    Run $tpy @("-c", "from huggingface_hub import snapshot_download as d; d('ResembleAI/chatterbox', allow_patterns=['ve.safetensors', 't3_cfg.safetensors', 's3gen.safetensors', 'tokenizer.json', 'conds.pt'])")
    Ok "Hype narrator voice"
  } else { Ok "Skipped (run setup.ps1 -Voice any time to add it)" }
}

# ---------------------------------------------------------------- Desktop shortcut
Step "Desktop shortcut"
if (-not $NoShortcut -and (Ask "Put a 'Start Reelmaker' shortcut on your Desktop?" $true)) {
  $desk = [Environment]::GetFolderPath("Desktop")
  $ws = New-Object -ComObject WScript.Shell
  $lnk = $ws.CreateShortcut((Join-Path $desk "Start Reelmaker.lnk"))
  $lnk.TargetPath = Join-Path $Here "Start Reelmaker.bat"
  $lnk.WorkingDirectory = $Here
  $lnk.Description = "Start Reelmaker (or open it if it is already running)"
  $lnk.Save()
  Ok "Desktop > Start Reelmaker"
} else { Ok "Skipped" }

Write-Host ""
Write-Host "Reelmaker is ready." -ForegroundColor Green
Write-Host "Start it with 'Start Reelmaker' on your Desktop (or Start Reelmaker.bat in this folder)."
Write-Host "It opens at http://127.0.0.1:8765 in your browser. Reels are saved in your Videos\Reels folder."
