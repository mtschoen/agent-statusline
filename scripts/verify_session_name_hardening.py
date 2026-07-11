"""Verify statusline.py's line-1 session-id/session-name appenders survive a
non-string value from the harness payload.

Both `_append_session_id` and `_append_session_name` do `(x or "").strip()`,
which crashes with AttributeError for any truthy non-string `x` (e.g. an int).
Antigravity CLI shares Claude Code's JSON-payload protocol but is a separate
implementation, so its payload isn't guaranteed to match Claude's field types
exactly -- these appenders must degrade instead of crashing the whole render.

statusline.py is imported directly (not just driven via subprocess) so this
runs fast and reports precise failures; importing it is safe since all
side-effecting work happens inside main()'s `__main__` guard.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text):
    return _ANSI.sub("", text)


def _check_append_session_name_non_string(failures):
    result = _strip(statusline._append_session_name("base", 123))
    if result != "base 123":
        failures.append(
            f"_append_session_name with int should coerce to str; got {result!r}"
        )


def _check_append_session_name_none(failures):
    result = statusline._append_session_name("base", None)
    if result != "base":
        failures.append(
            f"_append_session_name with None should be a no-op; got {result!r}"
        )


def _check_append_session_id_non_string(failures):
    result = _strip(statusline._append_session_id("base", 456))
    if result != "base [456]":
        failures.append(
            f"_append_session_id with int should coerce to str; got {result!r}"
        )


def _check_append_session_id_none(failures):
    result = statusline._append_session_id("base", None)
    if result != "base":
        failures.append(
            f"_append_session_id with None should be a no-op; got {result!r}"
        )


def check(failures):
    _check_append_session_name_non_string(failures)
    _check_append_session_name_none(failures)
    _check_append_session_id_non_string(failures)
    _check_append_session_id_none(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: session-id/session-name appenders survive non-string payload fields")


if __name__ == "__main__":
    main()
