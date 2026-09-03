"""Verify render_subagent_rows: the subagent panel's payload -> NDJSON-row
list function, extracted out of subagent_statusline.py so it falls under the
statusline_lib coverage gate (Task 6, resident-server plan).

Drives render_subagent_rows in-process against a payload carrying a running
task, a completed task, a lead task, and a task whose agent transcript is
missing from disk -- all four are renderable and each produces one JSON row.
Two more entries are deliberately not renderable: a task with no id (returns
None, silently dropped) and a malformed entry that is not even a dict (silently
skipped by render_subagent_rows rather than logging a traceback or aborting the
whole list).

The remaining checks call the module's private helpers directly (matching the
existing verify_subagent_agent_jsonl_path.py / verify_subagent_elapsed_hardening.py
style) to reach branches that a single realistic payload cannot: the
transcript_path-missing fallback to _find_session_jsonl, the two internal
try/except guards inside _row_for_task (a metrics failure or a beacon failure
must each degrade just its own segment, not drop the row), and the outer
try/except guard inside render_subagent_rows (a failure in a valid task record
is logged and isolated).

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib import render_subagent as render

_TEXT_ENCODING = "utf-8"
_NOW = 1_700_000_000.0


def _assistant_line(message_id, model, usage):
    return json.dumps(
        {
            "message": {
                "role": "assistant",
                "id": message_id,
                "model": model,
                "usage": usage,
            }
        }
    )


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=_TEXT_ENCODING) as f:
        f.write(text)


def _build_payload(tmp):
    session_id = "lead-session-id"
    parent = os.path.join(tmp, "sess.jsonl")
    _write(
        parent,
        _assistant_line("lead1", "claude-opus-4-7", {"input_tokens": 1000}) + "\n",
    )

    sub_dir = os.path.join(tmp, "sess", "subagents")
    running_id = "running-task"
    completed_id = "completed-task"
    _write(
        os.path.join(sub_dir, f"agent-{running_id}.jsonl"),
        _assistant_line("r1", "claude-opus-4-7", {"input_tokens": 500}) + "\n",
    )
    _write(
        os.path.join(sub_dir, f"agent-{completed_id}.jsonl"),
        _assistant_line("c1", "claude-opus-4-7", {"input_tokens": 500}) + "\n",
    )

    tasks = [
        {
            "id": running_id,
            "description": "running task",
            "status": "running",
            "startTime": (_NOW - 5) * 1000,
        },
        {
            "id": completed_id,
            "description": "completed task",
            "status": "completed",
            "startTime": (_NOW - 30) * 1000,
        },
        {
            "id": session_id,
            "type": "lead",
            "description": "lead task",
            "status": "running",
            "startTime": (_NOW - 10) * 1000,
        },
        {
            "id": "missing-transcript-task",
            "description": "no transcript on disk",
            "status": "running",
            "startTime": (_NOW - 2) * 1000,
        },
        {"id": "", "description": "no id, dropped without raising"},
        "not-a-task-dict",  # malformed task entry; must be silently skipped
    ]

    return {
        "session_id": session_id,
        "transcript_path": parent,
        "tasks": tasks,
    }


def _check_renders_expected_rows_and_skips_the_rest(failures):
    with tempfile.TemporaryDirectory() as tmp:
        payload = _build_payload(tmp)
        log_calls = []
        original_log_traceback = render.log_traceback

        def fake_log_traceback(path):
            log_calls.append(path)

        render.log_traceback = fake_log_traceback
        try:
            rows = render.render_subagent_rows(payload, _NOW)
        finally:
            render.log_traceback = original_log_traceback

        if log_calls:
            failures.append(
                "a non-dict task should be skipped without logging a traceback; "
                f"got {log_calls!r}"
            )

        if len(rows) != 4:
            failures.append(f"expected 4 renderable rows, got {len(rows)}: {rows!r}")

        parsed_rows = []
        for row in rows:
            try:
                parsed = json.loads(row)
            except ValueError as exc:
                failures.append(f"row did not parse as JSON: {row!r} ({exc})")
                continue
            if "content" not in parsed:
                failures.append(f"row missing 'content' key: {parsed!r}")
            parsed_rows.append(parsed)

        ids = {row["id"] for row in parsed_rows}
        expected_ids = {
            "running-task",
            "completed-task",
            "lead-session-id",
            "missing-transcript-task",
        }
        if ids != expected_ids:
            failures.append(f"unexpected row id set: {ids!r} != {expected_ids!r}")


def _check_missing_transcript_path_falls_back_to_find_session_jsonl(failures):
    with tempfile.TemporaryDirectory() as tmp:
        parent = os.path.join(tmp, "found.jsonl")
        _write(
            parent,
            _assistant_line("f1", "claude-opus-4-7", {"input_tokens": 1000}) + "\n",
        )

        original = render._find_session_jsonl
        calls = []

        def fake_find(session_id):
            calls.append(session_id)
            return parent

        render._find_session_jsonl = fake_find
        try:
            payload = {
                "session_id": "session-without-transcript-path",
                "tasks": [
                    {
                        "id": "session-without-transcript-path",
                        "type": "lead",
                        "description": "lead via fallback lookup",
                        "status": "running",
                    }
                ],
            }
            rows = render.render_subagent_rows(payload, _NOW)
        finally:
            render._find_session_jsonl = original

        if calls != ["session-without-transcript-path"]:
            failures.append(
                f"_find_session_jsonl should be called once with the session id; got {calls!r}"
            )
        if len(rows) != 1:
            failures.append(
                f"fallback-discovered transcript should still produce a row; got {rows!r}"
            )


def _check_row_for_task_degrades_when_metrics_raise(failures):
    original = render._metrics_for_task

    def fake_metrics(*args, **kwargs):
        raise RuntimeError("synthetic metrics failure")

    render._metrics_for_task = fake_metrics
    try:
        row = render._row_for_task(
            {"id": "t1", "description": "still renders", "status": "running"},
            "",
            "",
            _NOW,
        )
    finally:
        render._metrics_for_task = original

    if row is None or "content" not in row:
        failures.append(
            f"a metrics failure should degrade the row, not drop it; got {row!r}"
        )
    elif "still renders" not in row["content"]:
        failures.append(
            f"degraded row should keep its description; got {row['content']!r}"
        )


def _check_row_for_task_degrades_when_beacon_raises(failures):
    original = render.format_beacon

    def fake_beacon(task_id):
        raise RuntimeError("synthetic beacon failure")

    render.format_beacon = fake_beacon
    try:
        row = render._row_for_task(
            {"id": "t1", "description": "still renders", "status": "running"},
            "",
            "",
            _NOW,
        )
    finally:
        render.format_beacon = original

    if row is None or "content" not in row:
        failures.append(
            f"a beacon failure should degrade the row, not drop it; got {row!r}"
        )


def _check_row_for_task_includes_a_truthy_beacon(failures):
    original = render.format_beacon

    def fake_beacon(task_id):
        return ("BEACON-TEXT", {"kind": "step"})

    render.format_beacon = fake_beacon
    try:
        row = render._row_for_task(
            {"id": "t1", "description": "has a beacon", "status": "running"},
            "",
            "",
            _NOW,
        )
    finally:
        render.format_beacon = original

    if row is None or "BEACON-TEXT" not in row["content"]:
        failures.append(f"a truthy beacon should appear in the row; got {row!r}")


def _check_live_payload_for_session(failures):
    original_log = render._MAIN_INPUT_LOG

    if render._live_payload_for_session("") is not None:
        failures.append(
            "_live_payload_for_session with a falsy session id should be None"
        )

    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "missing.json")
        malformed = os.path.join(tmp, "malformed.json")
        _write(malformed, "not json")
        mismatched = os.path.join(tmp, "mismatched.json")
        _write(mismatched, json.dumps({"session_id": "someone-else"}))
        matching = os.path.join(tmp, "matching.json")
        _write(
            matching,
            json.dumps(
                {
                    "session_id": "the-session",
                    "context_window": {"context_window_size": 200000},
                    "model": {"id": "claude-opus-4-7[1m]"},
                }
            ),
        )

        try:
            render._MAIN_INPUT_LOG = missing
            if render._live_payload_for_session("the-session") is not None:
                failures.append("a missing live payload file should return None")

            render._MAIN_INPUT_LOG = malformed
            if render._live_payload_for_session("the-session") is not None:
                failures.append("a malformed live payload file should return None")

            render._MAIN_INPUT_LOG = mismatched
            if render._live_payload_for_session("the-session") is not None:
                failures.append("a session id mismatch should return None")

            render._MAIN_INPUT_LOG = matching
            live = render._live_payload_for_session("the-session")
            if live != {"window_size": 200000, "model_id": "claude-opus-4-7[1m]"}:
                failures.append(
                    f"a matching live payload should be returned; got {live!r}"
                )
        finally:
            render._MAIN_INPUT_LOG = original_log


def _check_render_subagent_rows_logs_and_skips_when_row_for_task_raises(failures):
    original_row_for_task = render._row_for_task
    original_log_traceback = render.log_traceback
    log_calls = []

    def fake_log_traceback(path):
        log_calls.append(path)

    def fake_row_for_task(task, parent, session_id, now):
        if task.get("id") == "bad-task":
            raise RuntimeError("synthetic failure in valid task dict")
        return {"id": task["id"], "content": "valid"}

    render._row_for_task = fake_row_for_task
    render.log_traceback = fake_log_traceback
    try:
        payload = {
            "tasks": [
                {"id": "good-1"},
                {"id": "bad-task"},
                {"id": "good-2"},
            ]
        }
        rows = render.render_subagent_rows(payload, _NOW)
    finally:
        render._row_for_task = original_row_for_task
        render.log_traceback = original_log_traceback

    if not log_calls:
        failures.append(
            "render_subagent_rows should log a traceback when _row_for_task raises"
        )
    if len(rows) != 2:
        failures.append(
            f"render_subagent_rows should render remaining valid rows when one raises; got {rows!r}"
        )


def check(failures):
    _check_renders_expected_rows_and_skips_the_rest(failures)
    _check_missing_transcript_path_falls_back_to_find_session_jsonl(failures)
    _check_row_for_task_degrades_when_metrics_raise(failures)
    _check_row_for_task_degrades_when_beacon_raises(failures)
    _check_row_for_task_includes_a_truthy_beacon(failures)
    _check_render_subagent_rows_logs_and_skips_when_row_for_task_raises(failures)
    _check_live_payload_for_session(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: render_subagent_rows renders running/completed/lead/missing-transcript "
        "rows, silently skips a no-id and a malformed task, and degrades gracefully on "
        "metrics/beacon failures"
    )


if __name__ == "__main__":
    main()
