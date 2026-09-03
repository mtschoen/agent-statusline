"""Verify the render timer: the shared previous-render-duration + session-peak
state file (``statusline_lib/rendertimer.py``) that mirrors the Pi extension's
footer instrumentation (``pi-extension/renderer.ts``, commit 0323dbc) for the
spawn-per-render Python harnesses.

Covers the env gate, the read/record round-trip, per-session-file peak
tracking (and its natural reset on a session change), the no-session-id
shared-key fallback, and corrupt/absent state.

There is no entry-point end-to-end coverage (statusline.py /
qwen_statusline.py via subprocess) because those wrappers never call
record_render: they forward one datagram to the resident server and print
its reply, and the server is what records the duration.
format_render_suffix's own call site inside
render_claude_statusline / render_qwen_statusline is still reached (and
covered) by scripts/verify_render_claude.py and scripts/verify_qwen_-
render.py, which is where the resident server actually calls it now.

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from statusline_lib.rendertimer import (
    RENDER_TIMING_ENV_VAR,
    format_render_suffix,
    read_previous,
    record_render,
    render_timer_path,
    timing_enabled,
)

SID = "test-session-abc123"
OTHER = "other-session-def456"
TEXT_ENCODING = "utf-8"


def check_env_gate(failures):
    env = os.environ.pop(RENDER_TIMING_ENV_VAR, None)
    try:
        if not timing_enabled():
            failures.append("timing should default to enabled when env unset")
        os.environ[RENDER_TIMING_ENV_VAR] = "0"
        if timing_enabled():
            failures.append('"0" should disable timing')
        os.environ[RENDER_TIMING_ENV_VAR] = "1"
        if not timing_enabled():
            failures.append('any non-"0" value should leave timing enabled')
    finally:
        if env is None:
            os.environ.pop(RENDER_TIMING_ENV_VAR, None)
        else:
            os.environ[RENDER_TIMING_ENV_VAR] = env


def check_first_render_no_suffix(failures):
    with tempfile.TemporaryDirectory() as tmp:
        if read_previous(SID, state_dir=tmp) is not None:
            failures.append("absent state file should read as None")
        if format_render_suffix(SID, state_dir=tmp) != "":
            failures.append("first render (no prior state) should render no suffix")


def check_record_and_read_roundtrip(failures):
    with tempfile.TemporaryDirectory() as tmp:
        record_render(12.5, SID, state_dir=tmp)
        previous = read_previous(SID, state_dir=tmp)
        if previous != (12.5, 12.5):
            failures.append(
                f"first record should set last==peak==12.5; got {previous!r}"
            )
        suffix = format_render_suffix(SID, state_dir=tmp)
        if "ui 12.50ms peak 12.50ms" not in suffix:
            failures.append(f"suffix should mirror Pi's wording; got {suffix!r}")


def check_peak_tracking(failures):
    with tempfile.TemporaryDirectory() as tmp:
        record_render(10.0, SID, state_dir=tmp)
        record_render(40.0, SID, state_dir=tmp)
        if read_previous(SID, state_dir=tmp) != (40.0, 40.0):
            failures.append("peak should climb to the new high")
        record_render(5.0, SID, state_dir=tmp)
        last, peak = read_previous(SID, state_dir=tmp)
        if (last, peak) != (5.0, 40.0):
            failures.append(
                f"a faster render should update last but keep the session peak; got {(last, peak)!r}"
            )


def check_session_reset_resets_peak(failures):
    # Each session id keys its own state file (render_timer_path), so a new
    # session naturally starts with no prior peak -- no explicit reset logic
    # needed, but the behavior must hold end to end.
    with tempfile.TemporaryDirectory() as tmp:
        record_render(90.0, SID, state_dir=tmp)
        if read_previous(OTHER, state_dir=tmp) is not None:
            failures.append(
                "a different session id must not see another session's peak"
            )
        record_render(3.0, OTHER, state_dir=tmp)
        if read_previous(OTHER, state_dir=tmp) != (3.0, 3.0):
            failures.append(
                "new session's peak should start fresh at its own first render"
            )
        if read_previous(SID, state_dir=tmp) != (90.0, 90.0):
            failures.append("recording a second session must not disturb the first")


def check_shared_key_fallback(failures):
    with tempfile.TemporaryDirectory() as tmp:
        record_render(7.0, None, state_dir=tmp)
        if read_previous(None, state_dir=tmp) != (7.0, 7.0):
            failures.append("no session id should round-trip through the shared key")
        if render_timer_path(None, state_dir=tmp) != render_timer_path(
            "", state_dir=tmp
        ):
            failures.append("None and empty-string session ids should share one key")


def check_disabled_gate_suppresses_suffix_and_record(failures):
    with tempfile.TemporaryDirectory() as tmp:
        record_render(15.0, SID, state_dir=tmp)
        env = os.environ.get(RENDER_TIMING_ENV_VAR)
        os.environ[RENDER_TIMING_ENV_VAR] = "0"
        try:
            if format_render_suffix(SID, state_dir=tmp) != "":
                failures.append(
                    "disabled gate should render no suffix even with prior data"
                )
            record_render(99.0, SID, state_dir=tmp)
            if read_previous(SID, state_dir=tmp) != (15.0, 15.0):
                failures.append("disabled gate should skip the write entirely")
        finally:
            if env is None:
                os.environ.pop(RENDER_TIMING_ENV_VAR, None)
            else:
                os.environ[RENDER_TIMING_ENV_VAR] = env


def check_corrupt_state_file(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = render_timer_path(SID, state_dir=tmp)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding=TEXT_ENCODING) as f:
            f.write("{ not valid json")
        if read_previous(SID, state_dir=tmp) is not None:
            failures.append("corrupt state file should read as None")

        with open(path, "w", encoding=TEXT_ENCODING) as f:
            json.dump({"last_ms": "not-a-number", "peak_ms": 1.0}, f)
        if read_previous(SID, state_dir=tmp) is not None:
            failures.append("non-numeric fields should read as None")


def check_record_render_oserror(failures):
    # Mirrors nudge.py's blocker-file trick: put a file where makedirs
    # expects a directory so the write raises OSError, which record_render
    # must swallow (a failed write must never break the render).
    with tempfile.TemporaryDirectory() as tmp:
        blocker = os.path.join(tmp, "not_a_dir")
        with open(blocker, "w", encoding=TEXT_ENCODING) as f:
            f.write("blocker")
        bad_state_dir = os.path.join(blocker, "subdir")
        record_render(1.0, SID, state_dir=bad_state_dir)  # must not raise


def check_record_render_bad_elapsed(failures):
    # A non-numeric elapsed_ms (e.g. a caller bug upstream) must neither
    # raise nor write a corrupt state file -- record_render's whole job is
    # to never break the render that calls it at process exit.
    with tempfile.TemporaryDirectory() as tmp:
        record_render("not-a-number", SID, state_dir=tmp)  # must not raise
        if read_previous(SID, state_dir=tmp) is not None:
            failures.append("a bad elapsed_ms must not produce a readable state file")

        record_render(None, OTHER, state_dir=tmp)  # must not raise
        if read_previous(OTHER, state_dir=tmp) is not None:
            failures.append("elapsed_ms=None must not produce a readable state file")

        # A subsequent, valid call must still work -- the bad call above
        # should not have left anything behind that corrupts the next write.
        record_render(9.0, SID, state_dir=tmp)
        if read_previous(SID, state_dir=tmp) != (9.0, 9.0):
            failures.append(
                "a valid record_render after a bad one should still round-trip"
            )


def main():
    failures = []
    for check in (
        check_env_gate,
        check_first_render_no_suffix,
        check_record_and_read_roundtrip,
        check_peak_tracking,
        check_session_reset_resets_peak,
        check_shared_key_fallback,
        check_disabled_gate_suppresses_suffix_and_record,
        check_corrupt_state_file,
        check_record_render_oserror,
        check_record_render_bad_elapsed,
    ):
        check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: render timer mirrors Pi's previous-render + session-peak instrumentation"
    )


if __name__ == "__main__":
    main()
