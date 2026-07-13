"""Verify the vendored statusline_lib/process_safe.py (run_captured,
run_inherit, spawn_detached, ProcessTimeout) -- the hang-proof subprocess
wrapper that statusline.py's _git_command and statusline_lib/walker.py's
_walker_subcommand now route through instead of bare subprocess.run.

Most checks here spawn a real short-lived Python child: the whole point of
this module is the abandon-reader-thread behavior around a real OS process,
which a mocked Popen can't exercise faithfully. The two purely defensive
except branches (a reader thread whose communicate() call raises, and a
kill() that raises because the child is already gone) are exercised with a
fake Popen instead, since triggering them via a real process is racy.

Run from anywhere; imports from schoen-claude-status by path.
"""

import os
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_lib.process_safe as process_safe_module
from statusline_lib.process_safe import (
    ProcessTimeout,
    run_captured,
    run_inherit,
    spawn_detached,
)

_PY = sys.executable


def _check_success_captures_stdout(failures):
    result = run_captured([_PY, "-c", "print('hello')"], timeout=5)
    if result.returncode != 0 or result.stdout.strip() != "hello":
        failures.append(f"success: expected rc=0 stdout='hello', got {result!r}")


def _check_nonzero_returncode_and_stderr(failures):
    code = "import sys; sys.stderr.write('boom'); sys.exit(2)"
    result = run_captured([_PY, "-c", code], timeout=5)
    if result.returncode != 2 or "boom" not in result.stderr:
        failures.append(f"nonzero exit: expected rc=2 stderr~'boom', got {result!r}")


def _check_check_true_raises_on_failure(failures):
    code = "import sys; sys.exit(5)"
    try:
        run_captured([_PY, "-c", code], timeout=5, check=True)
        failures.append("check=True with nonzero exit must raise CalledProcessError")
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 5:
            failures.append(
                f"CalledProcessError.returncode: expected 5, got {exc.returncode}"
            )


def _check_check_true_no_raise_on_success(failures):
    result = run_captured([_PY, "-c", "pass"], timeout=5, check=True)
    if result.returncode != 0:
        failures.append(f"check=True with rc=0 must not raise; got {result!r}")


def _check_launch_failure_propagates_oserror(failures):
    raised = False
    try:
        run_captured(["definitely-not-a-real-binary-xyz123"], timeout=5)
    except OSError:
        raised = True
    if not raised:
        failures.append("launching a nonexistent binary must raise OSError")


def _check_env_override_reaches_child(failures):
    code = (
        "import os, sys; "
        "sys.exit(0 if os.environ.get('PROCESS_SAFE_TEST_VAR') == 'hello' else 1)"
    )
    env = dict(os.environ)
    env["PROCESS_SAFE_TEST_VAR"] = "hello"
    result = run_captured([_PY, "-c", code], timeout=5, env=env)
    if result.returncode != 0:
        failures.append("env= override did not reach the child process")


def _check_cwd_is_honored(failures):
    with tempfile.TemporaryDirectory() as tmp:
        real_tmp = os.path.realpath(tmp)
        code = "import os, sys; sys.stdout.write(os.path.realpath(os.getcwd()))"
        result = run_captured([_PY, "-c", code], timeout=5, cwd=tmp)
        if not os.path.samefile(result.stdout.strip(), real_tmp):
            failures.append(
                f"cwd= not honored: expected {real_tmp!r}, got {result.stdout.strip()!r}"
            )


def _check_timeout_kills_child_and_raises(failures):
    code = "import time; time.sleep(30)"
    try:
        run_captured([_PY, "-c", code], timeout=0.3)
        failures.append("a child that outlives the timeout must raise ProcessTimeout")
    except ProcessTimeout as exc:
        if exc.timeout != 0.3:
            failures.append(f"ProcessTimeout.timeout: expected 0.3, got {exc.timeout}")


class _FakeProcess:
    """Stand-in for subprocess.Popen used to hit the two defensive except
    branches (reader-thread exception, kill() failure) that a real process
    can't reliably be made to trigger."""

    def __init__(self, communicate, kill):
        self.returncode = 0
        self._communicate = communicate
        self._kill = kill

    def communicate(self):
        return self._communicate()

    def kill(self):
        return self._kill()


def _check_reader_exception_is_swallowed(failures):
    def raising_communicate():
        raise ValueError("pipe vanished mid-read")

    original_popen = process_safe_module.subprocess.Popen
    process_safe_module.subprocess.Popen = lambda *a, **kw: _FakeProcess(
        raising_communicate, lambda: None
    )
    try:
        result = run_captured(["ignored"], timeout=5)
    finally:
        process_safe_module.subprocess.Popen = original_popen

    if result.stdout != "" or result.stderr != "":
        failures.append(
            f"a reader() exception must degrade to empty captured output; got {result!r}"
        )


def _check_kill_oserror_is_swallowed(failures):
    never = threading.Event()

    def blocking_communicate():
        never.wait()
        return "", ""

    def raising_kill():
        raise OSError("child already gone")

    original_popen = process_safe_module.subprocess.Popen
    process_safe_module.subprocess.Popen = lambda *a, **kw: _FakeProcess(
        blocking_communicate, raising_kill
    )
    raised = False
    try:
        run_captured(["ignored"], timeout=0.05)
    except ProcessTimeout:
        raised = True
    finally:
        process_safe_module.subprocess.Popen = original_popen
    if not raised:
        failures.append("a still-alive reader thread must still raise ProcessTimeout")


def _check_run_inherit_forwards_returncode(failures):
    class FakeCompleted:
        returncode = 9

    original_run = process_safe_module.subprocess.run
    captured_command = []
    process_safe_module.subprocess.run = lambda command: (
        captured_command.append(command),
        FakeCompleted(),
    )[1]
    try:
        rc = run_inherit([_PY, "-c", "pass"])
    finally:
        process_safe_module.subprocess.run = original_run

    if rc != 9:
        failures.append(f"run_inherit: expected forwarded rc=9, got {rc}")
    if captured_command != [[_PY, "-c", "pass"]]:
        failures.append(f"run_inherit: command not forwarded, got {captured_command!r}")


def _check_spawn_detached_default_stdio(failures):
    calls = []
    original_popen = process_safe_module.subprocess.Popen
    process_safe_module.subprocess.Popen = lambda *a, **kw: (
        calls.append((a, kw)) or "sentinel"
    )
    try:
        result = spawn_detached([_PY, "-c", "pass"])
    finally:
        process_safe_module.subprocess.Popen = original_popen

    if result != "sentinel":
        failures.append(f"spawn_detached must return Popen's result, got {result!r}")
    _, kwargs = calls[0]
    if (
        kwargs.get("stdout") is not subprocess.DEVNULL
        or kwargs.get("stderr") is not subprocess.DEVNULL
    ):
        failures.append(f"spawn_detached default stdio must be DEVNULL, got {kwargs!r}")
    if kwargs.get("start_new_session") is not True:
        failures.append("spawn_detached must set start_new_session=True")


def _check_spawn_detached_explicit_stdio_and_env(failures):
    calls = []
    original_popen = process_safe_module.subprocess.Popen
    process_safe_module.subprocess.Popen = lambda *a, **kw: (
        calls.append((a, kw)) or "sentinel"
    )
    sentinel_out, sentinel_err = object(), object()
    try:
        spawn_detached(
            [_PY, "-c", "pass"],
            stdout=sentinel_out,
            stderr=sentinel_err,
            env={"FOO": "bar"},
        )
    finally:
        process_safe_module.subprocess.Popen = original_popen

    _, kwargs = calls[0]
    if (
        kwargs.get("stdout") is not sentinel_out
        or kwargs.get("stderr") is not sentinel_err
    ):
        failures.append(
            f"spawn_detached did not forward explicit stdio, got {kwargs!r}"
        )
    if kwargs.get("env") != {"FOO": "bar"}:
        failures.append(f"spawn_detached did not forward env, got {kwargs!r}")


def main():
    failures = []
    _check_success_captures_stdout(failures)
    _check_nonzero_returncode_and_stderr(failures)
    _check_check_true_raises_on_failure(failures)
    _check_check_true_no_raise_on_success(failures)
    _check_launch_failure_propagates_oserror(failures)
    _check_env_override_reaches_child(failures)
    _check_cwd_is_honored(failures)
    _check_timeout_kills_child_and_raises(failures)
    _check_reader_exception_is_swallowed(failures)
    _check_kill_oserror_is_swallowed(failures)
    _check_run_inherit_forwards_returncode(failures)
    _check_spawn_detached_default_stdio(failures)
    _check_spawn_detached_explicit_stdio_and_env(failures)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: process_safe run_captured/run_inherit/spawn_detached all verified")


if __name__ == "__main__":
    main()
