"""Verify the backtest harness reconstruction + scoring on a synthetic corpus.

Writes a temp transcript spanning a single known window, then checks that
reconstruct_window bins the spend into an hourly series whose length matches the
window and whose sum equals the total turn cost; normalize_to_full turns the
cumulative curve into a 0..100 synthetic-util series ending at exactly 100; and
score_candidate runs project_delta across the window and returns finite
convergence / jumpiness / false-call metrics.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import math
import os
import sys
import tempfile
from datetime import UTC, datetime
from itertools import pairwise

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scripts.backtest_pace as bt
from statusline_lib.project import DEFAULT_PARAMS

_HOUR = 3600.0


def _line(ts_unix, message_id, output_tokens):
    return json.dumps(
        {
            "timestamp": datetime.fromtimestamp(ts_unix, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "message": {
                "role": "assistant",
                "id": message_id,
                "model": "claude-opus-4-8",
                "usage": {"output_tokens": output_tokens},
            },
        }
    )


def _check_reconstruct_and_score(failures):
    period = 7 * 24 * _HOUR
    win_start = 1_700_000_000.0
    win_end = win_start + period
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects", "slug")
        os.makedirs(root)
        path = os.path.join(root, "sess.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            # one $25 turn every 24h -> 7 turns, total $175.
            for day in range(7):
                f.write(
                    _line(win_start + day * 24 * _HOUR + 60, f"d{day}", 1_000_000)
                    + "\n"
                )

        hourly = bt.reconstruct_window(
            [os.path.join(tmp, "projects")], win_start, win_end
        )
        if len(hourly) != int(period // 3600):
            failures.append(
                f"hourly length {len(hourly)} != {int(period // 3600)} window hours"
            )
        if abs(sum(hourly) - 175.0) > 1e-6:
            failures.append(f"window total should be $175.00, got ${sum(hourly):.2f}")

        util_series = bt.normalize_to_full(hourly)
        if not util_series or abs(util_series[-1] - 100.0) > 1e-6:
            failures.append(
                f"normalized util should end at 100, got {util_series[-1] if util_series else 'EMPTY'}"
            )
        if any(b - a < -1e-9 for a, b in pairwise(util_series)):
            failures.append("normalized util must be monotonically non-decreasing")

        metrics = bt.score_candidate(hourly, util_series, period, DEFAULT_PARAMS)
        for key in ("convergence_mae_h", "jumpiness_h", "false_calls"):
            if key not in metrics or not math.isfinite(metrics[key]):
                failures.append(f"score_candidate missing/non-finite metric: {key}")


def _check_window_segmentation(failures):
    # Four weekly resets => three candidate windows; exclude drops the middle one.
    period = 7 * 24 * _HOUR
    r0 = 1_700_000_000.0
    resets = [r0, r0 + period, r0 + 2 * period, r0 + 3 * period]
    windows = list(
        bt.iter_windows(
            resets, period_seconds=period, exclude=(r0 + period, r0 + 2 * period)
        )
    )
    starts = [w[0] for w in windows]
    if (r0 + period) in starts:
        failures.append(
            "excluded window (start inside exclude range) should not be yielded"
        )
    for start, end in windows:
        if abs((end - start) - period) > 1e-6:
            failures.append(f"window {start}->{end} should span exactly one period")
    if not windows:
        failures.append("should still yield the non-excluded windows")


def check(failures):
    _check_reconstruct_and_score(failures)
    _check_window_segmentation(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: backtest reconstruct + normalize + score produce sane, finite metrics")


if __name__ == "__main__":
    main()
