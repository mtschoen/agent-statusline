"""Verify the wrap-nudge hook's Python-floor handling: the installer bakes an
absolute interpreter path into the POSIX command and re-running it updates the
entry in place, and wrap_nudge.py prints one clear stderr line and exits 0 when
run below the minimum version (simulated by patching ``sys.version_info``).

Run from anywhere.
"""

import io
import os
import runpy
import sys
from contextlib import redirect_stderr
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import python_floor
from statusline_lib import nudge_install
from statusline_lib.nudge_install import (
    _merge_nudge_hook,
    _nudge_command,
    _nudge_hook_current,
    _nudge_markers,
)

OLD = (3, 9, 6, "final", 0)


def check_floor_helpers(failures):
    if not python_floor.is_too_old(OLD):
        failures.append("3.9 should be below the floor")
    if python_floor.is_too_old((*python_floor.MIN_PYTHON, 0, "final", 0)):
        failures.append("the floor itself should be accepted")
    message = python_floor.too_old_message(OLD, "/usr/bin/python3", "wrap_nudge")
    if "3.9.6" not in message or "3.11+" not in message or "install.py" not in message:
        failures.append(f"message should name versions and the fix, got {message!r}")


def check_absolute_interpreter(failures):
    with mock.patch.object(nudge_install.os, "name", "posix"):
        _, command = _nudge_command("/repo")
        if not command.startswith(f'"{os.path.abspath(sys.executable)}" '):
            failures.append(f"command should pin sys.executable, got {command!r}")
        _, pinned = _nudge_command("/repo", interpreter="/opt/py/bin/python3.12")
        if not pinned.startswith('"/opt/py/bin/python3.12" "/repo/wrap_nudge.py"'):
            failures.append(f"explicit interpreter ignored: {pinned!r}")
        if pinned.startswith("python3"):
            failures.append("bare python3 must not be used")


def check_old_installer_interpreter_refused(failures):
    with (
        mock.patch.object(nudge_install.os, "name", "posix"),
        mock.patch.object(nudge_install.sys, "version_info", OLD),
    ):
        try:
            _nudge_command("/repo")
        except RuntimeError as exc:
            if "3.11+" not in str(exc):
                failures.append(f"refusal should name the floor: {exc}")
        else:
            failures.append("installer on a too-old interpreter should refuse")


def check_idempotent_update(failures):
    with mock.patch.object(nudge_install.os, "name", "posix"):
        target, old = _nudge_command("/repo", interpreter="/usr/bin/python3")
        _, new = _nudge_command("/repo", interpreter="/opt/py/bin/python3.12")
        markers = _nudge_markers(target)
        bare = 'python3 "/repo/wrap_nudge.py" 2>>"$HOME/.claude/x.log" || true '
        bare += nudge_install._NUDGE_SENTINEL
        settings = {"hooks": {"UserPromptSubmit": [{"hooks": [{"command": bare}]}]}}
        for command in (new, new, old, new):
            _merge_nudge_hook(settings, markers, command)
        groups = settings["hooks"]["UserPromptSubmit"]
        if len(groups) != 1 or len(groups[0]["hooks"]) != 1:
            failures.append(f"re-running must not duplicate: {groups!r}")
        if not _nudge_hook_current(settings, markers, new):
            failures.append("entry should end on the latest command")


def check_hook_guard(failures):
    stderr = io.StringIO()
    with mock.patch.object(sys, "version_info", OLD), redirect_stderr(stderr):
        try:
            runpy.run_path(os.path.join(REPO, "wrap_nudge.py"), run_name="guard")
        except SystemExit as exc:
            if exc.code != 0:
                failures.append(f"guard must exit 0, got {exc.code!r}")
        else:
            failures.append("guard should exit before importing statusline_lib")
    lines = stderr.getvalue().strip().splitlines()
    if len(lines) != 1 or "re-run install.py" not in lines[0].lower():
        failures.append(f"expected one clear stderr line, got {lines!r}")


def main():
    failures = []
    for check in (
        check_floor_helpers,
        check_absolute_interpreter,
        check_old_installer_interpreter_refused,
        check_idempotent_update,
        check_hook_guard,
    ):
        check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: wrap nudge pins an absolute interpreter and guards the Python floor")


if __name__ == "__main__":
    main()
