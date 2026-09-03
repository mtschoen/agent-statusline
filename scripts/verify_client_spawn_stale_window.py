"""Extra spawn-lock staleness checks that would not fit inside the 400-line
cap on scripts/verify_client_spawn.py. Reuses that file's fixtures rather
than rebuilding them, same split rationale as verify_client_liveness.py.

Nothing here waits on a clock either: `_write_spawn_lock` still pins the
lock's mtime through os.utime against a synthetic timestamp, this time with
a negative age to push it into the future.

Run from anywhere; imports from agent-statusline by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_client_fallback import _release_spawn_lock
from verify_client_spawn import (
    _dead_server,
    _injected_spawner,
    _Spawns,
    _write_spawn_lock,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_client
from statusline_lib.server_info import read_server_info, server_info_path


def check_a_near_future_spawn_lock_is_held(failures):
    """A lock stamped a couple of seconds ahead is a fresh lock on a
    filesystem whose mtime rounds up to a coarse tick, not a clock that
    moved. A second racer must not be let through it."""
    with _dead_server() as context, _injected_spawner(_Spawns()) as spawner:
        info = read_server_info(server_info_path(context.state_directory))
        _write_spawn_lock(pid=os.getpid(), age_seconds=-2)
        if statusline_client.claim_spawn_lock():
            failures.append("a lock 2 seconds in the future must not be claimable")
        if statusline_client.ensure_server(info, "dead"):
            failures.append("a lock 2 seconds in the future must suppress a spawn")
        if spawner.calls:
            failures.append(
                f"a lock 2 seconds in the future must spawn nothing, got {spawner.calls}"
            )


def check_a_far_future_spawn_lock_is_stale(failures):
    """A lock stamped well past the window, in either direction, is a clock
    that moved rather than a coarse-tick rounding, so it is replaced same as
    one stamped far in the past."""
    with _dead_server() as context, _injected_spawner(_Spawns()) as spawner:
        info = read_server_info(server_info_path(context.state_directory))
        _write_spawn_lock(pid=999999, age_seconds=-60)
        if not statusline_client.claim_spawn_lock():
            failures.append("a lock 60 seconds in the future must be treated as stale")
        _release_spawn_lock()
        _write_spawn_lock(pid=999999, age_seconds=-60)
        if not statusline_client.ensure_server(info, "dead"):
            failures.append("ensure_server must spawn through a far-future stale lock")
        if len(spawner.calls) != 1:
            failures.append(
                f"expected 1 spawn through a far-future lock, {spawner.calls}"
            )


def check(failures):
    check_a_near_future_spawn_lock_is_held(failures)
    check_a_far_future_spawn_lock_is_stale(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: spawn lock staleness window verified in both directions")


if __name__ == "__main__":
    main()
