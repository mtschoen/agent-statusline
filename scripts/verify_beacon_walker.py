"""Verify beacon.py walker-dependent paths: format_beacon,
_bias_factor_cached, and format_calibrated_eta.

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

    _beacon_mod._walker_subcommand = lambda *args, **kw: None
    rendered, beacon = format_beacon("some-session")
    if rendered is not None or beacon is not None:
        failures.append(
            f"format_beacon with no walker data must be (None,None), got ({rendered!r},{beacon!r})"
        )

    _beacon_mod._walker_subcommand = lambda *args, **kw: {
        "beacon": None,
        "age_seconds": 10,
    }
    rendered, beacon = format_beacon("some-session")
    if rendered is not None or beacon is not None:
        failures.append(
            f"format_beacon with None beacon must be (None,None), got ({rendered!r},{beacon!r})"
        )

    _beacon_mod._walker_subcommand = lambda *args, **kw: {
        "beacon": {"kind": "end"},
        "age_seconds": 10,
    }
    rendered, beacon = format_beacon("some-session")
    if rendered is not None or beacon is not None:
        failures.append(
            f"format_beacon with kind=end must be (None,None), got ({rendered!r},{beacon!r})"
        )

    _beacon_mod._walker_subcommand = lambda *args, **kw: {
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
    original_walker = _beacon_mod._walker_subcommand
    original_anchors = _beacon_mod._find_beacon_anchors

    try:
        _check_format_beacon_hidden(failures)

        recent_begin = (datetime.now(UTC) - timedelta(minutes=3)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        recent_step = (datetime.now(UTC) - timedelta(minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        _beacon_mod._walker_subcommand = lambda *args, **kw: {
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
        _beacon_mod._walker_subcommand = original_walker
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


def _check_bias_factor_cached(failures):
    original_walker = _beacon_mod._walker_subcommand
    original_cache_path = _beacon_mod._BIAS_CACHE_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            _check_bias_cache_read(failures, tmpdir)
            _check_bias_cache_invalidation(failures, tmpdir)
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


def main():
    failures = []
    _check_format_beacon(failures)
    _check_bias_factor_cached(failures)
    _check_format_calibrated_eta(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: beacon walker-dependent paths all verified")


if __name__ == "__main__":
    main()
