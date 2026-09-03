"""Verify that a beacon cache miss self-heals on the next render.

The first format call serves the true miss as a hidden column and submits a
refresh to the resident server's worker pool. A bounded completion event then
proves the refresh wrote its cache before the second call reads it.
"""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statusline_lib.beacon as beacon
import statusline_lib.beacon_cache as beacon_cache
from statusline_lib.server_jobs import WorkerPool, set_refresh_sink

_SESSION_ID = "self-heal-session"
_WAIT_TIMEOUT_SECONDS = 2.0


def check_a_beacon_cache_miss_self_heals(failures):
    completed = threading.Event()
    original_state_directory = os.environ.get("CLAUDE_STATE_DIR")
    original_walker = beacon_cache._walker_subcommand
    original_writer = beacon_cache.write_ttl_cache
    original_anchors = beacon._find_beacon_anchors

    def fake_walker(*arguments):
        del arguments
        return {
            "beacon": {
                "kind": "report",
                "eta_seconds": 60,
                "summary": "recovered",
            },
            "age_seconds": 0,
        }

    def recording_writer(path, data):
        original_writer(path, data)
        completed.set()

    pool = WorkerPool(size=1)
    previous_sink = set_refresh_sink(pool.submit)
    try:
        with tempfile.TemporaryDirectory() as state_directory:
            os.environ["CLAUDE_STATE_DIR"] = state_directory
            beacon_cache._walker_subcommand = fake_walker
            beacon_cache.write_ttl_cache = recording_writer
            beacon._find_beacon_anchors = lambda _session_id: (None, None, None)
            pool.start()

            first_rendered, first_beacon = beacon.format_beacon(_SESSION_ID)
            if first_rendered is not None or first_beacon is not None:
                failures.append(
                    "a true beacon cache miss must hide the column on its first render"
                )
            elif not completed.wait(timeout=_WAIT_TIMEOUT_SECONDS):
                failures.append("the beacon refresh did not complete within the bound")
            else:
                second_rendered, second_beacon = beacon.format_beacon(_SESSION_ID)
                if "recovered" not in (second_rendered or ""):
                    failures.append(
                        "the beacon column did not reappear after refresh: "
                        f"{second_rendered!r}"
                    )
                if (second_beacon or {}).get("summary") != "recovered":
                    failures.append(
                        f"the refreshed beacon payload was not returned: {second_beacon!r}"
                    )
    finally:
        pool.stop()
        set_refresh_sink(previous_sink)
        beacon_cache._walker_subcommand = original_walker
        beacon_cache.write_ttl_cache = original_writer
        beacon._find_beacon_anchors = original_anchors
        if original_state_directory is None:
            os.environ.pop("CLAUDE_STATE_DIR", None)
        else:
            os.environ["CLAUDE_STATE_DIR"] = original_state_directory


def main():
    failures = []
    check_a_beacon_cache_miss_self_heals(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: beacon cache miss self-heal verified")


if __name__ == "__main__":
    main()
