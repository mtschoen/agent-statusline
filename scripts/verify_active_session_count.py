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

    # Positive cases
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


def main():
    failures = []
    check_dispatch(failures)
    check_classifier(failures)
    check_cache(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: count_active_sessions + _process_matches behave correctly across all cases"
    )


if __name__ == "__main__":
    main()
