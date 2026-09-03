"""Shared fixtures for verify_server_requests.py and the other render suites
built on it: the synthetic ~/.claude a suite renders against, and the home
redirection that keeps every one of them off the developer's real one.
"""

import atexit
import json
import os
import shutil
import tempfile
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_fixture_home(root, n_sessions=8, turns_per_session=40):
    """Synthetic ~/.claude with enough transcript bulk to make walks real."""
    projects = os.path.join(root, ".claude", "projects", "C--fixture-proj")
    os.makedirs(projects, exist_ok=True)
    now_iso = "2026-07-11T00:00:00.000Z"
    for _ in range(n_sessions):
        sid = str(uuid.uuid4())
        lines = []
        for t in range(turns_per_session):
            lines.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": now_iso,
                        "message": {
                            "model": "claude-opus-4-8",
                            "usage": {
                                "input_tokens": 10 + t,
                                "output_tokens": 20 + t,
                                "cache_read_input_tokens": 1000,
                                "cache_creation_input_tokens": 50,
                            },
                        },
                    }
                )
            )
        with open(os.path.join(projects, f"{sid}.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    return projects


def isolate_home(prefix):
    """Point HOME, USERPROFILE, CLAUDE_STATE_DIR and the prefs path at a fresh
    temp directory, removed at exit, and return it.

    Call this BEFORE importing statusline_lib. Several of its modules resolve
    app_dir()-based paths at import time, and a render reaches the prefs file,
    the session-count cache and ~/.claude/teams through whatever home was
    resolved then, so a suite that skips this is scored against the
    developer's own live statusline state rather than its fixtures.
    """
    home = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    os.environ["HOME"] = home
    os.environ["USERPROFILE"] = home
    os.environ["CLAUDE_STATE_DIR"] = os.path.join(home, "state")
    os.environ["STATUSLINE_PREFS_PATH"] = os.devnull
    return home
