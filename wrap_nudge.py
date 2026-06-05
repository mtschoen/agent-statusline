"""UserPromptSubmit hook: a one-shot nudge to consider ``/wrap`` once a session
crosses the 250K-token context-hygiene line (NOT a pricing boundary -- the 1M
tier bills flat; see ``statusline_lib/nudge.py``).

Reads the per-session occupancy file that ``statusline.py`` writes -- the
UserPromptSubmit payload itself can't see ``context_window`` -- so it needs no
transcript walk. Fires at most once per session via a marker file, and emits
its text as UserPromptSubmit ``additionalContext``. On any error it stays silent
and exits 0 so it can never block a prompt.

See ``statusline_lib/nudge.py`` for the shared state-file contract, and the
README ("Wrap nudge") for install and rationale.
"""

import datetime
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statusline_lib.nudge import (
    format_nudge,
    read_ctx_used,
    should_nudge,
    write_marker,
)


def _log_error(message):
    """Best-effort append a timestamped line to the hook's error log, so a
    failure is recorded somewhere durable instead of vanishing. The hook must
    never break because logging broke, so any OSError is swallowed. We log to a
    file rather than stdout on purpose: stdout would inject the error into
    Claude's context on every prompt while the hook is broken."""
    try:
        log = os.path.join(os.path.expanduser("~/.claude"), "wrap_nudge_hook.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {message}\n")
    except OSError:
        # Logging is best-effort; a full disk or unwritable ~/.claude must not
        # turn a benign nudge failure into a hook crash.
        pass


def _emit(context_text):
    """Print the UserPromptSubmit additionalContext envelope to stdout."""
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context_text,
            }
        },
        sys.stdout,
    )


def run(stdin_text):
    """Core decision, separated from I/O for testing. Returns the context string
    to emit, or None to stay silent."""
    try:
        payload = json.loads(stdin_text or "{}")
    except ValueError:
        return None
    session_id = payload.get("session_id") or ""
    if not session_id:
        return None
    ctx_used = read_ctx_used(session_id)
    if not should_nudge(ctx_used, session_id):
        return None
    write_marker(session_id)
    return format_nudge(ctx_used)


def main():
    context_text = run(sys.stdin.read())
    if context_text:
        _emit(context_text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Never break prompt submission: record the failure to a log file (not
        # stdout, which would inject the error into Claude's context) and exit 0.
        _log_error(
            "wrap_nudge.py raised: " + traceback.format_exc().replace("\n", " | ")
        )
        sys.exit(0)
