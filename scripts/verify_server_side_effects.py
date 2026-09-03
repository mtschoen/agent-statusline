"""Verify the resident server's side effects beyond the reply text: the
`.statusline-input.log` / `.subagent-statusline-input.log` dumps and the
render-timer duration record it writes for every render it serves (see
server_render.write_debug_input_log and server_render.record_render_timing
for why each exists).

Reuses scripts/verify_server_requests.py's fixtures (the isolated HOME, the
fixture transcript, `_server()`/`_claude_payload()`) by import rather than
rebuilding them, so the two files can never drift on what "the fixture home"
means. No socket is involved here either, same as verify_server_requests.py:
handle_request is driven directly.

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_server_requests import (
    _SESSION_ID,
    _STATE_DIR,
    _claude_payload,
    _server,
    _subagent_payload,
)

from statusline_lib import server_render
from statusline_lib.render_subagent import _MAIN_INPUT_LOG
from statusline_lib.rendertimer import read_previous
from statusline_lib.server_render import _SUBAGENT_INPUT_LOG

_ENCODING = "utf-8"


def check_a_claude_request_writes_the_input_log(failures):
    server = _server()
    payload = _claude_payload()
    server.handle_request({"kind": "claude", "payload": payload})
    with open(_MAIN_INPUT_LOG, encoding=_ENCODING) as f:
        logged = json.loads(f.read())
    if logged != payload:
        failures.append("the input log must hold the claude request's payload")


def check_a_subagent_request_writes_the_subagent_input_log(failures):
    server = _server()
    payload = _subagent_payload()
    server.handle_request({"kind": "subagent", "payload": payload})
    with open(_SUBAGENT_INPUT_LOG, encoding=_ENCODING) as f:
        logged = json.loads(f.read())
    if logged != payload:
        failures.append(
            "the subagent input log must hold the subagent request's payload"
        )


def check_a_timed_render_records_its_duration(failures):
    previous_timing_env = os.environ.get("STATUSLINE_RENDER_TIMING")
    os.environ["STATUSLINE_RENDER_TIMING"] = "1"
    try:
        server = _server()
        server.handle_request({"kind": "claude", "payload": _claude_payload()})
        recorded = read_previous(_SESSION_ID, _STATE_DIR)
        if recorded is None:
            failures.append("a timed claude render must record a duration")
            return
        last_ms, peak_ms = recorded
        if not isinstance(last_ms, float) or not isinstance(peak_ms, float):
            failures.append(f"a recorded duration must be numeric: {recorded}")
    finally:
        if previous_timing_env is None:
            del os.environ["STATUSLINE_RENDER_TIMING"]
        else:
            os.environ["STATUSLINE_RENDER_TIMING"] = previous_timing_env


def check_a_failing_input_log_write_does_not_change_the_reply(failures):
    """Timing forced off for this one: with it on, the reply legitimately
    changes between calls anyway (format_render_suffix embeds the PREVIOUS
    call's measured duration), which would make this check indistinguishable
    from the thing it is trying to prove. Comparing two calls with the same
    payload only isolates the safe_write failure when nothing else about the
    reply can drift between them."""
    previous_timing_env = os.environ.get("STATUSLINE_RENDER_TIMING")
    os.environ["STATUSLINE_RENDER_TIMING"] = "0"
    try:
        server = _server()
        payload = _claude_payload()
        baseline = server.handle_request({"kind": "claude", "payload": payload})

        def _raise(path, text):
            del path, text
            raise OSError("disk is full")

        original = server_render.safe_write
        server_render.safe_write = _raise
        try:
            reply = server.handle_request({"kind": "claude", "payload": payload})
        finally:
            server_render.safe_write = original
        if reply != baseline:
            failures.append("a failing input log write must not change the reply")
    finally:
        if previous_timing_env is None:
            del os.environ["STATUSLINE_RENDER_TIMING"]
        else:
            os.environ["STATUSLINE_RENDER_TIMING"] = previous_timing_env


def check(failures):
    check_a_claude_request_writes_the_input_log(failures)
    check_a_subagent_request_writes_the_subagent_input_log(failures)
    check_a_timed_render_records_its_duration(failures)
    check_a_failing_input_log_write_does_not_change_the_reply(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: server side effects verified")


if __name__ == "__main__":
    main()
