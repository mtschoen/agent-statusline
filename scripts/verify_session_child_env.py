"""Verify the child-session environment-variable classifier in
`statusline_lib.sessions` (issue #11: a Task-tool subagent sharing its
parent's cwd was tripping the [N sessions] badge).

Covers:
  - `_is_child_session_env`: pure marker classifier over a process
    environment mapping.
  - `_count_via_psutil` end to end against a fake psutil: a process carrying
    Claude Code's own CLAUDE_CODE_CHILD_SESSION marker is excluded even when
    its process-tree shape alone would count it; an unreadable environ()
    fails open (still counts) rather than hiding a real session.

Split out from verify_session_tree.py to keep both files under aislop's
400-line file-size gate. Run from anywhere; imports from
`schoen-claude-status` by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts._session_helpers import FakePsutilError, FakeSnapProc, make_fake_psutil


def check_is_child_session_env(failures):
    # Pure classifier: Claude Code's own child-session marker
    # (CLAUDE_CODE_CHILD_SESSION), independent of process ancestry.
    from statusline_lib.sessions import _is_child_session_env

    if not _is_child_session_env({"CLAUDE_CODE_CHILD_SESSION": "1"}):
        failures.append("CLAUDE_CODE_CHILD_SESSION=1 should mark a child session")
    if _is_child_session_env(None):
        failures.append("unreadable (None) environ should fail open, not mark")
    if _is_child_session_env({}):
        failures.append("empty environ should not mark a child session")
    if _is_child_session_env({"CLAUDE_CODE_CHILD_SESSION": "0"}):
        failures.append("CLAUDE_CODE_CHILD_SESSION=0 should not mark a child session")
    if _is_child_session_env({"CLAUDE_CODE_CHILD_SESSION": ""}):
        failures.append("empty CLAUDE_CODE_CHILD_SESSION should not mark")
    if _is_child_session_env({"OTHER_VAR": "1"}):
        failures.append("unrelated env vars should not mark a child session")


def check_count_excludes_child_session_env(failures):
    # Issue #11: a Task-tool subagent's own tool-execution process shares the
    # parent's cwd and (per a shell-launched, intact ancestor chain) would
    # NOT be caught by _is_excluded_by_tree alone -- it needs the direct
    # environment marker to be excluded.
    import statusline_lib.sessions as sessions_mod

    target_cwd = os.path.normcase("/home/user/proj")
    procs = [
        FakeSnapProc(40, 1, "explorer.exe", 100.0),
        FakeSnapProc(50, 40, "cmd.exe", 200.0),
        # Genuine top-level session: no child-session marker -> counts.
        FakeSnapProc(100, 50, "claude", 300.0, ["claude"], target_cwd, env={}),
        # Subagent tool-execution process: shell-launched shape (would count
        # under the tree walk alone), but carries the child-session marker.
        FakeSnapProc(
            110,
            50,
            "claude",
            310.0,
            ["claude"],
            target_cwd,
            env={"CLAUDE_CODE_CHILD_SESSION": "1"},
        ),
    ]
    count = sessions_mod._count_via_psutil(target_cwd, make_fake_psutil(procs))
    if count != 1:
        failures.append(
            f"child-session-marked process should not count; expected 1, got {count}"
        )


def check_count_child_session_env_unreadable_fails_open(failures):
    # environ() can raise (AccessDenied, missing /proc entry, etc.) -- must
    # not crash, and must not exclude a real session just because its
    # environment couldn't be read.
    import statusline_lib.sessions as sessions_mod

    target_cwd = os.path.normcase("/home/user/proj")
    procs = [
        FakeSnapProc(40, 1, "explorer.exe", 100.0),
        FakeSnapProc(50, 40, "cmd.exe", 200.0),
        FakeSnapProc(
            100,
            50,
            "claude",
            300.0,
            ["claude"],
            target_cwd,
            env=FakePsutilError("no access"),
        ),
    ]
    count = sessions_mod._count_via_psutil(target_cwd, make_fake_psutil(procs))
    if count != 1:
        failures.append(
            f"unreadable environ() should fail open and still count; got {count}"
        )


def main():
    failures = []
    check_is_child_session_env(failures)
    check_count_excludes_child_session_env(failures)
    check_count_child_session_env_unreadable_fails_open(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print("OK: child-session environment-variable classification behaves correctly")


if __name__ == "__main__":
    main()
