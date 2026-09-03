"""In-process refresh jobs for the resident server.

A cache reader serves whatever entry it has, stale included, and hands the
recomputation to a bounded thread pool inside the one resident server
process. The pool is what keeps that promise cheap: four threads is a hard
ceiling on how much work a burst of stale reads can start, however many
sessions are rendering at once.

This module holds the dispatch table (REFRESHER_MODULES), the refresher
runner (run_refresh), the pluggable request sink (set_refresh_sink,
request_refresh) every cache reader calls, and the bounded WorkerPool the
resident server installs as that sink's implementation. request_refresh
forwards to whatever sink is installed and returns False when none is,
which is the case for every process that is not the resident server: a
plain render serves its stale cache entry and starts nothing, and a reader
with nothing to serve asks has_refresh_sink whether a pool exists at all.

Imports: standard library only, plus a lazy per-call import of the refresher
module inside `run_refresh` (pace, burnrate, gitref, beacon_cache, beacon,
sessions, qwen_quota, fable_quota and transcript_summaries all import THIS
module, so a top-level import here would be circular).
"""

import importlib
import os
import queue
import threading

from .base import app_dir, log_traceback

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
    "transcript-walk": ("transcript_summaries", "refresh_transcript_walk"),
    "beacon-anchors": ("transcript_summaries", "refresh_beacon_anchors"),
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


def has_refresh_sink():
    """True when a sink is installed, which is true only inside the resident
    server. A cache reader uses it to tell "hand this to the pool" apart from
    "there is no pool, so compute it here"."""
    return _REFRESH_SINK is not None


def run_refresh(kind, argument):
    """Recompute cache `kind` for `argument` by calling the refresher named
    in REFRESHER_MODULES, returning whatever that refresher returns. Raises
    ValueError on an unknown kind; any exception the refresher itself raises
    propagates to the pool, which logs it."""
    target = REFRESHER_MODULES.get(kind)
    if target is None:
        raise ValueError(f"unknown refresh kind: {kind!r}")
    module_name, attribute = target
    module = importlib.import_module(f".{module_name}", package=__package__)
    return getattr(module, attribute)(argument)


# Four threads is the whole point of the pool: the failure this replaced was
# unbounded process creation, so the replacement must have a hard ceiling that
# no amount of load can lift.
WORKER_POOL_SIZE = 4

# How long a worker blocks on the queue before re-checking the stop flag.
# Bounded rather than infinite so stop() cannot wedge on an idle pool, and
# small enough that shutdown is not perceptible.
_QUEUE_POLL_SECONDS = 0.25

_SHUTDOWN = object()

_POOL_ERROR_LOG = os.path.join(app_dir(), ".statusline-server-error.log")


def _log_pool_error(error):
    """Default error_logger for WorkerPool: record the traceback through the
    package's existing log_traceback helper. Only correct when called from
    within the except block that is still handling `error`, which is how
    WorkerPool._drain calls it; log_traceback reads the traceback off the
    live exception context rather than off `error` itself."""
    del error
    log_traceback(_POOL_ERROR_LOG)


class WorkerPool:
    """A fixed number of daemon threads draining one job queue, with at most
    one job in flight per (kind, argument).

    `runner(kind, argument)` does the work; the default is run_refresh.
    `error_logger(exception)` receives anything a job raises, so one bad
    refresher can never take the server down; it defaults to logging the
    traceback rather than swallowing it, and the parameter exists so tests
    can inject their own recorder instead.

    Both `runner` and `error_logger` are caught broadly, including
    SystemExit and KeyboardInterrupt: a worker thread is not the main
    thread, so neither actually stops the interpreter, and letting either
    escape would silently shrink the pool by one thread with nothing left
    to notice. A raising error_logger falls back to the module's own
    traceback log rather than taking the worker down with it.

    No job carries a per-job deadline: a job is bounded by whatever
    subprocess and HTTP timeouts it makes on its own, plus the fixed size of
    this pool.
    """

    def __init__(self, size=WORKER_POOL_SIZE, runner=run_refresh, error_logger=None):
        self._size = size
        self._runner = runner
        self._error_logger = (
            error_logger if error_logger is not None else _log_pool_error
        )
        self._queue = queue.Queue()
        self._threads = []
        self._lock = threading.Lock()
        self._claimed = set()
        self._in_flight = 0
        self._peak_in_flight = 0
        self._stopped = False

    def start(self):
        """Start the worker threads. Idempotent.

        Publishing a thread and starting it happen under one lock hold:
        anything that takes the lock in between, stop() above all, would see
        a thread in self._threads that was never started and join it, which
        raises RuntimeError.
        """
        with self._lock:
            if self._threads or self._stopped:
                return
            for index in range(self._size):
                thread = threading.Thread(
                    target=self._drain, name=f"statusline-worker-{index}", daemon=True
                )
                self._threads.append(thread)
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
            except BaseException as error:
                try:
                    self._error_logger(error)
                except BaseException:
                    log_traceback(_POOL_ERROR_LOG)
            finally:
                with self._lock:
                    self._in_flight -= 1
                    self._claimed.discard(key)

    def stop(self, timeout=2.0):
        """Stop accepting work and wait briefly for the threads to notice.
        The threads are daemons, so a straggler never keeps the process
        alive. The default matches the two second cap
        scripts/verify_render_budget_static.py enforces on every literal
        timeout in the package."""
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
        """Count of worker threads still alive, so a status handler reports
        the truth even if a worker somehow died: this never counts a thread
        that has exited, whether from stop() or (despite the broad catches
        in _drain) an unexpected death."""
        return sum(1 for thread in self._threads if thread.is_alive())
