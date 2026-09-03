"""Verify the resident server's socket lifecycle: statusline_lib.server.Server
binds a localhost UDP port, publishes server.json, and gives all of it back.

The receive loop that runs between those two moments is verified next door in
scripts/verify_server_loop.py, which imports the fixtures below; the request
half, handle_request against a dict, is scripts/verify_server_requests.py,
whose fixtures this file imports in turn. That module also has to be imported
before statusline_lib is touched anywhere, because it installs the temporary
HOME and CLAUDE_STATE_DIR the whole suite is isolated by.

Sockets here are real and bound to 127.0.0.1 port 0, but no check decides
anything by reading the wall clock.

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import socket
import sys

# The scripts directory, so the request-half suite next door is importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_server_requests import (
    _ENCODING,
    _REPO,
    _STATE_DIR,
    _RecordingPool,
    _server,
)

# Only now the repository root, and only now statusline_lib: importing the
# suite above is what installed the temporary HOME and CLAUDE_STATE_DIR, and
# several statusline_lib modules resolve app_dir()-based paths at import time,
# so the package must not be imported before that isolation is in place.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_lib.server as server_module
import statusline_lib.server_socket as socket_module
from statusline_lib.server import IDLE_EXIT_SECONDS, Server
from statusline_lib.server_info import read_server_info, server_info_path
from statusline_lib.server_jobs import WORKER_POOL_SIZE, set_refresh_sink
from statusline_lib.server_socket import RECEIVE_BUFFER_BYTES, SPAWN_LOCK_FILENAME

# The two prefs seams the server exposes so a check can shrink its idle window
# and its pool instead of waiting ten minutes or starting four idle threads.
_IDLE_PREF = "STATUSLINE_SERVER_IDLE_SECONDS"
_WORKERS_PREF = "STATUSLINE_SERVER_WORKERS"


class _SocketPool(_RecordingPool):
    """The recording pool plus the two lifecycle methods the socket half
    calls. Still nothing ever runs: a socket check is about the transport."""

    def __init__(self):
        super().__init__()
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self, timeout=2.0):
        del timeout
        self.stopped = True


class _FakeSocket:
    """A socket that records its options and binds nothing. Used only to
    force the Windows arm of bind() on every operating system."""

    def __init__(self, options):
        self._options = options

    def setsockopt(self, level, option, value):
        self._options.append((level, option, value))

    def bind(self, address):
        del address

    def settimeout(self, seconds):
        del seconds

    def getsockname(self):
        return ("127.0.0.1", 54321)

    def close(self):
        pass


class _FakeSocketModule:
    """Stand-in for the socket module inside statusline_lib.server, carrying
    the Windows-only SO_EXCLUSIVEADDRUSE constant so that arm of bind() is
    exercised on Linux too. The repo's platform-branch rule is to force the
    foreign arm rather than leave a line uncovered on one platform."""

    AF_INET = socket.AF_INET
    SOCK_DGRAM = socket.SOCK_DGRAM
    SOL_SOCKET = socket.SOL_SOCKET
    SO_RCVBUF = socket.SO_RCVBUF
    SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)

    def __init__(self):
        self.options = []

    def socket(self, family, kind):
        del family, kind
        return _FakeSocket(self.options)


def _socket_server(clock=None, pool=None):
    """A server wired to the shared fixture home with a pool that can be
    started and stopped, which the request-half pool has no reason to be."""
    return _server(clock=clock, pool=pool or _SocketPool())


def check_bind_writes_the_info_file(failures):
    server = _socket_server()
    port = server.bind()
    try:
        info = read_server_info(server_info_path(_STATE_DIR))
        if info is None:
            failures.append("bind must write server.json")
        elif info["port"] != port or info["pid"] != os.getpid():
            failures.append(f"server.json disagrees with the process: {info}")
    finally:
        server.close()


def check_bind_starts_the_pool_and_close_stops_it(failures):
    """The pool has to be draining before the first datagram lands, and has
    to be told to stop when the socket goes."""
    pool = _SocketPool()
    server = _socket_server(pool=pool)
    server.bind()
    server.close()
    if not pool.started:
        failures.append("bind must start the worker pool")
    if not pool.stopped:
        failures.append("close must stop the worker pool")


def check_bind_clears_a_stale_client_spawn_lock(failures):
    """The client single-flights server spawns behind a lock file. The server
    it spawned is the one that knows the spawn finished, so it clears it."""
    lock = os.path.join(_STATE_DIR, SPAWN_LOCK_FILENAME)
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(lock, "w", encoding=_ENCODING) as f:
        f.write("held")
    server = _socket_server()
    server.bind()
    server.close()
    if os.path.exists(lock):
        failures.append("bind must remove the client's spawn lock")


def check_bind_publishes_before_clearing_the_spawn_lock(failures):
    """Order matters here too, and in the other direction: the lock is what
    single-flights the spawn, so a client that finds it free has to find a
    server.json naming a port that answers. Clearing it while server.json is
    still the previous server's leaves a window where a second client reads
    the stale file, finds the lock free, and spawns a second server onto a
    machine that is meant to hold exactly one."""
    lock = os.path.join(_STATE_DIR, SPAWN_LOCK_FILENAME)
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(lock, "w", encoding=_ENCODING) as f:
        f.write("held")
    published = []
    original_clear = server_module.clear_spawn_lock

    def recording_clear(state_directory):
        published.append(read_server_info(server_info_path(_STATE_DIR)))
        original_clear(state_directory)

    server_module.clear_spawn_lock = recording_clear
    server = _socket_server()
    try:
        port = server.bind()
    finally:
        server_module.clear_spawn_lock = original_clear
    try:
        info = published[0] if published else None
        if not published:
            failures.append("bind must clear the client's spawn lock")
        elif info is None or info.get("port") != port or info.get("pid") != os.getpid():
            failures.append(
                f"bind must publish server.json before clearing the spawn lock: {info}"
            )
        if os.path.exists(lock):
            failures.append("bind must remove the client's spawn lock")
    finally:
        server.close()


def check_close_removes_the_info_file_and_is_idempotent(failures):
    server = _socket_server()
    server.bind()
    server.close()
    if read_server_info(server_info_path(_STATE_DIR)) is not None:
        failures.append("a clean exit must remove server.json")
    # Twice, because serve() closes in a finally that can run after an
    # explicit close on the shutdown path.
    server.close()


def check_close_restores_the_refresh_sink(failures):
    """A stopped server must hand the package-wide refresh sink back, or
    every later request_refresh queues into a pool nobody drains."""
    outer = _socket_server()
    inner = _socket_server()
    inner.bind()
    inner.close()
    if set_refresh_sink(None) != outer._pool.submit:
        failures.append("close must restore the refresh sink it displaced")
    outer.stop_refresh_sink()


def check_bind_sets_the_windows_exclusive_option(failures):
    """SO_EXCLUSIVEADDRUSE is Windows-only: without it a second server can
    bind the same port and silently steal half the datagrams. The receive
    buffer is raised in the same breath, because a burst of concurrent
    renders arrives as a burst of datagrams and one the kernel drops costs
    that client its reply. Forced on every platform by patching os.name with
    the socket module stubbed, per the repo's platform-branch rule."""
    fake = _FakeSocketModule()
    saved_name, saved_socket = os.name, socket_module.socket
    os.name, socket_module.socket = "nt", fake
    try:
        server = _socket_server()
        server.bind()
        server.close()
    finally:
        os.name, socket_module.socket = saved_name, saved_socket
    if (fake.SOL_SOCKET, fake.SO_EXCLUSIVEADDRUSE, 1) not in fake.options:
        failures.append(f"bind must set SO_EXCLUSIVEADDRUSE: {fake.options}")
    if (fake.SOL_SOCKET, fake.SO_RCVBUF, RECEIVE_BUFFER_BYTES) not in fake.options:
        failures.append(f"bind must raise the receive buffer: {fake.options}")


def check_closing_an_unbound_server_leaves_a_live_info_file(failures):
    """A server that never published server.json must not remove one on the
    way out. Two servers overlap whenever a client spawns a successor, and a
    second server whose bind fails part way through would otherwise delete the
    first one's file and leave every client spawning a third."""
    live = _socket_server()
    live.bind()
    never_bound = _socket_server()
    never_bound.close()
    survived = read_server_info(server_info_path(_STATE_DIR)) is not None
    live.close()
    if not survived:
        failures.append("closing an unbound server removed a live server's info file")


class _WatchingPool(_SocketPool):
    """Records whether server.json was still on disk when the drain began, so
    a check can assert the order the two happen in rather than just the end
    state. Reads the path itself, since the drain is the observation point."""

    def __init__(self):
        super().__init__()
        self.info_file_at_stop = []

    def stop(self, timeout=2.0):
        self.info_file_at_stop.append(os.path.exists(server_info_path(_STATE_DIR)))
        super().stop(timeout)


def check_close_removes_the_info_file_before_draining_the_pool(failures):
    """Order matters: stopping the pool waits on every worker and a wedged
    refresher can hold each of them for seconds. server.json has to be gone
    before that wait starts, or it spends the whole window advertising a port
    nothing answers on."""
    pool = _WatchingPool()
    server = _socket_server(pool=pool)
    server.bind()
    server.close()
    if pool.info_file_at_stop != [False]:
        failures.append(
            f"server.json must be gone before the pool drains: {pool.info_file_at_stop}"
        )


def _idle_window_for(raw):
    """The idle window a server resolves with the override set to `raw`."""
    if raw is None:
        os.environ.pop(_IDLE_PREF, None)
    else:
        os.environ[_IDLE_PREF] = raw
    server = _socket_server()
    try:
        return server._idle_exit_seconds
    finally:
        server.close()
        os.environ.pop(_IDLE_PREF, None)


def check_the_idle_window_is_overridable(failures):
    """Unset, unparseable and non-positive all fall back to the constant; a
    positive number wins."""
    for raw, expected in (
        (None, float(IDLE_EXIT_SECONDS)),
        ("nonsense", float(IDLE_EXIT_SECONDS)),
        ("-1", float(IDLE_EXIT_SECONDS)),
        ("30", 30.0),
    ):
        resolved = _idle_window_for(raw)
        if resolved != expected:
            failures.append(f"idle override {raw!r} resolved to {resolved}")


def check_the_pool_size_is_overridable(failures):
    """The same seam for the pool. Its ceiling is the whole point of the
    pool, so the override takes a positive whole number and anything else
    keeps the built-in default."""
    for raw, expected in (("2", 2), ("nonsense", WORKER_POOL_SIZE)):
        os.environ[_WORKERS_PREF] = raw
        server = Server(_STATE_DIR, _REPO)
        size = server._pool._size
        server._pool.stop()
        server.close()
        os.environ.pop(_WORKERS_PREF, None)
        if size != expected:
            failures.append(f"worker override {raw!r} sized the pool {size}")


def check_close_leaves_a_successors_info_file(failures):
    """Two servers overlap whenever a client replaces one: the version
    mismatch path sends the shutdown and spawns without waiting for the old
    process to go. A server that publishes server.json and then drains slowly
    must not delete the file its successor published in the meantime, or every
    render after that finds no server and spawns yet another."""
    server = _socket_server()
    server.bind()
    path = server_info_path(_STATE_DIR)
    info = read_server_info(path)
    info["pid"] = os.getpid() + 1
    with open(path, "w", encoding=_ENCODING) as f:
        json.dump(info, f)
    server.close()
    if read_server_info(path) is None:
        failures.append("close removed a server.json belonging to another process")
    elif os.path.exists(path):
        os.remove(path)


def check(failures):
    check_bind_writes_the_info_file(failures)
    check_bind_starts_the_pool_and_close_stops_it(failures)
    check_bind_clears_a_stale_client_spawn_lock(failures)
    check_bind_publishes_before_clearing_the_spawn_lock(failures)
    check_close_removes_the_info_file_and_is_idempotent(failures)
    check_close_restores_the_refresh_sink(failures)
    check_bind_sets_the_windows_exclusive_option(failures)
    check_closing_an_unbound_server_leaves_a_live_info_file(failures)
    check_close_removes_the_info_file_before_draining_the_pool(failures)
    check_the_idle_window_is_overridable(failures)
    check_the_pool_size_is_overridable(failures)
    check_close_leaves_a_successors_info_file(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: server socket lifecycle verified")


if __name__ == "__main__":
    main()
