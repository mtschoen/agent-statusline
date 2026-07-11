"""Verify the hourly in-window $-burn walk bins transcript turns by hour offset.

Writes a tiny temp transcript with assistant turns at known offsets from a
synthetic window start and asserts the returned hourly series places each turn's
cost in the right bucket (index = floor((ts - win_start)/3600)).

Also covers _parse_pace_line edge cases (blank/malformed/non-assistant/no-ts/
bad-ts/duplicate-id/too-old), _pace_hourly_for_file OSError path,
_walk_pace_hourly empty-roots and empty-groups branches, and the parallel path
(>2 groups). _discover_pace_groups checks live in verify_pace_walk.py.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from unittest.mock import patch

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
            # A non-assistant line the file walk must skip (the continue branch)
            f.write(json.dumps({"message": {"role": "user"}}) + "\n")

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


def _check_parse_pace_line_edge_cases(failures):
    """Cover the skip/return-None branches of _parse_pace_line."""
    win_start = 1_700_000_000.0
    seen = set()

    # Blank line -> None (line 46)
    result = pace._parse_pace_line(b"   \n", seen, win_start)
    if result is not None:
        failures.append(f"blank line should return None, got {result!r}")

    # Malformed JSON -> None (lines 49-50)
    result = pace._parse_pace_line(b"not-json\n", seen, win_start)
    if result is not None:
        failures.append(f"malformed JSON should return None, got {result!r}")

    # Non-assistant role -> None
    non_assistant = json.dumps(
        {"message": {"role": "user"}, "timestamp": "2023-01-01T00:00:00Z"}
    )
    result = pace._parse_pace_line(non_assistant.encode(), seen, win_start)
    if result is not None:
        failures.append(f"user role should return None, got {result!r}")

    # No timestamp -> None (line 61)
    no_ts = json.dumps({"message": {"role": "assistant", "id": "x1"}})
    result = pace._parse_pace_line(no_ts.encode(), seen, win_start)
    if result is not None:
        failures.append(f"missing timestamp should return None, got {result!r}")

    # Bad timestamp format -> None (lines 64-65)
    bad_ts = json.dumps(
        {"message": {"role": "assistant", "id": "x2"}, "timestamp": "not-a-date"}
    )
    result = pace._parse_pace_line(bad_ts.encode(), seen, win_start)
    if result is not None:
        failures.append(f"bad timestamp should return None, got {result!r}")

    # Too old (ts < earliest) -> None (line 67)
    too_old = json.dumps(
        {
            "message": {"role": "assistant", "id": "x3"},
            "timestamp": "2000-01-01T00:00:00Z",
        }
    )
    result = pace._parse_pace_line(too_old.encode(), seen, win_start)
    if result is not None:
        failures.append(f"too-old timestamp should return None, got {result!r}")

    # Duplicate message id -> None (line 57)
    good_ts = (
        datetime.fromtimestamp(win_start + 60, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    good_line = json.dumps(
        {"message": {"role": "assistant", "id": "dup1"}, "timestamp": good_ts}
    )
    seen2 = set()
    result1 = pace._parse_pace_line(good_line.encode(), seen2, win_start)
    if result1 is None:
        failures.append("first parse of good line should succeed")
    result2 = pace._parse_pace_line(good_line.encode(), seen2, win_start)
    if result2 is not None:
        failures.append(f"duplicate id should return None, got {result2!r}")

    # Valid line with no id (id branch not taken) -> tuple
    no_id_line = json.dumps({"message": {"role": "assistant"}, "timestamp": good_ts})
    result3 = pace._parse_pace_line(no_id_line.encode(), seen, win_start)
    if not isinstance(result3, tuple):
        failures.append(f"no-id assistant line should return tuple, got {result3!r}")


def _check_parse_pace_line_truncated_id_not_poisoned(failures):
    """A truncated line (same id, no/bad timestamp) must not mark the id as
    seen -- otherwise the later complete line with that id is wrongly dropped
    as a duplicate, silently undercounting spend."""
    win_start = 1_700_000_000.0
    seen = set()

    truncated = json.dumps({"message": {"role": "assistant", "id": "trunc1"}})
    result_truncated = pace._parse_pace_line(truncated.encode(), seen, win_start)
    if result_truncated is not None:
        failures.append(
            f"truncated line (no timestamp) should return None, got {result_truncated!r}"
        )

    good_ts = (
        datetime.fromtimestamp(win_start + 60, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )
    complete = json.dumps(
        {"message": {"role": "assistant", "id": "trunc1"}, "timestamp": good_ts}
    )
    result_complete = pace._parse_pace_line(complete.encode(), seen, win_start)
    if result_complete is None:
        failures.append(
            "complete line following a truncated same-id line must not be "
            "dropped as a duplicate (id poisoned by the truncated line)"
        )


def _check_pace_hourly_for_file_oserror(failures):
    """Cover the OSError return in _pace_hourly_for_file (lines 117-118)."""
    result = pace._pace_hourly_for_file(
        "/nonexistent/path/that/cannot/exist.jsonl", set(), 1_700_000_000.0, 4
    )
    if result != [0.0, 0.0, 0.0, 0.0]:
        failures.append(
            f"OSError opening file should return zero buckets, got {result!r}"
        )


def _check_walk_pace_hourly_empty_roots(failures):
    """Cover the empty-roots early return (line 178)."""
    real_roots = pace._walker_root_list
    pace._walker_root_list = list
    try:
        result = pace._walk_pace_hourly(1_700_000_000.0)
    finally:
        pace._walker_root_list = real_roots
    if result != []:
        failures.append(f"empty roots should return [], got {result!r}")


def _check_walk_pace_hourly_empty_groups(failures):
    """Cover the empty-groups zero-buckets return (line 183)."""
    win_start = 1_700_000_000.0
    now = win_start + 3600

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        os.makedirs(root)
        # Root exists but has no .jsonl files under slug dirs -> groups is empty
        real_roots = pace._walker_root_list
        real_now = pace._now_unix
        pace._walker_root_list = lambda: [root]
        pace._now_unix = lambda: now
        try:
            result = pace._walk_pace_hourly(win_start)
        finally:
            pace._walker_root_list = real_roots
            pace._now_unix = real_now

    # Should get zero buckets (at least 1 bucket), all zeros
    if not result or any(v != 0.0 for v in result):
        failures.append(f"empty groups should return all-zero buckets, got {result!r}")


def _check_walk_pace_hourly_parallel(failures):
    """Cover the parallel branch (line 186) by placing >2 session groups."""
    win_start = 1_700_000_000.0
    now = win_start + 3600 + 600

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        # Create 3 sessions so len(groups) > 2 -> parallel path
        good_ts = (
            datetime.fromtimestamp(win_start + 60, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
        for session_index in range(3):
            slug_dir = os.path.join(root, f"slug{session_index}")
            os.makedirs(slug_dir)
            path = os.path.join(slug_dir, f"sess{session_index}.jsonl")
            turn = json.dumps(
                {
                    "timestamp": good_ts,
                    "message": {
                        "role": "assistant",
                        "id": f"par{session_index}",
                        "model": "claude-opus-4-8",
                        "usage": {"output_tokens": 1_000_000},
                    },
                }
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(turn + "\n")

        real_roots = pace._walker_root_list
        real_now = pace._now_unix
        pace._walker_root_list = lambda: [root]
        pace._now_unix = lambda: now
        try:
            result = pace._walk_pace_hourly(win_start)
        finally:
            pace._walker_root_list = real_roots
            pace._now_unix = real_now

    # 3 sessions x $25 each = $75 in hour 0
    if not result or abs(result[0] - 75.0) > 1e-4:
        failures.append(f"parallel walk: expected $75 in bucket 0, got {result!r}")


def _check_walk_hourly_parallel_oserror_fallback(failures):
    """Cover the OSError/RuntimeError fallback to inline in _walk_hourly_parallel (lines 164-165)."""
    win_start = 1_700_000_000.0
    n_buckets = 2
    # Build a small groups dict with one real entry so inline fallback has something to sum
    good_ts = (
        datetime.fromtimestamp(win_start + 60, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        slug_dir = os.path.join(root, "slug0")
        os.makedirs(slug_dir)
        path = os.path.join(slug_dir, "sess0.jsonl")
        turn = json.dumps(
            {
                "timestamp": good_ts,
                "message": {
                    "role": "assistant",
                    "id": "fb0",
                    "model": "claude-opus-4-8",
                    "usage": {"output_tokens": 1_000_000},
                },
            }
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(turn + "\n")

        groups = pace._discover_pace_groups([root], win_start)

        # Patch ProcessPoolExecutor to raise OSError, forcing the fallback
        # branch. pace.py imports it inside the function at call time, so
        # patch the source module attribute, not a pace-module name. Must run
        # inside the tempdir context so the inline fallback can read the file.
        class _RaisingExecutor:
            def __init__(self, *args, **kwargs):
                raise OSError("injected pool error")

        with patch("concurrent.futures.ProcessPoolExecutor", _RaisingExecutor):
            result = pace._walk_hourly_parallel(groups, win_start, n_buckets)

    if result is None or len(result) != n_buckets:
        failures.append(
            f"parallel OSError fallback should return {n_buckets}-bucket list, got {result!r}"
        )
    if abs(result[0] - 25.0) > 1e-4:
        failures.append(
            f"parallel OSError fallback: expected $25 in bucket 0, got {result[0]!r}"
        )


def _check_walk_hourly_parallel_worker_failure(failures):
    """Cover the per-future except -> continue in _walk_hourly_parallel: a group
    whose worker raises is skipped (zeroes out) while good groups still count."""
    win_start = 1_700_000_000.0
    n_buckets = 2
    good_ts = (
        datetime.fromtimestamp(win_start + 60, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        slug_dir = os.path.join(root, "slugw")
        os.makedirs(slug_dir)
        path = os.path.join(slug_dir, "sessw.jsonl")
        turn = json.dumps(
            {
                "timestamp": good_ts,
                "message": {
                    "role": "assistant",
                    "id": "wf0",
                    "model": "claude-opus-4-8",
                    "usage": {"output_tokens": 1_000_000},
                },
            }
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(turn + "\n")

        groups = pace._discover_pace_groups([root], win_start)
        # Add a poisoned group: paths=None makes the worker raise TypeError
        # (iterating None), so fut.result() raises and the except skips it.
        groups[("slugbad", "sessbad")] = None
        result = pace._walk_hourly_parallel(groups, win_start, n_buckets)

    if result is None or len(result) != n_buckets:
        failures.append(
            f"worker failure: expected {n_buckets}-bucket list, got {result!r}"
        )
        return
    if abs(result[0] - 25.0) > 1e-4:
        failures.append(
            f"worker failure: good group should still count $25, got {result[0]!r}"
        )


def check(failures):
    _check_hourly_binning(failures)
    _check_parse_pace_line_edge_cases(failures)
    _check_parse_pace_line_truncated_id_not_poisoned(failures)
    _check_pace_hourly_for_file_oserror(failures)
    _check_walk_pace_hourly_empty_roots(failures)
    _check_walk_pace_hourly_empty_groups(failures)
    _check_walk_pace_hourly_parallel(failures)
    _check_walk_hourly_parallel_oserror_fallback(failures)
    _check_walk_hourly_parallel_worker_failure(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: hourly walk bins turns correctly; edge cases and parallel/fallback paths covered"
    )


if __name__ == "__main__":
    main()
