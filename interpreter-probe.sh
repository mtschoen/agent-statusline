#!/usr/bin/env bash
# Shared interpreter probe, sourced (not executed) by the statusline wrapper
# scripts that live alongside it. Sets $PY as a side effect; callers do
# `exec $PY "$DIR/some_script.py" "$@"`.
#
# Interpreter choice matters a LOT here: on Windows, bare `python`/`python3`
# resolve to the Microsoft Store app-execution-alias shim, whose ~750ms
# per-invocation launch overhead dominated the whole render (statusline went
# from ~1000ms -> ~280ms just by switching off it). The `py` launcher points at
# the python.org build (~50ms startup) and is where orjson/psutil are installed.
# On Linux `py` doesn't exist, so we fall back to python3/python (already fast).
# shellcheck disable=SC2034  # PY is consumed by the script that sources this file
if command -v py >/dev/null 2>&1; then
  PY="py -3"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi
