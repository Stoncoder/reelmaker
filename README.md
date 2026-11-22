# Reelmaker

Turns moments from a YouTube stream (or a video file) into ready-to-post 1080×1920 vertical reels:
webcam on top, gameplay below, an opening hook with a voiceover, word-by-word captions, and a checked final export.
Make 10 reels from 10 timestamps in one go — they're cut, captioned and exported automatically.

**Runs on your own computer** — macOS (Apple silicon) and Windows 10/11. The AI parts (speech to text, voices,
hook writing) run locally and free; an optional free online hook writer (Groq) is available.

## What you need

| | Mac | Windows PC |
|---|---|---|
| System | Apple silicon (M1 or newer), recent macOS | Windows 10 or 11, 64-bit |
| Memory | 16 GB recommended (8 GB works, slowly) | 16 GB recommended |
| Disk | about 15 GB free | about 15 GB free |
| GPU | built in — used automatically | an NVIDIA card makes transcription much faster; without one it runs on the CPU |

## Install (once)

**Mac** — open **Terminal** (Spotlight → "Terminal"), paste this line and press Return:

```bash
curl -fsSL https://raw.githubusercontent.com/Stoncoder/reelmaker/main/install.sh | zsh
```

**Windows** — open **PowerShell** (Start menu → "PowerShell"), paste this line and press Enter:

```powershell
irm https://raw.githubusercontent.com/Stoncoder/reelmaker/main/install.ps1 | iex
```

That's it. The installer downloads Reelmaker into `Documents/Reelmaker` (no git or GitHub account needed),
then runs setup, which installs everything Reelmaker needs and asks before anything big. Setup downloads about
9 GB (the AI models) and takes 10–30 minutes the first time. When it's done there's a **Start Reelmaker**
shortcut on your Desktop.

<details><summary>Other ways to install</summary>

- **ZIP**: on GitHub click **Code → Download ZIP**, unzip it somewhere permanent (e.g. `Documents/Reelmaker`),
  then double-click **`Setup.command`** (Mac — if macOS says it can't verify the file, right-click it → **Open**)
  or **`Setup.bat`** (Windows — if it says "Windows protected your PC", click **More info → Run anyway**).
- **git**: `git clone https://github.com/Stoncoder/reelmaker.git` and run `./setup` (Mac) or `Setup.bat` (Windows).
- **Another folder**: set `REELMAKER_DIR` first — Mac: `curl -fsSL …/install.sh | REELMAKER_DIR=~/Apps/Reelmaker zsh`,
  Windows: `$env:REELMAKER_DIR = "D:\Reelmaker"` then the `irm` line.

Setup is safe to run again any time — it only does what's missing.
</details>

What setup installs: Python 3.12, FFmpeg (full build, for captions), Deno, Ollama and ExifTool (via Homebrew on
Mac, winget on Windows), the Python packages in `requirements.txt` (in a private `.venv` folder), and the models:
Whisper large-v3-turbo (speech to text), Kokoro (8 voices), Qwen 2.5 7B (local hook AI), and optionally the
expressive "Hype narrator" voice (Chatterbox, ~3 GB more).

## Start it

Double-click **Start Reelmaker** on your Desktop (or `Start Reelmaker.command` / `Start Reelmaker.bat` in the
folder). A small terminal window opens and Reelmaker opens in your browser at http://127.0.0.1:8765.
Keep that window open while you work; closing it stops Reelmaker.

- Already running? Double-clicking again just opens it.
- **⟳ Restart** (top right in Reelmaker) restarts it.

## Update

When a new version is out, a green **⬆ Update** button appears at the top of Reelmaker (it checks GitHub every
few hours; **Check for updates** at the bottom of the home page checks right now). Click it, read what's new,
confirm — Reelmaker downloads only the changed files, installs any new packages or models, and restarts itself.
Your reels, edits, settings and API keys are kept.

Other ways, same result:
- double-click **`Update.command`** (Mac) or **`Update.bat`** (Windows) in the Reelmaker folder, or
- run the install line above again — on a computer that already has Reelmaker it updates instead.

Installed with `git clone`? Updates are a `git pull` on the `main` branch; the Update button does that for you
when you have no local changes.

## Daily use

1. **Day folder** (top right): pick one or click **+ New day**.
2. **Make reels**: paste a YouTube link or choose a file. Then either:
   - type your moments, one per line, optionally with timestamps: `5:08:45-5:16:30 he finds out about the ending`
     — one reel per line; or
   - choose **Auto-find best clips** and set how many: the app picks the best moments by itself
     (loud reactions, webcam movement, the transcript). Set a search window for long streams.

   Leave **Export final MP4s automatically** on and the finished MP4s appear without any more clicks.
3. Reels are made one after another; the header shows what's running, its progress, CPU/GPU use and download speed.
4. To adjust a reel, open it (home page, or **← Previous / Next reel →**):
   - **Story**: untick parts to drop them; more cut tools under *More cut tools*.
   - **Layout**: drag the blue webcam box and the orange gameplay box. *Webcam top · whole game screen* shows
     the full game (good for menus). **Use this layout for all reels from this video** fixes a whole batch at once.
   - **Hook**: pick or write a hook, preview the voice, save your favourite voice as the default.
     *Hook writer* can switch to the free online AI (Groq) for better hooks.
   - **Captions**: colours and size; drag the captions up or down right on the reel player.

   Click **Update preview**, then **Export final MP4** (or **Export all ready** on the home page).

## Free online hook writer (optional)

The local AI writes hooks for free on your computer. For better and faster hooks, Reelmaker can use
**Groq** (groq.com — not "Grok"), which has a free tier (no card, ~1,000 requests a day):
get a key at <https://console.groq.com/keys>, then in a reel's **Hook** tab choose *Groq online AI* and paste it.
The key is stored in your Mac's Keychain / Windows Credential Manager, never in a file. Using it sends the clip's
description and transcript to Groq. If Groq is unavailable, the local AI takes over automatically.

## Where files go

| | Mac | Windows |
|---|---|---|
| Your reels | `~/Movies/Reels/<day>/reel-01/` | `Videos\Reels\<day>\reel-01\` |
| Settings, layouts, caches | `~/Library/Application Support/Reelmaker` | `%APPDATA%\Reelmaker` |

Inside each reel folder: `project.json` (all edits), `source/` (the fetched footage), `drafts/` (previews),
`FINAL-reel-01.mp4` (the export to upload) and `export-report.txt` (the export checks).

## Export details

- H.264 High, yuv420p, AAC 48 kHz stereo, loudness normalized to about −14 LUFS, fast-start MP4.
- 60 fps sources are exported at a constant 60 fps, everything else at a constant 30 fps.
- Metadata (titles, dates, encoder names, device, location) is removed; the report confirms it.
- Only clip videos you have the rights to use.

## Troubleshooting

- **A YouTube video won't download** — YouTube changes often. Run setup again: it updates the downloader (yt-dlp).
- **A red bar says the server is outdated** — click **Restart now** in it (or **⟳ Restart**).
- **Transcription is slow on Windows** — without an NVIDIA card it runs on the CPU; it still works.
- **Check that everything works**: `./.venv/bin/python -m tests.smoke` (Mac) or
  `.venv\Scripts\python -m tests.smoke` (Windows) renders a test reel and checks every part.

## For developers

- `app/` — the FastAPI server (`server.py`) and the page (`static/`). Everything that differs between macOS and
  Windows is in `app/system.py`; speech to text picks MLX (Apple silicon) or faster-whisper (everything else)
  in `app/analysis.py`; the video encoder is chosen per computer in `app/media.py` (VideoToolbox / NVENC /
  Quick Sync / AMF, else x264).
- `setup` / `setup.ps1` install everything; `reelmaker` / `reelmaker.ps1` start it (exit code 75 = restart).
- Not in the repo (rebuilt by setup): `.venv/`, `.venv-tts/`, `models/`.
- Installing and updating: `install.sh` / `install.ps1` download a commit as a ZIP and record it in
  `.reelmaker-version`; `app/updater.py` compares that with the newest commit on `main`, rewrites only the
  files that changed, then runs `setup --update` / `setup.ps1 -Update` (no questions). Pushing to `main` is
  what ships an update — everyone's Reelmaker shows **⬆ Update** within a few hours.
- `.github/workflows/smoke.yml` runs `tests/smoke.py` on macOS and Windows for every push.

## Credits

Speech to text: OpenAI Whisper (MIT) via MLX / faster-whisper · Voices: Kokoro (Apache-2.0), Chatterbox by
Resemble AI (MIT) · Local hook AI: Qwen 2.5 via Ollama · Video: FFmpeg · Downloads: yt-dlp.
