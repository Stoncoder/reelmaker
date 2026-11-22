"""Paths, settings and small shared helpers."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path

from . import system

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
MODELS_DIR = ROOT_DIR / "models"
DATA_DIR = Path(os.environ.get("REELMAKER_DATA", system.data_dir()))
SETTINGS_FILE = DATA_DIR / "settings.json"
PRESETS_FILE = DATA_DIR / "presets.json"

DEFAULT_REELS_ROOT = Path(os.environ.get("REELMAKER_ROOT", system.default_reels_root()))

OUT_W, OUT_H = 1080, 1920
HALF_H = OUT_H // 2

FONTS_DIR, CAPTION_FONT = system.caption_fonts()

# speech to text: MLX Whisper on Apple silicon Macs, faster-whisper (CTranslate2) everywhere else — same model
_LOCAL_WHISPER = MODELS_DIR / "whisper-large-v3-turbo"
WHISPER_MODEL = str(_LOCAL_WHISPER) if (_LOCAL_WHISPER / "weights.safetensors").exists() else "mlx-community/whisper-large-v3-turbo"
_LOCAL_FW = MODELS_DIR / "faster-whisper-large-v3-turbo"
FW_MODEL = os.environ.get("REELMAKER_FW_MODEL") or (str(_LOCAL_FW) if (_LOCAL_FW / "model.bin").exists() else "large-v3-turbo")
def _pick_model() -> str:
    if os.environ.get("REELMAKER_OLLAMA_MODEL"):
        return os.environ["REELMAKER_OLLAMA_MODEL"]
    # use the bigger model when it has been downloaded (better hooks and story picks)
    if (Path.home() / ".ollama/models/manifests/registry.ollama.ai/library/qwen2.5/14b").exists():
        return "qwen2.5:14b"
    return "qwen2.5:7b"


OLLAMA_MODEL = _pick_model()
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

KOKORO_MODEL = MODELS_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = MODELS_DIR / "voices-v1.0.bin"
VOICES = {
    "cb": "Hype narrator (expressive, Chatterbox)",
    "am_michael": "Michael (US, warm)",
    "am_puck": "Puck (US, playful)",
    "am_fenrir": "Fenrir (US, deep)",
    "am_eric": "Eric (US, upbeat)",
    "af_heart": "Heart (US, female)",
    "af_bella": "Bella (US, female, energetic)",
    "bm_george": "George (UK)",
    "bf_emma": "Emma (UK, female)",
}
DEFAULT_VOICE = "am_puck"

_lock = threading.Lock()


def tool(name: str) -> str:
    """ffmpeg / ffprobe / yt-dlp / exiftool on this computer (prefers an ffmpeg that can burn in captions)."""
    return system.find_tool(name, system.ffmpeg_dirs() if name in ("ffmpeg", "ffprobe") else ())


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def settings() -> dict:
    s = read_json(SETTINGS_FILE, {})
    s.setdefault("reels_root", str(DEFAULT_REELS_ROOT))
    s.setdefault("last_day", "")
    return s


def save_settings(**kw) -> dict:
    with _lock:
        s = settings()
        s.update(kw)
        write_json(SETTINGS_FILE, s)
        return s


def reels_root() -> Path:
    p = Path(settings()["reels_root"]).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


SAFE_NAME = re.compile(r"[^A-Za-z0-9 _\-]+")


def safe_name(name: str) -> str:
    name = SAFE_NAME.sub("", name).strip()
    return re.sub(r"\s+", " ", name)[:60]


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:30].strip("-")


def parse_time(t: str) -> float:
    t = t.strip()
    if not t:
        raise ValueError("empty time")
    parts = t.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad time {t!r}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def parse_ranges(text: str, max_len: float = 600) -> list[tuple[float, float]]:
    """Parse lines like '12:10-12:30', '25:42 – 26:08', '1:02:03 to 1:02:30'."""
    out = []
    for raw in re.split(r"[\n,;]+", text):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([\d:.]+)\s*(?:-|–|—|to)\s*([\d:.]+)$", line)
        if not m:
            raise ValueError(f"Couldn't read the range “{line}”. Use a format like 12:10-12:30.")
        a, b = parse_time(m.group(1)), parse_time(m.group(2))
        if b <= a:
            raise ValueError(f"The range “{line}” ends before it starts.")
        if b - a > max_len:
            raise ValueError(f"The range “{line}” is longer than {max_len / 60:.0f} minutes.")
        out.append((a, b))
    if not out:
        raise ValueError("Enter at least one time range.")
    return out


def fmt_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{int(h)}:{int(m):02d}:{s:04.1f}"
    return f"{int(m)}:{s:04.1f}"
