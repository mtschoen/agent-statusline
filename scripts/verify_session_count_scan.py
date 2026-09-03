"""Verify that one machine-wide process walk answers every live directory.

Walking the whole process table to answer a single directory would cost six
full psutil walks for six live directories. The resident server is one
process that knows every live cwd, so it hands the whole set to one refresh
call: the walk is taken once and every directory is scored against that one
snapshot.

Covers the widened refresher end to end -- the shared snapshot, the
one-directory-per-string backward compatibility, and the real
`_count_via_psutil` scoring several directories off a single process_iter
pass against a fake psutil.

Run from anywhere; imports from `agent-statusline` by path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.sessions as sessions_mod
from scripts._session_helpers import FakeSnapProc, make_fake_psutil


def _load(path):
    """The session-count cache file as a dict."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_one_scan_answers_every_known_cwd(failures):
    """The psutil walk is machine-wide, so one walk must fill in every live
    directory's count, rather than costing one full walk each."""
    scored = []
    snapshots_seen = []

    def counting_scan(target_cwd, psutil_module, scan=None):
        scored.append(target_cwd)
        snapshots_seen.append(scan)
        return 2

    real_count = sessions_mod._count_via_psutil
    real_resolve = sessions_mod._resolve_psutil
    sessions_mod._count_via_psutil = counting_scan
    sessions_mod._resolve_psutil = lambda: make_fake_psutil([])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "counts.json")
            sessions_mod.refresh_session_count_cache(
                ["/repo-a", "/repo-b", "/repo-c"], cache_path=path
            )
            cache = _load(path)
            taken = sessions_mod.process_snapshots_taken()
    finally:
        sessions_mod._count_via_psutil = real_count
        sessions_mod._resolve_psutil = real_resolve

    if len(cache) != 3:
        failures.append(f"one refresh must write three entries, wrote {len(cache)}")
    if scored != ["/repo-a", "/repo-b", "/repo-c"]:
        failures.append(f"every directory handed in must be scored: {scored!r}")
    if taken != 1:
        failures.append(f"three directories must cost one process snapshot: {taken}")
    if snapshots_seen[0] is None or len({id(s) for s in snapshots_seen}) != 1:
        failures.append("every directory must be scored against the same snapshot")


def check_an_empty_set_costs_nothing(failures):
    """The server hands over whatever its cwd table holds, which is empty
    until the first render and again once the last session is dropped. A
    machine-wide walk that answers nobody must not be taken at all."""
    real_resolve = sessions_mod._resolve_psutil
    sessions_mod._resolve_psutil = lambda: make_fake_psutil([])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "counts.json")
            returned = sessions_mod.refresh_session_count_cache([], cache_path=path)
            taken = sessions_mod.process_snapshots_taken()
            wrote_cache = os.path.exists(path)
    finally:
        sessions_mod._resolve_psutil = real_resolve
    if returned != 0:
        failures.append(f"an empty refresh should return 0: {returned!r}")
    if taken != 0:
        failures.append(f"an empty refresh must take no process snapshot: {taken}")
    if wrote_cache:
        failures.append("an empty refresh has nothing to write")


def check_a_single_cwd_argument_still_works(failures):
    """The signature stays backward compatible: every existing caller passes
    one string, and the pool submits one argument per job. A bare string is
    one directory, never a sequence of one-character ones."""
    real_count = sessions_mod._count_via_psutil
    real_resolve = sessions_mod._resolve_psutil
    sessions_mod._count_via_psutil = lambda cwd, ps, scan=None: 5
    sessions_mod._resolve_psutil = lambda: make_fake_psutil([])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "counts.json")
            cwd = os.path.join(tmp, "proj")
            returned = sessions_mod.refresh_session_count_cache(cwd, cache_path=path)
            cache = _load(path)
    finally:
        sessions_mod._count_via_psutil = real_count
        sessions_mod._resolve_psutil = real_resolve
    if returned != 5:
        failures.append(f"a single-cwd refresh should return its count: {returned!r}")
    if list(cache) != [os.path.normcase(cwd)]:
        failures.append(f"a bare string is exactly one directory: {list(cache)!r}")


def check_one_process_walk_serves_every_directory(failures):
    """End to end through the real _count_via_psutil against a fake psutil:
    two directories, exactly one process_iter pass, each directory scored
    against that one pass on its own."""
    shell = FakeSnapProc(50, 40, "cmd.exe", 100.0)
    first = FakeSnapProc(
        60, 50, "claude.exe", 200.0, cmdline=["claude.exe"], cwd="/repo-a"
    )
    second = FakeSnapProc(
        61, 50, "claude.exe", 200.0, cmdline=["claude.exe"], cwd="/repo-b"
    )
    fake = make_fake_psutil([shell, first, second])
    real_resolve = sessions_mod._resolve_psutil
    sessions_mod._resolve_psutil = lambda: fake
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "counts.json")
            sessions_mod.refresh_session_count_cache(
                ["/repo-a", "/repo-b", "/repo-c"], cache_path=path
            )
            cache = _load(path)
    finally:
        sessions_mod._resolve_psutil = real_resolve
    counts = {key: entry["count"] for key, entry in cache.items()}
    expected = {
        os.path.normcase("/repo-a"): 1,
        os.path.normcase("/repo-b"): 1,
        os.path.normcase("/repo-c"): 0,
    }
    if counts != expected:
        failures.append(f"one walk must score each directory on its own: {counts!r}")
    if len(fake.seen_attrs) != 1:
        failures.append(
            f"three directories must cost one process_iter pass: {fake.seen_attrs!r}"
        )


def check_bad_candidate_does_not_zero_other_directories(failures):
    shell = FakeSnapProc(50, 40, "cmd.exe", 100.0)
    bad = FakeSnapProc(
        59,
        50,
        "claude.exe",
        190.0,
        cmdline=RuntimeError("zombie cmdline"),
        cwd="/repo-a",
    )
    first = FakeSnapProc(
        60, 50, "claude.exe", 200.0, cmdline=["claude.exe"], cwd="/repo-a"
    )
    second = FakeSnapProc(
        61, 50, "claude.exe", 200.0, cmdline=["claude.exe"], cwd="/repo-b"
    )
    fake = make_fake_psutil([shell, bad, first, second])
    real_resolve = sessions_mod._resolve_psutil
    sessions_mod._resolve_psutil = lambda: fake
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "counts.json")
            sessions_mod.refresh_session_count_cache(
                ["/repo-a", "/repo-b"], cache_path=path
            )
            cache = _load(path)
    finally:
        sessions_mod._resolve_psutil = real_resolve
    counts = {key: entry["count"] for key, entry in cache.items()}
    expected = {
        os.path.normcase("/repo-a"): 1,
        os.path.normcase("/repo-b"): 1,
    }
    if counts != expected:
        failures.append(
            f"one bad candidate must not zero shared-scan counts: {counts!r}"
        )
    if len(fake.seen_attrs) != 1:
        failures.append(
            f"candidate isolation must preserve one process scan: {fake.seen_attrs!r}"
        )


def check(failures):
    check_one_scan_answers_every_known_cwd(failures)
    check_an_empty_set_costs_nothing(failures)
    check_a_single_cwd_argument_still_works(failures)
    check_one_process_walk_serves_every_directory(failures)
    check_bad_candidate_does_not_zero_other_directories(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: one process walk answers every live directory")


if __name__ == "__main__":
    main()
