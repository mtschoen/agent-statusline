"""Test telemetry and process fixtures for verify_server_concurrency.py."""

import atexit
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

# The scripts directory and repository root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from verify_client_fallback import (
    _hold_spawn_lock,
    _release_spawn_lock,
)
from verify_server_loop import _serving, _stop
from verify_server_protocol import _client_environment, _run_client
from verify_server_requests import _STATE_DIR, _claude_payload
from verify_server_socket import _socket_server

import statusline_client_support
from statusline_lib import server_jobs
from statusline_lib.server_info import read_server_info, server_info_path
from statusline_lib.server_jobs import WORKER_POOL_SIZE, WorkerPool

_CWD_COUNT = 4
_SPAWN_LOCK_PREFERENCE = "STATUSLINE_SPAWN_LOCK_STALE_SECONDS"
_HELD_SPAWN_LOCK_SECONDS = "3600"
_FALLBACK_AGE_PREFERENCE = "STATUSLINE_FALLBACK_MAXIMUM_AGE_SECONDS"
_UNUSABLE_FALLBACK_AGE_SECONDS = "0.001"

_CWD_ROOT = tempfile.mkdtemp(prefix="verify-server-concurrency-")
atexit.register(shutil.rmtree, _CWD_ROOT, ignore_errors=True)
_CWDS = []
for _index in range(_CWD_COUNT):
    _directory = os.path.join(_CWD_ROOT, f"cwd-{_index}")
    os.makedirs(_directory, exist_ok=True)
    _CWDS.append(_directory)


class RecordingWorkerPool(WorkerPool):
    """A WorkerPool subclass that records every submit() attempt and its
    verdict without changing any production pool behavior."""

    def __init__(self, size, runner, error_logger):
        super().__init__(size=size, runner=runner, error_logger=error_logger)
        self._telemetry_lock = threading.Lock()
        self._submissions = []

    def submit(self, kind, argument):
        accepted = super().submit(kind, argument)
        with self._telemetry_lock:
            self._submissions.append((kind, str(argument), accepted))
        return accepted

    def submissions(self):
        """A shallow snapshot of (kind, argument_str, accepted) in call order."""
        with self._telemetry_lock:
            return list(self._submissions)


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
            **{
                _SPAWN_LOCK_PREFERENCE: _HELD_SPAWN_LOCK_SECONDS,
                _FALLBACK_AGE_PREFERENCE: _UNUSABLE_FALLBACK_AGE_SECONDS,
            }
        )
        self.cwds = _CWDS


@contextlib.contextmanager
def running_server(failures, runner=None):
    """A bound server with a real worker pool, serving real datagrams on its
    own thread and shut down over the wire on the way out."""
    errors = []
    pool = RecordingWorkerPool(
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


def payload(context, cwd_index, session_index=None):
    """One Claude Code payload for the burst."""
    if session_index is None:
        session_index = cwd_index
    return _claude_payload(
        cwd=context.cwds[cwd_index % _CWD_COUNT],
        session_id=f"concurrency-{session_index:04d}",
    )


_WRAPPER_SOURCE = """
import json
import os
import subprocess
import sys

command = json.loads(sys.argv[1])
payload_path = sys.argv[2]
pid_path = sys.argv[3]

with open(payload_path, "rb") as stdin_file:
    child = subprocess.Popen(
        command,
        stdin=stdin_file,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

temp_pid_path = f"{pid_path}.tmp"
with open(temp_pid_path, "w", encoding="utf-8") as f:
    f.write(str(child.pid))
os.replace(temp_pid_path, pid_path)

child.wait()
"""


def start_client_under_parent(command, payload_path, environment, pid_path):
    """Launch a wrapper parent process that starts a client with stdin fed from
    payload_path, writes the client child's PID atomically to pid_path, and
    waits for it."""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _WRAPPER_SOURCE,
            json.dumps(command),
            payload_path,
            pid_path,
        ],
        env=environment,
    )


def command_lines(psutil_module):
    """(pid, command line) for every live Python interpreter psutil can see."""
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


def names_this_repository(line, repo):
    """Whether a command line names this checkout."""
    return os.path.normcase(repo) in os.path.normcase(line)


def count_server_processes(context, psutil_module, server_entry_point, repo):
    """How many distinct live servers this checkout has, by pid."""
    pids = {os.getpid()}
    info = read_server_info(server_info_path(context.state_directory))
    if info is not None:
        pid = info.get("pid")
        if (
            psutil_module is None
            or not isinstance(pid, int)
            or psutil_module.pid_exists(pid)
        ):
            pids.add(pid)
    for pid, line in command_lines(psutil_module) or ():
        if server_entry_point in line and names_this_repository(line, repo):
            pids.add(pid)
    return len(pids)


def count_repository_python_processes(psutil_module, repo):
    """Every live Python interpreter whose command line names this checkout."""
    lines = command_lines(psutil_module)
    if lines is None:
        return 0
    return sum(1 for _pid, line in lines if names_this_repository(line, repo))


def run_clients_in_parallel(context, count, cwd_count, client_kind):
    """`count` real client subprocesses at once."""

    def one(index):
        return _run_client(
            context, client_kind, payload(context, index % cwd_count, index)
        )

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(one, range(count)))


def wedged(entered, count, timeout_seconds):
    """True once `count` refresh jobs have reported that they are blocked."""
    return all(entered.acquire(timeout=timeout_seconds) for _ in range(count))


def expected_fallbacks(context, count, cwd_count):
    """The fallback line each client in the burst would print if the server
    failed to answer."""
    return [
        statusline_client_support.minimal_line(
            payload(context, index % cwd_count, index)
        ).strip()
        for index in range(count)
    ]
