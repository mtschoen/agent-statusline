"""Verify `count_active_sessions` (stale-while-revalidate), its detached
refresher `refresh_session_count_cache`, and the classifier
`_process_matches`.

Covers:
  - count_active_sessions never scans inline: a fresh entry is served, a
    stale/missing entry is served too (0 on a true miss) while a detached
    refresh is requested via maybe_spawn_refresh -- same contract as
    verify_pace_refresh.py / verify_spend_refresh.py for the walk-priced
    caches, applied to the psutil process-tree scan (render-perf ratchet
    step 3, PLAN.md: an uncached scan measured ~120ms on a machine with a
    few hundred processes).
  - refresh_session_count_cache actually runs the psutil scan and persists
    it, including the psutil-unavailable and scan-raises degrade paths.
  - Pure-function tests of `_process_matches` with synthesized
    (name, cmdline, cwd) inputs -- no live or mocked psutil needed.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.sessions as sessions_mod
from statusline_lib import _process_matches, count_active_sessions


class _SpawnRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, kind, argument):
        self.calls.append((kind, argument))
        return True


def check_dispatch(failures):
    # Empty cwd cannot be enumerated against -> 0, no exception, no spawn.
    if count_active_sessions("") != 0:
        failures.append("empty cwd should return 0")

    # A cwd with no cache entry at all -> 0 (honest miss) plus a detached
    # refresh request, never an inline psutil scan.
    spawn = _SpawnRecorder()
    original = sessions_mod.maybe_spawn_refresh
    sessions_mod.maybe_spawn_refresh = spawn
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "sessioncount-cache.json")
            bogus_cwd = os.path.join(tmp, "definitely-not-a-claude-cwd-zzz")
            result = count_active_sessions(bogus_cwd, cache_path=cache_path)
            if result != 0:
                failures.append(f"cache miss should return 0; got {result!r}")
            if spawn.calls != [("session-count", bogus_cwd)]:
                failures.append(
                    f"cache miss should spawn a refresh; got {spawn.calls!r}"
                )
    finally:
        sessions_mod.maybe_spawn_refresh = original


def check_classifier(failures):
    target = os.path.normcase("/home/user/proj")

    if not _process_matches("claude", ["claude"], "/home/user/proj", target):
        failures.append("interactive claude in target cwd should match")
    if not _process_matches("claude.exe", ["claude.exe"], "/home/user/proj", target):
        failures.append("claude.exe in target cwd should match (Windows)")
    if not _process_matches(
        "node", ["node", "/path/to/claude/cli.js"], "/home/user/proj", target
    ):
        failures.append("node-wrapped claude should match")

    with patch.object(sessions_mod.os, "name", "nt"):
        if not _process_matches("kimi.exe", ["kimi.exe"], "/home/user/proj", target):
            failures.append("kimi.exe should match on Windows")
        if _process_matches("kimi", ["kimi"], "/home/user/proj", target):
            failures.append("POSIX kimi binary should not match on Windows")

    with patch.object(sessions_mod.os, "name", "posix"):
        if not _process_matches("kimi", ["kimi"], "/home/user/proj", target):
            failures.append("kimi binary should match on POSIX")
        if not _process_matches(
            "node",
            ["node", "/opt/@moonshot-ai/kimi-code/dist/cli.js"],
            "/home/user/proj",
            target,
        ):
            failures.append("node-wrapped kimi should match on POSIX")
        if _process_matches("kimi.exe", ["kimi.exe"], "/home/user/proj", target):
            failures.append("Windows kimi.exe should not match on POSIX")

    for render_name, render_command in (
        ("py.exe", ["py", "-3", "C:/repo/kimi_statusline.py"]),
        ("python3", ["python3", "/repo/kimi_statusline.py"]),
    ):
        if _process_matches(render_name, render_command, "/home/user/proj", target):
            failures.append(f"Kimi statusline renderer {render_name} should not match")

    # Negative: -p / --print headless mode (Task subagents, scripted)
    if _process_matches(
        "claude.exe",
        ["claude.exe", "-p", "--output-format", "json"],
        "/home/user/proj",
        target,
    ):
        failures.append("-p subagent should NOT match")
    if _process_matches(
        "claude", ["claude", "--print", "hi"], "/home/user/proj", target
    ):
        failures.append("--print subagent should NOT match")

    # Negative: wrong cwd
    if _process_matches("claude", ["claude"], "/other/cwd", target):
        failures.append("wrong cwd should NOT match")

    # Negative: unrelated process name
    if _process_matches("python", ["python", "script.py"], "/home/user/proj", target):
        failures.append("non-claude process should NOT match")

    # Negative: node without 'claude' in argv (regular Node app)
    if _process_matches("node", ["node", "server.js"], "/home/user/proj", target):
        failures.append("node without claude in argv should NOT match")

    # Negative: empty / None cwd
    if _process_matches("claude", ["claude"], "", target):
        failures.append("empty cwd should NOT match")
    if _process_matches("claude", ["claude"], None, target):
        failures.append("None cwd should NOT match")


def check_cache(failures):
    """On-disk memoization is stale-while-revalidate: a fresh entry is served
    with no spawn; a stale entry (expired TTL, or future-stamped after a
    backwards clock jump) is still served -- stale beats blocked -- but
    requests a detached refresh. Seed the cache directly so the expected
    value is deterministic and no live `claude` process is required -- the
    seeded sentinel (42) could never come from a real scan of an empty temp
    dir, so getting it back proves a cache read, not a re-scan.
    """
    spawn = _SpawnRecorder()
    original = sessions_mod.maybe_spawn_refresh
    sessions_mod.maybe_spawn_refresh = spawn
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "sessioncount-cache.json")
            cwd = os.path.join(tmp, "proj")
            key = os.path.normcase(cwd)

            def seed(count, ts):
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump({key: {"count": count, "ts": ts}}, f)

            # Fresh entry within TTL -> served from cache, no spawn.
            seed(42, 1000)
            spawn.calls.clear()
            if count_active_sessions(cwd, now=1001, cache_path=cache_path, ttl=8) != 42:
                failures.append("fresh cache entry should be served without refresh")
            if spawn.calls:
                failures.append(
                    f"fresh hit should not spawn a refresh: {spawn.calls!r}"
                )

            # Entry older than TTL -> served stale (42), refresh requested.
            seed(42, 1000)
            spawn.calls.clear()
            if count_active_sessions(cwd, now=1009, cache_path=cache_path, ttl=8) != 42:
                failures.append("expired cache entry should still be served (stale)")
            if spawn.calls != [("session-count", cwd)]:
                failures.append(
                    f"expired entry should spawn a refresh: {spawn.calls!r}"
                )

            # Future-stamped entry (clock moved backwards) -> served stale too.
            seed(42, 5000)
            spawn.calls.clear()
            if count_active_sessions(cwd, now=1001, cache_path=cache_path, ttl=8) != 42:
                failures.append(
                    "future-stamped cache entry should still be served (clock-skew guard)"
                )
            if spawn.calls != [("session-count", cwd)]:
                failures.append(
                    f"future-stamped entry should spawn a refresh: {spawn.calls!r}"
                )
    finally:
        sessions_mod.maybe_spawn_refresh = original


def check_refresh_writes_cache(failures):
    """refresh_session_count_cache persists the psutil scan where the
    render's cached read can serve it."""
    real_count = sessions_mod._count_via_psutil
    sessions_mod._count_via_psutil = lambda cwd, ps: 7
    saved_path = sessions_mod._SESSION_COUNT_CACHE_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "cache.json")
            cwd = os.path.join(tmp, "proj")
            sessions_mod._SESSION_COUNT_CACHE_PATH = cache_path
            returned = sessions_mod.refresh_session_count_cache(cwd)
            spawn = _SpawnRecorder()
            original_spawn = sessions_mod.maybe_spawn_refresh
            sessions_mod.maybe_spawn_refresh = spawn
            served = count_active_sessions(cwd, cache_path=cache_path)
            sessions_mod.maybe_spawn_refresh = original_spawn
    finally:
        sessions_mod._count_via_psutil = real_count
        sessions_mod._SESSION_COUNT_CACHE_PATH = saved_path
    if returned != 7:
        failures.append(f"refresh return: expected 7, got {returned!r}")
    if served != 7:
        failures.append(f"refresh then read: expected 7, got {served!r}")
    if spawn.calls:
        failures.append(f"fresh read after refresh spawned: {spawn.calls!r}")


def check_refresh_psutil_unavailable(failures):
    """refresh_session_count_cache degrades to 0 (and still writes the
    cache) when psutil cannot be imported."""
    real_psutil = sys.modules.get("psutil")
    real_cached = sessions_mod._psutil
    sys.modules["psutil"] = None  # type: ignore[assignment]
    sessions_mod._psutil = None
    saved_path = sessions_mod._SESSION_COUNT_CACHE_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            sessions_mod._SESSION_COUNT_CACHE_PATH = os.path.join(tmp, "cache.json")
            result = sessions_mod.refresh_session_count_cache(os.path.join(tmp, "proj"))
            if result != 0:
                failures.append(
                    f"refresh_session_count_cache without psutil should return 0;"
                    f" got {result}"
                )
    finally:
        sessions_mod._SESSION_COUNT_CACHE_PATH = saved_path
        if real_psutil is None:
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = real_psutil
        sessions_mod._psutil = real_cached


def check_refresh_count_via_psutil_exception(failures):
    """A raising _count_via_psutil degrades to 0 rather than propagating out
    of the detached refresh child."""
    real_count = sessions_mod._count_via_psutil
    sessions_mod._count_via_psutil = lambda cwd, ps: (_ for _ in ()).throw(
        RuntimeError("simulated psutil failure")
    )
    saved_path = sessions_mod._SESSION_COUNT_CACHE_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            sessions_mod._SESSION_COUNT_CACHE_PATH = os.path.join(tmp, "cache.json")
            result = sessions_mod.refresh_session_count_cache(os.path.join(tmp, "proj"))
            if result != 0:
                failures.append(
                    f"refresh_session_count_cache when _count_via_psutil raises"
                    f" should return 0; got {result}"
                )
    finally:
        sessions_mod._count_via_psutil = real_count
        sessions_mod._SESSION_COUNT_CACHE_PATH = saved_path


def check_save_session_count_cache_oserror(failures):
    # sessions.py: _save_session_count_cache swallows OSError.
    with tempfile.TemporaryDirectory() as tmp:
        blocker = os.path.join(tmp, "not_a_dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("blocker")
        bad_path = os.path.join(blocker, "cache.json")
        # Must not raise.
        sessions_mod._save_session_count_cache(
            bad_path, {"k": {"count": 1, "ts": 1.0}}, 1.0
        )


def check_load_session_count_cache_non_dict(failures):
    # sessions.py: _load_session_count_cache returns {} when JSON root is not a dict.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cache.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        result = sessions_mod._load_session_count_cache(path)
        if result != {}:
            failures.append(
                f"_load_session_count_cache with JSON array should return {{}}; got {result!r}"
            )


def main():
    failures = []
    check_dispatch(failures)
    check_classifier(failures)
    check_cache(failures)
    check_refresh_writes_cache(failures)
    check_refresh_psutil_unavailable(failures)
    check_refresh_count_via_psutil_exception(failures)
    check_save_session_count_cache_oserror(failures)
    check_load_session_count_cache_non_dict(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: count_active_sessions is stale-while-revalidate, its refresher"
        " persists correctly, and _process_matches behaves correctly across"
        " all cases"
    )


if __name__ == "__main__":
    main()
