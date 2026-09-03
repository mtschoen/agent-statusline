"""Qwen Code statusline entry point.

Thin shim (wave-3 canonical-model fold, PLAN.md): all rendering logic lives
in statusline_lib (statusline_lib/qwen.py's render_qwen_statusline), reached
through the resident server behind `--statusline-platform qwen`. This file
still exists, unrenamed, because deployed Qwen Code machines invoke it by
its literal path (qwen-statusline-command.sh / .bat -> qwen_statusline.py);
it just forwards into statusline_client.py instead of duplicating its own
copy of the client.

The platform flag must be injected into sys.argv BEFORE importing
statusline_client: statusline_client's application_directory() reads the
platform from sys.argv when STATUSLINE_PLATFORM is unset.
"""

import sys

if not any(arg.startswith("--statusline-platform") for arg in sys.argv):
    sys.argv += ["--statusline-platform", "qwen"]

# aislop's hallucinated-import rule resolves local *packages* (directories with
# __init__.py) but not root-level top-level modules, so it reads this as a
# missing pip dependency. statusline_client.py is a real sibling file. See
# issue #23.
# aislop-ignore-next-line hallucinated-import -- real root-level sibling module
import statusline_client

if __name__ == "__main__":
    sys.exit(statusline_client.main("qwen"))
