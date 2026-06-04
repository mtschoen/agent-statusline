"""Shared spend/clock stubbing seam for the burn-rate verify scripts.

verify_burn_rate.py and verify_target_rate.py both drive
statusline_lib.burnrate.format_burn_rate with injected spend; this holds the
common _NOW clock pin and the _with_spend stubbing context so neither script
duplicates the seam (and verify_burn_rate.py stays under the file-size gate).
Underscore-prefixed so the CI `scripts/verify_*.py` glob does not run it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Isolate from any real ~/.claude/.statusline-prefs.json: point the resolver at
# os.devnull so pref() reads {} and falls through to the env vars these tests
# set. Without this, a live prefs file would silently override the test env.
os.environ["STATUSLINE_PREFS_PATH"] = os.devnull
import statusline_lib.burnrate as burnrate
import statusline_lib.pace as pace

_NOW = 1_700_000_000.0


def _with_spend(window_to_total, fn, weekly_hourly=None):
    """Run fn() with the spend walk stubbed from a {win_start: total} map and
    the clock pinned to _NOW. burnrate imported _now_unix BY VALUE
    (`from .pace import _now_unix`), so we patch burnrate._now_unix - patching
    pace._now_unix alone would not reach the window-key computation.

    `weekly_hourly` stubs pace._pace_hourly_cached, which the adaptive weekly
    target (pace.weekly_sustainable_rate) sums for its quota calibration; it
    defaults to [] so cases that don't exercise the derivation derive no target
    and fall back to the flat default."""
    real_sum = burnrate._sum_window_spend
    real_cached = burnrate._window_spend_cached
    real_now_b = burnrate._now_unix
    real_now_p = pace._now_unix
    real_hourly = pace._pace_hourly_cached
    burnrate._sum_window_spend = lambda ws: window_to_total.get(int(ws), 0.0)
    burnrate._window_spend_cached = lambda ws: window_to_total.get(int(ws), 0.0)
    burnrate._now_unix = lambda: _NOW
    pace._now_unix = lambda: _NOW
    pace._pace_hourly_cached = lambda ws: list(weekly_hourly or [])
    try:
        return fn()
    finally:
        burnrate._sum_window_spend = real_sum
        burnrate._window_spend_cached = real_cached
        burnrate._now_unix = real_now_b
        pace._now_unix = real_now_p
        pace._pace_hourly_cached = real_hourly
