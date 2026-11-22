"""Updates: is there a newer Reelmaker on GitHub, and install it.

Two kinds of installs:
  zip — made by install.sh / install.ps1 or a downloaded ZIP. The installed commit is in .reelmaker-version.
        Updating downloads that exact commit as a ZIP and replaces only the files that changed.
  git — a `git clone`. Updating is a fast-forward `git pull` (only on the main branch, with no local edits).
Afterwards setup runs in update mode (no questions) so new packages, tools or models get installed.
Never touched: .venv, .venv-tts, models/ — and your reels and settings, which live outside this folder anyway.

Command line (used by Update.command / Update.bat and the installers):
    python -m app.updater            check and say whether an update is available
    python -m app.updater --apply    install it (then restarts a running Reelmaker if it can)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = os.environ.get("REELMAKER_REPO", "Stoncoder/reelmaker")
BRANCH = os.environ.get("REELMAKER_BRANCH", "main")
ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ".reelmaker-version"
KEEP = {".venv", ".venv-tts", "models", VERSION_FILE, ".git"}   # never replaced or removed by an update
UA = {"User-Agent": "Reelmaker-updater", "Accept": "application/vnd.github+json"}
if os.environ.get("REELMAKER_GITHUB_TOKEN"):   # optional (used by the CI test): a higher GitHub API limit
    UA["Authorization"] = "Bearer " + os.environ["REELMAKER_GITHUB_TOKEN"]
_CACHE: dict = {}


class UpdateError(RuntimeError):
    pass


def _get(url: str, timeout: float = 20):
    import requests
    try:
        hdr = UA if url.startswith("https://api.github.com/") else {k: v for k, v in UA.items() if k != "Authorization"}
        r = requests.get(url, headers=hdr, timeout=timeout)
    except requests.RequestException:
        raise UpdateError("Couldn't reach GitHub — check your internet connection.")
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise UpdateError("GitHub's limit for update checks is reached for now — try again in an hour.")
    if r.status_code != 200:
        raise UpdateError(f"GitHub answered {r.status_code} for {url.split('github.com/')[-1]}")
    return r


def _git(root: Path, *args, timeout=60) -> subprocess.CompletedProcess:
    from .system import NO_WINDOW
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=NO_WINDOW)


def install_kind(root: Path = ROOT) -> str:
    return "git" if (root / ".git").exists() and shutil.which("git") else "zip"


def current_version(root: Path = ROOT) -> str | None:
    if install_kind(root) == "git":
        p = _git(root, "rev-parse", "HEAD")
        return p.stdout.strip() if p.returncode == 0 else None
    try:
        v = (root / VERSION_FILE).read_text().strip()
        return v if len(v) == 40 else None
    except OSError:
        return None


def latest() -> dict:
    """The newest commit on GitHub (cached for 30 minutes)."""
    c = _CACHE.get("latest")
    if c and time.time() - c["at"] < 1800:
        return c
    d = _get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}").json()
    c = {"sha": d["sha"], "date": d["commit"]["committer"]["date"], "message": d["commit"]["message"].split("\n")[0],
         "at": time.time()}
    _CACHE["latest"] = c
    return c


def whats_new(old: str | None, new: str) -> list[str]:
    """First lines of the commit messages between the installed version and the newest one."""
    if not old:
        return []
    try:
        d = _get(f"https://api.github.com/repos/{REPO}/compare/{old}...{new}").json()
        return [c["commit"]["message"].split("\n")[0] for c in d.get("commits", [])][-15:][::-1]
    except UpdateError:
        return []


def check(force: bool = False, root: Path = ROOT) -> dict:
    if force:
        _CACHE.pop("latest", None)
    kind, cur = install_kind(root), current_version(root)
    out = {"kind": kind, "current": cur, "current_short": (cur or "")[:7] or "unknown", "repo": REPO,
           "branch": BRANCH, "update_available": False, "can_apply": False, "reason": None, "whats_new": [],
           "latest": None, "latest_short": None, "latest_date": None, "error": None}
    try:
        lt = latest()
    except UpdateError as e:
        out["error"] = str(e)
        return out
    out.update(latest=lt["sha"], latest_short=lt["sha"][:7], latest_date=lt["date"])
    if cur == lt["sha"]:
        return out
    if kind == "git":   # a developer checkout: only fast-forward a clean main branch
        _git(root, "fetch", "--quiet", "origin", BRANCH)
        behind = _git(root, "rev-list", "--count", f"HEAD..origin/{BRANCH}").stdout.strip()
        if not behind or behind == "0":
            return out   # already has it (or is ahead: your own unpushed work)
        out["update_available"] = True
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        dirty = _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
        if branch != BRANCH:
            out["reason"] = f"This is a git checkout on branch '{branch}'. Switch to {BRANCH} and run git pull to update."
        elif dirty:
            out["reason"] = "This git checkout has local changes. Commit or stash them, then run git pull to update."
        else:
            out["can_apply"] = True
    else:
        out["update_available"] = out["can_apply"] = True
        if not cur:
            out["reason"] = None   # unknown version (e.g. downloaded ZIP): updating makes it known
    out["whats_new"] = whats_new(cur, lt["sha"])
    return out


def _same(a: Path, b: bytes) -> bool:
    try:
        return a.stat().st_size == len(b) and hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b).digest()
    except OSError:
        return False


def apply(log=print, root: Path = ROOT, run_setup: bool = True) -> dict:
    """Install the newest version. log(text) gets progress lines. Returns {"version", "changed", "setup_ok"}."""
    info = check(force=True, root=root)
    if info["error"]:
        raise UpdateError(info["error"])
    if not info["update_available"]:
        log(f"Already up to date ({info['current_short']}).")
        return {"version": info["current"], "changed": [], "setup_ok": True, "up_to_date": True}
    if not info["can_apply"]:
        raise UpdateError(info["reason"])
    sha = info["latest"]
    changed: list[str] = []
    if info["kind"] == "git":
        log(f"Updating with git to {sha[:7]}…")
        p = _git(root, "pull", "--ff-only", "origin", BRANCH, timeout=300)
        if p.returncode != 0:
            raise UpdateError("git pull failed: " + (p.stderr or p.stdout).strip()[-300:])
        changed = ["(git pull)"]
    else:
        log(f"Downloading Reelmaker {sha[:7]}…")
        data = _get(f"https://codeload.github.com/{REPO}/zip/{sha}", timeout=120).content
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for item in z.infolist():
                parts = item.filename.split("/", 1)   # "reelmaker-<sha>/app/server.py" -> "app/server.py"
                if len(parts) < 2 or not parts[1] or item.is_dir():
                    continue
                rel = parts[1]
                if rel.split("/")[0] in KEEP:
                    continue
                dst = root / rel
                body = z.read(item)
                if _same(dst, body):
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".update-tmp")
                tmp.write_bytes(body)
                mode = (item.external_attr >> 16) & 0o777
                if mode & 0o111 and os.name != "nt":
                    os.chmod(tmp, mode)   # keep scripts runnable on macOS
                os.replace(tmp, dst)
                changed.append(rel)
        (root / VERSION_FILE).write_text(sha + "\n")
        log(f"Updated {len(changed)} file(s)." if changed else "No files changed.")
    setup_ok = True
    if run_setup and not os.environ.get("REELMAKER_UPDATE_NO_SETUP"):   # the env switch is for testing
        setup_ok = run_setup_update(log, root)
    return {"version": sha, "changed": changed, "setup_ok": setup_ok, "up_to_date": False}


def run_setup_update(log=print, root: Path = ROOT) -> bool:
    """Run setup in update mode (no questions; installs only what's new or missing)."""
    from .system import IS_WIN, NO_WINDOW
    log("Checking tools, packages and models (setup in update mode)…")
    cmd = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "setup.ps1"), "-Update"] if IS_WIN
           else ["/bin/zsh", str(root / "setup"), "--update"])
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
    p = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", env=env, creationflags=NO_WINDOW)
    for line in p.stdout:
        line = line.rstrip()
        if line:
            log(line)
    ok = p.wait() == 0
    log("Setup finished." if ok else "Setup reported a problem (see above). Run Setup again if Reelmaker misbehaves.")
    return ok


def _restart_running(port: int = 8765) -> str:
    """After a command-line update: ask a running Reelmaker to restart onto the new version."""
    import requests
    try:
        requests.get(f"http://127.0.0.1:{port}/api/state", timeout=1)
    except requests.RequestException:
        return "Start Reelmaker as usual (Start Reelmaker on your Desktop)."
    try:
        r = requests.post(f"http://127.0.0.1:{port}/api/restart", json={"force": False}, timeout=5)
        if r.ok:
            return "Reelmaker is restarting onto the new version."
    except requests.RequestException:
        pass
    return "Reelmaker is running: click ⟳ Restart in it (or close its window and start it again) to use the new version."


def main(argv: list[str]) -> int:
    root = ROOT
    if "--dir" in argv:
        root = Path(argv[argv.index("--dir") + 1]).resolve()
    if "--apply" not in argv:
        c = check(force=True, root=root)
        if c["error"]:
            print(c["error"]); return 1
        if not c["update_available"]:
            print(f"Reelmaker is up to date ({c['current_short']})."); return 0
        print(f"Update available: {c['current_short']} -> {c['latest_short']}")
        for m in c["whats_new"]:
            print("  - " + m)
        print(c["reason"] or "Run with --apply to install it (or use Update in Reelmaker).")
        return 0
    try:
        res = apply(print, root=root)
    except UpdateError as e:
        print("Update failed: " + str(e)); return 1
    if not res.get("up_to_date"):
        print(f"\nReelmaker is now at {res['version'][:7]}. " + _restart_running(int(os.environ.get("REELMAKER_PORT", "8765"))))
    return 0 if res["setup_ok"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
