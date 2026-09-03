"""Verify the statusline client's spawn machinery: the version digest it
compares against, how it classifies a failure, and the single-flight lock that
keeps a hundred racing renders from starting a hundred servers.

What the client does with that decision end to end, through main and against a
real process, is scripts/verify_client_liveness.py, which imports the fixtures
below rather than rebuilding them. Split that way because the repository holds
every file at or under 400 lines.

Nothing here waits on a clock to decide anything. The one age that matters,
the spawn lock's, is written with os.utime against a synthetic timestamp.
Every spawn goes through the module-level `_spawner` seam, so the checks
assert on the command and environment a server would have been started with
and no process is started at all.

Run from anywhere; imports from agent-statusline by path.
"""

import contextlib
import json
import os
import socket
import sys
import threading
import time

# The scripts directory, so the suites next door are importable. That import
# is what installs the isolated HOME and CLAUDE_STATE_DIR, so it has to happen
# before any statusline_lib module resolves an app_dir()-based path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_client_fallback import _clean_state, _no_server, _release_spawn_lock
from verify_server_protocol import _run_client_arguments
from verify_server_requests import _ENCODING, _REPO

# Only now the repository root and the package: the writer side of server.json
# and the version digest this suite pins the client's copy against.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_client
import statusline_client_support
from statusline_lib.server_info import (
    code_version,
    read_server_info,
    server_info_path,
    version_input_files,
    write_server_info,
)

# How long a check waits for a condition, and how often it looks. Nothing
# asserts on how much of this was actually used.
_POLL_CEILING_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.05

# How many clients race for the single-flight lock. Well above the number of
# renders that can plausibly collide, which is the point.
_RACING_CLIENTS = 8

# A version no checkout computes, so server.json carrying it is always stale.
_STALE_VERSION = "0000000000000000"


def _closed_port():
    """A localhost UDP port with nothing bound to it: a socket is opened just
    long enough to be assigned one, then closed. Sending to it draws an ICMP
    rejection, which is what a server whose process exited leaves behind."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _write_server_info(context, *, port, version, pid=None):
    """server.json as a running server would have published it."""
    write_server_info(
        server_info_path(context.state_directory),
        pid=os.getpid() if pid is None else pid,
        port=port,
        version=version,
        started_at=0.0,
        platform="claude",
    )


@contextlib.contextmanager
def _dead_server():
    """server.json naming a port nothing is bound to: the shape a server that
    crashed or was killed leaves behind."""
    with _clean_state() as context:
        _write_server_info(context, port=_closed_port(), version=code_version(_REPO))
        yield context


def _write_spawn_lock(pid, age_seconds):
    """A spawn lock as a client would have left it, with its mtime pinned
    `age_seconds` into the past. The age is synthetic; nothing here sleeps."""
    path = statusline_client.spawn_lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=_ENCODING) as f:
        json.dump({"pid": pid, "at": time.time() - age_seconds}, f)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


class _Spawns:
    """A recorder standing in for process_safe.spawn_detached: it keeps the
    command and environment it was handed and starts nothing. `error`, when
    set, is raised instead, which is the launch-failure arm."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, command, *, env):
        self.calls.append((list(command), dict(env)))
        if self.error is not None:
            raise self.error


@contextlib.contextmanager
def _injected_spawner(spawner):
    """The support module's `_spawner` seam, restored on the way out, with the
    lock cleared either side so no check inherits another's."""
    saved = statusline_client_support._spawner
    statusline_client_support._spawner = spawner
    _release_spawn_lock()
    try:
        yield spawner
    finally:
        statusline_client_support._spawner = saved
        _release_spawn_lock()


def _poll_until(predicate):
    """True once `predicate` holds, False once the ceiling passes. A bounded
    wait on a condition, not a measurement of one."""
    deadline = time.monotonic() + _POLL_CEILING_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return predicate()


def check_the_client_and_the_package_agree_on_the_version(failures):
    """The client carries its own copy of the version algorithm. If the two
    ever drift, every render pays a shutdown and a respawn, forever."""
    printed = _run_client_arguments(["--print-version"]).stdout.strip()
    if printed != code_version(_REPO):
        failures.append(
            f"client computed version {printed!r}, package computes "
            f"{code_version(_REPO)!r}"
        )


def check_the_client_and_the_package_agree_on_the_version_inputs(failures):
    """The digests can only agree because the file lists do. Compared name by
    name, so a file added to one list and not the other fails here rather than
    as a respawn loop nobody can explain."""
    client_names = statusline_client_support.version_input_files(_REPO)
    package_names = version_input_files(_REPO)
    if client_names != package_names:
        failures.append(
            f"version inputs differ: client only "
            f"{sorted(set(client_names) - set(package_names))}, package only "
            f"{sorted(set(package_names) - set(client_names))}"
        )
    if "statusline_client_support.py" not in client_names:
        failures.append("the client's own support module must feed the digest")


def check_the_windows_connection_reset_branch(failures):
    """ConnectionResetError is how Windows reports a dead port on the next
    receive, and ConnectionRefusedError is how Linux does. Forced here through
    the socket seam, so the branch is reachable on either operating system."""

    class _ResettingSocket:
        def __init__(self, *arguments):
            del arguments

        def settimeout(self, seconds):
            del seconds

        def connect(self, address):
            del address

        def send(self, data):
            return len(data)

        def recv(self, size):
            del size
            raise ConnectionResetError(10054, "forcibly closed by the remote host")

        def close(self):
            """Nothing to release; request_render closes in its finally."""

    saved_factory = statusline_client._socket_factory
    statusline_client._socket_factory = _ResettingSocket
    try:
        info = {"port": 1, "pid": 999999, "version": _STALE_VERSION}
        reply, reset = statusline_client.request_render("claude", {}, info)
        if reply is not None:
            failures.append(f"a reset connection must produce no reply: {reply!r}")
        if not reset:
            failures.append("a reset connection must be reported as a dead port")
        if statusline_client.classify_failure(info, reset=True) != "dead":
            failures.append("ConnectionResetError must classify the server as dead")
    finally:
        statusline_client._socket_factory = saved_factory


def check_every_failure_shape_is_classified(failures):
    """The four reasons, and the one that must not spawn. A server that took
    the datagram and did not answer in time is busy, not broken."""
    current = {"port": 1, "pid": os.getpid(), "version": code_version(_REPO)}
    stale = {"port": 1, "pid": os.getpid(), "version": _STALE_VERSION}
    for info, reset, expected in (
        (None, False, "missing"),
        (None, True, "missing"),
        (current, True, "dead"),
        (stale, True, "dead"),
        (stale, False, "version"),
        ({"port": 1}, False, "version"),
        (current, False, "silent"),
    ):
        resolved = statusline_client.classify_failure(info, reset=reset)
        if resolved != expected:
            failures.append(
                f"info {info!r} with reset={reset} classified {resolved!r}, "
                f"expected {expected!r}"
            )


def check_a_dead_server_is_replaced_exactly_once(failures):
    """Clients racing against the same dead server must leave exactly one
    replacement. This is the single-flight lock, and it is the difference
    between this design and the one it replaced. Every racer carries the same
    snapshot, which is the server.json the fixture published."""
    claimed = []

    def claim(snapshot):
        claimed.append(statusline_client.ensure_server(snapshot, "dead"))

    with _dead_server() as context, _injected_spawner(_Spawns()) as spawner:
        info = read_server_info(server_info_path(context.state_directory))
        threads = [
            threading.Thread(target=claim, args=(info,)) for _ in range(_RACING_CLIENTS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_POLL_CEILING_SECONDS)
        if len(spawner.calls) != 1:
            failures.append(
                f"{len(spawner.calls)} servers were spawned, expected exactly 1"
            )
        if claimed.count(True) != 1:
            failures.append(
                f"{claimed.count(True)} clients claimed the lock, expected 1"
            )


def check_a_stale_spawn_lock_is_replaced(failures):
    """A lock whose writer died leaves the file behind. Older than the
    staleness window, it is replaced rather than obeyed."""
    with _dead_server() as context, _injected_spawner(_Spawns()) as spawner:
        info = read_server_info(server_info_path(context.state_directory))
        _write_spawn_lock(pid=999999, age_seconds=30)
        if not statusline_client.claim_spawn_lock():
            failures.append("a lock older than the window must not block a spawn")
        _release_spawn_lock()
        _write_spawn_lock(pid=999999, age_seconds=30)
        if not statusline_client.ensure_server(info, "dead"):
            failures.append("ensure_server must spawn through a stale lock")
        if len(spawner.calls) != 1:
            failures.append(f"expected 1 spawn through a stale lock, {spawner.calls}")


def check_a_fresh_spawn_lock_blocks_a_second_spawn(failures):
    """The other arm: a lock inside the window is another client's spawn
    already in flight, and a second one would be the pile-up this prevents."""
    with _dead_server(), _injected_spawner(_Spawns()) as spawner:
        _write_spawn_lock(pid=os.getpid(), age_seconds=0)
        if statusline_client.claim_spawn_lock():
            failures.append("a fresh lock must not be claimable")
        if statusline_client.ensure_server(None, "missing"):
            failures.append("a fresh lock must suppress a duplicate spawn")
        if spawner.calls:
            failures.append(f"a fresh lock must spawn nothing, got {spawner.calls}")


def check_the_spawn_carries_the_server_command_and_platform_pin(failures):
    """The command is this checkout's own server entry point under this
    interpreter, and the environment pins the resolved platform: the spawned
    server's argv carries no --statusline-platform flag, so without the pin a
    Kimi or Qwen client would start a server writing its state where that
    harness never reads it."""
    expected_command = [sys.executable, os.path.join(_REPO, "statusline_server.py")]
    saved_platform = os.environ.get("STATUSLINE_PLATFORM")
    os.environ["STATUSLINE_PLATFORM"] = "kimi"
    try:
        with _no_server(), _injected_spawner(_Spawns()) as spawner:
            statusline_client.ensure_server(None, "missing")
            calls = list(spawner.calls)
    finally:
        if saved_platform is None:
            os.environ.pop("STATUSLINE_PLATFORM", None)
        else:
            os.environ["STATUSLINE_PLATFORM"] = saved_platform
    if not calls:
        failures.append("a missing server must be spawned")
        return
    command, environment = calls[0]
    if command != expected_command:
        failures.append(f"spawned {command!r}, expected {expected_command!r}")
    if environment.get("STATUSLINE_PLATFORM") != "kimi":
        failures.append("the spawn must pin the resolved platform in its environment")


def check_an_unsendable_shutdown_is_not_a_traceback(failures):
    """The shutdown is sent from a render that has already printed its line,
    so nothing it can hit is worth raising over: a server.json whose port is
    not a number, and a socket that will not open, both come back False."""
    if statusline_client_support._send_shutdown({"port": "not a number"}):
        failures.append("a non-numeric port must not be treated as sendable")
    if statusline_client_support._send_shutdown({}):
        failures.append("a server.json with no port must not be treated as sendable")

    class _NoSockets:
        """The support module's `socket` reference, swapped whole rather than
        reaching into the real module: patching socket.socket itself would
        follow every other import in this process."""

        AF_INET = socket.AF_INET
        SOCK_DGRAM = socket.SOCK_DGRAM

        @staticmethod
        def socket(*arguments, **keywords):
            del arguments, keywords
            raise OSError("no socket available")

    saved = statusline_client_support.socket
    statusline_client_support.socket = _NoSockets
    try:
        if statusline_client_support._send_shutdown({"port": 1}):
            failures.append("a socket that cannot open must not report a shutdown")
    finally:
        statusline_client_support.socket = saved


def check_a_failed_spawn_is_logged_and_releases_the_lock(failures):
    """A launch that raises must not leave the lock behind: the next render
    would find a lock nobody is going to clear and never spawn at all."""
    log_path = os.path.join(
        statusline_client.application_directory(), ".statusline-error.log"
    )
    with contextlib.suppress(OSError):
        os.remove(log_path)
    with _no_server(), _injected_spawner(_Spawns(error=OSError("no interpreter"))):
        if statusline_client.ensure_server(None, "missing"):
            failures.append("a failed spawn must not report success")
        if os.path.exists(statusline_client.spawn_lock_path()):
            failures.append("a failed spawn must release the single-flight lock")
    try:
        with open(log_path, encoding=_ENCODING) as f:
            logged = f.read()
    except OSError:
        logged = ""
    if "statusline_server.py" not in logged:
        failures.append(f"a failed spawn must be logged, log holds {logged!r}")


def check(failures):
    check_the_client_and_the_package_agree_on_the_version(failures)
    check_the_client_and_the_package_agree_on_the_version_inputs(failures)
    check_the_windows_connection_reset_branch(failures)
    check_every_failure_shape_is_classified(failures)
    check_a_dead_server_is_replaced_exactly_once(failures)
    check_a_stale_spawn_lock_is_replaced(failures)
    check_a_fresh_spawn_lock_blocks_a_second_spawn(failures)
    check_the_spawn_carries_the_server_command_and_platform_pin(failures)
    check_an_unsendable_shutdown_is_not_a_traceback(failures)
    check_a_failed_spawn_is_logged_and_releases_the_lock(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: client version digest and single-flight spawn verified")


if __name__ == "__main__":
    main()
