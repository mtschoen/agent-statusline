"""Verify statusline_lib/server_info.py: pid_is_alive and _resolve_psutil.

Split out of verify_server_info.py (which covers the rest of the module) so
neither file grows past the 400-line file-size cap.

Covers:
  - pid_is_alive: invalid-input None, the current process True, and an
    impossible pid False (via the real, installed psutil).
  - The psutil branch: True/False results, and a psutil error falling back
    to the next probe.
  - The POSIX os.kill branch (forced by patching os.name to "posix", since
    this may run on real Windows): success, ProcessLookupError,
    PermissionError, and a general OSError.
  - The Windows OpenProcess/GetExitCodeProcess probe (forced by patching
    os.name to "nt", since this may run on real POSIX in CI), stubbing
    _resolve_kernel32 so no real ctypes.windll call is needed on either
    operating system: ERROR_INVALID_PARAMETER, ERROR_ACCESS_DENIED, an
    unrecognized error, STILL_ACTIVE, a non-STILL_ACTIVE exit code, and a
    failing GetExitCodeProcess -- plus that the process handle is closed.
  - One unmocked check against the real, running platform (psutil forced
    off): the current process reports True, and a freshly-exited child
    process (via process_safe.spawn_detached, since raw subprocess.Popen is
    banned) reports False. This is the regression test for the bug this
    round fixed: on Windows, os.kill(pid, 0) is not a liveness check
    (signal 0 equals CTRL_C_EVENT), so it silently did nothing to an
    already-exited pid (read as alive) and raised a plain OSError for a pid
    that never existed (read as undetermined).
  - _resolve_psutil across the installed-module and ImportError arms.
  - The real, unstubbed _resolve_kernel32 body: on Windows it must return a
    truthy handle, and on POSIX (where ctypes has no windll attribute) it
    must raise AttributeError.

Run from anywhere; imports from `agent-statusline` by path.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.server_info as server_info
from statusline_lib.process_safe import spawn_detached


def _check_pid_is_alive_basics(failures):
    for invalid in (None, 0, -1, "123", 12.5):
        if server_info.pid_is_alive(invalid) is not None:
            failures.append(f"pid_is_alive({invalid!r}) must return None")

    if server_info.pid_is_alive(os.getpid()) is not True:
        failures.append("pid_is_alive(os.getpid()) must be True")

    impossible_pid = 2**31 - 1
    if server_info.pid_is_alive(impossible_pid) is not False:
        failures.append("pid_is_alive for a pid that cannot exist must be False")


def _check_pid_is_alive_psutil_branch(failures):
    # The os.kill fallback below is exercised on the (patched) POSIX arm;
    # the Windows probe when psutil is unavailable has its own dedicated
    # check, _check_pid_is_alive_windows_probe.
    saved_resolve = server_info._resolve_psutil
    saved_os_name = os.name
    try:
        fake_psutil = types.SimpleNamespace(pid_exists=lambda pid: pid == 111)
        server_info._resolve_psutil = lambda: fake_psutil
        if server_info.pid_is_alive(111) is not True:
            failures.append("pid_is_alive via psutil True failed")
        if server_info.pid_is_alive(222) is not False:
            failures.append("pid_is_alive via psutil False failed")

        def raising_pid_exists(pid):
            raise OSError("psutil error")

        fake_psutil.pid_exists = raising_pid_exists
        os.name = "posix"
        saved_kill = os.kill
        try:
            os.kill = lambda pid, sig: None
            if server_info.pid_is_alive(111) is not True:
                failures.append(
                    "pid_is_alive did not fall back to os.kill after a psutil error"
                )
        finally:
            os.kill = saved_kill
    finally:
        server_info._resolve_psutil = saved_resolve
        os.name = saved_os_name


def _check_pid_is_alive_os_kill_fallback(failures):
    """Forces the POSIX arm (patching os.name, since this may run on real
    Windows) with psutil unavailable, across the os.kill exception mapping."""
    saved_resolve = server_info._resolve_psutil
    saved_kill = os.kill
    saved_os_name = os.name
    try:
        server_info._resolve_psutil = lambda: None
        os.name = "posix"

        os.kill = lambda pid, sig: None
        if server_info.pid_is_alive(123) is not True:
            failures.append("pid_is_alive with os.kill success must be True")

        def lookup_error(pid, sig):
            raise ProcessLookupError()

        os.kill = lookup_error
        if server_info.pid_is_alive(123) is not False:
            failures.append("pid_is_alive with ProcessLookupError must be False")

        def permission_error(pid, sig):
            raise PermissionError()

        os.kill = permission_error
        if server_info.pid_is_alive(123) is not True:
            failures.append("pid_is_alive with PermissionError must be True")

        def other_os_error(pid, sig):
            raise OSError("mystery error")

        os.kill = other_os_error
        if server_info.pid_is_alive(123) is not None:
            failures.append("pid_is_alive with a general OSError must be None")
    finally:
        server_info._resolve_psutil = saved_resolve
        os.kill = saved_kill
        os.name = saved_os_name


class _FakeKernel32:
    """Stand-in for ctypes.windll.kernel32, so the Windows liveness probe
    can be exercised on any operating system without real ctypes.windll
    (which does not exist off Windows)."""

    def __init__(
        self, open_process_result, last_error=0, exit_code_success=True, exit_code=0
    ):
        self.open_process_result = open_process_result
        self.last_error = last_error
        self.exit_code_success = exit_code_success
        self.exit_code = exit_code
        self.closed_handles = []

    def OpenProcess(self, flags, inherit_handle, pid):
        return self.open_process_result

    def GetLastError(self):
        return self.last_error

    def GetExitCodeProcess(self, handle, exit_code_pointer):
        if not self.exit_code_success:
            return 0
        exit_code_pointer.contents.value = self.exit_code
        return 1

    def CloseHandle(self, handle):
        self.closed_handles.append(handle)


def _check_pid_is_alive_windows_probe(failures):
    """Forces the Windows arm (patching os.name, since this may run on real
    POSIX in CI) with psutil unavailable, stubbing _resolve_kernel32 so no
    real ctypes.windll call is needed on either operating system."""
    saved_resolve_psutil = server_info._resolve_psutil
    saved_resolve_kernel32 = server_info._resolve_kernel32
    saved_os_name = os.name
    try:
        server_info._resolve_psutil = lambda: None
        os.name = "nt"

        server_info._resolve_kernel32 = lambda: _FakeKernel32(
            open_process_result=0,
            last_error=server_info._WINDOWS_ERROR_INVALID_PARAMETER,
        )
        if server_info.pid_is_alive(123) is not False:
            failures.append("Windows probe with ERROR_INVALID_PARAMETER must be False")

        server_info._resolve_kernel32 = lambda: _FakeKernel32(
            open_process_result=0,
            last_error=server_info._WINDOWS_ERROR_ACCESS_DENIED,
        )
        if server_info.pid_is_alive(123) is not True:
            failures.append("Windows probe with ERROR_ACCESS_DENIED must be True")

        server_info._resolve_kernel32 = lambda: _FakeKernel32(
            open_process_result=0, last_error=1234
        )
        if server_info.pid_is_alive(123) is not None:
            failures.append("Windows probe with an unrecognized error must be None")

        active_fake = _FakeKernel32(
            open_process_result=42,
            exit_code_success=True,
            exit_code=server_info._WINDOWS_STILL_ACTIVE,
        )
        server_info._resolve_kernel32 = lambda: active_fake
        if server_info.pid_is_alive(123) is not True:
            failures.append("Windows probe with STILL_ACTIVE must be True")
        if active_fake.closed_handles != [42]:
            failures.append("Windows probe did not close the process handle")

        server_info._resolve_kernel32 = lambda: _FakeKernel32(
            open_process_result=42, exit_code_success=True, exit_code=0
        )
        if server_info.pid_is_alive(123) is not False:
            failures.append(
                "Windows probe with a non-STILL_ACTIVE exit code must be False"
            )

        server_info._resolve_kernel32 = lambda: _FakeKernel32(
            open_process_result=42, exit_code_success=False
        )
        if server_info.pid_is_alive(123) is not None:
            failures.append(
                "Windows probe with a failing GetExitCodeProcess must be None"
            )
    finally:
        server_info._resolve_psutil = saved_resolve_psutil
        server_info._resolve_kernel32 = saved_resolve_kernel32
        os.name = saved_os_name


def _check_pid_is_alive_real_platform(failures):
    """Unmocked check against the real, running platform (with psutil
    forced off so this actually exercises the real os.kill/Windows-probe
    arm): the current process reports True, and a freshly-exited subprocess
    child reports False."""
    saved_resolve_psutil = server_info._resolve_psutil
    try:
        server_info._resolve_psutil = lambda: None

        if server_info.pid_is_alive(os.getpid()) is not True:
            failures.append("pid_is_alive(os.getpid()) without psutil must be True")

        child = spawn_detached([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        if server_info.pid_is_alive(child.pid) is not False:
            failures.append(
                "pid_is_alive for a freshly-exited child pid without psutil must be False"
            )
    finally:
        server_info._resolve_psutil = saved_resolve_psutil


def _check_resolve_psutil(failures):
    resolved = server_info._resolve_psutil()
    if resolved is not None and not hasattr(resolved, "pid_exists"):
        failures.append(f"unexpected _resolve_psutil result: {resolved!r}")

    real_psutil = sys.modules.get("psutil")
    try:
        sys.modules["psutil"] = None
        if server_info._resolve_psutil() is not None:
            failures.append("_resolve_psutil with ImportError must return None")
    finally:
        if real_psutil is not None:
            sys.modules["psutil"] = real_psutil
        else:
            sys.modules.pop("psutil", None)


def _check_resolve_kernel32_real(failures):
    """Calls the real, unstubbed _resolve_kernel32 so its body executes on
    every operating system, not just the checks above that stub it out."""
    if os.name == "nt":
        try:
            handle = server_info._resolve_kernel32()
        except Exception as error:
            failures.append(f"_resolve_kernel32 raised on Windows: {error!r}")
        else:
            if not handle:
                failures.append("_resolve_kernel32 must return a truthy handle")
    else:
        raised = None
        try:
            server_info._resolve_kernel32()
        except Exception as error:
            raised = error
        if not isinstance(raised, AttributeError):
            if raised is None:
                failures.append("_resolve_kernel32 on POSIX must raise AttributeError")
            else:
                failures.append(
                    f"_resolve_kernel32 on POSIX must raise AttributeError, got {raised!r}"
                )


def check(failures):
    _check_pid_is_alive_basics(failures)
    _check_pid_is_alive_psutil_branch(failures)
    _check_pid_is_alive_os_kill_fallback(failures)
    _check_pid_is_alive_windows_probe(failures)
    _check_pid_is_alive_real_platform(failures)
    _check_resolve_psutil(failures)
    _check_resolve_kernel32_real(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: pid_is_alive and _resolve_psutil verified")


if __name__ == "__main__":
    main()
