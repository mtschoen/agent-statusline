"""Verify statusline_lib.agy's quota adapter: agy's `quota` payload block
(remaining_fraction/reset_time/reset_in_seconds per window) mapped into the
same '5h: P% +Hh wk: P% +Hh' form pace.format_quota renders for Claude's
rate_limits, plus the per-horizon window-selection rule.

Selection is per-HORIZON, not per-pair: the 5h slot independently picks the
more-utilized of {gemini-5h, 3p-5h}, and the wk slot independently picks the
more-utilized of {gemini-weekly, 3p-weekly}. A prior pair-vs-pair design (pick
one whole family by its worst window, render both of that family's windows)
could hide the single most-utilized window when it was paired with a
low-utilization sibling from the same family -- see
_check_format_agy_quota_never_hides_hottest_window for the reviewer's exact
reproduction of that bug, kept as a permanent regression test.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.pace as pace
from statusline_lib.agy import (
    _AGY_QUOTA_HORIZONS,
    _agy_most_constrained_window,
    _agy_window_metrics,
    format_agy_quota,
)
from statusline_lib.base import RESET, ramp_color

_HOUR = 3600.0
_NOW = 1_700_000_000.0


def _iso(unix_ts):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(unix_ts, tz=UTC).isoformat().replace("+00:00", "Z")


def _window(remaining_fraction, resets_in_seconds):
    return {
        "remaining_fraction": remaining_fraction,
        "reset_time": _iso(_NOW + resets_in_seconds),
        "reset_in_seconds": resets_in_seconds,
    }


def _pin(fn):
    real_now = pace._now_unix
    pace._now_unix = lambda: _NOW
    try:
        return fn()
    finally:
        pace._now_unix = real_now


def _check_window_metrics_basic(failures):
    util, resets_at = _agy_window_metrics(_window(0.7, 2 * _HOUR))
    if util is None or abs(util - 30.0) > 0.01:
        failures.append(
            f"_agy_window_metrics: 0.7 remaining should be 30% used, got {util!r}"
        )
    if resets_at is None or abs(resets_at - (_NOW + 2 * _HOUR)) > 1:
        failures.append(
            f"_agy_window_metrics: resets_at should match reset_time, got {resets_at!r}"
        )


def _check_window_metrics_clamps(failures):
    util, _ = _agy_window_metrics(_window(-0.5, _HOUR))
    if util != 100.0:
        failures.append(
            f"_agy_window_metrics: over-100% remaining_fraction should clamp to 100, got {util!r}"
        )
    util, _ = _agy_window_metrics(_window(1.5, _HOUR))
    if util != 0.0:
        failures.append(
            f"_agy_window_metrics: >1 remaining_fraction should clamp used to 0, got {util!r}"
        )


def _check_window_metrics_malformed(failures):
    for bad in (
        None,
        {},
        {"remaining_fraction": 0.5},
        {"reset_time": "not-a-real-date", "remaining_fraction": 0.5},
        "nope",
    ):
        util, resets_at = _agy_window_metrics(bad)
        if util is not None or resets_at is not None:
            failures.append(
                f"_agy_window_metrics: malformed window {bad!r} should yield (None, None), got {(util, resets_at)!r}"
            )


def _check_horizons_shape(failures):
    if _AGY_QUOTA_HORIZONS.get("5h") != ("gemini-5h", "3p-5h"):
        failures.append(
            f"_AGY_QUOTA_HORIZONS['5h'] should be ('gemini-5h', '3p-5h'), got {_AGY_QUOTA_HORIZONS.get('5h')!r}"
        )
    if _AGY_QUOTA_HORIZONS.get("wk") != ("gemini-weekly", "3p-weekly"):
        failures.append(
            f"_AGY_QUOTA_HORIZONS['wk'] should be ('gemini-weekly', '3p-weekly'), got {_AGY_QUOTA_HORIZONS.get('wk')!r}"
        )


def _check_most_constrained_window_picks_higher_util(failures):
    quota = {
        "gemini-5h": _window(0.9, _HOUR),  # 10% used
        "3p-5h": _window(0.2, _HOUR),  # 80% used -- more constrained
    }
    best = _agy_most_constrained_window(quota, ("gemini-5h", "3p-5h"))
    if best is None or abs(best[0] - 80.0) > 0.01:
        failures.append(
            f"_agy_most_constrained_window: should pick the higher-util candidate, got {best!r}"
        )


def _check_most_constrained_window_missing_keys(failures):
    if _agy_most_constrained_window({}, ("missing-a", "missing-b")) is not None:
        failures.append("_agy_most_constrained_window: missing keys should yield None")


def _check_most_constrained_window_one_side_missing(failures):
    quota = {"gemini-5h": _window(0.5, _HOUR)}
    best = _agy_most_constrained_window(quota, ("gemini-5h", "3p-5h"))
    if best is None or abs(best[0] - 50.0) > 0.01:
        failures.append(
            f"_agy_most_constrained_window: should use the only usable candidate, got {best!r}"
        )


def _check_format_agy_quota_empty(failures):
    if format_agy_quota(None) != "":
        failures.append("format_agy_quota: None quota should render ''")
    if format_agy_quota({}) != "":
        failures.append("format_agy_quota: empty quota dict should render ''")
    if format_agy_quota({"gemini-5h": {}}) != "":
        failures.append(
            "format_agy_quota: quota with only malformed windows should render ''"
        )


def _check_format_agy_quota_renders_both_windows(failures):
    def render():
        quota = {
            "gemini-5h": _window(0.7, 2 * _HOUR),  # 30% used, 2h into a 5h window
            "gemini-weekly": _window(0.5, 3 * 86400),  # 50% used, mid-week
        }
        return format_agy_quota(quota)

    out = _pin(render)
    if "5h:" not in out or "wk:" not in out:
        failures.append(
            f"format_agy_quota: should render both 5h: and wk:, got {out!r}"
        )
    if RESET not in out:
        failures.append("format_agy_quota: colored output must reset")


def _check_format_agy_quota_show_pace_toggle(failures):
    def render(show_pace):
        quota = {"gemini-5h": _window(0.7, 2 * _HOUR)}
        return format_agy_quota(quota, show_pace=show_pace)

    on = _pin(lambda: render(True))
    off = _pin(lambda: render(False))
    if "h" not in on.split("5h:")[-1]:
        failures.append(
            f"format_agy_quota: show_pace=True should include an h pace token, got {on!r}"
        )
    if "h" in off.split("5h:")[-1].split("%")[-1]:
        failures.append(
            f"format_agy_quota: show_pace=False should drop the pace token, got {off!r}"
        )


def _check_format_agy_quota_per_horizon_selection(failures):
    # The 5h slot and wk slot are chosen INDEPENDENTLY: gemini wins 5h (higher
    # util), but 3p wins wk (higher util) -- a genuine cross-family split.
    def render():
        quota = {
            "gemini-5h": _window(0.1, 2 * _HOUR),  # 90% used -- wins the 5h slot
            "gemini-weekly": _window(0.9, 3 * 86400),  # 10% used
            "3p-5h": _window(0.95, 2 * _HOUR),  # 5% used
            "3p-weekly": _window(0.4, 3 * 86400),  # 60% used -- wins the wk slot
        }
        return format_agy_quota(quota)

    out = _pin(render)
    if "90" not in out.split("wk:")[0]:
        failures.append(
            f"format_agy_quota: 5h slot should show gemini's 90%, got {out!r}"
        )
    if "60" not in out.split("wk:")[-1]:
        failures.append(f"format_agy_quota: wk slot should show 3p's 60%, got {out!r}")


def _check_format_agy_quota_never_hides_hottest_window(failures):
    # Reviewer's exact reproduction: a prior pair-vs-pair rule picked the whole
    # 3p pair here (3p-weekly's 96% beats gemini's pair-worst of 95%), which
    # rendered 3p-5h's irrelevant 5% and completely hid gemini-5h's 95% -- the
    # single most urgent, soonest-actionable number. Per-horizon selection
    # must show gemini-5h (95%, the hotter 5h window) in the 5h slot AND
    # 3p-weekly (96%, the hotter wk window) in the wk slot -- both real
    # numbers, neither hidden.
    def render():
        quota = {
            "gemini-5h": _window(0.05, 2 * _HOUR),  # 95% used
            "gemini-weekly": _window(0.9, 3 * 86400),  # 10% used
            "3p-5h": _window(0.95, 2 * _HOUR),  # 5% used
            "3p-weekly": _window(0.04, 3 * 86400),  # 96% used
        }
        return format_agy_quota(quota)

    out = _pin(render)
    five_hour_part, _, weekly_part = out.partition("wk:")
    if "95" not in five_hour_part:
        failures.append(
            f"format_agy_quota: must not hide gemini-5h's 95% behind the 3p pair, got {out!r}"
        )
    if "5%" in five_hour_part.replace("95%", ""):
        failures.append(
            f"format_agy_quota: 5h slot must not show 3p-5h's irrelevant 5%, got {out!r}"
        )
    if "96" not in weekly_part:
        failures.append(
            f"format_agy_quota: wk slot should show 3p-weekly's 96%, got {out!r}"
        )


def _check_format_agy_quota_malformed_never_raises(failures):
    for bad in (
        "nope",
        42,
        {"gemini-5h": "nope"},
        {"gemini-5h": {"remaining_fraction": "nan-ish"}},
    ):
        try:
            out = format_agy_quota(bad)
        except Exception as exc:
            failures.append(f"format_agy_quota: must not raise on {bad!r}, got {exc!r}")
            continue
        if out != "":
            failures.append(
                f"format_agy_quota: malformed input {bad!r} should render '', got {out!r}"
            )


def _check_format_agy_quota_color_bands(failures):
    def render(util):
        return format_agy_quota({"gemini-5h": _window(1.0 - util / 100.0, 2 * _HOUR)})

    high = _pin(lambda: render(95.0))
    if ramp_color(1.0) not in high:
        failures.append(
            f"format_agy_quota: 95% util should hit the red ramp end, got {high!r}"
        )


def check(failures):
    _check_window_metrics_basic(failures)
    _check_window_metrics_clamps(failures)
    _check_window_metrics_malformed(failures)
    _check_horizons_shape(failures)
    _check_most_constrained_window_picks_higher_util(failures)
    _check_most_constrained_window_missing_keys(failures)
    _check_most_constrained_window_one_side_missing(failures)
    _check_format_agy_quota_empty(failures)
    _check_format_agy_quota_renders_both_windows(failures)
    _check_format_agy_quota_show_pace_toggle(failures)
    _check_format_agy_quota_per_horizon_selection(failures)
    _check_format_agy_quota_never_hides_hottest_window(failures)
    _check_format_agy_quota_malformed_never_raises(failures)
    _check_format_agy_quota_color_bands(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: agy quota adapter selects the hottest window per horizon, never hides it"
    )


if __name__ == "__main__":
    main()
