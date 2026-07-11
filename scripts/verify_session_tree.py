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
    """Fake psutil process for the snapshot-based _count_via_psutil."""

    def __init__(self, pid, ppid, name, create_time, cmdline=None, cwd=None):
        self.info = {"pid": pid, "ppid": ppid, "name": name, "create_time": create_time}
        self._cmdline = cmdline
        self._cwd = cwd

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


def _make_fake_psutil(procs):
    by_pid = {p.info["pid"]: p for p in procs}

    class FakePsutil:
        NoSuchProcess = _FakePsutilNoSuchProcess
        AccessDenied = _FakePsutilError

        @staticmethod
        def process_iter(attrs):
            yield from procs

        @staticmethod
        def Process(pid):
            proc = by_pid.get(pid)
            if proc is None:
                raise _FakePsutilNoSuchProcess(pid)
            return proc

    return FakePsutil


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

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print("OK: process-tree session classification behaves correctly")


if __name__ == "__main__":
    main()
