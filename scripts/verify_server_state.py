"""Verify statusline_lib.server_state: the resident server's per-session
incremental transcript walk. A session's accumulator is folded once, then on
every later render only the transcript bytes appended since the last fold are
read, so a long-running session's render cost stays flat instead of growing
with the whole transcript on every call. The core invariant under test is
that incremental folding across many small appends must exactly match one
full walk_transcript call over the finished file.
"""

import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib.cost import walk_transcript
from statusline_lib.server_state import SessionEntry, StateTables, read_appended

_ENCODING = "utf-8"

_next_message_id = 0


def _turn(inp=10, out=100, model="claude-opus-4-8", text=None):
    """One assistant-turn JSONL line with a fresh, never-repeated message id
    so every call to _write_turns/_append_turns adds distinct, undeduped
    turns even across separate files in the same test run. `text`, when
    given, is embedded raw (not JSON-escaped away): serialized with
    ensure_ascii=False so a literal non-ASCII character such as U+2028
    lands unescaped in the line on disk, the way a real transcript would."""
    global _next_message_id
    _next_message_id += 1
    usage = {
        "input_tokens": inp,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": out,
    }
    message = {
        "role": "assistant",
        "id": f"turn-{_next_message_id}",
        "model": model,
        "usage": usage,
    }
    if text is not None:
        message["content"] = [{"type": "text", "text": text}]
    entry = {"type": "assistant", "message": message}
    return json.dumps(entry, ensure_ascii=False)


def _write_turns(path, count):
    """Truncate (or create) `path` and write exactly `count` fresh turns."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=_ENCODING) as f:
        for _ in range(count):
            f.write(_turn() + "\n")


def _append_turns(path, count):
    """Append `count` fresh turns to the existing content of `path`."""
    with open(path, "a", encoding=_ENCODING) as f:
        for _ in range(count):
            f.write(_turn() + "\n")


class _FakeClock:
    """A mutable, injectable stand-in for time.time. Every test in this file
    injects it rather than reading the wall clock."""

    def __init__(self):
        self.now = 1_700_000_000.0

    def read(self):
        return self.now


def check_second_walk_reads_only_appended_bytes(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=3)
        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        first = tables.walk_for(entry)
        offset_after_first = entry.offsets[path]

        _append_turns(path, count=2)
        second = tables.walk_for(entry)

        if entry.offsets[path] <= offset_after_first:
            failures.append("the offset did not advance after appended turns")
        if second["assistant_turns"] != 5:
            failures.append(f"expected 5 turns after the append, got {second}")
        if first["assistant_turns"] != 3:
            failures.append("the first walk should have seen exactly 3 turns")


def check_incremental_walk_matches_a_full_walk(failures):
    """Two appends folded incrementally must equal one walk_transcript call
    over the finished file. This is the invariant that makes the server's
    numbers trustworthy."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=4)
        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        tables.walk_for(entry)
        _append_turns(path, count=3)
        incremental = tables.walk_for(entry)
        if incremental != walk_transcript(path, include_subagents=True):
            failures.append("incremental totals diverged from a full walk")


def check_a_partial_trailing_line_is_not_consumed(failures):
    """A transcript being written to can end mid-line. The offset must stop
    at the last newline so the partial line is folded once, later, whole."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=2)
        with open(path, "a", encoding=_ENCODING) as f:
            f.write('{"type": "assistant", "mess')
        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        walked = tables.walk_for(entry)
        if walked["assistant_turns"] != 2:
            failures.append("a partial trailing line must not be folded")
        if entry.offsets[path] != os.path.getsize(path) - len(
            '{"type": "assistant", "mess'
        ):
            failures.append("the offset must stop at the last complete newline")


def check_a_truncated_transcript_triggers_one_rewalk(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=5)
        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        tables.walk_for(entry)
        _write_turns(path, count=1)  # truncate and rewrite
        walked = tables.walk_for(entry)
        if walked["assistant_turns"] != 1:
            failures.append(f"a shrunk file must be rewalked from zero: {walked}")
        if entry.rewalk_count != 1:
            failures.append(f"expected exactly one rewalk, got {entry.rewalk_count}")


def check_subagent_transcripts_are_walked_incrementally_too(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=2)
        subagents = os.path.join(tmp, "session", "subagents")
        os.makedirs(subagents)
        agent = os.path.join(subagents, "agent-one.jsonl")
        _write_turns(agent, count=2)

        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        first = tables.walk_for(entry)
        _append_turns(agent, count=2)
        second = tables.walk_for(entry)

        if first["assistant_turns"] != 4:
            failures.append(f"parent plus subagent turns should be 4: {first}")
        if second["assistant_turns"] != 6:
            failures.append(f"appended subagent turns were missed: {second}")
        if second["parent_cost"] != first["parent_cost"]:
            failures.append("subagent turns must never move the parent cost")
        if second["subagent_cost"] <= first["subagent_cost"]:
            failures.append("appended subagent turns must raise the subagent cost")


def check_a_truncated_subagent_transcript_triggers_one_rewalk(failures):
    """A subagent transcript that shrinks independently of the parent must
    invalidate the whole session's fold state, not just its own offset: the
    shared accumulator and seen_ids already carry the subagent's old
    contribution, so re-folding it from zero on top would double-count it.
    Mirrors check_a_truncated_transcript_triggers_one_rewalk but for a
    subagent file, and checks the full summary against a from-scratch
    walk_transcript call rather than just the turn count."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=2)
        subagents = os.path.join(tmp, "session", "subagents")
        os.makedirs(subagents)
        agent = os.path.join(subagents, "agent-one.jsonl")
        _write_turns(agent, count=5)

        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        tables.walk_for(entry)

        _write_turns(agent, count=1)  # truncate and rewrite the subagent file
        walked = tables.walk_for(entry)

        expected = walk_transcript(path, include_subagents=True)
        if walked != expected:
            failures.append(
                "a truncated subagent transcript must rebuild the whole"
                f" session to match a full walk: {walked} != {expected}"
            )
        if entry.rewalk_count != 1:
            failures.append(
                f"a subagent shrink must trigger exactly one rewalk, got {entry.rewalk_count}"
            )


def check_incremental_walk_survives_a_unicode_line_separator(failures):
    """JSON permits a raw U+2028/U+2029 inside a string. str.splitlines()
    treats those (and \\v, \\f, \\x1c-\\x1e, \\x85) as line boundaries, unlike
    walk_transcript's own text-mode file iteration; read_appended must not
    fragment a line on one either, or the line is torn in two and dropped."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=2)
        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        tables.walk_for(entry)

        with open(path, "a", encoding=_ENCODING) as f:
            f.write(_turn(text="before\u2028after") + "\n")
        incremental = tables.walk_for(entry)

        expected = walk_transcript(path, include_subagents=True)
        if incremental != expected:
            failures.append(
                "a raw U+2028 inside an appended line diverged the incremental"
                f" walk from a full walk_transcript call: {incremental} != {expected}"
            )
        if incremental["assistant_turns"] != 3:
            failures.append(
                f"the U+2028-bearing turn should still count as exactly one turn: {incremental}"
            )


def check_touch_session_reuses_the_same_entry(failures):
    """Calling touch_session twice for the same session id must return the
    same SessionEntry (and update last_seen), not silently replace state."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=1)
        clock = _FakeClock()
        tables = StateTables(clock=clock.read)
        first = tables.touch_session("session-a", path)
        clock.now += 5
        second = tables.touch_session("session-a", path)
        if first is not second:
            failures.append("touch_session must reuse the existing SessionEntry")
        if second.last_seen != clock.now:
            failures.append("touch_session must refresh last_seen on the clock")


def check_read_appended_reports_no_rewalk_on_missing_file(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "does-not-exist.jsonl")
        lines, offset, rewalked = read_appended(path, 0)
        if lines != [] or offset != 0 or rewalked:
            failures.append(
                f"a missing file must read as empty and not a rewalk: {(lines, offset, rewalked)}"
            )


def check_read_appended_handles_an_open_failure(failures):
    """getsize can succeed and then the open still fail (permissions, a
    concurrent delete). That must degrade to no new lines, not raise."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=1)
        with mock.patch(
            "statusline_lib.server_state.open",
            side_effect=OSError("simulated open failure"),
            create=True,
        ):
            lines, offset, rewalked = read_appended(path, 0)
        if lines != [] or offset != 0 or rewalked:
            failures.append(
                "an open failure mid-read must return no lines, offset unchanged,"
                f" no rewalk: {(lines, offset, rewalked)}"
            )


def check_read_appended_treats_a_lineless_partial_write_as_nothing_new(failures):
    """A file that has grown but still holds no complete line yet (the very
    first bytes of a turn being written) must yield nothing to fold."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        with open(path, "w", encoding=_ENCODING) as f:
            f.write('{"type": "assistant", "no newline yet')
        lines, offset, rewalked = read_appended(path, 0)
        if lines != [] or offset != 0 or rewalked:
            failures.append(
                "a file with no complete line yet must read as empty:"
                f" {(lines, offset, rewalked)}"
            )


def check_walk_for_skips_subagent_lookup_for_a_non_jsonl_transcript(failures):
    """A transcript path that does not end in .jsonl (should never happen in
    production, but the guard exists) must not be walked for subagents."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl.bak")
        _write_turns(path, count=1)
        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        walked = tables.walk_for(entry)
        if walked["assistant_turns"] != 1:
            failures.append(
                f"a non-.jsonl transcript should still fold its own turns: {walked}"
            )


def check_session_entry_has_no_unused_last_reply_field(failures):
    entry = SessionEntry("session", "session.jsonl", lambda: 1.0)
    if hasattr(entry, "last_reply"):
        failures.append("SessionEntry must not retain the unused last_reply field")


def check(failures):
    check_second_walk_reads_only_appended_bytes(failures)
    check_incremental_walk_matches_a_full_walk(failures)
    check_a_partial_trailing_line_is_not_consumed(failures)
    check_a_truncated_transcript_triggers_one_rewalk(failures)
    check_a_truncated_subagent_transcript_triggers_one_rewalk(failures)
    check_incremental_walk_survives_a_unicode_line_separator(failures)
    check_subagent_transcripts_are_walked_incrementally_too(failures)
    check_touch_session_reuses_the_same_entry(failures)
    check_read_appended_reports_no_rewalk_on_missing_file(failures)
    check_read_appended_handles_an_open_failure(failures)
    check_read_appended_treats_a_lineless_partial_write_as_nothing_new(failures)
    check_walk_for_skips_subagent_lookup_for_a_non_jsonl_transcript(failures)
    check_session_entry_has_no_unused_last_reply_field(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: server state tables verified")


if __name__ == "__main__":
    main()
