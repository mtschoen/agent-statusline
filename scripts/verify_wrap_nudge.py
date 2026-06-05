"""Verify the wrap nudge: the shared state-file contract
(``statusline_lib/nudge.py``) and the hook entry point (``wrap_nudge.py``).

Covers the threshold gate, the one-shot marker, the producer/consumer file
round-trip, corrupt/absent state, concurrent-session keying, and an end-to-end
run of the actual hook script via a subprocess with ``CLAUDE_STATE_DIR`` pointed
at a temp dir (so the test never touches the real ``~/.claude/state``).

Run from anywhere; imports from schoen-claude-status by path.
"""

import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from statusline_lib.nudge import (
    NUDGE_THRESHOLD_TOKENS,
    ctx_state_path,
    marker_path,
    read_ctx_used,
    should_nudge,
    write_ctx_state,
    write_marker,
)

SID = "test-session-abc123"
OTHER = "other-session-def456"


def check_threshold(failures):
    with tempfile.TemporaryDirectory() as tmp:
        if should_nudge(NUDGE_THRESHOLD_TOKENS - 1, SID, state_dir=tmp):
            failures.append("below threshold should not nudge")
        if not should_nudge(NUDGE_THRESHOLD_TOKENS, SID, state_dir=tmp):
            failures.append("at threshold with no marker should nudge")
        if not should_nudge(NUDGE_THRESHOLD_TOKENS + 50_000, SID, state_dir=tmp):
            failures.append("above threshold with no marker should nudge")
        if should_nudge(None, SID, state_dir=tmp):
            failures.append("missing occupancy (None) should not nudge")


def check_one_shot(failures):
    with tempfile.TemporaryDirectory() as tmp:
        if not should_nudge(250_000, SID, state_dir=tmp):
            failures.append("first crossing should nudge")
        write_marker(SID, state_dir=tmp)
        if not os.path.exists(marker_path(SID, state_dir=tmp)):
            failures.append("write_marker should create the marker file")
        if should_nudge(250_000, SID, state_dir=tmp):
            failures.append("second crossing (marker present) should stay silent")


def check_roundtrip(failures):
    with tempfile.TemporaryDirectory() as tmp:
        if read_ctx_used(SID, state_dir=tmp) is not None:
            failures.append("absent state file should read as None")
        write_ctx_state(SID, 234_567, 1_000_000, now=1000.0, state_dir=tmp)
        if read_ctx_used(SID, state_dir=tmp) != 234_567:
            failures.append("ctx round-trip should return the written occupancy")
        write_ctx_state("", 999_999, 1_000_000, now=1000.0, state_dir=tmp)
        if read_ctx_used("", state_dir=tmp) is not None:
            failures.append("empty session id should not produce readable state")


def check_corrupt(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = ctx_state_path(SID, state_dir=tmp)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not valid json")
        if read_ctx_used(SID, state_dir=tmp) is not None:
            failures.append("corrupt state file should read as None (no nudge)")


def check_concurrent_keying(failures):
    with tempfile.TemporaryDirectory() as tmp:
        write_ctx_state(SID, 250_000, 1_000_000, now=1.0, state_dir=tmp)
        write_ctx_state(OTHER, 30_000, 1_000_000, now=1.0, state_dir=tmp)
        if not should_nudge(read_ctx_used(SID, state_dir=tmp), SID, state_dir=tmp):
            failures.append("crossed session should nudge")
        if should_nudge(read_ctx_used(OTHER, state_dir=tmp), OTHER, state_dir=tmp):
            failures.append("quiet concurrent session should not nudge")
        write_marker(SID, state_dir=tmp)
        if should_nudge(read_ctx_used(SID, state_dir=tmp), SID, state_dir=tmp):
            failures.append("marking one session must not leave it firing")
        if should_nudge(read_ctx_used(OTHER, state_dir=tmp), OTHER, state_dir=tmp):
            failures.append("marking one session must not nudge the other")


def _run_hook(tmp, payload):
    """Invoke the real hook script with CLAUDE_STATE_DIR pointed at ``tmp``."""
    return subprocess.run(
        [sys.executable, os.path.join(REPO, "wrap_nudge.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_STATE_DIR": tmp},
        timeout=30,
        check=False,
    )


def check_hook_subprocess(failures):
    with tempfile.TemporaryDirectory() as tmp:
        write_ctx_state(SID, 260_000, 1_000_000, now=1.0, state_dir=tmp)
        first = _run_hook(tmp, {"session_id": SID, "prompt": "hi"})
        if first.returncode != 0:
            failures.append(f"hook should exit 0 (got {first.returncode})")
        if "additionalContext" not in first.stdout:
            failures.append("hook should emit additionalContext when over threshold")
        if not os.path.exists(marker_path(SID, state_dir=tmp)):
            failures.append("hook should write the one-shot marker")

        second = _run_hook(tmp, {"session_id": SID, "prompt": "hi again"})
        if second.stdout.strip():
            failures.append("second hook run should be silent (one-shot)")

        write_ctx_state(OTHER, 30_000, 1_000_000, now=1.0, state_dir=tmp)
        quiet = _run_hook(tmp, {"session_id": OTHER})
        if quiet.stdout.strip():
            failures.append("below-threshold session should emit nothing")


def main():
    failures = []
    for check in (
        check_threshold,
        check_one_shot,
        check_roundtrip,
        check_corrupt,
        check_concurrent_keying,
        check_hook_subprocess,
    ):
        check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: wrap nudge fires once past threshold, keyed per session")


if __name__ == "__main__":
    main()
