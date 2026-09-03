"""Shared bounded waits used by resident-server verification scripts."""

import os
import time

_SERVER_JSON_POLL_SECONDS = 0.5
_SERVER_JSON_POLL_INTERVAL_SECONDS = 0.02


def server_json_appeared(path):
    """Return as soon as server.json exists, or False after a bounded poll."""
    deadline = time.monotonic() + _SERVER_JSON_POLL_SECONDS
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(_SERVER_JSON_POLL_INTERVAL_SECONDS)
    return os.path.exists(path)
