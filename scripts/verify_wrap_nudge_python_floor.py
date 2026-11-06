"""Verify the Python-floor handling of the hooks and the installer: the floor is
3.9; POSIX hook commands use a bare ``python3`` and re-running the installer
converts an entry that an earlier install pinned to an absolute interpreter
back in place; each entry point (wrap_nudge.py, statusline_client.py, install.py)
prints one clear stderr line below the floor (simulated by patching
``sys.version_info``; hooks exit 0, the installer exits 1); and the guard files
parse as old Python 3 (no f-strings, annotations, walrus), so an interpreter
older than 3.9 reaches the message instead of a SyntaxError.

Run from anywhere.
"""

import ast
import io
import os
import runpy
import sys
import tokenize
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

OLD = (3, 8, 10, "final", 0)
GUARD_FILES = ("python_floor.py", "wrap_nudge.py", "statusline_client.py", "install.py")
NUDGE_SENTINEL = nudge_install._NUDGE_SENTINEL


def _one_line(failures, label, text):
    lines = text.strip().splitlines()
    if len(lines) != 1 or "needs 3.9+" not in lines[0] or "3.8.10" not in lines[0]:
        failures.append(f"{label}: expected one clear stderr line, got {lines!r}")


def check_floor_helpers(failures):
    if python_floor.MIN_PYTHON != (3, 9):
        failures.append(f"floor should be 3.9, got {python_floor.MIN_PYTHON!r}")
    if not python_floor.is_too_old(OLD):
        failures.append("3.8 should be below the floor")
    if python_floor.is_too_old((3, 9, 6, "final", 0)):
        failures.append("macOS /usr/bin/python3 (3.9.6) must be accepted")
    message = python_floor.too_old_message(OLD, "/usr/bin/python3", "wrap_nudge")
    if "3.8.10" not in message or "3.9+" not in message or "python3" not in message:
        failures.append(f"message should name versions and the fix, got {message!r}")


def check_bare_python3_commands(failures):
    with (
        mock.patch.object(nudge_install.os, "name", "posix"),
        mock.patch.object(server_install.os, "name", "posix"),
    ):
        _, nudge = _nudge_command("/repo")
        _, server = _server_hook_command("/repo")
    if not nudge.startswith('python3 "/repo/wrap_nudge.py" '):
        failures.append(f"nudge command should use bare python3, got {nudge!r}")
    if not server.startswith('python3 "/repo/statusline_client.py" --ensure-server'):
        failures.append(f"server command should use bare python3, got {server!r}")


def check_absolute_path_entries_convert_back(failures):
    with (
        mock.patch.object(nudge_install.os, "name", "posix"),
        mock.patch.object(server_install.os, "name", "posix"),
    ):
        target, nudge = _nudge_command("/repo")
        pinned = f'"/opt/py/bin/python3.12" "{target}" 2>>"$HOME/x.log" || true '
        settings = {
            "hooks": {
                "UserPromptSubmit": [{"hooks": [{"command": pinned + NUDGE_SENTINEL}]}]
            }
        }
        markers = _nudge_markers(target)
        for _ in range(3):
            _merge_nudge_hook(settings, markers, nudge)
        groups = settings["hooks"]["UserPromptSubmit"]
        if len(groups) != 1 or len(groups[0]["hooks"]) != 1:
            failures.append(f"re-running must not duplicate: {groups!r}")
        if not _nudge_hook_current(settings, markers, nudge):
            failures.append("absolute-path nudge entry should convert to bare python3")

        target, server = _server_hook_command("/repo")
        pinned = f'"/opt/py/bin/python3.12" "{target}" --ensure-server || true '
        settings = {
            "hooks": {
                "SessionStart": [{"hooks": [{"command": pinned + _SERVER_SENTINEL}]}]
            }
        }
        markers = _server_hook_markers(target)
        for _ in range(3):
            _merge_server_hook(settings, markers, server)
        groups = settings["hooks"]["SessionStart"]
        if len(groups) != 1 or len(groups[0]["hooks"]) != 1:
            failures.append(f"re-running must not duplicate: {groups!r}")
        if not _server_hook_current(settings, markers, server):
            failures.append("absolute-path server entry should convert to bare python3")


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
    _one_line(failures, "wrap_nudge", stderr.getvalue())


def check_client_guard(failures):
    stderr = io.StringIO()
    with mock.patch.object(sys, "version_info", OLD), redirect_stderr(stderr):
        code = statusline_client.main("claude", ["--ensure-server"])
    if code != 0:
        failures.append(f"client guard must return 0, got {code!r}")
    _one_line(failures, "client main", stderr.getvalue())
    if statusline_client._MINIMUM_PYTHON != python_floor.MIN_PYTHON:
        failures.append("client floor copy drifted from python_floor.MIN_PYTHON")
    expected = python_floor.too_old_message(OLD, sys.executable, "ensure-server")
    if expected != stderr.getvalue().strip():
        failures.append("client message drifted from python_floor.too_old_message")
    with mock.patch.object(sys, "version_info", sys.version_info):
        if statusline_client._python_too_old():
            failures.append("current interpreter should pass the guard")
    # The module-level guard: importing the client on an old interpreter exits 0
    # with the message before it touches sys.stdout.reconfigure (3.7+).
    stderr = io.StringIO()
    with mock.patch.object(sys, "version_info", OLD), redirect_stderr(stderr):
        try:
            runpy.run_path(os.path.join(REPO, "statusline_client.py"), run_name="guard")
        except SystemExit as exc:
            if exc.code != 0:
                failures.append(f"client import guard must exit 0, got {exc.code!r}")
        else:
            failures.append("client import guard should exit on an old interpreter")
    _one_line(failures, "client import", stderr.getvalue())


def check_installer_guard(failures):
    sys.path.insert(0, REPO)
    spec_path = os.path.join(REPO, "install.py")
    stderr = io.StringIO()
    with mock.patch.object(sys, "version_info", OLD), redirect_stderr(stderr):
        namespace = runpy.run_path(spec_path, run_name="not_main")
        code = namespace["main"]()
    if code != 1:
        failures.append(f"installer must refuse an old interpreter, got {code!r}")
    _one_line(failures, "install.py", stderr.getvalue())


# AST node types that need Python newer than 3.4 to parse.
_NEW_SYNTAX = (
    ast.JoinedStr,
    ast.AnnAssign,
    ast.NamedExpr,
    ast.AsyncFunctionDef,
    ast.Await,
)


def _new_syntax_in(path):
    """Constructs in `path` that a Python 3.4 parser rejects."""
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source, feature_version=(3, 7))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, _NEW_SYNTAX):
            found.append((node.lineno, type(node).__name__))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            every = arguments.posonlyargs + arguments.args + arguments.kwonlyargs
            every += [a for a in (arguments.vararg, arguments.kwarg) if a]
            if node.returns or any(a.annotation for a in every):
                found.append((node.lineno, "annotation"))
            if arguments.posonlyargs:
                found.append((node.lineno, "positional-only parameter"))
        if isinstance(node, ast.Dict) and None in node.keys:
            found.append((node.lineno, "dict unpacking"))
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NUMBER and "_" in token.string:
            found.append((token.start[0], "numeric underscore"))
    return sorted(set(found))


def check_guard_files_parse_as_old_python(failures):
    for name in GUARD_FILES:
        for line, what in _new_syntax_in(os.path.join(REPO, name)):
            failures.append(f"{name}:{line}: {what} breaks the old-Python guard")


def main():
    failures = []
    for check in (
        check_floor_helpers,
        check_bare_python3_commands,
        check_absolute_path_entries_convert_back,
        check_hook_guard,
        check_client_guard,
        check_installer_guard,
        check_guard_files_parse_as_old_python,
    ):
        check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: floor is 3.9, hooks use bare python3 and convert pinned entries, "
        "and every entry point guards below the floor in old-Python syntax"
    )


if __name__ == "__main__":
    main()
