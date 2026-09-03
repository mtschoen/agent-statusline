"""Verify statusline_lib/server_jobs.py: the refresh dispatch table, the
pluggable request sink, and the bounded worker pool that together replaced
the detached-child spawner.

Run from anywhere; imports from agent-statusline by path.
"""

import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib import server_jobs

# Generous upper bound for every Event/Semaphore/Thread wait below: a
# deadlock safety net, not a race window to tune, since every wait here
# synchronizes on a real condition rather than elapsed wall-clock time.
_EVENT_WAIT_SECONDS = 5


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
        "transcript-walk",
        "beacon-anchors",
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
        if not done.wait(timeout=_EVENT_WAIT_SECONDS):
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
        release.wait(timeout=_EVENT_WAIT_SECONDS)

    pool = server_jobs.WorkerPool(size=4, runner=runner)
    pool.start()
    try:
        first = pool.submit("git-ref", "/repo")
        started.wait(timeout=_EVENT_WAIT_SECONDS)
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
        release.wait(timeout=_EVENT_WAIT_SECONDS)

    pool = server_jobs.WorkerPool(size=2, runner=runner)
    pool.start()
    try:
        for index in range(6):
            pool.submit("git-ref", f"/repo-{index}")
        for _ in range(2):
            if not entered.acquire(timeout=_EVENT_WAIT_SECONDS):
                failures.append("pool did not start its two workers")
        if pool.in_flight_count() > 2:
            failures.append(f"{pool.in_flight_count()} jobs in flight, pool size is 2")
        if pool.queue_depth() != 4:
            failures.append(
                f"queue_depth was {pool.queue_depth()}, expected 4 queued jobs"
            )
        # worker_count() reports live threads, so check it before stop().
        if pool.worker_count() != 2:
            failures.append(f"pool started {pool.worker_count()} threads, expected 2")
        release.set()
    finally:
        pool.stop()
    if pool.peak_in_flight() > 2:
        failures.append(f"peak in flight was {pool.peak_in_flight()}, cap is 2")


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
        done.wait(timeout=_EVENT_WAIT_SECONDS)
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


def check_pool_start_stop_idempotency_and_live_worker_count(failures):
    """start()/stop() must be idempotent, and worker_count() must report
    threads that are actually alive rather than how many were ever started,
    so a status handler is never told about workers that have exited."""
    pool = server_jobs.WorkerPool(size=2, runner=lambda kind, argument: None)
    pool.start()
    pool.start()
    if pool.worker_count() != 2:
        failures.append(
            f"start() must be idempotent, worker_count is {pool.worker_count()}"
        )
    pool.stop()
    pool.stop()
    if pool.worker_count() != 0:
        failures.append(
            f"worker_count() was {pool.worker_count()} after stop(), expected 0 live workers"
        )


def check_pool_default_error_logger_calls_log_traceback(failures):
    calls = []
    original = server_jobs.log_traceback
    server_jobs.log_traceback = lambda path: calls.append(path)
    done = threading.Event()

    def runner(kind, argument):
        try:
            raise RuntimeError("boom")
        finally:
            done.set()

    pool = server_jobs.WorkerPool(size=1, runner=runner)
    pool.start()
    try:
        pool.submit("git-ref", "/repo")
        if not done.wait(timeout=_EVENT_WAIT_SECONDS):
            failures.append("pool never ran the raising job")
    finally:
        pool.stop()
        server_jobs.log_traceback = original
    if calls != [server_jobs._POOL_ERROR_LOG]:
        failures.append(
            f"default error_logger must call log_traceback once, got {calls}"
        )


def check_pool_worker_idle_poll_branches(failures):
    """Covers both arms of _drain's queue.Empty handler: looping while still
    running, and exiting once _stopped flips true between polls (the race
    stop() can hit before its sentinel reaches a worker)."""
    idled = threading.Event()
    empty_polls = [0]
    pool = server_jobs.WorkerPool(size=1, runner=lambda kind, argument: None)
    original_get = pool._queue.get

    def counting_get(timeout=None):
        try:
            return original_get(timeout=timeout)
        except queue.Empty:
            empty_polls[0] += 1
            if empty_polls[0] >= 3:
                idled.set()
            raise

    pool._queue.get = counting_get
    original_poll_seconds = server_jobs._QUEUE_POLL_SECONDS
    server_jobs._QUEUE_POLL_SECONDS = 0.01
    try:
        pool.start()
        if not idled.wait(timeout=_EVENT_WAIT_SECONDS):
            failures.append("worker never idled on an empty, unstopped queue")
        pool._stopped = True
        for thread in pool._threads:
            thread.join(timeout=_EVENT_WAIT_SECONDS)
        if any(thread.is_alive() for thread in pool._threads):
            failures.append(
                "worker did not exit once idle-stopped without a shutdown sentinel"
            )
    finally:
        server_jobs._QUEUE_POLL_SECONDS = original_poll_seconds
        pool._stopped = False
        pool.stop()


def _run_pool_where_the_first_job_raises(
    failures, raise_first_job, error_logger, death_message
):
    """Shared harness: a size-1 pool whose first job raises via the no-arg
    `raise_first_job`, then a second job that only runs if the worker
    thread is still alive."""
    done_first = threading.Event()
    done_second = threading.Event()

    def runner(kind, argument):
        if argument == "/repo-0":
            done_first.set()
            raise_first_job()
        done_second.set()

    pool = server_jobs.WorkerPool(size=1, runner=runner, error_logger=error_logger)
    pool.start()
    try:
        pool.submit("git-ref", "/repo-0")
        if not done_first.wait(timeout=_EVENT_WAIT_SECONDS):
            failures.append("pool never ran the first (raising) job")
        pool.submit("git-ref", "/repo-1")
        if not done_second.wait(timeout=_EVENT_WAIT_SECONDS):
            failures.append(death_message)
    finally:
        pool.stop()


def check_pool_survives_a_raising_first_job(failures):
    """A runner or its error_logger raising, even SystemExit, must not kill
    the worker: a second job still has to run, and a raising logger falls
    back to the module's own traceback log instead of taking it down."""

    def raise_boom():
        raise RuntimeError("boom")

    def raising_logger(error):
        raise RuntimeError("the injected logger is itself broken")

    fallback_calls = []
    original = server_jobs.log_traceback
    server_jobs.log_traceback = lambda path: fallback_calls.append(path)
    try:
        _run_pool_where_the_first_job_raises(
            failures,
            raise_boom,
            raising_logger,
            "worker died after a raising error_logger; the second job never ran",
        )
    finally:
        server_jobs.log_traceback = original
    if fallback_calls != [server_jobs._POOL_ERROR_LOG]:
        failures.append(
            f"a raising error_logger must fall back to log_traceback once, got {fallback_calls}"
        )

    def raise_system_exit():
        raise SystemExit("boom")

    logged = []
    _run_pool_where_the_first_job_raises(
        failures,
        raise_system_exit,
        logged.append,
        "worker died on SystemExit; the second job never ran",
    )
    if not logged:
        failures.append("a job raising SystemExit must still reach the error logger")


def main():
    failures = []
    for check in (
        check_dispatch_table_covers_every_refresher,
        check_request_refresh_without_sink_is_a_no_op,
        check_request_refresh_forwards_to_the_sink,
        check_set_refresh_sink_returns_the_previous_sink,
        check_run_refresh_calls_the_named_refresher,
        check_run_refresh_rejects_an_unknown_kind,
        check_pool_runs_a_submitted_job,
        check_pool_dedupes_a_job_already_in_flight,
        check_pool_never_exceeds_its_size,
        check_pool_logs_and_survives_a_raising_job,
        check_pool_refuses_submissions_after_stop,
        check_pool_start_stop_idempotency_and_live_worker_count,
        check_pool_default_error_logger_calls_log_traceback,
        check_pool_worker_idle_poll_branches,
        check_pool_survives_a_raising_first_job,
    ):
        check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: refresh dispatch table, sink, and worker pool all verified")


if __name__ == "__main__":
    main()
