"""Backtest harness: replay candidate current-rate estimators against
reconstructed, normalized historical quota windows.

Why this exists: the live util% is never logged (it arrives in the statusline
payload and is discarded), so we cannot backtest against real historical %.
Instead we reconstruct each past window's cumulative DOLLAR curve from the
transcripts and normalize it to 100% at the window's end -- valid *because* the
user maxes the weekly quota every cycle, so weekly-total $ ~= quota. This makes
the comparison between estimators fair (they all see identical synthetic data)
even though the absolute % is not calibrated. Reset boundaries are supplied by
the user (--resets) because the Opus-4.8 mid-week reset broke the clean 7-day
cadence; the anomalous window is excluded, not modeled.

The replay calls statusline_lib.project.project_delta -- the SAME function the
live statusline uses -- so the winning params transfer directly.
"""

import argparse
import os
import sys
from datetime import UTC, datetime
from itertools import pairwise

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib.cost import _cost_for_turn
from statusline_lib.pace import _discover_pace_groups, _parse_pace_line
from statusline_lib.project import project_delta
from statusline_lib.walker import _walker_root_list

_HOUR = 3600.0
_CANDIDATES = [
    {
        "label": "trailing-12h",
        "estimator": "trailing_hours",
        "window_hours": 12.0,
        "lambda": 0.5,
        "warmup_seconds": 18 * 3600,
    },
    {
        "label": "trailing-24h",
        "estimator": "trailing_hours",
        "window_hours": 24.0,
        "lambda": 0.5,
        "warmup_seconds": 18 * 3600,
    },
    {
        "label": "ewma-0.5",
        "estimator": "ewma",
        "window_hours": 18.0,
        "lambda": 0.5,
        "warmup_seconds": 18 * 3600,
    },
    {
        "label": "ewma-0.8",
        "estimator": "ewma",
        "window_hours": 18.0,
        "lambda": 0.8,
        "warmup_seconds": 18 * 3600,
    },
    {
        "label": "slope-18h",
        "estimator": "recent_slope",
        "window_hours": 18.0,
        "lambda": 0.5,
        "warmup_seconds": 18 * 3600,
    },
]


def _accumulate_file(path, seen_ids, win_start_unix, win_end_unix, n_buckets, buckets):
    """Parse one transcript file and add its per-hour cost into buckets in place."""
    last_model = ""
    try:
        with open(path, "rb") as f:
            for line in f:
                parsed = _parse_pace_line(line, seen_ids, earliest=win_start_unix)
                if parsed is None:
                    continue
                ts, usage, model_id = parsed
                if model_id:
                    last_model = model_id
                if ts >= win_end_unix:
                    continue
                index = int((ts - win_start_unix) // 3600)
                if 0 <= index < n_buckets:
                    buckets[index] += _cost_for_turn(usage, model_id or last_model)
    except OSError:
        # Transcript unreadable mid-walk; skip and use whatever was accumulated.
        return


def reconstruct_window(roots, win_start_unix, win_end_unix):
    """Hourly $-burn series for [win_start, win_end). Reuses the live walk's
    discovery/parse/cost helpers so reconstruction matches the live walk."""
    n_buckets = max(1, int((win_end_unix - win_start_unix) // 3600))
    buckets = [0.0] * n_buckets
    groups = _discover_pace_groups(roots, win_start_unix)
    for paths in groups.values():
        seen_ids = set()
        for path in paths:
            _accumulate_file(
                path, seen_ids, win_start_unix, win_end_unix, n_buckets, buckets
            )
    return buckets


def normalize_to_full(hourly):
    """Cumulative $-curve -> 0..100 synthetic util series ending at 100.

    The user maxes the quota every week, so the window total maps to 100%.
    Returns [] if the window had no spend (cannot normalize)."""
    total = sum(hourly)
    if total <= 0:
        return []
    util = []
    running = 0.0
    for value in hourly:
        running += value
        util.append(100.0 * running / total)
    return util


def _count_reverting_flips(values, horizon):
    """Count sign changes that revert to the original sign within `horizon` steps."""
    flips = 0
    for i in range(1, len(values)):
        a, b = values[i - 1], values[i]
        if (a < 0) == (b < 0):
            continue
        look = values[i + 1 : i + 1 + horizon]
        if any((c < 0) == (a < 0) for c in look):
            flips += 1
    return flips


def score_candidate(hourly, util_series, period_seconds, params):
    """Replay project_delta hour-by-hour; return convergence/jumpiness/false-calls.

    * convergence_mae_h: mean |cumulative_delta| over the final 25% of the window,
      in hours (lower = locks onto the true ~0 landing sooner/steadier).
    * jumpiness_h: mean |delta current_rate_delta| hour-to-hour, in hours.
    * false_calls: sign flips of current_rate_delta that revert within 6 hours.
    """
    deltas_cum = []
    deltas_cur = []
    n = len(util_series)
    for t in range(1, n + 1):
        elapsed = t * _HOUR
        remaining = period_seconds - elapsed
        if remaining <= 0:
            break
        util = util_series[t - 1]
        cum, cur = project_delta(
            hourly[:t], util, elapsed, remaining, period_seconds, params
        )
        deltas_cum.append(0.0 if cum is None else cum)
        deltas_cur.append(cur)

    tail_start = int(0.75 * len(deltas_cum))
    tail = deltas_cum[tail_start:] or deltas_cum
    convergence = (
        sum(abs(d) for d in tail) / len(tail) / _HOUR if tail else float("inf")
    )

    cur_vals = [d for d in deltas_cur if d is not None]
    jumps = [abs(b - a) for a, b in pairwise(cur_vals)]
    jumpiness = (sum(jumps) / len(jumps) / _HOUR) if jumps else 0.0

    false_calls = _count_reverting_flips(cur_vals, horizon=6)

    return {
        "convergence_mae_h": convergence,
        "jumpiness_h": jumpiness,
        "false_calls": false_calls,
    }


def parse_resets(raw):
    """Comma-separated ISO timestamps -> sorted list of unix floats."""
    out = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        dt = datetime.fromisoformat(token.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        out.append(dt.timestamp())
    return sorted(out)


def iter_windows(resets, period_seconds, exclude=None):
    """Yield (win_start, win_end) for each adjacent reset pair spanning ~one
    period. Skips a pair more than half a period off-cadence (e.g. the Opus
    anomaly straddle) and any window whose start is inside the exclude range."""
    for start, end in pairwise(resets):
        if abs((end - start) - period_seconds) > 0.5 * period_seconds:
            continue
        if exclude and exclude[0] <= start < exclude[1]:
            continue
        yield start, end


def _format_table(rows):
    header = (
        f"{'candidate':<14}{'conv(h)':>10}{'jump(h)':>10}{'false':>8}{'windows':>9}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row['label']:<14}{row['convergence_mae_h']:>10.2f}"
            f"{row['jumpiness_h']:>10.2f}{row['false_calls']:>8}{row['windows']:>9}"
        )
    return "\n".join(lines)


def _aggregate_windows(windows, roots, period):
    """Score every candidate over every window that has spend.

    Returns (agg, skipped): agg maps candidate label -> summed metrics + window
    count; skipped counts windows with no spend to normalize."""
    agg = {
        c["label"]: {
            "convergence_mae_h": 0.0,
            "jumpiness_h": 0.0,
            "false_calls": 0,
            "windows": 0,
        }
        for c in _CANDIDATES
    }
    skipped = 0
    for win_start, win_end in windows:
        hourly = reconstruct_window(roots, win_start, win_end)
        util_series = normalize_to_full(hourly)
        if not util_series:
            skipped += 1
            continue
        for candidate in _CANDIDATES:
            params = {
                k: candidate[k]
                for k in ("estimator", "window_hours", "lambda", "warmup_seconds")
            }
            metrics = score_candidate(hourly, util_series, period, params)
            bucket = agg[candidate["label"]]
            bucket["convergence_mae_h"] += metrics["convergence_mae_h"]
            bucket["jumpiness_h"] += metrics["jumpiness_h"]
            bucket["false_calls"] += metrics["false_calls"]
            bucket["windows"] += 1
    return agg, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Backtest quota-pace current-rate estimators."
    )
    parser.add_argument(
        "--resets", required=True, help="comma-separated ISO reset timestamps"
    )
    parser.add_argument("--period-days", type=float, default=7.0)
    parser.add_argument(
        "--exclude",
        default="",
        help="ISO 'start,end' window to drop (the Opus anomaly)",
    )
    args = parser.parse_args(argv)

    period = args.period_days * 24 * _HOUR
    try:
        resets = parse_resets(args.resets)
    except ValueError as exc:
        parser.error(f"--resets has a malformed ISO timestamp: {exc}")
    exclude = None
    if args.exclude:
        try:
            bounds = parse_resets(args.exclude)
        except ValueError as exc:
            parser.error(f"--exclude has a malformed ISO timestamp: {exc}")
        if len(bounds) != 2:
            parser.error("--exclude must be exactly two ISO timestamps: 'start,end'")
        exclude = (bounds[0], bounds[1])
    roots = _walker_root_list()

    windows = list(iter_windows(resets, period, exclude))
    if not windows:
        print("no usable windows (need >=2 adjacent resets ~one period apart)")
        return 1

    agg, skipped = _aggregate_windows(windows, roots, period)

    rows = []
    for candidate in _CANDIDATES:
        bucket = agg[candidate["label"]]
        divisor = max(1, bucket["windows"])
        rows.append(
            {
                "label": candidate["label"],
                "convergence_mae_h": bucket["convergence_mae_h"] / divisor,
                "jumpiness_h": bucket["jumpiness_h"] / divisor,
                "false_calls": bucket["false_calls"],
                "windows": bucket["windows"],
            }
        )
    rows.sort(
        key=lambda r: (r["convergence_mae_h"], r["jumpiness_h"], r["false_calls"])
    )

    if skipped:
        print(f"# note: skipped {skipped} window(s) with no spend to normalize")
    print("# CAVEAT: util is synthetic (dollar-normalized to 100%); rankings are")
    print("# relative-only. Reset boundaries are user-supplied; anomaly excluded.")
    print(_format_table(rows))
    print(f"\n# recommended default: {rows[0]['label']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
