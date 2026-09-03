"""Verify what the statusline client does with its liveness decision: which
failures cost the server its life, which one is left alone, and that
--ensure-server really leaves a running server behind.

The spawn machinery underneath (the version digest, classify_failure, the
single-flight lock) is scripts/verify_client_spawn.py, whose fixtures this
file imports rather than rebuilding. Split that way because the repository
holds every file at or under 400 lines.

Every check but the last drives main() in this process with the spawn
replaced, so a decision is observed without a process being started. The last
one starts a real server, because a seam cannot prove the command actually
runs, and stops it again in its finally.

Run from anywhere; imports from agent-statusline by path.
"""

import contextlib
import io
import json
import os
import socket
import sys

# The scripts directory, so the suites next door are importable. That import
# is what installs the isolated HOME and CLAUDE_STATE_DIR, so it has to happen
# before any statusline_lib module resolves an app_dir()-based path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_client_fallback import (
    _no_server,
    _release_spawn_lock,
    _silent_server,
)
from verify_client_spawn import (
    _POLL_CEILING_SECONDS,
    _STALE_VERSION,
    _dead_server,
    _poll_until,
    _write_server_info,
    _write_spawn_lock,
)
from verify_server_protocol import (
    _run_client,
    _run_client_arguments,
    _running_server,
)
from verify_server_requests import _ENCODING, _claude_payload

# Only now the repository root and the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_client
from statusline_lib.server_info import pid_is_alive, read_server_info, server_info_path


def _drive_main(kind, payload):
    """main() in this process with stdin and the spawn replaced, returning
    what it printed and the reasons it would have spawned for. Both are module
    attributes main resolves at call time, so nothing about the decision under
    test is stubbed out."""
    recorded = []

    def fake_payload():
        return payload

    def fake_ensure(info, reason):
        del info
        recorded.append(reason)
        return True

    saved_payload = statusline_client._payload_from_stdin
    saved_ensure = statusline_client.ensure_server
    saved_stdout = sys.stdout
    statusline_client._payload_from_stdin = fake_payload
    statusline_client.ensure_server = fake_ensure
    sys.stdout = io.StringIO()
    try:
        statusline_client.main(kind, [])
        return sys.stdout.getvalue(), recorded
    finally:
        sys.stdout = saved_stdout
        statusline_client._payload_from_stdin = saved_payload
        statusline_client.ensure_server = saved_ensure


def check_a_version_mismatch_shuts_the_old_server_down(failures):
    """A server running code the checkout has moved past is asked to stop, so
    the next render is served by one built from the current tree. The spawn
    itself is suppressed with a fresh lock, so this check leaves no process
    behind while still proving the shutdown datagram went out."""
    with _running_server(failures) as context:
        _write_server_info(context, port=context.port, version=_STALE_VERSION)
        _write_spawn_lock(pid=os.getpid(), age_seconds=0)
        try:
            result = _run_client(context, "claude", _claude_payload())
            if not result.stdout.strip():
                failures.append("a stale-version server must still print a fallback")
            if not _poll_until(lambda: context.server.stop_requested):
                failures.append("a stale-version server must be told to shut down")
        finally:
            _release_spawn_lock()


def check_the_render_path_spawns_only_when_something_is_wrong(failures):
    """Every shape end to end through main: what it printed, and whether it
    asked for a replacement. A live server is served and left alone; the
    stale-version, dead and missing shapes are all replaced."""
    payload = _claude_payload()
    with _running_server(failures) as context:
        printed, reasons = _drive_main("claude", payload)
        if not printed.strip():
            failures.append("a live server must produce a rendered line")
        if reasons:
            failures.append(f"a live server must not be replaced, got {reasons}")
        _write_server_info(context, port=context.port, version=_STALE_VERSION)
        printed, reasons = _drive_main("claude", payload)
        if not printed.strip():
            failures.append("a stale-version server must still print a fallback")
        if reasons != ["version"]:
            failures.append(f"a stale-version server must be replaced, got {reasons}")
    with _dead_server():
        printed, reasons = _drive_main("claude", payload)
        if not printed.strip():
            failures.append("a dead server must still print a fallback")
        if reasons != ["dead"]:
            failures.append(f"a dead server must be replaced, got {reasons}")
    with _no_server():
        printed, reasons = _drive_main("claude", payload)
        if not printed.strip():
            failures.append("a missing server must still print a fallback")
        if reasons != ["missing"]:
            failures.append(f"a missing server must be replaced, got {reasons}")


def check_a_silent_server_is_never_replaced(failures):
    """The one failure that is not worth a spawn: a bound port that never
    answers is a server that is busy, and a second one would make it worse."""
    with _silent_server():
        printed, reasons = _drive_main("claude", _claude_payload())
    if not printed.strip():
        failures.append("a silent server must still print a fallback")
    if reasons:
        failures.append(f"a silent server must not be replaced, got {reasons}")


def _stop_spawned_server(context, failures):
    """Shut down whatever server is named in server.json and wait for its
    process to go. Bounded polling on a condition, so a server that will not
    die fails the check instead of being left running."""
    info = read_server_info(server_info_path(context.state_directory))
    if info is None:
        return
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        control.settimeout(_POLL_CEILING_SECONDS)
        with contextlib.suppress(OSError):
            control.sendto(
                json.dumps({"kind": "shutdown"}).encode(_ENCODING),
                ("127.0.0.1", info["port"]),
            )
    if not _poll_until(lambda: pid_is_alive(info["pid"]) is False):
        failures.append(f"the spawned server (pid {info['pid']}) did not stop")


def check_ensure_server_reads_no_stdin_and_prints_nothing(failures):
    """The one real spawn in this family: --ensure-server against no server at
    all must leave a live one behind and print nothing. Stopped in the finally,
    so no process outlives the check."""
    with _no_server() as context:
        # The fixture holds a spawn lock so no other case starts a server;
        # this is the one case that wants a real one, so it drops the lock.
        _release_spawn_lock()
        try:
            result = _run_client_arguments(
                ["--ensure-server"], environment=context.environment
            )
            if result.stdout.strip():
                failures.append(f"--ensure-server printed {result.stdout!r}")
            if result.returncode != 0:
                failures.append(f"--ensure-server exited {result.returncode}")
            path = server_info_path(context.state_directory)
            if not _poll_until(lambda: read_server_info(path) is not None):
                failures.append("--ensure-server must start a server when none is live")
        finally:
            _stop_spawned_server(context, failures)
            _release_spawn_lock()


def check(failures):
    check_a_version_mismatch_shuts_the_old_server_down(failures)
    check_the_render_path_spawns_only_when_something_is_wrong(failures)
    check_a_silent_server_is_never_replaced(failures)
    check_ensure_server_reads_no_stdin_and_prints_nothing(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: client liveness decision and --ensure-server verified")


if __name__ == "__main__":
    main()
