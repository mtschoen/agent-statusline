"""Verify the pure pace-projection core: estimators, calibration, day-1 prior.

`project_delta` returns two deltas in seconds relative to reset (positive =
surplus / lands after reset; negative = exhausts before reset). The current-rate
delta uses the window's own util/$ ratio to turn recent $/h into %/h, and both
deltas are shrunk toward 0 early in the window by the warmup prior. These checks
feed synthetic hourly arrays so no transcripts or clock are involved.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib.project import (
    DEFAULT_PARAMS,
    current_rate,
    project_delta,
)

_HOUR = 3600.0


def _params(**overrides):
    p = dict(DEFAULT_PARAMS)
    p.update(overrides)
    return p


def _check_estimators_pick_recent(failures):
    """A burn that ramps up reads hotter under every estimator than its flat mean."""
    ramp = [1.0, 1.0, 1.0, 10.0, 10.0, 10.0]
    flat_mean = sum(ramp) / len(ramp)
    for est in ("trailing_hours", "ewma", "recent_slope"):
        rate = current_rate(
            ramp, _params(estimator=est, window_hours=3, **{"lambda": 0.5})
        )
        if rate <= flat_mean:
            failures.append(
                f"{est}: ramp-up rate {rate:.2f} should exceed flat mean {flat_mean:.2f}"
            )


def _check_flat_burn_is_on_pace(failures):
    """Flat burn that has used `util`% over exactly half the window projects to
    ~100% at reset: cumulative delta ~= 0 (after full warmup)."""
    period = 7 * 24 * _HOUR
    elapsed = period / 2
    hourly = [1.0] * int(elapsed // _HOUR)
    cum, _ = project_delta(
        hourly, 50.0, elapsed, period - elapsed, period, _params(warmup_seconds=1)
    )
    if abs(cum) > 0.02 * period:
        failures.append(
            f"flat 50%-at-half-window should land ~on-pace, got {cum / _HOUR:.1f}h"
        )


def _check_warmup_shrinks_to_zero(failures):
    """At elapsed << warmup, both deltas are pulled toward 0 regardless of raw rate."""
    period = 7 * 24 * _HOUR
    elapsed = 2 * _HOUR
    hourly = [50.0, 50.0]  # very hot start
    p = _params(warmup_seconds=48 * _HOUR)
    cum, cur = project_delta(hourly, 30.0, elapsed, period - elapsed, period, p)
    cum_raw, cur_raw = project_delta(
        hourly, 30.0, elapsed, period - elapsed, period, _params(warmup_seconds=1)
    )
    if not (abs(cum) < abs(cum_raw)):
        failures.append(
            "warmup should shrink the cumulative delta toward 0 early in window"
        )
    if cur is not None and cur_raw is not None and not (abs(cur) < abs(cur_raw)):
        failures.append(
            "warmup should shrink the current-rate delta toward 0 early in window"
        )


def _check_degenerate_window(failures):
    """No $ in window => current-rate delta is None (arrow omitted), cumulative still computed."""
    period = 7 * 24 * _HOUR
    elapsed = period / 2
    cum, cur = project_delta(
        [], 50.0, elapsed, period - elapsed, period, _params(warmup_seconds=1)
    )
    if cum is None:
        failures.append("cumulative delta should compute even with empty hourly burn")
    if cur is not None:
        failures.append("current-rate delta should be None when window has no dollars")


def _check_bad_util(failures):
    """util <= 0 or non-positive elapsed/remaining => (None, None)."""
    period = 7 * 24 * _HOUR
    for util, el, rem in (
        (0.0, period / 2, period / 2),
        (50.0, 0.0, period),
        (50.0, period, 0.0),
    ):
        cum, cur = project_delta([1.0], util, el, rem, period, DEFAULT_PARAMS)
        if cum is not None or cur is not None:
            failures.append(
                f"degenerate inputs util={util} el={el} rem={rem} should give (None, None)"
            )


def _check_partial_params(failures):
    """A sparse params dict is merged over DEFAULT_PARAMS (no KeyError)."""
    try:
        rate = current_rate([1.0, 2.0, 3.0], {"estimator": "trailing_hours"})
    except KeyError as exc:
        failures.append(
            f"current_rate must merge partial params over defaults, got KeyError {exc}"
        )
        return
    if rate <= 0:
        failures.append(
            f"current_rate with partial params should still compute, got {rate}"
        )
    period = 7 * 24 * _HOUR
    try:
        cum, _cur = project_delta(
            [1.0] * 84, 50.0, period / 2, period / 2, period, {"estimator": "ewma"}
        )
    except KeyError as exc:
        failures.append(
            f"project_delta must merge partial params over defaults, got KeyError {exc}"
        )
        return
    if cum is None:
        failures.append(
            "project_delta with partial params should compute a cumulative delta"
        )


def check(failures):
    _check_estimators_pick_recent(failures)
    _check_flat_burn_is_on_pace(failures)
    _check_warmup_shrinks_to_zero(failures)
    _check_degenerate_window(failures)
    _check_bad_util(failures)
    _check_partial_params(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: projection core — estimators favor recent burn, prior shrinks early, calibration + edges hold"
    )


if __name__ == "__main__":
    main()
