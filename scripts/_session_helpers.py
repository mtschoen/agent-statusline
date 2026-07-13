"""Shared fake-psutil fixtures for verify_session_tree.py and
verify_session_child_env.py -- split out to keep both files under aislop's
400-line file-size gate (same split pattern as _walker_helpers.py)."""

from typing import ClassVar


class FakeSnapProc:
    """Fake psutil process for the lazy-snapshot _count_via_psutil.

    `meta_raises` simulates ppid()/create_time() failing (AccessDenied) while
    the process is still visible in the name pre-filter pass.
    """

    def __init__(
        self,
        pid,
        ppid,
        name,
        create_time,
        cmdline=None,
        cwd=None,
        meta_raises=None,
        env=None,
    ):
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self._ppid = ppid
        self._name = name
        self._create_time = create_time
        self._cmdline = cmdline
        self._cwd = cwd
        self._meta_raises = meta_raises
        self._env = env
        self.meta_calls = 0

    def ppid(self):
        self.meta_calls += 1
        if self._meta_raises is not None:
            raise self._meta_raises
        return self._ppid

    def name(self):
        return self._name

    def create_time(self):
        if self._meta_raises is not None:
            raise self._meta_raises
        return self._create_time

    def cmdline(self):
        if isinstance(self._cmdline, Exception):
            raise self._cmdline
        return self._cmdline

    def cwd(self):
        if isinstance(self._cwd, Exception):
            raise self._cwd
        return self._cwd

    def environ(self):
        if isinstance(self._env, Exception):
            raise self._env
        return self._env


class FakePsutilError(Exception):
    pass


class FakePsutilNoSuchProcess(Exception):
    def __init__(self, pid=0):
        super().__init__(pid)


def make_fake_psutil(procs, hidden=()):
    """`procs` appear in process_iter AND Process(); `hidden` only in
    Process() -- simulates a pid that started after the name-scan pass."""
    by_pid = {p.pid: p for p in list(procs) + list(hidden)}

    class FakePsutil:
        NoSuchProcess = FakePsutilNoSuchProcess
        AccessDenied = FakePsutilError
        seen_attrs: ClassVar[list] = []

        @staticmethod
        def process_iter(attrs):
            FakePsutil.seen_attrs.append(list(attrs))
            yield from procs

        @staticmethod
        def Process(pid):
            proc = by_pid.get(pid)
            if proc is None:
                raise FakePsutilNoSuchProcess(pid)
            return proc

    return FakePsutil
