# Resident Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every statusline render a fixed-cost process that does no I/O beyond one datagram exchange, moving all computation into one bounded resident server process per harness configuration directory.

**Architecture:** Two processes per `app_dir()`. A thin standard-library client (`statusline_client.py`) reads the harness payload from stdin, sends one UDP datagram to `127.0.0.1:<port>` taken from `state_dir()/server.json`, waits at most 150 ms for one reply, and prints it verbatim. On timeout, dead port, missing server file, or code-version mismatch it prints a fallback line and single-flight spawns a replacement server without waiting. The server (`statusline_lib/server.py`, entry point `statusline_server.py`) binds a random localhost UDP port, serves renders inline on its receive loop from in-memory per-session, per-cwd and machine-wide state tables, and runs everything that can block (git, psutil, HTTP, walker) on a worker pool bounded at four threads. Per-session transcript state is incremental: only bytes appended since the last recorded offset are read. The detached-child refresh spawner is deleted; the eight refreshers become in-process scheduled jobs.

**Tech Stack:** Python 3.11 standard library only. `socket` (UDP, `SO_EXCLUSIVEADDRUSE` on Windows), `threading` plus `queue` for the worker pool, `json` for the wire format. Existing repo primitives are reused unchanged: `statusline_lib/prefs.py` for settings, `statusline_lib/ttlcache.py` for disk caches, `statusline_lib/process_safe.py` as the only sanctioned subprocess surface, `scripts/verify_*.py` as the test convention.

## Global Constraints

- Standard library only. No new dependencies. Python floor 3.11.
- `scripts/verify_*.py` is the only test convention. Every script is standalone, collects into a `failures` list, prints `FAIL: <message>` lines, and exits non-zero.
- 100 percent line coverage of `statusline_lib/` on Linux **and** Windows. No pragmas, no exclusions. Platform branches are covered on both operating systems by patching `os.name` to force the foreign arm.
- Entry-point glue at the repository root (`statusline.py`, `subagent_statusline.py`, `qwen_statusline.py`, `kimi_statusline.py`, `statusline_client.py`, `statusline_server.py`, `install.py`, `wrap_nudge.py`) is outside the measured coverage scope. Keep logic in `statusline_lib`, keep glue thin.
- `ruff format --check .` and `ruff check .` are hard gates. The committed hook at `hooks/pre-commit` runs both.
- The aislop gate scores 90 against a floor of 90. Headroom is exhausted. No new file may exceed 400 lines, and no existing file may be grown past 400 lines.
- No test asserts on wall-clock time. Clocks are injected or faked. The one exception is `scripts/verify_render_budget.py`, which is an explicitly marked benchmark with generous tolerances.
- No em-dashes anywhere: prose, code, comments, commit messages. ASCII only.
- Full words in identifiers. No abbreviations (`maximum` not `max`, `arguments` not `args`, `configuration` not `config`).
- No hard-coded machine-specific absolute paths. Derive from `app_dir()`, `state_dir()`, or arguments.
- Constants live as named module-level constants and are overridable through the prefs file for tests: idle exit 10 minutes, per-session and per-cwd drop 1 hour, client timeout 150 ms, fallback freshness 30 seconds, spawn lock staleness 10 seconds, worker pool 4, housekeeping age 7 days.

## File Structure

**Created:**

| Path | Responsibility | Target size |
|---|---|---|
| `statusline_lib/server_jobs.py` | Refresh dispatch table, request sink, bounded worker pool | ~190 lines |
| `statusline_lib/server_state.py` | In-memory per-session / per-cwd tables, incremental transcript walk, idle drops, housekeeping | ~250 lines |
| `statusline_lib/server_info.py` | Code-version hash, `server.json` read/write/remove, pid liveness | ~110 lines |
| `statusline_lib/server.py` | UDP socket lifecycle, receive loop, request dispatch, idle exit | ~300 lines |
| `statusline_lib/render_line2.py` | Line 2 inputs tuple and formatter, extracted from `statusline.py` | ~190 lines |
| `statusline_lib/render_claude.py` | Line 1 and line 3 plus `render_claude_statusline`, extracted from `statusline.py` | ~300 lines |
| `statusline_lib/render_subagent.py` | Subagent row rendering, extracted from `subagent_statusline.py` | ~230 lines |
| `statusline_lib/server_install.py` | SessionStart hook merge helpers, mirroring `nudge_install.py` | ~90 lines |
| `statusline_client.py` | The hot-path client entry point | ~220 lines |
| `statusline_server.py` | Server entry point shim | ~30 lines |
| `scripts/verify_server_jobs.py` | Dispatch table, sink, worker pool | new |
| `scripts/verify_server_state.py` | Incremental walk, rewalk, drops, housekeeping | new |
| `scripts/verify_server_info.py` | Version hash, info file, pid liveness | new |
| `scripts/verify_server_requests.py` | `handle_request` for every kind, in-process, no sockets | new |
| `scripts/verify_server_protocol.py` | Real server on a real port plus the real client as a subprocess | new |
| `scripts/verify_server_concurrency.py` | The 2026-09-02 incident regression | new |
| `scripts/verify_render_line2.py` | In-process coverage of the extracted line-2 formatter | new |
| `scripts/verify_render_claude.py` | In-process coverage of the extracted Claude render | new |
| `scripts/verify_render_subagent.py` | In-process coverage of the extracted subagent render | new |
| `scripts/verify_server_install.py` | SessionStart hook merge | new |

**Deleted:** `statusline_lib/refresh.py`, `scripts/verify_refresh_spawner.py`, `scripts/verify_refresh_liveness.py`, `prewarm.sh`.

**Modified:** `statusline_lib/cost.py` (accumulator seam), `statusline_lib/rendertimer.py` (drop spawn timings), the eight cache readers (`gitref.py`, `beacon.py`, `beacon_cache.py`, `sessions.py`, `pace.py`, `burnrate.py`, `qwen_quota.py`, `fable_quota.py`), `statusline.py`, `subagent_statusline.py`, `kimi_statusline.py`, `qwen_statusline.py`, `install.py`, `statusline_lib/claude_family_install.py`, `statusline_ctl.py`, `scripts/verify_render_budget.py`, `.gitea/workflows/ci.yml`, `AGENTS.md`, `README.md`, `TEST-REPORT.md`.

---

## Phase 1: In-process refresh jobs replace the detached spawner

Today every stale cache read calls `statusline_lib/refresh.py`'s `maybe_spawn_refresh(kind, argument)`, which claims an inflight marker file and starts a detached `python -c` child. That mechanism is what multiplied to roughly 1,000 processes on 2026-09-02. This phase replaces it with an in-process request sink and a bounded worker pool, so the eight cache readers keep their stale-while-revalidate contract but the recomputation happens on the server's own threads.

Outside the server (a bare `run_refresh` invocation, or any verify script that has not installed a sink) `request_refresh` is a no-op returning `False`. That is deliberate: nothing outside the server is allowed to start background work any more.

### Task 1: Refresh dispatch table and request sink

**Files:**
- Create: `statusline_lib/server_jobs.py`
- Create: `scripts/verify_server_jobs.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `REFRESHER_MODULES: dict[str, tuple[str, str]]` mapping refresh kind to (module name within the package, refresher attribute name).
  - `run_refresh(kind: str, argument) -> None`, raises `ValueError` on an unknown kind.
  - `set_refresh_sink(sink) -> object` where `sink` is `Callable[[str, object], bool]` or `None`; returns the previous sink.
  - `request_refresh(kind: str, argument) -> bool`, `False` when no sink is installed.

- [ ] **Step 1: Write the failing test**

Create `scripts/verify_server_jobs.py`:

```python
"""Verify statusline_lib/server_jobs.py: the refresh dispatch table, the
pluggable request sink that replaced the detached-child spawner, and the
bounded worker pool the resident server runs refreshers on.

Run from anywhere; imports from agent-statusline by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib import server_jobs


def check_dispatch_table_covers_every_refresher(failures):
    expected = {
        "pace-hourly",
        "window-spend",
        "git-ref",
        "beacon-latest",
        "session-count",
        "bias-factor",
        "qwen-quota",
        "fable-quota",
    }
    if set(server_jobs.REFRESHER_MODULES) != expected:
        failures.append(
            f"dispatch table is {sorted(server_jobs.REFRESHER_MODULES)},"
            f" expected {sorted(expected)}"
        )


def check_request_refresh_without_sink_is_a_no_op(failures):
    previous = server_jobs.set_refresh_sink(None)
    try:
        if server_jobs.request_refresh("git-ref", "/some/repo") is not False:
            failures.append("request_refresh with no sink installed must return False")
    finally:
        server_jobs.set_refresh_sink(previous)


def check_request_refresh_forwards_to_the_sink(failures):
    seen = []
    previous = server_jobs.set_refresh_sink(
        lambda kind, argument: seen.append((kind, argument)) or True
    )
    try:
        result = server_jobs.request_refresh("session-count", "/my/cwd")
    finally:
        server_jobs.set_refresh_sink(previous)
    if seen != [("session-count", "/my/cwd")]:
        failures.append(f"sink saw {seen}, expected one session-count request")
    if result is not True:
        failures.append("request_refresh must return whatever the sink returned")


def check_set_refresh_sink_returns_the_previous_sink(failures):
    first = object()
    previous = server_jobs.set_refresh_sink(first)
    restored = server_jobs.set_refresh_sink(previous)
    if restored is not first:
        failures.append("set_refresh_sink must return the sink it replaced")


def check_run_refresh_calls_the_named_refresher(failures):
    from statusline_lib import gitref

    calls = []
    original = gitref.refresh_git_ref_cache
    gitref.refresh_git_ref_cache = calls.append
    try:
        server_jobs.run_refresh("git-ref", "/some/repo")
    finally:
        gitref.refresh_git_ref_cache = original
    if calls != ["/some/repo"]:
        failures.append(f"run_refresh dispatched {calls}, expected ['/some/repo']")


def check_run_refresh_rejects_an_unknown_kind(failures):
    try:
        server_jobs.run_refresh("no-such-kind", None)
    except ValueError:
        return
    failures.append("run_refresh must raise ValueError on an unknown kind")


def main():
    failures = []
    for check in (
        check_dispatch_table_covers_every_refresher,
        check_request_refresh_without_sink_is_a_no_op,
        check_request_refresh_forwards_to_the_sink,
        check_set_refresh_sink_returns_the_previous_sink,
        check_run_refresh_calls_the_named_refresher,
        check_run_refresh_rejects_an_unknown_kind,
    ):
        check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: refresh dispatch table, sink, and worker pool all verified")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_jobs.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'statusline_lib.server_jobs'`.

- [ ] **Step 3: Write the minimal implementation**

Create `statusline_lib/server_jobs.py`:

```python
"""In-process refresh jobs for the resident server.

Replaces the detached-child spawner this repository used until 2026-09-02
(the deleted statusline_lib/refresh.py). Cache readers still serve a stale
entry immediately and still hand recomputation to something else, but that
something else is now a bounded thread pool inside the one resident server
process rather than a fresh detached Python interpreter per stale read.
Under load the old shape multiplied to roughly a thousand processes; a pool
of four threads cannot.

Two pieces:
  * `request_refresh`, the call every cache reader makes. It forwards to
    whatever sink is installed, and returns False when none is (the plain
    render path outside the server must never start background work).
  * `WorkerPool`, the sink the server installs: a fixed number of daemon
    threads draining one queue, with at most one job in flight per
    (kind, argument) pair.

Imports: standard library only, plus a lazy per-call import of the refresher
module inside `run_refresh` (pace, burnrate, gitref, beacon_cache, beacon,
sessions, qwen_quota and fable_quota all import THIS module, so a top-level
import here would be circular).
"""

import importlib
import queue
import threading

# Refresh kind -> (module name within this package, refresher attribute).
REFRESHER_MODULES = {
    "pace-hourly": ("pace", "refresh_pace_hourly_cache"),
    "window-spend": ("burnrate", "refresh_window_spend_cache"),
    "git-ref": ("gitref", "refresh_git_ref_cache"),
    "beacon-latest": ("beacon_cache", "refresh_beacon_latest_cache"),
    "session-count": ("sessions", "refresh_session_count_cache"),
    "bias-factor": ("beacon", "refresh_bias_factor_cache"),
    "qwen-quota": ("qwen_quota", "refresh_qwen_quota_cache"),
    "fable-quota": ("fable_quota", "refresh_fable_quota_cache"),
}

_REFRESH_SINK = None


def set_refresh_sink(sink):
    """Install `sink` as the destination for request_refresh, returning the
    sink it replaced so a caller (the server, a verify script) can restore
    it. `sink` is called as sink(kind, argument) and returns truthy when the
    job was accepted."""
    global _REFRESH_SINK
    previous = _REFRESH_SINK
    _REFRESH_SINK = sink
    return previous


def request_refresh(kind, argument):
    """Ask for cache `kind` to be recomputed for `argument`. Returns False
    when no sink is installed, which is the case for every process that is
    not the resident server: a plain render serves its stale cache entry and
    starts nothing."""
    sink = _REFRESH_SINK
    if sink is None:
        return False
    return sink(kind, argument)


def run_refresh(kind, argument):
    """Recompute cache `kind` for `argument` by calling the refresher named
    in REFRESHER_MODULES. Raises ValueError on an unknown kind; any exception
    the refresher itself raises propagates to the pool, which logs it."""
    target = REFRESHER_MODULES.get(kind)
    if target is None:
        raise ValueError(f"unknown refresh kind: {kind!r}")
    module_name, attribute = target
    module = importlib.import_module(f".{module_name}", package=__package__)
    getattr(module, attribute)(argument)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python scripts/verify_server_jobs.py`
Expected: `OK: refresh dispatch table, sink, and worker pool all verified`

- [ ] **Step 5: Format, lint, and commit**

```bash
python -m ruff format statusline_lib/server_jobs.py scripts/verify_server_jobs.py
python -m ruff check statusline_lib/server_jobs.py scripts/verify_server_jobs.py
git add statusline_lib/server_jobs.py scripts/verify_server_jobs.py
git commit -m "feat: refresh dispatch table and request sink for the resident server"
```

### Task 2: Bounded worker pool

**Files:**
- Modify: `statusline_lib/server_jobs.py` (append the pool)
- Modify: `scripts/verify_server_jobs.py` (append the pool checks)

**Interfaces:**
- Consumes: `run_refresh` and `set_refresh_sink` from Task 1.
- Produces:
  - `WORKER_POOL_SIZE = 4`
  - `class WorkerPool(size=WORKER_POOL_SIZE, runner=run_refresh, error_logger=None)` with methods `start()`, `submit(kind, argument) -> bool`, `stop(timeout=2.0)`, `queue_depth() -> int`, `in_flight_count() -> int`, `peak_in_flight() -> int`, `worker_count() -> int`.

The spec says "one in flight per kind". The dispatch entries are per kind but their arguments are per cwd (`git-ref`, `session-count`) and per window (`pace-hourly`, `window-spend`), so keying by kind alone would starve every cwd but one. The pool therefore keys by `(kind, str(argument))`, which is exactly the identity the deleted inflight marker used. See "Resolved ambiguities" at the end of this plan.

- [ ] **Step 1: Write the failing test**

Add `import threading` to `scripts/verify_server_jobs.py` and append these checks before `main()`, then add all five to `main()`'s tuple:

```python
def check_pool_runs_a_submitted_job(failures):
    done = threading.Event()
    seen = []

    def runner(kind, argument):
        seen.append((kind, argument))
        done.set()

    pool = server_jobs.WorkerPool(size=2, runner=runner)
    pool.start()
    try:
        accepted = pool.submit("git-ref", "/repo")
        if not done.wait(timeout=5):
            failures.append("pool never ran the submitted job")
    finally:
        pool.stop()
    if not accepted:
        failures.append("submit must return True for an accepted job")
    if seen != [("git-ref", "/repo")]:
        failures.append(f"pool ran {seen}, expected one git-ref job")


def check_pool_dedupes_a_job_already_in_flight(failures):
    release = threading.Event()
    started = threading.Event()
    ran = []

    def runner(kind, argument):
        ran.append((kind, argument))
        started.set()
        release.wait(timeout=5)

    pool = server_jobs.WorkerPool(size=4, runner=runner)
    pool.start()
    try:
        first = pool.submit("git-ref", "/repo")
        started.wait(timeout=5)
        duplicate = pool.submit("git-ref", "/repo")
        other_argument = pool.submit("git-ref", "/other")
        release.set()
    finally:
        pool.stop()
    if not first:
        failures.append("the first submit must be accepted")
    if duplicate is not False:
        failures.append("a job already in flight for the same argument must be refused")
    if other_argument is not True:
        failures.append("the same kind with a different argument must be accepted")
    if len(ran) != 2:
        failures.append(f"pool ran {ran}, expected two distinct jobs")


def check_pool_never_exceeds_its_size(failures):
    release = threading.Event()
    entered = threading.Semaphore(0)

    def runner(kind, argument):
        entered.release()
        release.wait(timeout=5)

    pool = server_jobs.WorkerPool(size=2, runner=runner)
    pool.start()
    try:
        for index in range(6):
            pool.submit("git-ref", f"/repo-{index}")
        for _ in range(2):
            if not entered.acquire(timeout=5):
                failures.append("pool did not start its two workers")
        if pool.in_flight_count() > 2:
            failures.append(f"{pool.in_flight_count()} jobs in flight, pool size is 2")
        release.set()
    finally:
        pool.stop()
    if pool.peak_in_flight() > 2:
        failures.append(f"peak in flight was {pool.peak_in_flight()}, cap is 2")
    if pool.worker_count() != 2:
        failures.append(f"pool started {pool.worker_count()} threads, expected 2")


def check_pool_logs_and_survives_a_raising_job(failures):
    logged = []
    done = threading.Event()

    def runner(kind, argument):
        try:
            raise RuntimeError("boom")
        finally:
            done.set()

    pool = server_jobs.WorkerPool(size=1, runner=runner, error_logger=logged.append)
    pool.start()
    try:
        pool.submit("git-ref", "/repo")
        done.wait(timeout=5)
    finally:
        pool.stop()
    if not logged:
        failures.append("a raising job must be reported to the error logger")
    if pool.in_flight_count() != 0:
        failures.append("a raising job must still clear its in-flight claim")


def check_pool_refuses_submissions_after_stop(failures):
    pool = server_jobs.WorkerPool(size=1, runner=lambda kind, argument: None)
    pool.start()
    pool.stop()
    if pool.submit("git-ref", "/repo") is not False:
        failures.append("a stopped pool must refuse new submissions")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_jobs.py`
Expected: FAIL with `AttributeError: module 'statusline_lib.server_jobs' has no attribute 'WorkerPool'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `statusline_lib/server_jobs.py`:

```python
# Four threads is the whole point of the pool: the failure this replaced was
# unbounded process creation, so the replacement must have a hard ceiling that
# no amount of load can lift.
WORKER_POOL_SIZE = 4

# How long a worker blocks on the queue before re-checking the stop flag.
# Bounded rather than infinite so stop() cannot wedge on an idle pool, and
# small enough that shutdown is not perceptible.
_QUEUE_POLL_SECONDS = 0.25

# Longest stop() waits for a worker thread to notice the stop flag. Kept at or
# below the two second cap scripts/verify_render_budget.py enforces on every
# literal timeout in the package.
_STOP_JOIN_SECONDS = 2.0

_SHUTDOWN = object()


class WorkerPool:
    """A fixed number of daemon threads draining one job queue, with at most
    one job in flight per (kind, argument).

    `runner(kind, argument)` does the work; the default is run_refresh.
    `error_logger(exception)` receives anything a job raises, so one bad
    refresher can never take the server down.
    """

    def __init__(self, size=WORKER_POOL_SIZE, runner=run_refresh, error_logger=None):
        self._size = size
        self._runner = runner
        self._error_logger = error_logger
        self._queue = queue.Queue()
        self._threads = []
        self._lock = threading.Lock()
        self._claimed = set()
        self._in_flight = 0
        self._peak_in_flight = 0
        self._stopped = False

    def start(self):
        """Start the worker threads. Idempotent."""
        with self._lock:
            if self._threads or self._stopped:
                return
            for index in range(self._size):
                thread = threading.Thread(
                    target=self._drain, name=f"statusline-worker-{index}", daemon=True
                )
                self._threads.append(thread)
        for thread in self._threads:
            thread.start()

    def submit(self, kind, argument):
        """Queue a job unless one for the same (kind, argument) is already
        queued or running, or the pool is stopped. Returns True when queued."""
        key = (kind, str(argument))
        with self._lock:
            if self._stopped or key in self._claimed:
                return False
            self._claimed.add(key)
        self._queue.put((kind, argument, key))
        return True

    def _drain(self):
        while True:
            try:
                item = self._queue.get(timeout=_QUEUE_POLL_SECONDS)
            except queue.Empty:
                if self._stopped:
                    return
                continue
            if item is _SHUTDOWN:
                return
            kind, argument, key = item
            with self._lock:
                self._in_flight += 1
                self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
            try:
                self._runner(kind, argument)
            except Exception as error:
                if self._error_logger is not None:
                    self._error_logger(error)
            finally:
                with self._lock:
                    self._in_flight -= 1
                    self._claimed.discard(key)

    def stop(self, timeout=_STOP_JOIN_SECONDS):
        """Stop accepting work and wait briefly for the threads to notice.
        The threads are daemons, so a straggler never keeps the process
        alive."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        for _ in self._threads:
            self._queue.put(_SHUTDOWN)
        for thread in self._threads:
            thread.join(timeout=timeout)

    def queue_depth(self):
        return self._queue.qsize()

    def in_flight_count(self):
        with self._lock:
            return self._in_flight

    def peak_in_flight(self):
        with self._lock:
            return self._peak_in_flight

    def worker_count(self):
        return len(self._threads)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python scripts/verify_server_jobs.py`
Expected: `OK: refresh dispatch table, sink, and worker pool all verified`

- [ ] **Step 5: Commit**

```bash
python -m ruff format statusline_lib/server_jobs.py scripts/verify_server_jobs.py
python -m ruff check statusline_lib/server_jobs.py scripts/verify_server_jobs.py
git add statusline_lib/server_jobs.py scripts/verify_server_jobs.py
git commit -m "feat: bounded worker pool for in-process refresh jobs"
```

### Task 3: Retire the detached spawner

Repoints all eight cache readers onto `request_refresh`, deletes `statusline_lib/refresh.py` and its two dedicated verify scripts, and strips the now-dead per-render spawn instrumentation out of `rendertimer.py` and `statusline.py`.

**Files:**
- Delete: `statusline_lib/refresh.py`, `scripts/verify_refresh_spawner.py`, `scripts/verify_refresh_liveness.py`
- Modify: `statusline_lib/gitref.py:32,69,71,100,106`, `statusline_lib/beacon.py:19,328,330`, `statusline_lib/beacon_cache.py:30,62,64`, `statusline_lib/sessions.py:29,110,112`, `statusline_lib/pace.py:18,262,264`, `statusline_lib/burnrate.py:35,166,168`, `statusline_lib/qwen_quota.py:68,216`, `statusline_lib/fable_quota.py:62,388,390`
- Modify: `statusline_lib/rendertimer.py` (drop the `reset_spawn_timings` import, the `start_phase_timer` call to it, and `summarize_spawns`)
- Modify: `statusline_lib/__init__.py:32-33` (module map entry)
- Modify: `statusline.py:78,~545` (drop `spawn_timings` / `summarize_spawns`)
- Modify: `scripts/verify_active_session_count.py`, `scripts/verify_beacon_walker.py`, `scripts/verify_git_ref_cache.py`, `scripts/verify_git_working_tree_cache.py`, `scripts/verify_pace_refresh.py`, `scripts/verify_spend_refresh.py`, `scripts/verify_qwen_quota.py`, `scripts/verify_fable_quota.py`, `scripts/verify_phase_timer.py`

**Interfaces:**
- Consumes: `request_refresh` from Task 1.
- Produces: no new symbols. `statusline_lib.refresh` no longer exists; `maybe_spawn_refresh`, `run_refresh` (old location), `spawn_timings`, `reset_spawn_timings` and `summarize_spawns` are gone.

- [ ] **Step 1: Repoint the eight cache readers**

In each of the eight modules, replace the import and the call sites. The call sites keep their exact arguments, so the stale-while-revalidate contract is unchanged:

```bash
python - <<'PY'
import pathlib
modules = [
    "gitref.py", "beacon.py", "beacon_cache.py", "sessions.py",
    "pace.py", "burnrate.py", "qwen_quota.py", "fable_quota.py",
]
for name in modules:
    path = pathlib.Path("statusline_lib") / name
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "from .refresh import maybe_spawn_refresh",
        "from .server_jobs import request_refresh",
    )
    text = text.replace("maybe_spawn_refresh(", "request_refresh(")
    text = text.replace("maybe_spawn_refresh", "request_refresh")
    text = text.replace("statusline_lib/refresh.py", "statusline_lib/server_jobs.py")
    path.write_text(text, encoding="utf-8")
PY
```

Then hand-fix the prose in each module's `Imports:` docstring block so it names `server_jobs -- for request_refresh (in-process cache recompute)`, and update the sentences that describe "a detached child" to describe the server's worker pool instead. The four modules with such prose are `gitref.py`, `beacon.py`, `beacon_cache.py` and `sessions.py`; `fable_quota.py:29` carries the same claim in its module docstring.

- [ ] **Step 2: Strip the spawn instrumentation**

In `statusline_lib/rendertimer.py`: delete the `from .refresh import reset_spawn_timings` import, delete the `reset_spawn_timings()` call from `start_phase_timer` (and the sentence about it in that docstring), delete the whole `summarize_spawns` function, and delete the `PhaseTimer.record` docstring sentence that names `statusline_lib.refresh`'s `spawn_timings()`. Keep `PhaseTimer.record` itself: the server uses it for worker-pool timings.

In `statusline.py`: delete `from statusline_lib.refresh import spawn_timings` (line 78) and the `summarize_spawns` name from the `statusline_lib.rendertimer` import block, and replace the `summarize_spawns(_phase_timer, spawn_timings())` call in `main()` with `_phase_timer.mark("beacon")` so the phase is still recorded.

In `statusline_lib/__init__.py`, replace the `refresh` entry in the module map comment (lines 32 to 33) with:

```
  server_jobs  -- in-process refresh jobs (request_refresh / WorkerPool)
                  shared by every cache that serves stale while revalidating
```

- [ ] **Step 3: Delete the spawner and its dedicated tests**

```bash
git rm statusline_lib/refresh.py scripts/verify_refresh_spawner.py scripts/verify_refresh_liveness.py
```

- [ ] **Step 4: Repoint the verify scripts that patch the spawner**

Nine verify scripts monkeypatch `<module>.maybe_spawn_refresh`. Rename the attribute they patch:

```bash
python - <<'PY'
import pathlib
scripts = [
    "verify_active_session_count.py", "verify_beacon_walker.py",
    "verify_git_ref_cache.py", "verify_git_working_tree_cache.py",
    "verify_pace_refresh.py", "verify_spend_refresh.py",
    "verify_qwen_quota.py", "verify_fable_quota.py",
]
for name in scripts:
    path = pathlib.Path("scripts") / name
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "from statusline_lib.refresh import maybe_spawn_refresh, run_refresh",
        "from statusline_lib.server_jobs import request_refresh, run_refresh",
    )
    text = text.replace("maybe_spawn_refresh", "request_refresh")
    text = text.replace("refresh.request_refresh", "server_jobs.request_refresh")
    path.write_text(text, encoding="utf-8")
PY
```

In `scripts/verify_phase_timer.py`, delete the check that calls `refresh.maybe_spawn_refresh("git-ref", "/some/repo")` and asserts on `summarize_spawns` output (around line 122), together with its entry in `main()`'s tuple and any `refresh` import it leaves unused. That behavior no longer exists.

- [ ] **Step 5: Run the whole suite under coverage**

```bash
python -m coverage erase
for t in scripts/verify_*.py; do python -m coverage run -a "$t" || echo "FAILED $t"; done
python -m coverage report -m --include="statusline_lib/*" --fail-under=100
```

Expected: every script passes and coverage reports 100 percent. `statusline_lib/server_jobs.py` is fully covered by `scripts/verify_server_jobs.py`; if `run_refresh`'s `ValueError` arm or the pool's `error_logger is None` arm shows uncovered, add the missing check to that script rather than adding a pragma.

- [ ] **Step 6: Confirm the aislop gate**

Run: `aislop ci .`
Expected: exit 0. Deleting `refresh.py` (294 lines) plus two verify scripts and adding one smaller module should leave the score at or above 90.

- [ ] **Step 7: Commit**

```bash
python -m ruff format .
python -m ruff check .
git add -A
git commit -m "refactor: replace the detached refresh spawner with in-process jobs"
```

---

## Phase 2: The render moves into statusline_lib

The server must turn a payload into rendered text without going through `statusline.py`'s stdin-and-stdout `main()`. Today that logic lives in the root entry points, which are outside the coverage gate. Moving it into `statusline_lib` is what the spec means by "rendering, formatting, prefs, quota, beacon and adapter code move into the server unchanged where possible", and it has two side benefits: the render finally comes under the 100 percent coverage gate, and `statusline.py` (616 lines, one of the seven files over aislop's 400-line limit) drops below 100 lines.

The one behavior change in this phase: the extracted Claude render takes the transcript walk as a parameter instead of calling `walk_transcript` itself. That is what lets the server supply an incrementally maintained walk in Phase 3.

### Task 4: Extract line 2

**Files:**
- Create: `statusline_lib/render_line2.py`
- Modify: `statusline.py` (delete `_hide_cost`, `_Line2`, `_render_line2`; import them instead)
- Create: `scripts/verify_render_line2.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `class Line2Inputs(NamedTuple)` with exactly the fields `statusline.py`'s `_Line2` has today, in the same order: `model_summary: str`, `context_used: int`, `window_size: int`, `model_id: str`, `walk: dict`, `rate_limits: dict | None`, `day_budget_summary: str`, `cost_summary: str`, `hide_cost: bool`, `lines_summary: str`, `agy_quota: dict | None = None`, `current_usage: dict | None = None`, `is_agy: bool = False`.
  - `render_line2(flags: dict, inputs: Line2Inputs) -> str`
  - `hide_cost() -> bool`

- [ ] **Step 1: Write the failing test**

Create `scripts/verify_render_line2.py`. It calls `render_line2` in-process against a hand-built `Line2Inputs`, once with every flag on and once with `hide_cost=True`, and asserts on the presence and absence of the dollar-bearing fields:

```python
"""Verify statusline_lib/render_line2.py, the line-2 formatter extracted from
statusline.py so the resident server can render without the entry script.

Run from anywhere; imports from agent-statusline by path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.compact import resolve_flags
from statusline_lib.render_line2 import Line2Inputs, hide_cost, render_line2

_WALK = {
    "read": 40000,
    "write": 100,
    "input": 10,
    "output": 50,
    "read_cost": 0.012,
    "write_cost": 0.000375,
    "input_cost": 0.00015,
    "output_cost": 0.00375,
    "ttl_evictions": 2,
    "ttl_wasted": 0.5,
}


def _inputs(**overrides):
    fields = {
        "model_summary": "Opus",
        "context_used": 40110,
        "window_size": 200000,
        "model_id": "claude-opus-4-8",
        "walk": _WALK,
        "rate_limits": None,
        "day_budget_summary": "day $1.00",
        "cost_summary": "$2.50",
        "hide_cost": False,
        "lines_summary": "+10/-2",
    }
    fields.update(overrides)
    return Line2Inputs(**fields)


def check_full_line_carries_every_field(failures):
    flags = resolve_flags(lambda f: "", None)
    rendered = render_line2(flags, _inputs())
    for fragment in ("$2.50", "day $1.00", "+10/-2"):
        if fragment not in rendered:
            failures.append(f"line 2 lost {fragment!r}: {rendered!r}")


def check_hide_cost_suppresses_every_dollar_figure(failures):
    flags = resolve_flags(lambda f: "", None)
    rendered = render_line2(flags, _inputs(hide_cost=True))
    if "$" in rendered:
        failures.append(f"hide_cost must suppress every dollar figure: {rendered!r}")
    if "+10/-2" not in rendered:
        failures.append("the diffstat is not money and must survive hide_cost")


def check_hide_cost_reads_the_pref(failures):
    saved = os.environ.get("STATUSLINE_HIDE_COST")
    os.environ["STATUSLINE_HIDE_COST"] = "1"
    try:
        if hide_cost() is not True:
            failures.append("STATUSLINE_HIDE_COST=1 must resolve to True")
    finally:
        if saved is None:
            os.environ.pop("STATUSLINE_HIDE_COST", None)
        else:
            os.environ["STATUSLINE_HIDE_COST"] = saved
```

Add the remaining checks the extracted code needs for 100 percent coverage: an Antigravity payload (`is_agy=True`, empty walk) exercising the `format_agy_cache` fallback, and a payload with `rate_limits` set exercising the `format_quota` and `format_fable_quota` arms. Close with the standard `main()` collector.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_render_line2.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'statusline_lib.render_line2'`.

- [ ] **Step 3: Move the code**

Create `statusline_lib/render_line2.py` with a module docstring explaining that it holds the line-2 formatter the resident server calls, then move these three definitions out of `statusline.py` verbatim, renaming as they land:

| In `statusline.py` | In `render_line2.py` |
|---|---|
| `_hide_cost` | `hide_cost` |
| `_Line2` | `Line2Inputs` |
| `_render_line2` | `render_line2` |

Rename the `_Line2` field `ctx_used` to `context_used` (full words, no abbreviations) and update the two reads of it inside `render_line2`. The module's imports come from siblings, not from `statusline_lib`'s package root:

```python
from typing import NamedTuple

from .agy import format_agy_cache, format_agy_quota
from .badge import format_context
from .cachefmt import format_cache, format_ttl
from .fable_quota import format_fable_quota
from .pace import format_quota
from .prefs import pref_bool
```

Verify each name's real home before writing the import block (`grep -rn "^def format_cache" statusline_lib/`); the table above is the expected layout, not a guess to be trusted blind.

In `statusline.py`, delete the three moved definitions and import the replacements:

```python
from statusline_lib.render_line2 import Line2Inputs, hide_cost, render_line2
```

Update `main()`'s two call sites: `_Line2(...)` becomes `Line2Inputs(...)`, `_hide_cost()` becomes `hide_cost()`, and both `_render_line2` references become `render_line2`.

- [ ] **Step 4: Run the test and the suite**

```bash
python scripts/verify_render_line2.py
python scripts/verify_compact_mode.py
python scripts/verify_hide_cost.py
python scripts/verify_quota_render.py
```
Expected: all four print their `OK:` line.

- [ ] **Step 5: Confirm coverage of the new module**

```bash
python -m coverage erase
for t in scripts/verify_*.py; do python -m coverage run -a "$t" || echo "FAILED $t"; done
python -m coverage report -m --include="statusline_lib/render_line2.py" --fail-under=100
```
Expected: 100 percent. Any uncovered line gets a new check in `scripts/verify_render_line2.py`, never a pragma.

- [ ] **Step 6: Commit**

```bash
python -m ruff format .
python -m ruff check .
git add -A
git commit -m "refactor: extract line 2 rendering into statusline_lib"
```

### Task 5: Extract the Claude render

**Files:**
- Create: `statusline_lib/render_claude.py`
- Modify: `statusline.py` (shrinks to the client wrapper skeleton plus the qwen and kimi branches)
- Create: `scripts/verify_render_claude.py`

**Interfaces:**
- Consumes: `Line2Inputs`, `render_line2`, `hide_cost` from Task 4.
- Produces:
  - `render_claude_statusline(payload: dict, cwd: str, walk: dict, now: float, *, state_dir=None) -> str` returning the complete rendered text, up to three lines joined with `"\n"`, no trailing newline.
  - `context_usage(payload) -> tuple[int, int]` returning `(context_used, window_size)`, so the server can write the wrap-nudge occupancy file without re-deriving it.
  - `transcript_path_for(payload) -> str`, the payload's `transcript_path` or the `_find_session_jsonl` fallback, so the server can key its per-session table before rendering.

- [ ] **Step 1: Write the failing test**

Create `scripts/verify_render_claude.py`. It builds a payload plus a `walk` dict (the shape `walk_transcript` returns), calls `render_claude_statusline` in-process, and asserts on structure rather than exact strings:

```python
def check_render_produces_three_lines(failures):
    payload = _payload()
    walk = _walk()
    rendered = render_claude_statusline(payload, _REPO, walk, now=_NOW, state_dir=state)
    lines = rendered.split("\n")
    if not 1 <= len(lines) <= 3:
        failures.append(f"expected one to three lines, got {len(lines)}")
    if _SESSION_ID[:8] not in lines[0]:
        failures.append(f"line 1 must carry the session badge: {lines[0]!r}")
    if "|" not in lines[1]:
        failures.append(f"line 2 must be the pipe-joined field list: {lines[1]!r}")


def check_render_takes_the_walk_it_is_given(failures):
    """The extracted render must never call walk_transcript itself: the
    server owns the walk and maintains it incrementally."""
    walk = _walk(read=123456)
    rendered = render_claude_statusline(_payload(), _REPO, walk, now=_NOW, state_dir=state)
    if "123" not in rendered:
        failures.append("the render ignored the walk it was handed")


def check_context_usage_sums_the_three_usage_fields(failures):
    used, window = context_usage(_payload())
    if (used, window) != (40110, 200000):
        failures.append(f"context_usage returned {(used, window)}")
```

Add checks covering the branches the entry script used to reach only by subprocess: a payload with no `workspace` (the `cwd` fallback), a payload whose `project_dir` differs from `current_dir` (the relative-hop colouring in `_format_cwd`), a detached-HEAD git ref, a session name long enough to be dropped by the width fit-check, and the `STATUSLINE_BEACON=0` arm of `_beacon_line`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_render_claude.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'statusline_lib.render_claude'`.

- [ ] **Step 3: Move the code**

Create `statusline_lib/render_claude.py` and move these definitions from `statusline.py` verbatim, keeping their bodies and docstrings: `_GIT_HASH_COLOR`, `_HOST_COLOR`, `_git_ref`, `_CWD_REL_COLOR`, `_format_cwd`, `_line1`, `_append_session_id`, `_append_turn_count`, `_append_session_name`, `_beacon_line`.

Then write the new orchestrator, which is `statusline.py`'s `main()` body with the stdin read, the JSON parse, the platform routing, the `walk_transcript` call and every `sys.stdout.write` removed:

```python
def context_usage(payload):
    """(context_used, window_size) from the payload's context_window block.

    Anchored on token counts rather than the payload's used_percentage, which
    rounds to whole percent (10K tokens of slop on a 1M window)."""
    context_window = payload.get("context_window") or {}
    window_size = context_window.get("context_window_size") or 200_000
    current_usage = context_window.get("current_usage") or {}
    context_used = (
        (current_usage.get("input_tokens") or 0)
        + (current_usage.get("cache_creation_input_tokens") or 0)
        + (current_usage.get("cache_read_input_tokens") or 0)
    )
    return context_used, window_size


def transcript_path_for(payload):
    """The session transcript path: the payload's own field, else the
    filename scan by session id. "" when neither resolves."""
    path = payload.get("transcript_path")
    if path:
        return path
    session_id = payload.get("session_id") or payload.get("conversation_id") or ""
    if not session_id:
        return ""
    return _find_session_jsonl(session_id) or ""


def render_claude_statusline(payload, cwd, walk, now, *, state_dir=None):
    """Render the Claude Code and Antigravity CLI status line from an already
    computed transcript `walk`.

    The walk is a parameter, not something this function computes: the
    resident server maintains one accumulator per session and folds in only
    the transcript bytes appended since the last render, so re-walking here
    would undo the entire point of the server."""
```

The body follows `main()`'s existing order exactly: model badge, `write_ctx_state` removed (the server calls it), cost summary from `format_cost_with_subagents`, diffstat, day budget, terminal width hint, `is_agy`, spinner, turn summary, line 1, the `Line2Inputs` construction, `resolve_flags` plus `render_line2`, and line 3's `"  ·  ".join(...)`. Return `"\n".join(part for part in (line1, line2, line3) if part)`.

Two substitutions in the moved code:
- `time.time()` inside `format_teammates(...)` becomes the `now` parameter, so tests inject the clock.
- `state_dir` is threaded into `_git_ref` and `format_render_suffix` instead of defaulting, so tests point at a temporary directory.

In `statusline.py`, delete every moved definition. What remains of `main()` for the Claude branch is:

```python
    walk = walk_transcript(transcript_path_for(d), include_subagents=True)
    write_ctx_state(session_id, *context_usage(d), time.time())
    sys.stdout.write(render_claude_statusline(d, cwd, walk, time.time()))
```

This keeps `statusline.py` runnable and green for the whole of Phase 2; Task 17 replaces it with the client wrapper.

- [ ] **Step 4: Run the render suites**

```bash
python scripts/verify_render_claude.py
python scripts/verify_compact_mode.py
python scripts/verify_beacon_render.py
python scripts/verify_session_fields.py
python scripts/verify_teams.py
```
Expected: every one prints its `OK:` line.

- [ ] **Step 5: Confirm coverage and file size**

```bash
python -m coverage erase
for t in scripts/verify_*.py; do python -m coverage run -a "$t" || echo "FAILED $t"; done
python -m coverage report -m --include="statusline_lib/*" --fail-under=100
wc -l statusline_lib/render_claude.py statusline.py
aislop ci .
```
Expected: 100 percent coverage, `render_claude.py` under 400 lines, `statusline.py` far below its previous 616, and `aislop ci .` exit 0 with one fewer oversized file than before.

- [ ] **Step 6: Commit**

```bash
python -m ruff format .
python -m ruff check .
git add -A
git commit -m "refactor: extract the Claude render into statusline_lib"
```

### Task 6: Extract the subagent render

**Files:**
- Create: `statusline_lib/render_subagent.py`
- Modify: `subagent_statusline.py` (keeps only the stdin read, the JSON parse and the write loop)
- Create: `scripts/verify_render_subagent.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `render_subagent_rows(payload: dict, now: float) -> list[str]`, one JSON-encoded row string per renderable task, in payload order, ready for the client to print one per line.

- [ ] **Step 1: Write the failing test**

Create `scripts/verify_render_subagent.py` driving `render_subagent_rows` in-process against a payload carrying a running task, a completed task, a lead task, and a task whose agent transcript is missing. Assert each returned string parses as JSON with a `content` key, that the count matches the renderable tasks, and that a task which raises inside `_row_for_task` is skipped rather than aborting the list.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_render_subagent.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'statusline_lib.render_subagent'`.

- [ ] **Step 3: Move the code**

Move `_agent_jsonl_path`, `_status_icon`, `_is_terminal`, `_format_elapsed`, `_live_payload_for_session`, `_is_lead_task`, `_metrics_for_task` and `_row_for_task` from `subagent_statusline.py` into `statusline_lib/render_subagent.py` verbatim, then add:

```python
def render_subagent_rows(payload, now):
    """One JSON row string per renderable task, in payload order.

    A task that cannot be rendered at all is logged and skipped: one bad row
    must never take down the rest of the panel."""
    session_id = payload.get("session_id") or payload.get("conversation_id") or ""
    parent = payload.get("transcript_path") or ""
    if not parent and session_id:
        parent = _find_session_jsonl(session_id) or ""
    prefix = f"{ORANGE}LOCAL{RESET} | " if is_local_mode() else ""
    rows = []
    for task in payload.get("tasks") or []:
        try:
            row = _row_for_task(task, parent, session_id, now)
        except Exception:
            log_traceback(os.path.join(app_dir(), ".statusline-error.log"))
            continue
        if row is None:
            continue
        row["content"] = prefix + row["content"]
        rows.append(json.dumps(row))
    return rows
```

Thread `now` through `_row_for_task`, `_metrics_for_task` and `_format_elapsed` in place of their internal `time.time()` reads, so the elapsed-time formatting is testable without touching the wall clock.

`subagent_statusline.py` keeps only its stdin read, `safe_write` payload dump, JSON parse, and:

```python
    for row in render_subagent_rows(d, time.time()):
        sys.stdout.write(row + "\n")
```

- [ ] **Step 4: Run the tests**

```bash
python scripts/verify_render_subagent.py
python scripts/verify_subagent_agent_jsonl_path.py
python scripts/verify_subagent_elapsed_hardening.py
python scripts/verify_cost_subagent_split.py
```
Expected: every one prints its `OK:` line.

- [ ] **Step 5: Confirm coverage**

```bash
python -m coverage erase
for t in scripts/verify_*.py; do python -m coverage run -a "$t" || echo "FAILED $t"; done
python -m coverage report -m --include="statusline_lib/*" --fail-under=100
```
Expected: 100 percent.

- [ ] **Step 6: Commit**

```bash
python -m ruff format .
python -m ruff check .
git add -A
git commit -m "refactor: extract the subagent render into statusline_lib"
```

---

## Phase 3: Server state tables

All of this is in memory and rebuilt on start. The on-disk cache files stay exactly as they are: they remain the server's warm-start source and the client's fallback, written on the same schedule as today, so no cache-reading code changes.

### Task 7: An accumulator seam in cost.py

`walk_transcript` today builds its accumulator, walks a whole file, and summarizes, all in one call. The server needs those three steps separately so it can fold in only the bytes appended since the last render. This task exposes them without changing `walk_transcript`'s signature or behavior.

**Files:**
- Modify: `statusline_lib/cost.py:284-385`
- Modify: `scripts/verify_cache_cost_split.py` (add the seam checks)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `new_walk_accumulator() -> dict`, the accumulator `walk_transcript` builds today, with `track_evictions` and `track_user_prompts` both `False`.
  - `fold_transcript_lines(lines, accumulator, seen_ids) -> None`, folding an iterable of raw JSONL lines.
  - `summarize_walk(accumulator, parent_cost) -> dict`, the exact dictionary `walk_transcript` returns.
  - `walk_transcript(path, include_subagents=False) -> dict`, unchanged signature and unchanged output.

- [ ] **Step 1: Write the failing test**

Append to `scripts/verify_cache_cost_split.py`:

```python
def check_the_seam_reproduces_walk_transcript(failures):
    """Folding a transcript one line at a time through the seam must produce
    exactly what walk_transcript produces in one call. This is the contract
    the server's incremental walk depends on."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        lines = _fixture_lines()
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        expected = walk_transcript(path)

        accumulator = new_walk_accumulator()
        accumulator["track_evictions"] = True
        accumulator["track_user_prompts"] = True
        seen_ids = set()
        for line in lines:
            fold_transcript_lines([line], accumulator, seen_ids)
        actual = summarize_walk(accumulator, accumulator["cost"])

        if actual != expected:
            failures.append(
                "line-at-a-time folding diverged from walk_transcript:"
                f" {actual} != {expected}"
            )


def check_new_walk_accumulator_defaults_tracking_off(failures):
    accumulator = new_walk_accumulator()
    if accumulator["track_evictions"] or accumulator["track_user_prompts"]:
        failures.append(
            "a fresh accumulator must not track evictions or user prompts:"
            " those are parent-only and the caller opts in"
        )
```

`_fixture_lines()` returns a handful of JSON-encoded assistant turns with distinct message ids and usage blocks, plus one typed user prompt, so both eviction tracking and prompt counting are exercised.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_cache_cost_split.py`
Expected: FAIL with `ImportError: cannot import name 'new_walk_accumulator'`.

- [ ] **Step 3: Write the implementation**

In `statusline_lib/cost.py`, lift the accumulator literal out of `walk_transcript` into `new_walk_accumulator`, lift the per-line loop out of `_walk_one_transcript` into `fold_transcript_lines`, and lift the return dictionary into `summarize_walk`. Then rebuild the three existing functions on top of them:

```python
def fold_transcript_lines(lines, accumulator, seen_ids):
    """Fold each raw JSONL line in `lines` into `accumulator`. A line that
    does not parse is skipped, not fatal: a torn write at the tail of a live
    transcript is normal, not an error."""
    for line in lines:
        try:
            entry = _json_loads(line)
        except Exception:
            continue
        _accumulate_assistant_turn(entry, accumulator, seen_ids)
        _accumulate_user_prompt(entry, accumulator)


def _walk_one_transcript(path, accumulator, seen_ids):
    """Stream one JSONL transcript, folding each line into `accumulator`."""
    try:
        with open(path, encoding="utf-8") as f:
            fold_transcript_lines(f, accumulator, seen_ids)
    except OSError:
        # Transcript became unreadable mid-walk; use the totals gathered so
        # far rather than failing the whole render.
        pass
```

`walk_transcript` keeps its body but reads `accumulator = new_walk_accumulator()` at the top and `return summarize_walk(accumulator, parent_cost)` at the bottom. Nothing else in it moves, so its behavior is bit-identical.

- [ ] **Step 4: Run the tests**

```bash
python scripts/verify_cache_cost_split.py
python scripts/verify_cost_subagent_split.py
python scripts/verify_turn_count.py
python scripts/verify_ttl_evictions.py
```
Expected: every one prints its `OK:` line, proving the refactor changed no totals.

- [ ] **Step 5: Commit**

```bash
python -m ruff format .
python -m ruff check .
git add -A
git commit -m "refactor: expose the transcript accumulator seam in cost.py"
```

### Task 8: Per-session incremental walk

**Files:**
- Create: `statusline_lib/server_state.py`
- Create: `scripts/verify_server_state.py`

**Interfaces:**
- Consumes: `new_walk_accumulator`, `fold_transcript_lines`, `summarize_walk` from Task 7.
- Produces:
  - `class SessionEntry` with attributes `session_id`, `transcript_path`, `offsets: dict[str, int]`, `accumulator: dict`, `seen_ids: set`, `parent_cost: float`, `last_seen: float`, `last_reply: str`, `rewalk_count: int`.
  - `class StateTables(clock=time.time)` with `touch_session(session_id, transcript_path) -> SessionEntry` and `walk_for(entry) -> dict` returning the `summarize_walk` dictionary.
  - `read_appended(path, offset) -> tuple[list[str], int, bool]` returning `(complete_lines, new_offset, rewalked)`.

- [ ] **Step 1: Write the failing test**

Create `scripts/verify_server_state.py`:

```python
def check_second_walk_reads_only_appended_bytes(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=3)
        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        first = tables.walk_for(entry)
        offset_after_first = entry.offsets[path]

        _append_turns(path, count=2)
        second = tables.walk_for(entry)

        if entry.offsets[path] <= offset_after_first:
            failures.append("the offset did not advance after appended turns")
        if second["assistant_turns"] != 5:
            failures.append(f"expected 5 turns after the append, got {second}")
        if first["assistant_turns"] != 3:
            failures.append("the first walk should have seen exactly 3 turns")


def check_incremental_walk_matches_a_full_walk(failures):
    """Two appends folded incrementally must equal one walk_transcript call
    over the finished file. This is the invariant that makes the server's
    numbers trustworthy."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=4)
        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        tables.walk_for(entry)
        _append_turns(path, count=3)
        incremental = tables.walk_for(entry)
        if incremental != walk_transcript(path, include_subagents=True):
            failures.append("incremental totals diverged from a full walk")


def check_a_partial_trailing_line_is_not_consumed(failures):
    """A transcript being written to can end mid-line. The offset must stop
    at the last newline so the partial line is folded once, later, whole."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=2)
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"type": "assistant", "mess')
        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        walked = tables.walk_for(entry)
        if walked["assistant_turns"] != 2:
            failures.append("a partial trailing line must not be folded")
        if entry.offsets[path] != os.path.getsize(path) - len('{"type": "assistant", "mess'):
            failures.append("the offset must stop at the last complete newline")


def check_a_truncated_transcript_triggers_one_rewalk(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=5)
        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        tables.walk_for(entry)
        _write_turns(path, count=1)  # truncate and rewrite
        walked = tables.walk_for(entry)
        if walked["assistant_turns"] != 1:
            failures.append(f"a shrunk file must be rewalked from zero: {walked}")
        if entry.rewalk_count != 1:
            failures.append(f"expected exactly one rewalk, got {entry.rewalk_count}")


def check_subagent_transcripts_are_walked_incrementally_too(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=2)
        subagents = os.path.join(tmp, "session", "subagents")
        os.makedirs(subagents)
        agent = os.path.join(subagents, "agent-one.jsonl")
        _write_turns(agent, count=2)

        tables = StateTables(clock=_FakeClock().read)
        entry = tables.touch_session("session-a", path)
        first = tables.walk_for(entry)
        _append_turns(agent, count=2)
        second = tables.walk_for(entry)

        if first["assistant_turns"] != 4:
            failures.append(f"parent plus subagent turns should be 4: {first}")
        if second["assistant_turns"] != 6:
            failures.append(f"appended subagent turns were missed: {second}")
        if second["parent_cost"] != first["parent_cost"]:
            failures.append("subagent turns must never move the parent cost")
        if second["subagent_cost"] <= first["subagent_cost"]:
            failures.append("appended subagent turns must raise the subagent cost")
```

`_FakeClock` is a tiny helper with a mutable `now` attribute and a `read()` method; every test in this file injects it rather than reading the wall clock.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_state.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'statusline_lib.server_state'`.

- [ ] **Step 3: Write the implementation**

Create `statusline_lib/server_state.py` with `read_appended` and the per-session half of `StateTables`:

```python
def read_appended(path, offset):
    """Return (complete_lines, new_offset, rewalked) for `path` beyond
    `offset`.

    Only bytes up to the last newline are consumed, so a transcript caught
    mid-write leaves its partial trailing line for the next call rather than
    folding half a JSON object. A file that has shrunk below `offset` was
    rewritten under us, so it is rewalked from zero and `rewalked` is True.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset, False
    rewalked = False
    if size < offset:
        offset = 0
        rewalked = True
    if size == offset:
        return [], offset, rewalked
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
    except OSError:
        return [], offset, rewalked
    consumed = chunk.rfind(b"\n") + 1
    if consumed == 0:
        return [], offset, rewalked
    text = chunk[:consumed].decode("utf-8", "replace")
    return text.splitlines(), offset + consumed, rewalked
```

`SessionEntry` holds one accumulator, one `seen_ids` set, one offset per transcript path (parent plus each subagent file), and a running `parent_cost`. `StateTables.walk_for` folds the parent first with both tracking flags on, records the parent's cost delta, then folds each subagent file with tracking off, exactly reproducing `walk_transcript`'s ordering and its parent-versus-subagent cost split:

```python
    def walk_for(self, entry):
        """Fold whatever has been appended since the last call and return the
        session's totals."""
        accumulator = entry.accumulator
        parent_lines, offset, rewalked = read_appended(
            entry.transcript_path, entry.offsets.get(entry.transcript_path, 0)
        )
        if rewalked:
            self._reset(entry)
            parent_lines, offset, _ = read_appended(entry.transcript_path, 0)
            accumulator = entry.accumulator
        entry.offsets[entry.transcript_path] = offset

        accumulator["track_evictions"] = True
        accumulator["track_user_prompts"] = True
        before = accumulator["cost"]
        fold_transcript_lines(parent_lines, accumulator, entry.seen_ids)
        entry.parent_cost += accumulator["cost"] - before

        accumulator["track_evictions"] = False
        accumulator["track_user_prompts"] = False
        for subagent_path in self._subagent_paths(entry.transcript_path):
            lines, subagent_offset, _ = read_appended(
                subagent_path, entry.offsets.get(subagent_path, 0)
            )
            entry.offsets[subagent_path] = subagent_offset
            fold_transcript_lines(lines, accumulator, entry.seen_ids)

        entry.last_seen = self._clock()
        return summarize_walk(accumulator, entry.parent_cost)
```

`_reset(entry)` replaces the accumulator, the `seen_ids` set, the offsets and `parent_cost`, and increments `rewalk_count`. `_subagent_paths` is the existing `glob.glob(path[:-6] + "/subagents/agent-*.jsonl")` rule from `walk_transcript`, so nothing about which files count changes.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python scripts/verify_server_state.py`
Expected: `OK: server state tables verified`

- [ ] **Step 5: Commit**

```bash
python -m ruff format statusline_lib/server_state.py scripts/verify_server_state.py
python -m ruff check statusline_lib/server_state.py scripts/verify_server_state.py
git add statusline_lib/server_state.py scripts/verify_server_state.py
git commit -m "feat: incremental per-session transcript walk for the resident server"
```

### Task 9: Per-cwd table, machine-wide session scan, and idle drops

**Files:**
- Modify: `statusline_lib/server_state.py`
- Modify: `statusline_lib/sessions.py:79-135`
- Modify: `scripts/verify_server_state.py`
- Modify: `scripts/verify_active_session_count.py`

**Interfaces:**
- Consumes: `StateTables` from Task 8.
- Produces:
  - `SESSION_DROP_SECONDS = 3600`, `CWD_DROP_SECONDS = 3600`
  - `StateTables.touch_cwd(cwd) -> CwdEntry`
  - `StateTables.known_cwds() -> list[str]`
  - `StateTables.drop_idle() -> tuple[int, int]` returning `(sessions_dropped, cwds_dropped)`
  - `StateTables.summary() -> dict` with keys `sessions`, `cwds`, `rewalks`, for the `status` request kind.
  - `sessions.refresh_session_count_cache(cwds)` accepting either one cwd or a sequence of them, writing every entry from a single process scan.

A per-cwd entry carries only `cwd` and `last_seen`. The values it stands for (git ref, working-tree badge, session count) already live in their own on-disk TTL caches, which the render reads directly. The table's job is lifetime: a cwd is refreshed only while at least one live session references it, and one hour after the last referencing session it is dropped along with its scheduled refreshes.

The session count is the exception worth restructuring. `refresh_session_count_cache(cwd)` walks every process on the machine to answer one directory, so six live directories paid six full psutil walks. In the server there is one process that knows every live cwd, so one walk answers all of them.

- [ ] **Step 1: Write the failing test**

```python
def check_an_unseen_session_is_dropped_after_an_hour(failures):
    clock = _FakeClock()
    tables = StateTables(clock=clock.read)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=1)
        tables.touch_session("session-a", path)
        tables.touch_cwd("/repo-a")
        clock.now += 3599
        if tables.drop_idle() != (0, 0):
            failures.append("nothing may be dropped one second before the hour")
        clock.now += 2
        if tables.drop_idle() != (1, 1):
            failures.append("both tables must drop their idle entry past the hour")


def check_touching_a_session_keeps_it_alive(failures):
    clock = _FakeClock()
    tables = StateTables(clock=clock.read)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.jsonl")
        _write_turns(path, count=1)
        tables.touch_session("session-a", path)
        for _ in range(4):
            clock.now += 1800
            tables.touch_session("session-a", path)
            tables.drop_idle()
        if tables.summary()["sessions"] != 1:
            failures.append("a session touched every 30 minutes must never drop")


def check_a_new_transcript_path_resets_the_session_entry(failures):
    """A session id reused against a different transcript file must not fold
    the new file's turns into the old file's accumulator."""
    with tempfile.TemporaryDirectory() as tmp:
        first_path = os.path.join(tmp, "first.jsonl")
        second_path = os.path.join(tmp, "second.jsonl")
        _write_turns(first_path, count=5)
        _write_turns(second_path, count=2)
        tables = StateTables(clock=_FakeClock().read)
        tables.walk_for(tables.touch_session("session-a", first_path))
        walked = tables.walk_for(tables.touch_session("session-a", second_path))
        if walked["assistant_turns"] != 2:
            failures.append(f"a new transcript path must start clean: {walked}")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_state.py`
Expected: FAIL with `AttributeError: 'StateTables' object has no attribute 'touch_cwd'`.

- [ ] **Step 3: Write the implementation**

Add the constants, `CwdEntry`, `touch_cwd`, `known_cwds`, `drop_idle` and `summary` to `server_state.py`. `drop_idle` compares `self._clock() - entry.last_seen` against the constants and deletes in place. `touch_session` compares the incoming `transcript_path` against the stored one and calls `_reset` when they differ.

- [ ] **Step 4: Write the failing test for the machine-wide scan**

Append to `scripts/verify_active_session_count.py`:

```python
def check_one_scan_answers_every_known_cwd(failures):
    """The psutil walk is machine-wide, so one walk must fill in every live
    directory's count. Six directories used to cost six full walks."""
    scans = []

    def counting_scan(target_cwd, psutil_module):
        scans.append(target_cwd)
        return 2

    saved = sessions._count_via_psutil
    sessions._count_via_psutil = counting_scan
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "counts.json")
            sessions.refresh_session_count_cache(
                ["/repo-a", "/repo-b", "/repo-c"], cache_path=path
            )
            cache = _load(path)
    finally:
        sessions._count_via_psutil = saved

    if len(cache) != 3:
        failures.append(f"one refresh must write three entries, wrote {len(cache)}")
    if len(sessions.process_snapshots_taken()) != 1:
        failures.append("three directories must cost exactly one process snapshot")


def check_a_single_cwd_argument_still_works(failures):
    """The signature stays backward compatible: every existing caller passes
    one string, and the pool submits one argument per job."""
```

- [ ] **Step 5: Widen the session-count refresher**

Change `refresh_session_count_cache(cwd)` to accept a string or a sequence, normalize to a list, take one `_LazySnapshot` for the whole call, and score every directory against it. `_count_via_psutil` gains an optional snapshot parameter so the loop reuses one instead of building one per directory. Add `process_snapshots_taken()` as a small counter the test reads, reset per call.

The server submits the whole set as one job: `_render_claude` calls `request_refresh("session-count", tuple(self._tables.known_cwds()))` rather than one job per directory, so the pool's per-argument deduplication collapses a burst across directories into one scan.

- [ ] **Step 6: Run the tests**

```bash
python scripts/verify_server_state.py
python scripts/verify_active_session_count.py
python scripts/verify_session_tree.py
python scripts/verify_session_debounce.py
```
Expected: every one prints its `OK:` line.

- [ ] **Step 7: Commit**

```bash
python -m ruff format .
python -m ruff check .
git add -A
git commit -m "feat: per-cwd table, machine-wide session scan, and idle drops"
```

### Task 10: State directory housekeeping

The state directory currently holds roughly 2,000 stale per-session cache files. Nothing ever deleted them because no process outlived a render. The server does, so it sweeps on start and hourly after that.

**Files:**
- Modify: `statusline_lib/server_state.py`
- Modify: `scripts/verify_server_state.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `HOUSEKEEPING_MAX_AGE_SECONDS = 7 * 86400`, `HOUSEKEEPING_INTERVAL_SECONDS = 3600`
  - `HOUSEKEEPING_PREFIXES = ("beacons-latest-", "last-render-", "render-timer-")`
  - `housekeep_state_dir(directory, now, max_age_seconds=HOUSEKEEPING_MAX_AGE_SECONDS) -> int` returning the number of files deleted.

- [ ] **Step 1: Write the failing test**

```python
def check_housekeeping_deletes_only_old_matching_files(failures):
    with tempfile.TemporaryDirectory() as state:
        now = 1_700_000_000.0
        old = now - 8 * 86400
        recent = now - 3600
        _touch(state, "beacons-latest-aaa.json", old)
        _touch(state, "last-render-bbb.txt", old)
        _touch(state, "render-timer-ccc.json", recent)
        _touch(state, "server.json", old)
        _touch(state, "something-else.json", old)

        deleted = housekeep_state_dir(state, now)

        remaining = sorted(os.listdir(state))
        if deleted != 2:
            failures.append(f"housekeeping deleted {deleted} files, expected 2")
        if "server.json" not in remaining:
            failures.append("housekeeping must never delete the server info file")
        if "something-else.json" not in remaining:
            failures.append("housekeeping must only touch its own prefixes")
        if "render-timer-ccc.json" not in remaining:
            failures.append("a recent file must survive")


def check_housekeeping_survives_an_unreadable_directory(failures):
    if housekeep_state_dir(os.path.join("no", "such", "dir"), 0.0) != 0:
        failures.append("a missing state directory must sweep zero files, not raise")
```

`_touch(directory, name, mtime)` writes the file and pins its mtime with `os.utime`, per the repo rule that tests build their own fixtures against a synthetic clock.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_state.py`
Expected: FAIL with `ImportError: cannot import name 'housekeep_state_dir'`.

- [ ] **Step 3: Write the implementation**

```python
def housekeep_state_dir(directory, now, max_age_seconds=HOUSEKEEPING_MAX_AGE_SECONDS):
    """Delete per-session state files older than `max_age_seconds`, returning
    how many went.

    Scoped to HOUSEKEEPING_PREFIXES: these are per-session caches that no
    longer have a session, and nothing reads a week-old one. Every other file
    in the state directory, the server info file included, is left alone.
    Best effort throughout: an unreadable directory or an undeletable file
    costs the sweep, never the server.
    """
    deleted = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(HOUSEKEEPING_PREFIXES):
            continue
        path = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(path) < max_age_seconds:
                continue
            os.remove(path)
        except OSError:
            continue
        deleted += 1
    return deleted
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python scripts/verify_server_state.py`
Expected: `OK: server state tables verified`

- [ ] **Step 5: Confirm coverage**

```bash
python -m coverage erase
python -m coverage run -a scripts/verify_server_state.py
python -m coverage report -m --include="statusline_lib/server_state.py" --fail-under=100
```
Expected: 100 percent.

- [ ] **Step 6: Commit**

```bash
python -m ruff format statusline_lib/server_state.py scripts/verify_server_state.py
python -m ruff check statusline_lib/server_state.py scripts/verify_server_state.py
git add statusline_lib/server_state.py scripts/verify_server_state.py
git commit -m "feat: hourly state directory housekeeping"
```

---

## Phase 4: The server

### Task 11: Server info file and code version

The client and the server must compute the same code version from the same tree, and the client cannot import `statusline_lib`. This task writes the server side and pins the algorithm precisely enough that Task 14's client copy provably matches it; Task 16 asserts the two agree.

**Files:**
- Create: `statusline_lib/server_info.py`
- Create: `scripts/verify_server_info.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `SERVER_INFO_FILENAME = "server.json"`
  - `server_info_path(state_directory=None) -> str`
  - `code_version(repository_root) -> str`, a 16 character hexadecimal digest
  - `write_server_info(path, *, pid, port, version, started_at, platform) -> None`, atomic
  - `read_server_info(path) -> dict | None`
  - `remove_server_info(path) -> None`, best effort
  - `pid_is_alive(pid) -> bool | None`, `None` when undetermined

The version algorithm, stated once so both copies implement the same thing: sort the file list, and for each file feed `"<name>\0<size>\0<integer mtime>\n"` into a `hashlib.sha256`, then take the first 16 characters of the hexadecimal digest. The file list is every `*.py` under `statusline_lib/` plus the root entry points `statusline.py`, `subagent_statusline.py`, `qwen_statusline.py`, `kimi_statusline.py`, `statusline_client.py` and `statusline_server.py`, each named relative to the repository root with forward slashes. A file that cannot be stat'ed contributes `"<name>\0missing\n"`. Mtimes are truncated to whole seconds because Windows and Linux disagree about sub-second resolution on the same tree.

- [ ] **Step 1: Write the failing test**

Create `scripts/verify_server_info.py` with checks for: a stable digest across two calls on an unchanged tree, a changed digest after `os.utime` bumps one file's mtime by a second, a changed digest after a file grows, `read_server_info` returning `None` for a missing file and for malformed JSON, `write_server_info` producing a file that `read_server_info` round-trips, `remove_server_info` being safe on a missing path, `pid_is_alive(os.getpid())` being `True`, and `pid_is_alive` returning `False` for a pid that cannot exist. Force both arms of the psutil-versus-`os.kill` branch by patching the module's `_resolve_psutil` to return `None`, and force the Windows arm of any `os.name` branch by patching `os.name`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_info.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'statusline_lib.server_info'`.

- [ ] **Step 3: Write the implementation**

```python
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
```

`version_input_files(repository_root)` returns the sorted relative names described above and is exported so the client's copy can be diffed against it in Task 18. `write_server_info` uses the same pid-scoped temporary file plus `os.replace` pattern `ttlcache.write_ttl_cache` uses, so a client never reads a partial info file.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python scripts/verify_server_info.py`
Expected: `OK: server info file and code version verified`

- [ ] **Step 5: Commit**

```bash
python -m ruff format statusline_lib/server_info.py scripts/verify_server_info.py
python -m ruff check statusline_lib/server_info.py scripts/verify_server_info.py
git add statusline_lib/server_info.py scripts/verify_server_info.py
git commit -m "feat: server info file and code version digest"
```

### Task 12: Request handling

This is the whole server minus the socket. Every unit test in this task drives `handle_request` in-process against the fixture corpus builder, no sockets involved, which is what makes the server's behavior cheap to test.

**Files:**
- Create: `statusline_lib/server.py`
- Create: `scripts/verify_server_requests.py`

**Interfaces:**
- Consumes: `StateTables`, `housekeep_state_dir` (Tasks 8 to 10), `WorkerPool`, `set_refresh_sink` (Tasks 1 and 2), `render_claude_statusline`, `context_usage`, `transcript_path_for` (Task 5), `render_subagent_rows` (Task 6), `render_kimi_statusline`, `render_qwen_statusline` (existing).
- Produces:
  - `REQUEST_KINDS = ("claude", "subagent", "kimi", "qwen", "shutdown", "status")`
  - `class Server(state_directory, repository_root, clock=time.time, pool=None, tables=None, error_log_path=None)` with `handle_request(request: dict) -> str | None` and `stop_requested: bool`.
  - `last_render_path(session_id, state_directory) -> str`
  - `write_last_render(session_id, text, state_directory) -> None`

`handle_request` returns the reply text, or `None` when the client should fall back. `None` is returned for exactly two reasons: the request was a `shutdown` (nothing to say), or rendering raised. Both are logged.

- [ ] **Step 1: Write the failing test**

Create `scripts/verify_server_requests.py`. Build a synthetic home with `build_fixture_home` from `scripts/_render_fixture_helpers.py`, point `CLAUDE_STATE_DIR` at a temporary directory, construct a `Server` with an injected fake clock and a pool whose runner records instead of running, then:

```python
def check_every_render_kind_replies(failures):
    server = _server()
    for kind, payload in (
        ("claude", _claude_payload()),
        ("subagent", _subagent_payload()),
        ("kimi", _kimi_payload()),
        ("qwen", _qwen_payload()),
    ):
        reply = server.handle_request({"kind": kind, "payload": payload})
        if not reply:
            failures.append(f"the {kind} kind produced no reply")


def check_a_render_writes_the_client_fallback_file(failures):
    server = _server()
    reply = server.handle_request({"kind": "claude", "payload": _claude_payload()})
    path = last_render_path(_SESSION_ID, _STATE_DIR)
    with open(path, encoding="utf-8") as f:
        if f.read() != reply:
            failures.append("last-render file must hold exactly what was replied")


def check_a_render_writes_the_wrap_nudge_occupancy_file(failures):
    server = _server()
    server.handle_request({"kind": "claude", "payload": _claude_payload()})
    if read_ctx_used(_SESSION_ID, state_dir=_STATE_DIR) != 40110:
        failures.append("the server must keep writing the wrap-nudge occupancy state")


def check_a_raising_render_logs_and_replies_nothing(failures):
    """A server exception must never leave a client waiting: no reply is sent,
    the client falls through to its own fallback inside its own timeout."""
    server = _server()
    server._render_claude = _raise
    reply = server.handle_request({"kind": "claude", "payload": _claude_payload()})
    if reply is not None:
        failures.append("a failed render must reply nothing at all")
    with open(_ERROR_LOG, encoding="utf-8") as f:
        if "Traceback" not in f.read():
            failures.append("a failed render must log its traceback")


def check_an_unknown_kind_replies_nothing(failures):
    if _server().handle_request({"kind": "nonsense", "payload": {}}) is not None:
        failures.append("an unknown kind must reply nothing")


def check_shutdown_sets_the_stop_flag(failures):
    server = _server()
    reply = server.handle_request({"kind": "shutdown"})
    if not server.stop_requested:
        failures.append("shutdown must set stop_requested")
    if reply is not None:
        failures.append("shutdown replies nothing")


def check_status_replies_a_json_summary(failures):
    server = _server()
    server.handle_request({"kind": "claude", "payload": _claude_payload()})
    summary = json.loads(server.handle_request({"kind": "status"}))
    for key in ("uptime_seconds", "sessions", "cwds", "queue_depth", "version", "pid"):
        if key not in summary:
            failures.append(f"status reply is missing {key}")


def check_a_render_touches_the_cwd_table(failures):
    server = _server()
    server.handle_request({"kind": "claude", "payload": _claude_payload(cwd="/repo-a")})
    server.handle_request({"kind": "kimi", "payload": _kimi_payload(cwd="/repo-b")})
    summary = json.loads(server.handle_request({"kind": "status"}))
    if summary["cwds"] != 2:
        failures.append(f"two distinct cwds should be tracked, got {summary}")


def check_housekeeping_runs_at_most_hourly(failures):
    """Driven with the fake clock: two requests one minute apart sweep once,
    a third an hour later sweeps again."""
    clock = _FakeClock()
    sweeps = []
    server = _server(clock=clock)
    server._housekeeper = lambda directory, now: sweeps.append(now) or 0
    server.handle_request({"kind": "claude", "payload": _claude_payload()})
    clock.now += 60
    server.handle_request({"kind": "claude", "payload": _claude_payload()})
    if len(sweeps) != 1:
        failures.append(f"housekeeping ran {len(sweeps)} times in one minute")
    clock.now += 3601
    server.handle_request({"kind": "claude", "payload": _claude_payload()})
    if len(sweeps) != 2:
        failures.append(f"housekeeping should have run twice by now: {sweeps}")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_requests.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'statusline_lib.server'`.

- [ ] **Step 3: Write the implementation**

Create `statusline_lib/server.py`. This task writes only the request half; Task 13 adds the socket half to the same file. Keep the file under 400 lines: if it approaches that, move `_render_claude`, `_render_subagent`, `_render_kimi` and `_render_qwen` into a new `statusline_lib/server_render.py` and have `Server` hold them as a dispatch table.

```python
class Server:
    """The resident render server for one app_dir().

    Renders are pure formatting over the payload plus in-memory state, so they
    are served inline on the receive loop and reply in single-digit
    milliseconds. Anything that can block (git, psutil, HTTP, the walker) runs
    on the worker pool and never on the receive path. That split is the whole
    design: the pool has a hard ceiling, the receive loop never waits on it,
    and a wedged refresher can therefore slow a field, never a render.
    """

    def __init__(
        self,
        state_directory,
        repository_root,
        *,
        clock=time.time,
        pool=None,
        tables=None,
        error_log_path=None,
    ):
        self._state_directory = state_directory
        self._repository_root = repository_root
        self._clock = clock
        self._tables = tables or StateTables(clock=clock)
        self._pool = pool or WorkerPool(error_logger=self._log_job_error)
        self._error_log_path = error_log_path or os.path.join(
            app_dir(), ".statusline-error.log"
        )
        self._started_at = clock()
        self._last_housekeeping = 0.0
        self._last_request_at = self._started_at
        self.stop_requested = False

    def handle_request(self, request):
        """Reply text for one request, or None when the client should fall
        back to its own last-render file."""
        self._last_request_at = self._clock()
        kind = request.get("kind")
        if kind == "shutdown":
            self.stop_requested = True
            return None
        try:
            self._maybe_housekeep()
            if kind == "status":
                return json.dumps(self._status())
            handler = self._HANDLERS.get(kind)
            if handler is None:
                return None
            return handler(self, request.get("payload") or {})
        except Exception:
            log_traceback(self._error_log_path)
            return None
```

`_render_claude` is the piece worth spelling out, because it is where the state tables meet the extracted render:

```python
    def _render_claude(self, payload):
        session_id = payload.get("session_id") or payload.get("conversation_id") or ""
        workspace = payload.get("workspace") or {}
        cwd = workspace.get("current_dir") or payload.get("cwd") or ""
        entry = self._tables.touch_session(session_id, transcript_path_for(payload))
        walk = self._tables.walk_for(entry)
        self._tables.touch_cwd(cwd)
        now = self._clock()
        context_used, window_size = context_usage(payload)
        write_ctx_state(
            session_id, context_used, window_size, now, state_dir=self._state_directory
        )
        text = render_claude_statusline(
            payload, cwd, walk, now, state_dir=self._state_directory
        )
        write_last_render(session_id, text, self._state_directory)
        return text
```

The constructor also stores `self._housekeeper = housekeep_state_dir`, so a test can swap the sweep for a recorder and drive the hourly schedule with a fake clock instead of touching the real state directory.

`_render_subagent` joins `render_subagent_rows(payload, self._clock())` with newlines. `_render_kimi` and `_render_qwen` call the existing adapters (`render_kimi_statusline`, `render_qwen_statusline`) with `spinner_frame()`, touch the cwd table, and write the last-render file the same way. `_maybe_housekeep` calls `housekeep_state_dir` and `self._tables.drop_idle()` when `self._clock() - self._last_housekeeping >= HOUSEKEEPING_INTERVAL_SECONDS`.

Install the pool as the refresh sink in the constructor so the eight cache readers reach it:

```python
        set_refresh_sink(self._pool.submit)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python scripts/verify_server_requests.py`
Expected: `OK: server request handling verified`

- [ ] **Step 5: Commit**

```bash
python -m ruff format statusline_lib/server.py scripts/verify_server_requests.py
python -m ruff check statusline_lib/server.py scripts/verify_server_requests.py
wc -l statusline_lib/server.py
git add statusline_lib/server.py scripts/verify_server_requests.py
git commit -m "feat: resident server request handling"
```

### Task 13: The socket loop

**Files:**
- Modify: `statusline_lib/server.py`
- Create: `statusline_server.py`
- Modify: `scripts/verify_server_requests.py`
- Modify: `.gitea/workflows/ci.yml` (both `py_compile` steps)

**Interfaces:**
- Consumes: `Server.handle_request` from Task 12, `server_info` helpers from Task 11.
- Produces:
  - `IDLE_EXIT_SECONDS = 600`, `RECEIVE_POLL_SECONDS = 0.5`, `MAXIMUM_DATAGRAM_BYTES = 65535`, `RECEIVE_BUFFER_BYTES = 1 << 20`
  - `Server.bind() -> int` returning the bound port
  - `Server.serve_forever() -> None`
  - `serve(argv=None) -> int`, the entry point body

- [ ] **Step 1: Write the failing test**

Append to `scripts/verify_server_requests.py`:

```python
def check_bind_writes_the_info_file(failures):
    server = _server()
    port = server.bind()
    try:
        info = read_server_info(server_info_path(_STATE_DIR))
        if info is None:
            failures.append("bind must write server.json")
        elif info["port"] != port or info["pid"] != os.getpid():
            failures.append(f"server.json disagrees with the process: {info}")
    finally:
        server.close()


def check_close_removes_the_info_file(failures):
    server = _server()
    server.bind()
    server.close()
    if read_server_info(server_info_path(_STATE_DIR)) is not None:
        failures.append("a clean exit must remove server.json")


def check_bind_sets_the_windows_exclusive_option(failures):
    """SO_EXCLUSIVEADDRUSE is Windows-only; force the arm on Linux by
    patching os.name, per the repo's platform-branch coverage rule."""
    saved = os.name
    os.name = "nt"
    try:
        server = _server()
        server.bind()
        server.close()
    except OSError:
        pass  # the option is absent off Windows; the branch was still taken
    finally:
        os.name = saved


def check_serve_forever_exits_when_idle(failures):
    """Driven with the fake clock: no request for the idle window ends the
    loop. Nothing here waits on the wall clock."""
    clock = _FakeClock()
    server = _server(clock=clock)
    server.bind()
    clock.now += 601
    server.serve_forever()
    if not server.stop_requested:
        failures.append("an idle server must stop itself")


def check_serve_forever_exits_on_shutdown(failures):
    server = _server()
    port = server.bind()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _send(port, {"kind": "shutdown"})
    thread.join(timeout=10)
    if thread.is_alive():
        failures.append("serve_forever did not return after a shutdown datagram")
    server.close()


def check_a_malformed_datagram_is_ignored(failures):
    """Bytes that are not JSON must be dropped without a reply and without
    taking the loop down."""
    server = _server()
    port = server.bind()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _send_bytes(port, b"not json at all")
        _send_bytes(port, b'"a bare string"')
        reply = _send_and_receive(port, {"kind": "status"})
        if not reply:
            failures.append("the loop stopped answering after a malformed datagram")
    finally:
        _send(port, {"kind": "shutdown"})
        thread.join(timeout=10)
        server.close()
```

`_send`, `_send_bytes` and `_send_and_receive` are three-line helpers over a UDP socket bound to nothing, with a generous receive timeout. They exist so the socket checks do not reach for the client, which is Task 14's subject.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_requests.py`
Expected: FAIL with `AttributeError: 'Server' object has no attribute 'bind'`.

- [ ] **Step 3: Write the implementation**

```python
    def bind(self):
        """Bind a random localhost UDP port and publish server.json.

        SO_EXCLUSIVEADDRUSE on Windows: without it a second server can bind
        the same port and silently steal half the datagrams. The receive
        buffer is raised well above the default because a burst of concurrent
        renders arrives as a burst of datagrams, and a datagram the kernel
        drops costs that client its reply (it falls back, which is correct but
        worse than a real render).
        """
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if os.name == "nt":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                self._socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        with contextlib.suppress(OSError):
            self._socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER_BYTES
            )
        self._socket.bind(("127.0.0.1", 0))
        self._socket.settimeout(RECEIVE_POLL_SECONDS)
        self._port = self._socket.getsockname()[1]
        self._pool.start()
        write_server_info(
            server_info_path(self._state_directory),
            pid=os.getpid(),
            port=self._port,
            version=code_version(self._repository_root),
            started_at=self._started_at,
            platform=platform_name() or "claude",
        )
        return self._port

    def serve_forever(self):
        """Receive, handle, reply, until shutdown or the idle window passes.

        The timeout on the socket is what makes the idle check work without a
        sleep anywhere: every RECEIVE_POLL_SECONDS the loop either has a
        datagram or gets a chance to notice it has been idle for ten minutes.
        """
        while not self.stop_requested:
            try:
                data, address = self._socket.recvfrom(MAXIMUM_DATAGRAM_BYTES)
            except socket.timeout:
                if self._clock() - self._last_request_at >= self._idle_exit_seconds:
                    self.stop_requested = True
                continue
            except OSError:
                log_traceback(self._error_log_path)
                continue
            try:
                request = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(request, dict):
                continue
            reply = self.handle_request(request)
            if reply is None:
                continue
            with contextlib.suppress(OSError):
                self._socket.sendto(reply.encode("utf-8"), address)
```

`serve(argv=None)` resolves the state directory and repository root, constructs a `Server`, binds, runs one housekeeping sweep immediately (the spec's "on start and hourly", and the reason roughly 2,000 stale beacon caches are still on disk), calls `serve_forever` in a `try` whose `finally` calls `close()` (which stops the pool and removes the info file), and returns 0.

`_idle_exit_seconds` reads `pref("STATUSLINE_SERVER_IDLE_SECONDS")` with `IDLE_EXIT_SECONDS` as the default, so a test can shrink it. Same treatment for the pool size via `STATUSLINE_SERVER_WORKERS`.

Create `statusline_server.py`:

```python
"""Resident statusline server entry point.

Thin shim, same shape as qwen_statusline.py: one process per app_dir(),
started by statusline_client.py when it finds no live server. All logic lives
in statusline_lib/server.py. Do not rename this file: statusline_client.py
spawns it by its literal path.
"""

import sys

from statusline_lib.server import serve

if __name__ == "__main__":
    sys.exit(serve(sys.argv[1:]))
```

Add `statusline_client.py` and `statusline_server.py` to both `py_compile` steps in `.gitea/workflows/ci.yml`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python scripts/verify_server_requests.py`
Expected: `OK: server request handling verified`

- [ ] **Step 5: Confirm coverage and size**

```bash
python -m coverage erase
for t in scripts/verify_*.py; do python -m coverage run -a "$t" || echo "FAILED $t"; done
python -m coverage report -m --include="statusline_lib/*" --fail-under=100
wc -l statusline_lib/server.py
aislop ci .
```
Expected: 100 percent, `server.py` under 400 lines, aislop exit 0. If `server.py` is over 400 lines, split the four `_render_*` methods into `statusline_lib/server_render.py` now, before committing.

- [ ] **Step 6: Commit**

```bash
python -m ruff format .
python -m ruff check .
git add -A
git commit -m "feat: resident server socket loop and entry point"
```

---

## Phase 5: The client

`statusline_client.py` lives at the repository root as entry-point glue, so it is outside the measured coverage scope. That is not a loophole, it is the only place it can live: the file must not import `statusline_lib` on its hot path, and a do-nothing Python client already costs about 65 ms end to end on this machine. Its tests are therefore black-box, in `scripts/verify_server_protocol.py`, which runs a real server on a real port and the real client as a subprocess. The tasks below each grow that one script.

The single exception to the no-`statusline_lib` rule is a function-local import of `statusline_lib.process_safe` inside the spawn helper, on the fallback path only. `process_safe` is the repository's only sanctioned subprocess surface, and paying an import there costs nothing: the render has already been printed. Task 18's static check enforces exactly this shape.

### Task 14: The client hot path

**Files:**
- Create: `statusline_client.py`
- Create: `scripts/verify_server_protocol.py`

**Interfaces:**
- Consumes: the wire format from Task 13. A request is one UTF-8 JSON datagram `{"kind": <kind>, "payload": <payload>, "version": <version>}`; a reply is one UTF-8 datagram of rendered text, printed verbatim.
- Produces:
  - `CLIENT_TIMEOUT_SECONDS = 0.150`
  - `main(kind, argv=None) -> int`, the function every entry wrapper calls
  - `application_directory() -> str` and `state_directory() -> str`, the client's own standard-library copies of `base.app_dir` and `base.state_dir`
  - `request_render(kind, payload, info) -> tuple[str | None, bool]`, the reply and whether the failure was a connection reset
  - `_socket_factory = socket.socket`, a module-level seam so a test can inject a socket that raises

- [ ] **Step 1: Write the failing test**

Create `scripts/verify_server_protocol.py`:

```python
"""Verify the wire protocol between statusline_client.py and the resident
server: a real server on a real port, the real client as a subprocess.

The client is entry-point glue (outside the coverage gate by AGENTS.md), so
this script is where its behavior is pinned. Nothing here asserts on elapsed
time; the client's own timeout is shortened through the prefs file where a
test needs it to expire.

Run from anywhere; imports from agent-statusline by path.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _render_fixture_helpers import _REPO, build_fixture_home

from statusline_lib.server import Server
from statusline_lib.server_info import read_server_info, server_info_path


def check_a_live_server_answers_the_client(failures):
    with _running_server() as context:
        result = _run_client(context, "claude", _claude_payload(context))
    if result.returncode != 0:
        failures.append(f"client exited {result.returncode}: {result.stderr!r}")
    if not result.stdout.strip():
        failures.append("client printed nothing against a live server")
    if "STATUSLINE ERROR" in result.stdout:
        failures.append(f"client printed an error line: {result.stdout!r}")


def check_the_client_prints_the_reply_verbatim(failures):
    """Whatever the server sends is what appears on stdout, byte for byte:
    the client is not allowed to reformat, wrap, or trim a reply."""
    with _running_server(reply="line one\nline two") as context:
        result = _run_client(context, "claude", _claude_payload(context))
    if result.stdout != "line one\nline two":
        failures.append(f"reply was not printed verbatim: {result.stdout!r}")


def check_the_subagent_kind_round_trips(failures):
    with _running_server() as context:
        result = _run_client(context, "subagent", _subagent_payload(context))
    for line in result.stdout.splitlines():
        try:
            json.loads(line)
        except ValueError:
            failures.append(f"subagent output line is not JSON: {line!r}")


def check_the_client_resolves_the_same_directories_as_the_package(failures):
    """The client carries its own copies of app_dir() and state_dir(). If the
    two ever drift, the client looks for server.json somewhere the server
    never writes it, and every render silently falls back forever."""
    from statusline_lib.base import app_dir, state_dir

    for environment in _DIRECTORY_CASES:
        printed = subprocess.run(
            [sys.executable, os.path.join(_REPO, "statusline_client.py"),
             "--print-directories"],
            capture_output=True, text=True, timeout=30, env=environment,
        ).stdout.strip().splitlines()
        with _patched_environment(environment):
            expected = [app_dir(), state_dir()]
        if printed != expected:
            failures.append(
                f"client resolved {printed}, the package resolves {expected}"
            )
```

`_DIRECTORY_CASES` covers the four platform values (`claude`, `qwen`, `kimi`, `antigravity`) set through `STATUSLINE_PLATFORM`, the same four set through the `--statusline-platform` argv flag, and one case each for `CLAUDE_STATE_DIR` and `ANTIGRAVITY_STATE_DIR`. Add `--print-directories` to the client alongside `--print-version`.

`_running_server()` is a context manager that builds a fixture home, points `HOME`, `USERPROFILE` and `CLAUDE_STATE_DIR` at it, constructs a `Server`, binds it, runs `serve_forever` on a daemon thread, yields the context, then sends a `shutdown` datagram and joins. `_run_client(context, kind, payload)` runs `[sys.executable, os.path.join(_REPO, "statusline_client.py"), "--kind", kind]` with the payload on stdin and the context's environment, capturing output with a generous hard timeout.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_protocol.py`
Expected: FAIL with the client subprocess exiting non-zero, `can't open file 'statusline_client.py'`.

- [ ] **Step 3: Write the implementation**

Create `statusline_client.py` with the hot path only. Fallback and spawning arrive in the next two tasks; for now a missing or silent server prints nothing and exits 0.

```python
"""The statusline client: one datagram, one reply, print it, exit.

Every installed statusline command is a wrapper around this file. It reads the
harness payload from stdin, sends it to the resident server named in
state_dir()/server.json, waits at most CLIENT_TIMEOUT_SECONDS for one reply,
and prints that reply verbatim.

Standard library only, and deliberately no imports from statusline_lib on the
hot path: importing the package costs more than everything this file does. The
duplication of app_dir() and state_dir() below is the price of that, and it is
covered by scripts/verify_server_protocol.py, which asserts the client and the
package resolve the same directories for the same environment.

Do not rename this file: install.py writes its literal path into the SessionStart
hook, and the four entry wrappers import it by name.
"""
```

The hot path, in the order the spec fixes:

```python
def main(kind, argv=None):
    argv = sys.argv[1:] if argv is None else argv
    payload_text = sys.stdin.read()
    try:
        payload = json.loads(payload_text)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    info = read_server_info(server_info_path())
    if info is not None:
        reply, reset = request_render(kind, payload, info)
        if reply is not None:
            sys.stdout.write(reply)
            return 0
    return 0  # fallback lands here in Task 15
```

```python
def request_render(kind, payload, info):
    """Send one datagram, wait for one reply. Returns (reply, saw_reset).

    ConnectionResetError is the Windows shape of a dead port: the send
    succeeds, the ICMP port-unreachable comes back, and the NEXT receive on
    that socket raises. On Linux the same situation is an ordinary timeout,
    which is indistinguishable from a server that is merely busy. So the reset
    is reported separately: it is the one signal that proves the port is dead
    and a replacement should be spawned.
    """
    port = info.get("port")
    if not isinstance(port, int):
        return None, False
    request = json.dumps(
        {"kind": kind, "payload": payload, "version": info.get("version")}
    )
    sock = _socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(_client_timeout_seconds())
        sock.sendto(request.encode("utf-8"), ("127.0.0.1", port))
        data = sock.recv(MAXIMUM_DATAGRAM_BYTES)
    except ConnectionResetError:
        return None, True
    except (OSError, ValueError):
        return None, False
    finally:
        sock.close()
    return data.decode("utf-8", "replace"), False
```

`ConnectionResetError` is a subclass of `OSError`, so the reset arm must come first. The test that forces it on Linux replaces `_socket_factory`; see Task 16. The client's own copies of `app_dir()` and `state_dir()` reproduce `statusline_lib/base.py`'s precedence exactly, including the `--statusline-platform` argv flag and the `STATUSLINE_PLATFORM`, `CLAUDE_STATE_DIR` and `ANTIGRAVITY_STATE_DIR` environment variables. Its `read_server_info`, `server_info_path` and `code_version` are standard-library copies of `statusline_lib/server_info.py`'s, following the algorithm pinned in Task 11.

`_client_timeout_seconds()` reads `STATUSLINE_CLIENT_TIMEOUT_MS` through the client's own ten-line prefs reader (the prefs file first, then the environment, then the default), mirroring `statusline_lib/prefs.py`'s precedence so a test can shorten the timeout without a restart.

Add `--kind <name>` argument parsing so the verify script can drive any kind from one file.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python scripts/verify_server_protocol.py`
Expected: `OK: client and server protocol verified`

- [ ] **Step 5: Commit**

```bash
python -m ruff format statusline_client.py scripts/verify_server_protocol.py
python -m ruff check statusline_client.py scripts/verify_server_protocol.py
git add statusline_client.py scripts/verify_server_protocol.py
git commit -m "feat: statusline client hot path"
```

### Task 15: The client fallback

The user never sees a blank line and never waits longer than the timeout. That is the whole contract of this task.

**Files:**
- Modify: `statusline_client.py`
- Modify: `scripts/verify_server_protocol.py`

**Interfaces:**
- Consumes: `request_render` from Task 14, `write_last_render` from Task 12.
- Produces:
  - `FALLBACK_MAXIMUM_AGE_SECONDS = 30`
  - `fallback_text(kind, payload) -> str`
  - `minimal_line(payload) -> str`
  - `last_render_path(session_id) -> str`, the client's own copy of Task 12's function, resolving the state directory through the client's own `state_directory()`

- [ ] **Step 1: Write the failing test**

Append to `scripts/verify_server_protocol.py`:

```python
def check_a_timeout_falls_back_to_the_last_render_file(failures):
    """A server that never answers must cost the client its timeout and
    nothing else: it prints the recent last-render file instead."""
    with _silent_server() as context:
        _write_last_render(context, "cached line from the last render")
        result = _run_client(context, "claude", _claude_payload(context))
    if result.stdout.strip() != "cached line from the last render":
        failures.append(f"expected the cached line, got {result.stdout!r}")
    if result.returncode != 0:
        failures.append("a fallback must still exit 0")


def check_a_stale_last_render_file_is_not_used(failures):
    with _silent_server() as context:
        _write_last_render(context, "ancient line", age_seconds=120)
        result = _run_client(context, "claude", _claude_payload(context))
    if "ancient line" in result.stdout:
        failures.append("a last-render file older than 30s must not be printed")
    if not result.stdout.strip():
        failures.append("the minimal line must never be empty")


def check_the_minimal_line_carries_the_payload_basics(failures):
    with _silent_server() as context:
        result = _run_client(context, "claude", _claude_payload(context))
    for fragment in ("Opus", "%"):
        if fragment not in result.stdout:
            failures.append(f"minimal line is missing {fragment!r}: {result.stdout!r}")


def check_no_server_file_at_all_still_prints_a_line(failures):
    with _no_server() as context:
        result = _run_client(context, "claude", _claude_payload(context))
    if not result.stdout.strip():
        failures.append("a missing server.json must still produce a line")


def check_a_malformed_server_file_still_prints_a_line(failures):
    with _no_server() as context:
        with open(server_info_path(context.state), "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        result = _run_client(context, "claude", _claude_payload(context))
    if not result.stdout.strip():
        failures.append("a corrupt server.json must still produce a line")
    if result.returncode != 0:
        failures.append("a corrupt server.json must not make the client exit non-zero")


def check_the_kimi_kind_prints_exactly_one_line_on_fallback(failures):
    """Kimi's TUI renders only the first stdout line and requires it to be
    non-empty; a multi-line fallback there would be a regression."""
    with _silent_server() as context:
        _write_last_render(context, "first line\nsecond line")
        result = _run_client(context, "kimi", _kimi_payload(context))
    if result.stdout.count("\n") > 0:
        failures.append(f"the kimi fallback must be one line: {result.stdout!r}")
    if not result.stdout.strip():
        failures.append("the kimi fallback line must never be empty")
```

`_silent_server()` writes a `server.json` pointing at a bound but never-read socket, so the client's send succeeds and the receive times out. `_write_last_render` writes the file `write_last_render` produces and pins its mtime with `os.utime`; the age is synthetic, never a real wait.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_protocol.py`
Expected: FAIL, the fallback checks print nothing on stdout.

- [ ] **Step 3: Write the implementation**

```python
def fallback_text(kind, payload):
    """What to print when the server did not answer.

    A recent last-render file is a real render that is at most a few seconds
    old, which beats anything this process could compute. Past
    FALLBACK_MAXIMUM_AGE_SECONDS it is worse than honest, so a minimal line
    built from the payload alone takes over. Both are single-line safe, which
    Kimi's TUI requires.
    """
    session_id = payload.get("session_id") or payload.get("conversation_id") or ""
    if session_id:
        path = last_render_path(session_id)
        try:
            age = time.time() - os.path.getmtime(path)
            if 0 <= age < _fallback_maximum_age_seconds():
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                if text.strip():
                    return text.splitlines()[0] if kind == "kimi" else text
        except OSError:
            pass
    return minimal_line(payload)


def minimal_line(payload):
    """Model, context percent and the current directory's basename, from the
    payload alone. No file reads, no subprocesses, no imports: this is the
    line that must be printable when everything else has failed."""
```

`minimal_line` reads `model.display_name` (falling back to `model.id` and then to the camelCase `model` key Kimi uses), derives the context percentage from `context_window` when present, and appends `os.path.basename` of `workspace.current_dir` or `cwd`. Every lookup is a `.get` chain that degrades to an empty string, and the function returns at least the directory basename so it can never be blank.

Wire it into `main`: after the `request_render` attempt returns `None`, or when `read_server_info` returned `None`, write `fallback_text(kind, payload)` to stdout, then run the liveness check that Task 16 adds.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python scripts/verify_server_protocol.py`
Expected: `OK: client and server protocol verified`

- [ ] **Step 5: Commit**

```bash
python -m ruff format statusline_client.py scripts/verify_server_protocol.py
python -m ruff check statusline_client.py scripts/verify_server_protocol.py
git add statusline_client.py scripts/verify_server_protocol.py
git commit -m "feat: client fallback line and last-render reuse"
```

### Task 16: Liveness, single-flight spawn, and version check

**Files:**
- Modify: `statusline_client.py`
- Modify: `scripts/verify_server_protocol.py`

**Interfaces:**
- Consumes: `fallback_text` from Task 15, `code_version` semantics from Task 11.
- Produces:
  - `SPAWN_LOCK_STALE_SECONDS = 10`
  - `ensure_server(info, reason) -> bool`, True when this process spawned one
  - `claim_spawn_lock() -> bool`
  - `spawn_lock_path() -> str`, `state_directory()/server.spawn.lock`
  - `classify_failure(info, *, reset) -> str`, one of `"missing"`, `"dead"`, `"version"`, `"silent"`
  - the `--ensure-server` flag, which performs only the liveness and version check and spawn, reads no stdin, and prints nothing
  - the `--print-version` and `--print-directories` flags, which exist only so the duplication checks in Tasks 14 and 16 have something to compare against

- [ ] **Step 1: Write the failing test**

```python
def check_a_dead_server_is_replaced_exactly_once(failures):
    """Two clients racing against the same dead server must leave exactly one
    replacement. This is the single-flight lock, and it is the difference
    between this design and the one it replaced."""
    with _dead_server() as context:
        results = _run_clients_in_parallel(context, count=8)
    servers = _count_running_servers(context)
    if servers != 1:
        failures.append(f"{servers} servers were spawned, expected exactly 1")
    for result in results:
        if not result.stdout.strip():
            failures.append("every racing client must still print its fallback")


def check_a_stale_spawn_lock_is_replaced(failures):
    with _dead_server() as context:
        _write_spawn_lock(context, pid=999999, age_seconds=30)
        _run_client(context, "claude", _claude_payload(context))
    if _count_running_servers(context) != 1:
        failures.append("a lock older than 10s must not block a spawn forever")


def check_a_fresh_spawn_lock_blocks_a_second_spawn(failures):
    with _dead_server() as context:
        _write_spawn_lock(context, pid=os.getpid(), age_seconds=0)
        _run_client(context, "claude", _claude_payload(context))
    if _count_running_servers(context) != 0:
        failures.append("a fresh lock must suppress a duplicate spawn")


def check_a_version_mismatch_shuts_the_old_server_down(failures):
    with _running_server() as context:
        _rewrite_server_info(context, version="0000000000000000")
        _run_client(context, "claude", _claude_payload(context))
        if not _server_stopped(context):
            failures.append("a stale-version server must be told to shut down")


def check_the_client_and_the_package_agree_on_the_version(failures):
    """The client carries its own copy of the version algorithm. If the two
    ever drift, every render pays a shutdown and a respawn, forever."""
    from statusline_lib.server_info import code_version, version_input_files

    client_version = subprocess.run(
        [sys.executable, os.path.join(_REPO, "statusline_client.py"), "--print-version"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    if client_version != code_version(_REPO):
        failures.append("client and server compute different code versions")


def check_ensure_server_reads_no_stdin_and_prints_nothing(failures):
    with _no_server() as context:
        result = _run_client_with_arguments(context, ["--ensure-server"], stdin=None)
    if result.stdout.strip():
        failures.append(f"--ensure-server must print nothing: {result.stdout!r}")
    if _count_running_servers(context) != 1:
        failures.append("--ensure-server must start a server when none is live")


def check_the_windows_connection_reset_branch(failures):
    """ConnectionResetError is how Windows reports a dead port on the next
    receive; on Linux the same case times out. Force the Windows arm here by
    patching os.name inside an in-process call, per the repo convention."""
    import statusline_client

    class _ResettingSocket:
        def __init__(self, *arguments):
            pass

        def settimeout(self, seconds):
            pass

        def sendto(self, data, address):
            return len(data)

        def recv(self, size):
            raise ConnectionResetError(10054, "forcibly closed by the remote host")

        def close(self):
            pass

    saved_name = os.name
    saved_factory = statusline_client._socket_factory
    os.name = "nt"
    statusline_client._socket_factory = _ResettingSocket
    try:
        info = {"port": 1, "pid": 999999, "version": "0" * 16}
        if statusline_client.request_render("claude", {}, info) is not None:
            failures.append("a reset connection must produce no reply")
        if statusline_client.classify_failure(info, reset=True) != "dead":
            failures.append("ConnectionResetError must classify the server as dead")
    finally:
        os.name = saved_name
        statusline_client._socket_factory = saved_factory
```

This is why `request_render` reports whether it saw a reset rather than only returning `None`: on Linux a dead port and a busy server both look like a timeout, and only the reset distinguishes them. Give the client a module-level `_socket_factory = socket.socket` seam and a `classify_failure(info, *, reset)` helper returning `"missing"`, `"dead"`, `"version"` or `"silent"`, so both arms are reachable on either operating system.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_protocol.py`
Expected: FAIL, zero servers spawned in the racing check.

- [ ] **Step 3: Write the implementation**

```python
def claim_spawn_lock():
    """True when this process may spawn the server.

    O_EXCL creation is the whole mechanism: fifty clients discovering the same
    dead server race to create one file, and exactly one wins. A lock whose
    writer died leaves the file behind, so a lock older than
    SPAWN_LOCK_STALE_SECONDS is replaced rather than obeyed; ten seconds is
    far longer than a spawn takes and far shorter than a user notices.
    """
    path = spawn_lock_path()
    payload = json.dumps({"pid": os.getpid(), "at": time.time()}).encode("utf-8")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not _lock_is_stale(path):
            return False
        with contextlib.suppress(OSError):
            os.remove(path)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
    except OSError:
        return False
    try:
        os.write(handle, payload)
    finally:
        os.close(handle)
    return True


def ensure_server(info, reason):
    """Spawn a replacement server when the current one is dead or stale.

    Dead means: no server.json, its pid is gone, or the request raised
    ConnectionResetError. Stale means: its recorded version does not match
    this checkout's. A stale server is asked to shut down first, so a
    fast-forward of the live checkout takes effect on the next render and a
    half-edited checkout crashes the NEW server rather than the running one.
    This never waits for the result: the render has already been printed.
    """
    if reason == "version" and info is not None:
        _send_shutdown(info)
    if not claim_spawn_lock():
        return False
    from statusline_lib.process_safe import spawn_detached

    environment = dict(os.environ)
    platform = _platform_name()
    if platform:
        environment["STATUSLINE_PLATFORM"] = platform
    with contextlib.suppress(OSError):
        spawn_detached(
            [sys.executable, os.path.join(_repository_root(), "statusline_server.py")],
            env=environment,
        )
    return True
```

The `STATUSLINE_PLATFORM` pin is the same fix the deleted `refresh._child_snippet` carried, and for the same reason: the spawned server's own argv carries no `--statusline-platform` flag, so without the pin a Kimi or Qwen client would start a server that resolves `app_dir()` to `~/.claude` and writes its caches where that harness never reads them.

The spawn lock is removed by the new server once it has bound, so the next client sees a live server rather than a lock. Add that removal to `Server.bind()`.

Wire the liveness decision into `main`: after printing the fallback, classify the failure as `"missing"`, `"dead"`, `"version"` or `"silent"`, and call `ensure_server` for everything except `"silent"`. A silent-but-alive server is busy, not broken, and spawning a second one would be the exact failure this design exists to prevent.

Add `--ensure-server` (liveness path only, no stdin, no output) and `--print-version` (prints the computed version and exits, so the agreement check has something to compare).

- [ ] **Step 4: Run the test to verify it passes**

Run: `python scripts/verify_server_protocol.py`
Expected: `OK: client and server protocol verified`

- [ ] **Step 5: Commit**

```bash
python -m ruff format .
python -m ruff check .
git add -A
git commit -m "feat: client liveness check, single-flight spawn, and version gate"
```

### Task 17: The entry wrappers

**Files:**
- Modify: `statusline.py`, `subagent_statusline.py`, `kimi_statusline.py`, `qwen_statusline.py`
- Modify: `scripts/verify_server_protocol.py`
- Modify: `scripts/verify_kimi_statusline_entry.py`, `scripts/verify_qwen_statusline_entry.py`

**Interfaces:**
- Consumes: `statusline_client.main(kind, argv)` from Tasks 14 to 16.
- Produces: no new symbols. Each of the four installed commands keeps its literal path and its existing `--statusline-platform` argv handling, and now forwards to the client with a render kind.

- [ ] **Step 1: Write the failing test**

Append to `scripts/verify_server_protocol.py` a check that runs each of the four entry points as a subprocess against a live server and asserts each prints a non-empty first line and exits 0. Extend `scripts/verify_kimi_statusline_entry.py` and `scripts/verify_qwen_statusline_entry.py` to assert their shim still injects `--statusline-platform` before anything else, since `app_dir()` resolution still depends on it.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_protocol.py`
Expected: FAIL, the entry points still render in-process rather than through the client.

- [ ] **Step 3: Write the implementation**

`statusline.py` keeps its docstring (updated to describe the client wrapper), its `sys.stdout.reconfigure` call, and nothing else:

```python
import sys

import statusline_client

if __name__ == "__main__":
    sys.exit(statusline_client.main("claude"))
```

`subagent_statusline.py` does the same with `"subagent"`. `kimi_statusline.py` and `qwen_statusline.py` keep their existing argv injection block and their `aislop-ignore-next-line hallucinated-import` directive (retargeted at `import statusline_client`), then forward with `"kimi"` and `"qwen"`.

Delete each wrapper's `record_render`, `_log_error` and slow-render block: those measured a render that no longer happens in this process. The server records render timings now.

The Claude branch of the old `main()`, along with `_render_qwen`, `_render_kimi`, `_INPUT_LOG`, `_ERROR_LOG`, `_log_error`, `_log_slow_render` and `_SLOW_RENDER_SECONDS`, is deleted from `statusline.py`. The payload dump the input log provided moves to the server, which writes it in `handle_request` before dispatching.

- [ ] **Step 4: Run the tests**

```bash
python scripts/verify_server_protocol.py
python scripts/verify_kimi_statusline_entry.py
python scripts/verify_qwen_statusline_entry.py
python scripts/verify_statusline_agy_dispatch.py
```
Expected: every one prints its `OK:` line.

- [ ] **Step 5: Confirm the whole suite and the gates**

```bash
python -m coverage erase
for t in scripts/verify_*.py; do python -m coverage run -a "$t" || echo "FAILED $t"; done
python -m coverage report -m --include="statusline_lib/*" --fail-under=100
aislop ci .
wc -l statusline.py subagent_statusline.py
```
Expected: 100 percent coverage, aislop exit 0, both entry points under 60 lines.

- [ ] **Step 6: Commit**

```bash
python -m ruff format .
python -m ruff check .
git add -A
git commit -m "feat: route every entry point through the statusline client"
```

---

## Phase 6: Budget and regression

### Task 18: Retarget the render-budget invariant

`scripts/verify_render_budget.py` exists because four production incidents shared one disease: a synchronous call inside a render that can block for seconds. In the server model the render path is `statusline_client.py` and nothing else, so the static check moves there. The subprocess-timeout scan over `statusline_lib` stays: the refreshers still shell out to git and the walker, and a two second cap on those is still the right bar now that they run on a four thread pool.

**Files:**
- Modify: `scripts/verify_render_budget.py`

**Interfaces:**
- Consumes: the client from Tasks 14 to 16, the server from Task 13.
- Produces: `check_client_is_import_free`, `check_client_sends_one_datagram`, `check_live_server_render_budget`; the cold and warm-core checks are replaced.

- [ ] **Step 1: Write the failing checks**

```python
_CLIENT = os.path.join(_REPO, "statusline_client.py")

# The client's entire budget. Its own socket timeout is 150ms, the interpreter
# and import floor is roughly 65ms on this machine, and Kimi kills the process
# tree at 300ms. 200ms leaves the client no room to grow a second file read.
_CLIENT_BUDGET_MS = float(os.environ.get("STATUSLINE_TEST_CLIENT_BUDGET_MS", "200"))


def check_client_is_import_free(failures):
    """The client must not import statusline_lib on its hot path. Importing
    the package costs several times everything else the client does, which is
    the entire reason this file exists as a separate entry point.

    One exception, asserted rather than merely allowed: the function-local
    import of statusline_lib.process_safe inside the spawn helper. That runs
    only on the fallback path, after the line has already been printed, and
    process_safe is the repository's only sanctioned subprocess surface.
    """
    tree = ast.parse(open(_CLIENT, encoding=_TEXT_ENCODING).read(), filename=_CLIENT)
    allowed_function = "ensure_server"
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        module = getattr(node, "module", "") or ""
        names = module.split(".") + [alias.name.split(".")[0] for alias in node.names]
        if "statusline_lib" not in names and "subprocess" not in names:
            continue
        enclosing = _enclosing_function_name(tree, node)
        if module != "statusline_lib.process_safe" or enclosing != allowed_function:
            failures.append(
                f"statusline_client.py:{node.lineno}: only a"
                f" statusline_lib.process_safe import inside {allowed_function}()"
                " is allowed"
            )


def check_client_sends_one_datagram(failures):
    """One send, one receive. A client that retries, polls, or fans out is a
    client that can block, which is the invariant this file guards."""
    tree = ast.parse(open(_CLIENT, encoding=_TEXT_ENCODING).read(), filename=_CLIENT)
    sends = _count_attribute_calls(tree, ("sendto", "send"))
    receives = _count_attribute_calls(tree, ("recv", "recvfrom"))
    if sends != 1:
        failures.append(f"client makes {sends} socket sends, expected exactly 1")
    if receives != 1:
        failures.append(f"client makes {receives} socket receives, expected exactly 1")
    for lineno, message in _subprocess_timeout_violations(_CLIENT):
        failures.append(f"statusline_client.py:{lineno}: {message}")


def check_live_server_render_budget(failures):
    """End to end against a live server: the median of nine client runs must
    beat the client budget.

    This is the one benchmark in the suite that reads the wall clock, and it
    is deliberately loose: the measured figure on this machine is roughly
    65ms, every historical incident was 5,000ms or worse, and the median of
    nine plus the best of three attempts absorbs a loaded CI runner.
    """
```

`check_live_server_render_budget` starts a real `statusline_server.py` subprocess against a fixture home, polls for `server.json` to appear with a bounded loop, runs the client nine times, takes the median, and sends `shutdown` in a `finally`. Keep `_CORE_MEDIAN_ATTEMPTS`'s best-of-three retry shape for the same reason it exists today.

Delete `check_cold_render_budget`, `check_warm_core_median`, `check_unreachable_host_render_budget`, `_CORE_TIMER_SNIPPET` and `_measure_warm_core_median`. The unreachable-host scenario moves to the server: add a check that starts the server with `STATUSLINE_FABLE_QUOTA_HOST` pointed at `192.0.2.1:8001` and asserts the client still replies inside the budget, which is the same guarantee expressed in the new architecture (the HTTP fetch is now a pool job and cannot touch the reply at all).

- [ ] **Step 2: Run the script to verify the new checks fail**

Run: `python scripts/verify_render_budget.py`
Expected: FAIL on `check_live_server_render_budget` before the client and server exist in their final shape, or FAIL on the import scan if the client imports anything it should not.

- [ ] **Step 3: Make it pass**

Fix whatever the checks flag in `statusline_client.py`. No production change should be needed if Tasks 14 to 16 were followed; if the import scan fires, move the offending import inside `ensure_server` rather than widening the allowance.

- [ ] **Step 4: Run it and record the number**

Run: `python scripts/verify_render_budget.py`
Expected: `OK: render path is free of unbounded sync calls and inside budget`. Note the measured median for the TEST-REPORT update in Task 22.

- [ ] **Step 5: Commit**

```bash
python -m ruff format scripts/verify_render_budget.py
python -m ruff check scripts/verify_render_budget.py
git add scripts/verify_render_budget.py
git commit -m "test: retarget the render-budget invariant at the client"
```

### Task 19: The 2026-09-02 incident regression

On 2026-09-02, six sessions plus CI drove renders to between 7 and 19 seconds against a 3 second refresh interval. The harness killed only each overdue render's shell wrapper, so the Python process survived as an orphan, and the detached refresh children multiplied until the machine held roughly 1,000 Python processes and no idle CPU. This is the test that says it cannot happen again.

**Files:**
- Create: `scripts/verify_server_concurrency.py`

**Interfaces:**
- Consumes: the whole system.
- Produces: nothing importable. This is a scenario test.

- [ ] **Step 1: Write the failing test**

```python
"""The 2026-09-02 incident regression.

Fifty concurrent renders across four working directories, with one refresher
deliberately blocked, must leave exactly one server process and at most four
worker threads, and every render must either get a reply or print its
fallback. The old design failed all three: a stale cache spawned a detached
child per render, the children never finished inside the harness's kill
window, and the process count climbed without a ceiling.

The slow refresher blocks on an Event this script controls, never on a sleep,
and nothing here asserts on elapsed time.

Run from anywhere; imports from agent-statusline by path.
"""

_RENDER_COUNT = 50
_CWD_COUNT = 4


def check_fifty_concurrent_renders_leave_one_server(failures):
    release = threading.Event()
    entered = threading.Semaphore(0)

    def slow_runner(kind, argument):
        if kind == "git-ref":
            entered.release()
            release.wait(timeout=30)
            return
        server_jobs.run_refresh(kind, argument)

    with _running_server(runner=slow_runner) as context:
        results = _run_clients_in_parallel(context, _RENDER_COUNT, _CWD_COUNT)
        release.set()

        if _count_server_processes(context) != 1:
            failures.append(
                f"{_count_server_processes(context)} servers alive, expected exactly 1"
            )
        if context.pool.peak_in_flight() > 4:
            failures.append(
                f"{context.pool.peak_in_flight()} jobs ran at once, the cap is 4"
            )
        if context.pool.worker_count() != 4:
            failures.append(f"pool has {context.pool.worker_count()} threads, expected 4")

    blank = [result for result in results if not result.stdout.strip()]
    nonzero = [result for result in results if result.returncode != 0]
    if blank:
        failures.append(f"{len(blank)} of {_RENDER_COUNT} renders printed nothing")
    if nonzero:
        failures.append(f"{len(nonzero)} of {_RENDER_COUNT} renders exited non-zero")


def check_a_blocked_refresher_never_blocks_a_reply(failures):
    """The receive loop must not be waiting on the pool. With every worker
    blocked, a render still replies from the caches it already has."""
    release = threading.Event()
    with _running_server(runner=lambda kind, argument: release.wait(timeout=30)) as ctx:
        for index in range(4):
            ctx.server.handle_request(
                {"kind": "claude", "payload": _payload(ctx, cwd_index=index)}
            )
        reply = ctx.server.handle_request(
            {"kind": "claude", "payload": _payload(ctx, cwd_index=0)}
        )
        release.set()
    if not reply:
        failures.append("a render must reply while every worker is blocked")


def check_no_orphan_processes_remain(failures):
    """The design that failed spawned a process per stale cache read. Count
    Python processes whose command line names this repository before and
    after the burst; the delta must be zero once the server has exited."""
    before = _count_repository_python_processes()
    with _running_server() as context:
        _run_clients_in_parallel(context, _RENDER_COUNT, _CWD_COUNT)
    after = _count_repository_python_processes()
    if after > before:
        failures.append(
            f"{after - before} python processes outlived the burst;"
            " the render path must leave nothing behind"
        )
```

`_count_repository_python_processes` uses psutil when it is importable and returns the `before` value unchanged when it is not, so the check is a real assertion on a machine with psutil and a harmless no-op on one without.

`_run_clients_in_parallel` uses a `ThreadPoolExecutor` to run `_RENDER_COUNT` client subprocesses, cycling the payload's `cwd` and `workspace.current_dir` across `_CWD_COUNT` temporary directories and giving each its own session id. `_count_server_processes` reads `server.json` and checks the recorded pid, plus a psutil scan for `statusline_server.py` in the command line when psutil is importable (the check degrades to the pid check when it is not, so CI without psutil still runs).

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_concurrency.py`
Expected: FAIL before the pool cap and the single-flight spawn lock are in place. If both are already correct from Tasks 2 and 16, verify the test's teeth by temporarily raising `WORKER_POOL_SIZE` to 40 and confirming the peak-in-flight assertion fires, then put it back.

- [ ] **Step 3: Make it pass**

No new production code should be needed. If the blank-output assertion fires, the cause is datagram loss under burst: raise `RECEIVE_BUFFER_BYTES` in `Server.bind`, and confirm the fallback path is reached rather than papering over it, because a dropped datagram must degrade to a fallback line, never to a blank one.

- [ ] **Step 4: Run it on both platforms**

Run on Windows and on WSL: `python scripts/verify_server_concurrency.py`
Expected: `OK: fifty concurrent renders leave one server and four workers` on both.

- [ ] **Step 5: Commit**

```bash
python -m ruff format scripts/verify_server_concurrency.py
python -m ruff check scripts/verify_server_concurrency.py
git add scripts/verify_server_concurrency.py
git commit -m "test: regression for the 2026-09-02 runaway refresh children"
```

---

## Phase 7: Install, control, documentation

### Task 20: The SessionStart hook

The installer gains one managed entry. The hook that ran `prewarm.sh` now runs the client with `--ensure-server`, identified by a sentinel comment exactly the way the wrap-nudge hook is, so re-running the installer repoints rather than duplicates.

**Files:**
- Create: `statusline_lib/server_install.py`
- Create: `scripts/verify_server_install.py`
- Modify: `statusline_lib/claude_family_install.py`, `install.py:248-320`
- Delete: `prewarm.sh`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (mirroring `statusline_lib/nudge_install.py` name for name):
  - `_SERVER_SENTINEL = "#managed-by:agent-statusline/ensure-server"`
  - `_server_hook_command(repo, platform="claude") -> tuple[str, str]`
  - `_server_hook_markers(target) -> tuple[str, str]`
  - `_find_server_hooks(settings, markers) -> list[tuple[dict, dict]]`
  - `_server_hook_current(settings, markers, command) -> bool`
  - `_merge_server_hook(settings, markers, command) -> None`

- [ ] **Step 1: Write the failing test**

Create `scripts/verify_server_install.py` with in-memory settings dictionaries, matching `scripts/verify_install_nudge_merge.py`'s shape: a fresh install appends one `SessionStart` group; a second merge is idempotent; an entry written by an older install (matched on basename, not sentinel) is repointed in place rather than duplicated; duplicates are collapsed to one and emptied matcher groups are dropped; an unrelated `SessionStart` hook the user configured is preserved untouched; the Windows and POSIX command strings both end in the sentinel and both force a zero exit.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_server_install.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'statusline_lib.server_install'`.

- [ ] **Step 3: Write the implementation**

`server_install.py` is `nudge_install.py` with three differences: the sentinel text, the target (`statusline_client.py` with `--ensure-server`), and the event name (`SessionStart` rather than `UserPromptSubmit`). Keep the same "never exit non-zero" wrapping, since a SessionStart hook that fails must not disturb the session:

```python
def _server_hook_command(repo, platform="claude"):
    """Shell-aware command for the SessionStart ensure-server hook.

    Runs the client's liveness-and-version check only: no stdin is read and
    nothing is printed, so the hook costs one interpreter start and either
    finds a live server or spawns one in the background. This replaces the
    prewarm script, which existed to warm an interpreter that a render no
    longer starts.
    """
    target = f"{repo}/statusline_client.py"
    app_subdirectory = ".gemini/antigravity-cli" if platform == "antigravity" else ".claude"
    if os.name == "nt":
        command = (
            f'py -3 "{target}" --ensure-server'
            f' 2>>"$HOME\\{app_subdirectory.replace("/", chr(92))}\\server_hook.log";'
            f" exit 0 {_SERVER_SENTINEL}"
        )
    else:
        command = (
            f'python3 "{target}" --ensure-server'
            f' 2>>"$HOME/{app_subdirectory}/server_hook.log" || true {_SERVER_SENTINEL}'
        )
    return target, command
```

Extend `claude_family_install.py`'s two public helpers to take the server hook alongside the nudge hook: `statusline_family_already_current(settings, desired_statusline, desired_subagent, nudge_markers, nudge_command, server_markers, server_command)` and the matching `merge_statusline_family_settings`. Update `install.py`'s `_install_claude_family` to build both, add `statusline_client.py` and `statusline_server.py` to its `missing_required_scripts` call, and print the `SessionStart:` line alongside the existing three.

```bash
git rm prewarm.sh
```

Check `interpreter-probe.sh` afterwards: it was sourced by `prewarm.sh` and is also sourced by `statusline-command.sh`, so it stays. Confirm with `grep -rn "interpreter-probe" .` before deleting anything else.

- [ ] **Step 4: Run the tests**

```bash
python scripts/verify_server_install.py
python scripts/verify_install_settings_merge.py
python scripts/verify_install_nudge_merge.py
python scripts/verify_install_platform_routing.py
python install.py --repo "$PWD" --dry-run
```
Expected: the three verify scripts print their `OK:` lines, and the dry run prints a settings block containing the `SessionStart` hook with the sentinel.

- [ ] **Step 5: Commit**

```bash
python -m ruff format .
python -m ruff check .
git add -A
git commit -m "feat: install the ensure-server SessionStart hook, retire prewarm"
```

### Task 21: statusline_ctl server subcommands

**Files:**
- Modify: `statusline_ctl.py:255-277`
- Modify: `scripts/verify_statusline_ctl.py`

**Interfaces:**
- Consumes: `read_server_info`, `server_info_path` (Task 11), the `status` and `shutdown` request kinds (Task 12).
- Produces: `statusline-ctl server status`, `statusline-ctl server stop`, `statusline-ctl server restart`.

- [ ] **Step 1: Write the failing test**

Append to `scripts/verify_statusline_ctl.py`: `server status` with no server prints a "no server running" line and returns 0; `server status` against a fixture `server.json` whose pid is alive prints the pid, port, version and uptime; `server stop` with no server returns 0 and says so; an unknown subcommand under `server` returns 2 with the usage line. Drive `main(["server", "status"])` in-process and capture stdout, as the existing checks in that script do.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python scripts/verify_statusline_ctl.py`
Expected: FAIL with `error: unknown command 'server'`.

- [ ] **Step 3: Write the implementation**

Add `_cmd_server(arguments)` dispatching on `arguments[0]`:

- `status`: read `server.json`, report absent-or-dead plainly, otherwise send a `status` datagram and print the JSON summary as aligned key and value lines. A server whose info file exists but whose pid is gone is reported as stale, not as running.
- `stop`: send `shutdown`, then report whether the info file went away.
- `restart`: `stop`, then run `statusline_client.py --ensure-server` through `process_safe.run_captured` so the caller sees a failure to start.

Register it in `_COMMANDS` and extend the module docstring's usage block, which `--help` prints.

Keep the logic that would otherwise grow `statusline_ctl.py` past its current 277 lines in `statusline_lib/server_info.py`: the control script should be argument parsing and printing only.

- [ ] **Step 4: Run the tests**

```bash
python scripts/verify_statusline_ctl.py
python statusline_ctl.py server status
```
Expected: the verify script prints its `OK:` line, and the live command prints either a summary or a clean "no server running".

- [ ] **Step 5: Commit**

```bash
python -m ruff format .
python -m ruff check .
git add -A
git commit -m "feat: statusline-ctl server status, stop and restart"
```

### Task 22: Documentation

Every statement this branch made untrue gets fixed here. The largest is `AGENTS.md`'s "Render-budget invariant" section, which documents the detached-child spawner, the inflight marker, the spawn timings and the platform pinning of refresh children, all of which this branch deletes.

**Files:**
- Modify: `AGENTS.md` (the "Render-budget invariant" section)
- Modify: `README.md`
- Modify: `TEST-REPORT.md`
- Modify: `PLAN.md` (the render-perf ratchet item)

- [ ] **Step 1: Rewrite the render-budget invariant section**

Replace the whole "Render-budget invariant (no long sync calls in the render path)" section of `AGENTS.md`. The replacement must:

- Keep the incident history that motivates the rule (2026-07-02 SMB stats and walker stalls, 2026-07-10 psutil attribute expansion, 2026-07-11 beacons-history over SMB, 2026-07-16 pace and spend walks) and add 2026-09-02, whose disease was different: not a slow render but unbounded process creation by the mechanism that existed to keep renders fast.
- Describe the current shape: the render path is `statusline_client.py`, one datagram out and one reply in, a 150 ms timeout, a fallback that prints the last render or a payload-only line. All computation is in one resident server per `app_dir()`, renders are served inline on its receive loop, and anything that can block runs on a four thread pool that the receive loop never waits on.
- State the mechanical enforcement: `scripts/verify_render_budget.py` asserts the client imports no `statusline_lib` outside the one allowed `process_safe` import inside `ensure_server`, makes exactly one socket send and one receive, and beats 200 ms end to end against a live server; `scripts/verify_server_concurrency.py` holds the ceiling at one server and four workers under fifty concurrent renders.
- Keep the stale-while-revalidate rule and restate its new mechanism: a cache reader still serves whatever it has, stale included, and still hands recomputation elsewhere, but "elsewhere" is `server_jobs.request_refresh` and the pool, not a detached child. A new walk-priced data source still goes through it rather than growing its own inline TTL cache.
- Describe the platform pin in its new home: the client pins `STATUSLINE_PLATFORM` into the spawned server's environment, because the server's own argv carries no `--statusline-platform` flag. This is the same failure the deleted `refresh._child_snippet` guarded against, now guarded once at spawn rather than per refresh child.
- Delete every reference to `maybe_spawn_refresh`, `refresh.spawn_timings`, `reset_spawn_timings`, the inflight marker file, `_child_snippet`, `verify_refresh_spawner.py`, and the `spawns=` figure in the slow-render breakdown. Per the repository's own convention, describe the current contract, do not narrate what was removed.
- Restate the performance tiers against the new shape: a client render is a fixed cost of roughly 65 ms of interpreter and import plus at most 150 ms of socket wait, and a server render is single-digit milliseconds from payload to string.

- [ ] **Step 2: Run the documentation check across the rest of the tree**

```bash
grep -rn "maybe_spawn_refresh\|spawn_timings\|inflight\|prewarm\|refresh\.py\|detached child\|detached refresh" \
  --include=*.md --include=*.py --include=*.sh --include=*.yml . | grep -v "^./.git"
```

Every hit is either a line to rewrite or a file to delete. Expect hits in `README.md` (the quota refresher description around line 151 and line 159, the working-tree badge note around line 498, the per-render timing note around line 629), `PLAN.md`'s render-perf item, and the module docstrings of the eight cache readers if Task 3's prose pass missed any.

- [ ] **Step 3: Update the README**

Add a short architecture section describing the two processes, the wire format, the fallback, and the version-mismatch restart. Update the install section to mention the `SessionStart` hook. Update the Kimi section: the 300 ms kill window is now met by a client that does one datagram exchange, not by an adapter that avoids transcript walks. Update the render-timing section: the figure the status line prints is the server's render, not a spawn-per-render process.

- [ ] **Step 4: Update TEST-REPORT.md**

New date, new commit, the new script count, the coverage figure from the final run, the ruff and aislop results, and the measured client budget median from Task 18. Follow the existing table format exactly.

- [ ] **Step 5: Final verification**

```bash
python -m ruff format --check .
python -m ruff check .
python -m coverage erase
for t in scripts/verify_*.py; do python -m coverage run -a "$t" || echo "FAILED $t"; done
python -m coverage report -m --include="statusline_lib/*" --fail-under=100
aislop ci .
npm audit
```
Expected: ruff clean, every script passing, 100 percent coverage, aislop exit 0, no new npm advisories. Run the suite on Windows and on WSL before opening the pull request, since the coverage gate runs on both.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: describe the resident server model"
```

---

## Resolved ambiguities

Recorded here because the spec is gone and these decisions are not derivable from the code.

**The client cannot import `statusline_lib`, but the spec says spawning uses `process_safe.spawn_detached`.** Resolved as a function-local import inside `ensure_server`, which runs only on the fallback path after the line has been printed. The hot path stays import-free, and `process_safe` remains the only subprocess surface. Task 18 asserts exactly this shape rather than merely permitting it.

**"One in flight per kind" would starve every working directory but one.** The dispatch entries are per kind but their arguments are per cwd (`git-ref`, `session-count`) and per window (`pace-hourly`, `window-spend`). The pool keys by `(kind, str(argument))`, which is the identity the deleted inflight marker used, so per-cwd refresh behavior is unchanged.

**The client duplicates `app_dir`, `state_dir`, the prefs precedence, the server info reader, the last-render path and the code-version digest.** Unavoidable given the no-import rule. The duplication is pinned by tests rather than by discipline: Task 16 asserts the client and the package compute the same version for the same tree, and Task 14 asserts they resolve the same directories across every platform value and both state-directory environment variables. Task 11 states the digest algorithm precisely enough to reimplement.

**Where the render lives.** The spec says rendering moves into the server, but `statusline.py` is coverage-exempt entry glue. Resolved by extracting the render into `statusline_lib` (Phase 2), which puts it under the 100 percent gate for the first time and takes `statusline.py` off aislop's over-400-lines list. The client stays at the root as glue and is covered black-box by the protocol tests.

**Parent versus subagent cost split under an incremental walk.** `walk_transcript` derives `parent_cost` by snapshotting the accumulator between the parent walk and the subagent walk, which an interleaved incremental walk cannot reproduce. The server instead accumulates the parent's cost delta on each fold, toggling the tracking flags around it exactly as `walk_transcript` does. Task 7's seam test asserts line-at-a-time folding equals a single full walk, so any divergence fails the suite.

**Beacon anchors, the latest beacon, and subagent rows stay on their existing disk caches rather than becoming in-memory per-session fields.** The spec lists them among the per-session table's contents, but it also says on-disk cache files remain the warm-start source and are written on the same schedule so the render code reading them does not change. Those two statements collide only for beacons, and the disk cache wins: `beacon_cache.py`'s stale-while-revalidate lookup already fits the budget, it survives a server restart, and duplicating it in memory would mean two sources of truth for the same field. If a beacon lookup ever shows up as a cost in the server, promoting it to the session table is a contained follow-up.

**There is no separate refresher scheduler.** The spec asks for refreshers "at their current TTLs" and for a test of "refresher scheduling with a fake clock". The TTLs already live in the cache readers (`_GIT_REF_CACHE_TTL_SECONDS`, `_SESSION_COUNT_CACHE_TTL_SECONDS`, and so on), each of which requests a refresh exactly when its own entry goes stale. Adding a timer wheel on top would give every TTL a second, competing owner. Scheduling is therefore tested where it lives, in the existing per-reader verify scripts, and the pool is tested for its ceiling and its deduplication.

**Client constants come from the prefs file, not only the environment.** The spec asks for prefs-file overrides. The client already reads one small JSON file on its hot path, so it reads the prefs file with its own ten-line resolver mirroring `statusline_lib/prefs.py`'s precedence, rather than dropping to environment variables only.
