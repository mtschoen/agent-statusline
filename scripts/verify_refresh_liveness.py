"""Verify statusline_lib/refresh.py PID liveness tracking, claim lifecycle,
concurrency limits, and watchdog timeout deadlines.

Covers:
  - Regression test for issue #54: a slow refresher running longer than
    _INFLIGHT_TTL_SECONDS must NOT spawn duplicate children while its PID is
    alive. When the process dies, the claim is pruned and respawns.
  - _pid_is_alive branches across psutil availability, os.kill success,
    ProcessLookupError, PermissionError, and general OSError.
  - _is_claim_live across float/int timestamps, dict entries with/without PID,
    dead PIDs, alive PIDs, and unparseable entries.
  - _record_inflight_pid across dict and numeric legacy entries.
  - _MAX_CONCURRENT_REFRESH cap: rejecting spawns once the cap is reached.
  - run_refresh self-deadline watchdog and _timeout_abort.

Run from anywhere; imports from `agent-statusline` by path.
"""

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.refresh as refresh

_WIN_START = 1_748_000_000.0
_NOW = _WIN_START + 7200.0


class _MockChildProcess:
    def __init__(self, pid):
        self.pid = pid


def _pin_refresh(tmp, now):
    marker_path = os.path.join(tmp, "inflight.json")
    saved = (refresh._INFLIGHT_PATH, refresh._now_unix, refresh.spawn_detached)
    refresh._INFLIGHT_PATH = marker_path
    refresh._now_unix = lambda: now
    return saved


def _restore_refresh(saved):
    (refresh._INFLIGHT_PATH, refresh._now_unix, refresh.spawn_detached) = saved


def _check_slow_refresher_pid_liveness(failures):
    """Regression test for issue #54: a slow refresher running longer than
    _INFLIGHT_TTL_SECONDS must NOT allow duplicate spawns while its PID is
    alive. When the process dies, the claim is pruned and respawns."""
    spawned = []
    fake_pid = 98765
    alive_pids = {fake_pid}

    with tempfile.TemporaryDirectory() as tmp:
        saved = _pin_refresh(tmp, _NOW)
        saved_pid_alive = getattr(refresh, "_pid_is_alive", None)
        refresh._pid_is_alive = lambda pid: pid in alive_pids
        refresh.spawn_detached = lambda command: (
            spawned.append(command) or _MockChildProcess(fake_pid)
        )
        try:
            first = refresh.maybe_spawn_refresh("session-count", "/my/cwd")
            # Time advances past the 120s TTL window, but the child process is still alive.
            refresh._now_unix = lambda: _NOW + refresh._INFLIGHT_TTL_SECONDS + 50
            second = refresh.maybe_spawn_refresh("session-count", "/my/cwd")
            third = refresh.maybe_spawn_refresh("session-count", "/my/cwd")
            # Child process terminates / exits
            alive_pids.remove(fake_pid)
            fourth = refresh.maybe_spawn_refresh("session-count", "/my/cwd")
        finally:
            _restore_refresh(saved)
            if saved_pid_alive is not None:
                refresh._pid_is_alive = saved_pid_alive
            elif hasattr(refresh, "_pid_is_alive"):
                delattr(refresh, "_pid_is_alive")

    if (first, second, third, fourth) != (True, False, False, True):
        failures.append(
            f"slow refresher debounce: expected (True, False, False, True), got "
            f"{(first, second, third, fourth)!r}"
        )
    if len(spawned) != 2:
        failures.append(f"slow refresher spawned {len(spawned)} children instead of 2")


def _check_pid_is_alive_branches(failures):
    """Exercise _pid_is_alive across valid and invalid PIDs, psutil and
    os.kill fallbacks."""
    # Invalid PIDs
    for invalid in (None, 0, -1, "123", 12.5):
        if refresh._pid_is_alive(invalid) is not None:
            failures.append(f"_pid_is_alive({invalid!r}) must return None")

    # psutil branch: exists and does not exist
    fake_psutil = types.SimpleNamespace(
        pid_exists=lambda pid: pid == 111,
    )
    saved_resolve = refresh._resolve_psutil
    try:
        refresh._resolve_psutil = lambda: fake_psutil
        if refresh._pid_is_alive(111) is not True:
            failures.append("_pid_is_alive with psutil True failed")
        if refresh._pid_is_alive(222) is not False:
            failures.append("_pid_is_alive with psutil False failed")

        def raising_pid_exists(pid):
            raise OSError("psutil error")

        fake_psutil.pid_exists = raising_pid_exists
        # Raising psutil falls back to os.kill
        saved_kill = os.kill
        try:
            os.kill = lambda pid, sig: None
            if refresh._pid_is_alive(111) is not True:
                failures.append("_pid_is_alive fallback on psutil error failed")
        finally:
            os.kill = saved_kill
    finally:
        refresh._resolve_psutil = saved_resolve

    # psutil is None -> os.kill fallbacks
    try:
        refresh._resolve_psutil = lambda: None
        saved_kill = os.kill
        try:
            # os.kill succeeds
            os.kill = lambda pid, sig: None
            if refresh._pid_is_alive(123) is not True:
                failures.append("_pid_is_alive with os.kill success failed")

            # ProcessLookupError
            def lookup_error(pid, sig):
                raise ProcessLookupError()

            os.kill = lookup_error
            if refresh._pid_is_alive(123) is not False:
                failures.append(
                    "_pid_is_alive with ProcessLookupError must return False"
                )

            # PermissionError
            def perm_error(pid, sig):
                raise PermissionError()

            os.kill = perm_error
            if refresh._pid_is_alive(123) is not True:
                failures.append("_pid_is_alive with PermissionError must return True")

            # Other OSError
            def other_os_error(pid, sig):
                raise OSError("mystery error")

            os.kill = other_os_error
            if refresh._pid_is_alive(123) is not None:
                failures.append("_pid_is_alive with general OSError must return None")
        finally:
            os.kill = saved_kill
    finally:
        refresh._resolve_psutil = saved_resolve


def _check_is_claim_live_branches(failures):
    """Exercise _is_claim_live across entry representations."""
    now = 1000.0

    # Numeric entry
    if not refresh._is_claim_live(950.0, now):
        failures.append("recent numeric entry should be live")
    if refresh._is_claim_live(800.0, now):
        failures.append("old numeric entry should be expired")

    # Dict entry without PID
    if not refresh._is_claim_live({"ts": 950.0}, now):
        failures.append("recent dict entry without PID should be live")
    if refresh._is_claim_live({"ts": 800.0}, now):
        failures.append("old dict entry without PID should be expired")

    # Invalid entries
    for invalid in (None, "bad", [], {"ts": "not-a-number"}):
        if refresh._is_claim_live(invalid, now):
            failures.append(f"invalid entry {invalid!r} must not be live")

    # Dict entry with PID
    saved_alive = refresh._pid_is_alive
    try:
        # Alive PID keeps claim live past TTL
        refresh._pid_is_alive = lambda pid: True
        if not refresh._is_claim_live({"ts": 100.0, "pid": 42}, now):
            failures.append("entry with alive PID must be live even past TTL")

        # Dead PID expires claim even within TTL
        refresh._pid_is_alive = lambda pid: False
        if refresh._is_claim_live({"ts": 999.0, "pid": 42}, now):
            failures.append("entry with dead PID must not be live")

        # Undetermined PID falls back to TTL
        refresh._pid_is_alive = lambda pid: None
        if not refresh._is_claim_live({"ts": 950.0, "pid": 42}, now):
            failures.append("entry with undetermined PID within TTL must be live")
        if refresh._is_claim_live({"ts": 800.0, "pid": 42}, now):
            failures.append("entry with undetermined PID past TTL must be expired")
    finally:
        refresh._pid_is_alive = saved_alive


def _check_record_inflight_pid(failures):
    """_record_inflight_pid updates dict entries, promotes legacy numeric
    entries, and safely ignores missing keys."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _pin_refresh(tmp, _NOW)
        try:
            # Missing key -> no-op
            refresh._record_inflight_pid("pace-hourly", _WIN_START, 1234)
            if refresh._read_inflight() != {}:
                failures.append(
                    "_record_inflight_pid on missing key must not create an entry"
                )

            # Existing dict entry -> updates pid
            refresh._claim_inflight("pace-hourly", _WIN_START)
            refresh._record_inflight_pid("pace-hourly", _WIN_START, 5678)
            marks = refresh._read_inflight()
            key = refresh._inflight_key("pace-hourly", _WIN_START)
            entry = marks.get(key)
            pid_val = entry.get("pid") if isinstance(entry, dict) else None
            if pid_val != 5678:
                failures.append(f"dict pid not updated: {marks!r}")

            # Legacy numeric entry -> promoted to dict with pid
            marks[key] = 12345.0
            refresh._write_inflight(marks)
            refresh._record_inflight_pid("pace-hourly", _WIN_START, 9999)
            marks = refresh._read_inflight()
            entry = marks.get(key)
            if (
                not isinstance(entry, dict)
                or entry.get("pid") != 9999
                or entry.get("ts") != 12345.0
            ):
                failures.append(f"numeric entry not promoted to dict: {marks!r}")
        finally:
            _restore_refresh(saved)


def _check_max_concurrent_refresh_cap(failures):
    """_claim_inflight respects _MAX_CONCURRENT_REFRESH."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _pin_refresh(tmp, _NOW)
        try:
            for i in range(refresh._MAX_CONCURRENT_REFRESH):
                claimed = refresh._claim_inflight(f"kind-{i}", _WIN_START)
                if not claimed:
                    failures.append(f"failed to claim slot {i}")
            # Next claim exceeds cap -> False
            overflow = refresh._claim_inflight("overflow-kind", _WIN_START)
            if overflow:
                failures.append(
                    "claim beyond _MAX_CONCURRENT_REFRESH should return False"
                )
        finally:
            _restore_refresh(saved)


def _check_watchdog_timeout_abort(failures):
    """_timeout_abort clears the inflight marker and exits with code 1."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _pin_refresh(tmp, _NOW)
        exited = []
        saved_exit = os._exit
        try:
            os._exit = lambda code: exited.append(code)
            refresh._claim_inflight("pace-hourly", _WIN_START)
            refresh._timeout_abort("pace-hourly", _WIN_START)
            if refresh._read_inflight() != {}:
                failures.append("_timeout_abort did not clear inflight marker")
            if exited != [1]:
                failures.append(
                    f"_timeout_abort exit code: expected [1], got {exited!r}"
                )
        finally:
            os._exit = saved_exit
            _restore_refresh(saved)


def _check_run_refresh_deadline_disabled(failures):
    """run_refresh with deadline=0 or None runs without error."""
    with tempfile.TemporaryDirectory() as tmp:
        saved = _pin_refresh(tmp, _NOW)
        try:
            refresh.run_refresh("fable-quota", 0, deadline=0)
            refresh.run_refresh("fable-quota", 0, deadline=None)
        finally:
            _restore_refresh(saved)


def _check_resolve_psutil_fallback(failures):
    """_resolve_psutil returns module or None on ImportError."""
    res = refresh._resolve_psutil()
    if res is not None and not hasattr(res, "pid_exists"):
        failures.append(f"unexpected _resolve_psutil result: {res!r}")

    real_psutil = sys.modules.get("psutil")
    try:
        sys.modules["psutil"] = None  # type: ignore[assignment]
        none_res = refresh._resolve_psutil()
        if none_res is not None:
            failures.append(
                f"_resolve_psutil with ImportError must return None; got {none_res!r}"
            )
    finally:
        if real_psutil is not None:
            sys.modules["psutil"] = real_psutil
        else:
            sys.modules.pop("psutil", None)


def main():
    failures = []
    _check_slow_refresher_pid_liveness(failures)
    _check_pid_is_alive_branches(failures)
    _check_is_claim_live_branches(failures)
    _check_record_inflight_pid(failures)
    _check_max_concurrent_refresh_cap(failures)
    _check_watchdog_timeout_abort(failures)
    _check_run_refresh_deadline_disabled(failures)
    _check_resolve_psutil_fallback(failures)

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
