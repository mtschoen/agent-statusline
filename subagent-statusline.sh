#!/usr/bin/env bash
# Thin shim: forward stdin to subagent_statusline.py. All logic lives there.
# See statusline-command.sh for why we prefer the `py` launcher over bare
# python/python3 (Microsoft Store alias shim ~750ms startup vs python.org ~50ms).
if command -v py >/dev/null 2>&1; then
  PY="py -3"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi
exec $PY "$(dirname "$0")/subagent_statusline.py" "$@"
