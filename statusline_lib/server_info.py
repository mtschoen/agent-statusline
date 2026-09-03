"""Server info file (server.json) and the code-version digest.

The resident server records its pid and UDP port in
`state_dir()/server.json` so the thin client can find it without any
coordination beyond that one small file. The file also carries a
`code_version` digest of the checkout's Python sources: the client computes
the same digest from its own standalone copy of `code_version` (it cannot
import this package -- statusline_client.py is deliberately dependency-free)
and compares it against the value the running server recorded. A mismatch
means the checkout moved since that server started, so the client asks it to
shut down and spawns a replacement.

This module owns both the file format and the version algorithm; a later
task's client-side copy of `code_version` is diffed against
`version_input_files` to keep the two in lockstep.

statusline-ctl's client side of the control protocol (sending the `status`
and `shutdown` datagrams and polling for this file to disappear afterward)
is server_control.py, not here: that code is a blocking poll loop that is
never reached by a render, and scripts/verify_render_budget_static.py
statically bans blocking waits across every other file in this package.

Imports:
  base -- for state_dir
  process_snapshot -- for _resolve_psutil, the package's one psutil import
                      seam (the session-count scan resolves it there too)
"""

import contextlib
import ctypes
import hashlib
import json
import os

from .base import state_dir
from .process_snapshot import _resolve_psutil

SERVER_INFO_FILENAME = "server.json"

# Entry-point glue at the repository root that isn't under statusline_lib/
# but still changes the running code's behavior, so a checkout move that
# only touches one of these must still be detected.
_ROOT_ENTRY_POINTS = (
    "statusline.py",
    "subagent_statusline.py",
    "qwen_statusline.py",
    "kimi_statusline.py",
    "statusline_client.py",
    "statusline_client_support.py",
    "statusline_server.py",
)


def server_info_path(state_directory=None):
    """Absolute path to the server info file under state_dir(state_directory)."""
    return os.path.join(state_dir(state_directory), SERVER_INFO_FILENAME)


def version_input_files(repository_root):
    """Sorted, repository-relative (forward-slash) names of every file that
    feeds code_version: every *.py under statusline_lib/, plus the root
    entry points. Exported so a later task can diff the client's standalone
    copy of this file list against the real one."""
    names = list(_ROOT_ENTRY_POINTS)
    library_directory = os.path.join(repository_root, "statusline_lib")
    for root, _directories, files in os.walk(library_directory):
        for file_name in files:
            if not file_name.endswith(".py"):
                continue
            absolute_path = os.path.join(root, file_name)
            relative_path = os.path.relpath(absolute_path, repository_root)
            names.append(relative_path.replace(os.sep, "/"))
    return sorted(names)


def code_version(repository_root):
    """A short digest of this checkout's Python sources.

    The client computes the same digest for the same tree with its own copy of
    this function (statusline_client.py cannot import statusline_lib), and a
    mismatch against the running server's recorded version means the checkout
    moved: the client asks that server to shut down and spawns a replacement.
    Stat metadata rather than file contents, because this runs on the client's
    hot path and a content hash of the whole package would not fit the budget.
    Mtimes are truncated to whole seconds: Windows and Linux report different
    sub-second resolution for the same tree.
    """
    digest = hashlib.sha256()
    for name in version_input_files(repository_root):
        path = os.path.join(repository_root, name.replace("/", os.sep))
        try:
            stat_result = os.stat(path)
        except OSError:
            digest.update(f"{name}\0missing\n".encode())
            continue
        digest.update(
            f"{name}\0{stat_result.st_size}\0{int(stat_result.st_mtime)}\n".encode()
        )
    return digest.hexdigest()[:16]


def write_server_info(path, *, pid, port, version, started_at, platform):
    """Atomically write the server info file: a pid-scoped tmp file plus
    os.replace, so a client never reads a partially-written file."""
    payload = {
        "pid": pid,
        "port": port,
        "version": version,
        "started_at": started_at,
        "platform": platform,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def read_server_info(path):
    """The server info dict at `path`, or None if the file is missing,
    unreadable, or does not parse as a JSON object."""
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def remove_server_info(path):
    """Best-effort removal of the server info file; a missing or unremovable
    path must never raise -- the caller is tearing down, not depending on
    this succeeding."""
    with contextlib.suppress(OSError):
        os.remove(path)


# Windows liveness probe constants (kernel32 / OpenProcess). Signal 0 is not
# a liveness check on Windows: os.kill maps signal 0 onto CTRL_C_EVENT
# (both are the integer 0), so os.kill(pid, 0) sends a console-control event
# instead of testing existence -- it silently does nothing to an already-
# exited pid (no exception, read as alive) and raises a plain OSError for a
# pid that never existed (read as undetermined). OpenProcess plus
# GetExitCodeProcess is the real liveness check on this platform.
_WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOWS_STILL_ACTIVE = 259
_WINDOWS_ERROR_INVALID_PARAMETER = 87
_WINDOWS_ERROR_ACCESS_DENIED = 5


def _resolve_kernel32():
    """The Windows kernel32 DLL handle used by the liveness probe. A seam
    so tests can stub it on any operating system, since ctypes.windll only
    exists on real Windows."""
    return ctypes.windll.kernel32


def _pid_is_alive_windows(pid):
    """Windows liveness probe: OpenProcess plus GetExitCodeProcess, since
    os.kill(pid, 0) is not a liveness check on this platform (see the
    constants above)."""
    kernel32 = _resolve_kernel32()
    handle = kernel32.OpenProcess(
        _WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        error = kernel32.GetLastError()
        if error == _WINDOWS_ERROR_INVALID_PARAMETER:
            return False
        if error == _WINDOWS_ERROR_ACCESS_DENIED:
            return True
        return None
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.pointer(exit_code)):
            return None
        return exit_code.value == _WINDOWS_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_is_alive(pid):
    """Whether process `pid` is currently alive: True, False, or None when
    undetermined (an invalid pid, or a lookup that raised something other
    than "no such process" / "permission denied")."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    psutil_module = _resolve_psutil()
    if psutil_module is not None:
        with contextlib.suppress(AttributeError, TypeError, ValueError, OSError):
            return bool(psutil_module.pid_exists(pid))
    if os.name == "nt":
        return _pid_is_alive_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
