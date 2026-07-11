"""Verify statusline_lib.agy's quota adapter: agy's `quota` payload block
(remaining_fraction/reset_time/reset_in_seconds per window) mapped into the
same '5h: P% +Hh wk: P% +Hh' form pace.format_quota renders for Claude's
rate_limits, plus the gemini-vs-3p pair-selection rule.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.pace as pace
from statusline_lib.agy import (
    _agy_pair_worst_utilization,
    _agy_primary_pair,
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


def _check_pair_worst_utilization(failures):
    quota = {
        "gemini-5h": _window(0.9, _HOUR),  # 10% used
        "gemini-weekly": _window(0.4, 5 * _HOUR),  # 60% used
    }
    worst = _agy_pair_worst_utilization(quota, ("gemini-5h", "gemini-weekly"))
    if worst is None or abs(worst - 60.0) > 0.01:
        failures.append(
            f"_agy_pair_worst_utilization: should pick the higher-util window, got {worst!r}"
        )
    if _agy_pair_worst_utilization(quota, ("missing-a", "missing-b")) is not None:
        failures.append("_agy_pair_worst_utilization: missing keys should yield None")


def _check_primary_pair_defaults_gemini(failures):
    quota = {
        "gemini-5h": _window(0.9, _HOUR),
        "3p-5h": _window(0.9, _HOUR),
    }
    if _agy_primary_pair(quota) != ("gemini-5h", "gemini-weekly"):
        failures.append(
            "_agy_primary_pair: equal utilization should default to gemini pair"
        )


def _check_primary_pair_switches_to_3p(failures):
    quota = {
        "gemini-5h": _window(0.95, _HOUR),  # 5% used
        "3p-5h": _window(0.2, _HOUR),  # 80% used -- more constrained
    }
    if _agy_primary_pair(quota) != ("3p-5h", "3p-weekly"):
        failures.append("_agy_primary_pair: strictly-more-utilized 3p pair should win")


def _check_primary_pair_no_gemini_data(failures):
    quota = {"3p-5h": _window(0.5, _HOUR)}
    if _agy_primary_pair(quota) != ("3p-5h", "3p-weekly"):
        failures.append(
            "_agy_primary_pair: 3p should win when gemini has no usable data at all"
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


def _check_format_agy_quota_selects_3p_pair(failures):
    def render():
        quota = {
            "gemini-5h": _window(0.98, 2 * _HOUR),  # near-empty utilization
            "3p-5h": _window(0.1, 2 * _HOUR),  # 90% used -- should be surfaced
        }
        return format_agy_quota(quota)

    out = _pin(render)
    band = out.split("5h:")[-1].split(" ")[0]
    if not band.startswith(ramp_color(1.0)) and "90" not in out:
        failures.append(
            f"format_agy_quota: should surface the more-constrained 3p pair, got {out!r}"
        )


def _check_format_agy_quota_never_raises(failures):
    # A non-dict quota, or one whose values are the wrong shape, must degrade
    # to "" rather than raise into the render path.
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


def check(failures):
    _check_window_metrics_basic(failures)
    _check_window_metrics_clamps(failures)
    _check_window_metrics_malformed(failures)
    _check_pair_worst_utilization(failures)
    _check_primary_pair_defaults_gemini(failures)
    _check_primary_pair_switches_to_3p(failures)
    _check_primary_pair_no_gemini_data(failures)
    _check_format_agy_quota_empty(failures)
    _check_format_agy_quota_renders_both_windows(failures)
    _check_format_agy_quota_show_pace_toggle(failures)
    _check_format_agy_quota_selects_3p_pair(failures)
    _check_format_agy_quota_never_raises(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: agy quota adapter maps quota -> the same 5h/wk render form")


if __name__ == "__main__":
    main()
