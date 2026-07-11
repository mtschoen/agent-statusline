"""Verify the process-tree session classifier in `statusline_lib.sessions`.

Covers:
  - `_is_agent_runtime`: pure (name, cmdline) runtime classifier.
  - `_is_excluded_by_tree`: pure snapshot-walk rules -- agent-descendant
    exclusion, orphan (dead/recycled/init parent) exclusion, and the
    chain-breaks-above-a-live-shell / pid-reuse / cycle terminations.
  - `_count_via_psutil` end to end against a fake psutil: only shell-launched
    sessions count; helpers (intact-chain or disowned) and children of
    node-wrapped sessions do not; AccessDenied candidates are skipped.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import os
import sys
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def check_is_agent_runtime(failures):
    # Pure classifier: does (name, cmdline) look like a claude/qwen runtime
    # process at all, regardless of cwd or headless flags?
    from statusline_lib.sessions import _is_agent_runtime

    for name in ("claude", "claude.exe", "qwen", "qwen.exe"):
        if not _is_agent_runtime(name, [name]):
            failures.append(f"{name} should be an agent runtime")
    if not _is_agent_runtime("node", ["node", "/path/to/claude/cli.js"]):
        failures.append("node wrapping claude should be an agent runtime")
    if _is_agent_runtime("node", ["node", "server.js"]):
        failures.append("plain node app should NOT be an agent runtime")
    if _is_agent_runtime("python", ["python", "script.py"]):
        failures.append("python should NOT be an agent runtime")
    if _is_agent_runtime(None, None):
        failures.append("None name should NOT be an agent runtime")


def check_excluded_by_tree(failures):
    # Pure process-tree classifier over a synthesized {pid: (ppid, name,
    # create_time)} snapshot. `cmdline_of` is injected so no psutil is needed.
    from statusline_lib.sessions import _is_excluded_by_tree

    no_cmdline = {}.get  # cmdline lookup that always returns None

    # Real session: shell parent alive and older -> not excluded.
    snap = {
        100: (50, "claude.exe", 3000.0),
        50: (40, "cmd.exe", 2000.0),
        40: (1, "explorer.exe", 1000.0),
    }
    if _is_excluded_by_tree(100, snap, no_cmdline):
        failures.append("shell-launched session should not be excluded")

    # Real session whose chain breaks ABOVE the first ancestor (live shape:
    # terminal host gone, shell still alive) -> still not excluded.
    snap = {100: (50, "claude.exe", 3000.0), 50: (99999, "cmd.exe", 2000.0)}
    if _is_excluded_by_tree(100, snap, no_cmdline):
        failures.append("chain breaking above a live shell should still count")

    # Helper spawned by a session: claude ancestor anywhere in the chain.
    snap = {
        100: (60, "claude.exe", 3000.0),
        60: (55, "bash.exe", 2500.0),
        55: (50, "claude.exe", 2000.0),
        50: (1, "cmd.exe", 1000.0),
    }
    if not _is_excluded_by_tree(100, snap, no_cmdline):
        failures.append("descendant of a claude session should be excluded")

    # node-wrapped claude ancestor (npm-installed CLI): needs cmdline lookup.
    snap = {
        100: (60, "claude.exe", 3000.0),
        60: (1, "node.exe", 2000.0),
    }
    cmdlines = {60: ["node", "/x/claude-code/cli.js"]}
    if not _is_excluded_by_tree(100, snap, cmdlines.get):
        failures.append("descendant of node-wrapped claude should be excluded")
    # ...but a plain node ancestor (some dev server) is not an agent runtime.
    cmdlines = {60: ["node", "server.js"]}
    if _is_excluded_by_tree(100, snap, cmdlines.get):
        failures.append("plain node ancestor should not exclude")
    # ...and an unreadable node ancestor is treated as non-agent.
    if _is_excluded_by_tree(100, snap, no_cmdline):
        failures.append("unreadable node ancestor should not exclude")

    # Orphan: immediate parent gone from the snapshot (disowned helper, or a
    # zombie session whose terminal died) -> excluded.
    snap = {100: (99999, "claude.exe", 3000.0)}
    if not _is_excluded_by_tree(100, snap, no_cmdline):
        failures.append("candidate with a dead parent should be excluded")

    # Orphan by pid reuse: parent pid exists but was created AFTER the
    # candidate, so the real parent is gone.
    snap = {100: (50, "claude.exe", 3000.0), 50: (1, "cmd.exe", 9000.0)}
    if not _is_excluded_by_tree(100, snap, no_cmdline):
        failures.append("candidate with a recycled parent pid should be excluded")

    # Orphan by reparenting to init (Unix): ppid 1 is never a shell the user
    # launched from.
    snap = {100: (1, "claude", 3000.0), 1: (0, "systemd", 10.0)}
    if not _is_excluded_by_tree(100, snap, no_cmdline):
        failures.append("candidate reparented to init should be excluded")

    # Candidate itself missing from the snapshot (exited mid-scan) -> excluded.
    if not _is_excluded_by_tree(200, {}, no_cmdline):
        failures.append("candidate absent from snapshot should be excluded")

    # Pid-reuse ABOVE the first ancestor ends the walk without excluding.
    snap = {
        100: (50, "claude.exe", 3000.0),
        50: (45, "cmd.exe", 2000.0),
        45: (1, "claude.exe", 8000.0),  # newer than its child: recycled pid
    }
    if _is_excluded_by_tree(100, snap, no_cmdline):
        failures.append("recycled pid above a live shell should not exclude")

    # Cyclic ppid data must terminate and not exclude.
    snap = {
        100: (50, "claude.exe", 3000.0),
        50: (60, "cmd.exe", 2000.0),
        60: (50, "cmd.exe", 2000.0),
    }
    if _is_excluded_by_tree(100, snap, no_cmdline):
        failures.append("cyclic ppid chain should terminate without excluding")


class _FakeSnapProc:
    """Fake psutil process for the lazy-snapshot _count_via_psutil.

    `meta_raises` simulates ppid()/create_time() failing (AccessDenied) while
    the process is still visible in the name pre-filter pass.
    """

    def __init__(
        self, pid, ppid, name, create_time, cmdline=None, cwd=None, meta_raises=None
    ):
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self._ppid = ppid
        self._name = name
        self._create_time = create_time
        self._cmdline = cmdline
        self._cwd = cwd
        self._meta_raises = meta_raises
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


class _FakePsutilError(Exception):
    pass


class _FakePsutilNoSuchProcess(Exception):
    def __init__(self, pid=0):
        super().__init__(pid)


def _make_fake_psutil(procs, hidden=()):
    """`procs` appear in process_iter AND Process(); `hidden` only in
    Process() -- simulates a pid that started after the name-scan pass."""
    by_pid = {p.pid: p for p in list(procs) + list(hidden)}

    class FakePsutil:
        NoSuchProcess = _FakePsutilNoSuchProcess
        AccessDenied = _FakePsutilError
        seen_attrs: ClassVar[list] = []

        @staticmethod
        def process_iter(attrs):
            FakePsutil.seen_attrs.append(list(attrs))
            yield from procs

        @staticmethod
        def Process(pid):
            proc = by_pid.get(pid)
            if proc is None:
                raise _FakePsutilNoSuchProcess(pid)
            return proc

    return FakePsutil


def check_process_iter_uses_cheap_attrs(failures):
    # Perf contract, learned the hard way: process_iter(["name"]) reads one
    # toolhelp snapshot (~20ms for ~600 procs on Windows); asking for ppid or
    # create_time there forces an OpenProcess per pid (~11s observed). The
    # enumeration pass must stay name-only; the tree walk fetches the rest
    # lazily for the few pids it visits.
    import statusline_lib.sessions as sessions_mod

    target_cwd = os.path.normcase("/home/user/proj")
    fake = _make_fake_psutil(
        [
            _FakeSnapProc(50, 40, "cmd.exe", 200.0),
            _FakeSnapProc(100, 50, "claude", 300.0, ["claude"], target_cwd),
        ]
    )
    sessions_mod._count_via_psutil(target_cwd, fake)
    if fake.seen_attrs != [["name"]]:
        failures.append(
            f"process_iter must be called once with ['name'] only; got {fake.seen_attrs}"
        )


def check_lazy_snapshot(failures):
    # _LazySnapshot semantics: rows fetched once per pid (cached), dead pids
    # absent, AccessDenied pids present-but-unwalkable, None pid safe, and
    # names fall back to proc.name() for pids missing from the name scan.
    from statusline_lib.sessions import _LazySnapshot

    shell = _FakeSnapProc(50, 40, "cmd.exe", 200.0)
    denied = _FakeSnapProc(60, 40, "cmd.exe", 210.0, meta_raises=_FakePsutilError())
    late = _FakeSnapProc(70, 40, "pwsh.exe", 220.0)
    fake = _make_fake_psutil([shell, denied], hidden=[late])
    snap = _LazySnapshot(fake, {50: "cmd.exe", 60: "cmd.exe"})

    if snap.get(50) != (40, "cmd.exe", 200.0):
        failures.append(f"live pid row wrong: {snap.get(50)!r}")
    snap.get(50)
    if shell.meta_calls != 1:
        failures.append(f"row should be fetched once, got {shell.meta_calls} fetches")

    if snap.get(99999, "absent") != "absent":
        failures.append("dead pid should return default")
    if snap.get(60) != (None, "cmd.exe", 0.0):
        failures.append(
            f"AccessDenied pid should be present-but-unwalkable: {snap.get(60)!r}"
        )
    if snap.get(None, "absent") != "absent":
        failures.append("None pid should return default")
    if snap.get(70) != (40, "pwsh.exe", 220.0):
        failures.append(f"name fallback via proc.name() failed: {snap.get(70)!r}")


def check_access_denied_parent_still_counts(failures):
    # A session whose parent shell exists but is unreadable (AccessDenied on
    # ppid/create_time) must still count: the orphan rule fires only when the
    # parent is GONE, not when it is merely opaque.
    import statusline_lib.sessions as sessions_mod

    target_cwd = os.path.normcase("/home/user/proj")
    procs = [
        _FakeSnapProc(50, 40, "cmd.exe", 200.0, meta_raises=_FakePsutilError()),
        _FakeSnapProc(100, 50, "claude", 300.0, ["claude"], target_cwd),
    ]
    count = sessions_mod._count_via_psutil(target_cwd, _make_fake_psutil(procs))
    if count != 1:
        failures.append(
            f"session with unreadable (but live) parent should count; got {count}"
        )


def check_count_excludes_agent_descendants(failures):
    # The regression this change exists for: a claude.exe child spawned BY a
    # session (update check, helper, agent runtime) must not count as a second
    # session, while true top-level sessions (shell ancestors) still count --
    # whether the helper's parent chain is intact or already dead (disowned).
    import statusline_lib.sessions as sessions_mod

    target_cwd = os.path.normcase("/home/user/proj")
    procs = [
        _FakeSnapProc(40, 1, "explorer.exe", 100.0),
        _FakeSnapProc(50, 40, "cmd.exe", 200.0),
        _FakeSnapProc(100, 50, "claude", 300.0, ["claude"], target_cwd),
        # Helper with intact chain: child of the session itself.
        _FakeSnapProc(
            110, 100, "claude.exe", 400.0, ["claude.exe", "update"], target_cwd
        ),
        # Disowned helper: parent already dead.
        _FakeSnapProc(
            120, 99999, "claude.exe", 500.0, ["claude.exe", "mcp", "list"], target_cwd
        ),
        # Child of an npm-installed (node-wrapped) claude session: the tree
        # walk must fetch the node ancestor's cmdline lazily to classify it.
        _FakeSnapProc(
            70, 50, "node.exe", 250.0, ["node", "/x/claude-code/cli.js"], "/elsewhere"
        ),
        _FakeSnapProc(130, 70, "claude.exe", 600.0, ["claude.exe"], target_cwd),
        # Same shape but the node ancestor's cmdline is unreadable: treated as
        # non-agent, so this one still counts (old-overcount fallback).
        _FakeSnapProc(
            80, 50, "node.exe", 250.0, _FakePsutilError("no access"), "/elsewhere"
        ),
        _FakeSnapProc(140, 80, "claude.exe", 700.0, ["claude.exe"], target_cwd),
    ]
    count = sessions_mod._count_via_psutil(target_cwd, _make_fake_psutil(procs))
    if count != 2:
        failures.append(
            f"session + unreadable-node-ancestor child should count; expected 2, got {count}"
        )


def check_count_via_psutil_access_denied(failures):
    # NoSuchProcess and AccessDenied from p.cmdline()/p.cwd() are caught and
    # the process is skipped, while a readable matching session still counts.
    import statusline_lib.sessions as sessions_mod

    target_cwd = os.path.normcase("/home/user/proj")
    denied = _FakePsutilError("no access")
    procs = [
        _FakeSnapProc(40, 1, "explorer.exe", 100.0),
        _FakeSnapProc(50, 40, "cmd.exe", 200.0),
        _FakeSnapProc(90, 50, "claude", 250.0, denied, denied),
        _FakeSnapProc(100, 50, "claude", 300.0, ["claude"], target_cwd),
    ]
    count = sessions_mod._count_via_psutil(target_cwd, _make_fake_psutil(procs))
    if count != 1:
        failures.append(
            f"_count_via_psutil should skip AccessDenied and count 1 matching process; got {count}"
        )


def main():
    failures = []
    check_count_via_psutil_access_denied(failures)
    check_is_agent_runtime(failures)
    check_excluded_by_tree(failures)
    check_count_excludes_agent_descendants(failures)
    check_process_iter_uses_cheap_attrs(failures)
    check_lazy_snapshot(failures)
    check_access_denied_parent_still_counts(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print("OK: process-tree session classification behaves correctly")


if __name__ == "__main__":
    main()
