"""Verify `count_active_sessions` (a pure cached read), its server-side
refresher `refresh_session_count_cache`, and the classifier
`_process_matches`.

Covers:
  - count_active_sessions never scans inline and never submits a refresh
    job: a fresh entry is served, a stale or missing entry is served too (0
    on a true miss). The resident server is the only submitter for the
    session-count kind, because it is the only process that knows the whole
    set of live directories, and a scan for one directory already answers
    all of them (render-perf ratchet step 3, PLAN.md: an uncached scan
    measured ~120ms on a machine with a few hundred processes).
  - refresh_session_count_cache answers every directory it is handed from
    ONE machine-wide process scan and persists each result where the
    render's cached read serves it, including the psutil-unavailable and
    scan-raises degrade paths.
  - Pure-function tests of `_process_matches` with synthesized
    (name, cmdline, cwd) inputs -- no live or mocked psutil needed.

Run from anywhere; imports from `agent-statusline` by path.
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.sessions as sessions_mod
from statusline_lib import _process_matches, count_active_sessions
from statusline_lib.server_jobs import set_refresh_sink

_ENCODING = "utf-8"


def _load(path):
    """The session-count cache file as a dict."""
    with open(path, encoding=_ENCODING) as f:
        return json.load(f)


class _SinkRecorder:
    """Stands in for the resident server's worker pool: records every job
    submitted through server_jobs.request_refresh while it is installed."""

    def __init__(self):
        self.calls = []

    def __call__(self, kind, argument):
        self.calls.append((kind, argument))
        return True


def check_dispatch(failures):
    # Empty cwd cannot be enumerated against -> 0, no exception.
    if count_active_sessions("") != 0:
        failures.append("empty cwd should return 0")

    # A cwd with no cache entry at all -> 0 (honest miss), never an inline
    # psutil scan and never a submitted job.
    sink = _SinkRecorder()
    previous = set_refresh_sink(sink)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "sessioncount-cache.json")
            bogus_cwd = os.path.join(tmp, "definitely-not-a-claude-cwd-zzz")
            result = count_active_sessions(bogus_cwd, cache_path=cache_path)
            if result != 0:
                failures.append(f"cache miss should return 0; got {result!r}")
            if sink.calls:
                failures.append(
                    f"a cache miss must not submit a refresh; got {sink.calls!r}"
                )
    finally:
        set_refresh_sink(previous)


def check_classifier(failures):
    target = os.path.normcase("/home/user/proj")

    if not _process_matches("claude", ["claude"], "/home/user/proj", target):
        failures.append("interactive claude in target cwd should match")
    if not _process_matches("claude.exe", ["claude.exe"], "/home/user/proj", target):
        failures.append("claude.exe in target cwd should match (Windows)")
    if not _process_matches(
        "node", ["node", "/path/to/claude/cli.js"], "/home/user/proj", target
    ):
        failures.append("node-wrapped claude should match")

    with patch.object(sessions_mod.os, "name", "nt"):
        if not _process_matches("kimi.exe", ["kimi.exe"], "/home/user/proj", target):
            failures.append("kimi.exe should match on Windows")
        if _process_matches("kimi", ["kimi"], "/home/user/proj", target):
            failures.append("POSIX kimi binary should not match on Windows")

    with patch.object(sessions_mod.os, "name", "posix"):
        if not _process_matches("kimi", ["kimi"], "/home/user/proj", target):
            failures.append("kimi binary should match on POSIX")
        if not _process_matches(
            "node",
            ["node", "/opt/@moonshot-ai/kimi-code/dist/cli.js"],
            "/home/user/proj",
            target,
        ):
            failures.append("node-wrapped kimi should match on POSIX")
        if _process_matches("kimi.exe", ["kimi.exe"], "/home/user/proj", target):
            failures.append("Windows kimi.exe should not match on POSIX")

    for render_name, render_command in (
        ("py.exe", ["py", "-3", "C:/repo/kimi_statusline.py"]),
        ("python3", ["python3", "/repo/kimi_statusline.py"]),
    ):
        if _process_matches(render_name, render_command, "/home/user/proj", target):
            failures.append(f"Kimi statusline renderer {render_name} should not match")

    # Negative: -p / --print headless mode (Task subagents, scripted)
    if _process_matches(
        "claude.exe",
        ["claude.exe", "-p", "--output-format", "json"],
        "/home/user/proj",
        target,
    ):
        failures.append("-p subagent should NOT match")
    if _process_matches(
        "claude", ["claude", "--print", "hi"], "/home/user/proj", target
    ):
        failures.append("--print subagent should NOT match")

    # Negative: wrong cwd
    if _process_matches("claude", ["claude"], "/other/cwd", target):
        failures.append("wrong cwd should NOT match")

    # Negative: unrelated process name
    if _process_matches("python", ["python", "script.py"], "/home/user/proj", target):
        failures.append("non-claude process should NOT match")

    # Negative: node without 'claude' in argv (regular Node app)
    if _process_matches("node", ["node", "server.js"], "/home/user/proj", target):
        failures.append("node without claude in argv should NOT match")

    # Negative: empty / None cwd
    if _process_matches("claude", ["claude"], "", target):
        failures.append("empty cwd should NOT match")
    if _process_matches("claude", ["claude"], None, target):
        failures.append("None cwd should NOT match")


def check_cache(failures):
    """Whatever the cache holds is served, whatever its age, and nothing is
    recomputed or submitted on the way. Recently written, long expired and
    future-stamped (a backwards clock jump) all read the same, because the
    reader has no way to act on the difference -- only the server can, and
    it refreshes every live directory on its own schedule. Seed the cache
    directly so the expected value is deterministic and no live `claude`
    process is required: the seeded sentinel (42) could never come from a
    real scan of an empty temp dir, so getting it back proves a cache read,
    not a re-scan.
    """
    sink = _SinkRecorder()
    previous = set_refresh_sink(sink)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "sessioncount-cache.json")
            cwd = os.path.join(tmp, "proj")
            key = os.path.normcase(cwd)

            def seed(count, ts):
                with open(cache_path, "w", encoding=_ENCODING) as f:
                    json.dump({key: {"count": count, "ts": ts}}, f)

            for label, stamp in (
                ("a just-written", 1000),
                ("a long-expired", 1),
                ("a future-stamped", 5_000_000_000),
            ):
                seed(42, stamp)
                if count_active_sessions(cwd, cache_path=cache_path) != 42:
                    failures.append(f"{label} cache entry should be served as-is")

            # A malformed entry (not a dict) is a miss, not a crash.
            with open(cache_path, "w", encoding=_ENCODING) as f:
                json.dump({key: "not-an-entry"}, f)
            if count_active_sessions(cwd, cache_path=cache_path) != 0:
                failures.append("a malformed cache entry should read as 0")

            if sink.calls:
                failures.append(
                    f"a cached read must never submit a refresh: {sink.calls!r}"
                )
    finally:
        set_refresh_sink(previous)


def check_refresh_writes_cache(failures):
    """refresh_session_count_cache persists the psutil scan where the
    render's cached read can serve it, under the module's default cache
    path when no cache_path is given."""
    real_count = sessions_mod._count_via_psutil
    sessions_mod._count_via_psutil = lambda cwd, ps, scan=None: 7
    saved_path = sessions_mod._SESSION_COUNT_CACHE_PATH
    sink = _SinkRecorder()
    previous = set_refresh_sink(sink)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "cache.json")
            cwd = os.path.join(tmp, "proj")
            sessions_mod._SESSION_COUNT_CACHE_PATH = cache_path
            returned = sessions_mod.refresh_session_count_cache(cwd)
            served = count_active_sessions(cwd, cache_path=cache_path)
    finally:
        set_refresh_sink(previous)
        sessions_mod._count_via_psutil = real_count
        sessions_mod._SESSION_COUNT_CACHE_PATH = saved_path
    if returned != 7:
        failures.append(f"refresh return: expected 7, got {returned!r}")
    if served != 7:
        failures.append(f"refresh then read: expected 7, got {served!r}")
    if sink.calls:
        failures.append(f"fresh read after refresh submitted: {sink.calls!r}")


def check_refresh_psutil_unavailable(failures):
    """refresh_session_count_cache degrades to 0 (and still writes the
    cache) when psutil cannot be imported."""
    real_resolve = sessions_mod._resolve_psutil
    sessions_mod._resolve_psutil = lambda: None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "cache.json")
            cwd = os.path.join(tmp, "proj")
            result = sessions_mod.refresh_session_count_cache(
                cwd, cache_path=cache_path
            )
            if result != 0:
                failures.append(
                    f"refresh_session_count_cache without psutil should return 0;"
                    f" got {result}"
                )
            if _load(cache_path).get(os.path.normcase(cwd), {}).get("count") != 0:
                failures.append(
                    "refresh without psutil must still write a 0 entry:"
                    f" {_load(cache_path)!r}"
                )
            if sessions_mod.process_snapshots_taken() != 0:
                failures.append("no psutil means no process snapshot may be taken")
    finally:
        sessions_mod._resolve_psutil = real_resolve


def check_refresh_count_via_psutil_exception(failures):
    """A raising _count_via_psutil degrades to 0 for that directory rather
    than propagating out of the server's worker pool, and the directories
    after it are still scored."""
    real_count = sessions_mod._count_via_psutil

    def raising_for_first(target_cwd, psutil_module, scan=None):
        if target_cwd.endswith("first"):
            raise RuntimeError("simulated psutil failure")
        return 3

    sessions_mod._count_via_psutil = raising_for_first
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "cache.json")
            first = os.path.join(tmp, "first")
            second = os.path.join(tmp, "second")
            sessions_mod.refresh_session_count_cache(
                [first, second], cache_path=cache_path
            )
            cache = _load(cache_path)
    finally:
        sessions_mod._count_via_psutil = real_count
    if cache.get(os.path.normcase(first), {}).get("count") != 0:
        failures.append(f"a raising scan should cache 0 for that cwd: {cache!r}")
    if cache.get(os.path.normcase(second), {}).get("count") != 3:
        failures.append(f"a raising scan must not skip later cwds: {cache!r}")


def check_save_session_count_cache_oserror(failures):
    # sessions.py: _save_session_count_cache swallows OSError.
    with tempfile.TemporaryDirectory() as tmp:
        blocker = os.path.join(tmp, "not_a_dir")
        with open(blocker, "w", encoding=_ENCODING) as f:
            f.write("blocker")
        bad_path = os.path.join(blocker, "cache.json")
        # Must not raise.
        sessions_mod._save_session_count_cache(
            bad_path, {"k": {"count": 1, "ts": 1.0}}, 1.0
        )


def check_load_session_count_cache_non_dict(failures):
    # sessions.py: _load_session_count_cache returns {} when JSON root is not a dict.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cache.json")
        with open(path, "w", encoding=_ENCODING) as f:
            f.write("[1, 2, 3]")
        result = sessions_mod._load_session_count_cache(path)
        if result != {}:
            failures.append(
                f"_load_session_count_cache with JSON array should return {{}}; got {result!r}"
            )


def main():
    failures = []
    check_dispatch(failures)
    check_classifier(failures)
    check_cache(failures)
    check_refresh_writes_cache(failures)
    check_refresh_psutil_unavailable(failures)
    check_refresh_count_via_psutil_exception(failures)
    check_save_session_count_cache_oserror(failures)
    check_load_session_count_cache_non_dict(failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print(
        "OK: count_active_sessions is a pure cached read, its refresher"
        " persists correctly, and _process_matches behaves correctly across"
        " all cases"
    )


if __name__ == "__main__":
    main()
