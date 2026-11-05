"""Verify the Python-floor handling of the wrap-nudge and ensure-server hooks:
the installer bakes an absolute interpreter path into each POSIX command and
re-running it converts an existing bare-python3 entry in place, and each hook
entry point (wrap_nudge.py, statusline_client.py --ensure-server) prints one
clear stderr line and exits 0 below the minimum version (simulated by patching
``sys.version_info``).

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
import statusline_client
from statusline_lib import nudge_install, server_install
from statusline_lib.nudge_install import (
    _merge_nudge_hook,
    _nudge_command,
    _nudge_hook_current,
    _nudge_markers,
)
from statusline_lib.server_install import (
    _SERVER_SENTINEL,
    _merge_server_hook,
    _server_hook_command,
    _server_hook_current,
    _server_hook_markers,
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
        mock.patch.object(python_floor.sys, "version_info", OLD),
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


def check_server_absolute_interpreter(failures):
    with mock.patch.object(server_install.os, "name", "posix"):
        _, command = _server_hook_command("/repo")
        if not command.startswith(f'"{os.path.abspath(sys.executable)}" '):
            failures.append(f"server command should pin sys.executable: {command!r}")
        _, pinned = _server_hook_command("/repo", interpreter="/opt/py/bin/python3.12")
        if not pinned.startswith(
            '"/opt/py/bin/python3.12" "/repo/statusline_client.py"'
        ):
            failures.append(f"explicit interpreter ignored: {pinned!r}")
    with (
        mock.patch.object(server_install.os, "name", "posix"),
        mock.patch.object(python_floor.sys, "version_info", OLD),
    ):
        try:
            _server_hook_command("/repo")
        except RuntimeError as exc:
            if "3.11+" not in str(exc):
                failures.append(f"refusal should name the floor: {exc}")
        else:
            failures.append("server installer on a too-old interpreter should refuse")


def check_server_idempotent_migration(failures):
    with mock.patch.object(server_install.os, "name", "posix"):
        target, new = _server_hook_command("/repo", interpreter="/opt/py/bin/python3")
        markers = _server_hook_markers(target)
        bare = f'python3 "/repo/statusline_client.py" --ensure-server || true {_SERVER_SENTINEL}'
        settings = {"hooks": {"SessionStart": [{"hooks": [{"command": bare}]}]}}
        for _ in range(3):
            _merge_server_hook(settings, markers, new)
        groups = settings["hooks"]["SessionStart"]
        if len(groups) != 1 or len(groups[0]["hooks"]) != 1:
            failures.append(f"re-running must not duplicate: {groups!r}")
        if not _server_hook_current(settings, markers, new):
            failures.append("bare python3 entry should be converted in place")


def check_client_guard(failures):
    stderr = io.StringIO()
    with mock.patch.object(sys, "version_info", OLD), redirect_stderr(stderr):
        code = statusline_client.main("claude", ["--ensure-server"])
    lines = stderr.getvalue().strip().splitlines()
    if code != 0:
        failures.append(f"client guard must return 0, got {code!r}")
    if len(lines) != 1 or "re-run install.py" not in lines[0].lower():
        failures.append(f"expected one clear stderr line, got {lines!r}")
    if statusline_client._MINIMUM_PYTHON != python_floor.MIN_PYTHON:
        failures.append("client floor copy drifted from python_floor.MIN_PYTHON")
    with mock.patch.object(sys, "version_info", OLD), redirect_stderr(io.StringIO()):
        if not statusline_client._python_too_old():
            failures.append("OLD should trip the guard")
    expected = python_floor.too_old_message(OLD, sys.executable, "ensure-server")
    if expected != stderr.getvalue().strip():
        failures.append("client message drifted from python_floor.too_old_message")
    with mock.patch.object(sys, "version_info", sys.version_info):
        if statusline_client._python_too_old():
            failures.append("current interpreter should pass the guard")


def main():
    failures = []
    for check in (
        check_floor_helpers,
        check_absolute_interpreter,
        check_old_installer_interpreter_refused,
        check_idempotent_update,
        check_hook_guard,
        check_server_absolute_interpreter,
        check_server_idempotent_migration,
        check_client_guard,
    ):
        check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: wrap-nudge and ensure-server hooks pin an absolute interpreter and guard the Python floor"
    )


if __name__ == "__main__":
    main()
