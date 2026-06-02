"""Verify the quota render split: format_quota is number-only; the current-rate
arrow and on-target glyph now come from pace.weekly_needle (relocated to the
burn-rate field). Pins the clock + hourly walk, mirroring the burn-rate verifier.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.pace as pace
from statusline_lib.base import RESET, ramp_color
from statusline_lib.pace import (
    ARROW_DOWN,
    ARROW_UP,
    ON_TARGET_GLYPH,
    format_quota,
    weekly_needle,
)

_HOUR = 3600.0
_WIN_START = 1_700_000_000.0


def _pin(hourly, elapsed_hours, period_days=7):
    period = period_days * 24 * _HOUR
    now = _WIN_START + elapsed_hours * _HOUR
    real_now, real_hourly = pace._now_unix, pace._pace_hourly_cached
    pace._now_unix = lambda: now
    pace._pace_hourly_cached = lambda _ws: hourly
    return period, real_now, real_hourly


def _render_quota(util, hourly, elapsed_hours):
    period, real_now, real_hourly = _pin(hourly, elapsed_hours)
    rl = {"seven_day": {"used_percentage": util, "resets_at": _WIN_START + period}}
    try:
        return format_quota(rl)
    finally:
        pace._now_unix, pace._pace_hourly_cached = real_now, real_hourly


def _render_needle(util, hourly, elapsed_hours):
    period, real_now, real_hourly = _pin(hourly, elapsed_hours)
    rl = {"seven_day": {"used_percentage": util, "resets_at": _WIN_START + period}}
    try:
        return weekly_needle(rl)
    finally:
        pace._now_unix, pace._pace_hourly_cached = real_now, real_hourly


def _check_quota_is_number_only(failures):
    out = _render_quota(30.0, [0.1] * 70 + [50.0] * 14, 84)
    if ARROW_UP in out or ARROW_DOWN in out or ON_TARGET_GLYPH in out:
        failures.append(f"format_quota must be number-only now; got {out!r}")
    if "wk:" not in out:
        failures.append(f"format_quota should still render wk:; got {out!r}")


def _check_quota_number_color_bands(failures):
    # The last space-separated token is the pace delta (_fmt_delta), now a gradient.
    green = _render_quota(30.0, [], 84).rsplit(" ", 1)[-1]
    if not green.startswith(ramp_color(0.0)):
        failures.append(
            f"large surplus delta should be green-end gradient; got {green!r}"
        )
    red = _render_quota(60.0, [], 84).rsplit(" ", 1)[-1]
    if not red.startswith(ramp_color(1.0)):
        failures.append(f"deficit delta should be red-end gradient; got {red!r}")
    if RESET not in green:
        failures.append("colored output must reset")


def _check_needle_hot_up(failures):
    out = _render_needle(30.0, [0.1] * 70 + [50.0] * 14, 84)
    if ARROW_UP not in out:
        failures.append(f"hot recent rate should yield an up arrow; got {out!r}")


def _check_needle_cool_down(failures):
    out = _render_needle(60.0, [50.0] * 14 + [0.1] * 70, 84)
    if ARROW_DOWN not in out:
        failures.append(f"cooling recent rate should yield a down arrow; got {out!r}")


def _check_needle_on_target(failures):
    out = _render_needle(50.0, [1.0] * 84, 84)
    if ON_TARGET_GLYPH not in out:
        failures.append(
            f"both deltas ~0 + warmup done should show the glyph; got {out!r}"
        )
    if ARROW_UP in out or ARROW_DOWN in out:
        failures.append(f"on-target should replace the arrow; got {out!r}")


def _check_needle_empty_window(failures):
    out = _render_needle(40.0, [], 84)
    if ARROW_UP in out or ARROW_DOWN in out:
        failures.append(f"empty window should omit the arrow; got {out!r}")


def _render_quota_with_pace(util, hourly, elapsed_hours, show_pace=True):
    period, real_now, real_hourly = _pin(hourly, elapsed_hours)
    rl = {"seven_day": {"used_percentage": util, "resets_at": _WIN_START + period}}
    try:
        return format_quota(rl, show_pace=show_pace)
    finally:
        pace._now_unix, pace._pace_hourly_cached = real_now, real_hourly


def _check_show_pace_toggle(failures):
    # With show_pace=True (default), the pace projection (+Hh or -Hh) appears.
    on = _render_quota_with_pace(30.0, [1.0] * 84, 84, show_pace=True)
    if "h" not in on:
        failures.append(f"show_pace=True should include an h pace token; got {on!r}")
    if "%" not in on:
        failures.append(f"show_pace=True should still show %; got {on!r}")
    # With show_pace=False, pace projection is omitted but % remains.
    off = _render_quota_with_pace(30.0, [1.0] * 84, 84, show_pace=False)
    if "h" in off:
        failures.append(f"show_pace=False must drop the h pace token; got {off!r}")
    if "%" not in off:
        failures.append(f"show_pace=False must keep the % utilization; got {off!r}")


def check(failures):
    _check_quota_is_number_only(failures)
    _check_quota_number_color_bands(failures)
    _check_needle_hot_up(failures)
    _check_needle_cool_down(failures)
    _check_needle_on_target(failures)
    _check_needle_empty_window(failures)
    _check_show_pace_toggle(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: format_quota is number-only; weekly_needle carries the arrow/glyph")


if __name__ == "__main__":
    main()
