"""Verify the weekly quota render: pace number + colored current-rate arrow.

Drives `format_quota` with a synthetic rate_limits payload and a pinned clock +
pinned hourly walk, asserting: the cumulative-pace number is colored by its own
threshold; a hotter current rate yields a (worse) up arrow, a cooler one a down
arrow; the arrow is omitted when the window has no dollars; and STATUSLINE_VERBOSE_PACE
swaps the arrow for an explicit second number.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.pace as pace
from statusline_lib.base import GREEN, RED, RESET
from statusline_lib.pace import ARROW_DOWN, ARROW_UP, ON_TARGET_GLYPH, format_quota

_HOUR = 3600.0


def _rate_limits(util, win_start, period):
    return {
        "seven_day": {"used_percentage": util, "resets_at": win_start + period},
        # 5h omitted -> only the weekly part is rendered.
    }


def _render(util, hourly, elapsed_hours, period_days=7):
    period = period_days * 24 * _HOUR
    win_start = 1_700_000_000.0
    now = win_start + elapsed_hours * _HOUR
    real_now = pace._now_unix
    real_hourly = pace._pace_hourly_cached
    pace._now_unix = lambda: now
    pace._pace_hourly_cached = lambda _ws: hourly
    try:
        return format_quota(_rate_limits(util, win_start, period))
    finally:
        pace._now_unix = real_now
        pace._pace_hourly_cached = real_hourly


def _check_hot_rate_up_arrow(failures):
    # Half the week elapsed, only 30% used (cumulative surplus -> green), but the
    # recent hours are scorching -> current rate hotter than cumulative -> up arrow.
    elapsed_h = 84  # half of 168h
    hourly = [0.1] * 70 + [50.0] * 14  # cool early, blazing recent
    out = _render(30.0, hourly, elapsed_h)
    if ARROW_UP not in out:
        failures.append(f"hot recent rate should render an up arrow; got {out!r}")


def _check_cool_rate_down_arrow(failures):
    # Front-loaded: binged early, idle recently -> current rate cooler -> down arrow.
    elapsed_h = 84
    hourly = [50.0] * 14 + [0.1] * 70
    out = _render(60.0, hourly, elapsed_h)
    if ARROW_DOWN not in out:
        failures.append(f"cooling recent rate should render a down arrow; got {out!r}")


def _check_no_dollars_no_arrow(failures):
    out = _render(40.0, [], 84)
    if ARROW_UP in out or ARROW_DOWN in out:
        failures.append(f"empty window should omit the arrow; got {out!r}")


def _check_verbose_two_numbers(failures):
    os.environ["STATUSLINE_VERBOSE_PACE"] = "1"
    try:
        out = _render(30.0, [0.1] * 70 + [50.0] * 14, 84)
    finally:
        del os.environ["STATUSLINE_VERBOSE_PACE"]
    if "/" not in out:
        failures.append(
            f"verbose mode should show two slash-separated deltas; got {out!r}"
        )
    if ARROW_UP in out or ARROW_DOWN in out:
        failures.append(f"verbose mode should drop the arrow; got {out!r}")


def _pace_segment(out):
    """The pace portion of 'wk: <colored P%> <colored delta>'.

    The 'P%' badge is colored independently by color_high_bad, so we slice it
    off and assert only on the trailing pace number's color. With an empty
    hourly walk there is no arrow, so the segment holds just the number.
    """
    return out.rsplit(" ", 1)[-1]


def _check_number_color_bands(failures):
    """The cumulative-pace NUMBER is colored by its own threshold band.
    Empty hourly => no arrow, so the trailing segment is just the number."""
    from statusline_lib.base import YELLOW

    green = _pace_segment(_render(30.0, [], 84))  # large surplus
    if GREEN not in green or YELLOW in green or RED in green:
        failures.append(f"large surplus should be GREEN only; got {green!r}")
    yellow = _pace_segment(
        _render(49.0, [], 84)
    )  # small surplus (~+3.4h, inside 8.4h buffer)
    if YELLOW not in yellow or GREEN in yellow or RED in yellow:
        failures.append(f"small surplus should be YELLOW only; got {yellow!r}")
    red = _pace_segment(_render(60.0, [], 84))  # deficit (runs out before reset)
    if RED not in red:
        failures.append(f"deficit should be RED; got {red!r}")
    if RESET not in green:
        failures.append("colored output must reset")


def _check_on_target_glyph(failures):
    """Both signals within margin AND warmup done => green reward glyph, no arrow.
    Flat burn at util=50, 84h into a 7-day week: cumulative delta ~0 AND current-
    rate delta ~0 (flat burn at exactly half-used/half-elapsed lands on reset)."""
    out = _render(50.0, [1.0] * 84, 84)
    if ON_TARGET_GLYPH not in out:
        failures.append(
            f"on-target (both deltas ~0, warmup done) should show the reward glyph; got {out!r}"
        )
    if ARROW_UP in out or ARROW_DOWN in out:
        failures.append(f"on-target should replace the arrow, not show it; got {out!r}")


def _check_on_target_warmup_guard(failures):
    """Near-zero deltas DURING warmup must NOT earn the glyph (the zero is the
    prior shrinking deltas, not real on-pace burn). util chosen so cumulative ~0
    at 10h elapsed, but 10h < 18h warmup => glyph suppressed."""
    out = _render(100.0 * 10 / 168, [1.0] * 10, 10)
    if ON_TARGET_GLYPH in out:
        failures.append(
            f"glyph must be suppressed during warmup (elapsed<warmup); got {out!r}"
        )


def _check_off_target_no_glyph(failures):
    """'Both signals' rule: cumulative ~0 but a HOT recent rate (diverging) must
    NOT earn the glyph."""
    out = _render(50.0, [0.1] * 70 + [50.0] * 14, 84)  # cumulative ~0, current-rate hot
    if ON_TARGET_GLYPH in out:
        failures.append(
            f"diverging current rate should NOT earn the glyph; got {out!r}"
        )


def check(failures):
    _check_hot_rate_up_arrow(failures)
    _check_cool_rate_down_arrow(failures)
    _check_no_dollars_no_arrow(failures)
    _check_verbose_two_numbers(failures)
    _check_number_color_bands(failures)
    _check_on_target_glyph(failures)
    _check_on_target_warmup_guard(failures)
    _check_off_target_no_glyph(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: weekly render shows pace number + current-rate arrow (verbose swaps in 2nd number)"
    )


if __name__ == "__main__":
    main()
