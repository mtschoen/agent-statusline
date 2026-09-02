#!/usr/bin/env bash
# Thin shim: forward stdin to kimi_statusline.py. All logic lives there.
DIR="$(dirname "$0")"
export STATUSLINE_PLATFORM="${STATUSLINE_PLATFORM:-kimi}"
# shellcheck source=interpreter-probe.sh disable=SC1091
source "$DIR/interpreter-probe.sh"
exec $PY "$DIR/kimi_statusline.py" "$@"
