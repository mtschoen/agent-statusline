"""Verify the hourly in-window $-burn walk bins transcript turns by hour offset.

Writes a tiny temp transcript with assistant turns at known offsets from a
synthetic window start and asserts the returned hourly series places each turn's
cost in the right bucket (index = floor((ts - win_start)/3600)).

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.pace as pace


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


def _check_hourly_binning(failures):
    win_start = 1_700_000_000.0
    now = win_start + 3 * 3600 + 600  # 3h10m of window elapsed
    # 1M opus output tokens = $25.00 per turn.
    turns = [
        (win_start + 60, "a", 1_000_000),  # hour 0
        (win_start + 3600 + 60, "b", 1_000_000),  # hour 1
        (win_start + 3 * 3600 + 60, "c", 1_000_000),  # hour 3
    ]
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        slug_dir = os.path.join(root, "slug")
        os.makedirs(slug_dir)
        path = os.path.join(slug_dir, "sess.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for ts, mid, tok in turns:
                f.write(_line(ts, mid, tok) + "\n")

        # Pin roots + clock to the temp tree / synthetic now.
        real_roots = pace._walker_root_list
        real_now = pace._now_unix
        pace._walker_root_list = lambda: [root]
        pace._now_unix = lambda: now
        try:
            hourly = pace._walk_pace_hourly(win_start)
        finally:
            pace._walker_root_list = real_roots
            pace._now_unix = real_now

    if len(hourly) != 4:
        failures.append(f"expected 4 hourly buckets (0..3), got {len(hourly)}")
        return
    expected = [25.0, 25.0, 0.0, 25.0]
    for i, (got, want) in enumerate(zip(hourly, expected, strict=True)):
        if abs(got - want) > 1e-6:
            failures.append(f"bucket {i}: expected ${want:.2f}, got ${got:.2f}")


def check(failures):
    _check_hourly_binning(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: hourly walk bins in-window turn cost by hour offset from window start")


if __name__ == "__main__":
    main()
