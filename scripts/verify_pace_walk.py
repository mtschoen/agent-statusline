"""Verify _weekly_deltas guards, weekly_needle (verbose + exception), and
_discover_pace_groups (parent + subagent grouping, mtime-OSError skips) in
statusline_lib/pace.py. The _pace_hourly_cached stale-while-revalidate
contract lives in verify_pace_refresh.py.

Run from anywhere; imports from `schoen-claude-status` by path.
"""

import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.pace as pace

_WIN_START = 1_748_000_000.0
_PERIOD = 7 * 86400


def _make_rl(util, resets_at):
    return {"seven_day": {"used_percentage": util, "resets_at": resets_at}}


def _pin(now, hourly):
    real_now = pace._now_unix
    real_cached = pace._pace_hourly_cached
    pace._now_unix = lambda: now
    pace._pace_hourly_cached = lambda _ws: hourly
    return real_now, real_cached


def _restore(real_now, real_cached):
    pace._now_unix = real_now
    pace._pace_hourly_cached = real_cached


def _check_weekly_deltas_none_guards(failures):
    """Cover the early-return None guards in _weekly_deltas (lines 254, 259)."""
    if pace._weekly_deltas(None, _WIN_START + _PERIOD, _PERIOD) is not None:
        failures.append("util=None should return None")
    if pace._weekly_deltas(0, _WIN_START + _PERIOD, _PERIOD) is not None:
        failures.append("util=0 should return None")
    if pace._weekly_deltas(50, 0, _PERIOD) is not None:
        failures.append("resets_at=0 should return None")

    resets_at = _WIN_START + _PERIOD
    real_now = pace._now_unix
    real_cached = pace._pace_hourly_cached
    pace._now_unix = lambda: _WIN_START - 3600
    pace._pace_hourly_cached = lambda _ws: [1.0] * 10
    try:
        if pace._weekly_deltas(50, resets_at, _PERIOD) is not None:
            failures.append("elapsed<=0 should return None")
    finally:
        pace._now_unix = real_now
        pace._pace_hourly_cached = real_cached

    real_now = pace._now_unix
    real_cached = pace._pace_hourly_cached
    pace._now_unix = lambda: _WIN_START + _PERIOD + 3600
    pace._pace_hourly_cached = lambda _ws: [1.0] * 10
    try:
        if pace._weekly_deltas(50, resets_at, _PERIOD) is not None:
            failures.append("remaining<=0 should return None")
    finally:
        pace._now_unix = real_now
        pace._pace_hourly_cached = real_cached


def _check_weekly_deltas_cumulative_none(failures):
    """Cover the cumulative_delta is None propagation (line 267).

    With the guards at lines 253-259 already passed, the real project_delta
    cannot return a None cumulative (its degenerate-input conditions are the
    same ones _weekly_deltas pre-checks), so exercise the documented (None,
    None) contract by assignment on the module-level imported name.
    """
    resets_at = _WIN_START + _PERIOD
    real_now = pace._now_unix
    real_cached = pace._pace_hourly_cached
    real_project_delta = pace.project_delta
    pace._now_unix = lambda: _WIN_START + 84 * 3600
    pace._pace_hourly_cached = lambda _ws: [1.0] * 84
    pace.project_delta = lambda *arguments, **keywords: (None, None)
    try:
        if pace._weekly_deltas(50, resets_at, _PERIOD) is not None:
            failures.append("project_delta returning None should return None")
    finally:
        pace._now_unix = real_now
        pace._pace_hourly_cached = real_cached
        pace.project_delta = real_project_delta


def _check_weekly_needle_deltas_none(failures):
    """Cover the deltas is None -> return '' branch (line 284). util=0 makes
    _weekly_deltas bail deterministically at its first guard."""
    result = pace.weekly_needle(_make_rl(0, _WIN_START + _PERIOD))
    if result != "":
        failures.append(
            f"weekly_needle with no deltas should return '', got {result!r}"
        )


def _check_weekly_needle_verbose(failures):
    """Cover the verbose path (lines 288-291)."""
    resets_at = _WIN_START + _PERIOD
    now = _WIN_START + 84 * 3600
    hourly = [1.0] * 84

    real_now, real_cached = _pin(now, hourly)
    import statusline_lib.prefs as _prefs

    real_prefs_path = _prefs.prefs_path
    prefs_file = None
    try:
        import tempfile as _tf

        with _tf.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as pf:
            json.dump({"STATUSLINE_VERBOSE_PACE": "1"}, pf)
            prefs_file = pf.name

        _prefs.prefs_path = lambda: prefs_file
        result = pace.weekly_needle(_make_rl(50, resets_at))
    finally:
        _restore(real_now, real_cached)
        _prefs.prefs_path = real_prefs_path
        if prefs_file is not None:
            try:
                os.unlink(prefs_file)
            except OSError as exc:
                failures.append(f"could not clean up prefs file: {exc}")

    if "/" not in result:
        failures.append(f"verbose weekly_needle should contain '/', got {result!r}")


def _check_weekly_needle_exception_path(failures):
    """Cover the except Exception: return '' path (lines 300-301)."""

    class _Broken:
        def get(self, key, default=None):
            raise RuntimeError("injected")

    result = pace.weekly_needle(_Broken())
    if result != "":
        failures.append(f"weekly_needle exception should return '', got {result!r}")


class _StatRaisingEntry:
    """DirEntry stand-in whose stat() raises, for the unreadable-mtime branch."""

    name = "sess1.jsonl"
    path = os.path.join("nowhere", "sess1.jsonl")

    def is_dir(self, follow_symlinks=True):
        return False

    def stat(self, follow_symlinks=True):
        raise OSError("injected")


def _check_discover_pace_groups_oserror(failures):
    """Cover the OSError arms of the scandir walk: a missing/unreadable
    directory yields no entries, and an unreadable mtime skips the file
    instead of crashing the walk."""
    win_start = 1_700_000_000.0

    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "does-not-exist")
        if pace._scandir_entries(missing) != []:
            failures.append("_scandir_entries on a missing dir should return []")
        # A root that raises on scandir must fall out of discovery entirely.
        if pace._discover_pace_groups([missing], win_start):
            failures.append("missing root should produce no groups")

    if pace._entry_in_window(_StatRaisingEntry(), win_start):
        failures.append("_entry_in_window with a raising stat() should be False")

    # The stat-raises entry flows through the parent-file arm as a skip.
    with patch(
        "statusline_lib.pace._scandir_entries",
        side_effect=[[_FakeDirEntry("slug1")], [_StatRaisingEntry()]],
    ):
        groups = pace._discover_pace_groups(["fake-root"], win_start)
    if groups:
        failures.append(
            f"OSError in stat should skip files; got groups={list(groups.keys())}"
        )


class _FakeDirEntry:
    """Directory-shaped DirEntry stand-in for driving the walk without disk."""

    def __init__(self, name):
        self.name = name
        self.path = os.path.join("fake-root", name)

    def is_dir(self, follow_symlinks=True):
        return True


def _check_discover_pace_groups_skips_non_matches(failures):
    """Stray files at the root level (not slug dirs), non-jsonl files inside a
    slug dir, and non-agent files inside subagents/ are all skipped."""
    win_start = 1_700_000_000.0
    fresh = (win_start + 100, win_start + 100)

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        slug_dir = os.path.join(root, "slug1")
        sub_dir = os.path.join(slug_dir, "sess1", "subagents")
        os.makedirs(sub_dir)

        stray_root_file = os.path.join(root, "README.txt")
        stray_slug_file = os.path.join(slug_dir, "notes.txt")
        stray_sub_file = os.path.join(sub_dir, "not-an-agent.jsonl")
        keeper = os.path.join(sub_dir, "agent-x.jsonl")
        for path in (stray_root_file, stray_slug_file, stray_sub_file, keeper):
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
            os.utime(path, fresh)

        groups = pace._discover_pace_groups([root], win_start)

    paths = [p for group in groups.values() for p in group]
    if [os.path.basename(p) for p in paths] != ["agent-x.jsonl"]:
        failures.append(f"non-matching entries should be skipped, got {paths!r}")


def _check_discover_pace_groups_subagent(failures):
    """Subagent transcripts (slug/session/subagents/agent-*.jsonl) group under
    their parent session's (slug, session_id) key alongside the parent JSONL,
    and files whose mtime predates the window are skipped (both globs).
    Fixture-only with pinned mtimes - must not depend on live ~/.claude data."""
    win_start = 1_700_000_000.0
    fresh = (win_start + 100, win_start + 100)
    stale = (win_start - 100, win_start - 100)

    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        slug_dir = os.path.join(root, "slug1")
        os.makedirs(slug_dir)
        parent_path = os.path.join(slug_dir, "sess1.jsonl")
        with open(parent_path, "w", encoding="utf-8") as f:
            f.write("")
        os.utime(parent_path, fresh)

        sub_dir = os.path.join(root, "slug1", "sess1", "subagents")
        os.makedirs(sub_dir)
        agent_path = os.path.join(sub_dir, "agent-x.jsonl")
        with open(agent_path, "w", encoding="utf-8") as f:
            f.write("")
        os.utime(agent_path, fresh)

        # Too-old siblings in BOTH glob shapes: must be mtime-filtered out.
        stale_parent = os.path.join(slug_dir, "old.jsonl")
        with open(stale_parent, "w", encoding="utf-8") as f:
            f.write("")
        os.utime(stale_parent, stale)
        stale_agent = os.path.join(sub_dir, "agent-old.jsonl")
        with open(stale_agent, "w", encoding="utf-8") as f:
            f.write("")
        os.utime(stale_agent, stale)

        groups = pace._discover_pace_groups([root], win_start)

    if set(groups) != {("slug1", "sess1")}:
        failures.append(
            f"subagent grouping: expected one (slug1, sess1) group, got {list(groups)!r}"
        )
    paths = groups.get(("slug1", "sess1")) or []
    if sorted(os.path.basename(p) for p in paths) != ["agent-x.jsonl", "sess1.jsonl"]:
        failures.append(
            f"subagent grouping: parent + agent JSONL should share the group, got {paths!r}"
        )


def check(failures):
    _check_weekly_deltas_none_guards(failures)
    _check_weekly_deltas_cumulative_none(failures)
    _check_weekly_needle_deltas_none(failures)
    _check_weekly_needle_verbose(failures)
    _check_weekly_needle_exception_path(failures)
    _check_discover_pace_groups_oserror(failures)
    _check_discover_pace_groups_skips_non_matches(failures)
    _check_discover_pace_groups_subagent(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: _weekly_deltas guards; weekly_needle verbose/exception; "
        "_discover_pace_groups parent+subagent grouping and OSError skips"
    )


if __name__ == "__main__":
    main()
