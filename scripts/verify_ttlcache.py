"""Verify statusline_lib/ttlcache.py: the generic single-value TTL disk-cache
mechanics shared by statusline.py's git-ref cache and beacon_cache.py's
beacons-latest cache (see ttlcache.py's docstring for why the other disk
caches in the package -- beacon.py's bias cache, pace.py/burnrate.py's
hourly/window-spend caches -- do NOT use this helper).

Covers: miss, hit, expiry, corrupt file, non-dict JSON payload, an unwritable
cache directory (write must swallow the OSError and never raise), and the
atomic round-trip (write then read returns the same value plus the caller's
own fields untouched).

Run from anywhere; imports from schoen-claude-status by path.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statusline_lib.ttlcache import read_ttl_cache, write_ttl_cache


def check_miss_returns_none(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "missing.json")
        if read_ttl_cache(path, 10) is not None:
            failures.append("a missing cache file must read as None")


def check_write_then_read_hit(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "entry.json")
        write_ttl_cache(path, {"branch": "main", "short_hash": "abc123"})
        cached = read_ttl_cache(path, 10)
        if cached is None:
            failures.append("a fresh write must be read back as a hit")
            return
        if cached.get("branch") != "main" or cached.get("short_hash") != "abc123":
            failures.append(
                f"read must return the caller's own fields verbatim; got {cached!r}"
            )
        if "cached_at_unix" not in cached:
            failures.append("read must include the internal timestamp key")


def check_expiry_returns_none(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "stale.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"cached_at_unix": time.time() - 100, "data": "old"}, f)
        if read_ttl_cache(path, 10) is not None:
            failures.append("an entry older than the TTL must read as None")


def check_within_ttl_is_a_hit(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "fresh.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"cached_at_unix": time.time() - 1, "data": "recent"}, f)
        cached = read_ttl_cache(path, 10)
        if cached is None or cached.get("data") != "recent":
            failures.append(f"an entry within the TTL must be a hit; got {cached!r}")


def check_corrupt_json_returns_none(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "corrupt.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not-json")
        if read_ttl_cache(path, 10) is not None:
            failures.append("corrupt JSON must read as None, not raise")


def check_non_dict_json_returns_none(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "list.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        if read_ttl_cache(path, 10) is not None:
            failures.append("a non-dict JSON payload must read as None")


def check_unwritable_dir_does_not_raise(failures):
    with tempfile.TemporaryDirectory() as tmp:
        blocker = os.path.join(tmp, "not-a-dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        # A path component that is a file makes os.makedirs fail with an
        # OSError subclass on every platform -- simulates an unwritable
        # cache dir without relying on chmod semantics that differ on
        # Windows.
        path = os.path.join(blocker, "state", "entry.json")
        write_ttl_cache(path, {"data": "unpersisted"})  # must not raise
        if os.path.exists(path):
            failures.append("an unwritable dir must not somehow produce a file")


def check_second_write_overwrites_first(failures):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "entry.json")
        write_ttl_cache(path, {"data": "first"})
        write_ttl_cache(path, {"data": "second"})
        cached = read_ttl_cache(path, 10)
        if cached is None or cached.get("data") != "second":
            failures.append(f"a second write must replace the first; got {cached!r}")


def check_no_stray_tmp_file_left_behind(failures):
    # write_ttl_cache writes to a pid-scoped tmp file then os.replace()s it
    # into place -- the tmp file must not survive a successful write.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "entry.json")
        write_ttl_cache(path, {"data": "x"})
        leftovers = [name for name in os.listdir(tmp) if name != os.path.basename(path)]
        if leftovers:
            failures.append(
                f"a successful write must not leave tmp files: {leftovers!r}"
            )


def main():
    failures = []
    for check in (
        check_miss_returns_none,
        check_write_then_read_hit,
        check_expiry_returns_none,
        check_within_ttl_is_a_hit,
        check_corrupt_json_returns_none,
        check_non_dict_json_returns_none,
        check_unwritable_dir_does_not_raise,
        check_second_write_overwrites_first,
        check_no_stray_tmp_file_left_behind,
    ):
        check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print("OK: ttlcache hit/miss/expiry/corruption/unwritable-dir all verified")


if __name__ == "__main__":
    main()
