"""Verify the stale-while-revalidate contract for pace._pace_hourly_cached:
it must never walk transcript roots inline - it serves the cache, stale
included, and delegates recomputation to a detached child via
refresh.maybe_spawn_refresh. Siblings: verify_spend_refresh.py (the burnrate
spend cache's identical contract) and verify_refresh_spawner.py (the spawner).

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.pace as pace

_WIN_START = 1_748_000_000.0
_NOW = _WIN_START + 7200.0


class _SpawnRecorder:
    """Stands in for refresh.maybe_spawn_refresh; records (kind, argument)."""

    def __init__(self):
        self.calls = []

    def __call__(self, kind, argument):
        self.calls.append((kind, argument))
        return True


def _pace_cache_entry(win_start, computed_at, hourly):
    return {
        "entries": {
            str(int(win_start)): {
                "computed_at_unix": computed_at,
                "hourly": hourly,
            }
        }
    }


def _pin_pace(tmp, now, spawn, cache_payload=None):
    """Point pace at a temp cache file (optionally pre-seeded), a pinned
    clock, a recording spawner, and a walk stub that fails the test if the
    render path ever calls it inline. Returns the state to restore."""
    cache_path = os.path.join(tmp, "pace-cache-v2.json")
    if cache_payload is not None:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_payload, f)
    saved = (
        pace._PACE_HOURLY_CACHE_PATH,
        pace._now_unix,
        pace.maybe_spawn_refresh,
        pace._walk_pace_hourly,
    )
    pace._PACE_HOURLY_CACHE_PATH = cache_path
    pace._now_unix = lambda: now
    pace.maybe_spawn_refresh = spawn

    def inline_walk_forbidden(_win_start):
        raise AssertionError("render path walked transcripts inline")

    pace._walk_pace_hourly = inline_walk_forbidden
    return saved


def _restore_pace(saved):
    (
        pace._PACE_HOURLY_CACHE_PATH,
        pace._now_unix,
        pace.maybe_spawn_refresh,
        pace._walk_pace_hourly,
    ) = saved


def _check_pace_fresh_hit(failures):
    """A fresh cache entry is served with no spawn and no inline walk."""
    spawn = _SpawnRecorder()
    with tempfile.TemporaryDirectory() as tmp:
        payload = _pace_cache_entry(_WIN_START, _NOW - 5, [1.0, 2.0])
        saved = _pin_pace(tmp, _NOW, spawn, payload)
        try:
            result = pace._pace_hourly_cached(_WIN_START)
        finally:
            _restore_pace(saved)
    if result != [1.0, 2.0]:
        failures.append(f"fresh hit: expected [1.0, 2.0], got {result!r}")
    if spawn.calls:
        failures.append(f"fresh hit spawned a refresh: {spawn.calls!r}")


def _check_pace_stale_serves_and_spawns(failures):
    """A stale entry is still served, and a detached refresh is requested."""
    spawn = _SpawnRecorder()
    with tempfile.TemporaryDirectory() as tmp:
        payload = _pace_cache_entry(_WIN_START, _NOW - 60, [3.0, 4.0])
        saved = _pin_pace(tmp, _NOW, spawn, payload)
        try:
            result = pace._pace_hourly_cached(_WIN_START)
        finally:
            _restore_pace(saved)
    if result != [3.0, 4.0]:
        failures.append(f"stale serve: expected [3.0, 4.0], got {result!r}")
    if spawn.calls != [("pace-hourly", _WIN_START)]:
        failures.append(f"stale spawn calls: {spawn.calls!r}")


def _check_pace_miss_returns_empty_and_spawns(failures):
    """No entry for the window: honest no-data ([]) plus a refresh request.
    Covers the absent-file, unparseable, and wrong-shape cache cases."""
    for label, raw in (
        ("absent", None),
        ("corrupt", "not json"),
        ("non-dict-entries", '{"entries": [1]}'),
        # A v1-format or torn-write value (float where the v2 entry dict
        # belongs) must read as "no data", never crash the render.
        ("float-entry", f'{{"entries": {{"{int(_WIN_START)}": 5.0}}}}'),
    ):
        spawn = _SpawnRecorder()
        with tempfile.TemporaryDirectory() as tmp:
            saved = _pin_pace(tmp, _NOW, spawn)
            if raw is not None:
                with open(pace._PACE_HOURLY_CACHE_PATH, "w", encoding="utf-8") as f:
                    f.write(raw)
            try:
                result = pace._pace_hourly_cached(_WIN_START)
            finally:
                _restore_pace(saved)
        if result != []:
            failures.append(f"{label} miss: expected [], got {result!r}")
        if spawn.calls != [("pace-hourly", _WIN_START)]:
            failures.append(f"{label} miss spawn calls: {spawn.calls!r}")


def _check_pace_refresh_writes_cache(failures):
    """refresh_pace_hourly_cache persists the walk result where the render's
    cached read can serve it, and prunes to the newest entries."""
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = os.path.join(tmp, "pace-cache-v2.json")
        stale_entries = {
            str(1_000_000 + i): {"computed_at_unix": float(i), "hourly": [0.0]}
            for i in range(pace._PACE_CACHE_MAX_ENTRIES + 3)
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"entries": stale_entries}, f)
        saved = (pace._PACE_HOURLY_CACHE_PATH, pace._now_unix, pace._walk_pace_hourly)
        pace._PACE_HOURLY_CACHE_PATH = cache_path
        pace._now_unix = lambda: _NOW
        pace._walk_pace_hourly = lambda _ws: [5.0, 6.0]
        try:
            returned = pace.refresh_pace_hourly_cache(_WIN_START)
            spawn = _SpawnRecorder()
            saved_spawn = pace.maybe_spawn_refresh
            pace.maybe_spawn_refresh = spawn
            served = pace._pace_hourly_cached(_WIN_START)
            pace.maybe_spawn_refresh = saved_spawn
            with open(cache_path, encoding="utf-8") as f:
                entries = json.load(f)["entries"]
        finally:
            (
                pace._PACE_HOURLY_CACHE_PATH,
                pace._now_unix,
                pace._walk_pace_hourly,
            ) = saved
    if returned != [5.0, 6.0]:
        failures.append(f"refresh return: expected [5.0, 6.0], got {returned!r}")
    if served != [5.0, 6.0]:
        failures.append(f"refresh then read: expected [5.0, 6.0], got {served!r}")
    if spawn.calls:
        failures.append(f"fresh read after refresh spawned: {spawn.calls!r}")
    if len(entries) > pace._PACE_CACHE_MAX_ENTRIES:
        failures.append(f"prune kept {len(entries)} entries")
    if str(int(_WIN_START)) not in entries:
        failures.append("prune dropped the entry just written")


def _check_pace_refresh_write_error_suppressed(failures):
    """An unwritable cache path must not raise out of the refresher."""
    saved = (pace._PACE_HOURLY_CACHE_PATH, pace._now_unix, pace._walk_pace_hourly)
    with tempfile.TemporaryDirectory() as tmp:
        pace._PACE_HOURLY_CACHE_PATH = os.path.join(tmp, "missing-dir", "cache.json")
        pace._now_unix = lambda: _NOW
        pace._walk_pace_hourly = lambda _ws: [7.0]
        try:
            returned = pace.refresh_pace_hourly_cache(_WIN_START)
        finally:
            (
                pace._PACE_HOURLY_CACHE_PATH,
                pace._now_unix,
                pace._walk_pace_hourly,
            ) = saved
    if returned != [7.0]:
        failures.append(f"write-error refresh: expected [7.0], got {returned!r}")


def main():
    failures = []
    _check_pace_fresh_hit(failures)
    _check_pace_stale_serves_and_spawns(failures)
    _check_pace_miss_returns_empty_and_spawns(failures)
    _check_pace_refresh_writes_cache(failures)
    _check_pace_refresh_write_error_suppressed(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: pace hourly cache is stale-while-revalidate - no inline walks,"
        " stale entries served, misses degrade honestly, refresher persists"
        " and prunes"
    )


if __name__ == "__main__":
    main()
