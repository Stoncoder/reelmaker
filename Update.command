#!/bin/zsh
# Double-click to update Reelmaker to the newest version on GitHub. Your reels, edits and settings are kept.
# (Reelmaker also has an ⬆ Update button at the top of its page when an update is available.)
cd "$(dirname "$0")" || exit 1
if [[ -x .venv/bin/python ]]; then
  .venv/bin/python -m app.updater --apply
else
  echo "Reelmaker isn't set up yet — running Setup first."
  ./setup
fi
echo
read -k1 "?Press any key to close this window."
