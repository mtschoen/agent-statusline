"""Verify the resident server's request half: statusline_lib.server.Server
answers every request kind out of its in-memory tables.

No socket is involved anywhere in this file, which is the point of the
split: handle_request is a plain method over a dict, so the whole server
minus its transport is testable in process. Every check drives it against a
synthetic home built by scripts/_render_fixture_helpers.build_fixture_home,
with an injected clock and a pool that records submissions instead of
running them, so no refresher, subprocess or socket ever starts and no
check reads the wall clock or the real ~/.claude.

The socket half is verified by scripts/verify_server_socket.py (bind,
close, server.json) and scripts/verify_server_loop.py (serve_forever
over real datagrams, and serve). Both import the fixtures below.

Run from anywhere; imports from agent-statusline by path.
"""

import atexit
import glob
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolation has to be installed BEFORE statusline_lib is imported: several of
# its modules resolve app_dir()-based paths at import time, and a render
# reached through the server reads teammate, quota and session-count state
# from whatever home was resolved then.
_HOME = tempfile.mkdtemp(prefix="verify-server-requests-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
os.environ["CLAUDE_STATE_DIR"] = os.path.join(_HOME, "state")
os.environ["STATUSLINE_PREFS_PATH"] = os.devnull

from _render_fixture_helpers import build_fixture_home

# One real transcript in the synthetic home, so the claude kind exercises the
# incremental fold against a file that actually exists.
_PROJECTS = build_fixture_home(_HOME, n_sessions=1, turns_per_session=3)
_TRANSCRIPT = glob.glob(os.path.join(_PROJECTS, "*.jsonl"))[0]
_SESSION_ID = os.path.basename(_TRANSCRIPT)[: -len(".jsonl")]

from statusline_lib.nudge import read_ctx_used
from statusline_lib.server import (
    _HANDLER_NAMES,
    REQUEST_KINDS,
    Server,
    last_render_path,
    write_last_render,
)
from statusline_lib.server_jobs import set_refresh_sink

_ENCODING = "utf-8"
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_DIR = os.environ["CLAUDE_STATE_DIR"]
_ERROR_LOG = os.path.join(_HOME, "server-error.log")


class _FakeClock:
    """A mutable, injectable stand-in for time.time."""

    def __init__(self):
        self.now = 1_700_000_000.0

    def read(self):
        return self.now


class _RecordingPool:
    """The WorkerPool surface Server actually uses, recording submissions
    instead of running them. Nothing this pool is handed ever executes, so a
    render's refresh requests stay observable and inert."""

    def __init__(self):
        self.submitted = []

    def submit(self, kind, argument):
        self.submitted.append((kind, argument))
        return True

    def queue_depth(self):
        return len(self.submitted)

    def in_flight_count(self):
        return 0

    def peak_in_flight(self):
        return 0

    def worker_count(self):
        return 0


def _server(clock=None, pool=None):
    """A server wired to the fixture home, an injected clock and a recording
    pool. Task 13's socket checks reuse this helper."""
    return Server(
        _STATE_DIR,
        _REPO,
        clock=(clock or _FakeClock()).read,
        pool=pool or _RecordingPool(),
        error_log_path=_ERROR_LOG,
    )


def _claude_payload(cwd=_REPO, session_id=_SESSION_ID):
    """A Claude Code payload whose context_window sums to 40110 tokens, the
    figure the wrap-nudge occupancy check asserts on."""
    return {
        "session_id": session_id,
        "transcript_path": _TRANSCRIPT,
        "workspace": {"project_dir": cwd, "current_dir": cwd},
        "cwd": cwd,
        "context_window": {
            "context_window_size": 200_000,
            "current_usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 40_000,
            },
        },
        "model": {"id": "claude-opus-4-8", "display_name": "Opus"},
        "cost": {
            "total_cost_usd": 1.0,
            "total_lines_added": 10,
            "total_lines_removed": 2,
        },
    }


def _subagent_payload():
    return {
        "session_id": _SESSION_ID,
        "transcript_path": _TRANSCRIPT,
        "tasks": [
            {
                "id": "task-one",
                "description": "a running task",
                "status": "running",
                "startTime": (1_700_000_000.0 - 5) * 1000,
            }
        ],
    }


def _kimi_payload(cwd=_REPO):
    return {
        "sessionId": "kimi-session-0001",
        "cwd": cwd,
        "model": "kimi-k2",
        "contextTokens": 12_000,
        "maxContextTokens": 200_000,
        "gitBranch": "main",
        "version": "0.29.2",
    }


def _qwen_payload(cwd=_REPO):
    return {
        "cwd": cwd,
        "model": {"display_name": "qwen3-coder"},
        "context_window": {
            "context_window_size": 256_000,
            "current_usage": 12_000,
        },
        "metrics": {},
    }


def _raise(payload):
    """Stand-in renderer that fails the way a real one would: assigned onto
    the instance, so it only takes effect because dispatch is by name."""
    del payload
    raise RuntimeError("this render was always going to fail")


def check_request_kinds_match_the_dispatch_table(failures):
    """REQUEST_KINDS is the documented protocol surface. A kind that is
    served but undocumented, documented but unserved, or named for a method
    that does not exist, is a wire bug the client cannot diagnose."""
    expected = ("claude", "subagent", "kimi", "qwen", "shutdown", "status")
    if expected != REQUEST_KINDS:
        failures.append(f"REQUEST_KINDS should be {expected}, got {REQUEST_KINDS}")
    if tuple(_HANDLER_NAMES) != REQUEST_KINDS:
        failures.append(f"the dispatch table disagrees: {tuple(_HANDLER_NAMES)}")
    for kind, name in _HANDLER_NAMES.items():
        if not callable(getattr(Server, name, None)):
            failures.append(f"the {kind} kind names a missing handler: {name}")


def check_every_render_kind_replies(failures):
    server = _server()
    for kind, payload in (
        ("claude", _claude_payload()),
        ("subagent", _subagent_payload()),
        ("kimi", _kimi_payload()),
        ("qwen", _qwen_payload()),
    ):
        reply = server.handle_request({"kind": kind, "payload": payload})
        if not reply:
            failures.append(f"the {kind} kind produced no reply")


def check_a_render_writes_the_client_fallback_file(failures):
    server = _server()
    reply = server.handle_request({"kind": "claude", "payload": _claude_payload()})
    path = last_render_path(_SESSION_ID, _STATE_DIR)
    with open(path, encoding=_ENCODING) as f:
        if f.read() != reply:
            failures.append("last-render file must hold exactly what was replied")


def check_a_render_writes_the_wrap_nudge_occupancy_file(failures):
    server = _server()
    server.handle_request({"kind": "claude", "payload": _claude_payload()})
    if read_ctx_used(_SESSION_ID, state_dir=_STATE_DIR) != 40110:
        failures.append("the server must keep writing the wrap-nudge occupancy state")


def check_a_render_requests_one_session_count_job_for_every_cwd(failures):
    """One machine-wide process walk answers every live directory, so the
    server submits the whole set as a single job rather than one per cwd."""
    pool = _RecordingPool()
    server = _server(pool=pool)
    server.handle_request({"kind": "claude", "payload": _claude_payload(cwd="/repo-a")})
    server.handle_request({"kind": "claude", "payload": _claude_payload(cwd="/repo-b")})
    counts = [argument for kind, argument in pool.submitted if kind == "session-count"]
    if counts[-1] != ("/repo-a", "/repo-b"):
        failures.append(f"the last session-count job must carry every cwd: {counts}")


def check_a_raising_render_logs_and_replies_nothing(failures):
    """A server exception must never leave a client waiting: no reply is sent,
    the client falls through to its own fallback inside its own timeout."""
    server = _server()
    server._render_claude = _raise
    reply = server.handle_request({"kind": "claude", "payload": _claude_payload()})
    if reply is not None:
        failures.append("a failed render must reply nothing at all")
    with open(_ERROR_LOG, encoding=_ENCODING) as f:
        if "Traceback" not in f.read():
            failures.append("a failed render must log its traceback")


def check_an_unknown_kind_replies_nothing(failures):
    if _server().handle_request({"kind": "nonsense", "payload": {}}) is not None:
        failures.append("an unknown kind must reply nothing")
    with open(_ERROR_LOG, encoding=_ENCODING) as f:
        if "nonsense" not in f.read():
            failures.append("an unrecognized kind must be logged, naming the kind")


def check_shutdown_sets_the_stop_flag(failures):
    server = _server()
    reply = server.handle_request({"kind": "shutdown"})
    if not server.stop_requested:
        failures.append("shutdown must set stop_requested")
    if reply is not None:
        failures.append("shutdown replies nothing")
    with open(_ERROR_LOG, encoding=_ENCODING) as f:
        if "shutdown requested" not in f.read():
            failures.append("a shutdown request must be logged")


def check_status_replies_a_json_summary(failures):
    server = _server()
    server.handle_request({"kind": "claude", "payload": _claude_payload()})
    summary = json.loads(server.handle_request({"kind": "status"}))
    for key in (
        "uptime_seconds",
        "sessions",
        "cwds",
        "queue_depth",
        "version",
        "pid",
        "departed_client_resets",
    ):
        if key not in summary:
            failures.append(f"status reply is missing {key}")


def check_a_render_touches_the_cwd_table(failures):
    server = _server()
    server.handle_request({"kind": "claude", "payload": _claude_payload(cwd="/repo-a")})
    server.handle_request({"kind": "kimi", "payload": _kimi_payload(cwd="/repo-b")})
    summary = json.loads(server.handle_request({"kind": "status"}))
    if summary["cwds"] != 2:
        failures.append(f"two distinct cwds should be tracked, got {summary}")


def check_housekeeping_runs_at_most_hourly(failures):
    """Driven with the fake clock: two requests one minute apart sweep once,
    a third an hour later sweeps again."""
    clock = _FakeClock()
    sweeps = []
    server = _server(clock=clock)
    server._housekeeper = lambda directory, now: sweeps.append(now) or 0
    server.maybe_housekeep()
    server.handle_request({"kind": "claude", "payload": _claude_payload()})
    clock.now += 60
    server.handle_request({"kind": "claude", "payload": _claude_payload()})
    if len(sweeps) != 1:
        failures.append(f"housekeeping ran {len(sweeps)} times in one minute")
    clock.now += 3601
    server.handle_request({"kind": "claude", "payload": _claude_payload()})
    if len(sweeps) != 2:
        failures.append(f"housekeeping should have run twice by now: {sweeps}")


def check_a_qwen_render_writes_no_last_render_file(failures):
    """Qwen's payload carries no session id, so there is no key to file a
    last render under. The render still has to reply."""
    server = _server()
    reply = server.handle_request({"kind": "qwen", "payload": _qwen_payload()})
    if not reply:
        failures.append("a qwen render must reply even with no session id")
    if os.path.exists(last_render_path("", _STATE_DIR)):
        failures.append("no last-render file may be written without a session id")


def check_an_unwritable_state_directory_costs_only_the_fallback_file(failures):
    """The last-render file is a client convenience, so a failed write must
    cost the fallback and never the reply."""
    with tempfile.TemporaryDirectory() as tmp:
        blocker = os.path.join(tmp, "not-a-directory")
        with open(blocker, "w", encoding=_ENCODING) as f:
            f.write("blocker")
        unwritable = os.path.join(blocker, "state")
        # Must not raise: a file sits where makedirs needs a directory.
        write_last_render(_SESSION_ID, "text", unwritable)
        if os.path.exists(last_render_path(_SESSION_ID, unwritable)):
            failures.append("the unwritable-path check never reached its error arm")


def check_a_pool_job_error_reaches_the_server_log(failures):
    """The pool hands a raising job to the server's error logger; a refresher
    that blows up must leave a traceback rather than vanish."""
    server = _server()
    try:
        raise RuntimeError("a refresher blew up")
    except RuntimeError as error:
        server._log_job_error(error)
    with open(_ERROR_LOG, encoding=_ENCODING) as f:
        if "a refresher blew up" not in f.read():
            failures.append("a pool job error must reach the server error log")


def check_the_injectable_dependencies_all_have_defaults(failures):
    """Constructed with nothing but its two required arguments, a server
    builds its own tables, its own worker pool and its own error log path.
    The pool is never started here, so a submitted job only queues."""
    previous = set_refresh_sink(None)
    try:
        server = Server(_STATE_DIR, _REPO)
        if not server.handle_request({"kind": "claude", "payload": _claude_payload()}):
            failures.append("a default-constructed server must still render")
        status = json.loads(server.handle_request({"kind": "status"}))
        if status["queue_depth"] < 1:
            failures.append(f"the default pool should have queued a job: {status}")
        if not server._error_log_path.endswith(".statusline-error.log"):
            failures.append(f"unexpected default log: {server._error_log_path}")
        server._pool.stop()
    finally:
        set_refresh_sink(previous)


def check(failures):
    check_request_kinds_match_the_dispatch_table(failures)
    check_every_render_kind_replies(failures)
    check_a_render_writes_the_client_fallback_file(failures)
    check_a_render_writes_the_wrap_nudge_occupancy_file(failures)
    check_a_render_requests_one_session_count_job_for_every_cwd(failures)
    check_a_raising_render_logs_and_replies_nothing(failures)
    check_an_unknown_kind_replies_nothing(failures)
    check_shutdown_sets_the_stop_flag(failures)
    check_status_replies_a_json_summary(failures)
    check_a_render_touches_the_cwd_table(failures)
    check_housekeeping_runs_at_most_hourly(failures)
    check_a_qwen_render_writes_no_last_render_file(failures)
    check_an_unwritable_state_directory_costs_only_the_fallback_file(failures)
    check_a_pool_job_error_reaches_the_server_log(failures)
    check_the_injectable_dependencies_all_have_defaults(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: server request handling verified")


if __name__ == "__main__":
    main()
