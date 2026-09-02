"""Verify statusline_lib/refresh.py PID liveness tracking, claim lifecycle,
concurrency limits, watchdog timeout deadlines, and PID reuse detection.
"""

import contextlib
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.refresh as refresh

_WIN_START = 1_748_000_000.0
_NOW = _WIN_START + 7200.0


@contextlib.contextmanager
def _pinned_refresh(tmp, now):
    saved = (refresh._INFLIGHT_PATH, refresh._now_unix, refresh.spawn_detached)
    refresh._INFLIGHT_PATH = os.path.join(tmp, "inflight.json")
    refresh._now_unix = lambda: now
    try:
        yield
    finally:
        refresh._INFLIGHT_PATH, refresh._now_unix, refresh.spawn_detached = saved


def _check_slow_refresher_pid_liveness(failures):
    """Issue #54: slow refresher running longer than TTL debounces while PID is alive."""
    spawned, fake_pid, alive_pids = [], 98765, {98765}
    with tempfile.TemporaryDirectory() as tmp, _pinned_refresh(tmp, _NOW):
        saved_pid_alive = getattr(refresh, "_pid_is_alive", None)
        refresh._pid_is_alive = lambda pid, **kw: pid in alive_pids
        refresh.spawn_detached = lambda cmd: (
            spawned.append(cmd) or types.SimpleNamespace(pid=fake_pid)
        )
        try:
            r1 = refresh.maybe_spawn_refresh("session-count", "/cwd")
            refresh._now_unix = lambda: _NOW + refresh._INFLIGHT_TTL_SECONDS + 50
            r2 = refresh.maybe_spawn_refresh("session-count", "/cwd")
            r3 = refresh.maybe_spawn_refresh("session-count", "/cwd")
            alive_pids.remove(fake_pid)
            r4 = refresh.maybe_spawn_refresh("session-count", "/cwd")
        finally:
            refresh._pid_is_alive = saved_pid_alive

    if (r1, r2, r3, r4) != (True, False, False, True) or len(spawned) != 2:
        failures.append(
            f"slow refresher debounce failed: {(r1, r2, r3, r4)!r}, {len(spawned)}"
        )


def _check_reused_pid_create_time_mismatch(failures):
    """Issue #56: mismatched create_time spawns (reused PID); matching debounces."""
    reused_pid, created_time = 54321, 100.0
    current_ctime = {"ctime": 200.0}
    fake_psutil = types.SimpleNamespace(
        pid_exists=lambda pid: pid == reused_pid,
        Process=lambda pid: types.SimpleNamespace(
            create_time=lambda: current_ctime["ctime"]
        ),
        Error=Exception,
    )
    with tempfile.TemporaryDirectory() as tmp, _pinned_refresh(tmp, _NOW):
        saved_resolve = refresh._resolve_psutil
        refresh._resolve_psutil = lambda: fake_psutil
        refresh.spawn_detached = lambda cmd: types.SimpleNamespace(pid=99999)
        try:
            k1 = refresh._inflight_key("session-count", "/cwd")
            refresh._write_inflight(
                {
                    k1: {
                        "ts": _NOW - 30.0,
                        "pid": reused_pid,
                        "create_time": created_time,
                    }
                }
            )
            first = refresh.maybe_spawn_refresh("session-count", "/cwd")

            current_ctime["ctime"] = created_time
            k2 = refresh._inflight_key("git-ref", "/cwd")
            refresh._write_inflight(
                {
                    k2: {
                        "ts": _NOW - 30.0,
                        "pid": reused_pid,
                        "create_time": created_time,
                    }
                }
            )
            second = refresh.maybe_spawn_refresh("git-ref", "/cwd")
        finally:
            refresh._resolve_psutil = saved_resolve

    if first is not True or second is not False:
        failures.append(f"reused pid check failed: first={first!r}, second={second!r}")


def _check_hard_ceiling_pruning(failures):
    """Issue #56: claim older than _INFLIGHT_HARD_CEILING_SECONDS is pruned even if PID is alive."""
    fake_pid, ctime = 77777, 100.0
    fake_psutil = types.SimpleNamespace(
        pid_exists=lambda pid: pid == fake_pid,
        Process=lambda pid: types.SimpleNamespace(create_time=lambda: ctime),
        Error=Exception,
    )
    with tempfile.TemporaryDirectory() as tmp, _pinned_refresh(tmp, _NOW):
        saved_resolve = refresh._resolve_psutil
        refresh._resolve_psutil = lambda: fake_psutil
        refresh.spawn_detached = lambda cmd: types.SimpleNamespace(pid=88888)
        try:
            hard_ceiling = refresh._INFLIGHT_HARD_CEILING_SECONDS
            k = refresh._inflight_key("session-count", "/cwd")
            refresh._write_inflight(
                {
                    k: {
                        "ts": _NOW - hard_ceiling - 10.0,
                        "pid": fake_pid,
                        "create_time": ctime,
                    }
                }
            )
            res = refresh.maybe_spawn_refresh("session-count", "/cwd")
        finally:
            refresh._resolve_psutil = saved_resolve

    if res is not True:
        failures.append("claim past hard ceiling must be pruned")


def _check_pid_is_alive_branches(failures):
    """Exercise _pid_is_alive across valid/invalid PIDs, psutil, and os.kill."""
    for invalid in (None, 0, -1, "123", 12.5):
        if refresh._pid_is_alive(invalid) is not None:
            failures.append(f"_pid_is_alive({invalid!r}) must return None")

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            if self.pid == 333:
                raise OSError("error")
            return 100.0

    fake_psutil = types.SimpleNamespace(
        pid_exists=lambda pid: pid in (111, 333),
        Process=_Proc,
        Error=Exception,
    )
    saved_resolve = refresh._resolve_psutil
    try:
        refresh._resolve_psutil = lambda: fake_psutil
        for pid, ctime, expected, desc in (
            (111, None, True, "psutil True"),
            (111, 100.0, True, "matching create_time"),
            (111, 200.0, False, "mismatched create_time"),
            (222, None, False, "psutil False"),
        ):
            if refresh._pid_is_alive(pid, create_time=ctime) is not expected:
                failures.append(f"_pid_is_alive {desc} failed")

        saved_kill = os.kill
        try:
            os.kill = lambda pid, sig: None
            if refresh._pid_is_alive(333, create_time=100.0) is not True:
                failures.append("_pid_is_alive fallback on psutil error failed")
        finally:
            os.kill = saved_kill
    finally:
        refresh._resolve_psutil = saved_resolve

    try:
        refresh._resolve_psutil = lambda: None
        saved_kill = os.kill
        try:
            os.kill = lambda pid, sig: None
            if (
                refresh._pid_is_alive(123) is not True
                or refresh._pid_is_alive(123, create_time=100.0) is not True
            ):
                failures.append("_pid_is_alive without psutil failed")

            for exc, expected, name in (
                (ProcessLookupError(), False, "ProcessLookupError"),
                (PermissionError(), True, "PermissionError"),
                (OSError("error"), None, "OSError"),
            ):

                def _raise(pid, sig, e=exc):
                    raise e

                os.kill = _raise
                if refresh._pid_is_alive(123) is not expected:
                    failures.append(f"_pid_is_alive with {name} failed")
        finally:
            os.kill = saved_kill
    finally:
        refresh._resolve_psutil = saved_resolve


def _check_child_create_time_branches(failures):
    """Exercise _child_create_time across valid/invalid inputs and error paths."""
    for invalid in (None, 0, -1, "123", 12.5):
        if refresh._child_create_time(invalid) is not None:
            failures.append(f"_child_create_time({invalid!r}) must return None")

    saved_resolve = refresh._resolve_psutil
    try:
        refresh._resolve_psutil = lambda: None
        if refresh._child_create_time(123) is not None:
            failures.append("_child_create_time with psutil None must return None")

        class _Proc:
            def __init__(self, pid):
                self.pid = pid

            def create_time(self):
                if self.pid == 999:
                    raise OSError("fail")
                return 12345.67

        refresh._resolve_psutil = lambda: types.SimpleNamespace(
            Process=_Proc, Error=Exception
        )
        ctime = refresh._child_create_time(123)
        if ctime != 12345.67 or refresh._child_create_time(999) is not None:
            failures.append(f"unexpected _child_create_time result: {ctime!r}")
    finally:
        refresh._resolve_psutil = saved_resolve


def _check_is_claim_live_branches(failures):
    """Exercise _is_claim_live across entry representations and TTL/ceiling states."""
    now = 1000.0
    h_ceil = refresh._INFLIGHT_HARD_CEILING_SECONDS

    cases = [
        (950.0, True, "recent numeric"),
        (800.0, False, "old numeric"),
        (now - h_ceil - 5, False, "hard-ceiling numeric"),
        ({"ts": 950.0}, True, "recent dict without PID"),
        ({"ts": 800.0}, False, "old dict without PID"),
        ({"ts": now - h_ceil - 5}, False, "hard-ceiling dict without PID"),
        ({"ts": 950.0, "create_time": "bad"}, True, "bad create_time fallback"),
        *(
            (inv, False, f"invalid {inv!r}")
            for inv in (None, "bad", [], {"ts": "not-a-number"})
        ),
    ]
    for entry, expected, desc in cases:
        if refresh._is_claim_live(entry, now) is not expected:
            failures.append(f"_is_claim_live: {desc} expected {expected}")

    saved_alive = refresh._pid_is_alive
    try:
        for alive_val, entry, expected, desc in (
            (True, {"ts": 100.0, "pid": 42}, True, "alive PID past TTL"),
            (
                True,
                {"ts": now - h_ceil - 10, "pid": 42},
                False,
                "alive PID past hard ceiling",
            ),
            (False, {"ts": 999.0, "pid": 42}, False, "dead PID"),
            (None, {"ts": 950.0, "pid": 42}, True, "undetermined PID within TTL"),
            (None, {"ts": 800.0, "pid": 42}, False, "undetermined PID past TTL"),
        ):
            refresh._pid_is_alive = lambda pid, val=alive_val, **kw: val
            if refresh._is_claim_live(entry, now) is not expected:
                failures.append(f"_is_claim_live with {desc} failed")
    finally:
        refresh._pid_is_alive = saved_alive


def _check_record_inflight_pid(failures):
    """_record_inflight_pid updates dict entries, promotes numeric entries, and safely handles create_time."""
    with tempfile.TemporaryDirectory() as tmp, _pinned_refresh(tmp, _NOW):
        refresh._record_inflight_pid("pace-hourly", _WIN_START, 1234)
        if refresh._read_inflight() != {}:
            failures.append("_record_inflight_pid on missing key must be no-op")

        refresh._claim_inflight("pace-hourly", _WIN_START)
        k = refresh._inflight_key("pace-hourly", _WIN_START)
        for pid, ctime, expected_ct, name in (
            (5678, 123.45, 123.45, "update pid/ctime"),
            (5679, None, None, "clear ctime on None"),
        ):
            refresh._record_inflight_pid(
                "pace-hourly", _WIN_START, pid, create_time=ctime
            )
            e = refresh._read_inflight().get(k, {})
            if e.get("pid") != pid or e.get("create_time") != expected_ct:
                failures.append(f"_record_inflight_pid {name} failed: {e!r}")

        for ctime, expected_ct, name in (
            (987.65, 987.65, "promote numeric with ctime"),
            (None, None, "promote numeric without ctime"),
        ):
            refresh._write_inflight({k: 12345.0})
            refresh._record_inflight_pid(
                "pace-hourly", _WIN_START, 9999, create_time=ctime
            )
            e = refresh._read_inflight().get(k, {})
            if (
                e.get("pid") != 9999
                or e.get("ts") != 12345.0
                or e.get("create_time") != expected_ct
            ):
                failures.append(f"_record_inflight_pid {name} failed: {e!r}")


def _check_max_concurrent_refresh_cap(failures):
    """_claim_inflight respects _MAX_CONCURRENT_REFRESH."""
    with tempfile.TemporaryDirectory() as tmp, _pinned_refresh(tmp, _NOW):
        for i in range(refresh._MAX_CONCURRENT_REFRESH):
            if not refresh._claim_inflight(f"kind-{i}", _WIN_START):
                failures.append(f"failed to claim slot {i}")
        if refresh._claim_inflight("overflow-kind", _WIN_START):
            failures.append("claim beyond cap should return False")


def _check_watchdog_and_deadline(failures):
    """_timeout_abort clears marker and exits; run_refresh with disabled deadline runs clean."""
    with tempfile.TemporaryDirectory() as tmp, _pinned_refresh(tmp, _NOW):
        exited = []
        saved_exit = os._exit
        try:
            os._exit = lambda code: exited.append(code)
            refresh._claim_inflight("pace-hourly", _WIN_START)
            refresh._timeout_abort("pace-hourly", _WIN_START)
            if refresh._read_inflight() != {} or exited != [1]:
                failures.append(f"_timeout_abort failed: exited={exited!r}")
            refresh.run_refresh("fable-quota", 0, deadline=0)
            refresh.run_refresh("fable-quota", 0, deadline=None)
        finally:
            os._exit = saved_exit


def _check_resolve_psutil_fallback(failures):
    """_resolve_psutil returns module or None on ImportError."""
    res = refresh._resolve_psutil()
    if res is not None and not hasattr(res, "pid_exists"):
        failures.append(f"unexpected _resolve_psutil result: {res!r}")

    real_psutil = sys.modules.get("psutil")
    try:
        sys.modules["psutil"] = None  # type: ignore[assignment]
        if refresh._resolve_psutil() is not None:
            failures.append("_resolve_psutil with ImportError must return None")
    finally:
        if real_psutil is not None:
            sys.modules["psutil"] = real_psutil
        else:
            sys.modules.pop("psutil", None)


def main():
    failures = []
    for check in (
        _check_slow_refresher_pid_liveness,
        _check_reused_pid_create_time_mismatch,
        _check_hard_ceiling_pruning,
        _check_pid_is_alive_branches,
        _check_child_create_time_branches,
        _check_is_claim_live_branches,
        _check_record_inflight_pid,
        _check_max_concurrent_refresh_cap,
        _check_watchdog_and_deadline,
        _check_resolve_psutil_fallback,
    ):
        check(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: refresh liveness tracking, claim lifecycle, concurrency cap,"
        " and watchdog deadline all verified"
    )


if __name__ == "__main__":
    main()
