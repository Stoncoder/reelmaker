"""Smoke test: do Reelmaker's moving parts work on this computer (macOS or Windows)?

    .venv/bin/python -m tests.smoke              (macOS)
    .venv\\Scripts\\python -m tests.smoke         (Windows)

Renders a real preview and final export from a generated test video, runs speech-to-text, checks the external
tools, secret storage, the Trash / Recycle Bin, the usage sampler and the web server. Uses temporary folders only
— your reels and settings are never touched. Set REELMAKER_FW_MODEL=tiny to test transcription with a small model.
Exits with code 1 if anything fails. GitHub Actions runs it on macOS and Windows (see .github/workflows).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="reelmaker-smoke-"))
os.environ["REELMAKER_DATA"] = str(TMP / "data")     # never the real settings
os.environ["REELMAKER_ROOT"] = str(TMP / "reels")    # never the real reels
sys.path.insert(0, str(ROOT))

from app import media, render, system  # noqa: E402
from app.config import tool  # noqa: E402

results: list[tuple[str, str, str]] = []
CI = bool(os.environ.get("CI"))


def check(name, fn, soft=False):
    """soft: report a problem as WARN (things a CI machine may lack, like a desktop session), not FAIL."""
    t = time.time()
    try:
        info = fn()
        results.append(("PASS", name, f"{info or ''}  ({time.time() - t:.1f}s)"))
    except Exception as e:
        results.append(("WARN" if soft else "FAIL", name, f"{type(e).__name__}: {str(e)[:300]}"))
        if not soft and os.environ.get("SMOKE_VERBOSE"):
            traceback.print_exc()


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


# ------------------------------------------------------------------ tools
def t_ffmpeg():
    ff = tool("ffmpeg")
    out = run([ff, "-hide_banner", "-filters"]).stdout
    assert " subtitles " in out, f"{ff} can't burn in captions (no libass) — install ffmpeg-full (Mac) / Gyan.FFmpeg (Windows)"
    return ff


def t_ffprobe():
    return tool("ffprobe") if run([tool("ffprobe"), "-version"]).returncode == 0 else (_ for _ in ()).throw(RuntimeError("ffprobe missing"))


def t_ytdlp():
    p = run([tool("yt-dlp"), "--version"])
    assert p.returncode == 0, p.stderr[-200:]
    return f"{tool('yt-dlp')} {p.stdout.strip()}"


def t_encoder():
    hw = media.hw_encoder()
    return f"hardware: {hw[0]}" if hw else "no hardware encoder here — software x264 (fine, a bit slower)"


# ------------------------------------------------------------------ a real render
def make_piece(d: Path) -> dict:
    src = d / "source"
    src.mkdir(parents=True, exist_ok=True)
    p = run([tool("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=8",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:d=8",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src / "piece-1.mp4")])
    assert p.returncode == 0, p.stderr[-300:]
    info = media.video_info(src / "piece-1.mp4")
    return {"file": "piece-1.mp4", "offset": 0.0, "length": info["duration"], **info}


def project(piece: dict) -> dict:
    words = [{"w": w, "s": 1.0 + i * 0.5, "e": 1.4 + i * 0.5, "p": 1.0} for i, w in enumerate("this is a smoke test caption".split())]
    return {"pieces": [piece], "ranges": [{"start": 0, "end": 8}],
            "segments": [{"id": "a", "piece": 0, "range": 0, "start": 0.5, "end": 7.5, "enabled": True}],
            "cold_open": {"enabled": False, "piece": 0, "start": 0, "end": 0},
            "crops": {"layout": "split", "keyframes": [{"t": None, "face": {"x": 0.0, "y": 0.4, "w": 0.2, "h": 0.22},
                                                         "game": {"x": 0.2, "y": 0.0, "w": 0.6, "h": 1.0}}]},
            "hook": {"enabled": True, "screen": "SMOKE TEST HOOK", "voice_text": "", "voice": "am_puck",
                     "speed": 1.05, "energy": 0.65, "duck_db": -12, "voice_file": None},
            "captions": [{"id": "c1", "piece": 0, "start": 1.0, "end": 4.0, "text": "this is a smoke test caption",
                          "emph": [3], "words": words}],
            "caption_style": {"size": 80, "caps": True, "hook_size": 70, "highlight": "#FFE600", "hook_color": "#FF5FA2"}}


REEL = TMP / "reels" / "Smoke" / "reel-01"


def t_render_draft():
    piece = make_piece(REEL)
    out = render.render(REEL, project(piece), False)
    info = media.video_info(out)
    assert (info["width"], info["height"]) == (1080, 1920), f"{info['width']}x{info['height']}"
    return f"{out.name} {info['duration']:.1f}s 1080x1920, timeline for live captions: {bool(render.LAST_META.get('timeline'))}"


def t_render_final():
    out = render.render(REEL, project(media.video_info(REEL / "source" / "piece-1.mp4") | {"file": "piece-1.mp4", "offset": 0.0,
                                                                                          "length": 8.0}), True)
    rep = render.verify(out)
    bad = [f"{c['name']}: {c['detail']}" for c in rep["checks"] if not c["ok"]]
    assert not bad, "; ".join(bad)
    return f"{out.name} {rep['size_mb']} MB — all {len(rep['checks'])} export checks pass"


# ------------------------------------------------------------------ speech to text
def t_transcribe():
    from app import analysis
    words = analysis.transcribe(REEL / "source" / "piece-1.mp4", 0.0)   # a test tone: no words expected, but no crash
    engine = "MLX (Apple GPU)" if analysis.use_mlx() else f"faster-whisper ({os.environ.get('REELMAKER_FW_MODEL') or 'large-v3-turbo'})"
    return f"{engine}, {len(words)} words from a test tone"


# ------------------------------------------------------------------ system integration
def t_paths():
    return f"data: {system.data_dir()} · reels: {system.default_reels_root()} · fonts: {system.caption_fonts()}"


def t_secret():
    svc = "Reelmaker smoke test"
    system.secret_set(svc, "test-value-123")
    try:
        assert system.secret_get(svc) == "test-value-123", "stored secret didn't read back"
    finally:
        system.secret_delete(svc)
    assert system.secret_get(svc) is None, "secret wasn't deleted"
    return system.SECRET_STORE


def t_trash():
    f = TMP / "trash-me.txt"
    f.write_text("smoke test")
    system.trash(f)
    assert not f.exists(), "file still there"
    return "moved to " + ("the Trash" if system.IS_MAC else "the Recycle Bin")


def t_sysmon():
    from app import sysmon
    try:
        for _ in range(40):
            u = sysmon.usage()
            if u.get("cpu") is not None:
                return f"CPU {u['cpu']}% · GPU {u.get('gpu')} · RAM {u['mem_used'] / 1e9:.1f}/{u['mem_total'] / 1e9:.0f} GB"
            time.sleep(0.25)
        raise RuntimeError("no sample within 10 s")
    finally:
        sysmon.stop()


def t_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k != "REELMAKER_LAUNCHER"}
    # the server's output goes to a file: an unread pipe fills up (quickly on Windows) and freezes the server
    log = open(TMP / "server.log", "w", encoding="utf-8")
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.server:app", "--host", "127.0.0.1", "--port", str(port)],
                         cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=system.NO_WINDOW)
    tail = lambda: (TMP / "server.log").read_text(encoding="utf-8", errors="replace")[-600:]
    try:
        url = f"http://127.0.0.1:{port}"
        t0 = time.time()
        while True:
            try:
                st = json.loads(urllib.request.urlopen(url + "/api/state", timeout=5).read())
                break
            except Exception:
                if p.poll() is not None:
                    raise RuntimeError("server exited: " + tail())
                if time.time() - t0 > 120:
                    raise RuntimeError("server didn't answer within 120 s. Its output: " + tail())
                time.sleep(0.5)
        ver = json.loads(urllib.request.urlopen(url + "/api/ui-version", timeout=2).read())
        page = urllib.request.urlopen(url + "/", timeout=2).read().decode()
        assert "Reelmaker" in page
        req = urllib.request.Request(url + "/api/restart", data=b'{"force":false}', headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=2)
            raise RuntimeError("restart should be refused when not started by the launcher")
        except urllib.error.HTTPError as e:
            assert e.code == 409, e.code
        return f"answered after {time.time() - t0:.1f}s, api {ver['api']}, page ok, reels root {st['root']}"
    finally:
        p.terminate()
        try:
            p.wait(10)
        except Exception:
            p.kill()
        log.close()


if __name__ == "__main__":
    print(f"Reelmaker smoke test · {sys.platform} · Python {sys.version.split()[0]} · temp {TMP}\n")
    check("ffmpeg with captions support", t_ffmpeg)
    check("ffprobe", t_ffprobe)
    check("yt-dlp", t_ytdlp)
    check("video encoder", t_encoder)
    check("folders & fonts", t_paths)
    check("render preview (layout + hook)", t_render_draft)
    check("render final export + checks", t_render_final)
    check("speech to text", t_transcribe)
    check("secret storage", t_secret, soft=CI)
    check("Trash / Recycle Bin", t_trash, soft=CI)
    check("usage sampler", t_sysmon)
    check("web server", t_server)
    w = max(len(n) for _, n, _ in results)
    for status, name, info in results:
        print(f"  {status:4}  {name.ljust(w)}  {info}")
    fails = sum(1 for s, _, _ in results if s == "FAIL")
    print(f"\n{len(results) - fails}/{len(results)} passed" + (f", {fails} FAILED" if fails else ""))
    sys.exit(1 if fails else 0)
