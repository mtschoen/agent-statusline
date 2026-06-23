"""Verify pace.weekly_exhaustion (line-3 "wk 100% ~<clock>" field) and its
_fmt_local_clock helper across every branch.

weekly_exhaustion shows the local clock time the weekly quota is projected to
hit 100% at the current burn rate, but ONLY when utilization is past 90% AND the
current-rate forecast lands before the window resets. These checks pin
_now_unix + _pace_hourly_cached (so no transcripts or wall clock are involved)
and drive each guard plus the happy path.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.pace as pace

_WIN_START = 1_748_000_000.0
_PERIOD = 7 * 86400
_RESETS_AT = _WIN_START + _PERIOD


def _make_rl(util, resets_at=_RESETS_AT):
    return {"seven_day": {"used_percentage": util, "resets_at": resets_at}}


def _pin(now, hourly):
    real_now = pace._now_unix
    real_cached = pace._pace_hourly_cached
    pace._now_unix = lambda: now
    pace._pace_hourly_cached = lambda _ws: hourly
    return real_now, real_cached


def _restore(real_now, real_cached):
    pace._now_unix = real_now
    pace._pace_hourly_cached = real_cached


def _check_entry_guards(failures):
    """util None / at-or-below 90 / no resets_at all yield '' before any walk."""
    cases = (
        ("util=None", _make_rl(None)),
        ("util=80 (<=90)", _make_rl(80)),
        ("util=90 (boundary, not >90)", _make_rl(90)),
        ("resets_at=0", _make_rl(95, resets_at=0)),
    )
    for label, rl in cases:
        if pace.weekly_exhaustion(rl) != "":
            failures.append(f"weekly_exhaustion {label} should return ''")


def _check_deltas_none(failures):
    """now past reset -> _weekly_deltas returns None (remaining<=0) -> ''."""
    real_now, real_cached = _pin(_RESETS_AT + 3600, [1.0] * 10)
    try:
        result = pace.weekly_exhaustion(_make_rl(95))
    finally:
        _restore(real_now, real_cached)
    if result != "":
        failures.append(
            f"weekly_exhaustion deltas=None should return '', got {result!r}"
        )


def _check_current_rate_none(failures):
    """Empty window -> no $ to calibrate -> current_rate_delta None -> ''."""
    real_now, real_cached = _pin(_WIN_START + 6 * 86400, [])
    try:
        result = pace.weekly_exhaustion(_make_rl(95))
    finally:
        _restore(real_now, real_cached)
    if result != "":
        failures.append(
            f"weekly_exhaustion current_rate None should return '', got {result!r}"
        )


def _check_lands_after_reset(failures):
    """Low recent burn -> 100% projected long after reset (delta>=0) -> '' (you
    make it to reset, so nothing is shown)."""
    now = _RESETS_AT - 7200  # 2h of window left
    hourly = [100.0] * 10 + [0.1] * 24  # heavy early, near-cold recent tail
    real_now, real_cached = _pin(now, hourly)
    try:
        result = pace.weekly_exhaustion(_make_rl(91))
    finally:
        _restore(real_now, real_cached)
    if result != "":
        failures.append(
            f"weekly_exhaustion lands-after-reset should return '', got {result!r}"
        )


def _check_happy_path(failures):
    """Hot recent burn at 95% -> 100% projected before reset -> rendered field."""
    now = _WIN_START + 5 * 86400  # well past warmup, plenty of window left
    hourly = [5.0] * 48  # sustained hot recent rate
    real_now, real_cached = _pin(now, hourly)
    try:
        result = pace.weekly_exhaustion(_make_rl(95))
    finally:
        _restore(real_now, real_cached)
    if "wk 100% ~" not in result:
        failures.append(
            f"weekly_exhaustion happy path should render 'wk 100% ~...', got {result!r}"
        )


def _check_exception_path(failures):
    """A non-dict seven_day makes w.get raise; the guard degrades to '' not a crash."""
    result = pace.weekly_exhaustion({"seven_day": 123})
    if result != "":
        failures.append(
            f"weekly_exhaustion exception path should return '', got {result!r}"
        )


def _check_fmt_local_clock_today(failures):
    """Same-instant timestamp shares the local date with now -> bare clock, no weekday."""
    now = _WIN_START + 3 * 86400 + 5 * 3600
    real_now = pace._now_unix
    pace._now_unix = lambda: now
    try:
        result = pace._fmt_local_clock(now)
    finally:
        pace._now_unix = real_now
    # Bare "H:MMam/pm" carries no space (a weekday prefix would add one).
    if " " in result or not result.endswith(("am", "pm")):
        failures.append(
            f"_fmt_local_clock today should be a bare 12h clock, got {result!r}"
        )


def _check_fmt_local_clock_other_day(failures):
    """A timestamp 3 days out differs in calendar date under any timezone ->
    weekday-prefixed clock."""
    now = _WIN_START + 86400
    real_now = pace._now_unix
    pace._now_unix = lambda: now
    try:
        result = pace._fmt_local_clock(now + 3 * 86400)
    finally:
        pace._now_unix = real_now
    if " " not in result or not result.endswith(("am", "pm")):
        failures.append(
            f"_fmt_local_clock other day should be 'Wkd H:MMxm', got {result!r}"
        )


def check(failures):
    _check_entry_guards(failures)
    _check_deltas_none(failures)
    _check_current_rate_none(failures)
    _check_lands_after_reset(failures)
    _check_happy_path(failures)
    _check_exception_path(failures)
    _check_fmt_local_clock_today(failures)
    _check_fmt_local_clock_other_day(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: weekly_exhaustion guards (util/resets, deltas-None, rate-None, "
        "after-reset, exception) + happy path; _fmt_local_clock today/other-day"
    )


if __name__ == "__main__":
    main()
