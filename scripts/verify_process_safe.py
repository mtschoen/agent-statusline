"""Verify statusline_lib/process_safe.py subprocess wrapper."""

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_lib.process_safe as process_safe_module
from statusline_lib.process_safe import (
    ProcessTimeout,
    run_captured,
    run_inherit,
    spawn_detached,
)

_PY = sys.executable
_TIMEOUT = 5
_EXPECTED_TREE_KILL_TIMEOUT_SECONDS = 3


def _read_pid_file(path):
    with contextlib.suppress(OSError, ValueError), open(path) as f:
        return int(f.read().strip())
    return None


def _terminate_pid(pid):
    if pid is None:
        return
    if os.name == "nt":
        cf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            cmd = ["taskkill", "/F", "/T", "/PID", str(pid)]
            subprocess.run(cmd, capture_output=True, timeout=5, creationflags=cf)
    else:
        sig = getattr(signal, "SIGKILL", 9)
        for fn in (os.killpg, os.kill):
            with contextlib.suppress(OSError):
                fn(pid, sig)
        with contextlib.suppress(OSError):
            os.waitpid(pid, os.WNOHANG)


@contextlib.contextmanager
def _mock_popen(target):
    orig = process_safe_module.subprocess.Popen
    process_safe_module.subprocess.Popen = (
        target if callable(target) else (lambda *a, **kw: target)
    )
    try:
        yield
    finally:
        process_safe_module.subprocess.Popen = orig


def _check_run_captured_basics(failures):
    c = [_PY, "-c"]
    env = {**os.environ, "K": "v"}
    r1 = run_captured([*c, "print('hello')"], timeout=_TIMEOUT)
    r2 = run_captured(
        [*c, "import sys; sys.stderr.write('boom'); sys.exit(2)"], timeout=_TIMEOUT
    )
    r3 = run_captured([*c, "pass"], timeout=_TIMEOUT, check=True)
    r4 = run_captured(
        [*c, "import os; exit(os.getenv('K') != 'v')"], timeout=_TIMEOUT, env=env
    )
    if (
        (r1.returncode, r1.stdout.strip()) != (0, "hello")
        or (r2.returncode, "boom" in r2.stderr) != (2, True)
        or r3.returncode != 0
        or r4.returncode != 0
    ):
        failures.append(f"run_captured basics: {r1!r}, {r2!r}, {r3!r}, {r4!r}")

    r5, r6 = False, False
    try:
        run_captured([*c, "import sys; sys.exit(5)"], timeout=_TIMEOUT, check=True)
    except subprocess.CalledProcessError as exc:
        r5 = exc.returncode == 5
    try:
        run_captured(["definitely-not-a-real-binary-xyz123"], timeout=_TIMEOUT)
    except OSError:
        r6 = True
    if not (r5 and r6):
        failures.append(f"run_captured errors: r5={r5}, r6={r6}")

    with tempfile.TemporaryDirectory() as tmp:
        res = run_captured(
            [*c, "import os; print(os.getcwd())"], timeout=_TIMEOUT, cwd=tmp
        )
        if not os.path.samefile(res.stdout.strip(), tmp):
            failures.append(f"cwd= not honored: got {res.stdout.strip()!r}")


def _check_timeout_kills_child_and_raises(failures):
    try:
        run_captured([_PY, "-c", "import time; time.sleep(30)"], timeout=0.3)
        failures.append("a child that outlives the timeout must raise ProcessTimeout")
    except ProcessTimeout as exc:
        if exc.timeout != 0.3:
            failures.append(f"ProcessTimeout.timeout: expected 0.3, got {exc.timeout}")


_GRANDCHILD_CODE = (
    "import os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
    "os.write(fd, b'x'); os.lseek(fd, 0, 0)\n"
    "try:\n"
    " import msvcrt; msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)\n"
    "except ImportError:\n"
    " import fcntl; fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "open(sys.argv[2], 'w').write(str(os.getpid())); time.sleep(60)"
)
_CHILD_CODE = (
    "import os, subprocess, sys, time\n"
    "open(sys.argv[4], 'w').write(str(os.getpid()))\n"
    "c = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])\n"
    "open(sys.argv[5], 'w').write(str(c.pid))\n"
    "while not os.path.exists(sys.argv[3]): time.sleep(0.05)\n"
    "time.sleep(60)"
)


def _try_take_lock(lock_path):
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _check_timeout_kills_grandchild(failures):
    with tempfile.TemporaryDirectory() as tmp:
        lock_path, ready_path = os.path.join(tmp, "lock"), os.path.join(tmp, "ready")
        c_pid, gc_pid = os.path.join(tmp, "c.pid"), os.path.join(tmp, "gc.pid")
        child_procs = []
        orig_popen = process_safe_module.subprocess.Popen

        def popen_hook(*a, **kw):
            p = orig_popen(*a, **kw)
            if not child_procs:
                child_procs.append(p)
            return p

        try:
            timed_out = False
            cmd = [
                _PY,
                "-c",
                _CHILD_CODE,
                _GRANDCHILD_CODE,
                lock_path,
                ready_path,
                c_pid,
                gc_pid,
            ]
            try:
                with _mock_popen(popen_hook):
                    run_captured(cmd, timeout=_TIMEOUT)
            except ProcessTimeout:
                timed_out = True
            if not timed_out or not os.path.exists(ready_path):
                failures.append("grandchild test: child did not start/time out")
                return
            deadline = time.monotonic() + _TIMEOUT
            while not _try_take_lock(lock_path):
                if time.monotonic() > deadline:
                    failures.append("timed-out grandchild must die; lock held")
                    return
                time.sleep(0.05)
        finally:
            for path in (c_pid, gc_pid, ready_path):
                _terminate_pid(_read_pid_file(path))
            for p in list(child_procs):
                _terminate_pid(p.pid)
                with contextlib.suppress(Exception):
                    p.wait(timeout=1)


class _FakeProcess:
    pid = 4242
    returncode = 0

    def __init__(self, communicate, kill=lambda: None):
        self.communicate = communicate
        self.kill = kill


def _check_reader_exception_is_swallowed(failures):
    fake = _FakeProcess(lambda: (_ for _ in ()).throw(ValueError("pipe vanished")))
    with _mock_popen(fake):
        res = run_captured(["ignored"], timeout=_TIMEOUT)
    if res.stdout != "" or res.stderr != "":
        failures.append(f"reader() exception must degrade to empty output; got {res!r}")


@contextlib.contextmanager
def _patched_tree_kill(os_name, run_err=None, killpg_err=None):
    rec = {"taskkill": [], "killpg": []}

    def fake_run(argv, **kw):
        rec["taskkill"].append((argv, kw))
        if run_err:
            raise run_err

    def fake_killpg(pid, sig):
        rec["killpg"].append((pid, sig))
        if killpg_err:
            raise killpg_err

    orig_run, orig_os = process_safe_module.subprocess.run, process_safe_module.os.name
    orig_killpg = getattr(process_safe_module.os, "killpg", None)
    process_safe_module.subprocess.run, process_safe_module.os.name = fake_run, os_name
    process_safe_module.os.killpg = fake_killpg
    try:
        yield rec
    finally:
        process_safe_module.subprocess.run = orig_run
        process_safe_module.os.name = orig_os
        if orig_killpg is None:
            del process_safe_module.os.killpg
        else:
            process_safe_module.os.killpg = orig_killpg


def _check_kill_oserror_is_swallowed(failures):
    never = threading.Event()
    fake = _FakeProcess(
        lambda: (never.wait(), ("", ""))[1],
        lambda _n=never: (_n.set(), (_ for _ in ()).throw(OSError("child gone")))[1],
    )
    try:
        with _patched_tree_kill("posix") as rec, _mock_popen(fake):
            raised = False
            try:
                run_captured(["ignored"], timeout=0.05)
            except ProcessTimeout:
                raised = True
        sig = getattr(process_safe_module.signal, "SIGKILL", 9)
        if not raised or rec["killpg"] != [(4242, sig)]:
            failures.append(f"posix timeout error: raised={raised}, rec={rec!r}")
    finally:
        never.set()


def _check_tree_kill(failures):
    expired = subprocess.TimeoutExpired("taskkill", _EXPECTED_TREE_KILL_TIMEOUT_SECONDS)
    cases = [("nt", r, None) for r in (None, expired, OSError("gone"))] + [
        ("posix", None, r) for r in (None, ProcessLookupError("no such group"))
    ]
    for os_name, run_err, killpg_err in cases:
        kills, block = [], threading.Event()
        fake = _FakeProcess(
            lambda _b=block: (_b.wait(), ("", ""))[1],
            lambda _k=kills, _b=block: (_k.append("kill"), _b.set()),
        )
        try:
            with (
                _windows_constants(),
                _patched_tree_kill(os_name, run_err, killpg_err) as rec,
                _mock_popen(fake),
            ):
                raised = False
                try:
                    run_captured(["ignored"], timeout=0.01)
                except ProcessTimeout:
                    raised = True

                if not raised:
                    failures.append(f"{os_name} tree kill must raise ProcessTimeout")
                if os_name == "nt":
                    argv, kw = rec["taskkill"][0] if rec["taskkill"] else ([], {})
                    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    bad = (
                        len(rec["taskkill"]) != 1
                        or argv != ["taskkill", "/T", "/F", "/PID", "4242"]
                        or kw.get("timeout") != _EXPECTED_TREE_KILL_TIMEOUT_SECONDS
                        or not kw.get("creationflags", 0) & no_win
                        or rec["killpg"]
                        or kills != ["kill"]
                    )
                else:
                    sig = getattr(process_safe_module.signal, "SIGKILL", 9)
                    bad = (
                        rec["killpg"] != [(4242, sig)]
                        or rec["taskkill"]
                        or kills != ["kill"]
                    )
                if bad:
                    failures.append(f"{os_name} mismatch: rec={rec!r}, kills={kills!r}")
        finally:
            block.set()


def _check_run_inherit_forwards_returncode(failures):
    orig = process_safe_module.subprocess.run
    captured = []
    process_safe_module.subprocess.run = lambda cmd: (
        captured.append(cmd),
        type("R", (), {"returncode": 9})(),
    )[1]
    try:
        rc = run_inherit([_PY, "-c", "pass"])
    finally:
        process_safe_module.subprocess.run = orig
    if rc != 9 or captured != [[_PY, "-c", "pass"]]:
        failures.append(f"run_inherit failed: rc={rc}, captured={captured!r}")


@contextlib.contextmanager
def _windows_constants():
    fakes = {
        "CREATE_NO_WINDOW": 0x08000000,
        "CREATE_NEW_PROCESS_GROUP": 0x00000200,
        "STARTF_USESHOWWINDOW": 1,
        "STARTUPINFO": type("STARTUPINFO", (), {"dwFlags": 0, "wShowWindow": 1}),
    }
    installed = [k for k in fakes if not hasattr(subprocess, k)]
    for k in installed:
        setattr(subprocess, k, fakes[k])
    try:
        yield
    finally:
        for k in installed:
            delattr(subprocess, k)


def _check_spawn_kwargs(failures):
    fns = (
        (lambda: spawn_detached([_PY, "-c", "pass"]), "spawn_detached", "sentinel"),
        (
            lambda: run_captured(["ignored"], timeout=_TIMEOUT),
            "run_captured",
            _FakeProcess(lambda: ("", "")),
        ),
    )
    with _windows_constants():
        for fn, name, fake in fns:
            for os_name in ("posix", "nt"):
                calls = []
                orig_os = process_safe_module.os.name
                process_safe_module.os.name = os_name
                try:
                    with _mock_popen(
                        lambda *a, _c=calls, _f=fake, **kw: _c.append((a, kw)) or _f
                    ):
                        res = fn()
                finally:
                    process_safe_module.os.name = orig_os
                if name == "spawn_detached" and res is not fake:
                    failures.append(f"spawn_detached[{os_name}] returned {res!r}")
                _, kw = calls[0]
                flags = kw.get("creationflags", 0)
                no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                new_grp = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                bad = (
                    kw.get("start_new_session") is not True or "creationflags" in kw
                    if os_name == "posix"
                    else "start_new_session" in kw
                    or not (flags & no_win)
                    or not (flags & new_grp)
                    or kw.get("startupinfo") is None
                )
                if bad:
                    failures.append(f"{name}[{os_name}] bad kwargs: {kw!r}")
                if name == "spawn_detached" and (
                    kw.get("stdout") is not subprocess.DEVNULL
                    or kw.get("stderr") is not subprocess.DEVNULL
                ):
                    failures.append(f"spawn_detached[{os_name}] stdio not DEVNULL")


def _check_spawn_detached_explicit_stdio_and_env(failures):
    calls = []
    fake = type("FakePopen", (), {})()
    with _mock_popen(lambda *a, **kw: calls.append((a, kw)) or fake):
        out, err = object(), object()
        res = spawn_detached(
            [_PY, "-c", "pass"], stdout=out, stderr=err, env={"FOO": "bar"}
        )
    if res is not fake:
        failures.append(f"spawn_detached returned {res!r}")
    _, kw = calls[0]
    if (
        kw.get("stdout") is not out
        or kw.get("stderr") is not err
        or kw.get("env") != {"FOO": "bar"}
    ):
        failures.append(f"spawn_detached forwarding failed: {kw!r}")


def main():
    failures = []
    _check_run_captured_basics(failures)
    _check_timeout_kills_child_and_raises(failures)
    _check_timeout_kills_grandchild(failures)
    _check_tree_kill(failures)
    _check_reader_exception_is_swallowed(failures)
    _check_kill_oserror_is_swallowed(failures)
    _check_run_inherit_forwards_returncode(failures)
    _check_spawn_kwargs(failures)
    _check_spawn_detached_explicit_stdio_and_env(failures)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: process_safe run_captured/run_inherit/spawn_detached all verified")


if __name__ == "__main__":
    main()
