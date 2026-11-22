"""Reelmaker local web server."""
from __future__ import annotations

import os
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import ai, analysis, live, media, render, story, system
from .config import (APP_DIR, DEFAULT_VOICE, OLLAMA_MODEL, PRESETS_FILE, VOICES, fmt_time, parse_ranges,
                     read_json, reels_root, safe_name, save_settings, settings, slug, write_json)

app = FastAPI(title="Reelmaker")

API_VERSION = 5   # the page checks this: a page newer than the server shows "restart Reelmaker"


def _code_stamp() -> float:
    return max((f.stat().st_mtime for f in APP_DIR.glob("*.py")), default=0.0)


CODE_STAMP = _code_stamp()   # when the code this server is running was last changed


@app.on_event("shutdown")
def _stop_everything():
    """When Reelmaker closes, nothing it started may keep running: downloads, encodes, the voice engine."""
    for procs in list(media.PROCS.values()):
        for pr in list(procs):
            try:
                pr.kill()
            except Exception:
                pass
    try:
        ai.stop_voice_worker()
    except Exception:
        pass
    for w in list(WATCHERS.values()):
        if w.get("awake"):
            w["awake"].terminate()
    try:
        from . import sysmon
        sysmon.stop()
    except Exception:
        pass


@app.middleware("http")
async def no_stale_ui(request, call_next):
    """Always serve the latest page/scripts so a refresh picks up updates."""
    resp = await call_next(request)
    if not request.url.path.startswith(("/media/", "/api/")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
LOCK = threading.RLock()
JOBS: dict[str, dict] = {}            # reel id -> live job status
PIPE = ThreadPoolExecutor(max_workers=1)   # fetch / transcribe / analyze, one reel at a time
RENDER = ThreadPoolExecutor(max_workers=1)

EDITABLE = {"name", "segments", "cold_open", "crops", "hook", "captions", "caption_style"}


# ------------------------------------------------------------------ helpers

def day_dir(day: str) -> Path:
    d = reels_root() / day
    if day != safe_name(day) or not day or not d.is_dir():
        raise HTTPException(404, f"Day folder “{day}” not found")
    return d


def reel_dir(day: str, reel: str) -> Path:
    d = day_dir(day) / reel
    if "/" in reel or reel.startswith(".") or not (d / "project.json").exists():
        raise HTTPException(404, "Reel not found")
    return d


def load(d: Path) -> dict:
    return read_json(d / "project.json", {})


def save(d: Path, p: dict) -> None:
    with LOCK:
        p["updated"] = time.time()
        write_json(d / "project.json", p)


def rid(d: Path) -> str:
    return f"{d.parent.name}/{d.name}"


def set_job(d: Path, **kw):
    j = JOBS.setdefault(rid(d), {})
    j.update(kw)


# Overall progress: each stage owns a slice of one 0→1 bar, so the bar only ever moves forward.
def make_plan(locate: bool, youtube: bool, reuse_words: bool) -> dict:
    w = {"locate": 0.0, "fetch": 0.5 if youtube else 0.15, "transcribe": 0.03 if reuse_words else 0.22,
         "analyze": 0.2, "cut": 0.12}   # cut includes the local AI picking the story and writing hooks (~20-30 s)
    if not youtube:
        w["transcribe"] = 0.03 if reuse_words else 0.4
    total = sum(w.values())
    w = {k: v / total for k, v in w.items()}
    if locate:  # finding the moment in the whole video comes first and is the slow part
        w = {k: v * 0.45 for k, v in w.items()}
        w["locate"] = 0.55
    plan, lo = {}, 0.0
    for k in ("locate", "fetch", "transcribe", "analyze", "cut"):
        plan[k] = (lo, lo + w[k])
        lo += w[k]
    return plan


def stage(d: Path, name: str, frac: float = 0.0, span: tuple | None = None):
    """Enter a stage (or a part of it): live task reports from media.Meter fill the slice
    `span` (fractions of the stage, default its whole length)."""
    j = JOBS.setdefault(rid(d), {})
    lo, hi = (j.get("plan") or make_plan(False, True, False)).get(name, (0.0, 1.0))
    a, b = span or (0.0, 1.0)
    s_lo, s_hi = lo + (hi - lo) * a, lo + (hi - lo) * b
    prog = s_lo + (s_hi - s_lo) * max(0.0, min(1.0, frac))
    j.update(stage=name, span=(s_lo, s_hi), task=None, progress=max(j.get("progress", 0.0), prog))


def report(d: Path, info: dict):
    """media.Meter callback: show the live task and move the overall bar inside the current slice."""
    j = JOBS.setdefault(rid(d), {})
    s_lo, s_hi = j.get("span") or (j.get("progress", 0.0), j.get("progress", 0.0))
    prog = s_lo + (s_hi - s_lo) * (info.get("frac") or 0.0)
    j.update(task=info, progress=max(j.get("progress", 0.0), min(prog, s_hi)))


def log(d: Path, msg: str):
    with LOCK:
        p = load(d)
        p.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {msg}")
        save(d, p)
    set_job(d, message=msg)


def update(d: Path, **kw):
    with LOCK:
        p = load(d)
        p.update(kw)
        save(d, p)
        return p


def other_hooks(d: Path) -> list[str]:
    """Hooks the other reels in this day folder already use, so a batch doesn't repeat itself."""
    out = []
    for o in sorted(d.parent.iterdir()):
        if o == d or not (o / "project.json").exists():
            continue
        hk = read_json(o / "project.json", {}).get("hook") or {}
        out += [hk.get("screen", "")] + list(hk.get("options") or [])[:3]
    return [h for h in out if h][-30:]


def siblings(d: Path, src: dict) -> list[tuple[Path, dict]]:
    """Other reels in this day folder made from the same video (or file)."""
    key = src.get("url") or src.get("path")
    out = []
    for o in sorted(d.parent.iterdir()):
        if o == d or not (o / "project.json").exists():
            continue
        q = read_json(o / "project.json", {})
        s2 = q.get("source") or {}
        if (s2.get("url") or s2.get("path")) == key:
            out.append((o, q))
    return out


def median_box(boxes: list[dict]) -> dict:
    import statistics
    return {k: statistics.median(b[k] for b in boxes) for k in ("x", "y", "w", "h")}


_FFMPEG_OK: dict = {}


def ffmpeg_status() -> dict:
    """Can this computer's ffmpeg burn in captions (needs libass)? Checked once."""
    if "ok" not in _FFMPEG_OK:
        ff = media.tool("ffmpeg")
        try:
            out = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=20, creationflags=system.NO_WINDOW).stdout
            _FFMPEG_OK.update(ok=" subtitles " in out, path=ff, found=bool(out))
        except Exception:
            _FFMPEG_OK.update(ok=False, path=ff, found=False)
    return dict(_FFMPEG_OK)


VOICE_KEYS = ("voice", "energy", "speed", "duck_db", "voice_ref")


def voice_default() -> dict:
    """The voice tone new reels start with: the one you saved in the Hook tab, else the built-in default."""
    base = {"voice": "cb" if ai.chatterbox_available() else DEFAULT_VOICE, "energy": 0.65, "speed": 1.05,
            "duck_db": -12, "voice_ref": settings().get("voice_ref")}
    saved = settings().get("voice_default") or {}
    out = {**base, **{k: saved[k] for k in VOICE_KEYS if k in saved}}
    if out["voice"] == "cb" and not ai.chatterbox_available():
        out["voice"] = DEFAULT_VOICE
    if out.get("voice_ref") and not Path(out["voice_ref"]).is_file():
        out["voice_ref"] = None   # the sample file was moved or deleted
    return out


def follow_default_voice(p: dict) -> bool:
    """A reel whose voice you never changed by hand always uses your CURRENT default voice tone — also reels
    that were made before you saved the default. Returns True if the reel's hook was changed."""
    k = p.get("hook")
    if not k or k.get("voice_file"):
        return False
    if "voice_custom" not in k:            # reels from before this rule: keep a voice that's already exported
        k["voice_custom"] = bool(p.get("final"))
        if k["voice_custom"]:
            return True
    if k["voice_custom"]:
        return False
    vd = voice_default()
    if all(k.get(x) == vd[x] for x in VOICE_KEYS):
        return False
    k.update({x: vd[x] for x in VOICE_KEYS})
    return True


def presets() -> dict:
    return read_json(PRESETS_FILE, {"presets": {}, "channel_default": {}})


# ------------------------------------------------------------------ pipeline

def source_key(src: dict) -> str:
    import hashlib
    k = src.get("url") or src.get("path") or ""
    return hashlib.sha1(k.encode()).hexdigest()[:12]


def full_scan(d: Path, src: dict, win: dict | None = None) -> dict:
    """Whole-video analysis (cached per source): full file, transcript, loudness, webcam box + movement.
    With a search window, only that part is scanned; times inside are relative to scan["offset"]."""
    from .config import DATA_DIR
    cdir = DATA_DIR / "cache" / source_key(src)
    cdir.mkdir(parents=True, exist_ok=True)
    if src.get("live") and not win:       # a running stream has no end yet: search everything streamed so far
        win = {"start": 0.0, "end": max(60.0, live.head_seconds(src["url"]) - 10)}
        log(d, f"Live stream: searching everything streamed so far (0:00–{fmt_time(win['end'])}).")
    tag = f"-{int(win['start'])}-{int(win['end'])}" if win else ""
    scan = read_json(cdir / f"scan{tag}.json", {})
    if scan.get("done"):
        return scan
    if win:
        a, b = float(win["start"]), float(win["end"])
        path = cdir / f"window{tag}.mp4"
        if not path.exists():
            tmp = cdir / f"window{tag}-dl.mp4"
            tmp.unlink(missing_ok=True)
            stage(d, "locate", span=(0.0, 0.35))
            if src.get("live"):
                log(d, f"Downloading a small 360p copy of {fmt_time(a)}–{fmt_time(b)} from the live stream to search in…")
                live.fetch(src["url"], a, b, tmp, max_height=360, label="Downloading a 360p search copy")
            elif src["type"] == "youtube":
                log(d, f"Downloading a small 360p copy of {fmt_time(a)}–{fmt_time(b)} to search in…")
                # 360p is roughly 60 KB/s of video+audio: lets the bar move even though yt-dlp can't size a section
                media.ytdlp_download([media.tool("yt-dlp"), "--no-playlist", "-f",
                                      "bv*[protocol^=m3u8][height<=360]+ba[protocol^=m3u8]/b[protocol^=m3u8][height<=360]/bv*[height<=360]+ba/b[height<=360]/wa*+ba/b",
                                      "--merge-output-format", "mp4", "--download-sections", f"*{a}-{b}", "--force-keyframes-at-cuts",
                                      "--no-part", "-o", str(tmp), src["url"]], "Downloading a 360p search copy",
                                     timeout=7200, expect=(b - a) * 60e3)
            else:
                log(d, f"Preparing {fmt_time(a)}–{fmt_time(b)} of your file to search in…")
                m = media.Meter("Preparing a search copy", total=b - a, unit="sec")
                media.run([media.tool("ffmpeg"), "-y", "-v", "error", *media.PROG, "-ss", str(a), "-to", str(b), "-i", src["path"],
                           "-vf", "scale=-2:360", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(tmp)], timeout=7200,
                          on_line=media.ffmpeg_progress(lambda s: m.update(done=s, frac=s / (b - a))))
                m.finish()
            tmp.rename(path)
    elif src["type"] == "youtube":
        full = cdir / "full.mp4"
        if not full.exists():
            stage(d, "locate", span=(0.0, 0.35))
            log(d, "To find moments, downloading a small 360p copy of the whole video for searching (cached for next time). The clips themselves are fetched in full quality.")
            for f in cdir.glob("full-dl.*"):
                f.unlink()
            media.ytdlp_download([media.tool("yt-dlp"), "--no-playlist", "-f", "bv*[height<=360]+ba/b[height<=360]/wa*+ba/b",
                                  "--merge-output-format", "mp4", "--no-part", "-o", str(cdir / "full-dl.%(ext)s"), src["url"]],
                                 "Downloading a 360p search copy", timeout=7200)
            next(cdir.glob("full-dl.*")).rename(full)
        path = full
    else:
        path = Path(src["path"])
    info = media.video_info(path)
    if not win and info["duration"] > 3 * 3600:
        log(d, "Tip: this is a long stream. Next time set “Search only between” so the app scans just that part — much faster.")
    log(d, f"Transcribing the whole video ({info['duration'] / 60:.0f} min) on your Mac — this is the slow part…")
    stage(d, "locate", span=(0.35, 0.85))
    media.check_cancel()
    words = analysis.transcribe(path, 0.0, label="Transcribing the whole video")
    media.check_cancel()
    log(d, f"Transcript: {len(words)} words. Measuring reactions…")
    stage(d, "locate", span=(0.85, 1.0))
    prof = analysis.audio_profile(path, 0.0, hop=0.5)
    stage(d, "locate", 0.3, span=(0.85, 1.0))
    cam = analysis.detect_webcam(path, 0.0, every=max(2.0, info["duration"] / 50))
    stage(d, "locate", 0.5, span=(0.85, 1.0))
    motion = analysis.motion_series(path, 0.0, cam.get("box") if cam.get("found") else None, every=1.0)
    scan = {"done": True, "path": str(path), "duration": info["duration"], "words": words, "profile": prof,
            "webcam": cam, "motion": motion, "offset": float(win["start"]) if win else 0.0}
    write_json(cdir / f"scan{tag}.json", scan)
    return scan


def locate_for(d: Path, p: dict) -> dict:
    """Pick this reel's time range from the whole video (scenario or best moment)."""
    from .config import DATA_DIR
    src = p["source"]
    scan = full_scan(d, src, p.get("window"))
    cdir = DATA_DIR / "cache" / source_key(src)
    bfile = cdir / f"batch-{p['batch']}.json"
    found = read_json(bfile, None)
    if found is None:
        b = p["batch_info"]
        log(d, "Finding the best moments…" if not b["scenarios"] else "Finding your scenarios in the video…")
        found = story.locate(scan["words"], scan["profile"], scan["motion"], scan["duration"], b["scenarios"],
                             b["count"], p.get("creator", ""), p.get("target", 35))
        off = scan.get("offset", 0.0)
        for c in found:
            c["start"] += off; c["end"] += off
        write_json(bfile, found)
    i = p["batch_index"]
    if i >= len(found):
        raise media.MediaError("Couldn't find enough moments for this reel. Try describing the scenario, or give timestamps.")
    c = found[i]
    log(d, f"Picked {fmt_time(c['start'])}–{fmt_time(c['end'])}: {c['scenario']} ({c['why']})")
    return c


def pipeline(d: Path):
    media.CTX.tag = rid(d)
    media.CTX.report = lambda info: report(d, info)
    media.CANCELLED.discard(rid(d))
    try:
        p = load(d)
        src = p["source"]
        src_dir = d / "source"
        src_dir.mkdir(exist_ok=True)
        set_job(d, progress=0.0, task=None, span=None, started=time.time(),
                plan=make_plan(not p.get("ranges"), src["type"] == "youtube", not p.get("ranges")))
        stage(d, "locate" if not p.get("ranges") else "fetch")
        update(d, state="locating" if not p.get("ranges") else "fetching", error=None)
        if src["type"] == "youtube" and not src.get("title"):
            try:
                meta = media.youtube_meta(src["url"])
                src.update(title=meta["title"], channel=meta["channel"], video_id=meta["id"])
                if meta["is_live"] and "youtu" in src["url"]:
                    src["live"] = True
                    log(d, "This is a live stream that's still running — clipping straight from it (anything already streamed can be fetched).")
                update(d, source=src)
            except media.MediaError as e:
                log(d, f"Couldn't read video details ({str(e).splitlines()[-1][:120]}); trying the download anyway.")
        scan = None
        if not p.get("ranges"):
            update(d, state="locating")
            c = locate_for(d, p)
            p = update(d, ranges=[{"start": round(c["start"], 2), "end": round(c["end"], 2)}],
                       scenario=p.get("scenario") or c["scenario"], located=c, name=p.get("name"))
            scan = full_scan(d, src, p.get("window"))
        else:
            from .config import DATA_DIR
            sc = read_json(DATA_DIR / "cache" / source_key(src) / "scan.json", {})
            scan = sc if sc.get("done") else None
            if scan:
                set_job(d, plan=make_plan(False, src["type"] == "youtube", True))
        update(d, state="fetching")
        pieces = []
        nr = len(p["ranges"])
        for i, r in enumerate(p["ranges"]):
            stage(d, "fetch", span=(i / nr, (i + 1) / nr))
            dst = src_dir / f"piece-{i + 1}.mp4"
            if src.get("live"):           # a stream that's still running: fetch just its segments
                a0, b0 = max(0.0, r["start"] - media.PAD), r["end"] + media.PAD
                log(d, f"Fetching {fmt_time(a0)}–{fmt_time(b0)} straight from the live stream…")
                info = live.fetch(src["url"], a0, b0, dst)
            elif src["type"] == "youtube":  # full quality, just this section (the scan copy is only 360p)
                info = media.fetch_youtube_piece(src["url"], r["start"], r["end"], dst, lambda m: log(d, m))
            else:
                log(d, f"Cutting {fmt_time(r['start'])}–{fmt_time(r['end'])} (+ padding) from your file…")
                info = media.extract_local_piece(Path(src["path"]), r["start"], r["end"], dst)
            pieces.append(info)
            note = " (variable frame rate — will be made constant)" if info["vfr"] else ""
            log(d, f"Range {i + 1}: got {info['length']:.0f}s, {info['width']}×{info['height']} @ {info['fps']:g} fps{note}.")
        update(d, pieces=pieces, state="transcribing")

        words = []
        for i, pc in enumerate(pieces):
            stage(d, "transcribe", span=(i / len(pieces), (i + 1) / len(pieces)))
            a, b = pc["offset"], pc["offset"] + pc["length"]
            if scan:
                off = scan.get("offset", 0.0)
                ws = [dict(w, s=w["s"] + off, e=w["e"] + off) for w in scan["words"] if a <= w["s"] + off < b]
            else:
                log(d, f"Transcribing range {i + 1} on your Mac…")
                ws = analysis.transcribe(src_dir / pc["file"], pc["offset"],
                                         label="Transcribing" + (f" range {i + 1}" if len(pieces) > 1 else ""))
            for w in ws:
                w["piece"] = i
                words.append(w)
        update(d, words=words, state="analyzing")
        stage(d, "analyze")

        profiles, motions, peaks, sugg, cams, silences = [], [], [], [], [], []
        npc = len(pieces)
        for i, (pc, r) in enumerate(zip(pieces, p["ranges"])):
            span = (0.8 * i / npc, 0.8 * (i + 1) / npc)   # the last 20% is the webcam-moved check
            path = src_dir / pc["file"]
            prof = analysis.audio_profile(path, pc["offset"])
            profiles.append(prof)
            pw = [w for w in words if w["piece"] == i]
            log(d, f"Finding the webcam and the reactions in range {i + 1}…")
            stage(d, "analyze", 0.1, span=span)
            cam = analysis.detect_webcam(path, pc["offset"], every=2.0)
            if not cam.get("found") and scan and scan["webcam"].get("found"):
                cam = scan["webcam"]
            cams.append(cam)
            stage(d, "analyze", 0.5, span=span)
            mot = analysis.motion_series(path, pc["offset"], cam.get("box") if cam.get("found") else None, every=0.5)
            motions.append(mot)
            stage(d, "analyze", 0.9, span=span)
            pk = analysis.find_peaks(prof, r["start"], r["end"])
            for x in pk:
                x["piece"] = i
            peaks += pk
            silences.append(analysis.find_silences(prof, r["start"], r["end"]))
            sugg += analysis.boundary_suggestions(pw, (r["start"], r["end"]), i, prof, i)
            sugg += analysis.cut_suggestions(pw, (r["start"], r["end"]), i, prof)
        write_json(d / "analysis.json", {"profiles": profiles, "motions": motions})
        stage(d, "analyze", span=(0.8, 1.0))

        W, H = pieces[0]["width"], pieces[0]["height"]
        area = analysis.active_area(src_dir / pieces[0]["file"], W, H)
        cam = next((c for c in cams if c.get("found")), None)
        pr = presets()
        key = src.get("channel") or p.get("creator") or ""
        preset_name = pr["channel_default"].get(key)
        sib = siblings(d, src)
        fixed = next(((o, q) for o, q in reversed(sib) if (q.get("crops") or {}).get("edited")), None)
        if preset_name and preset_name in pr["presets"]:
            ps = pr["presets"][preset_name]
            crops = {"layout": ps["layout"], "keyframes": [{"t": None, "face": ps["face"], "game": ps["game"]}], "preset": preset_name}
            face_changes = []
            log(d, f"Applied your saved layout “{preset_name}”.")
        elif fixed:   # you already fixed the layout on another reel of this video: use that
            fc = fixed[1]["crops"]
            crops = {"layout": fc["layout"], "keyframes": [dict(fc["keyframes"][0], t=None)], "edited": True}
            face_changes = []
            log(d, f"Using the layout you set on {fixed[0].name} (same video).")
        elif cam:
            # the webcam sits in the same place for the whole stream: average this reel's guess with the
            # other reels' so one bad detection can't give one reel a different, wrong crop
            boxes = [cam["box"]] + [q["facecam"][0]["box"] for _, q in sib
                                    if (q.get("facecam") or [{}])[0].get("found") and q["facecam"][0].get("box")]
            if len(boxes) >= 2:
                cam = dict(cam, box=median_box(boxes))
            game = analysis.game_crop_in(area, W, H)
            kfs = [{"t": None, "face": cam["box"], "game": game}]
            # did the webcam move during the clip? compare where it is in each half
            face_changes = []
            media.Meter("Checking whether the webcam moves", unit="busy")
            for i, pc in enumerate(pieces):
                half = pc["length"] / 2
                if half < 6:
                    continue
                path = src_dir / pc["file"]
                tmp = d / "source" / f".half-{i}.mp4"
                media.run([media.tool("ffmpeg"), "-y", "-v", "error", "-ss", f"{half:.2f}", "-i", str(path), "-an",
                           "-c:v", "copy", str(tmp)])
                b2 = analysis.detect_webcam(tmp, pc["offset"] + half, every=2.0)
                tmp.unlink(missing_ok=True)
                bx = b2.get("box")
                if b2.get("found") and (abs(bx["x"] - cam["box"]["x"]) > 0.08 or abs(bx["y"] - cam["box"]["y"]) > 0.08):
                    t = round(pc["offset"] + half, 2)
                    kfs.append({"t": t, "face": bx, "game": game})
                    face_changes.append({"t": t})
            crops = {"layout": "split", "keyframes": kfs}
            log(d, "Webcam found — it goes in the top half, gameplay in the bottom half."
                + (" It seems to move around " + ", ".join(fmt_time(c["t"]) for c in face_changes)
                   + " — check Layout ▸ webcam moves." if face_changes else ""))
        else:
            crops = {"layout": "gameonly", "keyframes": [{"t": None, "face": {"x": 0.05, "y": 0.4, "w": 0.2, "h": 0.2},
                                                          "game": analysis.game_crop_in(area, W, H)}]}
            face_changes = []
            log(d, "No webcam found — using gameplay only. If there is a webcam, open Layout and click “Mark the webcam myself”.")

        # ---- story cut
        stage(d, "cut")
        target = int(p.get("target", 35))
        total_src = sum(r["end"] - r["start"] for r in p["ranges"])
        log(d, f"Cutting it down to a ~{target}s story…" if target else
            "Cutting the story to its natural length (setup → payoff → reaction, boring bits removed, 15–60s)…")
        segments, cold, hooks, story_note, conf, payoff = [], None, [], "", "low", None
        nrg = len(p["ranges"])
        for i, r in enumerate(p["ranges"]):
            stage(d, "cut", span=(0.9 * i / nrg, 0.9 * (i + 1) / nrg))
            media.Meter("Picking the story and writing hooks (local AI)" + (f" — range {i + 1}" if nrg > 1 else ""), unit="busy")
            t_i = max(8, int(round(target * (r["end"] - r["start"]) / total_src))) if target else 0
            res = story.smart_cut([w for w in words if w["piece"] == i], profiles[i], motions[i],
                                  [x for x in peaks if x["piece"] == i], r["start"], r["end"],
                                  p.get("scenario", ""), p.get("creator", ""), t_i, silences[i],
                                  avoid=other_hooks(d) if i == 0 else None)
            for s0, e0 in res["segments"]:
                segments.append({"id": analysis.uid(), "piece": i, "range": i, "start": s0, "end": e0, "enabled": True})
            if i == 0:
                hooks, story_note, conf, payoff = res["hooks"], res["story"], res["confidence"], res["payoff"]
                if res["cold_open"]:
                    cold = {"enabled": True, "piece": 0, "start": res["cold_open"][0], "end": res["cold_open"][1]}
        if not hooks:
            stage(d, "cut", span=(0.9, 1.0))
            media.Meter("Writing hook ideas (local AI)", unit="busy")
            hk = ai.suggest_hooks(words, peaks, p["ranges"][0]["start"], p["ranges"][-1]["end"])
            hooks = hk["hooks"]
        if cold is None:
            pt = payoff if isinstance(payoff, (int, float)) else (max(peaks, key=lambda x: x["z"])["t"] if peaks else p["ranges"][0]["start"] + 1)
            cold = {"enabled": False, "piece": 0, "start": round(pt - 0.9, 2), "end": round(pt + 0.7, 2)}
        captions = []
        for i in range(len(pieces)):
            for c in analysis.build_captions([w for w in words if w["piece"] == i]):
                c["piece"] = i
                captions.append(c)
        vd = voice_default()
        hook = {"enabled": True, "screen": hooks[0], "voice_text": hooks[0], "linked": True,
                "voice": vd["voice"], "voice_ref": vd["voice_ref"], "energy": vd["energy"],
                "speed": vd["speed"], "duck_db": vd["duck_db"], "options": hooks, "story": story_note, "confidence": conf,
                "payoff_time": payoff, "voice_file": None, "voice_custom": False,
                "source": story.HOOK_INFO.get("source") or ("local" if ai.ollama_ok() else "fallback"),
                "writer_model": story.HOOK_INFO.get("model"), "writer_note": story.HOOK_INFO.get("note")}
        if story.HOOK_INFO.get("source") == "groq":
            log(d, f"Hooks written by Groq ({story.HOOK_INFO.get('model')}).")
        elif story.HOOK_INFO.get("note"):
            log(d, "Hooks: " + story.HOOK_INFO["note"])
        update(d, peaks=peaks, suggestions=sugg, facecam=cams, face_changes=face_changes, crops=crops, segments=segments,
               captions=captions, hook=hook, cold_open=cold, state="ready")
        dur = sum(s["end"] - s["start"] for s in segments) + ((cold["end"] - cold["start"]) if cold["enabled"] else 0)
        log(d, f"Draft cut: {dur:.0f}s from {total_src:.0f}s of footage. Rendering a first preview…")
        set_job(d, stage="ready", progress=1, task=None, span=None)
        auto = bool(load(d).get("auto_export"))
        if auto:
            log(d, "Automatic export is on: the final MP4 is made right after the preview.")
        queue_render(d, False, then_final=auto)
    except Exception as e:
        if not d.exists():
            return  # reel was deleted
        if isinstance(e, media.Cancelled) or rid(d) in media.CANCELLED:
            update(d, state="error", error="Cancelled. Click “Try again” to restart it, or delete the reel.")
            log(d, "Cancelled.")
        else:
            traceback.print_exc()
            update(d, state="error", error=str(e)[:3000])
            log(d, "Stopped: " + str(e).splitlines()[0][:200])
        set_job(d, stage="error", progress=0, task=None, span=None)
    finally:
        media.CTX.tag = None
        media.CTX.report = None


def queue_render(d: Path, final: bool, learn: bool = True, then_final: bool = False):
    """Put a render in line. The reel shows "waiting to start" until its turn (one render at a time)."""
    set_job(d, render="final" if final else "draft", render_progress=0, render_phase="Waiting for its turn…",
            render_started=time.time(), render_error=None, render_done=None)
    RENDER.submit(do_render, d, final, learn, then_final)


def do_render(d: Path, final: bool, learn: bool = True, then_final: bool = False):
    """learn: remember this hook as a style example (only for exports you started yourself).
    then_final: right after this preview, export the final MP4 too (automatic batches)."""
    key = "final" if final else "draft"
    try:
        set_job(d, render=key, render_progress=0, render_error=None, render_phase=None, render_started=time.time())
        with LOCK:
            p = load(d)
            if follow_default_voice(p):   # use your current default voice unless you set one for this reel
                save(d, p)

        def prog(f, phase=None):
            j = JOBS.setdefault(rid(d), {})
            kw = {"render_progress": max(j.get("render_progress") or 0.0, f)}
            if phase:
                kw["render_phase"] = phase
            set_job(d, **kw)
        out = render.render(d, p, final, prog)
        entry = {"file": str(out.relative_to(d)), "time": time.time(), "duration": round(render.total_duration(p), 2),
                 **({} if final else render.LAST_META)}
        with LOCK:
            p = load(d)
            if final:
                hk = (p.get("hook") or {}).get("screen", "").strip()
                if learn and hk and (p.get("hook") or {}).get("enabled"):
                    ex = settings().get("hook_examples") or []
                    if hk.upper() not in [e.upper() for e in ex]:
                        save_settings(hook_examples=(ex + [hk.upper()])[-12:])
                rep = render.verify(out)
                render.write_report(out, rep)
                entry["report"] = rep
                p["final"] = entry
            else:
                p.setdefault("drafts", []).append(entry)
            save(d, p)
        if then_final and d.exists():
            set_job(d, render_done=key)
            queue_render(d, True, learn=False)   # keeps "render" set, so the page keeps showing progress
        else:
            set_job(d, render=None, render_done=key)
    except Exception as e:
        traceback.print_exc()
        set_job(d, render=None, render_error=str(e)[:3000])


def recover_interrupted():
    root = reels_root()
    for pj in root.glob("*/*/project.json"):
        p = read_json(pj, {})
        if p.get("state") in ("queued", "locating", "fetching", "transcribing", "analyzing"):
            p["state"] = "error"
            p["error"] = "Processing was interrupted (the app was closed). Click Retry."
            write_json(pj, p)


recover_interrupted()


# ------------------------------------------------------------------ API: days & settings

# ------------------------------------------------------------------ Live tab: who's live, find moments, approve

LIVE_DIR = None


def live_dir() -> Path:
    from .config import DATA_DIR
    d = DATA_DIR / "live-search"
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.get("/api/live/now")
def api_live_now(refresh: bool = False):
    """Only runs when you open the Live tab or click Refresh — never in the background."""
    return live.live_now(refresh)


@app.get("/api/live/upcoming")
def api_live_upcoming(refresh: bool = False):
    """Your creators' scheduled streams (YouTube). Only runs when the Live tab asks."""
    return live.upcoming(refresh)


@app.get("/api/live/creators")
def api_live_creators():
    return {"creators": live.creators(), "defaults": live.DEFAULT_CREATORS}


@app.put("/api/live/creators")
def api_set_live_creators(body: dict = Body(...)):
    out = []
    for c in (body.get("creators") or [])[:60]:
        name = str(c.get("name") or "").strip()[:40]
        row = {k: str(c.get(k) or "").strip()[:80] for k in live.PLATFORMS}
        if name and any(row.values()):
            out.append({"name": name, **{k: v for k, v in row.items() if v}})
    save_settings(live_creators=out or None)
    live._now.update(t=0, data=None)
    live._soon.update(t=0, data=None)
    return {"creators": live.creators()}


def live_search(d: Path):
    """Scan the last part of a live stream and suggest the best moments (nothing is clipped until you approve)."""
    media.CTX.tag = rid(d)
    media.CTX.report = lambda info: report(d, info)
    media.CANCELLED.discard(rid(d))
    try:
        p = load(d)
        st = p["stream"]
        set_job(d, progress=0.0, task=None, span=None, started=time.time(),
                plan={"locate": (0.0, 0.9), "cut": (0.9, 1.0)})
        stage(d, "locate")
        update(d, state="locating", error=None)
        head = live.stream_position(st)
        a = 0.0 if not p["minutes"] else max(0.0, head - p["minutes"] * 60)
        b = max(a + 60, head - 15)
        log(d, f"{st['creator']} is {fmt_time(head)} into the stream. Searching {fmt_time(a)}–{fmt_time(b)}…")
        src = {"type": "youtube", "url": st["clip_url"], "live": st["platform"] == "youtube"}
        scan = full_scan(d, src, {"start": a, "end": b})
        stage(d, "cut")
        log(d, "Picking the best moments (local AI)…")
        found = story.locate(scan["words"], scan["profile"], scan["motion"], scan["duration"], [], p["count"],
                             st["creator"], p.get("target") or 0)
        off = scan.get("offset", 0.0)
        for i, c in enumerate(found):
            c["start"] = round(c["start"] + off, 1); c["end"] = round(c["end"] + off, 1)
            c["said"] = " ".join(w["w"] for w in scan["words"] if c["start"] <= w["s"] + off < c["end"])[:400]
            c["id"] = i
        update(d, state="ready", moments=found, searched=[a, b])
        log(d, f"Found {len(found)} moment{'s' if len(found) != 1 else ''}. Tick the ones you want and click Make reels.")
        set_job(d, stage="ready", progress=1.0, task=None, span=None)
    except Exception as e:
        cancelled = isinstance(e, media.Cancelled) or rid(d) in media.CANCELLED
        if not cancelled:
            traceback.print_exc()
        update(d, state="error", error="Cancelled." if cancelled else str(e)[:2000])
        log(d, "Cancelled." if cancelled else "Stopped: " + str(e).splitlines()[0][:200])
        set_job(d, stage="error", progress=0, task=None, span=None)
    finally:
        media.CTX.tag = None
        media.CTX.report = None


@app.post("/api/live/search")
def api_live_search(body: dict = Body(...)):
    st = body.get("stream") or {}
    if not st.get("clip_url"):
        raise HTTPException(400, "This stream can't be clipped (no recording available).")
    sid = time.strftime("%Y%m%d-%H%M%S") + "-" + analysis.uid()[:4]
    d = live_dir() / sid
    d.mkdir()
    keep = {k: st.get(k) for k in ("creator", "platform", "handle", "title", "clip_url", "watch_url", "thumbnail")}
    save(d, {"stream": keep, "minutes": int(body.get("minutes") or 0), "count": int(min(10, max(1, int(body.get("count") or 3)))),
             "target": int(body.get("target") or 0), "state": "queued", "log": [], "created": time.time()})
    set_job(d, stage="queued", progress=0)
    PIPE.submit(live_search, d)
    return {"id": sid}


@app.get("/api/live/search/{sid}")
def api_live_search_get(sid: str):
    d = live_dir() / safe_name(sid)
    if not (d / "project.json").exists():
        raise HTTPException(404, "Search not found.")
    p = load(d)
    p["job"] = JOBS.get(rid(d), {})
    p["id"] = sid
    return p


@app.post("/api/live/search/{sid}/cancel")
def api_live_search_cancel(sid: str):
    d = live_dir() / safe_name(sid)
    media.cancel(rid(d))
    return {"ok": True}


@app.post("/api/live/search/{sid}/approve")
def api_live_approve(sid: str, body: dict = Body(...)):
    """Turn the moments you ticked into reels (clipped in full quality, one after another)."""
    d = live_dir() / safe_name(sid)
    p = load(d)
    pick = set(body.get("ids") or [])
    ms = [m for m in (p.get("moments") or []) if m.get("id") in pick]
    if not ms:
        raise HTTPException(400, "Tick at least one moment.")
    st = p["stream"]
    req = {"source": {"type": "youtube", "url": st["clip_url"]}, "creator": st["creator"],
           "target": int(body.get("target", p.get("target") or 0)), "auto_export": bool(body.get("auto_export", True)),
           "moments": [{"ranges": [{"start": m["start"], "end": m["end"]}], "scenario": m.get("scenario") or ""} for m in ms]}
    r = create_reels(body["day"], req)
    update(d, approved=sorted(pick), approved_day=body["day"])
    return r


# ------------------------------------------------------------------ Auto-clip: watch a live stream, make reels by itself

WATCH_CHUNK = 10 * 60      # look at the stream every 10 minutes of new footage
WATCH_OVERLAP = 60         # re-check the last minute of the previous chunk, so a moment on the edge isn't cut in half
WATCHERS: dict[str, dict] = {}   # id -> {"stop": Event, "thread": Thread, "awake": Popen}


def watch_dir() -> Path:
    from .config import DATA_DIR
    d = DATA_DIR / "live-watch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _keep_awake(on: bool, w: dict):
    """macOS: don't let the Mac fall asleep while a stream is being auto-clipped (the screen may still turn off)."""
    if on and system.IS_MAC and not w.get("awake"):
        try:
            # -w: caffeinate ends by itself when this server process ends (restart, crash, Ctrl+C) — never left behind
            w["awake"] = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except Exception:
            w["awake"] = None
    elif not on and w.get("awake"):
        w["awake"].terminate()
        w["awake"] = None


class _Stopped(Exception):
    pass


def _wait(stop, seconds: float) -> bool:
    """Sleep, but wake up at once when Stop is clicked. True = stop."""
    return stop.wait(seconds)


def _overlaps(m: dict, done: list) -> bool:
    return any(m["start"] < b - 5 and m["end"] > a + 5 for a, b in done)


def watcher(wid: str):
    d = watch_dir() / wid
    w = WATCHERS[wid]
    stop = w["stop"]
    _keep_awake(True, w)
    try:
        p = load(d)
        st, day = p["stream"], p["day"]
        if not st.get("clip_url"):                       # scheduled stream: wait until it goes live
            log(d, f"Waiting for {st.get('creator') or 'the stream'} to go live"
                   + (f" (scheduled {time.strftime('%H:%M', time.localtime(p['starts']))})" if p.get("starts") else "")
                   + ". Clipping starts by itself.")
            update(d, state="waiting")
            gave_up = time.time() + 24 * 3600
            while not stop.is_set() and time.time() < gave_up:
                starts = load(d).get("starts") or 0
                left = starts - time.time()
                # far away: look every 10 min; close to the start (or late): every minute
                nap = 600 if left > 900 else 60
                set_job(d, stage="ready", progress=1.0, task=None, span=None, next_check=time.time() + nap)
                try:
                    st = live.resolve(st["watch_url"])
                    break
                except live.Upcoming as e:
                    update(d, starts=e.info.get("starts"))
                except Exception:
                    pass
                if _wait(stop, nap):
                    break
            if stop.is_set():
                raise _Stopped()
            if not st.get("clip_url"):
                update(d, state="ended")
                log(d, "The stream didn't start within 24 hours. Auto-clip stopped.")
                return
            update(d, stream={**load(d)["stream"], **{k: st.get(k) for k in ("creator", "platform", "title", "clip_url", "thumbnail")}})
            log(d, f"{st.get('creator') or 'The stream'} is live! Auto-clipping begins.")
        per_chunk = max(1, round(p.get("per_hour", 4) * WATCH_CHUNK / 3600))
        src = {"type": "youtube", "url": st["clip_url"], "live": st["platform"] == "youtube"}
        update(d, state="watching", error=None)
        log(d, f"Auto-clipping {st['creator']} ({st['platform'].title()}): every {WATCH_CHUNK // 60} min of new stream, "
               f"up to {per_chunk} reel{'s' if per_chunk > 1 else ''} into “{day}”, exported automatically.")
        ended_checks = 0
        while not stop.is_set():
            p = load(d)
            try:
                head = live.stream_position(st)
            except Exception as e:
                log(d, f"Couldn't reach the stream ({str(e).splitlines()[0][:100]}); trying again in a minute.")
                if _wait(stop, 60):
                    break
                continue
            start = p.get("next_start")
            if start is None:                       # begin with the last chunk that's already streamed
                start = max(0.0, head - WATCH_CHUNK - 15)
                update(d, next_start=start)
            end = head - 15
            live_now = live.still_live(st)
            if end - start < WATCH_CHUNK and live_now:
                wait = max(30.0, WATCH_CHUNK - (end - start))
                set_job(d, stage="ready", progress=1.0, task=None, span=None, next_check=time.time() + wait)
                update(d, next_check=time.time() + wait, position=head)
                if _wait(stop, min(wait, 120)):       # check at least every 2 min whether the stream ended
                    break
                continue
            if end - start < 60:                     # stream ended and nothing new left to scan
                ended_checks += 1
                if ended_checks >= 2:
                    update(d, state="ended")
                    log(d, "The stream has ended. Auto-clipping stopped.")
                    break
                if _wait(stop, 60):
                    break
                continue
            # ---- scan the new part (queued with the other reels so only one heavy job runs at a time)
            a, b = max(0.0, start - WATCH_OVERLAP), end
            log(d, f"Scanning {fmt_time(a)}–{fmt_time(b)} for the best moments…")
            fut = PIPE.submit(_watch_scan, d, src, a, b, per_chunk, st["creator"], p.get("target") or 0)
            while not fut.done():
                if _wait(stop, 1):
                    media.cancel(rid(d))
                    break
            if stop.is_set():
                break
            try:
                found = fut.result()
            except Exception as e:
                log(d, f"Scan failed ({str(e).splitlines()[0][:150]}); will try this part again in a minute.")
                if _wait(stop, 60):
                    break
                continue
            done = [tuple(x) for x in (p.get("clipped") or [])]
            new = [m for m in found if not _overlaps(m, done)]
            if new:
                r = create_reels(day, {"source": {"type": "youtube", "url": st["clip_url"]}, "creator": st["creator"],
                                       "target": p.get("target") or 0, "auto_export": True,
                                       "moments": [{"ranges": [{"start": m["start"], "end": m["end"]}],
                                                    "scenario": m.get("scenario") or ""} for m in new]})
                p = load(d)
                reels = (p.get("reels") or []) + [{"folder": f, "start": m["start"], "end": m["end"], "scenario": m.get("scenario", "")}
                                                  for f, m in zip(r["created"], new)]
                update(d, reels=reels, clipped=done + [[m["start"], m["end"]] for m in new])
                log(d, f"Found {len(new)} moment{'s' if len(new) > 1 else ''}: " + "; ".join(
                    f"{fmt_time(m['start'])} {m.get('scenario') or 'big moment'}"[:90] for m in new) + " — making the reels now.")
            else:
                log(d, "Nothing strong enough in this part.")
            update(d, next_start=b, chunks=(p.get("chunks") or 0) + 1)
            if not live_now and b >= head - 30:
                update(d, state="ended")
                log(d, "The stream has ended and its last part is clipped. Auto-clipping stopped.")
                break
        else:
            pass
        if stop.is_set():
            update(d, state="stopped")
            log(d, "Stopped.")
    except _Stopped:
        update(d, state="stopped")
        log(d, "Stopped.")
    except Exception as e:
        traceback.print_exc()
        update(d, state="error", error=str(e)[:1500])
        log(d, "Stopped: " + str(e).splitlines()[0][:200])
    finally:
        _keep_awake(False, w)
        set_job(d, stage="ready", progress=1.0, task=None, span=None, next_check=None)
        WATCHERS.pop(wid, None)


def _watch_scan(d: Path, src: dict, a: float, b: float, count: int, creator: str, target: int) -> list:
    media.CTX.tag = rid(d)
    media.CTX.report = lambda info: report(d, info)
    media.CANCELLED.discard(rid(d))
    try:
        set_job(d, progress=0.0, task=None, span=None, started=time.time(), next_check=None,
                plan={"locate": (0.0, 0.9), "cut": (0.9, 1.0)})
        stage(d, "locate")
        scan = full_scan(d, src, {"start": a, "end": b})
        stage(d, "cut")
        found = story.locate(scan["words"], scan["profile"], scan["motion"], scan["duration"], [], count, creator, target)
        off = scan.get("offset", 0.0)
        for c in found:
            c["start"] = round(c["start"] + off, 1); c["end"] = round(c["end"] + off, 1)
        set_job(d, stage="ready", progress=1.0, task=None, span=None)
        # the scan copy is only needed once: don't let a long stream fill the disk
        from .config import DATA_DIR
        for f in (DATA_DIR / "cache" / source_key(src)).glob(f"window-{int(a)}-{int(b)}*.mp4"):
            f.unlink(missing_ok=True)
        return found
    finally:
        media.CTX.tag = None
        media.CTX.report = None


def _same_video(a: str | None, b: str | None) -> bool:
    """youtu.be/ID, youtube.com/watch?v=ID and youtube.com/live/ID are the same stream."""
    import re as _re
    def vid(u):
        m = _re.search(r"(?:youtu\.be/|[?&]v=|/live/)([\w-]{11})", u or "")
        return m.group(1) if m else (u or "").split("?")[0]
    return bool(a and b) and vid(a) == vid(b)


def start_watcher(wid: str):
    import threading as _th
    WATCHERS[wid] = {"stop": _th.Event(), "awake": None}
    t = _th.Thread(target=watcher, args=(wid,), daemon=True, name=f"watch-{wid}")
    WATCHERS[wid]["thread"] = t
    t.start()


@app.post("/api/live/watch")
def api_watch_start(body: dict = Body(...)):
    """Start auto-clipping a live stream: pass a stream from the Live tab, or {"url": any live link}."""
    st = body.get("stream")
    waiting = None
    if not st and body.get("url"):
        try:
            st = live.resolve(body["url"])
        except live.Upcoming as e:            # scheduled: wait for it to go live, then clip by itself
            waiting = e.info
            st = {"creator": e.info["creator"], "platform": "youtube", "handle": "", "title": e.info["title"],
                  "clip_url": None, "watch_url": body["url"], "thumbnail": e.info["thumbnail"]}
        except media.MediaError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"Couldn't open that link: {str(e).splitlines()[0][:200]}")
    if not st or not (st.get("clip_url") or waiting):
        raise HTTPException(400, "That stream can't be clipped (no recording of it is available).")
    for wid, w in WATCHERS.items():
        o = load(watch_dir() / wid).get("stream", {})
        if (st.get("clip_url") and o.get("clip_url") == st["clip_url"]) or \
                (waiting and _same_video(o.get("watch_url"), st["watch_url"])):
            raise HTTPException(409, "This stream is already being auto-clipped.")
    day = body.get("day") or ""
    if not day or not (reels_root() / day).is_dir():
        raise HTTPException(400, "Choose a day folder first (top right).")
    wid = time.strftime("%Y%m%d-%H%M%S") + "-" + analysis.uid()[:4]
    d = watch_dir() / wid
    d.mkdir()
    keep = {k: st.get(k) for k in ("creator", "platform", "handle", "title", "clip_url", "watch_url", "thumbnail")}
    save(d, {"stream": keep, "day": day, "per_hour": int(min(12, max(1, int(body.get("per_hour") or 4)))),
             "target": int(body.get("target") or 0), "state": "waiting" if waiting else "starting", "log": [],
             "created": time.time(), "reels": [], "clipped": [], "starts": (waiting or {}).get("starts")})
    start_watcher(wid)
    return {"id": wid, "waiting": bool(waiting), "starts": (waiting or {}).get("starts")}


@app.get("/api/live/watch")
def api_watch_list():
    out = []
    for d in sorted((x for x in watch_dir().iterdir() if x.is_dir()), reverse=True)[:20]:
        p = load(d)
        if not p:
            continue
        running = d.name in WATCHERS
        if not running and p.get("state") in ("watching", "starting", "waiting"):
            p["state"] = "stopped"
        j = JOBS.get(rid(d), {})
        out.append({"id": d.name, "stream": p.get("stream"), "day": p.get("day"), "state": p.get("state"),
                    "running": running, "reels": p.get("reels") or [], "chunks": p.get("chunks") or 0,
                    "position": p.get("position"), "next_check": j.get("next_check"), "error": p.get("error"),
                    "scanning": running and j.get("stage") in ("locate", "cut"), "progress": j.get("progress"),
                    "task": (j.get("task") or {}).get("label"), "log": (p.get("log") or [])[-6:],
                    "per_hour": p.get("per_hour"), "created": p.get("created"), "starts": p.get("starts")})
    return {"watchers": out}


@app.post("/api/live/watch/{wid}/stop")
def api_watch_stop(wid: str):
    w = WATCHERS.get(safe_name(wid))
    if w:
        w["stop"].set()
    return {"ok": True}


@app.on_event("startup")
def _resume_watchers():
    """Auto-clipping that was running when Reelmaker restarted (an update, ⟳ Restart) carries on by itself."""
    try:
        for d in watch_dir().iterdir():
            if not d.is_dir():
                continue
            p = load(d)
            if p.get("state") in ("watching", "starting", "waiting") and time.time() - p.get("updated", 0) < 6 * 3600:
                start_watcher(d.name)
    except Exception:
        traceback.print_exc()


@app.get("/api/state")
def state():
    root = reels_root()
    days = sorted([d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")],
                  key=lambda n: [int(x) if x.isdigit() else x for x in __import__("re").split(r"(\d+)", n)])
    s = settings()
    return {"root": str(root), "days": days, "last_day": s["last_day"] if s["last_day"] in days else "",
            "voices": VOICES, "default_voice": DEFAULT_VOICE, "ollama": ai.ollama_ok(), "ollama_model": OLLAMA_MODEL,
            "presets": presets(), "creator": s.get("creator", ""), "target": s.get("target", 0),
            "auto_export": s.get("auto_export", True), "voice_default": voice_default(), "ffmpeg": ffmpeg_status(),
            "platform": "mac" if system.IS_MAC else "windows" if system.IS_WIN else "other",
            "voice_default_saved": bool(s.get("voice_default")), "pronounce": ai.user_pronunciations()}


@app.put("/api/pronounce")
def set_pronounce(body: dict = Body(...)):
    """Text like "ISHOWSPEED = I Show Speed", one per line -> {written: said}."""
    table = {}
    for line in str(body.get("text", "")).splitlines()[:100]:
        name, sep, say = line.partition("=")
        if sep and name.strip() and say.strip():
            table[name.strip()[:60]] = say.strip()[:80]
    save_settings(pronounce=table)
    return {"pronounce": table}


@app.get("/api/system")
def system_usage(lite: bool = False):
    """CPU / GPU / memory / download speed, plus what Reelmaker is doing right now (for the usage bar)."""
    from . import sysmon
    names = {"queued": "Waiting in line", "locate": "Finding the moment", "fetch": "Downloading", "transcribe": "Transcribing",
             "analyze": "Finding webcam & reactions", "cut": "Cutting the story"}
    active = []
    for key, j in list(JOBS.items()):
        day, reel = key.split("/", 1)
        if j.get("render"):
            waiting = (j.get("render_phase") or "").startswith("Waiting")
            active.append({"day": day, "reel": reel, "kind": "render",
                           "label": ("Waiting to export" if waiting else "Exporting") if j["render"] == "final"
                           else ("Waiting to render preview" if waiting else "Rendering preview"),
                           "progress": j.get("render_progress") or 0.0, "waiting": waiting})
        elif j.get("stage") in names and j.get("stage") != "ready":
            t = j.get("task") or {}
            active.append({"day": day, "reel": reel, "kind": "pipeline", "label": t.get("label") or names[j["stage"]],
                           "progress": j.get("progress") or 0.0, "waiting": j.get("stage") == "queued",
                           "download_speed": t.get("speed") if t.get("unit") == "bytes" else None})
    active.sort(key=lambda a: (a["waiting"], a["kind"] != "render"))
    # lite: only "is anything running?" — the sampler (top/ioreg every second) runs only while something is
    # idle: nothing to measure. As soon as anything runs, the sampler starts (even on a "lite" check) so the
    # first real numbers arrive about a second later.
    return {**(sysmon.usage() if active else {}), "active": active}


RESTART_CODE = 75   # the launcher (./reelmaker) starts the server again when it exits with this code


def busy_jobs() -> list[str]:
    """Reels that are being made or rendered right now."""
    out = []
    for key, j in list(JOBS.items()):
        if j.get("render") or j.get("stage") in ("queued", "locate", "fetch", "transcribe", "analyze", "cut"):
            out.append(key.split("/", 1)[1])
    return out


@app.post("/api/restart")
def restart(force: bool = Body(False, embed=True)):
    """Restart Reelmaker (loads new code). Only when started by the launcher, which brings it straight back."""
    if not os.environ.get("REELMAKER_LAUNCHER"):
        raise HTTPException(409, "Reelmaker wasn't started with the launcher, so it can't restart itself. Press Ctrl+C in "
                                 "its Terminal window, then double-click “Start Reelmaker” on your Desktop.")
    busy = busy_jobs()
    if busy and not force:
        raise HTTPException(409, {"busy": busy})
    restart_soon()
    return {"restarting": True, "interrupted": busy}


def restart_soon(delay: float = 0.4) -> None:
    """Stop everything Reelmaker started and exit with the restart code; the launcher starts it again."""
    def go():
        time.sleep(delay)   # let the current answer reach the page first
        for procs in list(media.PROCS.values()):
            for pr in list(procs):
                try:
                    pr.kill()
                except Exception:
                    pass
        for stop in (ai.stop_voice_worker, lambda: __import__("app.sysmon", fromlist=["stop"]).stop()):
            try:
                stop()
            except Exception:
                pass
        os._exit(RESTART_CODE)
    threading.Thread(target=go, daemon=True).start()


# ------------------------------------------------------------------ updates (see app/updater.py)

UPDATE: dict = {"running": False, "log": [], "done": False, "error": None, "restarting": False, "result": None}


@app.get("/api/update")
def update_check(force: bool = False):
    from . import updater
    info = updater.check(force=force)
    info["can_restart"] = bool(os.environ.get("REELMAKER_LAUNCHER"))
    return info


@app.get("/api/update/status")
def update_status():
    return {**UPDATE, "log": UPDATE["log"][-40:]}


@app.post("/api/update/apply")
def update_apply(force: bool = Body(False, embed=True)):
    """Install the newest version in the background, then restart (when started by the launcher)."""
    from . import updater
    if UPDATE["running"]:
        raise HTTPException(409, "An update is already running.")
    busy = busy_jobs()
    if busy and not force:
        raise HTTPException(409, {"busy": busy})
    UPDATE.update(running=True, log=[], done=False, error=None, restarting=False, result=None)

    def say(line: str):
        import re as _re
        UPDATE["log"].append(_re.sub(r"\x1b\[[0-9;]*m", "", line))   # no terminal colour codes in the page

    def run():
        try:
            res = updater.apply(say)
            UPDATE["result"] = res
            if not res.get("up_to_date") and os.environ.get("REELMAKER_LAUNCHER"):
                say("Restarting Reelmaker onto the new version…")
                UPDATE["restarting"] = True
                restart_soon(1.5)
            elif not res.get("up_to_date"):
                say("Update installed. Close Reelmaker's window and start it again to use the new version.")
        except Exception as e:   # UpdateError or anything unexpected: show it, keep running the old version
            UPDATE["error"] = str(e)[:500]
            say("Update failed: " + str(e)[:300])
        finally:
            UPDATE.update(running=False, done=True)
    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


@app.get("/api/ui-version")
def ui_version():
    """build changes whenever the page's files change (an open tab offers a reload); restart_needed is true when
    the server's own code changed on disk after it started (only a restart of Reelmaker loads it)."""
    st = APP_DIR / "static"
    return {"build": int(max((f.stat().st_mtime for f in st.iterdir() if f.is_file()), default=0)),
            "api": API_VERSION, "restart_needed": _code_stamp() > CODE_STAMP + 1}


@app.post("/api/days")
def create_day(name: str = Body(..., embed=True)):
    n = safe_name(name)
    if not n:
        raise HTTPException(400, "Use letters, numbers and spaces for the folder name.")
    (reels_root() / n).mkdir(exist_ok=True)
    save_settings(last_day=n)
    return {"day": n}


@app.post("/api/last-day")
def last_day(day: str = Body(..., embed=True)):
    day_dir(day)
    save_settings(last_day=day)
    return {"ok": True}


@app.get("/api/days/{day}/reels")
def list_reels(day: str):
    out = []
    for d in sorted(day_dir(day).iterdir()):
        if (d / "project.json").exists():
            p = load(d)
            out.append({"folder": d.name, "name": p.get("name"), "state": p.get("state"),
                        "source": (p.get("source") or {}).get("title") or (p.get("source") or {}).get("url") or (p.get("source") or {}).get("path"),
                        "ranges": [f"{fmt_time(r['start'])}–{fmt_time(r['end'])}" for r in p.get("ranges", [])],
                        "drafts": len(p.get("drafts", [])), "final": bool(p.get("final")), "updated": p.get("updated"),
                        "progress": JOBS.get(rid(d), {}).get("progress"),
                        "rendering": JOBS.get(rid(d), {}).get("render"),
                        "render_progress": JOBS.get(rid(d), {}).get("render_progress"),
                        "render_phase": JOBS.get(rid(d), {}).get("render_phase")})
    return out


@app.post("/api/days/{day}/export-all")
def export_all(day: str, folders: list = Body(None, embed=True)):
    """Queue the final MP4 for every ready reel that has none yet (or just `folders`, if given)."""
    dd = day_dir(day)
    queued, skipped = [], []
    for d in sorted(dd.iterdir()):
        if not (d / "project.json").exists() or (folders and d.name not in folders):
            continue
        p, j = load(d), JOBS.get(rid(d), {})
        if p.get("state") != "ready":
            skipped.append({"folder": d.name, "why": "not ready yet" if p.get("state") != "error" else "stopped — retry it first"})
        elif j.get("render"):
            skipped.append({"folder": d.name, "why": "already rendering"})
        elif p.get("final") and not folders:
            skipped.append({"folder": d.name, "why": "already exported"})
        else:
            queue_render(d, True)
            queued.append(d.name)
    return {"queued": queued, "skipped": skipped}


@app.post("/api/days/{day}/retry-stopped")
def retry_stopped(day: str):
    """Restart every reel that stopped (cancelled, failed or interrupted by closing the app)."""
    dd = day_dir(day)
    restarted = []
    for d in sorted(dd.iterdir()):
        if (d / "project.json").exists() and load(d).get("state") == "error":
            media.CANCELLED.discard(rid(d))
            update(d, state="queued", error=None)
            set_job(d, stage="queued", progress=0, task=None)
            PIPE.submit(pipeline, d)
            restarted.append(d.name)
    return {"restarted": restarted}


@app.post("/api/reel/{day}/{reel}/apply-layout-all")
def apply_layout_all(day: str, reel: str):
    """Copy this reel's layout to every other reel in the day folder made from the same video."""
    d = reel_dir(day, reel)
    p = load(d)
    c = p.get("crops") or {}
    k0 = dict(c["keyframes"][0], t=None)
    updated = []
    for o, q in siblings(d, p.get("source") or {}):
        if q.get("state") != "ready":
            continue
        with LOCK:
            q = load(o)
            q["crops"] = {"layout": c["layout"], "keyframes": [dict(k0)], "edited": True, "preset": c.get("preset")}
            q.setdefault("caption_style", {})["y"] = None   # captions follow the new webcam line
            save(o, q)
        updated.append({"folder": o.name, "had_final": bool(q.get("final"))})
    with LOCK:
        p = load(d)
        p.setdefault("crops", {})["edited"] = True
        save(d, p)
    return {"updated": updated}


@app.post("/api/parse-ranges")
def api_parse_ranges(text: str = Body(..., embed=True)):
    try:
        return [{"start": a, "end": b, "label": f"{fmt_time(a)}–{fmt_time(b)}", "seconds": round(b - a, 1)}
                for a, b in parse_ranges(text)]
    except ValueError as e:
        raise HTTPException(400, str(e))


TIME_RE = r"(\d{1,2}(?::\d{1,2}){1,2}(?:\.\d+)?)"


@app.post("/api/parse-moments")
def parse_moments(text: str = Body(..., embed=True)):
    """Each line: optional time range(s) joined with +, then an optional description."""
    import re as _re
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        ranges, rest, err = [], line, None
        pat = _re.compile(r"^\s*\+?\s*" + TIME_RE + r"\s*(?:-|–|—|to)\s*" + TIME_RE)
        while True:
            m = pat.match(rest)
            if not m:
                break
            try:
                (a, b), = parse_ranges(f"{m.group(1)}-{m.group(2)}", max_len=3 * 3600)
                ranges.append({"start": a, "end": b, "label": f"{fmt_time(a)}–{fmt_time(b)}"})
            except ValueError as e:
                a, b = (sum(float(x) * 60 ** i for i, x in enumerate(reversed(t.split(":")))) for t in (m.group(1), m.group(2)))
                hint = ""
                if b - a > 3 * 3600 and m.group(2).count(":") == 2:
                    hint = f" “{m.group(2)}” reads as {fmt_time(b)} (hours:minutes:seconds). Did you mean {m.group(2).rsplit(':', 1)[0]}?"
                err = str(e) + hint
            rest = rest[m.end():]
        desc = rest.strip(" -–—:,")
        if _re.search(TIME_RE, desc) and not ranges and not err:
            err = "Couldn't read the times. Use a format like 10:28-11:35."
        note = None
        if not err and len(ranges) == 1 and ranges[0]["end"] - ranges[0]["start"] > 600:
            mins = (ranges[0]["end"] - ranges[0]["start"]) / 60
            note = (f"{mins:.0f} min range — the app searches it for the best moment"
                    + (" matching your description" if desc else "") + " and clips only that.")
        elif not err and sum(r["end"] - r["start"] for r in ranges) > 600:
            err = "Joined ranges (with +) can be 10 minutes at most in total. Use one long range to let the app search it."
        out.append({"line": n, "ranges": ranges, "scenario": desc, "error": err, "note": note})
    return out


@app.post("/api/pick-file")
def pick_file():
    return {"path": system.pick_file("video")}


@app.post("/api/days/{day}/reels")
def create_reels(day: str, body: dict = Body(...)):
    dd = day_dir(day)
    src = body.get("source", {})
    if src.get("type") == "youtube":
        url = (src.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "Paste a full YouTube link (https://…).")
        source = {"type": "youtube", "url": url}
    elif src.get("type") == "file":
        path = Path((src.get("path") or "").strip()).expanduser()
        if not path.is_file():
            raise HTTPException(400, f"File not found: {path}")
        source = {"type": "file", "path": str(path), "title": path.name}
    else:
        raise HTTPException(400, "Choose a YouTube link or a local file.")
    creator = (body.get("creator") or "").strip()[:40]
    auto_export = bool(body.get("auto_export", True))
    t_raw = int(body.get("target") or 0)
    target = 0 if t_raw <= 0 else int(min(90, max(15, t_raw)))   # 0 = auto: the moment decides the length
    scenarios = [s.strip() for s in (body.get("scenarios") or []) if s and s.strip()][:10]
    ranges = body.get("ranges") or []
    win = None
    w = body.get("window") or {}
    if w.get("start") is not None and w.get("end") is not None:
        ws_, we_ = float(w["start"]), float(w["end"])
        if we_ - ws_ < 60:
            raise HTTPException(400, "The search window must be at least 1 minute long.")
        win = {"start": ws_, "end": we_}
    jobs = []  # (ranges, scenario, batch_index)
    moments = body.get("moments")
    if moments:
        timed = [m for m in moments if m.get("ranges")]
        untimed = [m.get("scenario", "") for m in moments if not m.get("ranges")]
        scenarios = [s for s in untimed if s]
        for m in timed:
            rr = [{"start": float(r["start"]), "end": float(r["end"])} for r in m["ranges"]]
            if len(rr) == 1 and rr[0]["end"] - rr[0]["start"] > 600:
                sc = m.get("scenario", "")
                jobs.append(([], sc, 0, {"batch": analysis.uid(), "window": rr[0],
                                         "batch_info": {"scenarios": [sc] if sc else [], "count": 1}}))
                continue
            jobs.append((rr, m.get("scenario", ""), None))
        for k, sc in enumerate(untimed):
            jobs.append(([], sc, k))
    elif ranges:
        groups: dict[int, list] = {}
        for r in ranges:
            groups.setdefault(int(r.get("reel", 1)), []).append({"start": float(r["start"]), "end": float(r["end"])})
        for k, g in enumerate(sorted(groups)):
            jobs.append((groups[g], scenarios[k] if k < len(scenarios) else "", None))
    if not jobs:
        n = len(scenarios) or int(min(10, max(1, int(body.get("count") or 3))))
        for k in range(n):
            jobs.append(([], scenarios[k] if k < len(scenarios) else "", k))
    batch = analysis.uid()
    existing = [int(x.name.split("-")[1]) for x in dd.glob("reel-*") if x.name.split("-")[1].isdigit()]
    n = max(existing, default=0)
    created = []
    for job in jobs:
        rngs, scen, bi = job[:3]
        own = job[3] if len(job) > 3 else None     # a long range: searched on its own
        n += 1
        label = slug(scen)[:24] if scen else ""
        if rngs and not scen:
            label = ""
        folder = f"reel-{n:02d}" + (f"-{label}" if label else "")
        d = dd / folder
        d.mkdir()
        p = {"version": 2, "name": scen[:60] or f"Reel {n}", "day": day, "created": time.time(), "source": source,
             "ranges": rngs, "scenario": scen, "creator": creator, "target": target, "state": "queued", "log": [],
             "auto_export": auto_export,
             "batch": batch, "batch_index": bi, "window": win if bi is not None else None,
             "batch_info": {"scenarios": scenarios, "count": sum(1 for j in jobs if j[2] is not None and len(j) == 3)},
             "caption_style": {"size": 80, "caps": True, "hook_size": 70, "highlight": "#FFE600", "hook_color": "#FF5FA2"}}
        if own:
            p.update(own)
        save(d, p)
        set_job(d, stage="queued", progress=0)
        PIPE.submit(pipeline, d)
        created.append(folder)
    save_settings(last_day=day, creator=creator, target=target, auto_export=auto_export)
    return {"created": created}


# ------------------------------------------------------------------ API: a reel

@app.get("/api/reel/{day}/{reel}")
def get_reel(day: str, reel: str):
    d = reel_dir(day, reel)
    with LOCK:
        p = load(d)
        if follow_default_voice(p):
            save(d, p)
    p["job"] = JOBS.get(rid(d), {})
    p["folder_path"] = str(d)
    try:
        p["duration"] = round(render.total_duration(p), 2) if p.get("segments") else 0
    except Exception:
        p["duration"] = 0
    return p


@app.put("/api/reel/{day}/{reel}")
def put_reel(day: str, reel: str, body: dict = Body(...)):
    d = reel_dir(day, reel)
    with LOCK:
        p = load(d)
        for k, v in body.items():
            if k in EDITABLE:
                p[k] = v
        save(d, p)
    return {"ok": True, "duration": round(render.total_duration(p), 2) if p.get("segments") else 0}


def trash(path: Path) -> None:
    """Move to the Trash / Recycle Bin (recoverable) instead of deleting outright."""
    try:
        system.trash(path)
    except Exception as e:
        raise HTTPException(500, "Couldn't move it to the " + ("Trash" if system.IS_MAC else "Recycle Bin") + f": {str(e)[:200]}")


@app.post("/api/reel/{day}/{reel}/cancel")
def cancel_reel(day: str, reel: str):
    d = reel_dir(day, reel)
    media.cancel(rid(d))
    if load(d).get("state") == "queued":
        update(d, state="error", error="Cancelled before it started.")
    return {"ok": True}


@app.delete("/api/reel/{day}/{reel}")
def delete_reel(day: str, reel: str):
    d = reel_dir(day, reel)
    media.cancel(rid(d))
    if JOBS.get(rid(d), {}).get("render"):
        raise HTTPException(409, "Wait for the render to finish, then delete.")
    trash(d)
    JOBS.pop(rid(d), None)
    return {"ok": True}


@app.delete("/api/reel/{day}/{reel}/draft/{name}")
def delete_draft(day: str, reel: str, name: str):
    d = reel_dir(day, reel)
    f = (d / "drafts" / name)
    if "/" in name or not f.is_file():
        raise HTTPException(404, "Draft not found")
    trash(f)
    with LOCK:
        p = load(d)
        p["drafts"] = [x for x in p.get("drafts", []) if x["file"] != f"drafts/{name}"]
        save(d, p)
    return {"drafts": p["drafts"]}


@app.post("/api/reel/{day}/{reel}/retry")
def retry(day: str, reel: str):
    d = reel_dir(day, reel)
    if load(d).get("state") != "error":
        return {"ok": True}   # already queued or running: don't start it twice
    media.CANCELLED.discard(rid(d))
    update(d, state="queued", error=None)
    set_job(d, stage="queued", progress=0, task=None)
    PIPE.submit(pipeline, d)
    return {"ok": True}


@app.post("/api/reel/{day}/{reel}/suggestion")
def apply_suggestion(day: str, reel: str, id: str = Body(..., embed=True), undo: bool = Body(False, embed=True)):
    d = reel_dir(day, reel)
    with LOCK:
        p = load(d)
        s = next((x for x in p.get("suggestions", []) if x["id"] == id), None)
        if not s:
            raise HTTPException(404, "Suggestion not found")
        segs = p["segments"]
        if s["kind"] in ("extend_start", "extend_end"):
            mine = [x for x in segs if x.get("range") == s["range"]]
            if not mine:
                raise HTTPException(400, "That range has no segments any more.")
            if s["kind"] == "extend_start":
                first = min(mine, key=lambda x: x["start"])
                if undo:
                    first["start"] = s["end"]
                else:
                    first["start"] = min(first["start"], s["start"])
            else:
                last = max(mine, key=lambda x: x["end"])
                if undo:
                    last["end"] = s["start"]
                else:
                    last["end"] = max(last["end"], s["end"])
        else:  # cut
            if undo:
                # re-join the two halves around the cut
                a = next((x for x in segs if x["piece"] == s["piece"] and abs(x["end"] - s["start"]) < 0.01), None)
                b = next((x for x in segs if x["piece"] == s["piece"] and abs(x["start"] - s["end"]) < 0.01), None)
                if a and b:
                    a["end"] = b["end"]
                    segs.remove(b)
            else:
                new = []
                for x in segs:
                    if x["piece"] == s["piece"] and x["start"] < s["end"] and x["end"] > s["start"]:
                        if x["start"] < s["start"] - 0.05:
                            new.append({**x, "end": s["start"]})
                        if x["end"] > s["end"] + 0.05:
                            new.append({**x, "id": analysis.uid(), "start": s["end"]})
                    else:
                        new.append(x)
                p["segments"] = new
        s["applied"] = not undo
        save(d, p)
    return get_reel(day, reel)


@app.post("/api/clean-file")
def clean_file(path: str = Body(..., embed=True)):
    """Remove all metadata from any video. Writes '<name>-clean.mp4' next to it; the original is not changed."""
    from . import meta
    src = Path(path).expanduser()
    if not src.is_file():
        raise HTTPException(400, f"File not found: {src}")
    before = meta.check(src)
    out = src.with_name(src.stem + "-clean.mp4")
    try:
        meta.clean(src, out)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    after = meta.check(out)
    return {"output": str(out), "removed": list(before["leftover"].items())[:20], "ok": after["ok"],
            "leftover": after["leftover"], "read_by": after["read_by"]}


@app.post("/api/reveal-path")
def reveal_path(path: str = Body(..., embed=True)):
    if Path(path).exists():
        system.reveal(path, select=True)
    return {"ok": True}


@app.post("/api/pick-audio")
def pick_audio():
    return {"path": system.pick_file("audio")}


@app.post("/api/voice-ref")
def set_voice_ref(path: str = Body("", embed=True)):
    if path and not Path(path).is_file():
        raise HTTPException(400, "File not found")
    save_settings(voice_ref=path or None)
    return {"voice_ref": path or None}


@app.post("/api/voice-default")
def set_voice_default(body: dict = Body(...)):
    """Save (or with {"reset": true} clear) the voice tone new reels start with."""
    if body.get("reset"):
        save_settings(voice_default=None)
        return voice_default()
    v = {}
    if body.get("voice") in VOICES or body.get("voice") == "cb":
        v["voice"] = body["voice"]
    for k, lo, hi in (("energy", 0.3, 1.2), ("speed", 0.85, 1.3), ("duck_db", -24, 0)):
        if isinstance(body.get(k), (int, float)):
            v[k] = round(min(hi, max(lo, float(body[k]))), 2)
    ref = body.get("voice_ref")
    if ref and not Path(ref).is_file():
        raise HTTPException(400, "The voice sample file wasn't found.")
    v["voice_ref"] = ref or None
    save_settings(voice_default=v)
    return voice_default()


@app.post("/api/days/{day}/apply-voice")
def apply_voice_all(day: str, body: dict = Body(...)):
    """Give every reel in the day folder this voice tone (their hook text stays as it is)."""
    dd = day_dir(day)
    vals = {k: body[k] for k in VOICE_KEYS if k in body}
    changed = []
    for d in sorted(dd.iterdir()):
        if not (d / "project.json").exists():
            continue
        with LOCK:
            p = load(d)
            if p.get("state") != "ready" or not p.get("hook"):
                continue
            if all(p["hook"].get(k) == v for k, v in vals.items()):
                continue
            p["hook"].update(vals)
            p["hook"]["voice_custom"] = True   # you picked this voice for them on purpose
            save(d, p)
        changed.append({"folder": d.name, "had_final": bool(p.get("final"))})
    return {"updated": changed}


@app.get("/api/hook-ai")
def hook_ai_state(models: bool = False):
    from . import online_ai
    s = settings()
    key, src = online_ai.get_key()
    out = {"mode": s.get("hook_ai", "local"), "has_key": bool(key), "key_source": src, "secret_store": system.SECRET_STORE,
           "model": s.get("groq_model") or online_ai.PREFERRED_MODELS[0], "models": [], "error": None}
    if models and key:
        try:
            out["models"] = online_ai.list_models(key)
            out["model"] = online_ai.pick_model(out["models"], s.get("groq_model"))
        except online_ai.OnlineAIError as e:
            out["error"] = str(e)
    return out


@app.post("/api/hook-ai")
def set_hook_ai(mode: str = Body("local", embed=True), model: str = Body("", embed=True)):
    if mode not in ("local", "groq"):
        raise HTTPException(400, "Unknown hook writer.")
    save_settings(hook_ai=mode, **({"groq_model": model} if model else {}))
    return hook_ai_state(models=False)


@app.post("/api/hook-ai/key")
def set_groq_key(key: str = Body(..., embed=True)):
    """Check a Groq key with Groq, then keep it in the Keychain. Turns the online writer on."""
    from . import online_ai
    try:
        models = online_ai.list_models(key.strip())   # proves the key works, costs nothing
        if not models:
            raise online_ai.OnlineAIError("This Groq account has no text models available.")
        online_ai.save_key(key)
    except online_ai.OnlineAIError as e:
        raise HTTPException(400, str(e))
    s = settings()
    save_settings(hook_ai="groq", groq_model=online_ai.pick_model(models, s.get("groq_model")))
    return hook_ai_state(models=True)


@app.delete("/api/hook-ai/key")
def delete_groq_key():
    from . import online_ai
    online_ai.delete_key()
    save_settings(hook_ai="local")
    return hook_ai_state(models=False)


@app.get("/api/local-audio")
def local_audio(path: str):
    f = Path(path)
    if f.suffix.lower() not in (".wav", ".mp3", ".m4a", ".aac", ".aiff", ".aif") or not f.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(f)


@app.post("/api/reel/{day}/{reel}/reset-cut")
def reset_cut(day: str, reel: str):
    d = reel_dir(day, reel)
    with LOCK:
        p = load(d)
        p["segments"] = [{"id": analysis.uid(), "piece": i, "range": i, "start": r["start"], "end": r["end"], "enabled": True}
                         for i, r in enumerate(p["ranges"])]
        for s in p.get("suggestions", []):
            s["applied"] = False
        save(d, p)
    return get_reel(day, reel)


@app.post("/api/reel/{day}/{reel}/hooks")
def regen_hooks(day: str, reel: str, idea: str = Body("", embed=True), apply_best: bool = Body(False, embed=True)):
    """New hook ideas. apply_best: also put the best one in the reel, unless you typed your own hook there."""
    d = reel_dir(day, reel)
    p = load(d)
    if not ai.ollama_ok() and not story.online_on():
        raise HTTPException(503, "The hook AI (Ollama) isn't running. Restart Reelmaker, or switch on the free online writer (Groq) in the Hook tab.")
    segs = [s for s in p.get("segments", []) if s.get("enabled", True)]
    said = " ".join(w["w"] for w in p.get("words", []) if any(s["piece"] == w["piece"] and s["start"] <= w["s"] <= s["end"] for s in segs))
    idea = (idea or "").strip()[:300]
    hooks = story.write_hooks(p.get("scenario", ""), p.get("creator", ""), said[:1500], (p.get("hook") or {}).get("story", ""), idea,
                              avoid=other_hooks(d) + list((p.get("hook") or {}).get("options") or []))
    if not hooks:
        raise HTTPException(502, "Couldn't write hooks this time — try again.")
    with LOCK:
        p = load(d)
        k = p["hook"]
        old = [o.upper() for o in (k.get("options") or [])]
        typed_own = (k.get("screen") or "").strip() and (k.get("screen") or "").strip().upper() not in old
        k.update(options=hooks, idea=idea, source=story.HOOK_INFO.get("source", "local"),
                 writer_model=story.HOOK_INFO.get("model"), writer_note=story.HOOK_INFO.get("note"))
        if apply_best and not typed_own:
            k["screen"] = hooks[0]
            if k.get("linked", True):
                k["voice_text"] = hooks[0]
        save(d, p)
    return {**p["hook"], "applied": bool(apply_best and not typed_own)}


@app.post("/api/reel/{day}/{reel}/tts")
def tts(day: str, reel: str, body: dict = Body(...)):
    d = reel_dir(day, reel)
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Write the voiceover line first.")
    try:
        path, dur = ai.synth(text, body.get("voice") or DEFAULT_VOICE, float(body.get("speed", 1.05)), d / "voice",
                             body.get("ref"), float(body.get("energy", 0.65)))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"url": f"/media/{day}/{reel}/voice/{path.name}", "duration": round(dur, 2)}


@app.post("/api/reel/{day}/{reel}/captions/rebuild")
def rebuild_captions(day: str, reel: str):
    d = reel_dir(day, reel)
    with LOCK:
        p = load(d)
        caps = []
        for i in range(len(p.get("pieces", []))):
            for c in analysis.build_captions([w for w in p.get("words", []) if w["piece"] == i]):
                c["piece"] = i
                caps.append(c)
        p["captions"] = caps
        save(d, p)
    return caps


@app.post("/api/reel/{day}/{reel}/render")
def start_render(day: str, reel: str, final: bool = Body(False, embed=True)):
    d = reel_dir(day, reel)
    j = JOBS.get(rid(d), {})
    if j.get("render"):
        raise HTTPException(409, "Already rendering this reel.")
    if load(d).get("state") != "ready":
        raise HTTPException(409, "This reel isn't ready yet.")
    queue_render(d, final)
    return {"ok": True}


@app.post("/api/reel/{day}/{reel}/reveal")
def reveal(day: str, reel: str, file: str = Body("", embed=True)):
    d = reel_dir(day, reel)
    target = (d / file).resolve() if file else d
    if d.resolve() not in target.parents and target != d.resolve():
        raise HTTPException(400, "Bad path")
    system.reveal(target, select=bool(file))
    return {"ok": True}


# ------------------------------------------------------------------ presets

@app.post("/api/presets")
def save_preset(body: dict = Body(...)):
    name = safe_name(body.get("name", ""))
    if not name:
        raise HTTPException(400, "Give the preset a name.")
    pr = presets()
    pr["presets"][name] = {"layout": body["layout"], "face": body["face"], "game": body["game"]}
    if body.get("channel"):
        pr["channel_default"][body["channel"]] = name
    write_json(PRESETS_FILE, pr)
    return pr


@app.delete("/api/presets/{name}")
def delete_preset(name: str):
    pr = presets()
    pr["presets"].pop(name, None)
    pr["channel_default"] = {k: v for k, v in pr["channel_default"].items() if v != name}
    write_json(PRESETS_FILE, pr)
    return pr


# ------------------------------------------------------------------ files

@app.get("/media/{day}/{reel}/{path:path}")
def media_file(day: str, reel: str, path: str):
    d = reel_dir(day, reel).resolve()
    f = (d / path).resolve()
    if d not in f.parents or not f.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(f)


app.mount("/", StaticFiles(directory=APP_DIR / "static", html=True), name="static")
