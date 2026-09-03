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

import atexit
import contextlib
import os
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

# The scripts directory, so the suites next door are importable. That import
# is what installs the isolated HOME and CLAUDE_STATE_DIR, so it has to happen
# before any statusline_lib module resolves an app_dir()-based path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_client_fallback import _hold_spawn_lock, _release_spawn_lock
from verify_server_loop import _serving, _stop
from verify_server_protocol import _client_environment, _run_client
from verify_server_requests import _REPO, _STATE_DIR, _claude_payload
from verify_server_socket import _socket_server

# Only now the repository root and the package: the pool whose ceiling is the
# whole point, and the reader side of server.json.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib import server_jobs
from statusline_lib.server_info import read_server_info, server_info_path
from statusline_lib.server_jobs import WORKER_POOL_SIZE, WorkerPool

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

# The prefs key the client's spawn-lock staleness is retuned through, held far
# past the burst so the lock this script writes can never age out mid-run and
# let a client start a real second server.
_SPAWN_LOCK_PREFERENCE = "STATUSLINE_SPAWN_LOCK_STALE_SECONDS"
_HELD_SPAWN_LOCK_SECONDS = "3600"

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

# Four working directories for the burst. Real directories rather than the
# repository itself, so the git-ref refresher has four distinct arguments and
# no check depends on what a git checkout happens to hold.
_CWD_ROOT = tempfile.mkdtemp(prefix="verify-server-concurrency-")
atexit.register(shutil.rmtree, _CWD_ROOT, ignore_errors=True)
_CWDS = []
for _index in range(_CWD_COUNT):
    _directory = os.path.join(_CWD_ROOT, f"cwd-{_index}")
    os.makedirs(_directory, exist_ok=True)
    _CWDS.append(_directory)


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


def _command_lines():
    """(pid, command line) for every live Python interpreter psutil can see,
    or None when psutil is not importable. One scan feeds both counters below.

    Interpreters only, which is the whole reliability of both. A shell or a
    build tool can carry this repository's path and even the server's file
    name in its own command line (an agent harness running these very checks
    does), and counting those would make the assertions depend on what invoked
    them rather than on what the render path left behind.
    """
    psutil_module = _psutil()
    if psutil_module is None:
        return None
    lines = []
    with contextlib.suppress(Exception):
        for process in psutil_module.process_iter(
            ["pid", "name", "cmdline"], ad_value=None
        ):
            name = os.path.basename(process.info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            arguments = process.info.get("cmdline") or []
            lines.append((process.info.get("pid"), " ".join(arguments)))
    return lines


def _pid_is_alive(pid):
    """Whether `pid` names a live process. True when psutil is absent, so the
    server.json half of the count still contributes without it."""
    psutil_module = _psutil()
    if psutil_module is None or not isinstance(pid, int):
        return True
    return psutil_module.pid_exists(pid)


def _names_this_repository(line):
    """Whether a command line names this checkout. Scoped to _REPO on purpose:
    another checkout's server on the same machine is not this test's subject,
    and counting it would make the assertion depend on what else is running."""
    return os.path.normcase(_REPO) in os.path.normcase(line)


def _count_server_processes(context):
    """How many distinct live servers this checkout has, by pid.

    Three sources, deduplicated: this process, which hosts the server under
    test; the pid server.json records, when it is still alive; and every real
    statusline_server.py process psutil can see out of this checkout. All
    three, because any one alone can miss a duplicate: a second server that
    republished server.json owns the recorded pid, so the file stops naming
    the first one, and only counting this process too makes the pair visible.
    """
    pids = {os.getpid()}
    info = read_server_info(server_info_path(context.state_directory))
    if info is not None and _pid_is_alive(info.get("pid")):
        pids.add(info.get("pid"))
    for pid, line in _command_lines() or ():
        if _SERVER_ENTRY_POINT in line and _names_this_repository(line):
            pids.add(pid)
    return len(pids)


def _count_repository_python_processes():
    """Every live Python interpreter whose command line names this checkout,
    or 0 when psutil is absent. Zero on both sides of a burst is what makes the
    orphan check a no-op rather than a false pass on a machine without it."""
    lines = _command_lines()
    if lines is None:
        return 0
    return sum(1 for _pid, line in lines if _names_this_repository(line))


class _Context:
    """What a check needs to drive one running server: the server itself, the
    port it bound, the pool whose ceiling is under test, the state directory
    it published server.json into, the environment a client subprocess
    inherits to find it, and the four directories the burst cycles."""

    def __init__(self, server, port, pool):
        self.server = server
        self.port = port
        self.pool = pool
        self.state_directory = _STATE_DIR
        self.environment = _client_environment(
            **{_SPAWN_LOCK_PREFERENCE: _HELD_SPAWN_LOCK_SECONDS}
        )
        self.cwds = _CWDS


@contextlib.contextmanager
def _running_server(failures, runner=None):
    """A bound server with a real worker pool, serving real datagrams on its
    own thread and shut down over the wire on the way out.

    The pool is the production WorkerPool at its production size, because its
    ceiling is the property under test; only the runner is swappable, so a
    check can wedge a refresher without wedging anything real. The spawn lock
    is taken after bind(), since bind() clears whatever lock the client that
    started this server was holding.
    """
    errors = []
    pool = WorkerPool(
        size=WORKER_POOL_SIZE,
        runner=server_jobs.run_refresh if runner is None else runner,
        error_logger=errors.append,
    )
    server = _socket_server(pool=pool)
    port = server.bind()
    _hold_spawn_lock()
    thread = _serving(server)
    try:
        yield _Context(server, port, pool)
    finally:
        _stop(server, port, thread, failures)
        _release_spawn_lock()
    if errors:
        failures.append(f"{len(errors)} refresh jobs raised, first {errors[0]!r}")


def _payload(context, cwd_index, session_index=None):
    """One Claude Code payload for the burst: the cwd cycles across the four
    temporary directories and each render carries its own session id, so no
    two renders in the burst share a last-render file."""
    if session_index is None:
        session_index = cwd_index
    return _claude_payload(
        cwd=context.cwds[cwd_index % _CWD_COUNT],
        session_id=f"concurrency-{session_index:04d}",
    )


def _run_clients_in_parallel(context, count, cwd_count):
    """`count` real client subprocesses at once, cycling the payload's cwd and
    workspace.current_dir across `cwd_count` directories. Returns every
    CompletedProcess, in submission order."""

    def one(index):
        return _run_client(
            context, _CLIENT_KIND, _payload(context, index % cwd_count, index)
        )

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(one, range(count)))


def _wedged(entered, count):
    """True once `count` refresh jobs have reported that they are blocked. A
    bounded wait on a condition: a pool that never fills returns False at the
    ceiling and the caller fails, rather than hanging here."""
    return all(entered.acquire(timeout=_WEDGE_TIMEOUT_SECONDS) for _ in range(count))


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
        wedged = _wedged(entered, _CWD_COUNT)
        alive = _count_server_processes(context)
        peak = context.pool.peak_in_flight()
        workers = context.pool.worker_count()
        release.set()

    if not wedged:
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
        wedged = _wedged(entered, _WORKER_CAP)
        reply = context.server.handle_request(
            {"kind": _CLIENT_KIND, "payload": _payload(context, 0)}
        )
        release.set()

    if not wedged:
        failures.append(
            f"fewer than {_WORKER_CAP} workers ever blocked;"
            " the reply below was not served against a saturated pool"
        )
    if not reply:
        failures.append("a render must reply while every worker is blocked")


def check_no_orphan_processes_remain(failures):
    """The design that failed spawned a process per stale cache read. Count
    Python processes whose command line names this repository before and
    after the burst; the delta must be zero once the server has exited."""
    before = _count_repository_python_processes()
    with _running_server(failures) as context:
        _run_clients_in_parallel(context, _RENDER_COUNT, _CWD_COUNT)
    after = _count_repository_python_processes()
    if after > before:
        failures.append(
            f"{after - before} python processes outlived the burst;"
            " the render path must leave nothing behind"
        )


def check(failures):
    check_fifty_concurrent_renders_leave_one_server(failures)
    check_a_blocked_refresher_never_blocks_a_reply(failures)
    check_no_orphan_processes_remain(failures)


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
