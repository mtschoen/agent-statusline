"""Verify beacon.py walker-dependent paths (format_beacon, _bias_factor_cached,
format_calibrated_eta) and beacon_cache.py's TTL disk-cache for beacons-latest.

Patches _walker_subcommand and _find_beacon_anchors in-process so no real
walker binary is required.

Run from anywhere; imports from `schoen-claude-status` package by path.
"""

import json
import os
import re
import sys
import tempfile
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


def _check_bias_cache_read(failures, tmpdir):
    """Walker miss, first write, and fresh-cache hit."""
    cache_path = os.path.join(tmpdir, "bias-cache.json")
    _beacon_mod._BIAS_CACHE_PATH = cache_path

    _beacon_mod._walker_subcommand = lambda *args, **kw: None
    n, bias = _beacon_mod._bias_factor_cached(604800)
    if (n, bias) != (0, None):
        failures.append(
            f"_bias_factor_cached with no walker data must return (0,None), got ({n!r},{bias!r})"
        )

    _beacon_mod._walker_subcommand = lambda *args, **kw: {
        "n_pairs": 25,
        "bias_factor": 1.4,
    }
    # The miss above is negative-cached (failed=True, longer TTL): a healthy
    # walker must NOT be consulted while the failure entry is fresh.
    n, bias = _beacon_mod._bias_factor_cached(604800)
    if (n, bias) != (0, None):
        failures.append(f"walker failure must be negative-cached, got ({n!r},{bias!r})")
    os.remove(cache_path)
    n, bias = _beacon_mod._bias_factor_cached(604800)
    if n != 25 or abs(bias - 1.4) > 0.001:
        failures.append(
            f"_bias_factor_cached with walker data: expected (25,1.4), got ({n!r},{bias!r})"
        )
    if not os.path.exists(cache_path):
        failures.append("_bias_factor_cached must write cache file")

    _beacon_mod._walker_subcommand = lambda *args, **kw: {
        "n_pairs": 99,
        "bias_factor": 9.9,
    }
    n2, bias2 = _beacon_mod._bias_factor_cached(604800)
    if n2 != 25 or abs(bias2 - 1.4) > 0.001:
        failures.append(
            f"_bias_factor_cached must return cached value on second call; got ({n2!r},{bias2!r})"
        )


def _check_bias_cache_invalidation(failures, tmpdir):
    """Stale TTL, wrong period, corrupt JSON, and unwritable path all recompute."""
    cache_path = os.path.join(tmpdir, "bias-cache2.json")
    _beacon_mod._BIAS_CACHE_PATH = cache_path

    stale_data = {
        "computed_at_unix": datetime.now(UTC).timestamp() - 120,
        "period_seconds": 604800,
        "n_pairs": 5,
        "bias_factor": 0.5,
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(stale_data, f)
    _beacon_mod._walker_subcommand = lambda *args, **kw: {
        "n_pairs": 30,
        "bias_factor": 1.6,
    }
    n3, bias3 = _beacon_mod._bias_factor_cached(604800)
    if n3 != 30 or abs(bias3 - 1.6) > 0.001:
        failures.append(
            f"_bias_factor_cached with stale cache must recompute; got ({n3!r},{bias3!r})"
        )

    fresh_wrong_period = {
        "computed_at_unix": datetime.now(UTC).timestamp() - 1,
        "period_seconds": 999,
        "n_pairs": 7,
        "bias_factor": 0.7,
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(fresh_wrong_period, f)
    _beacon_mod._walker_subcommand = lambda *args, **kw: {
        "n_pairs": 40,
        "bias_factor": 1.8,
    }
    n4, bias4 = _beacon_mod._bias_factor_cached(604800)
    if n4 != 40 or abs(bias4 - 1.8) > 0.001:
        failures.append(
            f"_bias_factor_cached with wrong period must recompute; got ({n4!r},{bias4!r})"
        )

    with open(cache_path, "w", encoding="utf-8") as f:
        f.write("not-json")
    _beacon_mod._walker_subcommand = lambda *args, **kw: {
        "n_pairs": 22,
        "bias_factor": 1.1,
    }
    n5, bias5 = _beacon_mod._bias_factor_cached(604800)
    if n5 != 22 or abs(bias5 - 1.1) > 0.001:
        failures.append(
            f"_bias_factor_cached with corrupt cache must recompute; got ({n5!r},{bias5!r})"
        )

    _beacon_mod._BIAS_CACHE_PATH = os.path.join(tmpdir, "no_such_dir", "cache.json")
    _beacon_mod._walker_subcommand = lambda *args, **kw: {
        "n_pairs": 15,
        "bias_factor": 1.2,
    }
    n6, bias6 = _beacon_mod._bias_factor_cached(604800)
    if n6 != 15 or abs(bias6 - 1.2) > 0.001:
        failures.append(
            f"_bias_factor_cached with unwritable cache must still return values; got ({n6!r},{bias6!r})"
        )


def _check_bias_cache_valid_key_ttl_expiry(failures, tmpdir):
    """A VALIDLY-KEYED entry (new per-period cache shape) whose TTL has
    expired must still trigger a recompute, and a validly-keyed fresh entry
    must NOT.

    _check_bias_cache_invalidation's "stale cache" case seeds a flat,
    LEGACY-shaped file (no period key at all) -- under the new per-period
    format that's a key-miss (cache.get(key) is None), which also forces a
    recompute but never exercises the `age < ttl` comparison on an entry
    that actually matched its key. This closes that gap directly.
    """
    cache_path = os.path.join(tmpdir, "bias-cache-valid-key-ttl.json")
    _beacon_mod._BIAS_CACHE_PATH = cache_path
    period = 604800
    key = str(period)

    calls = []
    _beacon_mod._walker_subcommand = lambda *_a, **_kw: (
        calls.append(1) or {"n_pairs": 50, "bias_factor": 3.0}
    )

    # Expired: computed_at_unix is older than the (non-failure) TTL.
    expired = {
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
        json.dump(expired, f)
    n, bias = _beacon_mod._bias_factor_cached(period)
    if len(calls) != 1:
        failures.append(
            f"expired validly-keyed entry must recompute (1 walker call); "
            f"got {len(calls)} calls"
        )
    if (n, bias) != (50, 3.0):
        failures.append(
            f"expired validly-keyed entry must return the fresh walk result; "
            f"got ({n!r},{bias!r})"
        )

    # Fresh: computed_at_unix is well within TTL -- must be a cache hit, no
    # new walker call, and must return the OLD (still-cached) values.
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
    calls.clear()
    n2, bias2 = _beacon_mod._bias_factor_cached(period)
    if len(calls) != 0:
        failures.append(
            f"fresh validly-keyed entry must be a cache hit (0 walker calls); "
            f"got {len(calls)} calls"
        )
    if (n2, bias2) != (8, 0.8):
        failures.append(
            f"fresh validly-keyed entry must return the cached values; "
            f"got ({n2!r},{bias2!r})"
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
    """Two periods interleaved within TTL must each stay cached, not thrash.

    Before the cache was keyed by period, a fresh entry for period B would
    overwrite period A's fresh entry outright (a single-entry cache file), so
    alternating calls (A, B, A, B, ...) recomputed on every single call even
    though each period's own entry was still well within TTL.
    """
    cache_path = os.path.join(tmpdir, "bias-cache-alternating.json")
    _beacon_mod._BIAS_CACHE_PATH = cache_path

    calls = []

    def fake_walker(*_args, **kw):
        calls.append(kw.get("timeout"))
        period = _args[2] if len(_args) > 2 else None
        return {"n_pairs": 30, "bias_factor": 1.0 if period == "604800" else 2.0}

    _beacon_mod._walker_subcommand = fake_walker

    period_a, period_b = 604800, 300
    n_a1, bias_a1 = _beacon_mod._bias_factor_cached(period_a)
    n_b1, bias_b1 = _beacon_mod._bias_factor_cached(period_b)
    if len(calls) != 2:
        failures.append(
            f"alternating periods: both first calls should walk; got {len(calls)} calls"
        )
    # Re-querying period A right after B must hit A's own cached entry, not
    # recompute just because B's entry is now the most recent write.
    n_a2, bias_a2 = _beacon_mod._bias_factor_cached(period_a)
    if len(calls) != 2:
        failures.append(
            f"alternating periods: re-querying period A must be a cache hit "
            f"(no new walker call); got {len(calls)} total calls"
        )
    if (n_a2, bias_a2) != (n_a1, bias_a1):
        failures.append(
            f"alternating periods: period A's cached value must be stable; "
            f"got ({n_a1!r},{bias_a1!r}) then ({n_a2!r},{bias_a2!r})"
        )
    n_b2, bias_b2 = _beacon_mod._bias_factor_cached(period_b)
    if len(calls) != 2:
        failures.append(
            f"alternating periods: re-querying period B must also be a cache "
            f"hit; got {len(calls)} total calls"
        )
    if (n_b2, bias_b2) != (n_b1, bias_b1):
        failures.append(
            f"alternating periods: period B's cached value must be stable; "
            f"got ({n_b1!r},{bias_b1!r}) then ({n_b2!r},{bias_b2!r})"
        )


def _check_bias_factor_cached(failures):
    original_walker = _beacon_mod._walker_subcommand
    original_cache_path = _beacon_mod._BIAS_CACHE_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            _check_bias_cache_read(failures, tmpdir)
            _check_bias_cache_invalidation(failures, tmpdir)
            _check_bias_cache_valid_key_ttl_expiry(failures, tmpdir)
            _check_bias_cache_alternating_periods(failures, tmpdir)
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
    """The beacons-history walk must pass --no-config so it never touches the
    SMB extra roots from walker-roots.json: measured 8-38s over the network
    mount vs 0.5s local, against a 5s subprocess timeout -- every cache miss
    stalled a render for the full timeout and then failed. Bias calibration
    is local-machine semantics, so local-only is also more correct."""
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
            _beacon_mod._bias_factor_cached(604800)
    finally:
        _beacon_mod._walker_subcommand = original_walker
        _beacon_mod._BIAS_CACHE_PATH = original_path

    if not captured:
        failures.append("bias walk should invoke the walker on a cold cache")
    elif "--no-config" not in captured[0]:
        failures.append(
            f"beacons-history must pass --no-config (local roots only); got {captured[0]!r}"
        )


def _check_beacons_latest_walk_is_local_only(failures):
    """beacons-latest must pass --no-config for the same reason the bias walk
    does: the session transcript it looks up always lives on THIS machine, and
    the SMB extra roots measured 170-190ms per render vs ~55ms local-only --
    paid on EVERY render, uncached (found via profile: 0.5s of a 0.63s warm
    render was this one call). Exercises the REAL beacon_cache path (a cache
    miss) end to end through format_beacon, not a stubbed cache lookup.

    format_beacon doesn't take a state_dir seam (the cache dir is an
    implementation detail of beacon_cache.py it never needs), so isolation
    here goes through the CLAUDE_STATE_DIR env var that base.state_dir()
    honors -- the same mechanism scripts/verify_render_timer.py's subprocess
    end-to-end checks use, just via env var instead of a spawned process.
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
            _beacon_mod.format_beacon("some-session-id")
    finally:
        _beacon_cache_mod._walker_subcommand = original_walker
        if original_env is None:
            os.environ.pop("CLAUDE_STATE_DIR", None)
        else:
            os.environ["CLAUDE_STATE_DIR"] = original_env

    if not captured:
        failures.append("format_beacon should invoke the walker")
    elif "--no-config" not in captured[0]:
        failures.append(
            f"beacons-latest must pass --no-config (local roots only); got {captured[0]!r}"
        )


def _check_beacons_latest_cache_hit_skips_walker(failures, tmpdir):
    """Render-perf ratchet step 2 (PLAN.md): beacons-latest costs ~60ms/render
    local, uncached. A cache hit within the TTL must not touch the walker at
    all, and must return the previously-cached payload verbatim (including a
    now-stale age_seconds -- acceptable, since the staleness threshold that
    matters is beacon._BEACON_STALE_SECONDS, two orders of magnitude looser)."""
    _beacon_cache_mod._walker_subcommand = lambda *args, **kw: {
        "beacon": {"kind": "report", "eta_seconds": 90, "summary": "first"},
        "age_seconds": 1,
    }
    data1 = _beacon_cache_mod._beacons_latest_cached(
        "cache-hit-session", state_dir=tmpdir
    )

    calls = []
    _beacon_cache_mod._walker_subcommand = lambda *args, **kw: (
        calls.append(1) or {"beacon": {"kind": "report"}, "age_seconds": 999}
    )
    data2 = _beacon_cache_mod._beacons_latest_cached(
        "cache-hit-session", state_dir=tmpdir
    )
    if calls:
        failures.append(
            f"a fresh cache entry must not call the walker again; got {len(calls)} calls"
        )
    if data2 != data1:
        failures.append(
            f"a cache hit must return the previously-cached payload verbatim; "
            f"got {data2!r} vs {data1!r}"
        )


def _check_beacons_latest_cache_expiry_recomputes(failures, tmpdir):
    cache_path = _beacon_cache_mod._beacon_latest_cache_path(
        "expiring-session", state_dir=tmpdir
    )
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    stale_ts = (
        datetime.now(UTC).timestamp()
        - _beacon_cache_mod._BEACON_LATEST_CACHE_TTL_SECONDS
        - 1
    )
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(
            {"cached_at_unix": stale_ts, "data": {"beacon": {"kind": "report"}}}, f
        )

    calls = []
    _beacon_cache_mod._walker_subcommand = lambda *args, **kw: (
        calls.append(1) or {"beacon": {"kind": "report", "summary": "fresh"}}
    )
    data = _beacon_cache_mod._beacons_latest_cached(
        "expiring-session", state_dir=tmpdir
    )
    if len(calls) != 1:
        failures.append(
            f"an expired cache entry must recompute (1 walker call); got {len(calls)} calls"
        )
    beacon = data.get("beacon") or {}
    if beacon.get("summary") != "fresh":
        failures.append(
            f"expired-cache recompute must return the fresh data; got {data!r}"
        )


def _check_beacons_latest_cache_corrupt_file_recomputes(failures, tmpdir):
    cache_path = _beacon_cache_mod._beacon_latest_cache_path(
        "corrupt-session", state_dir=tmpdir
    )
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write("not-json")

    _beacon_cache_mod._walker_subcommand = lambda *args, **kw: {
        "beacon": {"kind": "report", "summary": "recovered"}
    }
    data = _beacon_cache_mod._beacons_latest_cached("corrupt-session", state_dir=tmpdir)
    beacon = data.get("beacon") or {}
    if beacon.get("summary") != "recovered":
        failures.append(
            f"a corrupt cache file must degrade to a fresh walker read; got {data!r}"
        )


def _check_beacons_latest_cache_unwritable_dir_still_returns_value(
    failures, cache_base
):
    """A cache-write failure must not break rendering -- the freshly-computed
    payload is still returned, just not persisted."""
    blocker_file = os.path.join(cache_base, "not-a-dir")
    with open(blocker_file, "w", encoding="utf-8") as f:
        f.write("x")
    # A path component that is a file makes os.makedirs fail with an OSError
    # subclass on every platform -- simulates an unwritable cache dir without
    # relying on chmod semantics that differ on Windows.
    unwritable_state_dir = os.path.join(blocker_file, "state")
    _beacon_cache_mod._walker_subcommand = lambda *args, **kw: {
        "beacon": {"kind": "report", "summary": "still works"}
    }
    data = _beacon_cache_mod._beacons_latest_cached(
        "unwritable-session", state_dir=unwritable_state_dir
    )
    beacon = data.get("beacon") or {}
    if beacon.get("summary") != "still works":
        failures.append(
            f"unwritable cache dir must still return the computed payload; got {data!r}"
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
                _check_beacons_latest_cache_hit_skips_walker(failures, tmpdir)
            with tempfile.TemporaryDirectory(dir=base) as tmpdir:
                _check_beacons_latest_cache_expiry_recomputes(failures, tmpdir)
            with tempfile.TemporaryDirectory(dir=base) as tmpdir:
                _check_beacons_latest_cache_corrupt_file_recomputes(failures, tmpdir)
            with tempfile.TemporaryDirectory(dir=base) as tmpdir:
                _check_beacons_latest_cache_unwritable_dir_still_returns_value(
                    failures, tmpdir
                )
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
