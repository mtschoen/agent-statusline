"""Verify beacon.py walker-dependent paths (format_beacon, _bias_factor_cached,
refresh_bias_factor_cache, format_calibrated_eta) and beacon_cache.py's TTL
disk-cache for beacons-latest.

_bias_factor_cached follows the same stale-while-revalidate contract as
_git_ref_raw_cached (gitref.py) and _beacons_latest_cached (beacon_cache.py,
below): the render never walks the fleet inline. A fresh entry is served, a
stale/missing entry is served too (neutral (0, None) on a true miss) while a
detached refresh is requested via refresh.maybe_spawn_refresh, and
refresh_bias_factor_cache (the detached child's entry point) actually runs
the walker and persists the result.

Patches _walker_subcommand and _find_beacon_anchors in-process so no real
walker binary is required.

Run from anywhere; imports from `agent-statusline` package by path.
"""

import json
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_lib.beacon as _beacon_mod
import statusline_lib.beacon_cache as _beacon_cache_mod
from statusline_lib.beacon import format_beacon, format_calibrated_eta

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text):
    return _ANSI.sub("", text) if text else text


def _check_format_beacon_hidden(failures):
    """Paths where the beacon column hides or shows the stale marker."""
    rendered, beacon = format_beacon(None)
    if rendered is not None or beacon is not None:
        failures.append(
            f"format_beacon(None) must be (None,None), got ({rendered!r},{beacon!r})"
        )

    _beacon_mod._beacons_latest_cached = lambda session_id: None
    rendered, beacon = format_beacon("some-session")
    if rendered is not None or beacon is not None:
        failures.append(
            f"format_beacon with no walker data must be (None,None), got ({rendered!r},{beacon!r})"
        )

    _beacon_mod._beacons_latest_cached = lambda session_id: {
        "beacon": None,
        "age_seconds": 10,
    }
    rendered, beacon = format_beacon("some-session")
    if rendered is not None or beacon is not None:
        failures.append(
            f"format_beacon with None beacon must be (None,None), got ({rendered!r},{beacon!r})"
        )

    _beacon_mod._beacons_latest_cached = lambda session_id: {
        "beacon": {"kind": "end"},
        "age_seconds": 10,
    }
    rendered, beacon = format_beacon("some-session")
    if rendered is not None or beacon is not None:
        failures.append(
            f"format_beacon with kind=end must be (None,None), got ({rendered!r},{beacon!r})"
        )

    _beacon_mod._beacons_latest_cached = lambda session_id: {
        "beacon": {"kind": "report", "eta_seconds": 60, "summary": "working"},
        "age_seconds": 600,
    }
    _beacon_mod._find_beacon_anchors = lambda _sid: (None, None, None)
    rendered, _ = format_beacon("some-session")
    stripped = _strip(rendered)
    if "stale" not in stripped:
        failures.append(f"format_beacon stale must contain 'stale', got {stripped!r}")
    if "10m" not in stripped:
        failures.append(f"format_beacon stale must show minutes, got {stripped!r}")


def _check_format_beacon(failures):
    original_cached = _beacon_mod._beacons_latest_cached
    original_anchors = _beacon_mod._find_beacon_anchors

    try:
        _check_format_beacon_hidden(failures)

        recent_begin = (datetime.now(UTC) - timedelta(minutes=3)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        recent_step = (datetime.now(UTC) - timedelta(minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        _beacon_mod._beacons_latest_cached = lambda session_id: {
            "beacon": {"kind": "report", "eta_seconds": 120, "summary": "in progress"},
            "age_seconds": 30,
        }
        _beacon_mod._find_beacon_anchors = lambda _sid: (recent_begin, recent_step, 120)
        rendered, beacon_out = format_beacon("some-session")
        stripped = _strip(rendered)
        if "turn" not in stripped or "step" not in stripped:
            failures.append(
                f"format_beacon with both anchors must show turn+step, got {stripped!r}"
            )
        if "in progress" not in stripped:
            failures.append(f"format_beacon must include summary, got {stripped!r}")

        _beacon_mod._find_beacon_anchors = lambda _sid: (recent_begin, None, 120)
        rendered, beacon_out = format_beacon("some-session")
        stripped = _strip(rendered)
        if "turn" not in stripped or "step" in stripped:
            failures.append(
                f"format_beacon turn-only: must have 'turn', no 'step'; got {stripped!r}"
            )

        _beacon_mod._find_beacon_anchors = lambda _sid: (None, None, None)
        rendered, beacon_out = format_beacon("some-session")
        stripped = _strip(rendered)
        if "no begin" not in stripped:
            failures.append(
                f"format_beacon with no anchors must show 'no begin', got {stripped!r}"
            )
        if beacon_out != {
            "kind": "report",
            "eta_seconds": 120,
            "summary": "in progress",
        }:
            failures.append(
                f"format_beacon must pass the walker beacon dict through, got {beacon_out!r}"
            )

    finally:
        _beacon_mod._beacons_latest_cached = original_cached
        _beacon_mod._find_beacon_anchors = original_anchors


def _check_bias_cache_fresh_hit_skips_spawn(failures, tmpdir):
    """A fresh cache entry is served with no spawn and no walker call."""
    cache_path = os.path.join(tmpdir, "bias-cache-fresh.json")
    _beacon_mod._BIAS_CACHE_PATH = cache_path
    period = 604800
    key = str(period)
    fresh = {
        key: {
            "computed_at_unix": datetime.now(UTC).timestamp() - 1,
            "period_seconds": period,
            "n_pairs": 8,
            "bias_factor": 0.8,
        }
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(fresh, f)

    calls = []
    _beacon_mod._walker_subcommand = lambda *_a, **_kw: calls.append(1)
    spawn = _SpawnRecorder()
    original_spawn = _beacon_mod.maybe_spawn_refresh
    _beacon_mod.maybe_spawn_refresh = spawn
    try:
        n, bias = _beacon_mod._bias_factor_cached(period)
    finally:
        _beacon_mod.maybe_spawn_refresh = original_spawn
    if (n, bias) != (8, 0.8):
        failures.append(f"fresh hit: expected (8, 0.8), got ({n!r}, {bias!r})")
    if calls:
        failures.append(f"fresh hit must not call the walker; got {len(calls)} calls")
    if spawn.calls:
        failures.append(f"fresh hit must not spawn a refresh; got {spawn.calls!r}")


def _check_bias_cache_stale_serves_and_spawns(failures, tmpdir):
    """A stale entry (validly keyed, TTL expired) is still served, and a
    detached refresh is requested -- the render must never recompute inline."""
    cache_path = os.path.join(tmpdir, "bias-cache-stale.json")
    _beacon_mod._BIAS_CACHE_PATH = cache_path
    period = 604800
    key = str(period)
    stale = {
        key: {
            "computed_at_unix": datetime.now(UTC).timestamp()
            - _beacon_mod._BIAS_CACHE_TTL_SECONDS
            - 1,
            "period_seconds": period,
            "n_pairs": 5,
            "bias_factor": 0.5,
        }
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(stale, f)

    calls = []
    _beacon_mod._walker_subcommand = lambda *_a, **_kw: calls.append(1)
    spawn = _SpawnRecorder()
    original_spawn = _beacon_mod.maybe_spawn_refresh
    _beacon_mod.maybe_spawn_refresh = spawn
    try:
        n, bias = _beacon_mod._bias_factor_cached(period)
    finally:
        _beacon_mod.maybe_spawn_refresh = original_spawn
    if (n, bias) != (5, 0.5):
        failures.append(f"stale serve: expected (5, 0.5), got ({n!r}, {bias!r})")
    if calls:
        failures.append(
            f"stale entry must not walk inline -- got {len(calls)} walker calls"
        )
    if spawn.calls != [("bias-factor", period)]:
        failures.append(f"stale entry must spawn a refresh; got {spawn.calls!r}")


def _check_bias_cache_miss_and_wrong_period_serve_neutral_and_spawn(failures, tmpdir):
    """No entry at all, and a validly-keyed entry for a DIFFERENT period, both
    read as "no data yet" -- (0, None), which format_calibrated_eta already
    treats as "hide the field" -- while requesting a refresh, never walking
    inline. Covers absent-file, corrupt-JSON, and wrong-period-key cases."""
    period = 604800
    for label, seed in (
        ("absent", None),
        ("corrupt", "not-json"),
        (
            "wrong-period",
            json.dumps(
                {
                    "999": {
                        "computed_at_unix": datetime.now(UTC).timestamp() - 1,
                        "period_seconds": 999,
                        "n_pairs": 7,
                        "bias_factor": 0.7,
                    }
                }
            ),
        ),
    ):
        cache_path = os.path.join(tmpdir, f"bias-cache-{label}.json")
        _beacon_mod._BIAS_CACHE_PATH = cache_path
        if seed is not None:
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(seed)

        calls = []
        _beacon_mod._walker_subcommand = lambda *_a, _calls=calls, **_kw: _calls.append(
            1
        )
        spawn = _SpawnRecorder()
        original_spawn = _beacon_mod.maybe_spawn_refresh
        _beacon_mod.maybe_spawn_refresh = spawn
        try:
            n, bias = _beacon_mod._bias_factor_cached(period)
        finally:
            _beacon_mod.maybe_spawn_refresh = original_spawn
        if (n, bias) != (0, None):
            failures.append(f"{label}: expected (0, None), got ({n!r}, {bias!r})")
        if calls:
            failures.append(f"{label} must not walk inline; got {len(calls)} calls")
        if spawn.calls != [("bias-factor", period)]:
            failures.append(f"{label} must spawn a refresh; got {spawn.calls!r}")


def _check_refresh_bias_factor_cache_writes(failures, tmpdir):
    """refresh_bias_factor_cache (the detached child's entry point) runs the
    walker and persists the result where the render's cached read can serve
    it, merging into (not clobbering) other periods' entries. A walker
    failure is negative-cached (failed=True) under the longer TTL so a
    slow/unreachable walker doesn't respawn a refresh child every render."""
    cache_path = os.path.join(tmpdir, "bias-cache-refresh.json")
    _beacon_mod._BIAS_CACHE_PATH = cache_path
    # Seed an unrelated period's entry that must survive the write.
    other_period_entry = {
        "300": {
            "computed_at_unix": datetime.now(UTC).timestamp(),
            "period_seconds": 300,
            "n_pairs": 42,
            "bias_factor": 2.2,
        }
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(other_period_entry, f)

    _beacon_mod._walker_subcommand = lambda *_a, **_kw: {
        "n_pairs": 25,
        "bias_factor": 1.4,
    }
    n, bias = _beacon_mod.refresh_bias_factor_cache(604800)
    if n != 25 or abs(bias - 1.4) > 0.001:
        failures.append(
            f"refresh_bias_factor_cache must return the fresh values; got ({n!r},{bias!r})"
        )
    with open(cache_path, encoding="utf-8") as f:
        cache = json.load(f)
    if "300" not in cache:
        failures.append("refresh_bias_factor_cache must not clobber other periods")
    written_entry = cache.get("604800") or {}
    if written_entry.get("n_pairs") != 25:
        failures.append(
            f"refresh_bias_factor_cache did not persist its write: {cache!r}"
        )

    # A walker failure (None) must be negative-cached with failed=True.
    _beacon_mod._walker_subcommand = lambda *_a, **_kw: None
    n2, bias2 = _beacon_mod.refresh_bias_factor_cache(604800)
    if (n2, bias2) != (0, None):
        failures.append(
            f"refresh_bias_factor_cache walker failure: expected (0, None), got ({n2!r},{bias2!r})"
        )
    with open(cache_path, encoding="utf-8") as f:
        cache = json.load(f)
    failure_entry = cache.get("604800") or {}
    if not failure_entry.get("failed"):
        failures.append("a walker failure must be negative-cached (failed=True)")


def _check_bias_cache_never_waits_on_slow_walker(failures, tmpdir):
    """The decisive regression proof for the 2026-07-26 fix: with an
    artificially slow walker (simulating the real incident -- a
    beacons-history call stuck near its 2s timeout under contention) and a
    completely cold/absent bias cache, _bias_factor_cached must still return
    in well under a second. Before the fix this call inlined the walker and
    would have taken >= the sleep below; proving that decisively (rather
    than relying on this machine's real claude-walker.exe happening to be
    fast, which would pass even against the old, buggy code) is the point of
    this check specifically -- see also
    scripts/verify_cold_start.py's check_bias_factor_cold_cache_stays_fast
    for the real-subprocess end-to-end version of this same scenario."""
    cache_path = os.path.join(tmpdir, "bias-cache-slow-walker.json")
    _beacon_mod._BIAS_CACHE_PATH = cache_path

    def slow_walker(*_a, **_kw):
        time.sleep(1.5)
        return {"n_pairs": 25, "bias_factor": 1.4}

    _beacon_mod._walker_subcommand = slow_walker
    spawn = _SpawnRecorder()
    original_spawn = _beacon_mod.maybe_spawn_refresh
    _beacon_mod.maybe_spawn_refresh = spawn
    try:
        started = time.monotonic()
        n, bias = _beacon_mod._bias_factor_cached(604800)
        elapsed = time.monotonic() - started
    finally:
        _beacon_mod.maybe_spawn_refresh = original_spawn
    if (n, bias) != (0, None):
        failures.append(
            f"cold cache with a slow walker must still serve neutral (0, None)"
            f" immediately, not wait on it; got ({n!r}, {bias!r})"
        )
    if elapsed >= 1.0:
        failures.append(
            f"_bias_factor_cached took {elapsed:.2f}s despite a cold cache --"
            f" it must never wait on the walker inline (regression to the"
            f" 2026-07-26 bug); the 1.5s sleep in the fake walker should"
            f" never be observed by the caller"
        )
    if spawn.calls != [("bias-factor", 604800)]:
        failures.append(
            f"the slow walker must be handed to a detached refresh, not"
            f" called synchronously; spawn calls: {spawn.calls!r}"
        )


def _check_refresh_bias_factor_cache_unwritable_path(failures, tmpdir):
    """An unwritable cache path must not raise out of the refresher (same
    best-effort contract as every other refresher's cache write)."""
    _beacon_mod._BIAS_CACHE_PATH = os.path.join(tmpdir, "no_such_dir", "cache.json")
    _beacon_mod._walker_subcommand = lambda *_a, **_kw: {
        "n_pairs": 15,
        "bias_factor": 1.2,
    }
    n, bias = _beacon_mod.refresh_bias_factor_cache(604800)
    if n != 15 or abs(bias - 1.2) > 0.001:
        failures.append(
            f"refresh_bias_factor_cache with unwritable cache must still return"
            f" values; got ({n!r},{bias!r})"
        )


def _check_format_beacon_bad_eta_seconds(failures):
    """A string eta_seconds (malformed transcript data) must degrade
    gracefully, not raise. _apply_beacon already float()-coerces the same
    field defensively; format_beacon must match."""
    original_cached = _beacon_mod._beacons_latest_cached
    original_anchors = _beacon_mod._find_beacon_anchors
    try:
        _beacon_mod._beacons_latest_cached = lambda session_id: {
            "beacon": {
                "kind": "report",
                "eta_seconds": "not-a-number",
                "summary": "working",
            },
            "age_seconds": 10,
        }
        _beacon_mod._find_beacon_anchors = lambda _sid: (None, None, None)
        try:
            rendered, _beacon_out = format_beacon("some-session")
        except TypeError as exc:
            failures.append(f"format_beacon must not raise on bad eta_seconds: {exc}")
            return
        if rendered is None:
            failures.append("format_beacon with bad eta_seconds must still render")
        elif "~1m" not in _strip(rendered):
            failures.append(
                f"format_beacon with bad eta_seconds should degrade to ~1m, "
                f"got {rendered!r}"
            )
    finally:
        _beacon_mod._beacons_latest_cached = original_cached
        _beacon_mod._find_beacon_anchors = original_anchors


def _check_bias_cache_alternating_periods(failures, tmpdir):
    """Two periods interleaved must each keep their own cache entry, not
    thrash each other -- a single-entry (non-keyed) cache would have a fresh
    write for period B evict period A's still-fresh entry outright, forcing
    a respawn on every alternating call even though each period's own entry
    was well within TTL. Neither period is stale here, so no spawn at all is
    expected."""
    cache_path = os.path.join(tmpdir, "bias-cache-alternating.json")
    _beacon_mod._BIAS_CACHE_PATH = cache_path
    period_a, period_b = 604800, 300
    now = datetime.now(UTC).timestamp()
    seeded = {
        str(period_a): {
            "computed_at_unix": now - 1,
            "period_seconds": period_a,
            "n_pairs": 30,
            "bias_factor": 1.0,
        },
        str(period_b): {
            "computed_at_unix": now - 1,
            "period_seconds": period_b,
            "n_pairs": 30,
            "bias_factor": 2.0,
        },
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(seeded, f)

    spawn = _SpawnRecorder()
    original_spawn = _beacon_mod.maybe_spawn_refresh
    _beacon_mod.maybe_spawn_refresh = spawn
    try:
        n_a1, bias_a1 = _beacon_mod._bias_factor_cached(period_a)
        n_b1, bias_b1 = _beacon_mod._bias_factor_cached(period_b)
        # Re-querying A right after B must hit A's own cached entry, not
        # recompute/re-spawn just because B was queried in between.
        n_a2, bias_a2 = _beacon_mod._bias_factor_cached(period_a)
        n_b2, bias_b2 = _beacon_mod._bias_factor_cached(period_b)
    finally:
        _beacon_mod.maybe_spawn_refresh = original_spawn

    if spawn.calls:
        failures.append(
            f"alternating fresh periods must never spawn a refresh; got {spawn.calls!r}"
        )
    if (n_a1, bias_a1) != (30, 1.0) or (n_a2, bias_a2) != (30, 1.0):
        failures.append(
            f"period A's cached value must be stable; got ({n_a1!r},{bias_a1!r})"
            f" then ({n_a2!r},{bias_a2!r})"
        )
    if (n_b1, bias_b1) != (30, 2.0) or (n_b2, bias_b2) != (30, 2.0):
        failures.append(
            f"period B's cached value must be stable; got ({n_b1!r},{bias_b1!r})"
            f" then ({n_b2!r},{bias_b2!r})"
        )


def _check_bias_cache_stale_period_spawns_only_its_own_key(failures, tmpdir):
    """A stale period A alongside a still-fresh period B must spawn a refresh
    keyed to A specifically, and must not disturb or re-trigger a spawn for
    B's entry."""
    cache_path = os.path.join(tmpdir, "bias-cache-mixed-freshness.json")
    _beacon_mod._BIAS_CACHE_PATH = cache_path
    period_a, period_b = 604800, 300
    now = datetime.now(UTC).timestamp()
    seeded = {
        str(period_a): {
            "computed_at_unix": now - _beacon_mod._BIAS_CACHE_TTL_SECONDS - 1,
            "period_seconds": period_a,
            "n_pairs": 5,
            "bias_factor": 0.5,
        },
        str(period_b): {
            "computed_at_unix": now - 1,
            "period_seconds": period_b,
            "n_pairs": 30,
            "bias_factor": 2.0,
        },
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(seeded, f)

    spawn = _SpawnRecorder()
    original_spawn = _beacon_mod.maybe_spawn_refresh
    _beacon_mod.maybe_spawn_refresh = spawn
    try:
        n_a, bias_a = _beacon_mod._bias_factor_cached(period_a)
        n_b, bias_b = _beacon_mod._bias_factor_cached(period_b)
    finally:
        _beacon_mod.maybe_spawn_refresh = original_spawn

    if (n_a, bias_a) != (5, 0.5):
        failures.append(
            f"stale period A must still be served stale; got ({n_a!r},{bias_a!r})"
        )
    if (n_b, bias_b) != (30, 2.0):
        failures.append(f"fresh period B must be unaffected; got ({n_b!r},{bias_b!r})")
    if spawn.calls != [("bias-factor", period_a)]:
        failures.append(
            f"only the stale period must spawn, keyed to itself; got {spawn.calls!r}"
        )


def _check_bias_factor_cached(failures):
    original_walker = _beacon_mod._walker_subcommand
    original_cache_path = _beacon_mod._BIAS_CACHE_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            _check_bias_cache_fresh_hit_skips_spawn(failures, tmpdir)
            _check_bias_cache_stale_serves_and_spawns(failures, tmpdir)
            _check_bias_cache_miss_and_wrong_period_serve_neutral_and_spawn(
                failures, tmpdir
            )
            _check_refresh_bias_factor_cache_writes(failures, tmpdir)
            _check_refresh_bias_factor_cache_unwritable_path(failures, tmpdir)
            _check_bias_cache_alternating_periods(failures, tmpdir)
            _check_bias_cache_stale_period_spawns_only_its_own_key(failures, tmpdir)
            _check_bias_cache_never_waits_on_slow_walker(failures, tmpdir)
        finally:
            _beacon_mod._walker_subcommand = original_walker
            _beacon_mod._BIAS_CACHE_PATH = original_cache_path


def _check_format_calibrated_eta(failures):
    original_bias = _beacon_mod._bias_factor_cached

    try:
        if format_calibrated_eta(None) is not None:
            failures.append("format_calibrated_eta(None) must return None")
        if format_calibrated_eta(0) is not None:
            failures.append("format_calibrated_eta(0) must return None")
        if format_calibrated_eta(-5) is not None:
            failures.append("format_calibrated_eta(-5) must return None")

        _beacon_mod._bias_factor_cached = lambda period: (5, 1.4)
        if format_calibrated_eta(300) is not None:
            failures.append(
                "format_calibrated_eta with n_pairs=5 must return None (< 20)"
            )

        _beacon_mod._bias_factor_cached = lambda period: (25, None)
        if format_calibrated_eta(300) is not None:
            failures.append("format_calibrated_eta with bias=None must return None")

        _beacon_mod._bias_factor_cached = lambda period: (25, 1.4)
        result = format_calibrated_eta(300)
        if result is None:
            failures.append("format_calibrated_eta valid must not return None")
        elif "7m calibrated" not in result:
            failures.append(
                f"format_calibrated_eta: expected '7m calibrated', got {result!r}"
            )
        elif "1.4" not in result:
            failures.append(
                f"format_calibrated_eta: expected bias factor in output, got {result!r}"
            )

        _beacon_mod._bias_factor_cached = lambda period: (20, 2.0)
        result = format_calibrated_eta(3600)
        if result is None or "120m calibrated" not in result:
            failures.append(
                f"format_calibrated_eta large: expected '120m calibrated', got {result!r}"
            )

    finally:
        _beacon_mod._bias_factor_cached = original_bias


def _check_bias_history_walk_is_local_only(failures):
    """The beacons-history walk -- now only reachable via
    refresh_bias_factor_cache, the detached child's entry point; the render
    path itself (_bias_factor_cached) never calls the walker at all -- must
    pass --no-config so it never touches the SMB extra roots from
    walker-roots.json: measured 8-38s over the network mount vs 0.5s local,
    against a 5s subprocess timeout -- every cache miss stalled a render for
    the full timeout and then failed. Bias calibration is local-machine
    semantics, so local-only is also more correct."""
    captured = []

    def fake_walker(*args, **kw):
        captured.append(args)
        return {"n_pairs": 3, "bias_factor": 1.5}

    original_walker = _beacon_mod._walker_subcommand
    original_path = _beacon_mod._BIAS_CACHE_PATH
    _beacon_mod._walker_subcommand = fake_walker
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _beacon_mod._BIAS_CACHE_PATH = os.path.join(tmp, "bias.json")
            _beacon_mod.refresh_bias_factor_cache(604800)
    finally:
        _beacon_mod._walker_subcommand = original_walker
        _beacon_mod._BIAS_CACHE_PATH = original_path

    if not captured:
        failures.append("bias refresh should invoke the walker")
    elif "--no-config" not in captured[0]:
        failures.append(
            f"beacons-history must pass --no-config (local roots only); got {captured[0]!r}"
        )


class _SpawnRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, kind, argument):
        self.calls.append((kind, argument))
        return True


def _check_beacons_latest_walk_is_local_only(failures):
    """refresh_beacon_latest_cache (the detached child's entry point) must
    pass --no-config for the same reason the bias walk does: the session
    transcript it looks up always lives on THIS machine, and the SMB extra
    roots measured 170-190ms per render vs ~55ms local-only -- paid on EVERY
    render, uncached (found via profile: 0.5s of a 0.63s warm render was
    this one call).

    Render-perf ratchet step 3 (PLAN.md) moved the walker call off the
    render path entirely (_beacons_latest_cached never invokes it -- see
    _check_beacons_latest_cache_mechanics), so this now exercises the
    refresher directly rather than format_beacon end to end.

    refresh_beacon_latest_cache doesn't take a state_dir seam (the cache dir
    is an implementation detail it never needs), so isolation here goes
    through the CLAUDE_STATE_DIR env var that base.state_dir() honors -- the
    same mechanism scripts/verify_render_timer.py's subprocess end-to-end
    checks use, just via env var instead of a spawned process.
    """
    captured = []

    def fake_walker(*args, **kw):
        captured.append(args)
        return

    original_walker = _beacon_cache_mod._walker_subcommand
    original_env = os.environ.get("CLAUDE_STATE_DIR")
    _beacon_cache_mod._walker_subcommand = fake_walker
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CLAUDE_STATE_DIR"] = tmp
            _beacon_cache_mod.refresh_beacon_latest_cache("some-session-id")
    finally:
        _beacon_cache_mod._walker_subcommand = original_walker
        if original_env is None:
            os.environ.pop("CLAUDE_STATE_DIR", None)
        else:
            os.environ["CLAUDE_STATE_DIR"] = original_env

    if not captured:
        failures.append("refresh_beacon_latest_cache should invoke the walker")
    elif "--no-config" not in captured[0]:
        failures.append(
            f"beacons-latest must pass --no-config (local roots only); got {captured[0]!r}"
        )


def _check_beacons_latest_cache_hit_skips_spawn(failures, tmpdir):
    """Render-perf ratchet step 2+3 (PLAN.md): beacons-latest costs
    ~15-60ms/render, uncached. A cache hit within the TTL must not touch the
    walker OR spawn a refresh, and must return the cached payload verbatim
    (including a now-stale age_seconds -- acceptable, since the staleness
    threshold that matters is beacon._BEACON_STALE_SECONDS, two orders of
    magnitude looser). The cache is pre-seeded directly (fresh timestamp):
    _beacons_latest_cached never computes inline, so a first-ever read for a
    session with no prior entry is itself a miss, not a hit."""
    fresh_data = {"beacon": {"kind": "report", "eta_seconds": 90, "summary": "first"}}
    cache_path = _beacon_cache_mod._beacon_latest_cache_path(
        "cache-hit-session", state_dir=tmpdir
    )
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(
            {"cached_at_unix": datetime.now(UTC).timestamp(), "data": fresh_data}, f
        )

    calls = []
    _beacon_cache_mod._walker_subcommand = lambda *args, **kw: (
        calls.append(1) or {"beacon": {"kind": "report"}, "age_seconds": 999}
    )
    spawn = _SpawnRecorder()
    original_spawn = _beacon_cache_mod.maybe_spawn_refresh
    _beacon_cache_mod.maybe_spawn_refresh = spawn
    try:
        data = _beacon_cache_mod._beacons_latest_cached(
            "cache-hit-session", state_dir=tmpdir
        )
    finally:
        _beacon_cache_mod.maybe_spawn_refresh = original_spawn
    if calls:
        failures.append(
            f"a fresh cache hit must not call the walker; got {len(calls)} calls"
        )
    if spawn.calls:
        failures.append(
            f"a fresh cache hit must not spawn a refresh; got {spawn.calls!r}"
        )
    if data != fresh_data:
        failures.append(
            f"a cache hit must return the cached payload verbatim; got {data!r}"
        )


def _check_beacons_latest_cache_expiry_serves_stale_and_spawns(failures, tmpdir):
    cache_path = _beacon_cache_mod._beacon_latest_cache_path(
        "expiring-session", state_dir=tmpdir
    )
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    stale_ts = (
        datetime.now(UTC).timestamp()
        - _beacon_cache_mod._BEACON_LATEST_CACHE_TTL_SECONDS
        - 1
    )
    stale_data = {"beacon": {"kind": "report", "summary": "stale-but-served"}}
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"cached_at_unix": stale_ts, "data": stale_data}, f)

    spawn = _SpawnRecorder()
    original_spawn = _beacon_cache_mod.maybe_spawn_refresh
    _beacon_cache_mod.maybe_spawn_refresh = spawn
    try:
        data = _beacon_cache_mod._beacons_latest_cached(
            "expiring-session", state_dir=tmpdir
        )
    finally:
        _beacon_cache_mod.maybe_spawn_refresh = original_spawn
    if data != stale_data:
        failures.append(f"an expired entry must still be served stale; got {data!r}")
    if spawn.calls != [("beacon-latest", "expiring-session")]:
        failures.append(f"an expired entry must spawn a refresh; got {spawn.calls!r}")


def _check_beacons_latest_cache_corrupt_file_degrades(failures, tmpdir):
    cache_path = _beacon_cache_mod._beacon_latest_cache_path(
        "corrupt-session", state_dir=tmpdir
    )
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write("not-json")

    spawn = _SpawnRecorder()
    original_spawn = _beacon_cache_mod.maybe_spawn_refresh
    _beacon_cache_mod.maybe_spawn_refresh = spawn
    try:
        data = _beacon_cache_mod._beacons_latest_cached(
            "corrupt-session", state_dir=tmpdir
        )
    finally:
        _beacon_cache_mod.maybe_spawn_refresh = original_spawn
    if data is not None:
        failures.append(
            f"a corrupt cache file must degrade to None (hidden column), not crash;"
            f" got {data!r}"
        )
    if spawn.calls != [("beacon-latest", "corrupt-session")]:
        failures.append(
            f"a corrupt cache file must spawn a refresh; got {spawn.calls!r}"
        )


def _check_refresh_beacon_latest_cache_writes(failures, tmpdir):
    """refresh_beacon_latest_cache (the detached child's entry point) runs
    the walker and persists the result where the render's cached read can
    serve it. It resolves state_dir internally (matches gitref's
    refresher), so isolation goes through CLAUDE_STATE_DIR."""
    _beacon_cache_mod._walker_subcommand = lambda *args, **kw: {
        "beacon": {"kind": "report", "summary": "recovered"}
    }
    original_env = os.environ.get("CLAUDE_STATE_DIR")
    os.environ["CLAUDE_STATE_DIR"] = tmpdir
    try:
        data = _beacon_cache_mod.refresh_beacon_latest_cache("refreshed-session")
    finally:
        if original_env is None:
            os.environ.pop("CLAUDE_STATE_DIR", None)
        else:
            os.environ["CLAUDE_STATE_DIR"] = original_env
    beacon = (data or {}).get("beacon") or {}
    if beacon.get("summary") != "recovered":
        failures.append(
            f"refresh_beacon_latest_cache must return the fresh data; got {data!r}"
        )


def _check_beacons_latest_cache_distinct_sessions(failures, tmpdir):
    """Two session ids rendering concurrently must not clobber each other's
    cache entry (each gets its own per-session file)."""
    path_a = _beacon_cache_mod._beacon_latest_cache_path("session-a", state_dir=tmpdir)
    path_b = _beacon_cache_mod._beacon_latest_cache_path("session-b", state_dir=tmpdir)
    if path_a == path_b:
        failures.append(
            "distinct session ids must map to distinct cache files (concurrent-safe keying)"
        )


def _check_beacons_latest_cache_mechanics(failures):
    original_walker = _beacon_cache_mod._walker_subcommand
    try:
        with tempfile.TemporaryDirectory() as base:
            with tempfile.TemporaryDirectory(dir=base) as tmpdir:
                _check_beacons_latest_cache_hit_skips_spawn(failures, tmpdir)
            with tempfile.TemporaryDirectory(dir=base) as tmpdir:
                _check_beacons_latest_cache_expiry_serves_stale_and_spawns(
                    failures, tmpdir
                )
            with tempfile.TemporaryDirectory(dir=base) as tmpdir:
                _check_beacons_latest_cache_corrupt_file_degrades(failures, tmpdir)
            with tempfile.TemporaryDirectory(dir=base) as tmpdir:
                _check_refresh_beacon_latest_cache_writes(failures, tmpdir)
            with tempfile.TemporaryDirectory(dir=base) as tmpdir:
                _check_beacons_latest_cache_distinct_sessions(failures, tmpdir)
    finally:
        _beacon_cache_mod._walker_subcommand = original_walker


def main():
    failures = []
    _check_beacons_latest_walk_is_local_only(failures)
    _check_bias_history_walk_is_local_only(failures)
    _check_format_beacon(failures)
    _check_format_beacon_bad_eta_seconds(failures)
    _check_beacons_latest_cache_mechanics(failures)
    _check_bias_factor_cached(failures)
    _check_format_calibrated_eta(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: beacon walker-dependent paths all verified")


if __name__ == "__main__":
    main()
