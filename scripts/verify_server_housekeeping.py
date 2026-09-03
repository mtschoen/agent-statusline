"""Verify statusline_lib.server_state.housekeep_state_dir: the resident
server sweeps stale per-session cache files from the state directory on
start and hourly after that, since the server now outlives every render and
nothing else ever deletes them. The sweep is scoped to a fixed set of
filename prefixes and a minimum age, and it must never touch the server
info file or anything outside its own prefixes.
"""

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib.server_state import housekeep_state_dir

_ENCODING = "utf-8"


def _touch(directory, name, mtime):
    """Write an empty file named `name` under `directory` and pin its mtime
    to `mtime`, per the repo rule that tests build their own fixtures
    against a synthetic clock rather than the real one."""
    path = os.path.join(directory, name)
    with open(path, "w", encoding=_ENCODING):
        pass
    os.utime(path, (mtime, mtime))


def check_housekeeping_deletes_only_old_matching_files(failures):
    with tempfile.TemporaryDirectory() as state:
        now = 1_700_000_000.0
        old = now - 8 * 86400
        recent = now - 3600
        _touch(state, "beacons-latest-aaa.json", old)
        _touch(state, "last-render-bbb.txt", old)
        _touch(state, "render-timer-ccc.json", recent)
        _touch(state, "server.json", old)
        _touch(state, "something-else.json", old)

        deleted = housekeep_state_dir(state, now)

        remaining = sorted(os.listdir(state))
        if deleted != 2:
            failures.append(f"housekeeping deleted {deleted} files, expected 2")
        if "server.json" not in remaining:
            failures.append("housekeeping must never delete the server info file")
        if "something-else.json" not in remaining:
            failures.append("housekeeping must only touch its own prefixes")
        if "render-timer-ccc.json" not in remaining:
            failures.append("a recent file must survive")


def check_housekeeping_survives_an_unreadable_directory(failures):
    if housekeep_state_dir(os.path.join("no", "such", "dir"), 0.0) != 0:
        failures.append("a missing state directory must sweep zero files, not raise")


def check_housekeeping_survives_an_undeletable_file(failures):
    """A file that matches and is old enough can still fail to delete (a
    permission error, a concurrent remove). That must cost the sweep, not
    raise, and the file must not be counted as deleted."""
    with tempfile.TemporaryDirectory() as state:
        now = 1_700_000_000.0
        old = now - 8 * 86400
        _touch(state, "last-render-aaa.txt", old)
        with mock.patch(
            "statusline_lib.server_state.os.remove",
            side_effect=OSError("simulated remove failure"),
        ):
            deleted = housekeep_state_dir(state, now)
        if deleted != 0:
            failures.append(
                f"an undeletable file must not be counted, got {deleted} deleted"
            )
        if "last-render-aaa.txt" not in os.listdir(state):
            failures.append("an undeletable file must survive a failed remove")


def check(failures):
    check_housekeeping_deletes_only_old_matching_files(failures)
    check_housekeeping_survives_an_unreadable_directory(failures)
    check_housekeeping_survives_an_undeletable_file(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: state directory housekeeping verified")


if __name__ == "__main__":
    main()
