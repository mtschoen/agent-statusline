"""The 2026-09-02 incident regression.

Fifty concurrent renders across four working directories, with one refresher
deliberately blocked, must leave exactly one server process and at most four
worker threads, and every render must either get a reply or print its
fallback. Each of the three is a ceiling the 2026-09-02 incident had none
of: process creation was per render and per stale cache entry, so the count
climbed for as long as the renders kept arriving.

The slow refresher blocks on an Event this script controls, never on a sleep,
and nothing here asserts on elapsed time. Every wait is bounded by a ceiling
that only elapses when the thing waited for is never coming, and then the
check fails instead of hanging the suite.

Nothing here may leave a server behind either, which is the same property the
checks assert. The fixtures come from the server and client suites next door
rather than from a synthetic home of this file's own: importing them installs
the temporary HOME and CLAUDE_STATE_DIR the whole family is isolated by, and a
client subprocess inherits exactly that environment. Every server is built in
this process, so the only real process this script starts is a client, and a
client's own spawn path is suppressed by holding the production single-flight
lock rather than by stubbing anything out.

Run from anywhere; imports from agent-statusline by path.
"""

import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
import threading

# The scripts directory, so the suites next door are importable. That import
# is what installs the isolated HOME and CLAUDE_STATE_DIR, so it has to happen
# before any statusline_lib module resolves an app_dir()-based path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _server_concurrency_helpers import (
    count_repository_python_processes,
    count_server_processes,
    expected_fallbacks,
    payload,
    run_clients_in_parallel,
    running_server,
    start_client_under_parent,
    wedged,
)
from verify_client_fallback import _silent_server
from verify_client_spawn import _POLL_CEILING_SECONDS, _poll_until
from verify_server_requests import _REPO, _STATE_DIR, _claude_payload

# Only now the repository root and the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib import server_jobs
from statusline_lib.server_info import pid_is_alive

__all__ = ["_STATE_DIR"]

# The burst. Fifty is well past the six sessions plus CI that produced the
# incident, and four directories is what makes the git-ref refresher a set of
# distinct jobs rather than one the pool deduplicates down to a single entry.
_RENDER_COUNT = 50
_CWD_COUNT = 4

# The ceiling, written out rather than imported from WORKER_POOL_SIZE on
# purpose. The pool below is built at the production size, so this literal is
# what makes raising that constant a failing test rather than a moved
# goalpost: four is the number the incident says cannot be exceeded.
_WORKER_CAP = 4

# The kind every client in the burst renders.
_CLIENT_KIND = "claude"

# Ceilings, not measurements: a healthy pool wedges in milliseconds and a
# healthy client answers in well under a second, so these only elapse when the
# thing waited for is never coming.
_WEDGE_TIMEOUT_SECONDS = 30.0
_BLOCKED_JOB_TIMEOUT_SECONDS = 30.0

# The entry point a spawned server runs, as it appears in a command line.
_SERVER_ENTRY_POINT = "statusline_server.py"

# What this run says when it is done. Without psutil neither process counter
# sees anything, so the orphan delta is 0 > 0 and the duplicate-server count is
# this process alone: both are structurally incapable of failing, and the run
# states that gap rather than letting a bare OK line imply the incident's
# headline symptom was checked.
_OK_LINE = "OK: fifty concurrent renders leave one server and four workers"
_SKIPPED_SUFFIX = " (process assertions skipped, psutil missing)"
_SKIP_LINE = (
    "SKIPPED: orphan and duplicate-server assertions need psutil (not installed)"
)

# The psutil import, as a module-level seam: scripts/verify_server_concurrency_
# degradation.py assigns a stand-in, so both the present and the absent arm are
# covered on every machine whatever is installed. _UNSET rather than None,
# because None is the absent arm itself.
_UNSET = object()
_psutil_override = _UNSET


def _psutil():
    """The psutil module, or None when it is not installed. Every process
    count below degrades to a no-op without it, so a machine or a CI runner
    that lacks it still runs the rest of the scenario, and _report says so."""
    if _psutil_override is not _UNSET:
        return _psutil_override
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def _count_server_processes(context):
    """How many distinct live servers this checkout has, by pid."""
    return count_server_processes(context, _psutil(), _SERVER_ENTRY_POINT, _REPO)


def _count_repository_python_processes():
    """Every live Python interpreter whose command line names this checkout,
    or 0 when psutil is absent."""
    return count_repository_python_processes(_psutil(), _REPO)


def _running_server(failures, runner=None):
    return running_server(failures, runner=runner)


def _payload(context, cwd_index, session_index=None):
    return payload(context, cwd_index, session_index)


def _run_clients_in_parallel(context, count, cwd_count):
    return run_clients_in_parallel(context, count, cwd_count, _CLIENT_KIND)


def _expected_fallbacks(context, count, cwd_count):
    return expected_fallbacks(context, count, cwd_count)


def _wedged(entered, count):
    return wedged(entered, count, _WEDGE_TIMEOUT_SECONDS)


def check_fifty_concurrent_renders_leave_one_server(failures):
    """The incident itself. Every git-ref refresher wedges, which is the
    stale-cache read that drove process creation during the incident, and the
    burst still has to end with one server, four threads, and fifty lines."""
    release = threading.Event()
    entered = threading.Semaphore(0)

    def slow_runner(kind, argument):
        if kind == "git-ref":
            entered.release()
            release.wait(timeout=_BLOCKED_JOB_TIMEOUT_SECONDS)
            return
        server_jobs.run_refresh(kind, argument)

    with _running_server(failures, runner=slow_runner) as context:
        results = _run_clients_in_parallel(context, _RENDER_COUNT, _CWD_COUNT)
        is_wedged = _wedged(entered, _CWD_COUNT)
        alive = _count_server_processes(context)
        peak = context.pool.peak_in_flight()
        workers = context.pool.worker_count()
        submissions = context.pool.submissions()
        cwds = list(context.cwds)
        release.set()

    if not is_wedged:
        failures.append(
            f"fewer than {_CWD_COUNT} refreshers ever blocked;"
            " the wedged-pool scenario did not actually run"
        )
    if alive != 1:
        failures.append(f"{alive} servers alive, expected exactly 1")
    if peak > _WORKER_CAP:
        failures.append(f"{peak} jobs ran at once, the cap is {_WORKER_CAP}")
    if workers != _WORKER_CAP:
        failures.append(f"pool has {workers} threads, expected {_WORKER_CAP}")

    git_ref_submissions = [s for s in submissions if s[0] == "git-ref"]
    expected_keys = {str(cwd) for cwd in cwds}
    observed_keys = {arg for _, arg, _ in git_ref_submissions}
    total_accepted = sum(1 for _, _, accepted in git_ref_submissions if accepted)
    total_refused = sum(1 for _, _, accepted in git_ref_submissions if not accepted)
    accepted_by_key = {}
    refused_by_key = {}
    for _, arg, accepted in git_ref_submissions:
        if accepted:
            accepted_by_key[arg] = accepted_by_key.get(arg, 0) + 1
        else:
            refused_by_key[arg] = refused_by_key.get(arg, 0) + 1

    if observed_keys != expected_keys:
        failures.append(
            f"git-ref keys {observed_keys} do not match expected {expected_keys}"
        )
    bad_accepted = {k: v for k, v in accepted_by_key.items() if v != 1}
    unrefused_keys = expected_keys - set(refused_by_key.keys())
    if (
        bad_accepted
        or total_accepted != _CWD_COUNT
        or unrefused_keys
        or total_refused == 0
    ):
        failures.append(
            f"deduplication mismatch: accepted {total_accepted} (expected {_CWD_COUNT}), "
            f"refused {total_refused} (expected duplicate refusals for all keys), "
            f"per-key accepted {accepted_by_key}, per-key refused {refused_by_key}"
        )

    fallbacks = _expected_fallbacks(context, _RENDER_COUNT, _CWD_COUNT)
    server_replies = 0
    for index, (result, fallback) in enumerate(zip(results, fallbacks, strict=True)):
        output = result.stdout.strip()
        session_badge = f"concurrency-{index:04d}"[:8]
        if output == fallback:
            failures.append(
                f"render {index} fell back during the burst instead of receiving a server reply"
            )
            continue
        if session_badge in output and "opus" in output:
            server_replies += 1
        else:
            failures.append(f"render {index} produced unexpected output: {output!r}")
    if server_replies == 0:
        failures.append("no renders received a server reply during the burst")
    blank = [result for result in results if not result.stdout.strip()]
    nonzero = [result for result in results if result.returncode != 0]
    if blank:
        failures.append(f"{len(blank)} of {_RENDER_COUNT} renders printed nothing")
    if nonzero:
        failures.append(f"{len(nonzero)} of {_RENDER_COUNT} renders exited non-zero")


def check_a_blocked_refresher_never_blocks_a_reply(failures):
    """The receive loop must not be waiting on the pool. With every worker
    blocked, a render still replies from the caches it already has."""
    release = threading.Event()
    entered = threading.Semaphore(0)

    def blocked_runner(kind, argument):
        del kind, argument
        entered.release()
        release.wait(timeout=_BLOCKED_JOB_TIMEOUT_SECONDS)

    with _running_server(failures, runner=blocked_runner) as context:
        for index in range(_CWD_COUNT):
            context.server.handle_request(
                {"kind": _CLIENT_KIND, "payload": _payload(context, index)}
            )
        is_wedged = _wedged(entered, _WORKER_CAP)
        reply = context.server.handle_request(
            {"kind": _CLIENT_KIND, "payload": _payload(context, 0)}
        )
        release.set()

    if not is_wedged:
        failures.append(
            f"fewer than {_WORKER_CAP} workers ever blocked;"
            " the reply below was not served against a saturated pool"
        )
    if not reply:
        failures.append("a render must reply while every worker is blocked")


def check_no_orphan_processes_remain(failures):
    """The design that failed spawned a process per stale cache read. Count
    Python processes whose command line names this repository before and
    after the burst; the delta must be zero once the server has exited.
    Also assert that the worker pool has no surviving worker threads."""
    before = _count_repository_python_processes()
    with _running_server(failures) as context:
        _run_clients_in_parallel(context, _RENDER_COUNT, _CWD_COUNT)
    after = _count_repository_python_processes()
    if after > before:
        failures.append(
            f"{after - before} python processes outlived the burst;"
            " the render path must leave nothing behind"
        )
    workers = context.pool.worker_count()
    if workers != 0:
        failures.append(
            f"pool has {workers} live workers after server close, expected 0"
        )


def check_a_client_exits_after_its_wrapper_is_killed(failures):
    """The original incident: a harness killed a render's shell wrapper while
    the child interpreter survived as an orphan. When the wrapper parent dies,
    the client must still exit (via its socket timeout) and not outlive it."""
    with _silent_server() as context:
        temp_dir = tempfile.mkdtemp(prefix="verify-killed-parent-")
        parent = None
        child_pid = None
        try:
            payload_file = os.path.join(temp_dir, "payload.json")
            with open(payload_file, "w", encoding="utf-8") as f:
                json.dump(_claude_payload(), f)
            pid_file = os.path.join(temp_dir, "client.pid")
            client_command = [
                sys.executable,
                os.path.join(_REPO, "statusline_client.py"),
                "--kind",
                _CLIENT_KIND,
            ]
            parent = start_client_under_parent(
                client_command, payload_file, context.environment, pid_file
            )
            if not _poll_until(
                lambda: os.path.exists(pid_file) and os.path.getsize(pid_file) > 0
            ):
                failures.append("wrapper parent did not write child pid")
                return
            with open(pid_file, encoding="utf-8") as f:
                child_pid = int(f.read().strip())
            if pid_is_alive(child_pid) is not True:
                failures.append(f"child client (pid {child_pid}) never started")
                return
            parent.kill()
            with contextlib.suppress(Exception):
                parent.wait(timeout=_POLL_CEILING_SECONDS)
            if not _poll_until(lambda: pid_is_alive(child_pid) is False):
                failures.append(
                    f"client interpreter (pid {child_pid}) survived "
                    "after its wrapper parent was killed"
                )
        finally:
            if parent is not None and parent.poll() is None:
                with contextlib.suppress(OSError):
                    parent.kill()
                with contextlib.suppress(Exception):
                    parent.wait(timeout=_POLL_CEILING_SECONDS)
            if child_pid is not None and pid_is_alive(child_pid):
                sig = signal.SIGTERM if os.name == "nt" else signal.SIGKILL
                with contextlib.suppress(OSError):
                    os.kill(child_pid, sig)
            shutil.rmtree(temp_dir, ignore_errors=True)


def check(failures):
    check_fifty_concurrent_renders_leave_one_server(failures)
    check_a_blocked_refresher_never_blocks_a_reply(failures)
    check_no_orphan_processes_remain(failures)
    check_a_client_exits_after_its_wrapper_is_killed(failures)


def _report(failures):
    """Print this run's verdict and return its exit code. Split out of main so
    the sibling degradation script can drive both psutil arms of it."""
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    if _psutil() is None:
        print(_SKIP_LINE)
        print(f"{_OK_LINE}{_SKIPPED_SUFFIX}")
    else:
        print(_OK_LINE)
    return 0


def main():
    failures = []
    check(failures)
    sys.exit(_report(failures))


if __name__ == "__main__":
    main()
