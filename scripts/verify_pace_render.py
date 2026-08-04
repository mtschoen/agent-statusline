"""Verify weekly_sustainable_rate guards and _project_pace all branches.

Covers:
  - weekly_sustainable_rate: remaining<=0, window_spend<=0, quota fully spent
    (util at/over 100 caught by the remaining_dollars<=0 guard)
  - _project_pace: util=None/zero/no-resets_at guard (line 361), use_trailing=False
    5h path (lines 364-373), elapsed<=0 guard (line 370), use_trailing=True with
    deltas=None (line 376), exception path (lines 379-380)

Run from anywhere; imports from `agent-statusline` by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.pace as pace

_WIN_START = 1_748_000_000.0
_PERIOD = 7 * 86400


def _make_rl(util, resets_at):
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


def _check_sustainable_rate_remaining_le_zero(failures):
    """remaining<=0 (reset already past) -> None."""
    real_now, real_cached = _pin(_WIN_START + _PERIOD + 100, [1.0] * 10)
    try:
        result = pace.weekly_sustainable_rate(_make_rl(50, _WIN_START + _PERIOD))
    finally:
        _restore(real_now, real_cached)
    if result is not None:
        failures.append(
            f"sustainable_rate remaining<=0 should return None, got {result!r}"
        )


def _check_sustainable_rate_no_spend(failures):
    """window_spend<=0 (empty hourly) -> None."""
    real_now, real_cached = _pin(_WIN_START + 3600, [])
    try:
        result = pace.weekly_sustainable_rate(_make_rl(50, _WIN_START + _PERIOD))
    finally:
        _restore(real_now, real_cached)
    if result is not None:
        failures.append(
            f"sustainable_rate window_spend<=0 should return None, got {result!r}"
        )


def _check_sustainable_rate_quota_spent(failures):
    """util at/over 100% -> remaining_dollars<=0 -> None. The entry guard does
    not filter util>=100; the spent-quota arithmetic is the live guard."""
    for util in (100, 120):
        real_now, real_cached = _pin(_WIN_START + 3600, [1.0] * 10)
        try:
            result = pace.weekly_sustainable_rate(_make_rl(util, _WIN_START + _PERIOD))
        finally:
            _restore(real_now, real_cached)
        if result is not None:
            failures.append(
                f"sustainable_rate util={util} should return None, got {result!r}"
            )


def _check_sustainable_rate_happy_path(failures):
    """Verify a positive rate is returned when data is available."""
    now = _WIN_START + 84 * 3600
    hourly = [1.0] * 84
    real_now, real_cached = _pin(now, hourly)
    try:
        result = pace.weekly_sustainable_rate(_make_rl(50, _WIN_START + _PERIOD))
    finally:
        _restore(real_now, real_cached)
    if result is None or result <= 0:
        failures.append(
            f"sustainable_rate happy path should return positive rate, got {result!r}"
        )


def _check_project_pace_none_guards(failures):
    """util=None/zero/no-resets_at early return (line 361)."""
    if pace._project_pace(None, _WIN_START + _PERIOD, _PERIOD) != "":
        failures.append("_project_pace util=None should return ''")
    if pace._project_pace(0, _WIN_START + _PERIOD, _PERIOD) != "":
        failures.append("_project_pace util=0 should return ''")
    if pace._project_pace(50, 0, _PERIOD) != "":
        failures.append("_project_pace resets_at=0 should return ''")


def _check_project_pace_5h_path(failures):
    """use_trailing=False 5h calculation path (lines 364-373)."""
    period = 5 * 3600
    resets_at = _WIN_START + period
    real_now = pace._now_unix
    pace._now_unix = lambda: _WIN_START + 2.5 * 3600
    try:
        result = pace._project_pace(50, resets_at, period, use_trailing=False)
    finally:
        pace._now_unix = real_now
    if "h" not in result:
        failures.append(f"_project_pace 5h path should contain 'h', got {result!r}")


def _check_project_pace_5h_elapsed_guard(failures):
    """elapsed<=0 guard inside the 5h path (line 370)."""
    period = 5 * 3600
    resets_at = _WIN_START + period
    real_now = pace._now_unix
    pace._now_unix = lambda: _WIN_START - 3600
    try:
        result = pace._project_pace(50, resets_at, period, use_trailing=False)
    finally:
        pace._now_unix = real_now
    if result != "":
        failures.append(f"_project_pace 5h elapsed<=0 should return '', got {result!r}")


def _check_project_pace_trailing_deltas_none(failures):
    """use_trailing=True + deltas=None -> '' (line 376). _project_pace's own
    guard only checks util/resets_at, so a now past the reset reaches
    _weekly_deltas and gets None back (remaining<=0)."""
    resets_at = _WIN_START + _PERIOD
    real_now, real_cached = _pin(resets_at + 3600, [1.0] * 10)
    try:
        result = pace._project_pace(50, resets_at, _PERIOD, use_trailing=True)
    finally:
        _restore(real_now, real_cached)
    if result != "":
        failures.append(
            f"_project_pace trailing deltas=None should return '', got {result!r}"
        )


def _check_project_pace_exception_path(failures):
    """except Exception: return '' path (lines 379-380). A truthy non-numeric
    resets_at passes the guard, then datetime.fromtimestamp raises inside the
    try block."""
    result = pace._project_pace(50, "not-a-timestamp", _PERIOD, use_trailing=False)
    if result != "":
        failures.append(
            f"_project_pace exception path should return '', got {result!r}"
        )


def check(failures):
    _check_sustainable_rate_remaining_le_zero(failures)
    _check_sustainable_rate_no_spend(failures)
    _check_sustainable_rate_quota_spent(failures)
    _check_sustainable_rate_happy_path(failures)
    _check_project_pace_none_guards(failures)
    _check_project_pace_5h_path(failures)
    _check_project_pace_5h_elapsed_guard(failures)
    _check_project_pace_trailing_deltas_none(failures)
    _check_project_pace_exception_path(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: weekly_sustainable_rate guards; "
        "_project_pace None guards, 5h path, elapsed guard, trailing-None, exception"
    )


if __name__ == "__main__":
    main()
