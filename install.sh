#!/bin/zsh
# Reelmaker one-line installer for macOS (Apple silicon). Paste this into Terminal:
#
#   curl -fsSL https://raw.githubusercontent.com/Stoncoder/reelmaker/main/install.sh | zsh
#
# Downloads Reelmaker into ~/Documents/Reelmaker (no git needed) and runs its setup. If Reelmaker is already
# installed there, it updates it instead. Options (environment variables): REELMAKER_DIR=/other/folder
# REELMAKER_REPO=user/repo  REELMAKER_BRANCH=main  REELMAKER_SKIP_SETUP=1 (download only)

kbd() {   # run a command with answers from the keyboard (stdin is this piped script); no keyboard: defaults
  if [[ -r /dev/tty ]] && { : </dev/tty; } 2>/dev/null; then "$@" </dev/tty; else "$@" --yes </dev/null; fi
}

main() {   # everything runs inside a function, so a half-downloaded script can't run half-way
  set -e
  local REPO="${REELMAKER_REPO:-Stoncoder/reelmaker}" BRANCH="${REELMAKER_BRANCH:-main}"
  local DEST="${REELMAKER_DIR:-$HOME/Documents/Reelmaker}"
  print -P "%BReelmaker installer%b  ($REPO, $BRANCH → $DEST)"

  if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
    echo "Reelmaker needs a Mac with Apple silicon (M1 or newer). On Windows, use the PowerShell installer in the README."
    return 1
  fi

  local OVERLAY=""
  if [[ -f "$DEST/setup" && -d "$DEST/app" ]]; then
    echo "Reelmaker is already installed in $DEST — updating it instead."
    if [[ -x "$DEST/.venv/bin/python" && -f "$DEST/app/updater.py" ]]; then
      (cd "$DEST" && kbd .venv/bin/python -m app.updater --apply)
      return
    fi
    OVERLAY=1   # a copy from before the updater existed (or never set up): download the new version over it
  elif [[ -e "$DEST" && -n "$(ls -A "$DEST" 2>/dev/null)" ]]; then
    echo "$DEST already exists and isn't a Reelmaker folder. Move it away (or set REELMAKER_DIR=another/folder) and run this again."
    return 1
  fi

  echo "Finding the latest version…"
  local SHA
  SHA=$(curl -fsSL "https://api.github.com/repos/$REPO/commits/$BRANCH" | grep -m1 '"sha"' | sed -E 's/.*"sha": *"([0-9a-f]{40})".*/\1/')
  if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then echo "Couldn't reach GitHub. Check your internet connection and try again."; return 1; fi

  local TMP; TMP=$(mktemp -d)
  echo "Downloading Reelmaker ${SHA:0:7}…"
  curl -fsSL -o "$TMP/reelmaker.zip" "https://codeload.github.com/$REPO/zip/$SHA"
  unzip -q "$TMP/reelmaker.zip" -d "$TMP/x"
  local SRC; SRC=$(find "$TMP/x" -mindepth 1 -maxdepth 1 -type d | head -1)
  mkdir -p "$DEST"
  cp -R "$SRC/." "$DEST/"
  echo "$SHA" > "$DEST/.reelmaker-version"
  rm -rf "$TMP"
  chmod +x "$DEST/setup" "$DEST/reelmaker" "$DEST"/*.command 2>/dev/null || true
  echo "Downloaded to $DEST"

  if [[ -n "$REELMAKER_SKIP_SETUP" ]]; then echo "(setup skipped)"; return; fi
  cd "$DEST"
  if [[ -n "$OVERLAY" && -x .venv/bin/python ]]; then kbd ./setup --update; else kbd ./setup; fi
}

main "$@"
