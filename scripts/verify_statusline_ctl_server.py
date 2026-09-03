"""Verify statusline-ctl's `server status`, `server stop` and `server
restart` subcommands, against a real resident server bound to a fixture
state directory (never the live one).

Split out of scripts/verify_statusline_ctl.py to keep both files under the
repo's 400-line guideline; imports `ctl` (the statusline_ctl.py module,
loaded by path there), `_run` and `_ROOT` from that file rather than
reloading statusline_ctl.py a second time.

Points HOME/USERPROFILE/CLAUDE_STATE_DIR at a fresh temp directory for the
duration of each check, so nothing here ever touches the developer's real
~/.claude or a real resident server that might be running on this machine.

Run from anywhere.
"""

import contextlib
import io
import os
import shutil
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_statusline_ctl import _ROOT, _run, ctl


@contextlib.contextmanager
def _isolated_state_dir():
    """A fresh temp directory as HOME/USERPROFILE/CLAUDE_STATE_DIR for the
    duration of the block, restored on exit. Isolates both state_dir() (the
    server.json a `server` subcommand reads) and app_dir()-derived paths (a
    fixture server's own error log), so nothing in this file ever touches
    the developer's real ~/.claude, even if a real resident server happens
    to be running on this machine."""
    state_directory = tempfile.mkdtemp(prefix="statusline-ctl-state-")
    saved = {
        key: os.environ.get(key) for key in ("HOME", "USERPROFILE", "CLAUDE_STATE_DIR")
    }
    os.environ["HOME"] = state_directory
    os.environ["USERPROFILE"] = state_directory
    os.environ["CLAUDE_STATE_DIR"] = state_directory
    try:
        yield state_directory
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(state_directory, ignore_errors=True)


def _serve_and_close(server):
    """Run serve_forever, then close(), exactly the shape statusline_server.py's
    real entry point (statusline_lib.server.serve) runs -- server.json is only
    removed by close(), which serve_forever() itself never calls."""
    try:
        server.serve_forever()
    finally:
        server.close()


@contextlib.contextmanager
def _live_fixture_server(state_directory):
    """A real Server bound to `state_directory` and served on a background
    thread, for the duration of the block."""
    from statusline_lib.server import Server

    server = Server(state_directory, _ROOT)
    server.bind()
    thread = threading.Thread(target=_serve_and_close, args=(server,), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.stop_requested = True
        thread.join(timeout=5.0)
        server.close()  # idempotent; a safety net if the thread never woke


def _check_server_status_no_server(failures):
    with _isolated_state_dir():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = _run(["server", "status"])
        output = buf.getvalue()
        if code != 0:
            failures.append(f"server status with no server should exit 0; got {code}")
        if "no server running" not in output:
            failures.append(f"server status with no server should say so: {output!r}")


def _check_server_status_live_server(failures):
    with (
        _isolated_state_dir() as state_directory,
        _live_fixture_server(state_directory),
    ):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = _run(["server", "status"])
        output = buf.getvalue()
        if code != 0:
            failures.append(
                f"server status against a live server should exit 0; got {code}"
            )
        for expected in (str(os.getpid()), "port", "version", "uptime_seconds"):
            if expected not in output:
                failures.append(
                    f"server status output missing {expected!r}: {output!r}"
                )


def _check_server_status_stale_pid(failures):
    """A server.json whose pid is gone must be reported as stale, not as
    running -- and must not hang trying to reach a port nothing answers on."""
    from statusline_lib.server_info import server_info_path, write_server_info

    with _isolated_state_dir() as state_directory:
        write_server_info(
            server_info_path(state_directory),
            pid=2**31 - 1,  # the impossible-pid convention used elsewhere in
            # this suite (scripts/verify_server_info_liveness.py)
            port=1,
            version="deadbeef",
            started_at=0.0,
            platform="claude",
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = _run(["server", "status"])
        output = buf.getvalue()
        if code != 0:
            failures.append(
                f"server status for a stale entry should exit 0; got {code}"
            )
        if "stale" not in output:
            failures.append(
                f"server status for a dead pid should say stale: {output!r}"
            )


def _check_server_stop_no_server(failures):
    with _isolated_state_dir():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = _run(["server", "stop"])
        output = buf.getvalue()
        if code != 0:
            failures.append(f"server stop with no server should exit 0; got {code}")
        if "no server running" not in output:
            failures.append(f"server stop with no server should say so: {output!r}")


def _check_server_stop_live_server(failures):
    from statusline_lib.server_info import server_info_path

    with (
        _isolated_state_dir() as state_directory,
        _live_fixture_server(state_directory),
    ):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = _run(["server", "stop"])
        output = buf.getvalue()
        if code != 0:
            failures.append(f"server stop should exit 0; got {code}")
        if "stopped" not in output:
            failures.append(f"server stop should report success: {output!r}")
        if os.path.exists(server_info_path(state_directory)):
            failures.append("server stop should remove server.json")


@contextlib.contextmanager
def _patched_client_script(path):
    """Point the loaded ctl module's own client-script path at `path` for
    the duration of the block, restored after. A test must not depend on
    whether the real statusline_client.py happens to exist in this tree (it
    is owned by a separate task and, mid-development, may or may not be
    present, or may not yet implement --ensure-server) -- this is the
    deterministic seam instead."""
    real = ctl._CLIENT_SCRIPT
    ctl._CLIENT_SCRIPT = path
    try:
        yield
    finally:
        ctl._CLIENT_SCRIPT = real


def _check_server_restart_reports_a_failed_start(failures):
    """restart's second half runs `<client script> --ensure-server`; pointed
    at a path inside the isolated state directory that provably does not
    exist, it must fail cleanly (nonzero exit, an error on stderr) rather
    than raising. This also covers restart with no server running for its
    `stop` half."""
    with _isolated_state_dir() as state_directory:
        missing_script = os.path.join(state_directory, "missing-client.py")
        with _patched_client_script(missing_script):
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with (
                contextlib.redirect_stdout(buf_out),
                contextlib.redirect_stderr(buf_err),
            ):
                code = _run(["server", "restart"])
        if code == 0:
            failures.append(
                "server restart should report failure when the client script "
                f"is missing; stdout={buf_out.getvalue()!r} stderr={buf_err.getvalue()!r}"
            )
        if "error" not in buf_err.getvalue():
            failures.append(
                f"a missing client script should print an error: {buf_err.getvalue()!r}"
            )


def _check_server_restart_reports_success_with_a_working_client(failures):
    """The mirror case: a stub client script that exits 0 makes restart
    report success. Never touches the real (possibly still-incomplete)
    statusline_client.py."""
    with _isolated_state_dir() as state_directory:
        stub_script = os.path.join(state_directory, "stub-client.py")
        with open(stub_script, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.exit(0)\n")
        with _patched_client_script(stub_script):
            buf_out = io.StringIO()
            with contextlib.redirect_stdout(buf_out):
                code = _run(["server", "restart"])
        output = buf_out.getvalue()
        if code != 0:
            failures.append(
                f"server restart with a working client should exit 0; got {code}"
            )
        if "restarted" not in output:
            failures.append(f"server restart should report success: {output!r}")


def _check_server_restart_does_not_spawn_over_a_live_server(failures):
    """A stop that reports the server did not go has to end the restart
    there: spawning anyway leaves two live servers behind one server.json,
    and the one the file does not name holds a port nobody can find until its
    idle exit. wait_until_gone is stubbed, so nothing waits on a deadline."""
    with (
        _isolated_state_dir() as state_directory,
        _live_fixture_server(state_directory),
    ):
        stub = os.path.join(state_directory, "stub-client.py")
        with open(stub, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.exit(0)\n")
        saved = ctl.wait_until_gone
        ctl.wait_until_gone = lambda *arguments, **keywords: False
        buf = io.StringIO()
        try:
            with _patched_client_script(stub), contextlib.redirect_stdout(buf):
                code = _run(["server", "restart"])
        finally:
            ctl.wait_until_gone = saved
        if code == 0 or "restarted" in buf.getvalue():
            failures.append(f"restart spawned over a live server: {buf.getvalue()!r}")


def _check_server_restart_handles_a_hanging_client(failures):
    """process_safe.ProcessTimeout (the child never exits before the
    timeout) must surface as the same clean failure as a nonzero exit code,
    not propagate as an uncaught exception. Same 0.3s-timeout/sleep(30)
    shape scripts/verify_process_safe.py already uses for this."""
    with _isolated_state_dir() as state_directory:
        hanging_script = os.path.join(state_directory, "hanging-client.py")
        with open(hanging_script, "w", encoding="utf-8") as f:
            f.write("import time\ntime.sleep(30)\n")
        real_timeout = ctl._RESTART_TIMEOUT_SECONDS
        ctl._RESTART_TIMEOUT_SECONDS = 0.3
        try:
            with _patched_client_script(hanging_script):
                buf_out, buf_err = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(buf_out),
                    contextlib.redirect_stderr(buf_err),
                ):
                    code = _run(["server", "restart"])
        finally:
            ctl._RESTART_TIMEOUT_SECONDS = real_timeout
        if code != 1:
            failures.append(f"a hanging client should make restart exit 1; got {code}")
        if "error" not in buf_err.getvalue():
            failures.append(
                f"a hanging client should print a clean error, not raise: {buf_err.getvalue()!r}"
            )


def _check_server_unknown_subcommand(failures):
    with _isolated_state_dir():
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = _run(["server", "frobnicate"])
        output = buf.getvalue()
        if code != 2:
            failures.append(f"an unknown server subcommand should exit 2; got {code}")
        if "usage" not in output:
            failures.append(
                f"an unknown server subcommand should print usage: {output!r}"
            )


def _check_server_control_helpers(failures):
    """Direct coverage of the request/poll helpers in
    statusline_lib.server_control for the branches a live round trip
    doesn't reach: a reply that never arrives, and a reply that isn't
    valid JSON."""
    from statusline_lib import server_control

    with _isolated_state_dir():
        # No listener on this port at all -- _send_and_receive's OSError/
        # timeout branch, surfacing as request_status returning None. Bind
        # and immediately release a real port so nothing else is listening
        # on it for the rest of this check.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        if server_control.request_status(free_port, timeout=0.2) is not None:
            failures.append("request_status against a dead port should return None")

        # A reply that arrives but is not valid JSON -- request_status's
        # json.loads ValueError branch.
        real_send_and_receive = server_control._send_and_receive
        server_control._send_and_receive = lambda port, request, timeout: "not json"
        try:
            if server_control.request_status(0, timeout=0.2) is not None:
                failures.append(
                    "request_status with a non-JSON reply should return None"
                )
        finally:
            server_control._send_and_receive = real_send_and_receive

        # wait_until_gone: a file that is already absent returns True with no
        # polling; a file that never disappears returns False once its
        # timeout elapses (both bounded well under a second; neither asserts
        # on how long it took).
        missing_path = os.path.join(state_dir_for_test(), "never-existed")
        if (
            server_control.wait_until_gone(
                missing_path, timeout=1.0, poll_interval=0.05
            )
            is not True
        ):
            failures.append("wait_until_gone on a missing file should return True")
        fd, present_path = tempfile.mkstemp(prefix="statusline-ctl-present-")
        os.close(fd)
        try:
            if (
                server_control.wait_until_gone(
                    present_path, timeout=0.15, poll_interval=0.05
                )
                is not False
            ):
                failures.append(
                    "wait_until_gone should return False if the file never disappears"
                )
        finally:
            os.remove(present_path)


def state_dir_for_test():
    """The isolated state directory the current _isolated_state_dir() block
    set up, read back via the same env var the library resolves it from."""
    return os.environ["CLAUDE_STATE_DIR"]


def check(failures):
    _check_server_status_no_server(failures)
    _check_server_status_live_server(failures)
    _check_server_status_stale_pid(failures)
    _check_server_stop_no_server(failures)
    _check_server_stop_live_server(failures)
    _check_server_restart_reports_a_failed_start(failures)
    _check_server_restart_reports_success_with_a_working_client(failures)
    _check_server_restart_handles_a_hanging_client(failures)
    _check_server_restart_does_not_spawn_over_a_live_server(failures)
    _check_server_unknown_subcommand(failures)
    _check_server_control_helpers(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: statusline-ctl server status/stop/restart verified")


if __name__ == "__main__":
    main()
