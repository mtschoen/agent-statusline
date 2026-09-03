"""Detached resident-server process entry point and teardown."""

import os

from .base import state_dir
from .server import Server


def serve(argv=None):
    """Run one resident server and return its process exit code."""
    del argv
    repository_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server = Server(state_dir(), repository_root)
    try:
        server.bind()
        server.maybe_housekeep()
        server.serve_forever()
    except Exception:
        server.log_exception()
        return 1
    finally:
        server.close()
    return 0
