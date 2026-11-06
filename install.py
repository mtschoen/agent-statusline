"""Entry point for the installer (the real code is ``installer.py``).

Kept to syntax that Python 3.4+ can parse, so that an interpreter below the
supported floor (``python_floor.MIN_PYTHON``) is refused with one readable
stderr line instead of a SyntaxError traceback from ``installer.py``. See
``installer.py`` for what it installs and the supported platforms.

Usage (typically via the install.sh / install.bat wrappers):
    python install.py --repo /abs/path/to/repo [--platform claude|codex|qwen|both|antigravity|pi|kimi] [--dry-run]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from python_floor import is_too_old, too_old_message


def main():
    if is_too_old(sys.version_info):
        print(
            too_old_message(sys.version_info, sys.executable, "install.py"),
            file=sys.stderr,
        )
        return 1
    import installer

    return installer.main()


if __name__ == "__main__":
    sys.exit(main())
