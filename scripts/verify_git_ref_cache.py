"""Verify statusline_lib/gitref.py's stale-while-revalidate disk-cache for
_git_ref_raw_cached: the render never runs git inline. A fresh entry is
served, a stale/missing entry is served too (blank on a true miss) while a
detached refresh is requested via refresh.maybe_spawn_refresh, and
refresh_git_ref_cache (the detached child's entry point) actually runs git
and persists the result -- mirrors verify_pace_refresh.py's contract for the
walk-priced caches, applied to git-ref (render-perf ratchet step 3, PLAN.md).

The cache dir is not a module-level constant -- it is resolved fresh on every
call via statusline_lib.base.state_dir(), so isolation here uses the same
explicit `state_dir=` seam scripts/verify_render_timer.py uses for
rendertimer.py, rather than monkeypatching a baked-in path.

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline
import statusline_lib.gitref as gitref_mod

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text):
    return _ANSI.sub("", text) if text else text


class _SpawnRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, kind, argument):
        self.calls.append((kind, argument))
        return True


def _check_cache_miss_serves_blank_and_spawns(failures, tmpdir):
    spawn = _SpawnRecorder()
    original = gitref_mod.maybe_spawn_refresh
    gitref_mod.maybe_spawn_refresh = spawn
    try:
        branch, short_hash = gitref_mod._git_ref_raw_cached(
            "/some/repo", state_dir=tmpdir
        )
    finally:
        gitref_mod.maybe_spawn_refresh = original

    if (branch, short_hash) != ("", ""):
        failures.append(
            f"cache miss must serve a blank ref, never block on git;"
            f" got {(branch, short_hash)!r}"
        )
    if spawn.calls != [("git-ref", "/some/repo")]:
        failures.append(f"cache miss must spawn a refresh; got {spawn.calls!r}")


def _check_cache_hit_skips_spawn(failures, tmpdir):
    cache_path = gitref_mod._git_ref_cache_path("/cached/repo", state_dir=tmpdir)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "cached_at_unix": time.time(),
                "branch": "feature",
                "short_hash": "def456",
            },
            f,
        )

    spawn = _SpawnRecorder()
    original = gitref_mod.maybe_spawn_refresh
    gitref_mod.maybe_spawn_refresh = spawn
    try:
        branch, short_hash = gitref_mod._git_ref_raw_cached(
            "/cached/repo", state_dir=tmpdir
        )
    finally:
        gitref_mod.maybe_spawn_refresh = original

    if (branch, short_hash) != ("feature", "def456"):
        failures.append(
            f"cache hit must return cached values; got {(branch, short_hash)!r}"
        )
    if spawn.calls:
        failures.append(f"cache hit must not spawn a refresh; got {spawn.calls!r}")


def _check_cache_expiry_serves_stale_and_spawns(failures, tmpdir):
    cache_path = gitref_mod._git_ref_cache_path("/expired/repo", state_dir=tmpdir)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    stale_ts = time.time() - gitref_mod._GIT_REF_CACHE_TTL_SECONDS - 1
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(
            {"cached_at_unix": stale_ts, "branch": "old", "short_hash": "old123"}, f
        )

    spawn = _SpawnRecorder()
    original = gitref_mod.maybe_spawn_refresh
    gitref_mod.maybe_spawn_refresh = spawn
    try:
        branch, short_hash = gitref_mod._git_ref_raw_cached(
            "/expired/repo", state_dir=tmpdir
        )
    finally:
        gitref_mod.maybe_spawn_refresh = original

    if (branch, short_hash) != ("old", "old123"):
        failures.append(
            f"expired cache must still serve the stale value;"
            f" got {(branch, short_hash)!r}"
        )
    if spawn.calls != [("git-ref", "/expired/repo")]:
        failures.append(f"expired cache must spawn a refresh; got {spawn.calls!r}")


def _check_corrupt_cache_degrades(failures, tmpdir):
    cache_path = gitref_mod._git_ref_cache_path("/corrupt/repo", state_dir=tmpdir)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write("not-json")

    spawn = _SpawnRecorder()
    original = gitref_mod.maybe_spawn_refresh
    gitref_mod.maybe_spawn_refresh = spawn
    try:
        branch, short_hash = gitref_mod._git_ref_raw_cached(
            "/corrupt/repo", state_dir=tmpdir
        )
    finally:
        gitref_mod.maybe_spawn_refresh = original

    if (branch, short_hash) != ("", ""):
        failures.append(
            f"corrupt cache must degrade to a blank serve, not crash;"
            f" got {(branch, short_hash)!r}"
        )
    if spawn.calls != [("git-ref", "/corrupt/repo")]:
        failures.append(f"corrupt cache must spawn a refresh; got {spawn.calls!r}")


def _check_distinct_cwds_get_distinct_cache_entries(failures, tmpdir):
    path_a = gitref_mod._git_ref_cache_path("/repo/a", state_dir=tmpdir)
    path_b = gitref_mod._git_ref_cache_path("/repo/b", state_dir=tmpdir)
    if path_a == path_b:
        failures.append(
            "distinct cwds must map to distinct cache files (concurrent-safe keying)"
        )


def _check_git_ref_uses_cache(failures, tmpdir):
    original = statusline._git_ref_raw_cached
    statusline._git_ref_raw_cached = lambda cwd, state_dir=None: ("main", "abc123")
    try:
        rendered = _strip(statusline._git_ref("/some/repo", state_dir=tmpdir))
    finally:
        statusline._git_ref_raw_cached = original
    if rendered != "main:abc123":
        failures.append(
            f"_git_ref must render branch:hash from cached raw values; got {rendered!r}"
        )


def _check_git_ref_empty_cwd(failures):
    if statusline._git_ref("") != "":
        failures.append("_git_ref with empty cwd must return ''")


def _check_git_command(failures):
    """_git_command's three branches: a real successful call (against this
    checkout, which is a real git repo), a non-zero returncode, and an
    OSError/ProcessTimeout -- all degrade to ''."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    branch = gitref_mod._git_command(repo_root, "rev-parse", "--short", "HEAD")
    if not branch:
        failures.append("_git_command against a real repo must return a non-empty hash")

    original_run = gitref_mod.run_captured

    class _FakeResult:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    gitref_mod.run_captured = lambda *a, **kw: _FakeResult(1, "irrelevant")
    try:
        result = gitref_mod._git_command("/some/repo", "status")
    finally:
        gitref_mod.run_captured = original_run
    if result != "":
        failures.append(
            f"_git_command with non-zero returncode must return ''; got {result!r}"
        )

    def raising_run(*a, **kw):
        raise gitref_mod.ProcessTimeout("timed out", 2)

    gitref_mod.run_captured = raising_run
    try:
        result = gitref_mod._git_command("/some/repo", "status")
    finally:
        gitref_mod.run_captured = original_run
    if result != "":
        failures.append(
            f"_git_command on ProcessTimeout must return ''; got {result!r}"
        )


def _check_refresh_writes_cache(failures, tmpdir):
    """refresh_git_ref_cache runs git and persists the result where the
    render's cached read can serve it -- branch/short_hash plus the
    working-tree badge counters (numstat added/deleted, rev-list
    ahead/behind)."""
    original = gitref_mod._git_command
    calls = []

    def fake_git(cwd, *args):
        calls.append(args)
        if args[0] == "symbolic-ref":
            return "main"
        if args[0] == "rev-parse":
            return "abc123"
        if args[0] == "diff":
            return "3\t1\tfoo.py\n-\t-\tbin.dat"
        return "2\t58"  # rev-list --left-right --count: behind 2, ahead 58

    gitref_mod._git_command = fake_git
    try:
        branch, short_hash = gitref_mod.refresh_git_ref_cache("/refreshed/repo")
    finally:
        gitref_mod._git_command = original

    if (branch, short_hash) != ("main", "abc123"):
        failures.append(
            f"refresh_git_ref_cache must return the fresh values;"
            f" got {(branch, short_hash)!r}"
        )
    if len(calls) != 4:
        failures.append(
            f"refresh_git_ref_cache must call git four times; got {len(calls)} calls"
        )
    cache_path = gitref_mod._git_ref_cache_path("/refreshed/repo", state_dir=tmpdir)
    with open(cache_path, encoding="utf-8") as f:
        persisted = json.load(f)
    expected = {"added": 3, "deleted": 1, "ahead": 58, "behind": 2}
    for key, want in expected.items():
        if persisted.get(key) != want:
            failures.append(
                f"refresh must persist {key}={want}; got {persisted.get(key)!r}"
            )


def main():
    failures = []
    with tempfile.TemporaryDirectory() as tmpdir:
        _check_cache_miss_serves_blank_and_spawns(failures, tmpdir)
    with tempfile.TemporaryDirectory() as tmpdir:
        _check_cache_hit_skips_spawn(failures, tmpdir)
    with tempfile.TemporaryDirectory() as tmpdir:
        _check_cache_expiry_serves_stale_and_spawns(failures, tmpdir)
    with tempfile.TemporaryDirectory() as tmpdir:
        _check_corrupt_cache_degrades(failures, tmpdir)
    with tempfile.TemporaryDirectory() as tmpdir:
        _check_distinct_cwds_get_distinct_cache_entries(failures, tmpdir)
    with tempfile.TemporaryDirectory() as tmpdir:
        _check_git_ref_uses_cache(failures, tmpdir)
    _check_git_ref_empty_cwd(failures)
    _check_git_command(failures)
    with tempfile.TemporaryDirectory() as tmpdir:
        # refresh_git_ref_cache resolves state_dir via base.state_dir(), so
        # isolate it the same way beacon_cache's refresher test does: env var.
        original_env = os.environ.get("CLAUDE_STATE_DIR")
        os.environ["CLAUDE_STATE_DIR"] = tmpdir
        try:
            _check_refresh_writes_cache(failures, tmpdir)
        finally:
            if original_env is None:
                os.environ.pop("CLAUDE_STATE_DIR", None)
            else:
                os.environ["CLAUDE_STATE_DIR"] = original_env

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: _git_ref_raw_cached is stale-while-revalidate (hits/stale/miss/"
        "corruption) and refresh_git_ref_cache persists correctly"
    )


if __name__ == "__main__":
    main()
