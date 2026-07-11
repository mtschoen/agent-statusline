"""Verify beacon.py internal helpers: _compute_objective_drift,
_find_session_jsonl, _iter_beacons_in_text, _iter_assistant_beacons,
_apply_beacon, _scan_beacon_anchors, _find_beacon_anchors, and
_format_clock_and_elapsed.

Walker-dependent paths (format_beacon, _bias_factor_cached,
format_calibrated_eta) live in verify_beacon_walker.py.

Run from anywhere; imports from `schoen-claude-status` package by path.
"""

import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contextlib

import statusline_lib.beacon as _beacon_mod
from statusline_lib.beacon import (
    _compute_objective_drift,
    _find_beacon_anchors,
    _format_clock_and_elapsed,
    _iter_assistant_beacons,
    _iter_beacons_in_text,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text):
    return _ANSI.sub("", text) if text else text


def _mk_assistant(ts, beacon_kind, summary="test", eta=60):
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


def _check_compute_objective_drift(failures):
    if _compute_objective_drift(None, 300, 60) != "nominal":
        failures.append("drift: None begin_ts must be nominal")
    if _compute_objective_drift("2026-01-01T00:00:00Z", 0, 60) != "nominal":
        failures.append("drift: zero begin_eta must be nominal")
    if _compute_objective_drift("2026-01-01T00:00:00Z", -1, 60) != "nominal":
        failures.append("drift: negative begin_eta must be nominal")
    if _compute_objective_drift("not-a-date", 300, 60) != "nominal":
        failures.append("drift: bad timestamp must be nominal")
    if _compute_objective_drift("2020-01-01T00:00:00", 300, 60) != "material":
        failures.append("drift: naive far-past timestamp must be material")
    future_ts = (datetime.now(UTC) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    result = _compute_objective_drift(future_ts, 300, 60)
    if result != "nominal":
        failures.append(f"drift: future ts must be nominal, got {result!r}")
    old_ts = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if _compute_objective_drift(old_ts, 300, 60) != "material":
        failures.append("drift: 1h elapsed must be material")
    recent_ts = (datetime.now(UTC) - timedelta(seconds=10)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    if _compute_objective_drift(recent_ts, 300, "bad-eta") != "nominal":
        failures.append("drift: bad eta string must yield nominal")
    future_ts2 = (datetime.now(UTC) + timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    if _compute_objective_drift(future_ts2, 300, 700) != "material":
        failures.append("drift: ratio>=2 must be material")
    if _compute_objective_drift(future_ts2, 300, 500) != "moderate":
        failures.append("drift: ratio>=1.5 must be moderate")


def _check_find_session_jsonl(failures):
    if _beacon_mod._find_session_jsonl(None) is not None:
        failures.append("_find_session_jsonl(None) must return None")
    if _beacon_mod._find_session_jsonl("nonexistent-session-xyz-12345") is not None:
        failures.append("_find_session_jsonl(nonexistent) must return None")
    # A harness sending a non-string session id (e.g. int) must not crash
    # os.path.join -- coerce to str before building either candidate path.
    if _beacon_mod._find_session_jsonl(987654321) is not None:
        failures.append("_find_session_jsonl(non-string, nonexistent) must return None")
    home = os.path.expanduser("~")
    test_dir = os.path.join(home, ".claude", "projects", "_beacon_test_proj_")
    test_sid = "beacon-test-session-findme-0001"
    test_path = os.path.join(test_dir, f"{test_sid}.jsonl")
    os.makedirs(test_dir, exist_ok=True)
    try:
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        result = _beacon_mod._find_session_jsonl(test_sid)
        if result != test_path:
            failures.append(
                f"_find_session_jsonl with real file: expected {test_path!r}, got {result!r}"
            )
    finally:
        if os.path.exists(test_path):
            os.unlink(test_path)
        try:
            os.rmdir(test_dir)
        except OSError as exc:
            failures.append(f"cleanup _find_session_jsonl test dir failed: {exc}")

    # Test Antigravity path
    anti_dir = os.path.join(
        home,
        ".gemini",
        "antigravity-cli",
        "brain",
        test_sid,
        ".system_generated",
        "logs",
    )
    anti_path = os.path.join(anti_dir, "transcript.jsonl")
    os.makedirs(anti_dir, exist_ok=True)
    try:
        with open(anti_path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        result = _beacon_mod._find_session_jsonl(test_sid)
        if result != anti_path:
            failures.append(
                f"_find_session_jsonl with real antigravity file: expected {anti_path!r}, got {result!r}"
            )
    finally:
        if os.path.exists(anti_path):
            os.unlink(anti_path)
        # clean up directories
        curr = anti_dir
        for _ in range(4):
            if os.path.exists(curr):
                with contextlib.suppress(OSError):
                    os.rmdir(curr)
                curr = os.path.dirname(curr)


def _check_iter_beacons_in_text(failures):
    if list(_iter_beacons_in_text("no beacon here")) != []:
        failures.append("_iter_beacons_in_text with no tag should yield nothing")
    bad_text = "<progress-beacon>not-json</progress-beacon>"
    if list(_iter_beacons_in_text(bad_text)) != []:
        failures.append("_iter_beacons_in_text with bad JSON should yield nothing")
    # Matches the {...} block regex but fails JSON parsing, so the except
    # path (continue) runs; the valid second block must still be yielded.
    unparseable = (
        "<progress-beacon>{bad: json,}</progress-beacon>"
        '<progress-beacon>{"kind": "begin"}</progress-beacon>'
    )
    result = list(_iter_beacons_in_text(unparseable))
    if result != [{"kind": "begin"}]:
        failures.append(
            f"_iter_beacons_in_text unparseable block must be skipped, valid one kept; got {result!r}"
        )
    list_text = "<progress-beacon>[1,2,3]</progress-beacon>"
    if list(_iter_beacons_in_text(list_text)) != []:
        failures.append("_iter_beacons_in_text with list JSON should yield nothing")
    good_text = '<progress-beacon>{"kind": "report"}</progress-beacon>'
    result = list(_iter_beacons_in_text(good_text))
    if result != [{"kind": "report"}]:
        failures.append(
            f"_iter_beacons_in_text valid dict should yield it, got {result!r}"
        )


def _check_iter_assistant_beacons(failures):
    if list(_iter_assistant_beacons("not a dict")) != []:
        failures.append("_iter_assistant_beacons(str) must yield nothing")
    if list(_iter_assistant_beacons({"type": "user", "timestamp": "ts"})) != []:
        failures.append("_iter_assistant_beacons(user type) must yield nothing")
    if list(_iter_assistant_beacons({"type": "assistant"})) != []:
        failures.append("_iter_assistant_beacons(no timestamp) must yield nothing")
    entry_bad_content = {
        "type": "assistant",
        "timestamp": "ts",
        "message": {"content": "string-not-list"},
    }
    if list(_iter_assistant_beacons(entry_bad_content)) != []:
        failures.append("_iter_assistant_beacons(content=string) must yield nothing")
    entry_mixed = {
        "type": "assistant",
        "timestamp": "ts",
        "message": {
            "content": [
                {"type": "tool_use", "id": "x"},
                {
                    "type": "text",
                    "text": '<progress-beacon>{"kind":"report"}</progress-beacon>',
                },
            ]
        },
    }
    result = list(_iter_assistant_beacons(entry_mixed))
    if len(result) != 1 or result[0][1].get("kind") != "report":
        failures.append(
            f"_iter_assistant_beacons: tool_use chunk skipped, text chunk yielded; got {result!r}"
        )


def _check_apply_beacon_eta_parse(failures):
    state = {"begin_ts": None, "report_ts": None, "begin_eta": None}
    _beacon_mod._apply_beacon(
        {"kind": "begin", "eta_seconds": "not-a-number"}, "2026-01-01T00:00:00Z", state
    )
    if state["begin_ts"] != "2026-01-01T00:00:00Z":
        failures.append("_apply_beacon begin: begin_ts must be set even with bad eta")
    if state["begin_eta"] is not None:
        failures.append(
            f"_apply_beacon begin: bad eta should yield None begin_eta, got {state['begin_eta']!r}"
        )
    state2 = {"begin_ts": None, "report_ts": None, "begin_eta": None}
    _beacon_mod._apply_beacon({"kind": "report"}, "ts", state2)
    if state2["report_ts"] is not None:
        failures.append("_apply_beacon report with no begin_ts must not set report_ts")


def _check_scan_beacon_anchors_bad_json(failures):
    with tempfile.NamedTemporaryFile(
        suffix=".jsonl", delete=False, mode="w", encoding="utf-8"
    ) as tmp:
        tmp.write("not-json-at-all\n")
        tmp.write(
            json.dumps(_mk_assistant("2026-05-01T10:00:00.000Z", "begin", eta=120))
            + "\n"
        )
        tmp_path = tmp.name
    try:
        state = _beacon_mod._scan_beacon_anchors(tmp_path)
        if state["begin_ts"] != "2026-05-01T10:00:00.000Z":
            failures.append(
                f"_scan_beacon_anchors: bad line skipped, begin_ts wrong; got {state['begin_ts']!r}"
            )
    finally:
        os.unlink(tmp_path)


def _check_find_beacon_anchors_error_paths(failures):
    original_find = _beacon_mod._find_session_jsonl
    _beacon_mod._find_session_jsonl = lambda _sid: None
    try:
        result = _find_beacon_anchors("any-session")
        if result != (None, None, None):
            failures.append(
                f"_find_beacon_anchors with no path must return (None,None,None), got {result!r}"
            )
    finally:
        _beacon_mod._find_session_jsonl = original_find
    _beacon_mod._find_session_jsonl = lambda _sid: "/nonexistent/path/beacon.jsonl"
    try:
        result = _find_beacon_anchors("any-session")
        if result != (None, None, None):
            failures.append(
                f"_find_beacon_anchors with OSError must return (None,None,None), got {result!r}"
            )
    finally:
        _beacon_mod._find_session_jsonl = original_find


def _check_format_clock_and_elapsed(failures):
    if _format_clock_and_elapsed(None) is not None:
        failures.append("_format_clock_and_elapsed(None) must be None")
    if _format_clock_and_elapsed("not-a-date") is not None:
        failures.append("_format_clock_and_elapsed(bad) must be None")
    ts = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    result = _format_clock_and_elapsed(ts)
    if result is None:
        failures.append("_format_clock_and_elapsed(valid) must not be None")
    elif not re.match(r"^\d{2}:\d{2} \(\d+m\)$", result):
        failures.append(f"_format_clock_and_elapsed format wrong: {result!r}")
    naive_ts = (datetime.now(UTC) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S")
    if _format_clock_and_elapsed(naive_ts) is None:
        failures.append("_format_clock_and_elapsed(naive) must not be None")
    future_ts = (datetime.now(UTC) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    result = _format_clock_and_elapsed(future_ts)
    if result is None or "(0m)" not in result:
        failures.append(
            f"_format_clock_and_elapsed(future) must contain '(0m)', got {result!r}"
        )


def main():
    failures = []
    _check_compute_objective_drift(failures)
    _check_find_session_jsonl(failures)
    _check_iter_beacons_in_text(failures)
    _check_iter_assistant_beacons(failures)
    _check_apply_beacon_eta_parse(failures)
    _check_scan_beacon_anchors_bad_json(failures)
    _check_find_beacon_anchors_error_paths(failures)
    _check_format_clock_and_elapsed(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: beacon internal helpers all verified")


if __name__ == "__main__":
    main()
