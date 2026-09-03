"""Verify the re-read the client performs after it claims the spawn lock.

The lock alone is not enough. A client decides to spawn from the server.json
it read at the top of the render, and it can be delayed between that read and
the claim: another client's replacement server publishes its own server.json
and clears the lock in that gap, so the delayed client finds a free lock,
claims it, and starts a second server on a second port. The re-read closes
that window by comparing the file against the snapshot the decision was made
on, and abandoning the spawn when a current-version replacement is already
published.

Every arm is here: a replacement published (spawn nothing), the same file
still in place (the server really is gone, spawn once), a changed file whose
version this checkout has moved past (still worth replacing), and no file at
all (cold start, spawn once). Everything goes through
statusline_client_support's `_spawner` seam and a `read_info` stub, so no
process is started and no clock is consulted.

Run from anywhere; imports from agent-statusline by path.
"""

import os
import sys

# The scripts directory first: importing the fixtures next door is what
# installs the isolated HOME and CLAUDE_STATE_DIR, so it has to happen before
# any statusline_lib module resolves an app_dir()-based path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_client_fallback import _clean_state
from verify_client_spawn import (
    _STALE_VERSION,
    _injected_spawner,
    _Spawns,
    _write_server_info,
)

# Only now the repository root and the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from verify_server_requests import _REPO

import statusline_client
import statusline_client_support
from statusline_lib.server_info import code_version, read_server_info, server_info_path

# A port number no check here sends to: every one of them stops at the lock or
# at the re-read, so nothing is ever bound.
_UNUSED_PORT = 1


def _published_info(version):
    """server.json as a running server would have published it, as a plain
    dict, so a stub can hand it back without touching the filesystem."""
    return {
        "pid": os.getpid(),
        "port": _UNUSED_PORT,
        "version": version,
        "started_at": 0.0,
        "platform": "claude",
    }


def _ensure_with_reader(snapshot, reader, spawner):
    """support.ensure_server driven exactly as the client drives it, with the
    re-read served by `reader` rather than by a real file. Returns whether
    this process claimed the spawn, and whether the lock survived the call."""
    with _injected_spawner(spawner):
        claimed = statusline_client_support.ensure_server(
            snapshot,
            "missing",
            lock_path=statusline_client.spawn_lock_path(),
            stale_seconds=statusline_client._spawn_lock_stale_seconds(),
            repository_root=_REPO,
            platform="claude",
            error_log_path=os.path.join(
                statusline_client.application_directory(), ".statusline-error.log"
            ),
            info_path=statusline_client.server_info_path(),
            read_info=reader,
        )
        return claimed, os.path.exists(statusline_client.spawn_lock_path())


def check_a_published_replacement_cancels_the_spawn(failures):
    """The defect this file exists for: a client that decided to spawn from a
    missing server.json, was delayed, and woke to find a current-version
    replacement already published. It must spawn nothing and leave no lock."""
    spawner = _Spawns()
    with _clean_state():
        claimed, lock_survived = _ensure_with_reader(
            None, lambda path: _published_info(code_version(_REPO)), spawner
        )
    if claimed:
        failures.append("a published replacement must not be claimed as a spawn")
    if spawner.calls:
        failures.append(f"a published replacement spawned {len(spawner.calls)} servers")
    if lock_survived:
        failures.append("an abandoned spawn must release the single-flight lock")


def check_an_unchanged_server_file_still_spawns(failures):
    """The neighbour that must not regress: the re-read returns the same file
    the decision was made on, so no replacement has published and the server
    really is gone."""
    snapshot = _published_info(code_version(_REPO))
    spawner = _Spawns()
    with _clean_state():
        claimed, _ = _ensure_with_reader(snapshot, lambda path: dict(snapshot), spawner)
    if not claimed:
        failures.append("an unchanged server.json must not cancel the spawn")
    if len(spawner.calls) != 1:
        failures.append(f"expected exactly 1 spawn, got {len(spawner.calls)}")


def check_a_changed_but_stale_server_file_still_spawns(failures):
    """The other neighbour: server.json changed, but the server it names runs
    code this checkout has moved past, so it is not the replacement this
    client was going to start."""
    spawner = _Spawns()
    with _clean_state():
        claimed, _ = _ensure_with_reader(
            None, lambda path: _published_info(_STALE_VERSION), spawner
        )
    if not claimed:
        failures.append("a stale-version server.json must not cancel the spawn")
    if len(spawner.calls) != 1:
        failures.append(f"expected exactly 1 spawn, got {len(spawner.calls)}")


def check_a_missing_server_file_still_spawns(failures):
    """Cold start, where the re-read finds nothing either: the snapshot and
    the file agree that no server exists, so the spawn goes ahead."""
    spawner = _Spawns()
    with _clean_state():
        claimed, _ = _ensure_with_reader(None, lambda path: None, spawner)
    if not claimed:
        failures.append("a missing server.json must not cancel the spawn")
    if len(spawner.calls) != 1:
        failures.append(f"expected exactly 1 spawn, got {len(spawner.calls)}")


def check_the_client_re_reads_its_own_server_file(failures):
    """The wrapper's half: statusline_client.ensure_server has to hand the
    support module its own server.json path and its own reader, or the
    re-read looks at nothing. Driven with a real file rather than a stub."""
    spawner = _Spawns()
    with _clean_state() as context, _injected_spawner(spawner):
        _write_server_info(
            context, port=_UNUSED_PORT, version=code_version(_REPO), pid=os.getpid()
        )
        claimed = statusline_client.ensure_server(None, "missing")
        lock_survived = os.path.exists(statusline_client.spawn_lock_path())
        published = read_server_info(server_info_path(context.state_directory))
    if published is None:
        failures.append("the fixture must publish a server.json to re-read")
    if claimed:
        failures.append("the client must not spawn over a published replacement")
    if spawner.calls:
        failures.append(f"the client spawned {len(spawner.calls)} servers, expected 0")
    if lock_survived:
        failures.append("the client must release the lock it abandoned")


def check(failures):
    check_a_published_replacement_cancels_the_spawn(failures)
    check_an_unchanged_server_file_still_spawns(failures)
    check_a_changed_but_stale_server_file_still_spawns(failures)
    check_a_missing_server_file_still_spawns(failures)
    check_the_client_re_reads_its_own_server_file(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: the client re-reads server.json after claiming the spawn lock")


if __name__ == "__main__":
    main()
