#!/usr/bin/env bash
# Thin shim: forward stdin to statusline.py. All logic lives there.
#
# Interpreter choice matters a LOT here: on Windows, bare `python`/`python3`
# resolve to the Microsoft Store app-execution-alias shim, whose ~750ms
# per-invocation launch overhead dominated the whole render (statusline went
# from ~1000ms -> ~280ms just by switching off it). The `py` launcher points at
# the python.org build (~50ms startup) and is where orjson/psutil are installed.
# On Linux `py` doesn't exist, so we fall back to python3/python (already fast).
if command -v py >/dev/null 2>&1; then
  PY="py -3"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi
exec $PY "$(dirname "$0")/statusline.py" "$@"
