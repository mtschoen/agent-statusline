#!/usr/bin/env bash
# Install statusline hooks/settings for the selected platform.
# Re-run any time -- it preserves unrelated existing keys.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
exec "$PY" "$SCRIPT_DIR/install.py" --repo "$SCRIPT_DIR" "$@"
