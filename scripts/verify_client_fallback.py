"""Verify the statusline client's fallback path: what it prints when the
resident server does not answer.

The contract is two sentences. The user never sees a blank line, and the user
never waits longer than the client's own receive timeout. So every check here
either kills the server's answer (a bound socket nobody reads from, a missing
server.json, a corrupt one) or ages out the file the client would have read,
and then asserts on stdout.

Split out of scripts/verify_server_protocol.py rather than appended to it:
that script is 313 lines and the repository holds every file at or under 400.
Its fixtures are imported rather than rebuilt, so both suites run the real
client subprocess against the same isolated home.

Nothing here waits on a clock. The one age that matters is written with
os.utime against a synthetic timestamp, and the client's own timeout is
150 ms by construction.

Run from anywhere; imports from agent-statusline by path.
"""

import contextlib
import json
import os
import socket
import sys
import time
from unittest import mock

# The scripts directory, so the protocol suite next door is importable. That
# import is also what installs the temporary HOME and CLAUDE_STATE_DIR this
# whole family of suites is isolated by, so it has to happen before any
# statusline_lib module resolves an app_dir()-based path at import time.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_server_protocol import _client_environment, _run_client
from verify_server_requests import (
    _ENCODING,
    _REPO,
    _SESSION_ID,
    _STATE_DIR,
    _claude_payload,
    _kimi_payload,
    _subagent_payload,
)

# Only now the repository root and the package, for the writer side of the
# last-render file: the client reads that file, and this suite proves the two
# ends agree on its name, its encoding and its age.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.server import last_render_path, write_last_render
from statusline_lib.server_info import (
    SERVER_INFO_FILENAME,
    code_version,
    server_info_path,
    write_server_info,
)
from statusline_lib.server_socket import SPAWN_LOCK_FILENAME

# The session id _kimi_payload() carries, under Kimi's camelCase spelling.
_KIMI_SESSION_ID = "kimi-session-0001"

# The prefix server_render gives every last-render file, so a check can sweep
# them between cases without touching the rest of the state directory.
_LAST_RENDER_PREFIX = "last-render-"


class _FallbackContext:
    """What a fallback check needs: the environment a client subprocess runs
    under, and the state directory that client resolves server.json and its
    last-render file from."""

    def __init__(self):
        self.state_directory = _STATE_DIR
        self.environment = _client_environment()


def _remove_state_files(context):
    """Clear server.json and every last-render file. A leftover from one case
    is the difference between a check that proves something and a check that
    reads the previous check's answer."""
    try:
        names = os.listdir(context.state_directory)
    except OSError:
        return
    for name in names:
        if name == SERVER_INFO_FILENAME or name.startswith(_LAST_RENDER_PREFIX):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(context.state_directory, name))


def _spawn_lock_path():
    """The client's single-flight spawn lock, in this suite's state
    directory."""
    return os.path.join(_STATE_DIR, SPAWN_LOCK_FILENAME)


def _hold_spawn_lock():
    """A fresh spawn lock, so a client subprocess that finds no live server
    prints its fallback and declines to start one.

    Without it every case below would leave a real server process behind: a
    missing or unreadable server.json is exactly what the client spawns a
    replacement for, and these fixtures manufacture that on purpose. The lock
    is the production mechanism for "somebody else is already on it", so this
    suppresses the spawn without stubbing anything out.
    """
    path = _spawn_lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=_ENCODING) as f:
        json.dump({"pid": os.getpid(), "at": time.time()}, f)


def _release_spawn_lock():
    """Drop the lock, so the next client is free to spawn for real."""
    with contextlib.suppress(OSError):
        os.remove(_spawn_lock_path())


@contextlib.contextmanager
def _clean_state():
    """A context whose server.json and last-render files start absent and are
    removed again on the way out, so neither this suite's cases nor the
    protocol suite next door can inherit one."""
    context = _FallbackContext()
    os.makedirs(context.state_directory, exist_ok=True)
    _remove_state_files(context)
    _hold_spawn_lock()
    try:
        yield context
    finally:
        _release_spawn_lock()
        _remove_state_files(context)


@contextlib.contextmanager
def _silent_server():
    """server.json pointing at a bound socket nobody ever reads from.

    The client's send succeeds, so this is the timeout arm rather than the
    dead-port arm, and it costs exactly the client's own 150 ms ceiling.
    """
    with _clean_state() as context:
        silent_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            silent_socket.bind(("127.0.0.1", 0))
            write_server_info(
                server_info_path(context.state_directory),
                pid=os.getpid(),
                port=silent_socket.getsockname()[1],
                version=code_version(_REPO),
                started_at=0.0,
                platform="claude",
            )
            yield context
        finally:
            silent_socket.close()


@contextlib.contextmanager
def _no_server():
    """No server.json at all: the cold-start arm, before any server has run."""
    with _clean_state() as context:
        yield context


def _write_last_render(context, text, session_id=_SESSION_ID, age_seconds=0):
    """The last-render file the server would have written, with its mtime
    pinned `age_seconds` into the past. The age is synthetic; nothing here
    sleeps or measures elapsed time."""
    write_last_render(session_id, text, context.state_directory)
    path = last_render_path(session_id, context.state_directory)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def check_a_timeout_falls_back_to_the_last_render_file(failures):
    """A server that never answers must cost the client its timeout and
    nothing else: it prints the recent last-render file instead."""
    with _silent_server() as context:
        _write_last_render(context, "cached line from the last render")
        result = _run_client(context, "claude", _claude_payload())
    if result.stdout.strip() != "cached line from the last render":
        failures.append(f"expected the cached line, got {result.stdout!r}")
    if result.returncode != 0:
        failures.append("a fallback must still exit 0")


def check_a_stale_last_render_file_is_not_used(failures):
    with _silent_server() as context:
        _write_last_render(context, "ancient line", age_seconds=120)
        result = _run_client(context, "claude", _claude_payload())
    if "ancient line" in result.stdout:
        failures.append("a last-render file older than 30s must not be printed")
    if not result.stdout.strip():
        failures.append("the minimal line must never be empty")


def check_the_fallback_age_override_can_accept_an_older_render(failures):
    with _silent_server() as context:
        _write_last_render(context, "accepted older line", age_seconds=120)
        context.environment = _client_environment(
            STATUSLINE_FALLBACK_MAXIMUM_AGE_SECONDS="300"
        )
        result = _run_client(context, "claude", _claude_payload())
    if result.stdout.strip() != "accepted older line":
        failures.append(
            "a 300s fallback-age override did not accept a 120s-old render: "
            f"{result.stdout!r}"
        )


def check_an_empty_last_render_file_falls_through(failures):
    """A zero-length file is a render that legitimately produced no text. It
    is a real answer for the server to have written and a blank line for the
    client to print, so the minimal line takes over."""
    with _silent_server() as context:
        _write_last_render(context, "")
        result = _run_client(context, "claude", _claude_payload())
    if not result.stdout.strip():
        failures.append("an empty last-render file must not produce a blank line")


def check_the_minimal_line_carries_the_payload_basics(failures):
    with _silent_server() as context:
        result = _run_client(context, "claude", _claude_payload())
    for fragment in ("Opus", "%"):
        if fragment not in result.stdout:
            failures.append(f"minimal line is missing {fragment!r}: {result.stdout!r}")


def check_the_minimal_line_handles_every_payload_shape(failures):
    """Model and context reach the client in three shapes: Claude Code's model
    mapping over a mapping of token counts, Qwen's model mapping over a single
    number, and Kimi's bare model string with no context window at all. The
    fourth case is the worst one, and the one the whole task exists for: an
    unreadable stdin is an empty payload, and it still must not print blank.

    In process rather than through a subprocess, because this is formatting
    over a dict and ten more client spawns would prove nothing extra.
    """
    import statusline_client_support

    repository = os.path.basename(_REPO)
    for payload, expected in (
        (_claude_payload(), f"Opus | 20% | {repository}"),
        (
            {
                "model": {"display_name": "qwen3-coder"},
                "context_window": {"context_window_size": 100, "current_usage": 25},
                "cwd": _REPO,
            },
            f"qwen3-coder | 25% | {repository}",
        ),
        (_kimi_payload(), f"kimi-k2 | {repository}"),
        (
            {"model": {"id": "claude-opus-4-8"}, "cwd": "/repo-a/"},
            "claude-opus-4-8 | repo-a",
        ),
    ):
        line = statusline_client_support.minimal_line(payload)
        if line != expected:
            failures.append(f"minimal line was {line!r}, expected {expected!r}")
    if not statusline_client_support.minimal_line({}).strip():
        failures.append("an empty payload must still produce a non-blank line")
    with mock.patch.object(statusline_client_support.os, "getcwd", return_value="/"):
        line = statusline_client_support.minimal_line({})
    if line != "/":
        failures.append(f"an empty payload at the filesystem root returned {line!r}")


def check_no_server_file_at_all_still_prints_a_line(failures):
    with _no_server() as context:
        result = _run_client(context, "claude", _claude_payload())
    if not result.stdout.strip():
        failures.append("a missing server.json must still produce a line")


def check_a_malformed_server_file_still_prints_a_line(failures):
    with _no_server() as context:
        path = server_info_path(context.state_directory)
        with open(path, "w", encoding=_ENCODING) as f:
            f.write("{ this is not json")
        result = _run_client(context, "claude", _claude_payload())
    if not result.stdout.strip():
        failures.append("a corrupt server.json must still produce a line")
    if result.returncode != 0:
        failures.append("a corrupt server.json must not make the client exit non-zero")


def check_the_client_last_render_path_uses_its_state_directory(failures):
    import statusline_client

    expected = last_render_path("direct-shim-session", _STATE_DIR)
    original_state_directory = statusline_client.state_directory
    statusline_client.state_directory = lambda: _STATE_DIR
    try:
        actual = statusline_client.last_render_path("direct-shim-session")
    finally:
        statusline_client.state_directory = original_state_directory
    if actual != expected:
        failures.append(
            f"client last_render_path resolved {actual!r}, expected {expected!r}"
        )


def check_the_kimi_kind_prints_exactly_one_line_on_fallback(failures):
    """Kimi's TUI renders only the first stdout line and requires it to be
    non-empty; a multi-line fallback there would be a regression."""
    with _silent_server() as context:
        _write_last_render(
            context, "first line\nsecond line", session_id=_KIMI_SESSION_ID
        )
        result = _run_client(context, "kimi", _kimi_payload())
    if result.stdout != "first line":
        failures.append(
            f"the kimi fallback must be exactly the cached first line: {result.stdout!r}"
        )


def check_every_session_id_spelling_finds_its_last_render(failures):
    """The server names the file with whichever spelling the harness sent:
    Claude Code's session_id, Antigravity's conversation_id, Kimi's camelCase
    sessionId. A client that knew only one would fall back to a minimal line
    forever on the other two."""
    for key, session_id in (
        ("session_id", "spelling-session"),
        ("conversation_id", "spelling-conversation"),
        ("sessionId", "spelling-camel"),
    ):
        with _silent_server() as context:
            _write_last_render(context, f"cached for {key}", session_id=session_id)
            result = _run_client(context, "claude", {key: session_id, "cwd": _REPO})
        if result.stdout.strip() != f"cached for {key}":
            failures.append(
                f"the {key} spelling did not find its last render: {result.stdout!r}"
            )


def check_the_subagent_fallback_never_prints_a_claude_line(failures):
    """The subagent payload carries the same session id as the Claude one, so
    a shared last-render lookup would paste a Claude statusline into a panel
    that renders one JSON row per line."""
    with _silent_server() as context:
        _write_last_render(context, "Opus | 20% | agent-statusline")
        result = _run_client(context, "subagent", _subagent_payload())
    if "Opus" in result.stdout:
        failures.append(
            f"the subagent fallback printed a claude line: {result.stdout!r}"
        )
    for line in result.stdout.splitlines():
        try:
            json.loads(line)
        except ValueError:
            failures.append(f"subagent fallback line is not JSON: {line!r}")
    if result.returncode != 0:
        failures.append("the subagent fallback must still exit 0")


def check(failures):
    check_the_client_last_render_path_uses_its_state_directory(failures)
    check_a_timeout_falls_back_to_the_last_render_file(failures)
    check_a_stale_last_render_file_is_not_used(failures)
    check_the_fallback_age_override_can_accept_an_older_render(failures)
    check_an_empty_last_render_file_falls_through(failures)
    check_the_minimal_line_carries_the_payload_basics(failures)
    check_the_minimal_line_handles_every_payload_shape(failures)
    check_no_server_file_at_all_still_prints_a_line(failures)
    check_a_malformed_server_file_still_prints_a_line(failures)
    check_the_kimi_kind_prints_exactly_one_line_on_fallback(failures)
    check_every_session_id_spelling_finds_its_last_render(failures)
    check_the_subagent_fallback_never_prints_a_claude_line(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: client fallback verified")


if __name__ == "__main__":
    main()
