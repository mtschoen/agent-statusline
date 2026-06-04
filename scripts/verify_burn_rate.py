"""Verify the burn-rate field: live 5-min $/min, daily-budget needle, day: %.

Injects spend via the shared _with_spend seam (scripts/_burn_rate_harness.py),
which reassigns burnrate._sum_window_spend and pins the clock via pace._now_unix,
mirroring verify_quota_render.py. Target-rate resolution (the →$ arrow and the
adaptive weekly target) lives in its sibling verify_target_rate.py.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.burnrate as burnrate
from scripts._burn_rate_harness import _NOW, _with_spend
from statusline_lib.base import (
    CACHE_READ,
    GREEN,
    RESET,
    ramp_color,
)
from statusline_lib.pace import ARROW_DOWN, ARROW_UP, ON_TARGET_GLYPH


def _check_cache_dedupes_walk(failures):
    import tempfile

    calls = {"n": 0}
    real_sum = burnrate._sum_window_spend
    real_path = burnrate._SPEND_CACHE_PATH
    fd, tmp = tempfile.mkstemp(prefix="burnrate-cache-")
    os.close(fd)
    os.remove(tmp)  # start with no cache file present
    burnrate._SPEND_CACHE_PATH = tmp
    burnrate._sum_window_spend = lambda ws: (
        calls.__setitem__("n", calls["n"] + 1),
        12.5,
    )[1]
    try:
        a = burnrate._window_spend_cached(_NOW - 300)
        b = burnrate._window_spend_cached(_NOW - 300)
    finally:
        burnrate._sum_window_spend = real_sum
        burnrate._SPEND_CACHE_PATH = real_path
        with contextlib.suppress(OSError):
            os.remove(tmp)
    if a != 12.5 or b != 12.5:
        failures.append(f"cache should return the walked total; got {a!r}, {b!r}")
    if calls["n"] != 1:
        failures.append(f"second cached call should not re-walk; walks={calls['n']}")


def _check_five_min_rate_render(failures):
    # $6.00 in the last 5 min -> $1.20/min. No rate_limits, no budget -> no needle.
    out = _with_spend({int(_NOW - 300): 6.0}, lambda: burnrate.format_burn_rate(None))
    if "$1.20/min" not in out:
        failures.append(f"5-min rate should render $1.20/min; got {out!r}")
    if ARROW_UP in out or ARROW_DOWN in out or ON_TARGET_GLYPH in out:
        failures.append(f"no budget / no quota should show no needle; got {out!r}")


def _check_omitted_when_idle_no_budget(failures):
    out = _with_spend({}, lambda: burnrate.format_burn_rate(None))
    if out != "":
        failures.append(
            f"idle + no budget + no quota should omit the field; got {out!r}"
        )


def _check_budget_parse(failures):
    cases = {"100": 100.0, "49.5": 49.5, "0": None, "-5": None, "abc": None, "": None}
    real = os.environ.get("STATUSLINE_DAILY_BUDGET")
    try:
        for raw, expected in cases.items():
            os.environ["STATUSLINE_DAILY_BUDGET"] = raw
            got = burnrate._daily_budget()
            if got != expected:
                failures.append(f"budget {raw!r} -> {got!r}, expected {expected!r}")
        os.environ.pop("STATUSLINE_DAILY_BUDGET", None)
        if burnrate._daily_budget() is not None:
            failures.append("unset budget should be None")
    finally:
        if real is None:
            os.environ.pop("STATUSLINE_DAILY_BUDGET", None)
        else:
            os.environ["STATUSLINE_DAILY_BUDGET"] = real


def _check_budget_needle_ratio(failures):
    # budget $100/day.
    cases = [
        (40.0, ARROW_DOWN, ramp_color(0.0)),  # well under -> green down
        (90.0, ARROW_DOWN, ramp_color(0.0)),  # 10% under -> still solid green down
        (100.0, ON_TARGET_GLYPH, GREEN),  # on budget -> green yin-yang
        (102.0, ON_TARGET_GLYPH, GREEN),  # within 5% -> yin-yang
        (150.0, ARROW_UP, ramp_color(0.0)),  # 50% over -> still green (no yellow yet)
        (200.0, ARROW_UP, ramp_color(0.0)),  # 100% over -> green onset of the ramp
        (300.0, ARROW_UP, ramp_color(0.5)),  # 200% over -> yellow midpoint
        (400.0, ARROW_UP, ramp_color(1.0)),  # 300% over -> full red up
    ]
    for spend, glyph, color in cases:
        out = burnrate._budget_needle(spend, 100.0)
        if glyph not in out:
            failures.append(
                f"24h spend ${spend} vs $100 should show {glyph!r}; got {out!r}"
            )
        if color not in out:
            failures.append(
                f"24h spend ${spend} vs $100 should be {color!r}; got {out!r}"
            )
    if burnrate._budget_needle(0.0, 100.0) != "":
        failures.append("zero 24h spend should yield no needle")
    if burnrate._budget_needle(50.0, None) != "":
        failures.append("no budget should yield no needle")


def _check_api_key_needle_on_rate(failures):
    # No rate_limits, budget $100. 24h spend $40 -> down arrow on the rate.
    # 5-min spend $6 -> $1.20/min.
    windows = {int(_NOW - 300): 6.0, int(_NOW - 86400): 40.0}
    real = os.environ.get("STATUSLINE_DAILY_BUDGET")
    os.environ["STATUSLINE_DAILY_BUDGET"] = "100"
    try:
        out = _with_spend(windows, lambda: burnrate.format_burn_rate(None))
    finally:
        if real is None:
            os.environ.pop("STATUSLINE_DAILY_BUDGET", None)
        else:
            os.environ["STATUSLINE_DAILY_BUDGET"] = real
    if "$1.20/min" not in out:
        failures.append(f"API-key field should show the 5-min rate; got {out!r}")
    if ARROW_DOWN not in out:
        failures.append(f"under-budget 24h should show a down arrow; got {out!r}")


def _check_day_field(failures):
    # budget $100, since-midnight spend $47 -> day: 47%. Compute midnight under
    # the pinned clock (_NOW) so the window key matches _local_midnight_unix().
    from datetime import datetime

    midnight = (
        datetime.fromtimestamp(_NOW)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    real = os.environ.get("STATUSLINE_DAILY_BUDGET")
    os.environ["STATUSLINE_DAILY_BUDGET"] = "100"
    try:
        out = _with_spend(
            {int(midnight): 47.0}, lambda: burnrate.format_day_budget(None)
        )
    finally:
        if real is None:
            os.environ.pop("STATUSLINE_DAILY_BUDGET", None)
        else:
            os.environ["STATUSLINE_DAILY_BUDGET"] = real
    if "day: " not in out or "47%" not in out:
        failures.append(f"day: field should show 47%; got {out!r}")


def _check_day_field_omitted_for_subscription(failures):
    rl = {"seven_day": {"used_percentage": 22, "resets_at": _NOW + 1000}}
    real = os.environ.get("STATUSLINE_DAILY_BUDGET")
    os.environ["STATUSLINE_DAILY_BUDGET"] = "100"
    try:
        out = _with_spend({}, lambda: burnrate.format_day_budget(rl))
    finally:
        if real is None:
            os.environ.pop("STATUSLINE_DAILY_BUDGET", None)
        else:
            os.environ["STATUSLINE_DAILY_BUDGET"] = real
    if out != "":
        failures.append(
            f"day: must be suppressed when rate_limits present; got {out!r}"
        )


def _check_subscription_needle_on_rate(failures):
    # Subscription: rate_limits present. weekly_needle is stubbed to a known glyph;
    # format_burn_rate must place it on the $/min and ignore any budget env. No
    # weekly hourly is injected, so the derived target falls back to the flat $1.
    rl = {"seven_day": {"used_percentage": 22, "resets_at": _NOW + 1000}}
    real_needle = burnrate.weekly_needle
    burnrate.weekly_needle = lambda _rl: f"{GREEN}{ARROW_DOWN}{RESET}"
    real_budget = os.environ.get("STATUSLINE_DAILY_BUDGET")
    os.environ["STATUSLINE_DAILY_BUDGET"] = "100"  # must be IGNORED for subscription
    try:
        out = _with_spend({int(_NOW - 300): 5.0}, lambda: burnrate.format_burn_rate(rl))
    finally:
        burnrate.weekly_needle = real_needle
        if real_budget is None:
            os.environ.pop("STATUSLINE_DAILY_BUDGET", None)
        else:
            os.environ["STATUSLINE_DAILY_BUDGET"] = real_budget
    if "$1.00/min" not in out or ARROW_DOWN not in out:
        failures.append(f"subscription needle should ride the $/min; got {out!r}")


def _check_rate_number_color(failures):
    # r = rate / target. target via _rate_color(rate, target) directly.
    cases = [
        (0.2, CACHE_READ),  # r=0.2 < 0.5 -> teal
        (1.0, GREEN),  # r=1.0 in 0.5..1.5 band -> green
        (1.4, GREEN),  # still in band
        (2.0, ramp_color((2.0 - 1.5) / 2.5)),  # r=2.0 -> gradient
        (4.0, ramp_color(1.0)),  # r=4.0 -> hot end (red)
        (9.0, ramp_color(1.0)),  # clamped red
    ]
    for rate, expected in cases:
        got = burnrate._rate_color(rate, 1.0)
        if got != expected:
            failures.append(
                f"_rate_color({rate}, 1.0) -> {got!r}, expected {expected!r}"
            )


def _check_rate_number_colored_in_field(failures):
    # Default target ($1/min). $6 in 5 min -> $1.20/min -> r=1.2 -> green band.
    out = _with_spend({int(_NOW - 300): 6.0}, lambda: burnrate.format_burn_rate(None))
    if f"{GREEN}$1.20/min{RESET}" not in out:
        failures.append(f"rate number should be wrapped in its band color; got {out!r}")


def _check_glyph_text_presentation(failures):
    # U+FE0E forces text (monochrome) presentation so the ANSI green renders
    # cross-platform instead of Windows Terminal's emoji-font color.
    if "︎" not in ON_TARGET_GLYPH:
        failures.append("on-target glyph should carry the U+FE0E text selector")


def check(failures):
    _check_cache_dedupes_walk(failures)
    _check_five_min_rate_render(failures)
    _check_omitted_when_idle_no_budget(failures)
    _check_budget_parse(failures)
    _check_budget_needle_ratio(failures)
    _check_api_key_needle_on_rate(failures)
    _check_day_field(failures)
    _check_day_field_omitted_for_subscription(failures)
    _check_subscription_needle_on_rate(failures)
    _check_rate_number_color(failures)
    _check_rate_number_colored_in_field(failures)
    _check_glyph_text_presentation(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: burn-rate field renders rate, needle, and day: budget correctly")


if __name__ == "__main__":
    main()
