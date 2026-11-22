"""Turn a reel project into a 1080x1920 video: cut, crop, captions, voiceover, ducking, export, verify."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import meta
from .ai import synth
from .config import CAPTION_FONT, FONTS_DIR, HALF_H, OUT_H, OUT_W, tool
from .media import MediaError, probe, target_fps


# ------------------------------------------------------------------ timeline

def crop_at(crops: dict, t: float) -> dict:
    kfs = crops["keyframes"]
    cur = kfs[0]
    for kf in kfs[1:]:
        if kf.get("t") is not None and kf["t"] <= t + 1e-3:
            cur = kf
    return cur


def timeline(p: dict) -> list[dict]:
    """Ordered output items: {piece, start, end, kind, crop, out}."""
    raw = []
    co = p.get("cold_open") or {}
    if co.get("enabled") and co.get("end", 0) > co.get("start", 0):
        raw.append({"piece": co["piece"], "start": co["start"], "end": co["end"], "kind": "cold"})
    for s in p["segments"]:
        if s.get("enabled", True) and s["end"] - s["start"] >= 0.1:
            raw.append({"piece": s["piece"], "start": s["start"], "end": s["end"], "kind": "main"})
    cuts = sorted(kf["t"] for kf in p["crops"]["keyframes"][1:] if kf.get("t") is not None)
    items = []
    for it in raw:
        pts = [it["start"]] + [c for c in cuts if it["start"] + 0.05 < c < it["end"] - 0.05] + [it["end"]]
        for a, b in zip(pts, pts[1:]):
            items.append({**it, "start": a, "end": b, "crop": crop_at(p["crops"], a)})
    t = 0.0
    for it in items:
        it["out"] = t
        t += it["end"] - it["start"]
    return items


def total_duration(p) -> float:
    return sum(i["end"] - i["start"] for i in timeline(p))


# ------------------------------------------------------------------ captions (ASS)

def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(s: str) -> str:
    return s.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


TOP_MIN = 576   # the webcam band is never less than 30% of the reel
SPLITS = ("split", "splitfit")


def split_top(face: dict, W: int, H: int) -> int:
    """Height of the webcam band: the webcam at full width in its own shape (so no blur bars and no cropping),
    kept between 30% and 50% of the reel. A tall webcam gets the full half."""
    fw, fh = face["w"] * W, face["h"] * H
    if fw <= 0 or fh <= 0:
        return HALF_H
    return max(TOP_MIN, min(HALF_H, int(round(OUT_W * fh / fw / 2) * 2)))


def fit_game(game: dict, W: int, H: int, area_h: int):
    """Gameplay crop with exactly the shape of the space it fills (never stretched), centred on the game box,
    keeping the box's height. Returns pixel (w, h, x, y)."""
    target = OUT_W / area_h
    cx, cy = (game["x"] + game["w"] / 2) * W, (game["y"] + game["h"] / 2) * H
    h = game["h"] * H
    w = h * target
    if w > W:
        w, h = W, W / target
    if h > H:
        h, w = H, H * target
    w, h = max(2, int(round(w / 2) * 2)), max(2, int(round(h / 2) * 2))
    x = int(round(min(max(0, cx - w / 2), W - w)))
    y = int(round(min(max(0, cy - h / 2), H - h)))
    return w, h, x, y


def layout_top(p) -> int:
    """Webcam band height for the reel's first layout (captions sit on that line)."""
    k = p["crops"]["keyframes"][0]
    pc = (p.get("pieces") or [{"width": 1920, "height": 1080}])[0]
    return split_top(k["face"], pc["width"], pc["height"])


def caption_y(p) -> int:
    st = p.get("caption_style", {})
    if st.get("y"):
        return int(st["y"])
    return layout_top(p) if p["crops"]["layout"] in SPLITS else 1560


def _ass_color(hexcol: str, alpha: str = "00") -> str:
    h = (hexcol or "#FFE600").lstrip("#")
    if len(h) != 6:
        h = "FFE600"
    return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _split_two(text: str) -> tuple[str, str]:
    words = text.split()
    if len(words) < 3:
        return text, ""
    best, bi = 1e9, 1
    for i in range(1, len(words)):
        d = abs(len(" ".join(words[:i])) - len(" ".join(words[i:])))
        if d < best:
            best, bi = d, i
    return " ".join(words[:bi]), " ".join(words[bi:])


def build_ass(p: dict, items: list[dict], hook_end: float, text: bool = True) -> str:
    """text=False: no hook or caption text (previews: the page draws them live over the player)."""
    st = p.get("caption_style", {})
    size = int(st.get("size", 80))
    caps_on = st.get("caps", True)
    y = caption_y(p)
    hi = _ass_color(st.get("highlight", "#FFE600"))
    hook_size = int(st.get("hook_size", 70))
    hook_col = _ass_color(st.get("hook_color", "#FF5FA2"))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{CAPTION_FONT},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,9,4,5,140,140,0,1
Style: Hook,{CAPTION_FONT},{hook_size},&H00FFFFFF,&H00FFFFFF,&H28101010,&H28101010,-1,0,0,0,100,100,0,0,3,22,0,5,100,100,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    pop = "\\fscx70\\fscy70\\t(0,80,\\fscx110\\fscy110)\\t(80,150,\\fscx100\\fscy100)"
    lines = []
    if not text:
        return head
    hook = p.get("hook", {})
    if hook.get("enabled") and hook.get("screen", "").strip():
        l1, l2 = _split_two(_esc(hook["screen"].strip().upper()))
        txt = l1 + (f"\\N{{\\c{hook_col}}}{l2}" if l2 else "")
        lines.append(f"Dialogue: 1,{_ts(0.05)},{_ts(hook_end)},Hook,,0,0,0,,{{\\an5\\pos({OUT_W // 2},{y}){pop}}}{txt}")
    mains = [i for i in items if i["kind"] == "main"]
    for c in p.get("captions", []):
        words = c["text"].split()
        if not words:
            continue
        emph = set(c.get("emph", []))
        # per-word timing: from the transcript if the wording still matches, otherwise spread evenly
        wt = c.get("words") or []
        if len(wt) != len(words):
            d = (c["end"] - c["start"]) / len(words)
            wt = [{"s": c["start"] + k * d, "e": c["start"] + (k + 1) * d} for k in range(len(words))]
        shown = [_esc(w.upper() if caps_on else w) for w in words]
        for it in mains:
            if c.get("piece", it["piece"]) != it["piece"]:
                continue
            s, e = max(c["start"], it["start"]), min(c["end"], it["end"])
            if e - s < 0.12:
                continue
            m = lambda t: it["out"] + min(max(t, it["start"]), it["end"]) - it["start"]
            cs, ce = max(m(s), hook_end), m(e)
            if ce - cs < 0.12:
                continue
            for k in range(len(words)):
                ks = max(m(wt[k]["s"]), cs) if k else cs
                ke = m(wt[k + 1]["s"]) if k + 1 < len(words) else ce
                ke = min(max(ke, ks), ce)
                if ke - ks < 0.04:
                    continue
                parts = []
                for j, w in enumerate(shown):
                    if j == k:  # the word being said: highlight colour + a little bounce
                        parts.append(f"{{\\c{hi}\\fscx118\\fscy118\\t(0,90,\\fscx100\\fscy100)}}{w}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                    elif j in emph:
                        parts.append(f"{{\\c{hi}}}{w}{{\\c&H00FFFFFF&}}")
                    else:
                        parts.append(w)
                lead = pop if k == 0 else ""
                lines.append(f"Dialogue: 0,{_ts(ks)},{_ts(ke)},Cap,,0,0,0,,{{\\an5\\pos({OUT_W // 2},{y}){lead}}}" + " ".join(parts))
    return head + "\n".join(lines) + "\n"


# ------------------------------------------------------------------ filter graph

def _px(c: dict, W: int, H: int):
    w = max(2, int(round(c["w"] * W / 2) * 2))
    h = max(2, int(round(c["h"] * H / 2) * 2))
    x = min(max(0, int(round(c["x"] * W))), W - w)
    y = min(max(0, int(round(c["y"] * H))), H - h)
    return w, h, x, y


def build_graph(p, items, fps, vo_index, vo_dur, final):
    g = []
    layout = p["crops"]["layout"]
    for i, it in enumerate(items):
        pc = p["pieces"][it["piece"]]
        W, H = pc["width"], pc["height"]
        d = it["end"] - it["start"]
        gw, gh, gx, gy = _px(it["crop"]["game"], W, H)
        if layout in SPLITS:
            fw, fh, fx, fy = _px(it["crop"]["face"], W, H)
            top = split_top(it["crop"]["face"], W, H)
            area = OUT_H - top
            if fw / fh >= OUT_W / top:   # wide webcam: full width, centered vertically
                sw, sh = OUT_W, int(round(OUT_W * fh / fw / 2) * 2)
            else:                       # tall webcam: full height
                sw, sh = int(round(top * fw / fh / 2) * 2), top
            g.append(f"[{i}:v]setpts=PTS-STARTPTS,split=2[a{i}][b{i}]")
            g.append(f"[a{i}]crop={fw}:{fh}:{fx}:{fy},split=2[c{i}][d{i}]")
            g.append(f"[c{i}]scale={OUT_W}:{top}:force_original_aspect_ratio=increase,crop={OUT_W}:{top},boxblur=20:2,eq=brightness=-0.15,setsar=1[cb{i}]")
            g.append(f"[d{i}]scale={sw}:{sh}:flags=lanczos,setsar=1[cf{i}]")
            g.append(f"[cb{i}][cf{i}]overlay={(OUT_W - sw) // 2}:{(top - sh) // 2}[f{i}]")
            if layout == "splitfit":   # the whole game screen, uncropped, over a blurred fill of itself
                fh2 = int(round(OUT_W * H / W / 2) * 2)
                g.append(f"[b{i}]split=2[gb{i}][gf{i}]")
                g.append(f"[gb{i}]scale=-2:{area},crop={OUT_W}:{area},boxblur=24:2,eq=brightness=-0.15,setsar=1[gbb{i}]")
                g.append(f"[gf{i}]scale={OUT_W}:{fh2}:flags=lanczos,setsar=1[gff{i}]")
                g.append(f"[gbb{i}][gff{i}]overlay=0:{(area - fh2) // 2}[g{i}]")
            else:
                cw, ch, cx, cy = fit_game(it["crop"]["game"], W, H, area)
                g.append(f"[b{i}]crop={cw}:{ch}:{cx}:{cy},scale={OUT_W}:{area}:flags=lanczos,setsar=1[g{i}]")
            g.append(f"[f{i}][g{i}]vstack=2,fps={fps},format=yuv420p[v{i}]")
        else:  # gameplay only: cropped gameplay in the middle over a blurred fill
            g.append(f"[{i}:v]setpts=PTS-STARTPTS,split=2[a{i}][b{i}]")
            g.append(f"[a{i}]scale=-2:{OUT_H},crop={OUT_W}:{OUT_H},boxblur=24:2,eq=brightness=-0.12,setsar=1[bg{i}]")
            cw, ch, cx, cy = fit_game(it["crop"]["game"], W, H, HALF_H)
            g.append(f"[b{i}]crop={cw}:{ch}:{cx}:{cy},scale={OUT_W}:{HALF_H}:flags=lanczos,setsar=1[fg{i}]")
            g.append(f"[bg{i}][fg{i}]overlay=0:{(OUT_H - HALF_H) // 2},fps={fps},format=yuv420p[v{i}]")
        if pc.get("has_audio", True):
            g.append(f"[{i}:a]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                     f"afade=t=in:d=0.02,afade=t=out:st={max(0, d - 0.03):.3f}:d=0.03[a{i}x]")
        else:
            g.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{d:.3f}[a{i}x]")
    n = len(items)
    g.append("".join(f"[v{i}][a{i}x]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    from .system import ffmpeg_filter_path
    g.append(f"[vc]subtitles=captions.ass:fontsdir='{ffmpeg_filter_path(FONTS_DIR)}'[vout]")
    if vo_index is not None:
        gain = 10 ** (float(p["hook"].get("duck_db", -12)) / 20)
        D = 0.15 + vo_dur
        g.append(f"[ac]volume='if(lt(t,{D:.3f}),{gain:.4f},if(lt(t,{D + 0.5:.3f}),{gain:.4f}+(1-{gain:.4f})*(t-{D:.3f})/0.5,1))':eval=frame[ad]")
        g.append(f"[{vo_index}:a]aresample=48000,aformat=channel_layouts=stereo,adelay=150|150,volume=1.4[vo]")
        g.append("[ad][vo]amix=inputs=2:normalize=0:duration=first[am]")
        last = "am"
    else:
        last = "ac"
    g.append(f"[{last}]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
    return ";\n".join(g)


# ------------------------------------------------------------------ render

LAST_META: dict = {}   # what the last render showed: hook timing + the cut's timeline (one render runs at a time)


def render(reel_dir: Path, p: dict, final: bool, progress=lambda f: None) -> Path:
    items = timeline(p)
    if not items:
        raise MediaError("The cut is empty — enable at least one segment.")
    total = items[-1]["out"] + items[-1]["end"] - items[-1]["start"]
    fps = target_fps(max(pc["fps"] for pc in p["pieces"]))

    def step(f, phase):  # progress(fraction, phase text); older callers take one argument
        try:
            progress(f, phase)
        except TypeError:
            progress(f)

    hook = p.get("hook", {})
    vo_path, vo_dur = None, 0.0
    step(0.0, "Getting ready…")
    if hook.get("enabled") and hook.get("voice_file") and Path(hook["voice_file"]).is_file():
        import soundfile as sf
        vo_path = Path(hook["voice_file"])
        vo_dur = sf.info(vo_path).duration
    elif hook.get("enabled") and hook.get("voice_text", "").strip():
        step(0.0, "Making the AI voiceover…")
        vo_path, vo_dur = synth(hook["voice_text"].strip(), hook.get("voice"), float(hook.get("speed", 1.05)),
                                reel_dir / "voice", hook.get("voice_ref"), float(hook.get("energy", 0.65)))
    hook_end = 0.0
    if hook.get("enabled") and (hook.get("screen", "").strip() or vo_path):
        hook_end = min(max(2.2, vo_dur + 0.45), total)

    # previews leave hook + captions out: the page draws them live, so moving or restyling them needs no re-render
    (reel_dir / "captions.ass").write_text(build_ass(p, items, hook_end, text=final))
    global LAST_META
    LAST_META = {"hook_end": round(hook_end, 3), "live": not final,
                 "timeline": [{"piece": it["piece"], "start": round(it["start"], 3), "end": round(it["end"], 3),
                               "out": round(it["out"], 3), "kind": it["kind"]} for it in items]}
    cmd = [tool("ffmpeg"), "-y", "-v", "error", "-progress", "pipe:1", "-nostats"]
    for it in items:
        cmd += ["-ss", f"{it['start'] - p['pieces'][it['piece']]['offset']:.3f}",
                "-t", f"{it['end'] - it['start']:.3f}", "-i", f"source/{p['pieces'][it['piece']]['file']}"]
    vo_index = None
    if vo_path:
        vo_index = len(items)
        cmd += ["-i", str(vo_path.resolve())]
    (reel_dir / "filtergraph.txt").write_text(build_graph(p, items, fps, vo_index, vo_dur, final))
    cmd += ["-/filter_complex", "filtergraph.txt", "-map", "[vout]", "-map", "[aout]", "-r", str(fps)]

    if final:
        out = reel_dir / f"FINAL-{reel_dir.name}.mp4"
        cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high", "-level:v", "4.2",
                "-maxrate", "16M", "-bufsize", "32M", "-g", str(fps * 2), "-pix_fmt", "yuv420p",
                "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact",
                "-flags:v", "+bitexact", "-flags:a", "+bitexact", "-bsf:v", "filter_units=remove_types=6",
                "-metadata:s:v:0", "encoder=", "-metadata:s:a:0", "encoder=",
                "-movflags", "+faststart", "-t", f"{total:.3f}"]
    else:
        drafts = reel_dir / "drafts"
        drafts.mkdir(exist_ok=True)
        nums = [int(m.group(1)) for f in drafts.glob("draft-v*.mp4") if (m := re.match(r"draft-v(\d+)", f.stem))]
        out = drafts / f"draft-v{max(nums, default=0) + 1}.mp4"
        from .media import hw_encoder
        hw = hw_encoder()   # previews: the fast hardware encoder if this computer has one, else quick x264
        vcodec = (["-c:v", hw[0], "-b:v", "10M", "-pix_fmt", "yuv420p", *hw[1]] if hw
                  else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"])
        cmd += [*vcodec,
                "-c:a", "aac", "-b:a", "160k", "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
                "-flags:a", "+bitexact", "-movflags", "+faststart", "-t", f"{total:.3f}"]
    tmp = out.with_name(out.stem + ".partial.mp4")
    cmd.append(str(tmp.relative_to(reel_dir)))

    # the encode fills 5%→90% (final: loudness fix and checks come after) or 5%→98% (draft)
    lo, hi = 0.05, (0.9 if final else 0.98)
    step(lo, "Encoding the final video (high quality)…" if final else "Encoding the preview…")
    import threading
    from .media import PROCS
    from .system import NO_WINDOW
    proc = subprocess.Popen(cmd, cwd=reel_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    PROCS.setdefault("render", set()).add(proc)   # so closing the app stops it too
    errs = []
    reader = threading.Thread(target=lambda: errs.append(proc.stderr.read()), daemon=True)
    reader.start()  # read errors alongside, so a chatty ffmpeg can never block on a full pipe
    try:
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    step(lo + (hi - lo) * min(1.0, max(0, int(line.split("=")[1])) / 1e6 / total), None)
                except ValueError:
                    pass
        rc = proc.wait()
    finally:
        PROCS.get("render", set()).discard(proc)
    reader.join(5)
    err = "".join(errs)
    if rc != 0:
        tmp.unlink(missing_ok=True)
        raise MediaError("Render failed:\n" + err[-2500:])
    if final:
        step(hi, "Fixing loudness to −14 LUFS…")
        _fix_loudness(reel_dir, tmp)
    step(0.98 if not final else 0.97, "Removing metadata…")
    meta.clean(tmp)  # every file the app writes (drafts too) is stripped of all metadata
    tmp.replace(out)
    from .system import clear_file_attributes
    clear_file_attributes(out)
    progress(1.0)
    return out


def measure_lufs(path: Path):
    p = subprocess.run([tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=framelog=quiet:peak=true",
                        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", p.stderr)
    return float(m[-1]) if m else None


def _fix_loudness(reel_dir: Path, path: Path, target: float = -14.0) -> None:
    """Single-pass loudnorm can land short of the target on short clips; correct with a gain + limiter pass."""
    lufs = measure_lufs(path)
    if lufs is None or abs(lufs - target) <= 1.0:
        return
    gain = max(-12.0, min(12.0, target - lufs))
    fixed = path.with_name(path.stem + ".loud.mp4")
    subprocess.run([tool("ffmpeg"), "-y", "-v", "error", "-i", str(path), "-map", "0:v", "-map", "0:a", "-c:v", "copy",
                    "-af", f"volume={gain:.2f}dB,alimiter=limit=0.84:level=false,aresample=48000",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-map_metadata", "-1", "-map_chapters", "-1",
                    "-fflags", "+bitexact", "-flags:a", "+bitexact", "-metadata:s:v:0", "encoder=", "-metadata:s:a:0", "encoder=",
                    "-movflags", "+faststart", str(fixed)], check=True, capture_output=True)
    fixed.replace(path)


# ------------------------------------------------------------------ verification

ALLOWED_TAGS = {"major_brand", "minor_version", "compatible_brands", "handler_name", "vendor_id", "language"}


def verify(path: Path) -> dict:
    d = probe(path)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    checks = []

    def chk(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    chk("Resolution 1080×1920", v["width"] == 1080 and v["height"] == 1920, f"{v['width']}×{v['height']}")
    chk("H.264 video", v["codec_name"] == "h264", f"{v['codec_name']} ({v.get('profile')}, {v.get('pix_fmt')})")
    r, avg = v.get("r_frame_rate"), v.get("avg_frame_rate")
    fr = eval(r) if r else 0  # "60/1"
    chk("Constant frame rate", r == avg or abs(fr - eval(avg)) < 0.05, f"{fr:g} fps")
    chk("AAC audio, 48 kHz stereo", a and a["codec_name"] == "aac" and a.get("sample_rate") == "48000" and a.get("channels") == 2,
        f"{a['codec_name']} {a.get('sample_rate')} Hz, {a.get('channels')} ch" if a else "no audio")
    dur = float(d["format"]["duration"])
    chk("Length", 3 <= dur <= 180, f"{dur:.1f}s" + (" — over 90s; fine for Shorts/Reels up to 3 min" if dur > 90 else ""))
    mc = meta.check(path)
    chk("Metadata removed (checked with " + mc["read_by"] + ")", mc["ok"],
        "no title, dates, software/encoder, device, location or hidden encoder text" if mc["ok"]
        else "still found: " + ", ".join(f"{k} = {v}" for k, v in list(mc["leftover"].items())[:6]))
    head = path.read_bytes()[:4 * 1024 * 1024]
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    chk("Fast start (moov first)", 0 <= moov < mdat or (moov >= 0 and mdat < 0), "ready for streaming upload")
    p = subprocess.run([tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=framelog=quiet",
                        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", p.stderr)
    lufs = float(m[-1]) if m else None
    chk("Loudness ≈ −14 LUFS", lufs is not None and -16.5 <= lufs <= -12, f"{lufs} LUFS" if lufs is not None else "unknown")
    size = path.stat().st_size / 1e6
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "size_mb": round(size, 1),
            "duration": round(dur, 2), "file": path.name}


def write_report(path: Path, rep: dict) -> Path:
    lines = [f"Export check for {rep['file']}  ({rep['size_mb']} MB, {rep['duration']}s)", ""]
    for c in rep["checks"]:
        lines.append(f"[{'OK ' if c['ok'] else 'FIX'}] {c['name']}: {c['detail']}")
    lines += ["", "Before uploading: open the file's info (Finder ▸ Get Info, or `exiftool <file>`) and confirm",
              "there is no title, location, device or date data you don't want to share."]
    out = path.with_name("export-report.txt")
    out.write_text("\n".join(lines) + "\n")
    return out
