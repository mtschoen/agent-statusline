"""The render-budget invariant, measured end to end against a live server.

Four production incidents shared one disease: a synchronous call inside a
render that can block for seconds. In the resident-server model the render
path is statusline_client.py, plus the statusline_client_support.py it reaches
through a lazy import, and nothing else, so this file drives the real client
as a subprocess against a real server and holds the round trip to a budget.

BENCHMARK. This is the one script in the suite that reads the wall clock, and
the tolerances are deliberately loose: the budget is roughly three times the
measured figure on this machine, every historical incident was 5,000ms or
worse, and the verdict is the best of three medians of nine runs, so a loaded
CI runner costs an attempt rather than the check. Every wait on a real socket
or a real process is bounded, and the server is shut down in a finally.

The static half of the invariant (bounded subprocesses over statusline_lib,
the import-free client, the one-datagram exchange) lives in
verify_render_budget_static.py and is imported and run from here too, so
either file alone is a complete verdict. The split is for the 400-line file
gate, not a change of scope.

Run from anywhere; imports from agent-statusline by path.
"""

import contextlib
import os
import statistics
import subprocess
import sys
import time

# The scripts directory first: the static half of this check lives next door,
# and importing the server suite is what installs the temporary HOME and
# CLAUDE_STATE_DIR the live checks run under. A client subprocess inherits
# exactly that environment, so it looks for server.json where the server just
# wrote it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_render_budget_static import _TEXT_ENCODING, check_static_guards
from verify_server_protocol import _client_environment, _Context, _run_client
from verify_server_requests import _HOME, _REPO, _STATE_DIR, _claude_payload

# Only now the repository root, and only now statusline_lib: importing the
# suite above is what installed the isolated home, and several statusline_lib
# modules resolve app_dir()-based paths at import time.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The client's own fallback builder, so the expected fallback line is the one
# the client would actually print rather than a second guess at its format.
import statusline_client_support
from statusline_lib.server_control import (
    request_shutdown,
    request_status,
    wait_until_gone,
)
from statusline_lib.server_info import read_server_info, server_info_path

_SERVER = os.path.join(_REPO, "statusline_server.py")

# The client's entire budget. Its own socket timeout is 150ms, the interpreter
# and import floor is roughly 65ms on this machine, and Kimi kills the process
# tree at 300ms. 200ms leaves the client no room to grow a second file read.
_CLIENT_BUDGET_MS = float(os.environ.get("STATUSLINE_TEST_CLIENT_BUDGET_MS", "200"))

# Nine runs per attempt, so the median is a median rather than a coin flip, and
# the best of three attempts for the same reason the warm-core check took the
# best of three before it: a real regression misses the budget on every
# attempt, a scheduler blip on a shared runner spoils one. The loop stops at
# the first attempt inside budget, so the healthy case measures nine runs once.
_CLIENT_RUNS = 9
_MEDIAN_ATTEMPTS = 3

# Ceilings rather than measurements: a healthy server publishes server.json in
# well under a second and goes away within one receive-loop poll of its
# shutdown datagram, so these only elapse when something is already broken.
_SERVER_READY_SECONDS = 20.0
_SERVER_POLL_SECONDS = 0.05
_SERVER_EXIT_SECONDS = 10.0
_STATUS_TIMEOUT_SECONDS = 2.0

# How fresh the client will accept its recorded last render as, in seconds,
# while these runs are measured. Any real recorded file is at least one client
# timeout old by the time a fallback reads it, so a thousandth of a second
# rejects every one of them and leaves the minimal line as the only fallback
# the client can produce. See _median_client_milliseconds for why that matters.
_FALLBACK_AGE_PREFERENCE = "STATUSLINE_FALLBACK_MAXIMUM_AGE_SECONDS"
_UNUSABLE_FALLBACK_AGE_SECONDS = "0.001"

# RFC 5737 TEST-NET-1: guaranteed non-routable, and it drops rather than
# refuses, so a fetch aimed at it hangs until its own timeout instead of
# failing fast. That is the shape of the 2026-07-11 incident.
_UNREACHABLE_QUOTA_HOST = "192.0.2.1:8001"


def _log_tail(path, characters=400):
    """The end of the server's captured output, for a failure message."""
    try:
        with open(path, encoding=_TEXT_ENCODING, errors="replace") as f:
            return f.read()[-characters:]
    except OSError:
        return ""


def _wait_for_server(information_path, process):
    """Poll for the server's info file, bounded. None means it never appeared,
    or the process died first, which the caller reports rather than waiting on
    a server that is not coming."""
    deadline = time.monotonic() + _SERVER_READY_SECONDS
    while time.monotonic() < deadline:
        information = read_server_info(information_path)
        if information is not None and isinstance(information.get("port"), int):
            return information
        if process.poll() is not None:
            return None
        time.sleep(_SERVER_POLL_SECONDS)
    return None


def _shutdown_server(failures, process, information_path, information):
    """Leave no server behind: a shutdown datagram, then a bounded wait, then
    a kill. A client that had to fall back single-flight spawns a replacement
    of its own, so anything holding server.json afterwards is stopped too."""
    if information is not None:
        request_shutdown(information["port"])
        wait_until_gone(information_path, timeout=_SERVER_EXIT_SECONDS)
    try:
        process.wait(timeout=_SERVER_EXIT_SECONDS)
    except subprocess.TimeoutExpired:
        failures.append("the server ignored its shutdown and had to be killed")
        process.kill()
        process.wait(timeout=_SERVER_EXIT_SECONDS)
    stray = read_server_info(information_path)
    if stray is None:
        return
    request_shutdown(stray["port"])
    wait_until_gone(information_path, timeout=_SERVER_EXIT_SECONDS)
    failures.append(
        f"server.json still named a server (pid {stray.get('pid')}) after"
        " shutdown; a client fallback spawned a replacement"
    )


@contextlib.contextmanager
def _live_server(failures, **environment_overrides):
    """A real statusline_server.py subprocess on a random port, against this
    suite's isolated home, shut down over the wire on the way out. Yields its
    info file contents, or None when it never came up.

    A subprocess rather than verify_server_protocol's in-process fixture on
    purpose: that fixture hands the server a pool that records jobs instead of
    running them, and the whole point of the unreachable-host check is that a
    real pool thread stuck on a dead address still cannot reach the loop that
    answers a client.
    """
    information_path = server_info_path(_STATE_DIR)
    with contextlib.suppress(OSError):
        os.remove(information_path)
    log_path = os.path.join(_HOME, "live-server.log")
    # The child gets its own duplicate of the handle, so closing this one at
    # the end of the with block leaves the server's output going to the file.
    with open(log_path, "w", encoding=_TEXT_ENCODING) as log:
        process = subprocess.Popen(
            [sys.executable, _SERVER],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=_client_environment(**environment_overrides),
        )
    information = None
    try:
        information = _wait_for_server(information_path, process)
        if information is None:
            failures.append(
                f"the server never published server.json: {_log_tail(log_path)!r}"
            )
        yield information
    finally:
        _shutdown_server(failures, process, information_path, information)


def _measure_one_attempt(information, environment, payload, fallback):
    """One attempt: _CLIENT_RUNS real client subprocesses against the server
    already running. Returns (median milliseconds, None) when every run printed
    a server render, or (None, why) when the attempt has to be thrown away.

    Every run is checked, rather than the outcome being inferred at the end. A
    client that gives up on a slow server prints its fallback line quickly and
    exits 0, so without this the measurement would be of the fallback path and
    would pass for a fast render.

    Telling the two apart needs help, because by design they are the same
    text. The server writes its last-render file *before* it replies, so a
    server that renders and then answers a moment too late has already left
    this session a fallback that is character for character the reply the
    client did not wait for. _UNUSABLE_FALLBACK_AGE_SECONDS breaks the tie: the
    client's freshness window is set below any age a recorded file can have by
    the time a timeout expires, so the recorded tier is always rejected and the
    only line the client can fall back to is minimal_line(payload), which the
    caller computes from the same payload. A run whose output equals that line
    reached no server. The happy path is untouched, since a client holding a
    real reply never consults a fallback at all.

    _Context carries the isolated environment a client subprocess inherits;
    _run_client reads nothing else off it, so the override rides on there.
    """
    context = _Context(server=None, port=information["port"])
    context.environment = environment
    durations = []
    for run in range(1, _CLIENT_RUNS + 1):
        started_at = time.perf_counter()
        result = _run_client(context, "claude", payload)
        durations.append((time.perf_counter() - started_at) * 1000.0)
        printed = result.stdout.strip()
        if result.returncode != 0:
            return None, f"run {run} exited {result.returncode}: {result.stderr!r}"
        if not printed:
            return None, f"run {run} printed nothing"
        if printed == fallback:
            return None, (
                f"run {run} of {_CLIENT_RUNS} printed the client's fallback line"
                f" ({printed!r}) rather than a server render"
            )
    return statistics.median(durations), None


def _measure_against_a_live_server(failures, label, server_environment):
    """Start one server, take the best of _MEDIAN_ATTEMPTS medians against it,
    and hold that figure to the client budget.

    An attempt containing a run that fell back is thrown away rather than
    failing the check outright, which is the same tolerance the timing verdict
    already has and for the same reason: a one-off stall on a busy machine
    costs an attempt, while a server chronically too slow to answer inside the
    client's window spoils every attempt and fails here. Measured on this
    machine, 144 consecutive runs produced no fallback at all.

    The status query below is a second, independent look at the same question
    from the server's side: a server that folded no session served none of
    these renders.
    """
    with _live_server(failures, **server_environment) as information:
        if information is None:
            return
        environment = _client_environment(
            **{_FALLBACK_AGE_PREFERENCE: _UNUSABLE_FALLBACK_AGE_SECONDS}
        )
        payload = _claude_payload()
        fallback = statusline_client_support.minimal_line(payload).strip()
        best = None
        spoiled = None
        for _ in range(_MEDIAN_ATTEMPTS):
            median, spoiled = _measure_one_attempt(
                information, environment, payload, fallback
            )
            if median is None:
                continue
            best = median if best is None else min(best, median)
            if best <= _CLIENT_BUDGET_MS:
                break
        if best is None:
            failures.append(
                f"{label}: not one of {_MEDIAN_ATTEMPTS} attempts measured a"
                f" round trip; the last was thrown away because {spoiled}"
            )
            return
        status = request_status(information["port"], timeout=_STATUS_TIMEOUT_SECONDS)
        if status is None or not status.get("sessions"):
            failures.append(
                f"{label}: the server folded no session, so every client fell"
                " back and the measured figure means nothing"
            )
        print(f"{label}: median {best:.0f}ms (budget {_CLIENT_BUDGET_MS:.0f}ms)")
        if best > _CLIENT_BUDGET_MS:
            failures.append(
                f"{label}: median client run {best:.0f}ms (best of"
                f" {_MEDIAN_ATTEMPTS} attempts) exceeds {_CLIENT_BUDGET_MS:.0f}ms"
                " -- blocking work crept back onto the render path"
            )


def check_live_server_render_budget(failures):
    """End to end against a live server: the median of nine client runs must
    beat the client budget.

    This is the one benchmark in the suite that reads the wall clock, and it
    is deliberately loose: the measured figure on this machine is roughly
    40ms, every historical incident was 5,000ms or worse, and the median of
    nine plus the best of three attempts absorbs a loaded CI runner.
    """
    _measure_against_a_live_server(failures, "live render", {})


def check_unreachable_quota_host_render_budget(failures):
    """The same budget with the quota dashboard host pointed at an address
    that drops rather than refuses.

    This is an end-to-end measurement of one client round trip, and
    it is the scenario the architecture is meant to make structurally
    impossible: the HTTP fetch is a worker-pool job now, so a pool thread
    stuck on a dead address cannot touch the reply at all.
    """
    _measure_against_a_live_server(
        failures,
        "unreachable quota host",
        {"STATUSLINE_FABLE_QUOTA_HOST": _UNREACHABLE_QUOTA_HOST},
    )


def main():
    failures = []
    check_static_guards(failures)
    check_live_server_render_budget(failures)
    check_unreachable_quota_host_render_budget(failures)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: render path is free of unbounded sync calls and inside budget")


if __name__ == "__main__":
    main()
