"""Pure quota-pace projection math: two window-local signals (cumulative pace + a current-rate forecast chosen from three estimators).

No I/O and no clock. `pace.py` feeds this live window data and
`scripts/backtest_pace.py` feeds it replayed historical windows through the
SAME `project_delta`, so the backtest exercises the shipped projection path.

Two signals (both sourced from the current rate-limit window only):
  * cumulative pace -- util/elapsed projected to reset (the simple run-rate).
  * current-rate forecast -- a recency-weighted $/h over the recent part of the
    window, calibrated to %/h via the window's own util/$ ratio, projected
    forward. This is the "slow down / go nuts" needle.

Both deltas are shrunk toward 0 ("on track to 100% at reset") early in the
window by a warmup prior, so a fresh window reads ~0h instead of exploding on a
tiny elapsed denominator.

Imports: none from sibling modules (leaf module, like base).
"""

# Shipped defaults. `estimator` was chosen empirically by scripts/backtest_pace.py
# over 9 real weekly windows: trailing-24h had by far the lowest jumpiness (a
# stable arrow), while ewma/slope exploded on bursty real usage (recent burn -> 0
# sends the projected time-to-100% to infinity). See the plan/PR for the table.
DEFAULT_PARAMS = {
    "estimator": "trailing_hours",
    "window_hours": 24.0,  # trailing_hours / recent_slope: how much recent tail to average
    "lambda": 0.5,  # ewma only: per-hour decay weight (0<lambda<1; unused by trailing_hours)
    "warmup_seconds": 18 * 3600,  # day-1 prior horizon
}


def _trailing_hours_rate(hourly_burn, params):
    """Mean $/h over the last `window_hours` buckets (clamped to what exists)."""
    if not hourly_burn:
        return 0.0
    n = max(1, round(params["window_hours"]))
    recent = hourly_burn[-n:]
    return sum(recent) / len(recent)


def _ewma_rate(hourly_burn, params):
    """Exponentially-weighted mean $/h; the most-recent bucket carries the most weight."""
    if not hourly_burn:
        return 0.0
    decay = params["lambda"]
    weighted = 0.0
    norm = 0.0
    for index, value in enumerate(reversed(hourly_burn)):
        weight = decay**index
        weighted += weight * value
        norm += weight
    return weighted / norm if norm else 0.0


def _recent_slope_rate(hourly_burn, params):
    """Least-squares fit over the recent tail; return the projected next-hour
    level (clamped non-negative). Falls back to the last value for <2 points."""
    n = max(2, round(params["window_hours"]))
    recent = hourly_burn[-n:]
    count = len(recent)
    if count < 2:
        return recent[-1] if recent else 0.0
    xs = list(range(count))
    mean_x = sum(xs) / count
    mean_y = sum(recent) / count
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return mean_y
    slope = sum((xs[i] - mean_x) * (recent[i] - mean_y) for i in range(count)) / denom
    return max(0.0, mean_y + slope * (xs[-1] - mean_x + 1))


_ESTIMATORS = {
    "trailing_hours": _trailing_hours_rate,
    "ewma": _ewma_rate,
    "recent_slope": _recent_slope_rate,
}


def current_rate(hourly_burn, params=None):
    """Recent $/h burn via the params-selected estimator (defaults to ewma).

    params may be a partial dict; missing keys fall back to DEFAULT_PARAMS.
    """
    params = {**DEFAULT_PARAMS, **(params or {})}
    estimator = _ESTIMATORS.get(params["estimator"], _ewma_rate)
    return estimator(hourly_burn, params)


def is_on_target(
    cumulative_delta, current_rate_delta, elapsed, params=None, margin_seconds=14400.0
):
    """True when BOTH pace signals sit within `margin_seconds` of a perfect
    reset-time landing AND the warmup prior has fully phased out
    (elapsed >= warmup_seconds) -- so the near-zero reading reflects real on-pace
    burn, not the day-1 prior shrinking the deltas toward 0. Drives the on-target
    reward glyph. params may be a partial dict; missing keys fall back to
    DEFAULT_PARAMS.
    """
    params = {**DEFAULT_PARAMS, **(params or {})}
    if cumulative_delta is None or current_rate_delta is None:
        return False
    if elapsed < params["warmup_seconds"]:
        return False
    return (
        abs(cumulative_delta) <= margin_seconds
        and abs(current_rate_delta) <= margin_seconds
    )


def project_delta(hourly_burn, util, elapsed, remaining, period_seconds, params=None):
    """Return (cumulative_delta, current_rate_delta), both seconds vs reset.

    Positive => surplus (hits 100% after reset); negative => exhausts early.
    `current_rate_delta` is None when calibration is degenerate (no $ in window).
    Both deltas are scaled by the day-1 prior k = min(1, elapsed/warmup_seconds).
    Returns (None, None) on degenerate inputs (util<=0, non-positive elapsed/remaining).
    params may be a partial dict; missing keys fall back to DEFAULT_PARAMS.
    """
    params = {**DEFAULT_PARAMS, **(params or {})}
    if util is None or util <= 0 or elapsed <= 0 or remaining <= 0:
        return None, None

    cumulative_delta = 100.0 * elapsed / util - period_seconds

    current_rate_delta = None
    window_dollars = sum(hourly_burn) if hourly_burn else 0.0
    if window_dollars > 0:
        rate_per_hour = current_rate(hourly_burn, params)
        pct_per_hour = rate_per_hour * (util / window_dollars)
        if pct_per_hour > 0:
            seconds_to_full = (100.0 - util) / pct_per_hour * 3600.0
            current_rate_delta = seconds_to_full - remaining

    warmup = params["warmup_seconds"]
    k = min(1.0, elapsed / warmup) if warmup > 0 else 1.0
    cumulative_delta *= k
    if current_rate_delta is not None:
        current_rate_delta *= k
    return cumulative_delta, current_rate_delta
