"""Resident statusline server entry point.

Thin shim, same shape as qwen_statusline.py: one process per app_dir(),
started by statusline_client.py when it finds no live server. All logic lives
in statusline_lib/server.py. Do not rename this file: statusline_client.py
spawns it by its literal path.
"""

import sys

from statusline_lib.server import serve

if __name__ == "__main__":
    sys.exit(serve(sys.argv[1:]))
