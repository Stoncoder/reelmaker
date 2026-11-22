"""Checks a fresh install made by install.sh / install.ps1, then that the updater brings an older install up to date.

    python -m tests.update_test <install folder> <older commit sha>

The install must have been made with REELMAKER_BRANCH set to the commit being tested (CI uses github.sha).
"""
import os
import sys
from pathlib import Path

from app import updater

dest, old = Path(sys.argv[1]).resolve(), sys.argv[2]
sha = os.environ["REELMAKER_BRANCH"]
ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra else ""))
    ok &= bool(cond)


# 1. the fresh install
for f in ("app/server.py", "app/updater.py", "setup", "setup.ps1", "Setup.bat", "Update.command", "Update.bat",
          "requirements.txt", ".gitattributes"):
    check(f"installed {f}", (dest / f).is_file())
check("version file = tested commit", (dest / ".reelmaker-version").read_text().strip() == sha)
check("Setup.bat has Windows line endings", b"\r\n" in (dest / "Setup.bat").read_bytes())
if os.name != "nt":
    check("setup is runnable", os.access(dest / "setup", os.X_OK))
check("zip install detected", updater.install_kind(dest) == "zip")
c = updater.check(force=True, root=dest)
check("fresh install is up to date", not c["update_available"] and not c["error"], c["error"] or "")

# 2. pretend it's an older install with an out-of-date file, plus things an update must never touch
good_server = (dest / "app" / "server.py").read_bytes()
(dest / ".reelmaker-version").write_text(old + "\n")
(dest / "app" / "server.py").write_text("# old version\n")
(dest / "app" / "story.py").unlink()
(dest / "models").mkdir(exist_ok=True)
(dest / "models" / "keep.bin").write_text("model")
(dest / ".venv").mkdir(exist_ok=True)
(dest / ".venv" / "keep.txt").write_text("venv")
c = updater.check(force=True, root=dest)
check("older install sees the update", c["update_available"] and c["can_apply"], c["error"] or "")
check("what's new is listed", len(c["whats_new"]) > 0, "; ".join(c["whats_new"][:3]))
res = updater.apply(print, root=dest, run_setup=False)
check("version file updated", (dest / ".reelmaker-version").read_text().strip() == sha)
check("changed file replaced", (dest / "app" / "server.py").read_bytes() == good_server)
check("missing file restored", (dest / "app" / "story.py").is_file())
check("only changed files written", 0 < len(res["changed"]) < 10, ", ".join(res["changed"]))
check("models/ kept", (dest / "models" / "keep.bin").read_text() == "model")
check(".venv kept", (dest / ".venv" / "keep.txt").read_text() == "venv")
check("no leftover temp files", not list(dest.rglob("*.update-tmp")))
if os.name != "nt":
    check("setup still runnable", os.access(dest / "setup", os.X_OK))
c = updater.check(force=True, root=dest)
check("up to date after updating", not c["update_available"])

print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
