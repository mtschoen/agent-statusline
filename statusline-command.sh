#!/usr/bin/env bash
# Thin shim: forward stdin to statusline.py. All logic lives there.
DIR="$(dirname "$0")"
# shellcheck source=interpreter-probe.sh disable=SC1091
source "$DIR/interpreter-probe.sh"
exec $PY "$DIR/statusline.py" "$@"
