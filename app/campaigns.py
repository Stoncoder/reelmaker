"""Paid clipping campaigns from Content Rewards (contentrewards.com) — read from its public campaign list.

Only what can be auto-clipped from a live stream matters here: streamer campaigns. Each one is turned into
{who to clip (their Twitch / Kick / YouTube channels), how it pays, what's left of the budget, its rules link}.
Nothing here signs in or touches your Content Rewards account; joining a campaign and submitting clips stay yours.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .config import DATA_DIR, read_json, settings, write_json

API = "https://contentrewards.com/api/campaign/campaigns"
SITE = "https://contentrewards.com/c/campaigns/"
IMG = "https://crusercontent.com/cdn-cgi/image/width=480,quality=75,format=auto/"
CACHE = DATA_DIR / "campaigns.json"
TTL = 30 * 60
STREAMER_CATS = {"streamer", "twitch", "kick", "gta"}
_lock = threading.Lock()
_mem: dict = {"t": 0, "data": None}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Reelmaker)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _channels(text: str) -> dict:
    """Twitch / Kick / YouTube channels named in a campaign's description."""
    out = {}
    for plat, rx in (("twitch", r"twitch\.tv/([A-Za-z0-9_]{3,25})"), ("kick", r"kick\.com/([A-Za-z0-9_-]{3,25})"),
                     ("youtube", r"youtube\.com/(@[A-Za-z0-9_.-]{3,40})")):
        m = re.search(rx, text or "", re.I)
        if m and m.group(1).lower() not in ("videos", "clips", "directory", "watch", "categories"):
            out[plat] = m.group(1)
    return out


def _who(name: str) -> str:
    """'$12,500 LACY GTA VI Marathon Pool Campaign' -> 'LACY'; 'Kai Cenat - $10,000 …' -> 'Kai Cenat'."""
    n = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name or "")
    n = re.split(r"\s[-|–—:]\s|\s[x×]\s", n)[0]
    n = re.sub(r"\$[\d,.]+k?|\b\d[\d,.]*\b|\bFR\b|\bDE\b", " ", n, flags=re.I)
    n = re.sub(r"\b(gta\s*(vi|6|v)?|pool|campaign|clipping|clips?|clippe|official|marathon|streams?|twitch|kick|"
               r"cpm|content|budget|per|views?|streamer|dedicated|account|youtube|channel)\b", " ", n, flags=re.I)
    n = re.sub(r"[^\w\s.'-]", " ", n)
    return re.sub(r"\s+", " ", n).strip(" -.'")[:40]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _summary(c: dict, det: dict) -> dict:
    desc = det.get("description") or c.get("description") or ""
    m = c.get("metrics") or {}
    cfg = det.get("configuration") or {}
    kind = det.get("type") or ("pool" if not c.get("primaryPayoutCents") else "cpm")
    links = re.findall(r"https?://[^\s)\]]+", desc)
    rules = next((l for l in links if re.search(r"docs\.google|reachclipping|notion\.|/rules|brief", l, re.I)), None)
    budget = int(c.get("budgetCents") or cfg.get("budget") or 0)
    left = max(0, budget - int(m.get("budgetSpentCents") or 0))
    # can a live stream be clipped for it? not when it only pays for one past stream, or wants a logo on any clip
    past = bool(re.search(r"(must be|sourced|only).{0,40}(from|of).{0,40}(stream|vod)s?\s+(on\s+)?(\d{1,2}[./]\d{1,2}|november|december|october|\w+day)", desc, re.I)
                or re.search(r"VOD.{0,30}from their stream on", desc, re.I))
    logo = bool(re.search(r"\blogo\b", c.get("name", "") + " " + desc[:200], re.I) and not _channels(desc))
    who = _who(c.get("name", ""))
    return {
        "id": c["id"], "name": c.get("name", ""), "who": who, "org": c.get("organizationName") or "",
        "url": SITE + c["id"], "banner": (IMG + c["banner"]) if c.get("banner") else None,
        "kind": kind, "rate_cents": int(c.get("primaryPayoutCents") or 0),
        "max_payout_cents": max([p.get("maxPayoutCents") or 0 for p in c.get("payouts") or []] or [0]),
        "budget_cents": budget, "left_cents": left, "platforms": c.get("platforms") or [],
        "min_clip_views": cfg.get("minClipViews"), "min_creator_views": cfg.get("minCreatorViews"),
        "needs_application": bool(c.get("requiresApplication") or (cfg.get("requirement") or {}).get("preApproval")),
        "categories": [x.get("id") for x in c.get("categories") or []],
        "description": desc.strip()[:1200], "rules_url": rules, "channels": _channels(desc),
        "past_only": past, "logo_only": logo, "status": c.get("status"), "updated": c.get("updatedAt"),
    }


def fetch(refresh: bool = False) -> dict:
    """Active streamer campaigns, best paying first. Cached 30 min (also on disk, so the page opens instantly)."""
    with _lock:
        if not refresh and _mem["data"] and time.time() - _mem["t"] < TTL:
            return _mem["data"]
        if not refresh and not _mem["data"]:
            disk = read_json(CACHE, None)
            if disk and time.time() - disk.get("checked_at", 0) < TTL:
                _mem.update(t=disk["checked_at"], data=disk)
                return disk
    raw, cur = [], None
    for _ in range(14):                       # ~50 per page; the whole marketplace is a few hundred
        q = f"{API}/discover?collapseGroups=true&limit=50&view=summary" + (f"&cursor={urllib.parse.quote(cur)}" if cur else "")
        d = _get(q)
        raw += d.get("data") or []
        cur = (d.get("pagination") or {}).get("nextCursor")
        if not cur or not d.get("data"):
            break
    seen, picked = set(), []
    for c in raw:
        cats = {x.get("id") for x in c.get("categories") or []}
        if c["id"] in seen or c.get("status") != "active" or c.get("private"):
            continue
        seen.add(c["id"])
        if cats & STREAMER_CATS or _channels(c.get("description") or ""):
            picked.append(c)
    old = {x["id"]: x for x in ((_mem["data"] or read_json(CACHE, {}) or {}).get("campaigns") or [])}

    def detail(c):
        o = old.get(c["id"])
        if o and o.get("updated") == c.get("updatedAt") and o.get("_det"):
            return o["_det"]
        try:
            d = _get(f"{API}/{c['id']}").get("data") or {}
            return {k: d.get(k) for k in ("description", "type", "configuration")}
        except Exception:
            return {}

    with ThreadPoolExecutor(8) as ex:
        dets = list(ex.map(detail, picked))
    out = []
    for c, det in zip(picked, dets):
        s = _summary(c, det)
        s["_det"] = det
        out.append(s)
    out.sort(key=lambda s: (s["left_cents"] <= 0, -(s["rate_cents"] * 1000 if s["kind"] == "cpm" else s["left_cents"] / 20)))
    data = {"campaigns": out, "checked_at": time.time(), "total": len(seen)}
    with _lock:
        _mem.update(t=data["checked_at"], data=data)
    write_json(CACHE, data)
    return data


def public(c: dict) -> dict:
    return {k: v for k, v in c.items() if not k.startswith("_")}


# ------------------------------------------------------------------ who to auto-clip

def prefs() -> dict:
    p = dict(settings().get("autopilot") or {})
    p.setdefault("on", False)
    p.setdefault("day", "")
    p.setdefault("per_hour", 4)
    p.setdefault("favorites", ["iShowSpeed", "Kai Cenat"])   # famous creators: clip them even with no paid campaign
    p.setdefault("skip", [])                                 # campaign ids you turned off
    p.setdefault("channels", {})                             # campaign id -> {twitch, kick, youtube} you added
    return p


def clippable(c: dict) -> bool:
    return c["left_cents"] > 0 and not c["past_only"] and not c["logo_only"]


def match(c: dict, creators: list[dict]) -> dict | None:
    """Your creator this campaign is about: same channel handle, or the same name."""
    ch = {k: _norm(v) for k, v in (c.get("channels") or {}).items()}
    for cr in creators:
        for plat in ("twitch", "kick", "youtube"):
            if ch.get(plat) and _norm(cr.get(plat)) == ch[plat]:
                return cr
    who = _norm(c.get("who"))
    if len(who) >= 3:
        for cr in creators:
            n = _norm(cr.get("name"))
            if len(n) >= 3 and (n == who or (len(who) >= 5 and (who in n or n in who))):
                return cr
    return None


def targets(creators: list[dict], camps: list[dict], p: dict | None = None) -> list[dict]:
    """Everyone auto-pilot watches: streamers of paid campaigns you didn't switch off, plus your favourites.
    Each: {name, twitch, kick, youtube, campaign: best paying campaign or None, favorite: bool}."""
    p = p or prefs()
    out: dict[str, dict] = {}
    for c in camps:
        if c["id"] in p["skip"] or not clippable(c):
            continue
        cr = match(c, creators)
        chans = {**(c.get("channels") or {}), **(p["channels"].get(c["id"]) or {})}
        if not cr and not chans:
            continue                                   # nobody to watch yet: add their channel on the Live page
        t = dict(cr or {"name": c["who"] or c["name"]})
        for k, v in chans.items():
            t.setdefault(k, v)
        key = _norm(t["name"])
        prev = out.get(key)
        if not prev or _value(c) > _value(prev["campaign"]):
            t["campaign"] = c
            out[key] = {**(prev or {}), **t}
    favs = {_norm(f) for f in p["favorites"]}
    for cr in creators:
        key = _norm(cr.get("name"))
        if key in favs:
            out[key] = {**cr, **out.get(key, {}), "favorite": True}
            out[key].setdefault("campaign", None)
    return list(out.values())


def _value(c: dict | None) -> float:
    if not c:
        return -1
    return c["rate_cents"] * 1000 if c["kind"] == "cpm" else c["left_cents"] / 20


def tag(c: dict | None) -> dict | None:
    """The bit of a campaign that travels with an auto-clip and its reels."""
    if not c:
        return None
    return {k: c.get(k) for k in ("id", "name", "who", "org", "url", "kind", "rate_cents", "max_payout_cents", "left_cents",
                                   "platforms", "min_clip_views", "min_creator_views", "rules_url", "needs_application")}


def for_creator(name: str, handles: dict | None = None) -> dict | None:
    """Best paying campaign for this creator (used when you start an auto-clip yourself)."""
    try:
        camps = fetch()["campaigns"]
    except Exception:
        return None
    p = prefs()
    cr = {"name": name, **(handles or {})}
    best = None
    for c in camps:
        if c["id"] in p["skip"] or not clippable(c):
            continue
        if match(c, [cr]) and _value(c) > _value(best):
            best = c
    return tag(best)
