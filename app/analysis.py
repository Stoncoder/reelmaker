"""Transcription, audio analysis, facecam detection, cut suggestions and caption building."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import numpy as np

from .config import WHISPER_MODEL, fmt_time
from .media import PAD, extract_audio, grab_frames

SR = 16000


def uid() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------- transcription

class _ProgressBar:
    """Stands in for mlx_whisper's tqdm bar: turns its frame counts into Meter updates,
    and lets Cancel stop a transcription part-way instead of only after it finishes."""

    def __init__(self, meter, seconds: float, total=None, **_):
        self.meter, self.seconds, self.total, self.n = meter, seconds, total or 0, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, k=1):
        from .media import check_cancel
        self.n += k
        if self.meter and self.total:
            f = min(1.0, self.n / self.total)
            self.meter.update(done=f * self.seconds, frac=f)
        check_cancel()


def use_mlx() -> bool:
    """MLX Whisper runs on the GPU of Apple-silicon Macs; everything else uses faster-whisper."""
    import os
    import platform
    if os.environ.get("REELMAKER_WHISPER") == "faster":
        return False
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def transcribe(piece_path: Path, offset: float, label: str = "Transcribing") -> list[dict]:
    """Word-level transcript of a video file: [{"w", "s", "e", "p"}], times shifted by offset."""
    from .media import Meter, check_cancel
    audio = extract_audio(piece_path, SR)
    if len(audio) == 0:
        return []
    check_cancel()
    seconds = len(audio) / SR
    meter = Meter(label, total=seconds, unit="sec")
    raw = _transcribe_mlx(audio, meter, seconds) if use_mlx() else _transcribe_faster(audio, meter, seconds)
    meter.finish()
    words = []
    for text, s, e, p in raw:
        text = text.strip()
        if text:
            words.append({"w": text, "s": round(offset + s, 3), "e": round(offset + e, 3), "p": round(p, 2)})
    return words


def _transcribe_mlx(audio, meter, seconds):
    import sys
    import mlx_whisper
    mod = sys.modules.get("mlx_whisper.transcribe")
    real = getattr(mod, "tqdm", None)

    class _Shim:  # what mlx_whisper calls as `tqdm.tqdm(...)`
        @staticmethod
        def tqdm(*a, **kw):
            return _ProgressBar(meter, seconds, **kw)
    if mod is not None and real is not None:
        mod.tqdm = _Shim
    try:
        res = mlx_whisper.transcribe(audio, path_or_hf_repo=WHISPER_MODEL, word_timestamps=True,
                                     condition_on_previous_text=False, verbose=False if mod is not None else None,
                                     no_speech_threshold=0.6, hallucination_silence_threshold=2.0)
    finally:
        if mod is not None and real is not None:
            mod.tqdm = real
    return [(w["word"], w["start"], w["end"], w.get("probability", 1.0))
            for seg in res.get("segments", []) for w in seg.get("words", [])]


_FW = {}   # the loaded faster-whisper model (loading takes a few seconds, so it's kept)


def _faster_model():
    from faster_whisper import WhisperModel
    from .config import FW_MODEL
    if "m" not in _FW:
        try:
            import ctranslate2
            cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            cuda = False
        m = None
        if cuda:   # NVIDIA GPU: fast — but only if NVIDIA's CUDA/cuDNN libraries are really there, so try 1 s of silence
            try:
                m = WhisperModel(FW_MODEL, device="cuda", compute_type="float16")
                list(m.transcribe(np.zeros(SR, dtype=np.float32))[0])
            except Exception:
                m = None
        # otherwise the CPU with 8-bit weights: slower, but works on any PC
        _FW["m"] = m or WhisperModel(FW_MODEL, device="cpu", compute_type="int8")
    return _FW["m"]


def _transcribe_faster(audio, meter, seconds):
    from .media import check_cancel
    model = _faster_model()
    segments, _info = model.transcribe(audio, word_timestamps=True, condition_on_previous_text=False,
                                       no_speech_threshold=0.6, hallucination_silence_threshold=2.0, beam_size=5)
    out = []
    for seg in segments:   # a generator: transcription happens as we read it, so progress and Cancel work
        for w in (seg.words or []):
            out.append((w.word, w.start, w.end, w.probability))
        f = min(1.0, seg.end / seconds) if seconds else 1.0
        meter.update(done=f * seconds, frac=f)
        check_cancel()
    return out


# ---------------------------------------------------------------- audio energy

def audio_profile(piece_path: Path, offset: float, hop: float = 0.1) -> dict:
    a = extract_audio(piece_path, SR)
    n = int(SR * hop)
    frames = len(a) // n
    if frames == 0:
        return {"hop": hop, "offset": offset, "db": []}
    rms = np.sqrt(np.mean(a[: frames * n].reshape(frames, n) ** 2, axis=1) + 1e-10)
    db = 20 * np.log10(rms + 1e-10)
    return {"hop": hop, "offset": offset, "db": [round(float(x), 1) for x in db]}


def _times(prof):
    return prof["offset"] + np.arange(len(prof["db"])) * prof["hop"]


def find_silences(prof, start, end, min_len=0.7):
    db = np.array(prof["db"])
    if len(db) == 0:
        return []
    t = _times(prof)
    thresh = min(-38.0, float(np.median(db)) - 18)
    quiet = db < thresh
    out, run_start = [], None
    for i, q in enumerate(quiet):
        if q and run_start is None:
            run_start = t[i]
        if (not q or i == len(quiet) - 1) and run_start is not None:
            run_end = t[i]
            s, e = max(run_start, start), min(run_end, end)
            if e - s >= min_len:
                out.append((float(s), float(e)))
            run_start = None
    return out


def find_peaks(prof, start, end, k=3):
    """Loudest moments (yells, laughs, explosions) – candidate payoffs."""
    db = np.array(prof["db"])
    if len(db) < 10:
        return []
    t = _times(prof)
    win = 5
    sm = np.convolve(db, np.ones(win) / win, mode="same")
    mask = (t >= start) & (t <= end)
    if not mask.any():
        return []
    mu, sd = float(sm[mask].mean()), float(sm[mask].std() + 1e-6)
    order = np.argsort(-sm)
    peaks = []
    for i in order:
        if not (start <= t[i] <= end):
            continue
        z = (sm[i] - mu) / sd
        if z < 1.0:
            break
        if all(abs(t[i] - p["t"]) > 3 for p in peaks):
            peaks.append({"t": round(float(t[i]), 2), "z": round(float(z), 2)})
        if len(peaks) >= k:
            break
    return sorted(peaks, key=lambda p: p["t"])


# ---------------------------------------------------------------- facecam

def detect_facecam(piece_path: Path, offset: float, width: int, height: int) -> dict:
    import cv2
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    samples = []  # (t, [ (cx, cy, fw, fh) normalized ])
    for t, frame in grab_frames(piece_path, every=1.0, width=640):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        fh, fw_ = gray.shape
        faces = casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                      minSize=(int(fw_ * 0.035), int(fw_ * 0.035)))
        dets = [((x + w / 2) / fw_, (y + h / 2) / fh, w / fw_, h / fh) for (x, y, w, h) in faces]
        samples.append((offset + t, dets))
    if not samples:
        return {"found": False, "changes": [], "coverage": 0}

    # greedy clustering of face positions
    clusters = []  # dict(cx, cy, fw, fh, n)
    labels = []
    for t, dets in samples:
        lab = []
        for d in dets:
            best = None
            for ci, c in enumerate(clusters):
                if abs(c["cx"] - d[0]) < 0.07 and abs(c["cy"] - d[1]) < 0.09:
                    best = ci
                    break
            if best is None:
                clusters.append({"cx": d[0], "cy": d[1], "fw": d[2], "fh": d[3], "n": 1})
                best = len(clusters) - 1
            else:
                c = clusters[best]
                c["n"] += 1
                a = 1 / c["n"]
                c["cx"] += (d[0] - c["cx"]) * a
                c["cy"] += (d[1] - c["cy"]) * a
                c["fw"] += (d[2] - c["fw"]) * a
                c["fh"] += (d[3] - c["fh"]) * a
            lab.append(best)
        labels.append(lab)

    stable = [i for i, c in enumerate(clusters) if c["n"] >= max(3, 0.25 * len(samples))]
    if not stable:
        return {"found": False, "changes": [], "coverage": 0}
    # per-sample dominant stable cluster
    seq = []
    for lab in labels:
        cand = [l for l in lab if l in stable]
        seq.append(max(cand, key=lambda l: clusters[l]["n"]) if cand else None)
    coverage = sum(1 for s in seq if s is not None) / len(seq)
    # change points: label switches and holds for >= 3 samples
    changes = []
    current = next((s for s in seq if s is not None), None)
    for i, s in enumerate(seq):
        if s is not None and s != current:
            hold = seq[i:i + 3]
            if len(hold) >= 3 and all(h == s for h in hold):
                c = clusters[s]
                changes.append({"t": round(samples[i][0], 2), "cx": c["cx"], "cy": c["cy"],
                                "fw": c["fw"], "fh": c["fh"]})
                current = s
    first = clusters[next(s for s in seq if s is not None)]
    return {"found": True, "cx": first["cx"], "cy": first["cy"], "fw": first["fw"], "fh": first["fh"],
            "coverage": round(coverage, 2), "changes": changes}


def face_crop_from(face: dict, W: int, H: int) -> dict:
    """A 9:8 crop (top half of the reel) framed around a detected face."""
    fh_px = face["fh"] * H
    ch = min(H, max(fh_px * 3.4, H * 0.22))
    cw = ch * 9 / 8
    if cw > W:
        cw, ch = W, W * 8 / 9
    cx, cy = face["cx"] * W, face["cy"] * H
    x = min(max(cx - cw / 2, 0), W - cw)
    y = min(max(cy - ch * 0.42, 0), H - ch)
    return {"x": x / W, "y": y / H, "w": cw / W, "h": ch / H}


def default_game_crop(W: int, H: int) -> dict:
    ch = H
    cw = ch * 9 / 8
    if cw > W:
        cw, ch = W, W * 8 / 9
    return {"x": (W - cw) / 2 / W, "y": (H - ch) / 2 / H, "w": cw / W, "h": ch / H}


# ---------------------------------------------------------------- suggestions

END_PUNCT = re.compile(r"[.!?]$")


def boundary_suggestions(words, rng, piece_idx, profile, range_idx):
    s0, e0 = rng
    out = []
    ws = sorted(words, key=lambda w: w["s"])
    # --- start: speech already in progress?
    before = [w for w in ws if w["s"] < s0 - 0.05]
    after = [w for w in ws if w["s"] >= s0 - 0.05]
    straddle = any(w["s"] < s0 < w["e"] for w in ws)
    contiguous = before and after and (s0 - before[-1]["e"] < 0.6) and (after[0]["s"] - s0 < 0.6)
    if straddle or contiguous:
        i = len(before) - 1
        while i > 0:
            prev = before[i - 1]
            if before[i]["s"] - prev["e"] >= 0.6 or END_PUNCT.search(prev["w"]):
                break
            i -= 1
        new_s = max(before[i]["s"] - 0.15, s0 - PAD)
        if s0 - new_s >= 0.3:
            phrase = " ".join(w["w"] for w in before[i:i + 6])
            out.append({"id": uid(), "kind": "extend_start", "piece": piece_idx, "range": range_idx,
                        "start": round(new_s, 2), "end": s0, "seconds": round(s0 - new_s, 1),
                        "reason": f"Your range starts mid-sentence. “{phrase}…” begins at {fmt_time(before[i]['s'])}."})
    # --- end: speech continues past the end?
    inside = [w for w in ws if w["e"] <= e0 + 0.05]
    past = [w for w in ws if w["e"] > e0 + 0.05]
    straddle = any(w["s"] < e0 < w["e"] for w in ws)
    contiguous = inside and past and (past[0]["s"] - e0 < 0.6) and (e0 - inside[-1]["e"] < 0.6)
    new_e = None
    if straddle or contiguous:
        j = 0
        while j < len(past) - 1:
            if past[j + 1]["s"] - past[j]["e"] >= 0.6 or END_PUNCT.search(past[j]["w"]):
                break
            j += 1
        new_e = min(past[j]["e"] + 0.3, e0 + PAD)
        if new_e - e0 >= 0.3:
            phrase = " ".join(w["w"] for w in past[max(0, j - 5):j + 1])
            out.append({"id": uid(), "kind": "extend_end", "piece": piece_idx, "range": range_idx,
                        "start": e0, "end": round(new_e, 2), "seconds": round(new_e - e0, 1),
                        "reason": f"Speech continues past your range: “…{phrase}” ends at {fmt_time(past[j]['e'])}."})
        else:
            new_e = None
    # --- a big reaction right after the range?
    inner = find_peaks(profile, s0, e0, k=1)
    outer = find_peaks(profile, e0 + 0.3, e0 + PAD, k=1)
    if outer and outer[0]["z"] > 1.5 and (not inner or outer[0]["z"] >= inner[0]["z"] * 0.9):
        cand = min(outer[0]["t"] + 1.8, e0 + PAD)
        if new_e is None or cand > new_e + 0.5:
            out.append({"id": uid(), "kind": "extend_end", "piece": piece_idx, "range": range_idx,
                        "start": e0, "end": round(cand, 2), "seconds": round(cand - e0, 1),
                        "reason": f"There's a loud reaction at {fmt_time(outer[0]['t'])}, just after your range ends — it may be the payoff."})
    return out


def _norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower())


def cut_suggestions(words, rng, piece_idx, profile):
    s0, e0 = rng
    out = []
    for s, e in find_silences(profile, s0, e0):
        cs, ce = s + 0.15, e - 0.15
        if ce - cs >= 0.4:
            out.append({"id": uid(), "kind": "cut", "piece": piece_idx, "start": round(cs, 2),
                        "end": round(ce, 2), "seconds": round(ce - cs, 1),
                        "reason": f"Dead air: {ce - cs:.1f}s of near-silence at {fmt_time(cs)}."})
    ws = [w for w in sorted(words, key=lambda w: w["s"]) if s0 <= w["s"] <= e0]
    # long stretches with nobody talking (game audio only) – optional
    for a, b in zip(ws, ws[1:]):
        gap = b["s"] - a["e"]
        if gap >= 2.5:
            cs, ce = a["e"] + 0.4, b["s"] - 0.4
            if not any(o["start"] <= cs <= o["end"] for o in out):
                out.append({"id": uid(), "kind": "cut", "piece": piece_idx, "start": round(cs, 2),
                            "end": round(ce, 2), "seconds": round(ce - cs, 1), "optional": True,
                            "reason": f"No talking for {gap:.1f}s at {fmt_time(a['e'])}. Keep it if the gameplay is the point."})
    # repeated phrases (3+ words said twice within 6s)
    toks = [_norm(w["w"]) for w in ws]
    seen = {}
    for i in range(len(toks) - 2):
        tri = tuple(toks[i:i + 3])
        if not all(tri) or len(set(tri)) == 1:
            continue  # skip "no no no" – that's a reaction, not a retake
        if tri in seen and ws[i]["s"] - ws[seen[tri]]["s"] < 6 and i - seen[tri] >= 3:
            a = ws[seen[tri]]
            cs, ce = a["s"] - 0.05, ws[i]["s"] - 0.05
            out.append({"id": uid(), "kind": "cut", "piece": piece_idx, "start": round(cs, 2),
                        "end": round(ce, 2), "seconds": round(ce - cs, 1),
                        "reason": f"Repeated line “{' '.join(w['w'] for w in ws[i:i+3])}” — cut the first take."})
            seen.pop(tri)
        else:
            seen.setdefault(tri, i)
    return out


# ---------------------------------------------------------------- captions

HYPE = {
    "no", "nah", "what", "bro", "dude", "yo", "wait", "crazy", "insane", "dead", "died", "run",
    "go", "help", "oh", "omg", "wow", "why", "how", "never", "huge", "massive", "cops", "police",
    "wanted", "stars", "boom", "crash", "money", "million", "rich", "broke", "fire", "jump",
    "fly", "flying", "jail", "busted", "wasted", "lol", "lmao", "stop", "please", "god", "hell",
    "damn", "best", "worst", "first", "last", "literally", "actually", "bruh", "sheesh",
}


def is_emphasis(word: str) -> bool:
    raw = word.strip()
    w = _norm(raw)
    if not w:
        return False
    if (raw.endswith("!") and len(w) > 2) or (raw.isupper() and len(w) > 1) or w.isdigit():
        return True
    return w in HYPE


def build_captions(words, max_words=3, max_chars=18):
    caps, cur = [], []

    def flush():
        if cur:
            text = " ".join(w["w"] for w in cur)
            cand = [i for i, w in enumerate(cur) if is_emphasis(w["w"])]
            emph = [max(cand, key=lambda i: len(_norm(cur[i]["w"])))] if cand else []  # one punchy word per phrase
            caps.append({"id": uid(), "start": cur[0]["s"], "end": cur[-1]["e"], "text": text, "emph": emph,
                         "words": [{"w": w["w"], "s": w["s"], "e": w["e"]} for w in cur]})
            cur.clear()

    for w in sorted(words, key=lambda w: w["s"]):
        if cur:
            gap = w["s"] - cur[-1]["e"]
            chars = len(" ".join(x["w"] for x in cur)) + 1 + len(w["w"])
            if gap > 0.35 or len(cur) >= max_words or chars > max_chars or re.search(r"[.!?,]$", cur[-1]["w"]):
                flush()
        cur.append(w)
    flush()
    # readability: hold each caption a little, but never overlap the next
    for a, b in zip(caps, caps[1:]):
        a["end"] = round(min(max(a["end"], a["start"] + 0.45), b["start"]), 3)
    if caps:
        caps[-1]["end"] = round(max(caps[-1]["end"], caps[-1]["start"] + 0.6), 3)
    return caps


# ---------------------------------------------------------------- layout v2: webcam box + active picture

def active_area(path: Path, W: int, H: int) -> dict:
    """Picture area inside any black stream borders (normalized)."""
    import subprocess
    from .config import tool
    p = subprocess.run([tool("ffmpeg"), "-hide_banner", "-t", "20", "-i", str(path), "-vf",
                        "fps=1,cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", p.stderr)
    if not m:
        return {"x": 0, "y": 0, "w": 1, "h": 1}
    w, h, x, y = map(int, m[-1])
    if w < W * 0.5 or h < H * 0.5:
        return {"x": 0, "y": 0, "w": 1, "h": 1}
    return {"x": x / W, "y": y / H, "w": w / W, "h": h / H}


def game_crop_in(area: dict, W: int, H: int, cx: float = 0.5) -> dict:
    """9:8 gameplay crop using the full height of the active picture, centered at cx."""
    ah, aw = area["h"] * H, area["w"] * W
    ch, cw = ah, ah * 9 / 8
    if cw > aw:
        cw, ch = aw, aw * 8 / 9
    x = min(max(area["x"] * W + cx * aw - cw / 2, area["x"] * W), area["x"] * W + aw - cw)
    y = area["y"] * H + (ah - ch) / 2
    return {"x": x / W, "y": y / H, "w": cw / W, "h": ch / H}


def _edge(profile, lo, hi, near):
    """Strongest persistent edge in [lo, hi), preferring the one closest to the face (`near`)."""
    lo, hi = max(0, int(lo)), min(len(profile), int(hi))
    if hi - lo < 3:
        return None
    seg = profile[lo:hi]
    thr = max(np.median(profile) * 2.5, seg.max() * 0.45)
    cand = [lo + i for i in range(len(seg)) if seg[i] >= thr and (i == 0 or seg[i] >= seg[i - 1]) and (i == len(seg) - 1 or seg[i] >= seg[i + 1])]
    if not cand or seg.max() < np.median(profile) * 2.5:
        return None
    if near is None:
        return lo + int(np.argmax(seg))
    return min(cand, key=lambda c: abs(c - near))


def detect_webcam(path: Path, offset: float, every: float = 1.0) -> dict:
    """Find the streamer's webcam box: stable face position + persistent overlay edges around it."""
    import cv2
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    frames, dets = [], []
    for t, fr in grab_frames(path, every=every, width=1280):
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        frames.append(g.astype(np.float32))
        Hh, Ww = g.shape
        for (x, y, w, h) in casc.detectMultiScale(g, 1.08, 5, minSize=(int(Ww * 0.012), int(Ww * 0.012))):
            dets.append((t, (x + w / 2) / Ww, (y + h / 2) / Hh, w / Ww, h / Hh))
    if not frames:
        return {"found": False}
    Hh, Ww = frames[0].shape
    # stable cluster of face positions
    best, best_n = None, 0
    for d in dets:
        n = sum(1 for e in dets if abs(e[1] - d[1]) < 0.04 and abs(e[2] - d[2]) < 0.05)
        if n > best_n:
            best, best_n = d, n
    times = {round(d[0], 2) for d in dets if best and abs(d[1] - best[1]) < 0.04 and abs(d[2] - best[2]) < 0.05}
    if not best or len(times) < max(3, 0.2 * len(frames)):
        return {"found": False, "samples": len(frames)}
    near = [d for d in dets if abs(d[1] - best[1]) < 0.04 and abs(d[2] - best[2]) < 0.05]
    cx, cy = float(np.median([d[1] for d in near])), float(np.median([d[2] for d in near]))
    fw, fh = float(np.median([d[3] for d in near])), float(np.median([d[4] for d in near]))
    # persistent edges: overlay borders survive averaging, gameplay blurs away
    mean = np.mean(frames, axis=0)
    gx = np.abs(cv2.Sobel(mean, cv2.CV_32F, 1, 0, ksize=3))
    gy = np.abs(cv2.Sobel(mean, cv2.CV_32F, 0, 1, ksize=3))
    px, py, pfw, pfh = cx * Ww, cy * Hh, fw * Ww, fh * Hh
    r0, r1 = int(max(0, py - 1.2 * pfh)), int(min(Hh, py + 1.2 * pfh))
    colp = gx[r0:r1].sum(axis=0)
    left = _edge(colp, px - 7 * pfw, px - 1.0 * pfw, None)
    right = _edge(colp, px + 1.0 * pfw, px + 7 * pfw, None)
    L = left if left is not None else px - 2.5 * pfw
    R = right if right is not None else px + 2.5 * pfw
    rowp = gy[:, int(L):int(R)].sum(axis=1)
    top = _edge(rowp, py - 5 * pfh, py - 0.8 * pfh, py)
    bot = _edge(rowp, py + 0.8 * pfh, py + 6 * pfh, py)
    T = top if top is not None else py - 1.6 * pfh
    B = bot if bot is not None else py + 2.4 * pfh
    box = {"x": L / Ww, "y": T / Hh, "w": (R - L) / Ww, "h": (B - T) / Hh}
    return {"found": True, "box": box, "face": {"cx": cx, "cy": cy, "fw": fw, "fh": fh},
            "edges_found": [left is not None, right is not None, top is not None, bot is not None],
            "coverage": round(len(times) / len(frames), 2)}


def motion_series(path: Path, offset: float, box: dict | None, every: float = 0.5) -> dict:
    """How much the webcam (or whole picture) changes over time – standing up, dancing, jumping."""
    import subprocess
    from .config import tool
    from .media import video_info
    info = video_info(path)
    W, H = info["width"], info["height"]
    if box:
        cw, ch = max(16, int(box["w"] * W) // 2 * 2), max(16, int(box["h"] * H) // 2 * 2)
        vf = f"crop={cw}:{ch}:{int(box['x'] * W)}:{int(box['y'] * H)},"
    else:
        vf = ""
    vf += f"fps={1 / every},scale=64:48,format=gray"
    p = subprocess.run([tool("ffmpeg"), "-v", "error", "-i", str(path), "-vf", vf, "-f", "rawvideo", "-"], capture_output=True)
    a = np.frombuffer(p.stdout, dtype=np.uint8)
    n = len(a) // (64 * 48)
    a = a[: n * 64 * 48].reshape(n, 48, 64).astype(np.float32)
    vals = [0.0] + [float(np.mean(np.abs(a[i] - a[i - 1]))) for i in range(1, n)]
    return {"offset": offset, "every": every, "values": [round(v, 2) for v in vals]}


def caption_words(words, cap):
    return [{"w": w["w"], "s": w["s"], "e": w["e"]} for w in words if cap["start"] - 0.01 <= w["s"] <= cap["end"] + 0.01]
