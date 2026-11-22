"""Everything that differs between macOS and Windows lives here, so the rest of Reelmaker doesn't care.

macOS: Apple silicon (M1 or newer). Windows: 10/11, 64-bit. (Linux mostly works too, untested.)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0   # CREATE_NO_WINDOW: no console window pops up for helper processes


# ------------------------------------------------------------------ folders

def data_dir() -> Path:
    """Settings, presets and caches (not the reels themselves)."""
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "Reelmaker"
    if IS_WIN:
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Reelmaker"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "reelmaker"


def default_reels_root() -> Path:
    return Path.home() / ("Movies" if IS_MAC else "Videos") / "Reels"


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WIN else "bin/python")


def venv_bin(name: str) -> Path | None:
    """A tool installed inside Reelmaker's own .venv (e.g. yt-dlp), if there is one."""
    d = Path(sys.prefix) / ("Scripts" if IS_WIN else "bin")
    for n in ((f"{name}.exe", name) if IS_WIN else (name,)):
        if (d / n).exists():
            return d / n
    return None


# ------------------------------------------------------------------ fonts for burned-in captions

def caption_fonts() -> tuple[str, str]:
    """(fonts folder, font name). Arial Black ships with both macOS and Windows."""
    if IS_MAC:
        return "/System/Library/Fonts/Supplemental", "Arial Black"
    if IS_WIN:
        return str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"), "Arial Black"
    return "/usr/share/fonts", "DejaVu Sans"


def ffmpeg_filter_path(p: str) -> str:
    """A path written inside an ffmpeg filter (C:\\Windows\\Fonts -> C\\:/Windows/Fonts)."""
    return p.replace("\\", "/").replace(":", "\\:")


# ------------------------------------------------------------------ external tools

def find_tool(name: str, extra_dirs: tuple = ()) -> str:
    """Path of ffmpeg / ffprobe / yt-dlp / exiftool / ollama on this computer (or just the name if not found)."""
    exe = name + (".exe" if IS_WIN else "")
    for d in extra_dirs:
        if Path(d, exe).exists():
            return str(Path(d, exe))
    if name == "yt-dlp" and (v := venv_bin("yt-dlp")):   # the copy setup installed in .venv (kept up to date)
        return str(v)
    found = shutil.which(name)
    if found:
        return found
    if IS_MAC:   # apps started from Finder may not have Homebrew on PATH
        for d in ("/opt/homebrew/bin", "/usr/local/bin"):
            if Path(d, name).exists():
                return str(Path(d, name))
    if IS_WIN:   # winget puts command-line tools here
        links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / exe
        if links.exists():
            return str(links)
        if name == "ollama":
            p = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
            if p.exists():
                return str(p)
    return name


def ffmpeg_dirs() -> tuple:
    """Folders with an ffmpeg that has libass (needed to burn in captions), best first."""
    if IS_MAC:   # Homebrew's ffmpeg-full is keg-only, so it isn't on PATH
        return tuple(p for p in ("/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin") if Path(p).exists())
    return ()


# ------------------------------------------------------------------ file dialogs, reveal, trash

def pick_file(kind: str) -> str | None:
    """Show the system's Open dialog. kind: "video" or "audio". Returns the chosen path or None."""
    if IS_MAC:
        uti, prompt = ("public.movie", "Choose a video file") if kind == "video" else ("public.audio", "Choose your voiceover audio")
        r = subprocess.run(["osascript", "-e", f'POSIX path of (choose file with prompt "{prompt}" of type {{"{uti}"}})'],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    if IS_WIN:
        filt = ("Videos|*.mp4;*.mov;*.mkv;*.webm;*.m4v;*.avi|All files|*.*" if kind == "video"
                else "Audio|*.wav;*.mp3;*.m4a;*.aac;*.flac;*.ogg|All files|*.*")
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"   # paths with non-English letters survive the trip back
              "Add-Type -AssemblyName System.Windows.Forms;"
              "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"   # keeps the dialog in front of the browser
              f"$d=New-Object System.Windows.Forms.OpenFileDialog;$d.Filter='{filt}';"
              "if($d.ShowDialog($o) -eq 'OK'){[Console]::Out.Write($d.FileName)}")
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        return r.stdout.strip() or None
    return None


def reveal(path: Path | str, select: bool = True) -> None:
    """Show a file (selected) or a folder in Finder / Explorer."""
    p = str(path)
    if IS_MAC:
        subprocess.run(["open", "-R", p] if select else ["open", p])
    elif IS_WIN:
        subprocess.run(["explorer", f"/select,{p}"] if select else ["explorer", p])   # explorer's exit code is meaningless
    else:
        subprocess.run(["xdg-open", str(Path(p).parent if select else p)])


def trash(path: Path) -> None:
    """Move to the Trash / Recycle Bin (recoverable), never delete outright. Raises OSError on failure."""
    if IS_MAC:
        r = subprocess.run(["osascript", "-e", f'tell application "Finder" to delete (POSIX file "{path}" as alias)'],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise OSError(r.stderr.strip()[:200])
        return
    from send2trash import send2trash
    send2trash(str(path))


def clear_file_attributes(path: Path) -> None:
    """macOS extended attributes ("downloaded from", quarantine). Windows/Linux files carry none we care about."""
    if IS_MAC:
        subprocess.run(["xattr", "-c", str(path)], capture_output=True)


def file_attributes(path: Path) -> list[str]:
    if not IS_MAC:
        return []
    r = subprocess.run(["xattr", str(path)], capture_output=True, text=True)
    return [a for a in r.stdout.split() if a != "com.apple.provenance"]   # a local security tag, never uploaded


# ------------------------------------------------------------------ secrets (API keys)

def secret_get(service: str) -> str | None:
    """From the macOS Keychain / Windows Credential Manager."""
    try:
        import keyring
        return keyring.get_password(service, "reelmaker") or None
    except Exception:
        return None


def secret_set(service: str, value: str) -> None:
    import keyring
    keyring.set_password(service, "reelmaker", value)


def secret_delete(service: str) -> None:
    try:
        import keyring
        keyring.delete_password(service, "reelmaker")
    except Exception:
        pass


SECRET_STORE = "your Mac's Keychain" if IS_MAC else "Windows Credential Manager" if IS_WIN else "your system keyring"
