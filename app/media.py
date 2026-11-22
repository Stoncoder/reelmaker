"""Getting footage: YouTube section fetch (with full-download fallback) and local files."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from fractions import Fraction
from pathlib import Path

from .config import fmt_time, tool

PAD = 10.0  # extra seconds fetched on each side of a range


class MediaError(RuntimeError):
    pass


class Cancelled(MediaError):
    pass


CTX = threading.local()          # CTX.tag = the reel this thread is working on; CTX.report = live progress callback
PROCS: dict[str, set] = {}
CANCELLED: set[str] = set()


def check_cancel():
    tag = getattr(CTX, "tag", None)
    if tag and tag in CANCELLED:
        raise Cancelled("Cancelled.")


def cancel(tag: str) -> None:
    CANCELLED.add(tag)
    for pr in list(PROCS.get(tag, ())):
        try:
            pr.kill()
        except Exception:
            pass


# ------------------------------------------------------------------ live progress

class Meter:
    """One live task (a download, an encode, a transcription). Works out speed and time left
    from recent samples and hands {label, frac, done, total, speed, eta, ...} to CTX.report.
    unit="bytes": done/total/speed are bytes and bytes per second.
    unit="sec":   they are seconds of media, and speed is "× realtime"."""

    def __init__(self, label: str, total: float | None = None, unit: str = "bytes", approx: bool = False):
        self.label, self.total, self.unit, self.approx = label, total, unit, approx
        self.done, self.frac, self.known = 0.0, 0.0, False
        self.t0 = time.monotonic()
        self.hist: deque = deque(maxlen=600)
        self.last = 0.0
        self.lock = threading.Lock()
        self.report_cb = getattr(CTX, "report", None)   # worker threads report through the owner's callback
        self._send(self.info())

    def update(self, done: float | None = None, frac: float | None = None, total: float | None = None,
               force: bool = False) -> None:
        with self.lock:
            if total:
                self.total = total
            if done is not None:
                self.done = max(0.0, done)
            # a task's bar never steps back (two sources, e.g. yt-dlp's own report and the file size, can disagree)
            if frac is not None:
                self.frac, self.known = max(self.frac, min(1.0, max(0.0, frac))), True
            elif self.total and done is not None:
                self.frac, self.known = max(self.frac, min(1.0, self.done / self.total)), True
            now = time.monotonic()
            self.hist.append((now, self.done, self.frac))
            if not force and now - self.last < 0.4:
                return
            self.last = now
            info = self.info()
        self._send(info)

    def finish(self) -> None:
        self.update(frac=1.0, force=True)

    def info(self) -> dict:
        now = time.monotonic()
        recent = [h for h in self.hist if now - h[0] <= 6.0]
        speed = eta = None
        if len(recent) >= 2 and recent[-1][0] - recent[0][0] >= 1.0:
            dt = recent[-1][0] - recent[0][0]
            speed = max(0.0, (recent[-1][1] - recent[0][1]) / dt)
            fr = (recent[-1][2] - recent[0][2]) / dt
            if self.known and fr > 1e-5:
                eta = (1.0 - self.frac) / fr
            elif speed and self.total:
                eta = max(0.0, self.total - self.done) / speed
        return {"label": self.label, "frac": round(self.frac, 4), "known": self.known,
                "done": round(self.done, 1), "total": round(self.total, 1) if self.total else None,
                "speed": round(speed, 2) if speed is not None else None,
                "eta": round(eta, 1) if eta is not None else None, "unit": self.unit, "approx": self.approx,
                "elapsed": round(now - self.t0, 1), "at": time.time()}

    def _send(self, info: dict) -> None:
        if self.report_cb:
            try:
                self.report_cb(info)
            except Exception:
                pass


PROG = ["-progress", "pipe:1", "-nostats"]


def ffmpeg_progress(cb):
    """Line handler for ffmpeg's `-progress pipe:1`: calls cb(seconds encoded so far)."""
    def on(line: str):
        k, _, v = line.partition("=")
        if k == "out_time_us":
            v = v.strip()
            if v.lstrip("-").isdigit():
                cb(max(0, int(v)) / 1e6)
    return on


# yt-dlp: one parsable line per progress update, on stdout
YTDLP_PROG = ["--newline", "--progress-template",
              "download:RMPROG %(progress.downloaded_bytes)s %(progress.total_bytes)s "
              "%(progress.total_bytes_estimate)s %(progress.status)s"]


def ytdlp_progress(meter: Meter, streams: int = 2, expect: float | None = None):
    """Line handler for yt-dlp run with YTDLP_PROG. A merged format downloads the video, then the audio;
    the video gets ~90% of the bar since it is nearly all of the bytes. `expect` (bytes) is used when
    yt-dlp can't tell the size itself (section downloads)."""
    weights = [0.9, 0.1] if streams == 2 else [1.0]
    st = {"idx": 0, "last": 0.0, "base": 0.0}

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def on(line: str):
        if not line.startswith("RMPROG "):
            return
        f = line.split()
        if len(f) < 4:
            return
        got, tot, est = num(f[1]), num(f[2]), num(f[3])
        if got is None:
            return
        if st["last"] > 256e3 and got < st["last"] * 0.5:     # the next stream started
            st["base"] += st["last"]
            st["idx"] = min(st["idx"] + 1, len(weights) - 1)
        st["last"] = got
        T = tot or est
        i = st["idx"]
        if T:
            frac = sum(weights[:i]) + weights[i] * min(1.0, got / T)
            meter.update(done=st["base"] + got, frac=frac, total=st["base"] + T)
        elif expect:
            meter.update(done=st["base"] + got, frac=min(0.99, (st["base"] + got) / expect), total=expect)
        else:
            meter.update(done=st["base"] + got)
    return on


# ------------------------------------------------------------------ running tools

def run(cmd: list[str], cwd: Path | None = None, timeout: float | None = None, on_line=None) -> str:
    """Run a tool. With on_line, stdout is handed over line by line as it arrives (for live progress)."""
    check_cancel()
    tag = getattr(CTX, "tag", None)
    from . import system
    pr = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace", bufsize=1, creationflags=system.NO_WINDOW)
    PROCS.setdefault(tag, set()).add(pr)
    try:
        if on_line is None:
            out, err = pr.communicate(timeout=timeout)
        else:
            errs: list[str] = []
            reader = threading.Thread(target=lambda: errs.append(pr.stderr.read()), daemon=True)
            reader.start()
            timed_out = threading.Event()

            def expire():
                timed_out.set()
                pr.kill()
            timer = threading.Timer(timeout, expire) if timeout else None
            if timer:
                timer.daemon = True
                timer.start()
            tail: deque = deque(maxlen=300)
            try:
                for line in pr.stdout:
                    tail.append(line)
                    try:
                        on_line(line.rstrip("\r\n"))
                    except Exception:
                        pass
                pr.wait()
            finally:
                if timer:
                    timer.cancel()
            reader.join(5)
            out, err = "".join(tail), "".join(errs)
            if timed_out.is_set():
                raise MediaError(f"{Path(cmd[0]).name} took too long")
    except subprocess.TimeoutExpired:
        pr.kill()
        raise MediaError(f"{Path(cmd[0]).name} took too long")
    finally:
        PROCS.get(tag, set()).discard(pr)
    check_cancel()
    p = subprocess.CompletedProcess(cmd, pr.returncode, out, err)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "")[-2500:]
        raise MediaError(f"{Path(cmd[0]).name} failed:\n{tail}")
    return p.stdout


def probe(path: Path) -> dict:
    out = run([tool("ffprobe"), "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)])
    return json.loads(out)


def video_info(path: Path) -> dict:
    data = probe(path)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise MediaError(f"No video stream in {path.name}")
    r = float(Fraction(v.get("r_frame_rate", "30/1")))
    avg = float(Fraction(v.get("avg_frame_rate", "0/1"))) if v.get("avg_frame_rate", "0/0") != "0/0" else r
    return {
        "width": int(v["width"]),
        "height": int(v["height"]),
        "fps": round(avg or r, 3),
        "r_fps": round(r, 3),
        "vfr": abs(r - avg) > 0.5 if avg else False,
        "vcodec": v.get("codec_name"),
        "has_audio": a is not None,
        "duration": float(data["format"].get("duration", 0)),
    }


def target_fps(fps: float) -> int:
    """Constant output frame rate: keep 60 for high-frame-rate sources, otherwise 30."""
    return 60 if fps >= 45 else 30


def youtube_meta(url: str) -> dict:
    out = run([tool("yt-dlp"), "--no-playlist", "--skip-download", "--dump-single-json",
               "--no-warnings", url], timeout=120)
    d = json.loads(out)
    return {
        "id": d.get("id"),
        "title": d.get("title"),
        "channel": d.get("channel") or d.get("uploader") or "",
        "duration": d.get("duration") or 0,
        "is_live": bool(d.get("is_live")) or d.get("live_status") in ("is_live", "post_live"),
        "live_status": d.get("live_status"),
    }


# YouTube's HLS streams can be seeked into by ffmpeg; its plain https links now refuse (403) any program but yt-dlp.
FORMAT_HLS = ("bv*[protocol^=m3u8][vcodec^=avc1][height<=1080]+ba[protocol^=m3u8]/"
              "b[protocol^=m3u8][vcodec^=avc1][height<=1080]")
FULL_DOWNLOAD_MAX = 20 * 60   # never download a whole video longer than this just to cut a clip out of it

FORMAT = ("bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/"
          "bv*[height<=1080]+ba/b[height<=1080]/b")


def _out_glob(cmd: list[str]) -> tuple[Path, str] | None:
    """The folder and file-name prefix of a yt-dlp `-o` template, to watch the download grow on disk."""
    if "-o" not in cmd:
        return None
    o = Path(cmd[cmd.index("-o") + 1])
    # "piece-raw.%(ext)s" -> "piece-raw";  "window-dl.mp4" -> "window-dl" (yt-dlp writes window-dl.f137.mp4 etc.)
    stem = o.name.split("%(")[0].rstrip(".") if "%(" in o.name else o.stem
    return (o.parent, stem) if stem else None


def ytdlp_download(cmd: list[str], label: str, streams: int = 2, timeout: float = 3600,
                   expect: float | None = None) -> None:
    """Run a yt-dlp download with a live progress meter. Section downloads (ffmpeg under the hood) print
    almost nothing until they finish, so the file growing on disk is watched as well."""
    m = Meter(label, total=expect, approx=expect is not None)
    parse = ytdlp_progress(m, streams, expect)
    seen = {"t": 0.0}

    def on_line(line: str):
        if line.startswith("RMPROG "):
            seen["t"] = time.monotonic()
        parse(line)

    stop = threading.Event()
    where = _out_glob(cmd)

    def watch():
        while not stop.wait(0.5):
            if time.monotonic() - seen["t"] < 2.0 or not where:
                continue
            size = 0
            for f in where[0].glob(where[1] + "*"):
                try:
                    size += f.stat().st_size
                except OSError:
                    pass
            if size:
                # the size is only a guess: if the download outgrows it, keep creeping toward 99% instead of sitting there
                m.update(done=size, frac=min(0.99, size / max(expect, size * 1.08)) if expect else None,
                         total=max(expect, size * 1.08) if expect else None)
    w = threading.Thread(target=watch, daemon=True)
    w.start()
    try:
        run(cmd[:1] + YTDLP_PROG + cmd[1:], timeout=timeout, on_line=on_line)
    finally:
        stop.set()
        w.join(2)
    m.finish()


# Working files are encoded on the Mac's media engine (fast); software x264 only as a fallback.
_HW: dict = {}


def hw_encoder() -> tuple[str, list] | None:
    """The H.264 hardware encoder that works on this computer (tested once with a tiny encode), or None.
    Mac: Apple VideoToolbox. Windows: NVIDIA NVENC, Intel Quick Sync or AMD AMF, whichever this PC has."""
    if "enc" in _HW:
        return _HW["enc"]
    from . import system
    cands = ([("h264_videotoolbox", ["-allow_sw", "1"])] if system.IS_MAC else
             [("h264_nvenc", ["-preset", "p5", "-rc", "vbr"]), ("h264_qsv", ["-preset", "faster"]), ("h264_amf", ["-quality", "quality"])])
    _HW["enc"] = None
    for name, extra in cands:
        try:
            p = subprocess.run([tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2",
                                "-c:v", name, *extra, "-pix_fmt", "yuv420p", "-frames:v", "3", "-f", "null", "-"],
                               capture_output=True, timeout=30, creationflags=system.NO_WINDOW)
            if p.returncode == 0:
                _HW["enc"] = (name, extra)
                break
        except Exception:
            pass
    return _HW["enc"]


def video_codecs(bitrate: str = "24M") -> list[list]:
    """Encoder settings to try in order: the hardware encoder (fast) if there is one, then software x264."""
    hw = hw_encoder()
    out = []
    if hw:
        out.append(["-c:v", hw[0], "-b:v", bitrate, "-maxrate", "32M", "-bufsize", "48M", "-g", "30", "-pix_fmt", "yuv420p", *hw[1]])
    out.append(SW_V)
    return out


SW_V = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", "-g", "30"]
AUD = ["-c:a", "aac", "-b:a", "256k", "-ar", "48000"]


def _encode(inputs: list, maps: list, dst: Path, dur: float | None, timeout=None, on_sec=None) -> None:
    last = None
    for v in video_codecs():
        cmd = [tool("ffmpeg"), "-y", "-v", "error", *(PROG if on_sec else []), *inputs]
        if dur is not None:
            cmd += ["-t", f"{dur:.3f}"]
        cmd += [*maps, *v, *AUD, "-movflags", "+faststart", str(dst)]
        try:
            run(cmd, timeout=timeout, on_line=ffmpeg_progress(on_sec) if on_sec else None)
            return
        except Cancelled:
            raise
        except Exception as e:
            last = e
            dst.unlink(missing_ok=True)
    raise last


def _normalize(src: Path, dst: Path, start: float | None = None, dur: float | None = None,
               label: str = "Converting to a working copy") -> None:
    """Re-encode to a browser-friendly, frame-accurate H.264/AAC working file."""
    inp = (["-ss", f"{start:.3f}"] if start is not None else []) + ["-i", str(src)]
    total = dur
    if total is None:
        try:
            total = video_info(src)["duration"] or None
        except Exception:
            total = None
    m = Meter(label, total=total, unit="sec")
    _encode(inp, ["-map", "0:v:0", "-map", "0:a:0?"], dst, dur,
            on_sec=lambda s: m.update(done=s, frac=(s / total) if total else None))
    m.finish()


def _stream_rate(f: dict, duration: float) -> float:
    """Bytes per second of a YouTube stream (0 if unknown)."""
    if f.get("tbr"):
        return float(f["tbr"]) * 125.0
    size = f.get("filesize") or f.get("filesize_approx")
    return float(size) / duration if size and duration else 0.0


def _direct_fetch(url: str, a: float, b: float, dst: Path, fmt: str = FORMAT) -> None:
    """Seek straight into YouTube's streams and encode just [a, b] in one pass (no full download, no double encode).
    Progress: ffmpeg reports how far into the section it is; times the stream's bitrate that is the data downloaded."""
    m = Meter("Connecting to YouTube…", unit="busy")   # asking YouTube for the stream addresses takes a few seconds
    r = run([tool("yt-dlp"), "--no-playlist", "-f", fmt, "-j", url], timeout=120)
    out = r if isinstance(r, str) else getattr(r, "stdout", "")
    if isinstance(out, bytes):
        out = out.decode()
    info = json.loads(out.strip().splitlines()[-1])
    fmts = info.get("requested_formats") or [info]
    inputs = []
    for f in fmts:
        hdr = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
        hls = str(f.get("protocol", "")).startswith("m3u8")
        inputs += (["-headers", hdr] if hdr else []) + ["-reconnect", "1", "-reconnect_streamed", "1"] + \
                  ([] if hls else ["-request_size", "10000000"]) + ["-ss", f"{a:.3f}", "-i", f["url"]]
    dur = b - a
    vid_len = float(info.get("duration") or 0)
    rates = [_stream_rate(f, vid_len) for f in fmts]
    vrate = rates[0]
    arate = rates[1] if len(rates) > 1 else 0.0
    known = vrate > 0
    total = (vrate + arate) * dur if known else None
    m = Meter("Downloading from YouTube", total=total, unit="bytes" if known else "sec", approx=True)
    m.update(done=0.0, frac=0.0, force=True)   # bar starts at 0% (not the moving "busy" stripe)

    if len(fmts) < 2 or dur < 40:
        maps = ["-map", "0:v:0", "-map", "1:a:0"] if len(fmts) >= 2 else ["-map", "0:v:0", "-map", "0:a:0?"]

        def on_sec(s):
            s = min(s, dur)
            m.update(done=s * (vrate + arate) if known else s, frac=s / dur)
        _encode(inputs, maps, dst, dur, timeout=900, on_sec=on_sec)
        m.finish()
        return
    # Long section: YouTube limits speed per connection, so fetch the video in parallel chunks
    # (each one starts on its own keyframe, so they join without re-encoding); audio comes in one piece.
    from concurrent.futures import ThreadPoolExecutor
    vin = inputs[:inputs.index("-ss")]          # headers + reconnect for the video stream
    vurl = fmts[0]["url"]
    ain = inputs[inputs.index(vurl) + 1:]       # everything for the audio stream (incl. its -ss and -i)
    n = min(6, max(2, int(dur // 20)))
    L = dur / n
    parts = [dst.with_name(f"{dst.stem}-v{k}.mp4") for k in range(n)]
    tag = getattr(CTX, "tag", None)
    got = {"v": [0.0] * n, "a": 0.0}
    glock = threading.Lock()

    def tick():
        with glock:
            vs, as_ = sum(got["v"]), got["a"]
        if known:
            done = vs * vrate + as_ * arate
            m.update(done=done, frac=done / total)
        else:
            m.update(done=vs, frac=vs / dur)

    def one(k):
        CTX.tag = tag

        def on_sec(s):
            with glock:
                got["v"][k] = min(s, L)
            tick()
        _encode_v(vin + ["-ss", f"{a + k * L:.3f}", "-i", vurl], parts[k], L, on_sec=on_sec)
        with glock:
            got["v"][k] = L
        tick()

    try:
        with ThreadPoolExecutor(n + 1) as ex:
            aud = dst.with_name(dst.stem + "-a.m4a")

            def audio():
                CTX.tag = tag

                def on_sec(s):
                    with glock:
                        got["a"] = min(s, dur)
                    tick()
                run([tool("ffmpeg"), "-y", "-v", "error", *PROG, *ain, "-t", f"{dur:.3f}", "-map", "0:a:0", *AUD, str(aud)],
                    timeout=900, on_line=ffmpeg_progress(on_sec))
                with glock:
                    got["a"] = dur
                tick()
            futs = [ex.submit(one, k) for k in range(n)] + [ex.submit(audio)]
            for f in futs:
                f.result()
        lst = dst.with_name(dst.stem + "-list.txt")
        lst.write_text("".join(f"file '{x.name}'\n" for x in parts))
        run([tool("ffmpeg"), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(aud),
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", str(dst)])
        m.finish()
    finally:
        for x in parts + [dst.with_name(dst.stem + "-a.m4a"), dst.with_name(dst.stem + "-list.txt")]:
            x.unlink(missing_ok=True)


def _encode_v(inputs: list, dst: Path, dur: float, on_sec=None) -> None:
    last = None
    for v in video_codecs():
        try:
            run([tool("ffmpeg"), "-y", "-v", "error", *(PROG if on_sec else []), *inputs, "-t", f"{dur:.3f}",
                 "-map", "0:v:0", "-an", *v, str(dst)], timeout=900,
                on_line=ffmpeg_progress(on_sec) if on_sec else None)
            return
        except Cancelled:
            raise
        except Exception as e:
            last = e
            dst.unlink(missing_ok=True)
    raise last


def fetch_youtube_piece(url: str, start: float, end: float, dst: Path, log) -> dict:
    """Fetch [start-PAD, end+PAD] of a YouTube video into dst. Returns piece info."""
    a = max(0.0, start - PAD)
    b = end + PAD
    raw = dst.with_name(dst.stem + "-raw")
    for f in dst.parent.glob(raw.name + ".*"):
        f.unlink()
    method = "direct"
    try:
        site = "Twitch" if "twitch.tv" in url else "Kick" if "kick.com" in url else "YouTube"
        log(f"Fetching only {fmt_time(a)}–{fmt_time(b)} straight from {site} (one fast pass)…")
        try:
            last = None
            for fmt in ((FORMAT_HLS, FORMAT) if "youtu" in url else (FORMAT,)):
                try:
                    _direct_fetch(url, a, b, dst, fmt)
                    if video_info(dst)["duration"] < (b - a) * 0.8:
                        raise MediaError("short result")
                    last = None
                    break
                except Cancelled:
                    raise
                except Exception as e:
                    last = e
                    dst.unlink(missing_ok=True)
            if last:
                raise last
        except Cancelled:
            raise
        except Exception as e:
            method = "section"
            log(f"Direct fetch didn't work ({str(e).splitlines()[0][:100]}); trying yt-dlp sections…")
            ytdlp_download([tool("yt-dlp"), "--no-playlist", "-f", f"{FORMAT_HLS}/{FORMAT}", "--merge-output-format", "mp4",
                            "--download-sections", f"*{a:.2f}-{b:.2f}", "--force-keyframes-at-cuts",
                            "--no-part", "-o", str(raw) + ".%(ext)s", url],
                           "Downloading the section (backup method)", timeout=900,
                           expect=(b - a) * 0.65e6)   # 1080p60 from YouTube runs at roughly 0.65 MB per second of video
            got = next(dst.parent.glob(raw.name + ".*"))
            _normalize(got, dst)
            got.unlink()
    except Cancelled:
        raise
    except Exception as e:  # fall back to full download, trim locally — only for short videos
        try:
            vid_len = float(youtube_meta(url).get("duration") or 0)
        except Exception:
            vid_len = 0.0
        if not vid_len or vid_len > FULL_DOWNLOAD_MAX:
            raise MediaError("Couldn't fetch just this part from the site right now (it refused the fast methods), and the "
                             "app won't download the whole " + (f"{vid_len / 3600:.1f}-hour " if vid_len else "") +
                             "video to cut a clip. Click “Try again” in a minute; if it keeps failing, update the "
                             "downloader by running Setup again.\n\nDetails: " + str(e).splitlines()[-1][:300])
        log(f"Section fetch didn't work ({str(e).splitlines()[0][:120]}). It's a short video, so downloading it whole and trimming locally…")
        method = "full"
        full = dst.parent / "full-source"
        for f in dst.parent.glob("full-source.*"):
            f.unlink()
        ytdlp_download([tool("yt-dlp"), "--no-playlist", "-f", FORMAT, "--merge-output-format", "mp4",
                        "--no-part", "-o", str(full) + ".%(ext)s", url], "Downloading the full video", timeout=3600)
        got = next(dst.parent.glob("full-source.*"))
        _normalize(got, dst, a, b - a, label="Cutting your range out of the full video")
        got.unlink()
    info = video_info(dst)
    return {"file": dst.name, "offset": a, "length": info["duration"], "method": method, **info}


def extract_local_piece(src: Path, start: float, end: float, dst: Path) -> dict:
    info = video_info(src)
    a = max(0.0, start - PAD)
    b = min(info["duration"], end + PAD) if info["duration"] else end + PAD
    if a >= info["duration"] > 0:
        raise MediaError(f"The range starts after the end of the file ({info['duration']:.0f}s long).")
    _normalize(src, dst, a, b - a, label="Cutting your range from the file")
    pinfo = video_info(dst)
    return {"file": dst.name, "offset": a, "length": pinfo["duration"], "method": "local", **pinfo}


def extract_audio(src: Path, sr: int = 16000) -> "numpy.ndarray":
    import numpy as np
    p = subprocess.run([tool("ffmpeg"), "-v", "error", "-i", str(src), "-vn", "-ac", "1",
                        "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    if p.returncode != 0:
        raise MediaError(p.stderr.decode()[-1000:])
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def grab_frames(src: Path, every: float = 1.0, width: int = 640):
    """Yield (t, BGR frame) sampled every `every` seconds."""
    import numpy as np
    info = video_info(src)
    w = width
    h = int(round(info["height"] * w / info["width"] / 2) * 2)
    p = subprocess.run([tool("ffmpeg"), "-v", "error", "-i", str(src), "-vf",
                        f"fps=1/{every},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                       capture_output=True)
    if p.returncode != 0:
        raise MediaError(p.stderr.decode()[-1000:])
    buf = p.stdout
    size = w * h * 3
    for i in range(len(buf) // size):
        frame = np.frombuffer(buf[i * size:(i + 1) * size], dtype=np.uint8).reshape(h, w, 3)
        yield i * every, frame
