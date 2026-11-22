"""Live streams: grab any past moment of a stream that is still running, and find out who is live right now.

YouTube keeps a running stream as numbered 5-second segments (segment N starts N*5 s after the stream began,
and each one carries its exact frame time). yt-dlp won't cut sections out of a live stream, so we use its
extractor for the signed segment URLs and download only the segments we need, in parallel.
"""
from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import media
from .config import fmt_time

_META = re.compile(rb"([A-Z][A-Za-z-]+): ([0-9]+)\r\n")
_cache: dict[str, dict] = {}          # video url -> {"t": fetched-at, "ydl", "info", "v", "a", ...}
_lock = threading.Lock()


class LiveError(media.MediaError):
    pass


class _Quiet:
    """yt-dlp prints errors like "The channel is not currently live" even when quiet; we expect those."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


Q = {"quiet": True, "no_warnings": True, "logger": _Quiet()}


def _ydl():
    import yt_dlp
    return yt_dlp.YoutubeDL({**Q, "live_from_start": True, "noplaylist": True,
                             "socket_timeout": 30})


def _pick(formats, max_height):
    """Best H.264 video stream up to max_height, and the AAC audio stream (both segment-based)."""
    seg = [f for f in formats if str(f.get("protocol", "")).startswith("http_dash_segments")]
    vids = [f for f in seg if str(f.get("vcodec", "")).startswith("avc1") and (f.get("height") or 0) <= max_height]
    auds = [f for f in seg if f.get("vcodec") == "none" and str(f.get("acodec", "")).startswith("mp4a")]
    if not vids or not auds:
        raise LiveError("This live stream doesn't offer seekable segments (DVR may be off), so past moments can't be fetched.")
    v = max(vids, key=lambda f: ((f.get("height") or 0), (f.get("fps") or 0), (f.get("tbr") or 0)))
    a = max(auds, key=lambda f: f.get("abr") or f.get("tbr") or 0)
    return v, a


def _stream(url: str, max_height: int) -> dict:
    """Signed segment URLs for this stream (cached ~20 min; YouTube URLs last ~6 h)."""
    key = f"{url}|{max_height}"
    with _lock:
        s = _cache.get(key)
        if s and time.time() - s["t"] < 1200:
            return s
    ydl = _ydl()
    info = ydl.extract_info(url, download=False)
    if info.get("live_status") not in ("is_live", "post_live"):
        raise LiveError("not live")
    v, a = _pick(info["formats"], max_height)
    s = {"t": time.time(), "ydl": ydl, "info": info, "td": float(v.get("target_duration") or 5),
         "v": next(iter(v["fragments"]({})))["url"], "a": next(iter(a["fragments"]({})))["url"],
         "height": v.get("height"), "fps": v.get("fps")}
    with _lock:
        _cache[key] = s
    return s


def _get(s: dict, base: str, sq: int, tries: int = 4) -> bytes:
    import yt_dlp
    from yt_dlp.utils import update_url_query
    last = None
    for i in range(tries):
        media.check_cancel()
        try:
            return s["ydl"].urlopen(yt_dlp.networking.Request(update_url_query(base, {"sq": str(sq)}))).read()
        except Exception as e:      # flaky network or a segment that isn't there yet
            last = e
            time.sleep(0.5 * (i + 1))
    raise LiveError(f"Couldn't download live segment {sq}: {str(last)[:120]}")


def _frame_time(body: bytes) -> float | None:
    m = dict((k.decode(), int(v)) for k, v in _META.findall(body[:4000]))
    return m["First-Frame-Time-Us"] / 1e6 if "First-Frame-Time-Us" in m else None


def head_seconds(url: str) -> float:
    """How far the stream has got (seconds since it started)."""
    import yt_dlp
    s = _stream(url, 360)
    r = s["ydl"].urlopen(yt_dlp.networking.HEADRequest(s["a"]))
    head = int(r.headers.get("X-Head-Seqnum") or 0)
    r.close()
    return head * s["td"]


def fetch(url: str, a: float, b: float, dst: Path, max_height: int = 1080, label: str = "Fetching from the live stream") -> dict:
    """Download [a, b] (seconds from the start of the stream) into dst as a frame-accurate H.264/AAC file."""
    s = _stream(url, max_height)
    td = s["td"]
    head = head_seconds(url)
    if a >= head - td:
        raise LiveError(f"{fmt_time(a)} hasn't been streamed yet — the stream is at {fmt_time(head)} right now.")
    b = min(b, head - td)                      # the newest segment may still be growing
    sq_a, sq_b = max(0, int(a // td)), int(math.ceil(b / td))
    sqs = list(range(sq_a, sq_b + 1))
    meter = media.Meter(label, total=len(sqs) * 2, unit="parts")
    done = [0]
    tag = getattr(media.CTX, "tag", None)

    def grab(job):
        media.CTX.tag = tag
        kind, sq = job
        body = _get(s, s[kind], sq)
        done[0] += 1
        meter.update(done=done[0], frac=done[0] / (len(sqs) * 2))
        return body

    jobs = [("v", q) for q in sqs] + [("a", q) for q in sqs]
    with ThreadPoolExecutor(8) as ex:
        bodies = list(ex.map(grab, jobs))
    vb, ab = bodies[:len(sqs)], bodies[len(sqs):]
    # exact time of our first segment, measured from the first segment of the stream
    t0 = _frame_time(_get(s, s["a"], 0))
    ta = _frame_time(ab[0])
    start = (ta - t0) if (t0 and ta) else sq_a * td
    vraw, araw = dst.with_name(dst.stem + "-live-v.mp4"), dst.with_name(dst.stem + "-live-a.mp4")
    try:
        vraw.write_bytes(b"".join(vb))
        araw.write_bytes(b"".join(ab))
        skip = max(0.0, a - start)
        meter.finish()
        m2 = media.Meter("Converting to a working copy", total=b - a, unit="sec")
        media._encode(["-ss", f"{skip:.3f}", "-i", str(vraw), "-ss", f"{skip:.3f}", "-i", str(araw)],
                      ["-map", "0:v:0", "-map", "1:a:0"], dst, b - a,
                      on_sec=lambda x: m2.update(done=x, frac=x / (b - a)))
        m2.finish()
    finally:
        vraw.unlink(missing_ok=True)
        araw.unlink(missing_ok=True)
    info = media.video_info(dst)
    return {"file": dst.name, "offset": a, "length": info["duration"], "method": "live", **info}


# ------------------------------------------------------------------ who is live right now

# Creators known for letting fans clip their streams. Editable in the Live tab (saved as settings["live_creators"]).
# Always check each creator's own clipping rules before posting.
DEFAULT_CREATORS = [
    {"name": "iShowSpeed", "youtube": "@IShowSpeed"},
    {"name": "Kai Cenat", "twitch": "kaicenat", "youtube": "@KaiCenatLive"},
    {"name": "MrBeast", "youtube": "@MrBeast"},
    {"name": "xQc", "kick": "xqc", "twitch": "xqc"},
    {"name": "Adin Ross", "kick": "adinross"},
    {"name": "Jynxzi", "twitch": "jynxzi"},
    {"name": "Duke Dennis", "twitch": "dukedennis"},
    {"name": "Fanum", "twitch": "fanum"},
    {"name": "Agent00", "twitch": "agent00"},
    {"name": "CaseOh", "twitch": "caseoh_"},
    {"name": "N3on", "kick": "n3on"},
    {"name": "Druski", "youtube": "@Druski"},
]
PLATFORMS = ("youtube", "twitch", "kick")
_now: dict = {"t": 0, "data": None}


def creators() -> list[dict]:
    from .config import settings
    c = settings().get("live_creators")
    return c if isinstance(c, list) and c else DEFAULT_CREATORS


def channel_url(platform: str, handle: str) -> str:
    h = handle.strip().lstrip("/")
    if platform == "youtube":
        h = h if h.startswith(("@", "channel/", "c/")) else "@" + h
        return f"https://www.youtube.com/{h}/live"
    if platform == "twitch":
        return f"https://www.twitch.tv/{h.lstrip('@')}"
    return f"https://kick.com/{h.lstrip('@')}"


def _recording_url(platform: str, handle: str) -> str | None:
    """Twitch/Kick save a running stream as a video you can seek in — the newest one on the channel."""
    url = (f"https://www.twitch.tv/{handle.lstrip('@')}/videos?filter=archives&sort=time" if platform == "twitch"
           else f"https://kick.com/{handle.lstrip('@')}/videos")
    import yt_dlp
    with yt_dlp.YoutubeDL({**Q, "extract_flat": True, "playlist_items": "1"}) as y:
        info = y.extract_info(url, download=False)
    e = (info.get("entries") or [None])[0]
    if not e:
        return None
    u = e.get("url") or ""
    return u.split("#")[0] or None


def _check(creator: dict, platform: str) -> dict | None:
    import yt_dlp
    handle = (creator.get(platform) or "").strip()
    if not handle:
        return None
    try:
        with yt_dlp.YoutubeDL({**Q, "noplaylist": True, "socket_timeout": 20}) as y:
            d = y.extract_info(channel_url(platform, handle), download=False, process=False)
    except Exception:
        return None                       # offline (yt-dlp says "not currently live") or unreachable
    if not d or not (d.get("is_live") or d.get("live_status") == "is_live"):
        return None
    started = d.get("release_timestamp") or d.get("timestamp")
    out = {"creator": creator.get("name") or handle, "platform": platform, "handle": handle,
           "title": d.get("fulltitle") or d.get("title") or "", "viewers": d.get("concurrent_view_count"),
           "started": started, "uptime": (time.time() - started) if started else None,
           "thumbnail": d.get("thumbnail") or ((d.get("thumbnails") or [{}])[-1].get("url")),
           "watch_url": d.get("webpage_url") or channel_url(platform, handle), "clip_url": None, "note": None}
    if platform == "youtube":
        out["clip_url"] = f"https://www.youtube.com/watch?v={d['id']}"
    else:
        try:
            out["clip_url"] = _recording_url(platform, handle)
            if platform == "twitch" and out["clip_url"]:
                # the archive carries the real stream title (the live page only says "<name> (live)")
                with yt_dlp.YoutubeDL(Q) as y:
                    v = y.extract_info(out["clip_url"], download=False, process=False)
                out["title"] = v.get("title") or out["title"]
                out["thumbnail"] = out["thumbnail"] or v.get("thumbnail")
        except Exception:
            out["clip_url"] = None
        if not out["clip_url"]:
            out["note"] = "This channel doesn't keep a recording of the stream, so past moments can't be clipped."
    return out


def live_now(refresh: bool = False) -> dict:
    """Which of your creators are live on YouTube, Twitch and Kick. Cached for a minute; only runs when asked."""
    if not refresh and _now["data"] and time.time() - _now["t"] < 60:
        return _now["data"]
    cs = creators()
    jobs = [(c, p) for c in cs for p in PLATFORMS if c.get(p)]
    with ThreadPoolExecutor(min(12, max(1, len(jobs)))) as ex:
        res = list(ex.map(lambda j: _check(*j), jobs))
    streams = [r for r in res if r]
    streams.sort(key=lambda s: -(s.get("viewers") or 0))
    live_names = {s["creator"] for s in streams}
    data = {"streams": streams, "offline": [c.get("name") for c in cs if c.get("name") not in live_names],
            "checked_at": time.time()}
    _now.update(t=time.time(), data=data)
    return data


def stream_position(s: dict) -> float:
    """How many seconds of this stream can be clipped right now."""
    if s.get("platform") == "youtube":
        return head_seconds(s["clip_url"])
    import yt_dlp
    with yt_dlp.YoutubeDL(Q) as y:
        v = y.extract_info(s["clip_url"], download=False, process=False)
    if v.get("duration"):
        return float(v["duration"])
    return float(s.get("uptime") or 0)
