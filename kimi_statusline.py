"""Kimi Code CLI statusline entry point.

Thin shim (same canonical-model fold as qwen_statusline.py, PLAN.md): all
rendering logic lives in statusline.py's single entry point, behind
`--statusline-platform kimi` (see statusline_lib/kimi.py for the actual
Kimi payload adapter, statusline_lib.kimi.render_kimi_statusline). This
file exists under its own literal path because the command installed into
~/.kimi-code/tui.toml's [status_line] command invokes it directly
(kimi-statusline-command.sh / `py -3 kimi_statusline.py`); it just forwards
into statusline.py instead of duplicating its own copy of the rendering
loop.

The platform flag must be injected into sys.argv BEFORE importing
statusline: statusline.py resolves its log paths (app_dir()-based) at
import time, and app_dir() reads the platform from sys.argv.
"""

import contextlib
import sys
import time

if not any(arg.startswith("--statusline-platform") for arg in sys.argv):
    sys.argv += ["--statusline-platform", "kimi"]

import statusline
from statusline_lib import RED, RESET
from statusline_lib.rendertimer import record_render


def main():
    return statusline.main()


if __name__ == "__main__":
    _started = time.monotonic()
    _session_id = None
    try:
        _session_id = main()
    except Exception:
        statusline._log_error()
        with contextlib.suppress(Exception):
            sys.stdout.write(
                f"{RED}STATUSLINE ERROR{RESET} — see {statusline._ERROR_LOG}"
            )
    _elapsed_ms = (time.monotonic() - _started) * 1000
    # Excludes interpreter+import startup, same "warm core" scope as
    # statusline.py's slow-render clock -- see statusline_lib/rendertimer.py.
    record_render(_elapsed_ms, _session_id)
