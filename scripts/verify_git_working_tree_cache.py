"""Verify statusline_lib/gitref.py's working-tree badge counters
(git_working_tree_cached + the numstat/rev-list halves of
refresh_git_ref_cache): the render never runs git inline. Split from
verify_git_ref_cache.py when the branch/short_hash contract plus the
badge-counter contract together outgrew aislop's 400-line file gate --
same test shape and state_dir= isolation seam as that script.

Run from anywhere; imports from schoen-claude-status by path.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_lib.gitref as gitref_mod


class _SpawnRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, kind, argument):
        self.calls.append((kind, argument))
        return True


def _write_cache_entry(tmpdir, repo, payload):
    cache_path = gitref_mod._git_ref_cache_path(repo, state_dir=tmpdir)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _check_working_tree_empty_and_miss(failures, tmpdir):
    """git_working_tree_cached: empty cwd serves zeros without spawning;
    a cache miss serves zeros and requests a detached refresh."""
    if gitref_mod.git_working_tree_cached("", state_dir=tmpdir) != (0, 0, 0, 0):
        failures.append("empty cwd must return zero counters")

    spawn = _SpawnRecorder()
    original = gitref_mod.maybe_spawn_refresh
    gitref_mod.maybe_spawn_refresh = spawn
    try:
        if gitref_mod.git_working_tree_cached("", state_dir=tmpdir) != (0, 0, 0, 0):
            failures.append("empty cwd must return zero counters under patch")
        if spawn.calls:
            failures.append(f"empty cwd must not spawn a refresh; got {spawn.calls!r}")

        stats = gitref_mod.git_working_tree_cached("/miss/repo", state_dir=tmpdir)
        if stats != (0, 0, 0, 0):
            failures.append(f"cache miss must serve zero counters; got {stats!r}")
        if spawn.calls != [("git-ref", "/miss/repo")]:
            failures.append(f"cache miss must spawn a refresh; got {spawn.calls!r}")
    finally:
        gitref_mod.maybe_spawn_refresh = original


def _check_working_tree_fresh_and_stale(failures, tmpdir):
    """git_working_tree_cached: a fresh full entry serves its counters with
    no spawn; an expired entry still serves the stale counters while a
    detached refresh is requested."""
    _write_cache_entry(
        tmpdir,
        "/fresh/repo",
        {
            "cached_at_unix": time.time(),
            "branch": "main",
            "added": 2,
            "deleted": 1,
            "ahead": 58,
            "behind": 0,
        },
    )
    stale_ts = time.time() - gitref_mod._GIT_REF_CACHE_TTL_SECONDS - 1
    _write_cache_entry(
        tmpdir,
        "/stale/repo",
        {
            "cached_at_unix": stale_ts,
            "branch": "main",
            "added": 7,
            "deleted": 4,
            "ahead": 1,
            "behind": 2,
        },
    )

    spawn = _SpawnRecorder()
    original = gitref_mod.maybe_spawn_refresh
    gitref_mod.maybe_spawn_refresh = spawn
    try:
        stats = gitref_mod.git_working_tree_cached("/fresh/repo", state_dir=tmpdir)
        if stats != (2, 1, 58, 0):
            failures.append(f"fresh entry must serve its counters; got {stats!r}")
        if spawn.calls:
            failures.append(f"fresh entry must not spawn; got {spawn.calls!r}")

        stats = gitref_mod.git_working_tree_cached("/stale/repo", state_dir=tmpdir)
        if stats != (7, 4, 1, 2):
            failures.append(f"stale entry must still serve; got {stats!r}")
        if spawn.calls != [("git-ref", "/stale/repo")]:
            failures.append(f"stale entry must spawn; got {spawn.calls!r}")
    finally:
        gitref_mod.maybe_spawn_refresh = original


def _check_working_tree_backfill_and_tampered(failures, tmpdir):
    """git_working_tree_cached: a pre-badge entry (counter keys absent)
    serves zeros and spawns a backfill; wrong-typed counters (string, bool,
    negative, float) degrade to zeros without crashing or spawning."""
    _write_cache_entry(
        tmpdir,
        "/old/repo",
        {"cached_at_unix": time.time(), "branch": "main", "short_hash": "abc123"},
    )
    _write_cache_entry(
        tmpdir,
        "/tampered/repo",
        {
            "cached_at_unix": time.time(),
            "added": "x",
            "deleted": True,
            "ahead": -3,
            "behind": 2.5,
        },
    )

    spawn = _SpawnRecorder()
    original = gitref_mod.maybe_spawn_refresh
    gitref_mod.maybe_spawn_refresh = spawn
    try:
        stats = gitref_mod.git_working_tree_cached("/old/repo", state_dir=tmpdir)
        if stats != (0, 0, 0, 0):
            failures.append(f"pre-badge entry must serve zeros; got {stats!r}")
        if spawn.calls != [("git-ref", "/old/repo")]:
            failures.append(
                f"pre-badge entry must spawn a backfill; got {spawn.calls!r}"
            )

        stats = gitref_mod.git_working_tree_cached("/tampered/repo", state_dir=tmpdir)
        if stats != (0, 0, 0, 0):
            failures.append(
                f"wrong-typed counters must degrade to zeros; got {stats!r}"
            )
        if spawn.calls != [("git-ref", "/old/repo")]:
            failures.append(f"tampered entry must not spawn; got {spawn.calls!r}")
    finally:
        gitref_mod.maybe_spawn_refresh = original


def _check_parse_helpers(failures):
    """_parse_numstat / _parse_ahead_behind: real shapes parse, binary (`-`)
    and malformed lines skip, empty/garbage input degrades to zeros."""
    added, deleted = gitref_mod._parse_numstat(
        "3\t1\tfoo.py\n-\t-\tbin.dat\n\n2\t0\tbar.py\ngarbage\n1\t2"
    )
    if (added, deleted) != (5, 1):
        failures.append(f"_parse_numstat must sum and skip; got {(added, deleted)!r}")
    if gitref_mod._parse_numstat("") != (0, 0):
        failures.append("_parse_numstat of empty output must be zeros")

    if gitref_mod._parse_ahead_behind("2\t58") != (58, 2):
        failures.append("_parse_ahead_behind must map behind/ahead to (ahead, behind)")
    if gitref_mod._parse_ahead_behind("") != (0, 0):
        failures.append("_parse_ahead_behind of empty output must be zeros")
    if gitref_mod._parse_ahead_behind("not-a-count") != (0, 0):
        failures.append("_parse_ahead_behind of garbage must be zeros")


def main():
    failures = []
    with tempfile.TemporaryDirectory() as tmpdir:
        _check_working_tree_empty_and_miss(failures, tmpdir)
    with tempfile.TemporaryDirectory() as tmpdir:
        _check_working_tree_fresh_and_stale(failures, tmpdir)
    with tempfile.TemporaryDirectory() as tmpdir:
        _check_working_tree_backfill_and_tampered(failures, tmpdir)
    _check_parse_helpers(failures)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: git_working_tree_cached is stale-while-revalidate (miss/fresh/"
        "stale/backfill/tampered) and the numstat/rev-list parsers degrade"
        " to zeros on garbage"
    )


if __name__ == "__main__":
    main()
