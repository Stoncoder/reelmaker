"""Local voiceover (Kokoro) and hook / story suggestions (Ollama)."""
from __future__ import annotations

import hashlib
import os
import json
import threading
from pathlib import Path

import requests

from .config import DEFAULT_VOICE, KOKORO_MODEL, KOKORO_VOICES, OLLAMA_MODEL, OLLAMA_URL, fmt_time

_tts = None
_tts_lock = threading.Lock()


def _kokoro():
    global _tts
    if _tts is None:
        from kokoro_onnx import Kokoro
        _tts = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
    return _tts


# ---------------------------------------------------------------- Chatterbox (expressive voice)

import subprocess
from .config import ROOT_DIR

from .system import NO_WINDOW, venv_python

TTS_PY = venv_python(ROOT_DIR / ".venv-tts")   # the optional expressive voice has its own environment
_cb = None
_cb_lock = threading.Lock()


def chatterbox_available() -> bool:
    return TTS_PY.exists()


def _read_json(proc) -> dict:
    """Next JSON line from the worker (skips any stray library output)."""
    while True:
        line = proc.stdout.readline()
        if not line:
            return {}
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass


def _cb_proc():
    global _cb
    if _cb is None or _cb.poll() is not None:
        env = {**os.environ, "HF_HUB_DISABLE_XET": "1", "PYTORCH_ENABLE_MPS_FALLBACK": "1", "PYTHONIOENCODING": "utf-8"}
        _cb = subprocess.Popen([str(TTS_PY), "-I", str(ROOT_DIR / "app" / "tts_worker.py")], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
                               env=env, creationflags=NO_WINDOW)
        first = _read_json(_cb)
        if not first.get("ready"):
            _cb = None
            raise RuntimeError("The Chatterbox voice failed to start.")
    return _cb


_cb_timer = None


def stop_voice_worker():
    """Free the voice model (memory + GPU). It restarts by itself the next time a voice is needed."""
    global _cb
    with _cb_lock:
        if _cb is not None and _cb.poll() is None:
            try:
                _cb.stdin.close()
                _cb.wait(timeout=5)
            except Exception:
                _cb.kill()
        _cb = None


def _chatterbox(text, out, ref, energy):
    global _cb_timer
    import threading
    if _cb_timer:
        _cb_timer.cancel()
    with _cb_lock:
        p = _cb_proc()
        p.stdin.write(json.dumps({"text": text, "out": str(out), "ref": ref, "exaggeration": energy,
                                  "cfg": 0.35 if energy > 0.7 else 0.45}) + "\n")
        p.stdin.flush()
        r = _read_json(p)
    _cb_timer = threading.Timer(120, stop_voice_worker)
    _cb_timer.daemon = True
    _cb_timer.start()
    if not r.get("ok"):
        raise RuntimeError("Voice generation failed: " + str(r.get("error")))


ACRONYMS = {"GTA", "NPC", "NPCS", "RP", "VR", "AI", "FBI", "CIA", "USA", "UK", "PS5", "PS4", "PC", "FPS", "MVP", "GOAT",
             "OG", "DM", "TV", "ID", "ATM", "CEO", "NBA", "NFL", "UFC", "WWE", "IRL", "AFK", "GG", "RPG", "XP", "HP", "KO"}


# How to SAY names the voice gets wrong (matched as whole words, any capitalisation, also with 's).
# Users add their own in Hook ▸ Voice settings ▸ "Say names right" (saved as settings["pronounce"]).
SAY_AS = {
    "ISHOWSPEED": "I Show Speed", "IShowSpeed": "I Show Speed", "SHOWSPEED": "Show Speed",
    "KAI CENAT": "Kai Senat", "CENAT": "Senat",
    "ADIN ROSS": "Aiden Ross", "XQC": "X Q C", "NICKMERCS": "Nick Mercs", "TIMTHETATMAN": "Tim the Tatman",
    "DRDISRESPECT": "Doctor Disrespect", "SYPHERPK": "Sypher P K", "MRBEAST": "Mister Beast",
    "AMP": "Amp", "LUCIA": "Loo-see-ah", "LEONIDA": "Lee-oh-nee-dah",
}


def say_names(text: str, extra: dict | None = None) -> str:
    import re
    table = {**SAY_AS, **(extra or {})}
    for name in sorted(table, key=len, reverse=True):      # longest first: "KAI CENAT" before "CENAT"
        say = table[name].strip()
        if not name.strip() or not say:
            continue
        pat = r"(?<![\w])" + r"\s+".join(map(re.escape, name.split())) + r"(?=('s|'S|’s|’S)?(?![\w]))"
        text = re.sub(pat, say, text, flags=re.I)
    return text


def user_pronunciations() -> dict:
    from .config import settings
    p = settings().get("pronounce") or {}
    return p if isinstance(p, dict) else {}


def speakable(text: str) -> str:
    """ALL-CAPS hooks make voices spell words out ("KAI" -> K-A-I). Convert to normal case, keep real acronyms.
    Then names are swapped for how they're said (ISHOWSPEED -> I Show Speed)."""
    return say_names(_uncap(text), user_pronunciations())


def _uncap(text: str) -> str:
    import re
    letters = [c for c in text if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.7:
        return text

    def fix(m):
        w = m.group(0)
        base = re.sub(r"'S$", "", w)
        if base in ACRONYMS or (len(base) <= 4 and not re.search(r"[AEIOUY]", base)):  # GTA, CB, NPC
            return w if base == w else base + "'s"
        return w.capitalize()

    out = re.sub(r"[A-Z][A-Z']*", fix, text)
    return out[:1].upper() + out[1:]


def synth(text: str, voice: str, speed: float, out_dir: Path, ref: str | None = None, energy: float = 0.65) -> tuple[Path, float]:
    """Render the voiceover to a WAV (cached). voice 'cb' = Chatterbox (optionally matching `ref`), else a Kokoro voice."""
    import soundfile as sf
    text = speakable(text)
    voice = voice or DEFAULT_VOICE
    use_cb = voice == "cb" and chatterbox_available()
    key = hashlib.sha1(f"{text}|{voice}|{speed:.2f}|{ref if use_cb else ''}|{energy if use_cb else ''}".encode()).hexdigest()[:10]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"hook-{key}.wav"
    if not path.exists() and use_cb:
        raw = out_dir / f"hook-{key}-raw.wav"
        _chatterbox(text, raw, ref if ref and Path(ref).is_file() else None, energy)
        # speed + trim leading/trailing silence
        from .config import tool
        subprocess.run([tool("ffmpeg"), "-y", "-v", "error", "-i", str(raw), "-af",
                        f"silenceremove=start_periods=1:start_threshold=-45dB,areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse,atempo={max(0.5, min(2.0, speed)):.2f}",
                        str(path)], check=True)
        raw.unlink(missing_ok=True)
    if voice == "cb" and not use_cb:
        voice = DEFAULT_VOICE
    if not path.exists():
        with _tts_lock:
            samples, sr = _kokoro().create(text, voice=voice, speed=speed, lang="en-us")
        sf.write(path, samples, sr)
    info = sf.info(path)
    return path, info.frames / info.samplerate


def ollama_ok() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        names = [m["name"] for m in r.json().get("models", [])]
        return any(n.split(":")[0] == OLLAMA_MODEL.split(":")[0] for n in names)
    except Exception:
        return False


PROMPT = """You help a gaming creator turn a short GTA clip into a vertical reel.
Below is the transcript of the creator talking during the clip (times are seconds into the video),
plus the loudest audio moments (likely reactions, crashes or explosions).

TRANSCRIPT:
{transcript}

LOUDEST MOMENTS: {peaks}

Write 3 different opening hooks. Each hook is spoken by a voiceover in the first 2–3 seconds AND shown on screen.
Rules:
- Base it ONLY on what actually happens in the transcript. Be specific (mention the actual thing: the car, the cops, the jump…).
- Conversational, like a friend teasing a clip. 4–9 words. No hashtags, no emojis, no ALL CAPS, no "you won't believe".
- Tease the payoff without giving it away (don't say whether it worked).
- Sound like a person talking, not a headline. Good examples of the style (don't copy them):
  (from OTHER clips — never reuse their wording) "She really thought the boat could outrun the helicopter",
  "Nobody warned him about the guy on the roof". Bad: "Watch this insane jump!", "Epic fail!".

Also pick the payoff: the time of the funniest reaction / reveal, and a one-sentence note on the story (setup → payoff).
Say how confident you are (high/medium/low). Low if the transcript doesn't explain what happens on screen.

Reply with JSON only:
{{"hooks": ["...", "...", "..."], "payoff_time": 0.0, "story": "...", "confidence": "high|medium|low"}}"""


def suggest_hooks(words: list[dict], peaks: list[dict], start: float, end: float) -> dict:
    ws = [w for w in words if start - 1 <= w["s"] <= end + 1]
    lines, cur, t0 = [], [], None
    for w in ws:
        if t0 is None:
            t0 = w["s"]
        cur.append(w["w"])
        if len(cur) >= 12 or w["w"].endswith((".", "!", "?")):
            lines.append(f"[{t0:.1f}] {' '.join(cur)}")
            cur, t0 = [], None
    if cur:
        lines.append(f"[{t0:.1f}] {' '.join(cur)}")
    transcript = "\n".join(lines) or "(nobody talks in this clip)"
    peaks_s = ", ".join(f"{p['t']:.1f}s" for p in peaks) or "none"

    if not ollama_ok():
        return {"hooks": _fallback(ws), "payoff_time": peaks[-1]["t"] if peaks else None,
                "story": "Ollama isn't running, so these are simple placeholder hooks — edit them.",
                "confidence": "low", "source": "fallback"}
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", timeout=180, json={
            "model": OLLAMA_MODEL, "keep_alive": "30s", "prompt": PROMPT.format(transcript=transcript, peaks=peaks_s),
            "format": "json", "stream": False, "options": {"temperature": 0.8}})
        data = json.loads(r.json()["response"])
        hooks = [h.strip().strip('"') for h in data.get("hooks", []) if isinstance(h, str) and h.strip()][:3]
        if not hooks:
            raise ValueError("no hooks")
        pt = data.get("payoff_time")
        pt = float(pt) if isinstance(pt, (int, float)) and start - 1 <= pt <= end + 11 else None
        conf = data.get("confidence", "medium")
        if not ws:
            conf = "low"
        return {"hooks": hooks, "payoff_time": pt, "story": str(data.get("story", ""))[:300],
                "confidence": conf if conf in ("high", "medium", "low") else "medium", "source": "ollama"}
    except Exception as e:
        return {"hooks": _fallback(ws), "payoff_time": None, "confidence": "low", "source": "fallback",
                "story": f"Hook suggestion failed ({e}); edit these placeholders."}


def _fallback(ws):
    if not ws:
        return ["Watch what happens next", "This did not go to plan", "Wait for the ending"]
    return ["This did not go to plan", "Wait for the ending", "I was not ready for this"]


def describe_peaks(peaks):
    return ", ".join(fmt_time(p["t"]) for p in peaks)
