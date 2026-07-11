"""Verify statusline.py's TTL disk-cache for _git_ref: a cache hit skips the
git subprocess calls entirely, entries expire after the TTL, and a corrupt or
unwritable cache file degrades to a fresh computation rather than crashing
the render.

Render-perf ratchet step 1 (PLAN.md): _git_ref costs ~55ms/render (two git
subprocess calls) uncached. Caching branch+hash on disk per cwd with a short
TTL makes that cost invisible at statusline cadence; the coloured rendering
itself is intentionally NOT cached (colours are stable constants, so caching
raw strings keeps the cache file plain and reusable by any caller).

statusline.py is entry-point glue (outside the 100%-coverage gate), but this
still runs it directly since importing is side-effect-free until main().

Run from anywhere; imports from schoen-claude-status by path.
"""

import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text):
    return _ANSI.sub("", text) if text else text


def _check_cache_miss_computes_and_writes(failures, tmpdir):
    statusline._GIT_REF_CACHE_DIR = tmpdir
    calls = []

    def fake_git(cwd, *args):
        calls.append(args)
        return "main" if args[0] == "symbolic-ref" else "abc123"

    original = statusline._git_command
    statusline._git_command = fake_git
    try:
        branch, short_hash = statusline._git_ref_raw_cached("/some/repo")
    finally:
        statusline._git_command = original

    if (branch, short_hash) != ("main", "abc123"):
        failures.append(
            f"cache miss must compute fresh values; got {(branch, short_hash)!r}"
        )
    if len(calls) != 2:
        failures.append(
            f"cache miss must call git twice (branch+hash); got {len(calls)} calls"
        )
    cache_path = statusline._git_ref_cache_path("/some/repo")
    if not os.path.exists(cache_path):
        failures.append("cache miss must write a cache file")


def _check_cache_hit_skips_git(failures, tmpdir):
    statusline._GIT_REF_CACHE_DIR = tmpdir
    cache_path = statusline._git_ref_cache_path("/cached/repo")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "computed_at_unix": time.time(),
                "branch": "feature",
                "short_hash": "def456",
            },
            f,
        )

    calls = []

    def fake_git(cwd, *args):
        calls.append(args)
        return "must-not-be-called"

    original = statusline._git_command
    statusline._git_command = fake_git
    try:
        branch, short_hash = statusline._git_ref_raw_cached("/cached/repo")
    finally:
        statusline._git_command = original

    if (branch, short_hash) != ("feature", "def456"):
        failures.append(
            f"cache hit must return cached values; got {(branch, short_hash)!r}"
        )
    if calls:
        failures.append(f"cache hit must not call git; got {len(calls)} calls")


def _check_cache_expiry_recomputes(failures, tmpdir):
    statusline._GIT_REF_CACHE_DIR = tmpdir
    cache_path = statusline._git_ref_cache_path("/expired/repo")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    stale_ts = time.time() - statusline._GIT_REF_CACHE_TTL_SECONDS - 1
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(
            {"computed_at_unix": stale_ts, "branch": "old", "short_hash": "old123"}, f
        )

    calls = []

    def fake_git(cwd, *args):
        calls.append(args)
        return "new" if args[0] == "symbolic-ref" else "new456"

    original = statusline._git_command
    statusline._git_command = fake_git
    try:
        branch, short_hash = statusline._git_ref_raw_cached("/expired/repo")
    finally:
        statusline._git_command = original

    if (branch, short_hash) != ("new", "new456"):
        failures.append(f"expired cache must recompute; got {(branch, short_hash)!r}")
    if len(calls) != 2:
        failures.append(f"expired cache must call git twice; got {len(calls)} calls")


def _check_corrupt_cache_degrades(failures, tmpdir):
    statusline._GIT_REF_CACHE_DIR = tmpdir
    cache_path = statusline._git_ref_cache_path("/corrupt/repo")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write("not-json")

    original = statusline._git_command
    statusline._git_command = lambda cwd, *args: (
        "recovered" if args[0] == "symbolic-ref" else "rec789"
    )
    try:
        branch, short_hash = statusline._git_ref_raw_cached("/corrupt/repo")
    finally:
        statusline._git_command = original

    if (branch, short_hash) != ("recovered", "rec789"):
        failures.append(
            f"corrupt cache must degrade to a fresh computation; got {(branch, short_hash)!r}"
        )


def _check_unwritable_cache_dir_still_returns_value(failures):
    with tempfile.TemporaryDirectory() as tmp:
        blocker_file = os.path.join(tmp, "not-a-dir")
        with open(blocker_file, "w", encoding="utf-8") as f:
            f.write("x")
        # A path component that is a file makes os.makedirs fail with an
        # OSError subclass on every platform -- simulates an unwritable
        # cache dir without relying on chmod semantics that differ on Windows.
        statusline._GIT_REF_CACHE_DIR = os.path.join(blocker_file, "state")

        original = statusline._git_command
        statusline._git_command = lambda cwd, *args: (
            "ok" if args[0] == "symbolic-ref" else "okhash"
        )
        try:
            branch, short_hash = statusline._git_ref_raw_cached("/unwritable/repo")
        finally:
            statusline._git_command = original

    if (branch, short_hash) != ("ok", "okhash"):
        failures.append(
            f"unwritable cache dir must still return the computed value; "
            f"got {(branch, short_hash)!r}"
        )


def _check_distinct_cwds_get_distinct_cache_entries(failures, tmpdir):
    statusline._GIT_REF_CACHE_DIR = tmpdir
    path_a = statusline._git_ref_cache_path("/repo/a")
    path_b = statusline._git_ref_cache_path("/repo/b")
    if path_a == path_b:
        failures.append(
            "distinct cwds must map to distinct cache files (concurrent-safe keying)"
        )


def _check_git_ref_uses_cache(failures, tmpdir):
    statusline._GIT_REF_CACHE_DIR = tmpdir
    original = statusline._git_ref_raw_cached
    statusline._git_ref_raw_cached = lambda cwd: ("main", "abc123")
    try:
        rendered = _strip(statusline._git_ref("/some/repo"))
    finally:
        statusline._git_ref_raw_cached = original
    if rendered != "main:abc123":
        failures.append(
            f"_git_ref must render branch:hash from cached raw values; got {rendered!r}"
        )


def _check_git_ref_empty_cwd(failures):
    if statusline._git_ref("") != "":
        failures.append("_git_ref with empty cwd must return ''")


def main():
    failures = []
    original_dir = statusline._GIT_REF_CACHE_DIR
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            _check_cache_miss_computes_and_writes(failures, tmpdir)
        with tempfile.TemporaryDirectory() as tmpdir:
            _check_cache_hit_skips_git(failures, tmpdir)
        with tempfile.TemporaryDirectory() as tmpdir:
            _check_cache_expiry_recomputes(failures, tmpdir)
        with tempfile.TemporaryDirectory() as tmpdir:
            _check_corrupt_cache_degrades(failures, tmpdir)
        _check_unwritable_cache_dir_still_returns_value(failures)
        with tempfile.TemporaryDirectory() as tmpdir:
            _check_distinct_cwds_get_distinct_cache_entries(failures, tmpdir)
        with tempfile.TemporaryDirectory() as tmpdir:
            _check_git_ref_uses_cache(failures, tmpdir)
        _check_git_ref_empty_cwd(failures)
    finally:
        statusline._GIT_REF_CACHE_DIR = original_dir

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: _git_ref TTL cache hits/misses/expiry/corruption all verified")


if __name__ == "__main__":
    main()
