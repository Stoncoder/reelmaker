"""Finding moments in a long video and cutting a short story out of a range."""
from __future__ import annotations

import json
import re

import numpy as np
import requests

from .ai import ollama_ok
from .config import OLLAMA_MODEL, OLLAMA_URL, fmt_time


# ------------------------------------------------------------------ signals

def _series(prof_or_motion, key):
    vals = np.array(prof_or_motion[key], dtype=float)
    step = prof_or_motion.get("hop") or prof_or_motion.get("every")
    t = prof_or_motion["offset"] + np.arange(len(vals)) * step
    return t, vals


def energy(profile, motion, start, end, step=1.0):
    """Per-second reaction score: loud audio + lots of webcam movement (z-scores, summed)."""
    bins = np.arange(start, end, step)
    if len(bins) == 0:
        return bins, np.array([])

    def binned(t, v):
        out = np.array([v[(t >= b) & (t < b + step)].mean() if ((t >= b) & (t < b + step)).any() else np.nan for b in bins])
        out = np.nan_to_num(out, nan=np.nanmean(out) if np.isfinite(out).any() else 0)
        return (out - out.mean()) / (out.std() + 1e-6)

    score = np.zeros(len(bins))
    if profile and profile.get("db"):
        score += binned(*_series(profile, "db"))
    if motion and motion.get("values"):
        score += 0.8 * binned(*_series(motion, "values"))
    return bins, score


def chunks(words, bins, score, start, end, size=15.0):
    """Condensed transcript: one line per `size` seconds with its reaction score."""
    out = []
    t = start
    while t < end:
        ws = [w["w"] for w in words if t <= w["s"] < t + size]
        sel = (bins >= t) & (bins < t + size)
        sc = float(score[sel].max()) if sel.any() else 0.0
        out.append(f"[{fmt_time(t)} = {t:.0f}s] (reaction {sc:+.1f}) {' '.join(ws) or '(no talking)'}")
        t += size
    return out


def _ask(prompt, ctx=8192, temperature=0.4):
    """Finding moments and cutting: Groq when it's switched on (runs in the cloud, so the Mac stays cool),
    else the local AI. Any Groq problem falls back to the local AI."""
    if online_on():
        from . import online_ai
        from .config import settings
        try:
            return online_ai.chat_json(prompt, settings().get("groq_model") or online_ai.PREFERRED_MODELS[0], temperature, timeout=120)
        except Exception:
            pass
    return _ask_local(prompt, ctx, temperature)


def _ask_local(prompt, ctx=8192, temperature=0.4):
    r = requests.post(f"{OLLAMA_URL}/api/generate", timeout=600, json={
        "model": OLLAMA_MODEL, "prompt": prompt, "format": "json", "stream": False, "keep_alive": "30s",
        "options": {"temperature": temperature, "num_ctx": ctx}})
    return json.loads(r.json()["response"])


# ------------------------------------------------------------------ locate moments in a whole video

LOCATE = """You are finding clips for short vertical reels in a long gaming stream.
Creator: {creator}

Below is the stream transcript in {size:.0f}-second chunks. Each line starts with the time in seconds and a
"reaction" score (higher = louder voice and more movement on the webcam — jumping, dancing, standing up).

{lines}

TASK: {task}
Each clip should be {lo}–{hi} seconds long and must include the setup and the full reaction/payoff.
Return JSON only: {{"clips": [{{"start": seconds, "end": seconds, "scenario": "what happens, in one sentence", "why": "why it works"}}]}}"""


def locate(words, profile, motion, duration, scenarios, count, creator, target):
    bins, score = energy(profile, motion, 0, duration)
    lo, hi = (max(15, target - 15), target + 40) if target else (20, 90)
    target = target or 35
    found = []
    if ollama_ok():
        size = 15.0 if duration < 1800 else 30.0
        lines = "\n".join(chunks(words, bins, score, 0, duration, size))
        if scenarios:
            task = ("Find the ONE clip that best matches each of these scenarios, in this order:\n"
                    + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(scenarios)))
        else:
            task = f"Find the {count} best, most entertaining moments (big reactions, funny lines, reveals). No overlaps."
        try:
            data = _ask(LOCATE.format(creator=creator or "the streamer", size=size, lines=lines, task=task, lo=lo, hi=hi),
                        ctx=min(32768, 4096 + len(lines) // 3))
            for c in data.get("clips", []):
                s, e = float(c["start"]), float(c["end"])
                if 0 <= s < e <= duration + 1:
                    if e - s < lo:
                        e = min(duration, s + lo)
                    if e - s > hi + 30:
                        e = s + hi + 30
                    found.append({"start": s, "end": e, "scenario": str(c.get("scenario", ""))[:200],
                                  "why": str(c.get("why", ""))[:200], "source": "ai"})
        except Exception:
            found = []
    want = len(scenarios) if scenarios else count
    # top up with the strongest reaction windows if the AI found too few
    if len(found) < want and len(score):
        order = np.argsort(-score)
        for i in order:
            t = float(bins[i])
            if all(not (f["start"] - 10 < t < f["end"] + 10) for f in found):
                s = max(0.0, t - target * 0.7)
                found.append({"start": s, "end": min(duration, t + target * 0.3 + 4),
                              "scenario": scenarios[len(found)] if scenarios and len(found) < len(scenarios) else "Big reaction",
                              "why": f"Loudest reaction around {fmt_time(t)}", "source": "signals"})
            if len(found) >= want:
                break
    return found[:want]


# ------------------------------------------------------------------ story cut inside a range

CUT = """You are helping edit a short vertical reel from a gaming stream clip.
Creator: {creator}
What happens (creator's notes): {scenario}

Transcript, 5-second chunks, with times in seconds and a reaction score
(higher = louder voice and more webcam movement):
{lines}

Find three moments, all between {start:.0f} and {end:.0f}:
- "payoff": the single biggest reaction / reveal / funniest moment (seconds)
- "setup_start": where the viewer needs to start watching to understand it (a few seconds of context before the payoff)
- "reaction_end": when the reaction has fully landed (end on the laugh, the shout, the punchline)
Return JSON only: {{"setup_start": n, "payoff": n, "reaction_end": n, "story": "setup -> payoff in one sentence", "confidence": "high|medium|low"}}"""


def _natural_end(t, words, hard_end, max_extra=8.0):
    """Push an ending to the end of what's being said: never stop mid-sentence or mid-reaction.
    Follows the speech until the first real pause (0.9s), at most max_extra seconds further."""
    end = t
    for w in sorted((w for w in words if w["e"] > t - 0.3), key=lambda w: w["s"]):
        if w["s"] - end > 0.9 or w["e"] > min(hard_end, t + max_extra):
            break
        end = max(end, w["e"])
    return min(hard_end, end + 0.6)


def smart_cut(words, profile, motion, peaks, start, end, scenario, creator, target, silences, avoid=None, hard_end=None):
    """Returns dict(segments=[(s,e)], cold_open, payoff, hooks, story, confidence, source).
    target=0 means auto: keep the moment's natural length (15–60s), only dropping dead air and dull stretches.
    hard_end: the last second of footage there is (the fetched piece goes a bit past the range), so an ending
    that's still being spoken can run on past the range instead of being chopped."""
    auto = not target
    hard_end = max(end, hard_end or end)
    if auto:
        target = 30
    bins, score = energy(profile, motion, start, end)
    payoff = setup = rend = None
    story_note, conf, source = "", "low", "signals"
    if ollama_ok():
        try:
            d = _ask(CUT.format(creator=creator or "the streamer", scenario=scenario or "(no notes)",
                                lines="\n".join(chunks(words, bins, score, start, end, 5.0)), start=start, end=end), ctx=8192)
            vals = [d.get(k) for k in ("setup_start", "payoff", "reaction_end")]
            if all(isinstance(v, (int, float)) for v in vals):
                setup, payoff, rend = (float(v) for v in vals)
                if not (start - 1 <= setup < payoff < rend <= end + 1):
                    setup = payoff = rend = None
                else:
                    story_note, conf, source = str(d.get("story", ""))[:300], d.get("confidence", "medium"), "ai"
        except Exception:
            pass
    if payoff is None:  # biggest reaction in the range
        payoff = float(bins[int(np.argmax(score))]) if len(score) else end - 3
        setup, rend = payoff - target * 0.65, payoff + 5
    # THE ENDING: the payoff plus the whole reaction. A short moment's own end is its ending (that's where you or
    # the finder said it ends), and whatever is still being said then runs on to the next pause.
    rend = max(rend, payoff + 6)
    if end - start <= (90 if auto else max(target * 1.5, 30)):
        rend = max(rend, end)
    setup = max(start, setup)
    rend = _natural_end(min(rend, hard_end), words, hard_end)
    if auto:
        if rend - setup < 15:
            setup = max(start, rend - 15)
    elif rend - setup < min(target, end - start) * 0.6:  # too short: add context before the payoff
        setup = max(start, rend - target)

    # trim to the target by dropping the least interesting stretches (never the payoff, never the opening second)
    step = 2.5
    pieces = [[t, min(t + step, rend)] for t in np.arange(setup, rend, step)]
    sil = [(a, b) for a, b in silences if b - a >= 0.6]
    def interest(pc):
        sel = (bins >= pc[0] - 0.5) & (bins < pc[1])
        talk = sum(1 for w in words if pc[0] <= w["s"] < pc[1])
        quiet = sum(min(b, pc[1]) - max(a, pc[0]) for a, b in sil if a < pc[1] and b > pc[0])
        return (float(score[sel].mean()) if sel.any() else 0) + 0.25 * talk - 1.5 * quiet
    keep = [True] * len(pieces)
    # never trim the opening second, the payoff, or anything after it: the reaction IS the ending
    protected = {i for i, pc in enumerate(pieces) if pc[1] > payoff - 3} | {0}
    total = rend - setup
    if auto:
        # drop clearly dull pieces (no talk, quiet, low reaction), then cap at 60s
        vals = [interest(pc) for pc in pieces]
        med = float(np.median(vals)) if vals else 0
        for i, pc in enumerate(pieces):
            if i not in protected and total > 15 and vals[i] < med - 1.0 and not any(pc[0] <= w["s"] < pc[1] for w in words):
                keep[i] = False
                total -= pc[1] - pc[0]
        target = min(60, max(15, total))
    for i in sorted(range(len(pieces)), key=lambda i: interest(pieces[i])):
        if total <= target + 2:
            break
        if i in protected:
            continue
        keep[i] = False
        total -= pieces[i][1] - pieces[i][0]
    segs = []
    for pc, k in zip(pieces, keep):
        if not k:
            continue
        if segs and abs(segs[-1][1] - pc[0]) < 1e-6:
            segs[-1][1] = pc[1]
        else:
            segs.append(list(pc))
    result = {"segments": [tuple(x) for x in segs], "cold_open": None, "payoff": round(payoff, 2), "hooks": [],
              "story": story_note, "confidence": conf, "source": source}
    if score.size and conf != "low":
        result["cold_open"] = (round(payoff - 0.6, 2), round(payoff + 1.0, 2))
    if ollama_ok() or online_on():
        kept = [w for w in words if any(a <= w["s"] <= b for a, b in result["segments"])]
        result["hooks"] = write_hooks(scenario, creator, " ".join(w["w"] for w in kept)[:1500], story_note, avoid=avoid)
    # snap cut points to natural pauses between words so nobody gets cut off mid-word
    ws = sorted(words, key=lambda w: w["s"])
    gaps = [start, end] + [(a["e"] + b["s"]) / 2 for a, b in zip(ws, ws[1:]) if b["s"] - a["e"] >= 0.2]

    def snap(t, lo, hi):
        c = [g for g in gaps if lo <= g <= hi]
        return min(c, key=lambda g: abs(g - t)) if c else t

    out = [(snap(a, a - 1.2, a + 0.8), snap(b, b - 0.8, b + 1.2)) for a, b in result["segments"]]
    if out:   # the very last cut only ever moves later (to a pause), never earlier into the last words
        a, b = result["segments"][-1]
        out[-1] = (out[-1][0], max(b, snap(b, b, min(hard_end, b + 1.5))))
    merged = []
    for a, b in out:
        if merged and a - merged[-1][1] < 0.4:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        elif b - a >= 0.8:
            merged.append((a, b))
    result["segments"] = [(round(float(a), 2), round(float(b), 2)) for a, b in merged]
    return result


# Hooks that do well on Shorts/TikTok/Reels read like a fan texting a friend about the clip, not like a headline
# or an AI summary. The model writes 10 candidates in that voice, then a second pass (the "editor") throws out
# anything false or AI-sounding and keeps the 5 best, best first.
HOOKS = """You are a top gaming clipper who posts {creator_short} clips on TikTok, Shorts and Reels and gets millions of views.
Write the on-screen hook for ONE clip. It shows in big letters for the first 2 seconds and is also spoken.
Its only job: make someone who is scrolling stop and watch.

WHAT HAPPENS IN THE CLIP (written by the editor who watched it — this is the truth, trust it over the transcript):
{scenario}
Story notes: {story}
What is said in the clip (auto-transcribed, may have mistakes): "{said}"
{idea}
HOW GREAT HOOKS SOUND — like a fan texting a friend, not a news headline or a robot:
- 5 to 9 words. one idea. plain words a 15-year-old uses.
- must make sense to a stranger who has NEVER seen this stream (no inside words, no cryptic two-word fragments)
- specific to THIS clip: the real thing (the save file, 98%, the pool shot, the ending, 52 hours), never vague
- open a loop: the viewer must watch to find out what happens. hint at the stakes, never say how it ends
- casual and human: "bro", "nah", "lowkey", "POV:", "when…", "he really…", sentence fragments are good
- can be the viewer's reaction, the streamer's own best line, a quiet understatement, or the stakes right before it goes wrong
Examples of the STYLE only (from other clips — never copy their words):
  "speed hasn't slept in 52 hours and THIS happens"
  "he pressed one button and lost 3 days"
  "nobody tell him about the 2 million"
  "bro was one shot away"
  "POV: you trusted the save menu"
  "the way his face drops at 0:04"

NEVER write like this (instant scroll-past, sounds AI-made):
- headline / summary words: shocked, unexpected, epic, insane, crazy, intense, ultimate, legendary, unbelievable,
  reveals, unfolds, decides, triggers, leads to, the fate of, journey, chaos ensues, moment of truth, game changer
- "X's reaction to Y is everything", "X was not ready for Y", "X got Y doing Z", "you won't believe", "wait for it"
- colons that announce things ("The Impossible Shot: 1 Ball, 1 Chance"), hashtags, emojis, question marks
- inventing anything that isn't in the description, or changing a number from it

Use the name "{creator_short}" in at most 3 of the 10 (most great hooks just say "he" or "bro").
All 10 must be DIFFERENT ideas — different angle, different first words — not the same line reworded.
{avoid}
For tone only, hooks this creator liked before (never copy):
{examples}

Return JSON only: {{"event": "one plain sentence: what concretely happens and what's at stake",
"hooks": ["...", "...", "...", "...", "...", "...", "...", "...", "...", "..."]}}"""

DEFAULT_EXAMPLES = ["VICE CITY GOT SPEED OUT OF HIS CHAIR"]

# phrases that make a hook sound templated or AI-written; a hook containing one is thrown away
BANNED = re.compile(r"\b(was not ready|wasn'?t ready|not ready for|is everything|reaction to|waited all (night|stream|day)|"
                    r"sent (him|her|them|\w+) (crazy|wild|flying)|you won'?t believe|wait for it|insane|crazy|epic|"
                    r"shock(ed|ing)?|unexpected(ly)?|intense|ultimate|legendary|unbelievable|reveals?|unfolds?|"
                    r"decides?|triggers?|triggering|leads to|the fate|journey|ensues|moment of truth|game ?changer|"
                    r"your heart (races|sinks|drops|stops)|desperation|showdown|stuns?|jaw.?dropping|nada)\b", re.I)


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", h.lower()).strip()


JUDGE = """You are the editor of a gaming clip channel with millions of followers. Pick the hooks to post.
WHAT HAPPENS IN THE CLIP (the truth): {scenario}
Facts: {event}

Score each hook 1-10 for: would a real person scrolling TikTok stop for this?
- 1 if it says anything false or not in the facts (wrong numbers, people or events, other moments of the stream)
- at most 3 if it sounds AI-written or like a news headline (formal words, title-case announcements, "X's reaction",
  vague "this moment"), or if it's a random quote that means nothing without context
- at most 4 if a stranger who never saw the stream wouldn't understand it (cryptic fragments like "SHOT OR NO SHOT")
- high scores: short, specific, casual like a fan talking, opens a loop, makes you need to see the next 5 seconds

HOOKS:
{hooks}

Return JSON only: {{"scores": [{{"i": 1, "score": n}}, ...]}}"""


def _judge(scenario, event, hooks):
    """The editor pass: drop false / AI-sounding hooks, best first."""
    try:
        d = _hook_ask(JUDGE.format(scenario=scenario or "(see facts)", event=event or "-",
                                   hooks="\n".join(f"{i + 1}. {h}" for i, h in enumerate(hooks))), ctx=4096, temperature=0.1)
        sc = {int(x["i"]) - 1: float(x["score"]) for x in d.get("scores", []) if isinstance(x, dict) and "i" in x and "score" in x}
    except Exception:
        return hooks
    if not sc:
        return hooks
    ranked = sorted(range(len(hooks)), key=lambda i: -sc.get(i, 5))
    good = [hooks[i] for i in ranked if sc.get(i, 5) >= 6]
    return good if len(good) >= 3 else [hooks[i] for i in ranked][:5]


def _too_similar(h: str, others: list) -> bool:
    """Same line reworded? (same first two words, or mostly the same words)"""
    w = _norm(h).split()
    for o in others:
        ow = _norm(o).split()
        if w[:2] == ow[:2]:
            return True
        a, b = set(w), set(ow)
        if a and b and len(a & b) / len(a | b) >= 0.6:
            return True
    return False


def short_name(creator: str) -> str:
    """What fans call the creator in a hook: "iShowSpeed" -> "Speed", "xQc" stays."""
    c = (creator or "").strip()
    for pre in ("ishow", "its", "the", "real", "official"):
        if c.lower().startswith(pre) and len(c) > len(pre) + 2:
            c = c[len(pre):]
            return c[:1].upper() + c[1:]
    return c or "he"


HOOK_INFO: dict = {}   # who wrote the last hooks: {"source": "groq"|"local", "model": ..., "note": why it fell back}


def online_on() -> bool:
    """Is the free online writer (Groq) switched on, with a key saved?"""
    from .config import settings
    if settings().get("hook_ai") != "groq":
        return False
    from . import online_ai
    return bool(online_ai.get_key()[0])


def _hook_ask(prompt, ctx, temperature, writing=False):
    """Hook prompts go to Groq when it's switched on; on any Groq problem, to the local AI instead."""
    from .config import settings
    s = settings()
    if s.get("hook_ai") == "groq":
        from . import online_ai
        key, _ = online_ai.get_key()
        if not key:
            HOOK_INFO["note"] = "Groq is switched on but no key is saved, so the local AI wrote these."
        else:
            model = s.get("groq_model") or online_ai.PREFERRED_MODELS[0]
            try:
                d = online_ai.chat_json(prompt, model, temperature)
                if writing:
                    HOOK_INFO.update(source="groq", model=model)
                return d
            except online_ai.OnlineAIError as e:
                HOOK_INFO["note"] = str(e)
    if writing:
        HOOK_INFO.update(source="local", model=OLLAMA_MODEL)
    return _ask_local(prompt, ctx=ctx, temperature=temperature)


def write_hooks(scenario, creator, said, story_note="", idea="", avoid=None):
    """5 hooks for this clip. `avoid` = hooks already used by other reels (so a batch doesn't repeat itself)."""
    from .config import settings
    ex = [e for e in ((settings().get("hook_examples") or []) or DEFAULT_EXAMPLES) if not BANNED.search(e)] or DEFAULT_EXAMPLES
    avoid = [a for a in (avoid or []) if a][-30:]
    avoid_txt = ("Hooks already used for OTHER reels from this stream — don't reuse their wording or sentence shape:\n"
                 + "\n".join(f"  - {a}" for a in avoid)) if avoid else ""
    HOOK_INFO.clear()
    seen = {_norm(e) for e in ex} | {_norm(a) for a in avoid} | ({_norm(scenario)} if scenario else set())
    out: list[str] = []
    event = ""
    for attempt in range(2):   # a second try only if too many hooks were thrown away
        try:
            d = _hook_ask(HOOKS.format(creator_short=short_name(creator),
                                  scenario=scenario or "(no description: work it out from the transcript)",
                                  story=story_note or "-", said=(said or "(nothing)")[:1500],
                                  idea=(f"\nTHE EDITOR'S OWN IDEA (most important — keep its meaning, make it punchier;"
                                        f" the first hook must be a polished version of it): {idea}\n" if idea else ""),
                                  avoid=avoid_txt, examples="\n".join(f"  - {e}" for e in ex[-6:])),
                     ctx=6144, temperature=0.75, writing=True)
        except Exception:
            continue
        event = event or str(d.get("event", ""))[:300]
        for item in d.get("hooks", []):
            h = item.get("text") if isinstance(item, dict) else item
            if not isinstance(h, str):
                continue
            h = h.strip().strip('"').rstrip("?.!").strip()
            if not (4 <= len(h.split()) <= 11) or BANNED.search(h) or _norm(h) in seen or _too_similar(h, out):
                continue
            seen.add(_norm(h))
            out.append(h.upper())
        if len(out) >= 6:
            break
    if len(out) > 1 and not idea:   # with the editor's own idea, keep theirs first as asked
        out = _judge(scenario, event, out)
    return out[:5]
