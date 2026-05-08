#!/usr/bin/env bash
# Thin shim: forward stdin to subagent_statusline.py. All logic lives there.
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
exec "$PY" "$(dirname "$0")/subagent_statusline.py"
