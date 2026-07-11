"""Verify `statusline_lib.walker._walker_root_list()` honors the same
`--statusline-platform` argv flag as `statusline_lib.base.app_dir()`.

Split out from scripts/verify_walker.py to avoid pushing that file over
aislop's 400-line file-size threshold; see that file for the rest of
_walker_root_list()'s coverage (config parsing, dedup, filtering, the
STATUSLINE_PLATFORM env branches, and the ANTIGRAVITY_AGENT/
ANTIGRAVITY_CONVERSATION_ID env auto-detect fallback).

Why this exists: _walker_root_list() used to carry its own independent copy
of app_dir()'s platform if/elif chain (a prior fix, dccc87e, had to update
both in lockstep -- see TEST-REPORT.md). When app_dir() gained argv-flag
support (install.py now injects `--statusline-platform antigravity` into the
Antigravity CLI command string, since that CLI doesn't set ANTIGRAVITY_AGENT
/ ANTIGRAVITY_CONVERSATION_ID for the statusline subprocess), the duplicated
copy in walker.py was missed, leaving _walker_root_list() -- and therefore
burnrate.py/pace.py, which consume it -- silently reading Claude Code's own
~/.claude/projects instead of Antigravity's brain/ dir under the real
Antigravity invocation shape (argv flag, no env vars). Fixed by deleting the
duplicated chain and deriving the root from app_dir() directly.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.walker as walker_module
from scripts._walker_helpers import (
    fake_expanduser_for,
    restore_walker_state,
    save_walker_state,
)
from statusline_lib.walker import _walker_root_list


def _check_root_list_argv_override(failures):
    # This is the real Antigravity CLI invocation shape: the argv flag is
    # present, but no ANTIGRAVITY_AGENT / ANTIGRAVITY_CONVERSATION_ID env
    # vars are set (that CLI doesn't set them for the statusline subprocess).
    state = save_walker_state()
    original_expanduser = walker_module.os.path.expanduser
    original_environ = os.environ.copy()
    original_argv = sys.argv[:]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            anti_brain = os.path.join(tmp, ".gemini", "antigravity-cli", "brain")
            os.makedirs(anti_brain, exist_ok=True)
            claude_projects = os.path.join(tmp, ".claude", "projects")
            os.makedirs(claude_projects, exist_ok=True)

            walker_module.os.path.expanduser = fake_expanduser_for(
                tmp, original_expanduser
            )
            os.environ.clear()
            sys.argv = ["statusline.py", "--statusline-platform", "antigravity"]
            res = _walker_root_list()
            if os.path.realpath(anti_brain) not in res:
                failures.append(
                    f"argv platform=antigravity missing anti_brain, got {res!r}"
                )

            # `=`-joined form, and platform=claude should keep claude_projects.
            sys.argv = ["statusline.py", "--statusline-platform=claude"]
            res = _walker_root_list()
            if os.path.realpath(claude_projects) not in res:
                failures.append(
                    f"argv platform=claude (=form) missing claude_projects, got {res!r}"
                )

            # STATUSLINE_PLATFORM env still wins over the argv flag (matches
            # app_dir()'s precedence).
            sys.argv = ["statusline.py", "--statusline-platform", "antigravity"]
            os.environ["STATUSLINE_PLATFORM"] = "claude"
            res = _walker_root_list()
            if os.path.realpath(claude_projects) not in res:
                failures.append(f"env should win over argv; got {res!r}")
    finally:
        os.environ.clear()
        os.environ.update(original_environ)
        sys.argv = original_argv
        restore_walker_state(state)
        walker_module.os.path.expanduser = original_expanduser


def check(failures):
    _check_root_list_argv_override(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: _walker_root_list honors --statusline-platform the same way app_dir() does"
    )


if __name__ == "__main__":
    main()
