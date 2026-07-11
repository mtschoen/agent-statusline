#!/usr/bin/env bash
# Pre-warm the statusline interpreter at session start so the FIRST real render
# isn't a ~900ms cold hit. That cold cost is OS-level: Python's DLLs, the native
# orjson/psutil extension modules, and the (large) statusline_lib bytecode all
# load cold off disk, plus a first-launch antivirus scan of python.exe. Once
# warmed, renders settle to ~230ms.
#
# Wired as an async SessionStart hook (see README / settings.json) so it runs in
# the background while you read the screen -- by the time you submit the first
# prompt, the interpreter and its caches are warm. All output is discarded; this
# never affects what the statusline shows.
#
# We feed a payload carrying this repo dir as cwd (rather than forwarding the
# hook's stdin) so the warm deterministically exercises the full hot path --
# imports + git branch lookup + the psutil session scan -- regardless of what
# the SessionStart event happens to pipe in.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=interpreter-probe.sh disable=SC1091
source "$DIR/interpreter-probe.sh"

printf '{"cwd":"%s"}' "$DIR" | $PY "$DIR/statusline.py" >/dev/null 2>&1 || true
exit 0
