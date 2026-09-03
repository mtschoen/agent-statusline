"""Kimi Code CLI statusline entry point.

Thin shim (same canonical-model fold as qwen_statusline.py, PLAN.md): all
rendering logic lives in statusline_lib (statusline_lib/kimi.py's
render_kimi_statusline), reached through the resident server behind
`--statusline-platform kimi`. This file exists under its own literal path
because the command installed into ~/.kimi-code/tui.toml's [status_line]
command invokes it directly (kimi-statusline-command.sh / `py -3
kimi_statusline.py`); it just forwards into statusline_client.py instead of
duplicating its own copy of the client.

The platform flag must be injected into sys.argv BEFORE importing
statusline_client: statusline_client's application_directory() reads the
platform from sys.argv when STATUSLINE_PLATFORM is unset.
"""

import sys

if not any(arg.startswith("--statusline-platform") for arg in sys.argv):
    sys.argv += ["--statusline-platform", "kimi"]

# aislop's hallucinated-import rule resolves local *packages* (directories with
# __init__.py) but not root-level top-level modules, so it reads this as a
# missing pip dependency. statusline_client.py is a real sibling file. See
# issue #23.
# aislop-ignore-next-line hallucinated-import -- real root-level sibling module
import statusline_client

if __name__ == "__main__":
    sys.exit(statusline_client.main("kimi"))
