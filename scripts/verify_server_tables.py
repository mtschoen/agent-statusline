"""Verify the resident server's lifetime tables in statusline_lib.server_state:
the per-cwd table, the idle drops that bound both tables, and the status
summary.

Task 8's per-session table tracks transcript fold state. This script covers
what keeps both tables from growing without limit inside a server process
that outlives every session it has ever served: a session or a working
directory unseen for SESSION_DROP_SECONDS / CWD_DROP_SECONDS is dropped, so
the server's memory and its scheduled refresh set follow the machine's live
sessions rather than everything it has ever seen.

A per-cwd entry deliberately carries nothing but its name and its last-seen
stamp. The values a cwd stands for (git ref, working-tree badge, session
count) already live in their own on-disk TTL caches that the render reads
directly; the table's only job is lifetime.

Also covered here: a session id reused against a different transcript file
must start its fold state clean, or the new file's turns fold on top of the
old file's accumulator.

Every clock in this file is injected. No test reads the wall clock.
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib.server_state import (
    CWD_DROP_SECONDS,
    SESSION_DROP_SECONDS,
    StateTables,
)

_ENCODING = "utf-8"

_next_message_id = 0


def _turn():
    """One assistant-turn JSONL line with a fresh, never-repeated message id,
    so turns written to different files are never deduped against each
    other."""
    global _next_message_id
    _next_message_id += 1
    message = {
        "role": "assistant",
        "id": f"turn-{_next_message_id}",
        "model": "claude-opus-4-8",
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 100,
        },
    }
    return json.dumps({"type": "assistant", "message": message})


def _write_turns(path, count):
    """Truncate (or create) `path` and write exactly `count` fresh turns."""
    with open(path, "w", encoding=_ENCODING) as f:
        for _ in range(count):
            f.write(_turn() + "\n")


class _FakeClock:
    """A mutable, injectable stand-in for time.time."""

    def __init__(self):
        self.now = 1_700_000_000.0

    def read(self):
        return self.now


def check_drop_windows_are_one_hour(failures):
    """The plan's constant: one hour after the last referencing render."""
    if SESSION_DROP_SECONDS != 3600:
        failures.append(f"SESSION_DROP_SECONDS should be 3600: {SESSION_DROP_SECONDS}")
    if CWD_DROP_SECONDS != 3600:
        failures.append(f"CWD_DROP_SECONDS should be 3600: {CWD_DROP_SECONDS}")


def check_an_unseen_session_is_dropped_after_an_hour(failures):
    clock = _FakeClock()
    tables = StateTables(clock=clock.read)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=1)
        tables.touch_session("session-a", path)
        tables.touch_cwd("/repo-a")
        clock.now += 3599
        if tables.drop_idle() != (0, 0):
            failures.append("nothing may be dropped one second before the hour")
        clock.now += 2
        if tables.drop_idle() != (1, 1):
            failures.append("both tables must drop their idle entry past the hour")
        if tables.summary() != {"sessions": 0, "cwds": 0, "rewalks": 0}:
            failures.append(f"dropped tables must be empty: {tables.summary()}")


def check_touching_a_session_keeps_it_alive(failures):
    clock = _FakeClock()
    tables = StateTables(clock=clock.read)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=1)
        tables.touch_session("session-a", path)
        for _ in range(4):
            clock.now += 1800
            tables.touch_session("session-a", path)
            tables.drop_idle()
        if tables.summary()["sessions"] != 1:
            failures.append("a session touched every 30 minutes must never drop")


def check_touching_a_cwd_keeps_it_alive(failures):
    """The cwd table is refreshed by renders the same way, and re-touching an
    existing cwd must refresh it in place rather than add a second entry."""
    clock = _FakeClock()
    tables = StateTables(clock=clock.read)
    first = tables.touch_cwd("/repo-a")
    if first.cwd != os.path.normcase("/repo-a") or first.last_seen != 1_700_000_000.0:
        failures.append(f"a new cwd entry must record cwd and last_seen: {first.cwd}")
    for _ in range(4):
        clock.now += 1800
        again = tables.touch_cwd("/repo-a")
        tables.drop_idle()
        if again is not first:
            failures.append("re-touching a cwd must reuse its entry")
    if first.last_seen != clock.now:
        failures.append(f"touch_cwd must restamp last_seen: {first.last_seen}")
    if tables.known_cwds() != [os.path.normcase("/repo-a")]:
        failures.append(
            f"one cwd touched five times is one entry: {tables.known_cwds()}"
        )


def check_known_cwds_lists_every_live_directory(failures):
    """The session-count refresher is handed this whole list as one job, so
    it must hold every live directory and nothing that has been dropped."""
    clock = _FakeClock()
    tables = StateTables(clock=clock.read)
    tables.touch_cwd("/repo-a")
    tables.touch_cwd("/repo-b")
    clock.now += 3000
    tables.touch_cwd("/repo-b")
    tables.touch_cwd("/repo-c")
    expected = [
        os.path.normcase("/repo-a"),
        os.path.normcase("/repo-b"),
        os.path.normcase("/repo-c"),
    ]
    if tables.known_cwds() != expected:
        failures.append(f"known_cwds must list every live cwd: {tables.known_cwds()}")
    clock.now += 1000
    if tables.drop_idle() != (0, 1):
        failures.append("only the cwd unseen for an hour may be dropped")
    expected_after = [
        os.path.normcase("/repo-b"),
        os.path.normcase("/repo-c"),
    ]
    if tables.known_cwds() != expected_after:
        failures.append(f"a dropped cwd must leave known_cwds: {tables.known_cwds()}")


def check_a_new_transcript_path_resets_the_session_entry(failures):
    """A session id reused against a different transcript file must not fold
    the new file's turns into the previous file's accumulator."""
    with tempfile.TemporaryDirectory() as tmp:
        first_path = os.path.join(tmp, "first.jsonl")
        second_path = os.path.join(tmp, "second.jsonl")
        _write_turns(first_path, count=5)
        _write_turns(second_path, count=2)
        tables = StateTables(clock=_FakeClock().read)
        tables.walk_for(tables.touch_session("session-a", first_path))
        walked = tables.walk_for(tables.touch_session("session-a", second_path))
        if walked["assistant_turns"] != 2:
            failures.append(f"a new transcript path must start clean: {walked}")
        if tables.summary()["rewalks"] != 1:
            failures.append(
                f"the reset must be counted as a rewalk: {tables.summary()}"
            )


def check_summary_reports_both_tables(failures):
    """The status request kind renders this dict, so it must count both
    tables plus the rewalks the server has had to pay."""
    clock = _FakeClock()
    tables = StateTables(clock=clock.read)
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("a", "b"):
            path = os.path.join(tmp, f"{name}.jsonl")
            _write_turns(path, count=1)
            tables.touch_session(f"session-{name}", path)
        tables.touch_cwd("/repo-a")
        summary = tables.summary()
    if summary != {"sessions": 2, "cwds": 1, "rewalks": 0}:
        failures.append(f"unexpected summary: {summary}")


def check_touch_cwd_normcases_its_key(failures):
    clock = _FakeClock()
    tables = StateTables(clock=clock.read)
    with patch(
        "statusline_lib.server_state.os.path.normcase",
        side_effect=lambda path: path.lower(),
    ):
        first = tables.touch_cwd("C:\\Repo")
        clock.now += 1
        second = tables.touch_cwd("c:\\repo")
    if second is not first:
        failures.append("case-equivalent Windows cwd spellings must share one entry")
    if tables.known_cwds() != ["c:\\repo"]:
        failures.append(
            f"known_cwds must expose one canonical cwd: {tables.known_cwds()}"
        )
    if first.cwd != "c:\\repo" or first.last_seen != clock.now:
        failures.append(f"the canonical cwd entry must be restamped: {first.cwd!r}")


def check_rewalk_summary_survives_session_eviction(failures):
    clock = _FakeClock()
    tables = StateTables(clock=clock.read)
    with tempfile.TemporaryDirectory() as tmp:
        first_path = os.path.join(tmp, "first.jsonl")
        second_path = os.path.join(tmp, "second.jsonl")
        _write_turns(first_path, count=1)
        _write_turns(second_path, count=1)
        tables.touch_session("session-a", first_path)
        tables.touch_session("session-a", second_path)
        before_drop = tables.summary()["rewalks"]
        clock.now += SESSION_DROP_SECONDS
        tables.drop_idle()
        after_drop = tables.summary()["rewalks"]
    if (before_drop, after_drop) != (1, 1):
        failures.append(
            f"server-lifetime rewalks must survive session eviction: "
            f"{(before_drop, after_drop)!r}"
        )


def check(failures):
    check_drop_windows_are_one_hour(failures)
    check_an_unseen_session_is_dropped_after_an_hour(failures)
    check_touching_a_session_keeps_it_alive(failures)
    check_touching_a_cwd_keeps_it_alive(failures)
    check_known_cwds_lists_every_live_directory(failures)
    check_a_new_transcript_path_resets_the_session_entry(failures)
    check_summary_reports_both_tables(failures)
    check_touch_cwd_normcases_its_key(failures)
    check_rewalk_summary_survives_session_eviction(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: per-cwd table, idle drops, and the status summary verified")


if __name__ == "__main__":
    main()
