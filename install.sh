#!/usr/bin/env bash
# A.R.I.A. one-shot installer — makes this clone an always-on application:
#
#   ./install.sh
#
# What it does (all per-user, no sudo):
#   1. sanity-checks the setup (aria doctor)
#   2. installs the desktop entry (A.R.I.A. in your app menu)
#   3. installs systemd user services:
#        aria.service    — server at login  -> http://127.0.0.1:8700
#        aria-auto.timer — pipeline self-runs hourly (recursive mode)
#   4. installs the skills and specialist agents listed in
#      system/skill-sources.txt (Anthropic, ECC, …) and builds the skills
#      pack (needs network; skipped cleanly if offline)
#
# Optional extras afterwards (these need sudo, so they are yours to run):
#   sudo pacman -S bubblewrap       # she runs each project's tests, sandboxed
#   sudo pacman -S python-pyqt6-webengine   # a window of her own (aria app)
#   sudo pacman -S python-pytest libnotify  # fuller tests; notifications
#   pip install faster-whisper      # local voice input for Firefox
#   ollama pull qwen3:30b           # bigger 'smart' tier
#   ollama pull gpt-oss:120b        # 'deep' tier (needs ~64GB RAM)
#   bin/aria llama install          # llama.cpp Vulkan speed path
set -uo pipefail

cd "$(dirname "$0")"
echo "== A.R.I.A. installer =="

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v systemctl >/dev/null || { echo "systemd (user) is required for the service"; exit 1; }

echo; echo "-- 1/4 doctor"
bin/aria doctor || echo "   (warnings above are fine to fix later)"

echo; echo "-- 2/4 desktop entry"
bin/aria install-desktop

echo; echo "-- 3/4 always-on services"
bin/aria install-service

echo; echo "-- 4/4 skills"
if bin/aria install-skills; then
  bin/aria skills convert | tail -2
else
  echo "   skipped (offline?) — rerun later: bin/aria install-skills && bin/aria skills convert"
fi

echo
echo "== done =="
echo "open the app:   from your app menu (A.R.I.A.), or http://127.0.0.1:8700"
echo "talk to her:    'capture <idea>' -> read the plan -> 'approve <project>'"
echo "                -> 'work on <project>' ... 'stop working on <project>'"
echo "check it works: bin/aria test --mock   (seconds, no GPU needed)"
echo "her reach:      Settings -> Internet, Obsidian brain, Git & GitHub"
echo "voice (Firefox): pip install faster-whisper     Alexa: docs/ALEXA.md"
echo "bigger models:  ollama pull qwen3:30b   then Settings -> smart: ollama/qwen3:30b"
echo "giant models:   bin/aria models scan     (one role each, loaded on demand)"
echo "worth adding:   sudo pacman -S bubblewrap python-pyqt6-webengine"
echo "                (sandboxed tests of what she writes; a window of her own)"
echo "what is missing: bin/aria doctor"
echo "stop recursion: systemctl --user stop aria-auto.timer"
