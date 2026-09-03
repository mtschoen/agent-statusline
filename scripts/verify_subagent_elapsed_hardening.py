"""Verify statusline_lib/render_subagent.py's `_format_elapsed` survives a
non-numeric `startTime` from the harness payload and formats every elapsed
range.

Found by fuzzing subagent_statusline.py with Antigravity-shaped degenerate
payloads: `{"id": "abc", "startTime": "not-a-number"}` crashed
`start_time_ms <= 0` (str vs int) inside `_row_for_task`, which sits outside
`_row_for_task`'s own try/except -- the row still degraded gracefully (caught
one level up in `render_subagent_rows`'s per-task guard, dropping just that
row), but the appended-elapsed field should degrade on its own like every
other best-effort field in this module rather than taking the whole row with
it.

`_format_elapsed` takes `now` explicitly (threaded through by Task 6 of the
resident-server plan so the render is testable without touching the wall
clock) rather than reading `time.time()` internally.

statusline_lib.render_subagent is imported directly, matching
scripts/verify_session_name_hardening.py's approach for statusline.py.

Run from anywhere; imports from agent-statusline by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib import render_subagent as sub

_NOW = 1_700_000_000.0


def _check_non_numeric_start_time(failures):
    result = sub._format_elapsed("not-a-number", "running", _NOW)
    if result != "":
        failures.append(
            f"_format_elapsed with non-numeric startTime should be ''; got {result!r}"
        )


def _check_none_start_time(failures):
    if sub._format_elapsed(None, "running", _NOW) != "":
        failures.append("_format_elapsed with None startTime should be ''")


def _check_terminal_status_short_circuits(failures):
    # Terminal status returns "" before the startTime is even parsed, so a
    # bad startTime type is harmless once the task is done.
    if sub._format_elapsed("not-a-number", "completed", _NOW) != "":
        failures.append("_format_elapsed with terminal status should be ''")


def _check_numeric_string_still_works(failures):
    # A numeric string (some harnesses serialize ms timestamps as strings)
    # should still compute elapsed rather than being treated as garbage.
    start = str(int(_NOW * 1000) - 5000)
    result = sub._format_elapsed(start, "running", _NOW)
    if result != "5s":
        failures.append(
            f"_format_elapsed with numeric string startTime; got {result!r}"
        )


def _check_minutes_range_formatting(failures):
    # 125s elapsed -> "2m05s" (the seconds < 3600 branch).
    start_time_ms = _NOW * 1000 - 125_000
    result = sub._format_elapsed(start_time_ms, "running", _NOW)
    if result != "2m05s":
        failures.append(f"125s elapsed should format as '2m05s'; got {result!r}")


def _check_hours_range_formatting(failures):
    # 3661s elapsed -> "1h01m" (the >= 3600s branch).
    start_time_ms = _NOW * 1000 - 3_661_000
    result = sub._format_elapsed(start_time_ms, "running", _NOW)
    if result != "1h01m":
        failures.append(f"3661s elapsed should format as '1h01m'; got {result!r}")


def check(failures):
    _check_non_numeric_start_time(failures)
    _check_none_start_time(failures)
    _check_terminal_status_short_circuits(failures)
    _check_numeric_string_still_works(failures)
    _check_minutes_range_formatting(failures)
    _check_hours_range_formatting(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: _format_elapsed survives non-numeric startTime and formats every range")


if __name__ == "__main__":
    main()
