"""Verify statusline_lib/gitref.py's working-tree badge counters
(git_working_tree_cached + refresh_git_worktree_cache): the render never
runs git inline, and the refresh reruns the expensive numstat diff only
when the HEAD/index fingerprint moves or the max age passes. Split from
verify_git_ref_cache.py when the branch/short_hash contract plus the
badge-counter contract together outgrew aislop's 400-line file gate --
same test shape and state_dir= isolation seam as that script.

Run from anywhere; imports from agent-statusline by path.
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
    cache_path = gitref_mod._git_worktree_cache_path(repo, state_dir=tmpdir)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _check_working_tree_empty_and_miss(failures, tmpdir):
    """git_working_tree_cached: empty cwd serves zeros without requesting a
    refresh; a cache miss serves zeros and requests a refresh."""
    if gitref_mod.git_working_tree_cached("", state_dir=tmpdir) != (0, 0, 0, 0):
        failures.append("empty cwd must return zero counters")

    spawn = _SpawnRecorder()
    original = gitref_mod.request_refresh
    gitref_mod.request_refresh = spawn
    try:
        if gitref_mod.git_working_tree_cached("", state_dir=tmpdir) != (0, 0, 0, 0):
            failures.append("empty cwd must return zero counters under patch")
        if spawn.calls:
            failures.append(f"empty cwd must not spawn a refresh; got {spawn.calls!r}")

        stats = gitref_mod.git_working_tree_cached("/miss/repo", state_dir=tmpdir)
        if stats != (0, 0, 0, 0):
            failures.append(f"cache miss must serve zero counters; got {stats!r}")
        if spawn.calls != [("git-worktree", "/miss/repo")]:
            failures.append(f"cache miss must spawn a refresh; got {spawn.calls!r}")
    finally:
        gitref_mod.request_refresh = original


def _check_working_tree_fresh_and_stale(failures, tmpdir):
    """git_working_tree_cached: a fresh full entry serves its counters with
    no refresh request; an expired entry still serves the stale counters
    while a refresh is requested."""
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
    original = gitref_mod.request_refresh
    gitref_mod.request_refresh = spawn
    try:
        stats = gitref_mod.git_working_tree_cached("/fresh/repo", state_dir=tmpdir)
        if stats != (2, 1, 58, 0):
            failures.append(f"fresh entry must serve its counters; got {stats!r}")
        if spawn.calls:
            failures.append(f"fresh entry must not spawn; got {spawn.calls!r}")

        stats = gitref_mod.git_working_tree_cached("/stale/repo", state_dir=tmpdir)
        if stats != (7, 4, 1, 2):
            failures.append(f"stale entry must still serve; got {stats!r}")
        if spawn.calls != [("git-worktree", "/stale/repo")]:
            failures.append(f"stale entry must spawn; got {spawn.calls!r}")
    finally:
        gitref_mod.request_refresh = original


def _check_working_tree_tampered(failures, tmpdir):
    """git_working_tree_cached: wrong-typed or absent counters (string,
    bool, negative, float, missing) degrade to zeros without crashing, a
    fresh entry requests nothing whatever its contents, and a malformed
    timestamp treats the cache as expired while serving existing counters."""
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
    _write_cache_entry(tmpdir, "/sparse/repo", {"cached_at_unix": time.time()})
    _write_cache_entry(
        tmpdir,
        "/bad-time/repo",
        {"cached_at_unix": None, "added": 5, "deleted": 0, "ahead": 0, "behind": 0},
    )
    _write_cache_entry(
        tmpdir,
        "/oversized-pos/repo",
        {
            "cached_at_unix": 10**400,
            "added": 3,
            "deleted": 1,
            "ahead": 2,
            "behind": 4,
        },
    )
    _write_cache_entry(
        tmpdir,
        "/oversized-neg/repo",
        {
            "cached_at_unix": -(10**400),
            "added": 1,
            "deleted": 2,
            "ahead": 3,
            "behind": 4,
        },
    )

    spawn = _SpawnRecorder()
    original = gitref_mod.request_refresh
    gitref_mod.request_refresh = spawn
    try:
        for repo in ("/tampered/repo", "/sparse/repo"):
            stats = gitref_mod.git_working_tree_cached(repo, state_dir=tmpdir)
            if stats != (0, 0, 0, 0):
                failures.append(f"{repo} must degrade to zeros; got {stats!r}")
        if spawn.calls:
            failures.append(f"fresh entries must not spawn; got {spawn.calls!r}")

        cases = (
            ("/bad-time/repo", (5, 0, 0, 0)),
            ("/oversized-pos/repo", (3, 1, 2, 4)),
            ("/oversized-neg/repo", (1, 2, 3, 4)),
        )
        for repo, expected in cases:
            spawn.calls.clear()
            stats = gitref_mod.git_working_tree_cached(repo, state_dir=tmpdir)
            if stats != expected:
                failures.append(f"{repo} must serve counters; got {stats!r}")
            if spawn.calls != [("git-worktree", repo)]:
                failures.append(
                    f"malformed timestamp must spawn a refresh; got {spawn.calls!r}"
                )
    finally:
        gitref_mod.request_refresh = original


class _FakeGit:
    """Stands in for gitref._git_command: answers the fingerprint probe from
    `git_dir`/`head` and counts the expensive diff calls."""

    def __init__(self, git_dir, head="abc123"):
        self.git_dir = git_dir
        self.head = head
        self.diff_calls = 0

    def __call__(self, cwd, *args):
        if args[0] == "rev-parse":
            return f"{self.git_dir}\n{self.head}" if self.git_dir else ""
        if args[0] == "diff":
            self.diff_calls += 1
            return "3\t1\tfoo.py\n-\t-\tbin.dat"
        return "2\t58"  # rev-list --left-right --count: behind 2, ahead 58


def _read_entry(repo, tmpdir):
    with open(gitref_mod._git_worktree_cache_path(repo, state_dir=tmpdir)) as f:
        return json.load(f)


def _rewrite_entry(repo, tmpdir, **overrides):
    entry = _read_entry(repo, tmpdir)
    entry.update(overrides)
    _write_cache_entry(tmpdir, repo, entry)


def _check_fingerprint(failures, tmpdir):
    """_worktree_fingerprint: git dir + HEAD + the index's (mtime_ns, size);
    a missing index or a failed rev-parse leaves blanks, never raises, and
    never stats an `index` relative to the server's own cwd."""
    git_dir = os.path.join(tmpdir, "gitdir")
    os.makedirs(git_dir)
    index = os.path.join(git_dir, "index")
    original = gitref_mod._git_command
    gitref_mod._git_command = _FakeGit(git_dir)
    try:
        if gitref_mod._worktree_fingerprint("/r") != [git_dir, "abc123", 0, 0]:
            failures.append("missing index must fingerprint as (0, 0)")
        with open(index, "wb") as f:
            f.write(b"12345")
        os.utime(index, ns=(1_000_000_000, 1_000_000_000))
        got = gitref_mod._worktree_fingerprint("/r")
        if got != [git_dir, "abc123", 1_000_000_000, 5]:
            failures.append(f"index stat must enter the fingerprint; got {got!r}")
        gitref_mod._git_command = _FakeGit("")
        if gitref_mod._worktree_fingerprint("/r") != ["", "", 0, 0]:
            failures.append("failed rev-parse must fingerprint as blanks")
    finally:
        gitref_mod._git_command = original


def _check_refresh_skips_unchanged(failures, tmpdir):
    """refresh_git_worktree_cache: the first run diffs and persists; an
    unchanged fingerprint within the max age skips the diff but rewrites the
    entry; an index rewrite, a new HEAD, an expired max age, or a malformed
    computation timestamp each rerun it and repair the cache."""
    git_dir = os.path.join(tmpdir, "gitdir")
    os.makedirs(git_dir)
    index = os.path.join(git_dir, "index")
    with open(index, "wb") as f:
        f.write(b"x")
    os.utime(index, ns=(1_000_000_000, 1_000_000_000))
    repo = "/refreshed/repo"
    fake = _FakeGit(git_dir)
    original = gitref_mod._git_command
    gitref_mod._git_command = fake
    try:
        if gitref_mod.refresh_git_worktree_cache(repo) is not True:
            failures.append("first refresh must run the diff")
        entry = _read_entry(repo, tmpdir)
        counters = tuple(entry.get(key) for key in gitref_mod._GIT_STAT_KEYS)
        if counters != (3, 1, 58, 2):
            failures.append(f"refresh must persist the counters; got {counters!r}")

        _rewrite_entry(repo, tmpdir, cached_at_unix=0)
        if gitref_mod.refresh_git_worktree_cache(repo) is not False:
            failures.append("unchanged fingerprint must skip the diff")
        if _read_entry(repo, tmpdir)["cached_at_unix"] == 0:
            failures.append("a skipped refresh must still restamp the entry")

        os.utime(index, ns=(2_000_000_000, 2_000_000_000))
        if gitref_mod.refresh_git_worktree_cache(repo) is not True:
            failures.append("an index rewrite must rerun the diff")

        fake.head = "def456"
        if gitref_mod.refresh_git_worktree_cache(repo) is not True:
            failures.append("a new HEAD must rerun the diff")

        expired = time.time() - gitref_mod._GIT_WORKTREE_MAX_AGE_SECONDS - 1
        _rewrite_entry(repo, tmpdir, computed_at_unix=expired)
        if gitref_mod.refresh_git_worktree_cache(repo) is not True:
            failures.append("an expired max age must rerun the diff")

        malformed_timestamps = (
            None,
            "corrupt",
            [],
            {},
            float("nan"),
            float("inf"),
            True,
            10**400,
            -(10**400),
        )
        for malformed in malformed_timestamps:
            _rewrite_entry(repo, tmpdir, computed_at_unix=malformed)
            if gitref_mod.refresh_git_worktree_cache(repo) is not True:
                failures.append(
                    f"malformed computed_at_unix={malformed!r} must rerun the diff"
                )
            repaired = _read_entry(repo, tmpdir).get(
                gitref_mod._GIT_WORKTREE_COMPUTED_KEY
            )
            if not gitref_mod._is_finite_timestamp(repaired):
                failures.append(
                    f"cache must be repaired with a finite timestamp; got {repaired!r}"
                )
            if gitref_mod.refresh_git_worktree_cache(repo) is not False:
                failures.append(
                    f"repaired cache for {malformed!r} must skip subsequent diff"
                )
        expected_diffs = 4 + len(malformed_timestamps)
        if fake.diff_calls != expected_diffs:
            failures.append(
                f"expected {expected_diffs} diffs in total; got {fake.diff_calls}"
            )
    finally:
        gitref_mod._git_command = original


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
        _check_working_tree_tampered(failures, tmpdir)
    with tempfile.TemporaryDirectory() as tmpdir:
        _check_fingerprint(failures, tmpdir)
    with tempfile.TemporaryDirectory() as tmpdir:
        # refresh_git_worktree_cache resolves state_dir via base.state_dir(),
        # so isolate it through the env var, as verify_git_ref_cache.py does.
        original_env = os.environ.get("CLAUDE_STATE_DIR")
        os.environ["CLAUDE_STATE_DIR"] = tmpdir
        try:
            _check_refresh_skips_unchanged(failures, tmpdir)
        finally:
            if original_env is None:
                os.environ.pop("CLAUDE_STATE_DIR", None)
            else:
                os.environ["CLAUDE_STATE_DIR"] = original_env
    _check_parse_helpers(failures)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: git_working_tree_cached is stale-while-revalidate (miss/fresh/"
        "stale/tampered), refresh_git_worktree_cache diffs only on a moved"
        " fingerprint or expired max age, and the parsers degrade on garbage"
    )


if __name__ == "__main__":
    main()
