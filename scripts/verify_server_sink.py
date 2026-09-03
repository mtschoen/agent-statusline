"""Verify the resident server's ownership of the refresh sink, and the age
gate on the machine-wide session-count scan.

Two failure modes that only appear once a render server is long-lived:

The refresh sink is a module global in server_jobs. A server installs its
pool as that sink in its constructor, so the newest server silently owns
every request_refresh in the process, and a server that stops without
handing the sink back leaves every later refresh flowing into a pool nobody
drains. `Server.stop_refresh_sink` restores what it displaced.

The worker pool deduplicates a job only while it is queued or running, so an
ungated `request_refresh("session-count", ...)` on every render would start
the next machine-wide psutil walk the instant the previous one finished.
The submit is gated on the cached entry's age instead, the same shape
gitref.py uses.

Split from scripts/verify_server_requests.py, which is near the 400-line
limit and which Task 13 extends further; the fixture home, fake clock,
recording pool and payload builders are imported from it rather than
duplicated. Every clock here is injected.

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from verify_server_requests import (
    _ENCODING,
    _claude_payload,
    _FakeClock,
    _RecordingPool,
    _server,
)

# Importing the sibling script above is what redirects HOME and the state
# directory into a synthetic fixture, and it has to happen before
# statusline_lib resolves any app_dir()-based path. Reading the result here
# keeps the two import blocks apart so a sorter cannot reverse them, and
# check_the_fixture_home_is_isolated asserts the redirect actually took.
_FIXTURE_STATE_DIRECTORY = os.environ["CLAUDE_STATE_DIR"]

from statusline_lib.base import log_line
from statusline_lib.server_jobs import WorkerPool, request_refresh, set_refresh_sink
from statusline_lib.server_render import SESSION_COUNT_CACHE_TTL_SECONDS
from statusline_lib.sessions import _SESSION_COUNT_CACHE_PATH

_GATE_CWD = "/repo-under-the-gate"


def _submitted_kinds(pool):
    return [kind for kind, _argument in pool.submitted]


def _session_count_submits(pool):
    return _submitted_kinds(pool).count("session-count")


def _seed_session_count_cache(cwd, timestamp):
    """Write the cache entry a completed refresh job would have written, so
    the age gate can be driven without running a real psutil walk."""
    with open(_SESSION_COUNT_CACHE_PATH, "w", encoding=_ENCODING) as f:
        json.dump({os.path.normcase(cwd): {"count": 1, "ts": timestamp}}, f)


def check_the_fixture_home_is_isolated(failures):
    """Guards the import ordering above. If statusline_lib were ever imported
    before the sibling script installed the fixture home, every check here
    would quietly run against the real ~/.claude and seed a cache there."""
    fixture_home = os.path.dirname(_FIXTURE_STATE_DIRECTORY)
    if not _SESSION_COUNT_CACHE_PATH.startswith(fixture_home):
        failures.append(
            f"the session-count cache escaped the fixture: {_SESSION_COUNT_CACHE_PATH}"
        )


def check_stopping_a_server_restores_the_refresh_sink(failures):
    """The sink is a module global, so ownership has to be handed back rather
    than abandoned: a stopped server's pool is not being drained by anyone."""
    original = set_refresh_sink(None)
    try:
        outer_pool = _RecordingPool()
        outer = _server(pool=outer_pool)
        inner_pool = _RecordingPool()
        inner = _server(pool=inner_pool)

        request_refresh("git-ref", "/repo-a")
        if _submitted_kinds(inner_pool) != ["git-ref"] or outer_pool.submitted:
            failures.append("the newest live server must own the sink")

        inner.stop_refresh_sink()
        request_refresh("git-ref", "/repo-b")
        if len(inner_pool.submitted) != 1:
            failures.append("a stopped server's pool must receive nothing more")
        if _submitted_kinds(outer_pool) != ["git-ref"]:
            failures.append("stopping must hand the sink to the previous owner")

        inner.stop_refresh_sink()
        outer.stop_refresh_sink()
        request_refresh("git-ref", "/repo-c")
        if len(outer_pool.submitted) != 1:
            failures.append("a repeated stop must not resurrect a sink")
    finally:
        set_refresh_sink(original)


def check_the_session_count_refresh_is_gated_on_cache_age(failures):
    """Driven with the injected clock and a seeded cache entry: a render
    inside the TTL submits nothing, one past it submits again."""
    clock = _FakeClock()
    pool = _RecordingPool()
    server = _server(clock=clock, pool=pool)
    payload = _claude_payload(cwd=_GATE_CWD)

    server.handle_request({"kind": "claude", "payload": payload})
    if _session_count_submits(pool) != 1:
        failures.append(f"an absent entry must submit once: {_submitted_kinds(pool)}")

    _seed_session_count_cache(_GATE_CWD, clock.now)
    clock.now += SESSION_COUNT_CACHE_TTL_SECONDS - 1
    server.handle_request({"kind": "claude", "payload": payload})
    if _session_count_submits(pool) != 1:
        failures.append("a render inside the TTL must submit no further scan")

    clock.now += 2
    server.handle_request({"kind": "claude", "payload": payload})
    if _session_count_submits(pool) != 2:
        failures.append("a render past the TTL must submit again")

    server.stop_refresh_sink()


def check_a_render_without_a_cwd_submits_no_session_count(failures):
    """An empty cwd has no cache key, so there is nothing to score for
    staleness and nothing a scan could answer."""
    pool = _RecordingPool()
    server = _server(pool=pool)
    server.handle_request({"kind": "claude", "payload": _claude_payload(cwd="")})
    if _session_count_submits(pool):
        failures.append("an empty cwd must not submit a session-count scan")
    server.stop_refresh_sink()


def check_the_log_line_helper_survives_an_unwritable_path(failures):
    """Same best-effort contract as log_traceback: an unwritable log costs
    the line, never the request that was being served."""
    with tempfile.TemporaryDirectory() as tmp:
        blocker = os.path.join(tmp, "not-a-directory")
        with open(blocker, "w", encoding=_ENCODING) as f:
            f.write("blocker")
        # Must not raise: a file sits where the log's parent directory would be.
        log_line(os.path.join(blocker, "server.log"), "a message nobody reads")
        if os.path.exists(os.path.join(blocker, "server.log")):
            failures.append("the unwritable-log check never reached its error arm")


class _LockWatchingThread(threading.Thread):
    """Records whether the pool's lock was held at the moment start() was
    called on it, then starts for real."""

    def __init__(self, pool, held, **keywords):
        super().__init__(**keywords)
        self._pool = pool
        self._held = held

    def start(self):
        self._held.append(self._pool._lock.locked())
        super().start()


def check_the_pool_starts_every_worker_under_its_lock(failures):
    """WorkerPool.start() publishes its threads and starts them under one
    lock hold. Anything that takes the lock in between, stop() above all,
    sees a published thread that was never started and joins it, which
    raises RuntimeError and leaves the pool half up."""
    pool = WorkerPool(size=2, runner=lambda kind, argument: None)
    held = []
    original = threading.Thread
    threading.Thread = lambda **keywords: _LockWatchingThread(pool, held, **keywords)
    try:
        pool.start()
    finally:
        threading.Thread = original
        pool.stop()
    if held != [True, True]:
        failures.append(f"every worker must be started under the pool lock: {held}")


def check(failures):
    check_the_fixture_home_is_isolated(failures)
    check_stopping_a_server_restores_the_refresh_sink(failures)
    check_the_session_count_refresh_is_gated_on_cache_age(failures)
    check_a_render_without_a_cwd_submits_no_session_count(failures)
    check_the_log_line_helper_survives_an_unwritable_path(failures)
    check_the_pool_starts_every_worker_under_its_lock(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: refresh sink, session-count gating and pool start-up verified")


if __name__ == "__main__":
    main()
