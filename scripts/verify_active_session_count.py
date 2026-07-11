"""Verify `count_active_sessions` and its classifier `_process_matches`.

Covers:
  - Dispatch behavior of `count_active_sessions`: returns 0 for empty cwd
    and for a cwd no real claude is running in (psutil path with no match).
  - Pure-function tests of `_process_matches` with synthesized
    (name, cmdline, cwd) inputs -- no live or mocked psutil needed.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib import _process_matches, count_active_sessions


def check_dispatch(failures):
    # Empty cwd cannot be enumerated against -> 0, no exception.
    if count_active_sessions("") != 0:
        failures.append("empty cwd should return 0")

    # A cwd that no real claude process is running in -> 0. Use a throwaway cache
    # path so the test never reads or writes the user's real session-count cache.
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = os.path.join(tmp, "sessioncount-cache.json")
        bogus_cwd = os.path.join(tmp, "definitely-not-a-claude-cwd-zzz")
        if count_active_sessions(bogus_cwd, cache_path=cache_path) != 0:
            failures.append("cwd with no matching claude processes should return 0")


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
    """On-disk TTL memoization: a fresh entry is served from cache; an expired
    one (or a future-stamped one, after a backwards clock jump) forces a re-scan.
    Seed the cache directly so the expected value is deterministic and no live
    `claude` process is required -- the seeded sentinel (42) could never come
    from a real scan of an empty temp dir, so getting it back proves a cache hit.
    """
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = os.path.join(tmp, "sessioncount-cache.json")
        cwd = os.path.join(tmp, "proj")
        key = os.path.normcase(cwd)

        def seed(count, ts):
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({key: {"count": count, "ts": ts}}, f)

        # Fresh entry within TTL -> served from cache.
        seed(42, 1000)
        if count_active_sessions(cwd, now=1001, cache_path=cache_path, ttl=8) != 42:
            failures.append("fresh cache entry should be served without re-scanning")

        # Entry older than TTL -> re-scan (no real process here -> 0).
        seed(42, 1000)
        if count_active_sessions(cwd, now=1009, cache_path=cache_path, ttl=8) != 0:
            failures.append("expired cache entry should trigger a re-scan")

        # Future-stamped entry (clock moved backwards) -> treated as a miss.
        seed(42, 5000)
        if count_active_sessions(cwd, now=1001, cache_path=cache_path, ttl=8) != 0:
            failures.append(
                "future-stamped cache entry should be ignored (clock-skew guard)"
            )


def check_psutil_unavailable(failures):
    # sessions.py lines 27-28 and 89: when psutil cannot be imported,
    # _resolve_psutil returns None and count_active_sessions returns 0.
    import sys

    import statusline_lib.sessions as sessions_mod

    real_psutil = sys.modules.get("psutil")
    real_cached = sessions_mod._psutil
    # Simulate psutil being absent by hiding it from sys.modules and clearing the cache.
    sys.modules["psutil"] = None  # type: ignore[assignment]
    sessions_mod._psutil = None
    try:
        result = sessions_mod._resolve_psutil()
        if result is not None:
            failures.append(
                f"_resolve_psutil with psutil absent should return None; got {result!r}"
            )
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "cache.json")
            count = count_active_sessions(
                os.path.join(tmp, "proj"), cache_path=cache_path
            )
            if count != 0:
                failures.append(
                    f"count_active_sessions without psutil should return 0; got {count}"
                )
    finally:
        if real_psutil is None:
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = real_psutil
        sessions_mod._psutil = real_cached


def check_save_session_count_cache_oserror(failures):
    # sessions.py lines 59-62: _save_session_count_cache swallows OSError.
    import statusline_lib.sessions as sessions_mod

    with tempfile.TemporaryDirectory() as tmp:
        blocker = os.path.join(tmp, "not_a_dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("blocker")
        bad_path = os.path.join(blocker, "cache.json")
        # Must not raise.
        sessions_mod._save_session_count_cache(
            bad_path, {"k": {"count": 1, "ts": 1.0}}, 1.0
        )


def check_count_via_psutil_exception(failures):
    # sessions.py lines 92-93: if _count_via_psutil raises any Exception,
    # count_active_sessions catches it and returns 0.
    import statusline_lib.sessions as sessions_mod

    real_count = sessions_mod._count_via_psutil
    sessions_mod._count_via_psutil = lambda cwd, ps: (_ for _ in ()).throw(
        RuntimeError("simulated psutil failure")
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "cache.json")
            result = count_active_sessions(
                os.path.join(tmp, "proj"), cache_path=cache_path
            )
            if result != 0:
                failures.append(
                    f"count_active_sessions when _count_via_psutil raises should return 0; got {result}"
                )
    finally:
        sessions_mod._count_via_psutil = real_count


def check_load_session_count_cache_non_dict(failures):
    # sessions.py: _load_session_count_cache returns {} when JSON root is not a dict.
    import statusline_lib.sessions as sessions_mod

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
    check_psutil_unavailable(failures)
    check_save_session_count_cache_oserror(failures)
    check_count_via_psutil_exception(failures)
    check_load_session_count_cache_non_dict(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: count_active_sessions + _process_matches behave correctly across all cases"
    )


if __name__ == "__main__":
    main()
