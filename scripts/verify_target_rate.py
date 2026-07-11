"""Verify target-rate resolution: the →$ arrow, its STATUSLINE_TARGET_RATE
parse, and the adaptive weekly-sustainable target for subscription sessions.

Split from verify_burn_rate.py (which keeps the rate/needle/budget mechanics) to
stay under the file-size gate; shares its spend/clock seam via
scripts/_burn_rate_harness.py. Run from anywhere.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.burnrate as burnrate
import statusline_lib.pace as pace
from scripts._burn_rate_harness import _NOW, _with_spend
from statusline_lib.base import GREEN, RESET, ramp_color


def _sub_rl():
    """A subscription payload: 50% of the weekly quota used, reset in 1 day."""
    return {"seven_day": {"used_percentage": 50, "resets_at": _NOW + 86400}}


def _check_target_rate_parse(failures):
    cases = {"2": 2.0, "0.5": 0.5, "0": None, "-1": None, "abc": None}
    real = os.environ.get("STATUSLINE_TARGET_RATE")
    try:
        for raw, expected in cases.items():
            os.environ["STATUSLINE_TARGET_RATE"] = raw
            got = burnrate._target_rate()
            if got != expected:
                failures.append(
                    f"target rate {raw!r} -> {got!r}, expected {expected!r}"
                )
        os.environ.pop("STATUSLINE_TARGET_RATE", None)
        if burnrate._target_rate() != 1.0:
            failures.append("unset target rate should default to 1.0")
    finally:
        if real is None:
            os.environ.pop("STATUSLINE_TARGET_RATE", None)
        else:
            os.environ["STATUSLINE_TARGET_RATE"] = real


def _check_target_rate_arrow(failures):
    # No quota (API-key/none), default target $1/min. $6 in 5 min -> $1.20/min.
    # Green arrow to target; no quota/budget here -> no needle, just the arrow.
    out = _with_spend({int(_NOW - 300): 6.0}, lambda: burnrate.format_burn_rate(None))
    if f"{GREEN}→$1.00{RESET}" not in out:
        failures.append(f"target arrow should be green →$1.00; got {out!r}")
    # show_target=False suppresses the arrow.
    off = _with_spend(
        {int(_NOW - 300): 6.0},
        lambda: burnrate.format_burn_rate(None, show_target=False),
    )
    if "→" in off:
        failures.append(f"show_target=False must drop the arrow; got {off!r}")
    # Disabled target (env <=0) -> no arrow even when show_target=True.
    real = os.environ.get("STATUSLINE_TARGET_RATE")
    os.environ["STATUSLINE_TARGET_RATE"] = "0"
    try:
        none_out = _with_spend(
            {int(_NOW - 300): 6.0}, lambda: burnrate.format_burn_rate(None)
        )
    finally:
        if real is None:
            os.environ.pop("STATUSLINE_TARGET_RATE", None)
        else:
            os.environ["STATUSLINE_TARGET_RATE"] = real
    if "→" in none_out:
        failures.append(f"disabled target must show no arrow; got {none_out!r}")


def _check_weekly_sustainable_rate_unit(failures):
    # Pure derivation: remaining weekly quota $ over time to reset, calibrated
    # from util/$. util=50%, $720 burned, reset in 1 day -> quota $1440, $720
    # remaining over 1440 min -> $0.50/min. None on the degenerate guards.
    def rate(rl, hourly):
        return _with_spend(
            {}, lambda: pace.weekly_sustainable_rate(rl), weekly_hourly=hourly
        )

    guards = [
        (None, [100.0], "no rate_limits"),
        (
            {"seven_day": {"used_percentage": 0.5, "resets_at": _NOW + 86400}},
            [100.0],
            "util below the noise floor",
        ),
        (
            {"seven_day": {"used_percentage": 100, "resets_at": _NOW + 86400}},
            [100.0],
            "util at/over 100%",
        ),
        (
            {"seven_day": {"used_percentage": 50, "resets_at": _NOW - 10}},
            [100.0],
            "a past reset",
        ),
        (_sub_rl(), [], "no window spend"),
    ]
    for rl, hourly, why in guards:
        if rate(rl, hourly) is not None:
            failures.append(f"{why} should not derive a weekly target")
    got = rate(_sub_rl(), [720.0])
    if got is None or abs(got - 0.5) > 1e-9:
        failures.append(f"weekly target should be $0.50/min; got {got!r}")


def _check_weekly_sustainable_rate_malformed_resets_at(failures):
    # A malformed resets_at (string, e.g. from a corrupt/foreign payload) must
    # degrade to None like the util/quota guards above -- not raise into the
    # render path. Mirrors weekly_needle/weekly_exhaustion's own try/except.
    rl = {"seven_day": {"used_percentage": 50, "resets_at": "not-a-timestamp"}}
    try:
        got = _with_spend({}, lambda: pace.weekly_sustainable_rate(rl), [100.0])
    except TypeError as exc:
        failures.append(
            f"weekly_sustainable_rate must not raise on malformed resets_at: {exc}"
        )
        return
    if got is not None:
        failures.append(
            f"weekly_sustainable_rate with malformed resets_at should be None; got {got!r}"
        )


def _check_weekly_target_drives_arrow(failures):
    # Subscription, STATUSLINE_TARGET_RATE unset: the →$ arrow and the rate
    # coloring use the derived $0.50/min target. $5 in 5 min -> $1.00/min, so
    # r=2.0 -> the gradient band, not the flat-$1 green band.
    real_needle = burnrate.weekly_needle
    burnrate.weekly_needle = lambda _rl: ""
    real = os.environ.get("STATUSLINE_TARGET_RATE")
    os.environ.pop("STATUSLINE_TARGET_RATE", None)
    try:
        out = _with_spend(
            {int(_NOW - 300): 5.0},
            lambda: burnrate.format_burn_rate(_sub_rl()),
            weekly_hourly=[720.0],
        )
    finally:
        burnrate.weekly_needle = real_needle
        if real is not None:
            os.environ["STATUSLINE_TARGET_RATE"] = real
    if f"{GREEN}→$0.50{RESET}" not in out:
        failures.append(f"subscription target should derive →$0.50; got {out!r}")
    if f"{ramp_color((2.0 - 1.5) / 2.5)}$1.00/min{RESET}" not in out:
        failures.append(f"rate should color against the derived target; got {out!r}")


def _check_explicit_target_overrides_derivation(failures):
    # An explicit STATUSLINE_TARGET_RATE beats the weekly derivation for subs.
    real_needle = burnrate.weekly_needle
    burnrate.weekly_needle = lambda _rl: ""
    real = os.environ.get("STATUSLINE_TARGET_RATE")
    os.environ["STATUSLINE_TARGET_RATE"] = "2"
    try:
        out = _with_spend(
            {int(_NOW - 300): 5.0},
            lambda: burnrate.format_burn_rate(_sub_rl()),
            weekly_hourly=[720.0],
        )
    finally:
        burnrate.weekly_needle = real_needle
        if real is None:
            os.environ.pop("STATUSLINE_TARGET_RATE", None)
        else:
            os.environ["STATUSLINE_TARGET_RATE"] = real
    if f"{GREEN}→$2.00{RESET}" not in out:
        failures.append(f"explicit target must override derivation; got {out!r}")


def _check_weekly_target_fallback_when_thin(failures):
    # Subscription but no spend in the weekly window -> not derivable -> flat $1.
    real_needle = burnrate.weekly_needle
    burnrate.weekly_needle = lambda _rl: ""
    real = os.environ.get("STATUSLINE_TARGET_RATE")
    os.environ.pop("STATUSLINE_TARGET_RATE", None)
    try:
        out = _with_spend(
            {int(_NOW - 300): 5.0},
            lambda: burnrate.format_burn_rate(_sub_rl()),
            weekly_hourly=[],
        )
    finally:
        burnrate.weekly_needle = real_needle
        if real is not None:
            os.environ["STATUSLINE_TARGET_RATE"] = real
    if f"{GREEN}→$1.00{RESET}" not in out:
        failures.append(f"thin weekly data should fall back to →$1.00; got {out!r}")


def check(failures):
    _check_target_rate_parse(failures)
    _check_target_rate_arrow(failures)
    _check_weekly_sustainable_rate_unit(failures)
    _check_weekly_sustainable_rate_malformed_resets_at(failures)
    _check_weekly_target_drives_arrow(failures)
    _check_explicit_target_overrides_derivation(failures)
    _check_weekly_target_fallback_when_thin(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: target-rate arrow, parse, and adaptive weekly target resolve correctly")


if __name__ == "__main__":
    main()
