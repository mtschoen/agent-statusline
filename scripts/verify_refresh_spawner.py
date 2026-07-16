"""Verify statusline_lib/refresh.py: the inflight debounce (claim, TTL prune,
release on spawn failure), run_refresh child dispatch (marker cleared even on
a raising refresher, unknown kinds rejected), and the real-interpreter child
snippet end to end against a fixture corpus.

The cache-side stale-while-revalidate contract lives in verify_pace_refresh.py.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.burnrate as burnrate
import statusline_lib.pace as pace
import statusline_lib.refresh as refresh
from statusline_lib.process_safe import run_captured

_WIN_START = 1_748_000_000.0
_NOW = _WIN_START + 7200.0


def _check_now_unix_is_wall_clock(failures):
    """The unpinned clock seam returns real unix time (every other check
    pins it, so exercise the real body once)."""
    now = refresh._now_unix()
    if not isinstance(now, float) or now < 1_700_000_000:
        failures.append(f"refresh._now_unix() returned {now!r}")


def _pin_refresh(tmp, now):
    marker_path = os.path.join(tmp, "inflight.json")
    saved = (refresh._INFLIGHT_PATH, refresh._now_unix, refresh.spawn_detached)
    refresh._INFLIGHT_PATH = marker_path
    refresh._now_unix = lambda: now
    return saved


def _restore_refresh(saved):
    (refresh._INFLIGHT_PATH, refresh._now_unix, refresh.spawn_detached) = saved


def _check_spawn_debounce(failures):
    """First stale request spawns; a second within the inflight TTL does not;
    an inflight entry older than the TTL is pruned and respawns."""
    spawned = []
    with tempfile.TemporaryDirectory() as tmp:
        saved = _pin_refresh(tmp, _NOW)
        refresh.spawn_detached = lambda command: spawned.append(command)
        # A wrong-shape marker file (valid JSON, not a dict) must read as
        # "nothing in flight", not crash the render.
        with open(refresh._INFLIGHT_PATH, "w", encoding="utf-8") as f:
            f.write("[1, 2]")
        try:
            first = refresh.maybe_spawn_refresh("pace-hourly", _WIN_START)
            second = refresh.maybe_spawn_refresh("pace-hourly", _WIN_START)
            other = refresh.maybe_spawn_refresh("window-spend", _WIN_START)
            refresh._now_unix = lambda: _NOW + refresh._INFLIGHT_TTL_SECONDS + 1
            third = refresh.maybe_spawn_refresh("pace-hourly", _WIN_START)
        finally:
            _restore_refresh(saved)
    if (first, second, other, third) != (True, False, True, True):
        failures.append(
            f"debounce: expected (True, False, True, True), got"
            f" {(first, second, other, third)!r}"
        )
    if len(spawned) != 3:
        failures.append(f"debounce spawned {len(spawned)} children")


def _check_spawn_failure_clears_claim(failures):
    """A spawn OSError degrades to False and releases the inflight claim so
    the next render can retry."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _pin_refresh(tmp, _NOW)

        def failing_spawn(_command):
            raise OSError("no interpreter")

        refresh.spawn_detached = failing_spawn
        try:
            first = refresh.maybe_spawn_refresh("pace-hourly", _WIN_START)
            refresh.spawn_detached = lambda command: None
            second = refresh.maybe_spawn_refresh("pace-hourly", _WIN_START)
        finally:
            _restore_refresh(saved)
    if (first, second) != (False, True):
        failures.append(
            f"spawn failure: expected (False, True), got {(first, second)!r}"
        )


def _check_run_refresh_dispatch(failures):
    """run_refresh routes to the registered refresher, clears the inflight
    marker afterward (even when the refresher raises), and rejects unknown
    kinds."""
    calls = []
    saved_refresher = pace.refresh_pace_hourly_cache
    pace.refresh_pace_hourly_cache = lambda ws: calls.append(ws)
    with tempfile.TemporaryDirectory() as tmp:
        saved = _pin_refresh(tmp, _NOW)
        refresh.spawn_detached = lambda command: None
        try:
            refresh.maybe_spawn_refresh("pace-hourly", _WIN_START)
            refresh.run_refresh("pace-hourly", _WIN_START)
            respawned = refresh.maybe_spawn_refresh("pace-hourly", _WIN_START)

            def raising_refresher(_ws):
                raise OSError("walk exploded")

            pace.refresh_pace_hourly_cache = raising_refresher
            raised = False
            try:
                refresh.run_refresh("pace-hourly", _WIN_START)
            except OSError:
                raised = True
            cleared_after_raise = refresh.maybe_spawn_refresh(
                "pace-hourly", _WIN_START + 999_999
            )
            unknown_rejected = False
            try:
                refresh.run_refresh("mystery-kind", _WIN_START)
            except ValueError:
                unknown_rejected = True
        finally:
            _restore_refresh(saved)
            pace.refresh_pace_hourly_cache = saved_refresher
    if calls != [_WIN_START]:
        failures.append(f"dispatch calls: {calls!r}")
    if not respawned:
        failures.append("marker not cleared after successful run_refresh")
    if not raised:
        failures.append("refresher exception was swallowed by run_refresh")
    if not cleared_after_raise:
        failures.append("marker not cleared after raising run_refresh")
    if not unknown_rejected:
        failures.append("unknown kind did not raise ValueError")


def _check_run_refresh_spend_dispatch(failures):
    """run_refresh routes window-spend to the burnrate refresher."""
    calls = []
    saved_refresher = burnrate.refresh_window_spend_cache
    burnrate.refresh_window_spend_cache = lambda ws: calls.append(ws)
    with tempfile.TemporaryDirectory() as tmp:
        saved = _pin_refresh(tmp, _NOW)
        try:
            refresh.run_refresh("window-spend", _WIN_START)
        finally:
            _restore_refresh(saved)
            burnrate.refresh_window_spend_cache = saved_refresher
    if calls != [_WIN_START]:
        failures.append(f"spend dispatch calls: {calls!r}")


def _check_child_snippet_end_to_end(failures):
    """The exact snippet maybe_spawn_refresh hands the detached child must,
    in a real interpreter with HOME pointed at a fixture corpus, walk the
    fixture transcripts and write a cache the render path can serve."""
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        slug_dir = os.path.join(home, ".claude", "projects", "C--fixture")
        os.makedirs(slug_dir)
        turn = {
            "type": "assistant",
            "timestamp": "2025-05-23T12:00:00.000Z",
            "message": {
                "role": "assistant",
                "id": "msg_1",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 100, "output_tokens": 1000},
            },
        }
        with open(os.path.join(slug_dir, "s1.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(turn) + "\n")
        env = dict(os.environ)
        env["HOME"] = home
        env["USERPROFILE"] = home
        env.pop("CLAUDE_CONFIG_DIR", None)
        # 2025-05-23T12:00:00Z sits inside a window starting one hour before.
        win_start = 1_747_998_000.0
        result = run_captured(
            [sys.executable, "-c", refresh._child_snippet("pace-hourly", win_start)],
            timeout=30,
            env=env,
        )
        if result.returncode != 0:
            failures.append(
                f"child snippet exited {result.returncode}: {result.stderr!r}"
            )
            return
        cache_path = os.path.join(
            home, ".claude", ".statusline-pace-hourly-cache-v2.json"
        )
        try:
            with open(cache_path, encoding="utf-8") as f:
                entries = json.load(f)["entries"]
        except OSError:
            failures.append("child snippet wrote no cache file")
            return
    entry = entries.get(str(int(win_start)))
    if not entry or sum(entry["hourly"]) <= 0:
        failures.append(f"child snippet cache entry unusable: {entry!r}")


def main():
    failures = []
    _check_now_unix_is_wall_clock(failures)
    _check_spawn_debounce(failures)
    _check_spawn_failure_clears_claim(failures)
    _check_run_refresh_dispatch(failures)
    _check_run_refresh_spend_dispatch(failures)
    _check_child_snippet_end_to_end(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: refresh spawner debounces and releases claims, run_refresh"
        " dispatches and always clears its marker, child snippet works end"
        " to end"
    )


if __name__ == "__main__":
    main()
