"""Verify that `_find_beacon_anchors` treats `kind=end` as a hard lifecycle
terminator: after an `end`, a `report` with no intervening `begin` must
NOT keep the prior begin as the turn anchor.

Run from anywhere; imports from `schoen-claude-status` package by path.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from statusline_lib import _find_beacon_anchors


def write_jsonl(path, events):
    with open(path, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")


def mk_assistant(ts, beacon_kind, summary="x", eta=60):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "<progress-beacon>\n"
                        + json.dumps(
                            {
                                "kind": beacon_kind,
                                "eta_seconds": eta,
                                "summary": summary,
                                "drift": "nominal",
                            }
                        )
                        + "\n</progress-beacon>"
                    ),
                }
            ]
        },
    }


def run_case(name, events, expected_begin, expected_report):
    """Drive _find_beacon_anchors over `events` and compare its anchors. It
    resolves the JSONL by a session-id glob, so the test monkey-patches
    _find_session_jsonl rather than seeding files under ~/.claude/projects."""
    import statusline_lib.beacon as _beacon_mod

    with tempfile.NamedTemporaryFile(
        suffix=".jsonl", delete=False, mode="w", encoding="utf-8"
    ) as tmp:
        pass
    write_jsonl(tmp.name, events)

    original = _beacon_mod._find_session_jsonl
    _beacon_mod._find_session_jsonl = lambda _sid: tmp.name
    try:
        begin_ts, report_ts, _begin_eta = _find_beacon_anchors("ignored-sid")
    finally:
        _beacon_mod._find_session_jsonl = original
        os.unlink(tmp.name)

    ok_begin = begin_ts == expected_begin
    ok_report = report_ts == expected_report
    status = "PASS" if (ok_begin and ok_report) else "FAIL"
    print(f"[{status}] {name}")
    print(f"  got    begin={begin_ts!r}  report={report_ts!r}")
    print(f"  expect begin={expected_begin!r}  report={expected_report!r}")
    return ok_begin and ok_report


def main():
    results = []

    results.append(
        run_case(
            "begin -> report -> end -> report (no new begin): end clears anchors",
            [
                mk_assistant("2026-05-11T08:00:00.000Z", "begin"),
                mk_assistant("2026-05-11T08:01:00.000Z", "report"),
                mk_assistant("2026-05-11T08:02:00.000Z", "end"),
                mk_assistant("2026-05-11T08:05:00.000Z", "report"),
            ],
            expected_begin=None,
            expected_report=None,
        )
    )

    results.append(
        run_case(
            "begin -> report -> end -> begin -> report: new lifecycle anchors to new begin",
            [
                mk_assistant("2026-05-11T08:00:00.000Z", "begin"),
                mk_assistant("2026-05-11T08:01:00.000Z", "report"),
                mk_assistant("2026-05-11T08:02:00.000Z", "end"),
                mk_assistant("2026-05-11T08:05:00.000Z", "begin"),
                mk_assistant("2026-05-11T08:06:00.000Z", "report"),
            ],
            expected_begin="2026-05-11T08:05:00.000Z",
            expected_report="2026-05-11T08:06:00.000Z",
        )
    )

    results.append(
        run_case(
            "begin -> report -> report (no end): mid-lifecycle anchors normally",
            [
                mk_assistant("2026-05-11T08:00:00.000Z", "begin"),
                mk_assistant("2026-05-11T08:01:00.000Z", "report"),
                mk_assistant("2026-05-11T08:02:00.000Z", "report"),
            ],
            expected_begin="2026-05-11T08:00:00.000Z",
            expected_report="2026-05-11T08:02:00.000Z",
        )
    )

    results.append(
        run_case(
            "begin -> end (no later events): end clears (column hidden by format_beacon)",
            [
                mk_assistant("2026-05-11T08:00:00.000Z", "begin"),
                mk_assistant("2026-05-11T08:01:00.000Z", "end"),
            ],
            expected_begin=None,
            expected_report=None,
        )
    )

    if all(results):
        print("\nAll cases pass.")
        return 0
    print("\nSome cases failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
