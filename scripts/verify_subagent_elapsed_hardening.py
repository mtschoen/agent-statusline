"""Verify subagent_statusline.py's `_format_elapsed` survives a non-numeric
`startTime` from the harness payload.

Found by fuzzing subagent_statusline.py with Antigravity-shaped degenerate
payloads: `{"id": "abc", "startTime": "not-a-number"}` crashed
`start_time_ms <= 0` (str vs int) inside `_row_for_task`, which sits outside
`_row_for_task`'s own try/except -- the row still degraded gracefully (caught
one level up in `main()`'s per-task guard, dropping just that row), but the
appended-elapsed field should degrade on its own like every other
best-effort field in this file rather than taking the whole row with it.

subagent_statusline.py is imported directly, matching
scripts/verify_session_name_hardening.py's approach for statusline.py.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subagent_statusline as sub


def _check_non_numeric_start_time(failures):
    result = sub._format_elapsed("not-a-number", "running")
    if result != "":
        failures.append(
            f"_format_elapsed with non-numeric startTime should be ''; got {result!r}"
        )


def _check_none_start_time(failures):
    if sub._format_elapsed(None, "running") != "":
        failures.append("_format_elapsed with None startTime should be ''")


def _check_terminal_status_short_circuits(failures):
    # Terminal status returns "" before the startTime is even parsed, so a
    # bad startTime type is harmless once the task is done.
    if sub._format_elapsed("not-a-number", "completed") != "":
        failures.append("_format_elapsed with terminal status should be ''")


def _check_numeric_string_still_works(failures):
    # A numeric string (some harnesses serialize ms timestamps as strings)
    # should still compute elapsed rather than being treated as garbage.
    import time

    start = str(int(time.time() * 1000) - 5000)
    result = sub._format_elapsed(start, "running")
    if result != "5s":
        failures.append(
            f"_format_elapsed with numeric string startTime; got {result!r}"
        )


def check(failures):
    _check_non_numeric_start_time(failures)
    _check_none_start_time(failures)
    _check_terminal_status_short_circuits(failures)
    _check_numeric_string_still_works(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: _format_elapsed survives non-numeric startTime")


if __name__ == "__main__":
    main()
