"""Verify `debounce_session_count` — the dwell-based suppressor that keeps the
`[N sessions]` badge quiet during a restart handoff (an old `claude` process
still winding down while a new one spins up).

The statusline only re-renders when a turn is processed, so the dwell is
measured in wall-clock time (compare `now` against a stored "first elevated"
timestamp), never in render counts. State is keyed by cwd in a small JSON file
and is injectable here via `state_path`/`now` so the tests need no real clock
or real `~/.claude` file.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib import _SESSION_DEBOUNCE_DWELL_SECONDS, debounce_session_count

DWELL = _SESSION_DEBOUNCE_DWELL_SECONDS
CWD = os.path.normcase(r"C:\Users\mtsch\liminal")


def fresh_state_path(tmp):
    return os.path.join(tmp, "debounce.json")


def write_state(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def check(failures):
    with tempfile.TemporaryDirectory() as tmp:
        sp = fresh_state_path(tmp)

        # Case 1: count below the warn threshold passes straight through.
        if debounce_session_count(0, CWD, now=1000.0, state_path=sp) != 0:
            failures.append("raw 0 should pass through as 0")
        if debounce_session_count(1, CWD, now=1000.0, state_path=sp) != 1:
            failures.append("raw 1 should pass through as 1")

        # Case 2: first time the count is elevated -> suppressed (reported as 1)
        # so a momentary handoff never paints the badge.
        if debounce_session_count(2, CWD, now=1000.0, state_path=sp) != 1:
            failures.append("first elevated render should be suppressed (1)")

        # Case 3: still inside the dwell window -> still suppressed.
        if debounce_session_count(2, CWD, now=1000.0 + DWELL - 1, state_path=sp) != 1:
            failures.append("elevated within dwell should stay suppressed (1)")

        # Case 4: once the elevated count has persisted >= dwell, show the truth.
        if debounce_session_count(2, CWD, now=1000.0 + DWELL, state_path=sp) != 2:
            failures.append("elevated past dwell should report real count (2)")

        # Case 5: a higher real count surfaces too, once dwelled.
        if debounce_session_count(3, CWD, now=1000.0 + DWELL + 5, state_path=sp) != 3:
            failures.append("elevated past dwell should report real count (3)")

    with tempfile.TemporaryDirectory() as tmp:
        sp = fresh_state_path(tmp)
        # Case 6: a transient that elevates, clears, then re-elevates must
        # RE-ARM the dwell — i.e. dropping to 1 resets the timer, so the second
        # blip is suppressed again rather than shown instantly.
        debounce_session_count(2, CWD, now=2000.0, state_path=sp)  # elevate
        if debounce_session_count(1, CWD, now=2005.0, state_path=sp) != 1:  # clear
            failures.append("drop to 1 should pass through as 1")
        if debounce_session_count(2, CWD, now=2006.0, state_path=sp) != 1:
            failures.append("re-elevation after a clear should be suppressed again")

    with tempfile.TemporaryDirectory() as tmp:
        sp = fresh_state_path(tmp)
        # Case 7: a STALE 'elevated' stamp left by a prior session (the clearing
        # render was never observed because the bar updates lazily) must NOT make
        # a fresh blip show immediately. A long gap since the last elevated
        # observation re-arms the dwell.
        write_state(sp, {CWD: {"first": 3000.0, "last": 3000.0}})
        if debounce_session_count(2, CWD, now=3000.0 + 100000, state_path=sp) != 1:
            failures.append(
                "stale elevated stamp after a long gap should re-arm (suppress)"
            )

    with tempfile.TemporaryDirectory() as tmp:
        sp = fresh_state_path(tmp)
        # Case 8: a corrupt state file must not crash and must behave as fresh.
        with open(sp, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        if debounce_session_count(2, CWD, now=4000.0, state_path=sp) != 1:
            failures.append("corrupt state file should behave as fresh (suppress)")

    # Case 9: no cwd -> cannot key state, so report the truth unchanged.
    if debounce_session_count(2, "", now=5000.0) != 2:
        failures.append("empty cwd should pass real count through (cannot debounce)")


def main():
    failures = []
    check(failures)
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: debounce_session_count suppresses handoff blips and surfaces real sessions"
    )


if __name__ == "__main__":
    main()
