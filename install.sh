#!/usr/bin/env bash
# Wire both statusLine and subagentStatusLine into ~/.claude/settings.json.
# Re-run any time -- it preserves every other key in settings.json.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
exec "$PY" "$SCRIPT_DIR/install.py" --repo "$SCRIPT_DIR" "$@"
