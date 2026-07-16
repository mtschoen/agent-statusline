"""Verify the stale-while-revalidate contract for
burnrate._window_spend_cached: it must never rescan transcript roots inline -
it serves the cache (stale entries and the neighboring grid cell of a
trailing window included) and delegates recomputation to a detached child via
refresh.maybe_spawn_refresh. Siblings: verify_pace_refresh.py (the pace
hourly cache's identical contract) and verify_refresh_spawner.py (the
spawner).

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.burnrate as burnrate

_WIN_START = 1_748_000_000.0
_NOW = _WIN_START + 7200.0


class _SpawnRecorder:
    """Stands in for refresh.maybe_spawn_refresh; records (kind, argument)."""

    def __init__(self):
        self.calls = []

    def __call__(self, kind, argument):
        self.calls.append((kind, argument))
        return True


def _spend_cache_entry(win_start, computed_at, total):
    return {
        "sums": {
            str(int(win_start)): {
                "computed_at_unix": computed_at,
                "total": total,
            }
        }
    }


def _pin_spend(tmp, now, spawn, cache_payload=None):
    cache_path = os.path.join(tmp, "spend-cache-v2.json")
    if cache_payload is not None:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_payload, f)
    saved = (
        burnrate._SPEND_CACHE_PATH,
        burnrate._now_unix,
        burnrate.maybe_spawn_refresh,
        burnrate._sum_window_spend,
    )
    burnrate._SPEND_CACHE_PATH = cache_path
    burnrate._now_unix = lambda: now
    burnrate.maybe_spawn_refresh = spawn

    def inline_walk_forbidden(_win_start):
        raise AssertionError("render path walked transcripts inline")

    burnrate._sum_window_spend = inline_walk_forbidden
    return saved


def _restore_spend(saved):
    (
        burnrate._SPEND_CACHE_PATH,
        burnrate._now_unix,
        burnrate.maybe_spawn_refresh,
        burnrate._sum_window_spend,
    ) = saved


def _quantized(win_start):
    return int(win_start) - int(win_start) % burnrate._SPEND_CACHE_TTL_SECONDS


def _check_spend_fresh_hit(failures):
    spawn = _SpawnRecorder()
    win_q = _quantized(_WIN_START)
    with tempfile.TemporaryDirectory() as tmp:
        payload = _spend_cache_entry(win_q, _NOW - 5, 2.5)
        saved = _pin_spend(tmp, _NOW, spawn, payload)
        try:
            result = burnrate._window_spend_cached(_WIN_START)
        finally:
            _restore_spend(saved)
    if result != 2.5:
        failures.append(f"spend fresh hit: expected 2.5, got {result!r}")
    if spawn.calls:
        failures.append(f"spend fresh hit spawned: {spawn.calls!r}")


def _check_spend_stale_serves_and_spawns(failures):
    spawn = _SpawnRecorder()
    win_q = _quantized(_WIN_START)
    with tempfile.TemporaryDirectory() as tmp:
        payload = _spend_cache_entry(win_q, _NOW - 60, 3.5)
        saved = _pin_spend(tmp, _NOW, spawn, payload)
        try:
            result = burnrate._window_spend_cached(_WIN_START)
        finally:
            _restore_spend(saved)
    if result != 3.5:
        failures.append(f"spend stale serve: expected 3.5, got {result!r}")
    if spawn.calls != [("window-spend", win_q)]:
        failures.append(f"spend stale spawn calls: {spawn.calls!r}")


def _check_spend_neighbor_fallback(failures):
    """A trailing window's key moves every TTL grid step; the previous grid
    cell's total must be served (stale-while-revalidate) instead of 0.0."""
    spawn = _SpawnRecorder()
    win_q = _quantized(_WIN_START)
    neighbor = win_q - burnrate._SPEND_CACHE_TTL_SECONDS
    with tempfile.TemporaryDirectory() as tmp:
        payload = _spend_cache_entry(neighbor, _NOW - 20, 4.5)
        payload["sums"]["not-a-number"] = {"computed_at_unix": _NOW, "total": 9.9}
        saved = _pin_spend(tmp, _NOW, spawn, payload)
        try:
            result = burnrate._window_spend_cached(_WIN_START)
        finally:
            _restore_spend(saved)
    if result != 4.5:
        failures.append(f"spend neighbor fallback: expected 4.5, got {result!r}")
    if spawn.calls != [("window-spend", win_q)]:
        failures.append(f"spend neighbor spawn calls: {spawn.calls!r}")


def _check_spend_miss_returns_zero_and_spawns(failures):
    """No usable entry (absent file, corrupt file, or only a far-away window's
    key): honest 0.0 plus a refresh request."""
    far_payload = _spend_cache_entry(_quantized(_WIN_START) - 86_400, _NOW - 5, 9.9)
    win_q = _quantized(_WIN_START)
    for label, raw in (
        ("absent", None),
        ("corrupt", "not json"),
        ("non-dict-sums", '{"sums": [1]}'),
        ("far-key", json.dumps(far_payload)),
        # v1-format floats (exact key and near key) must read as "no data",
        # never crash the render - the live cache held exactly this shape
        # once, written by a half-updated tree mid-deploy.
        ("float-exact", f'{{"sums": {{"{win_q}": 5.0}}}}'),
        ("float-neighbor", f'{{"sums": {{"{win_q - 15}": 5.0}}}}'),
    ):
        spawn = _SpawnRecorder()
        with tempfile.TemporaryDirectory() as tmp:
            saved = _pin_spend(tmp, _NOW, spawn)
            if raw is not None:
                with open(burnrate._SPEND_CACHE_PATH, "w", encoding="utf-8") as f:
                    f.write(raw)
            try:
                result = burnrate._window_spend_cached(_WIN_START)
            finally:
                _restore_spend(saved)
        if result != 0.0:
            failures.append(f"spend {label} miss: expected 0.0, got {result!r}")
        if spawn.calls != [("window-spend", _quantized(_WIN_START))]:
            failures.append(f"spend {label} miss spawn calls: {spawn.calls!r}")


def _check_spend_refresh_writes_cache(failures):
    """refresh_window_spend_cache persists the sum where the render's cached
    read can serve it, and prunes to the newest entries."""
    win_q = _quantized(_WIN_START)
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = os.path.join(tmp, "spend-cache-v2.json")
        stale_sums = {
            str(2_000_000 + i): {"computed_at_unix": float(i), "total": 0.0}
            for i in range(burnrate._SPEND_CACHE_MAX_ENTRIES + 3)
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"sums": stale_sums}, f)
        saved = (
            burnrate._SPEND_CACHE_PATH,
            burnrate._now_unix,
            burnrate._sum_window_spend,
        )
        burnrate._SPEND_CACHE_PATH = cache_path
        burnrate._now_unix = lambda: _NOW
        burnrate._sum_window_spend = lambda _ws: 6.25
        try:
            returned = burnrate.refresh_window_spend_cache(win_q)
            spawn = _SpawnRecorder()
            saved_spawn = burnrate.maybe_spawn_refresh
            burnrate.maybe_spawn_refresh = spawn
            served = burnrate._window_spend_cached(_WIN_START)
            burnrate.maybe_spawn_refresh = saved_spawn
            with open(cache_path, encoding="utf-8") as f:
                sums = json.load(f)["sums"]
        finally:
            (
                burnrate._SPEND_CACHE_PATH,
                burnrate._now_unix,
                burnrate._sum_window_spend,
            ) = saved
    if returned != 6.25:
        failures.append(f"spend refresh return: expected 6.25, got {returned!r}")
    if served != 6.25:
        failures.append(f"spend refresh then read: expected 6.25, got {served!r}")
    if spawn.calls:
        failures.append(f"spend fresh read after refresh spawned: {spawn.calls!r}")
    if len(sums) > burnrate._SPEND_CACHE_MAX_ENTRIES:
        failures.append(f"spend prune kept {len(sums)} entries")
    if str(win_q) not in sums:
        failures.append("spend prune dropped the entry just written")


def _check_spend_refresh_write_error_suppressed(failures):
    saved = (
        burnrate._SPEND_CACHE_PATH,
        burnrate._now_unix,
        burnrate._sum_window_spend,
    )
    with tempfile.TemporaryDirectory() as tmp:
        burnrate._SPEND_CACHE_PATH = os.path.join(tmp, "missing-dir", "cache.json")
        burnrate._now_unix = lambda: _NOW
        burnrate._sum_window_spend = lambda _ws: 1.5
        try:
            returned = burnrate.refresh_window_spend_cache(_quantized(_WIN_START))
        finally:
            (
                burnrate._SPEND_CACHE_PATH,
                burnrate._now_unix,
                burnrate._sum_window_spend,
            ) = saved
    if returned != 1.5:
        failures.append(f"spend write-error refresh: expected 1.5, got {returned!r}")


def main():
    failures = []
    _check_spend_fresh_hit(failures)
    _check_spend_stale_serves_and_spawns(failures)
    _check_spend_neighbor_fallback(failures)
    _check_spend_miss_returns_zero_and_spawns(failures)
    _check_spend_refresh_writes_cache(failures)
    _check_spend_refresh_write_error_suppressed(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: spend cache is stale-while-revalidate - no inline rescans,"
        " stale entries and trailing-window neighbors served, misses degrade"
        " honestly, refresher persists and prunes"
    )


if __name__ == "__main__":
    main()
