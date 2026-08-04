"""Verify statusline_lib/qwen_quota.py - the Qwen Code plan-quota field.

Covers: week_start_unix's Monday-00:00-UTC+08:00 math, the tolerant
usage-jsonl line parse, anchor parsing/validity, the anchored window math
(_five_hour_used decay phases, _week_used reset crossing), the dual-predicate
usage walk (a window spanning two months, an unreadable file), the SWR cache
contract (miss/fresh/stale/anchor-mismatch), the limit pref arms, the plan
gate, and format_qwen_quota's render scenarios.

Fixtures build their own temp HOME (~/.qwen/usage/...), prefs file, and
cache file; the clock is pinned through the qwen_quota._now_unix and
pace._now_unix seams (AGENTS.md: never assert on real wall time).

Run from anywhere; imports from `agent-statusline` by path.
"""

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.pace as pace_module
import statusline_lib.qwen as qwen_module
import statusline_lib.qwen_quota as qwen_quota
from statusline_lib.qwen import render_qwen_statusline
from statusline_lib.qwen_quota import (
    _anchor,
    _count_window_calls,
    _five_hour_used,
    _limit,
    _plan_gate,
    _qwen_quota_cached,
    _record_timestamp_unix,
    _week_used,
    format_qwen_quota,
    refresh_qwen_quota_cache,
    week_start_unix,
)

_PLUS_8 = timezone(timedelta(hours=8))
# Thursday 2026-08-06 12:00 UTC+8: exactly 3.5 days past the Monday weekly
# reset, so a 50%-utilized weekly window paces to +0.0h.
NOW = int(datetime(2026, 8, 6, 12, 0, 0, tzinfo=_PLUS_8).timestamp())
WEEK_START = int(datetime(2026, 8, 3, 0, 0, 0, tzinfo=_PLUS_8).timestamp())
FIVE_H = 5 * 3600

# The unpatched seam, captured before any fixture rebinds the module
# attribute - exercising it covers the real time.time() body.
_REAL_NOW_UNIX = qwen_quota._now_unix

_PATCHED_ENV_KEYS = (
    "HOME",
    "USERPROFILE",
    "STATUSLINE_PLATFORM",
    "STATUSLINE_PREFS_PATH",
    "STATUSLINE_QWEN_QUOTA_5H",
    "STATUSLINE_QWEN_QUOTA_WEEKLY",
    "STATUSLINE_QWEN_QUOTA_ANCHOR",
    "BAILIAN_TOKEN_PLAN_API_KEY",
    "BAILIAN_CODING_PLAN_API_KEY",
)


class _Fixture:
    """Temp HOME + qwen platform + isolated prefs + pinned clocks. Every
    patched env key and module seam is restored on exit."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="qwen-quota-test-")
        self.prefs_path = os.path.join(self.home, "prefs.json")
        self._saved_env = {}
        self._saved_now = None
        self._saved_pace_now = None

    def __enter__(self):
        for key in _PATCHED_ENV_KEYS:
            self._saved_env[key] = os.environ.pop(key, None)
        os.environ["HOME"] = self.home
        os.environ["USERPROFILE"] = self.home
        os.environ["STATUSLINE_PLATFORM"] = "qwen"
        os.environ["STATUSLINE_PREFS_PATH"] = self.prefs_path
        self._saved_now = qwen_quota._now_unix
        self._saved_pace_now = pace_module._now_unix
        qwen_quota._now_unix = lambda: NOW
        pace_module._now_unix = lambda: NOW
        return self

    def __exit__(self, *_exc_info):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        qwen_quota._now_unix = self._saved_now
        pace_module._now_unix = self._saved_pace_now

    def set_env(self, **kwargs):
        for key, value in kwargs.items():
            os.environ[key] = value

    def write_prefs(self, data):
        with open(self.prefs_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def usage_path(self, month):
        usage_dir = os.path.join(self.home, ".qwen", "usage")
        os.makedirs(usage_dir, exist_ok=True)
        return os.path.join(usage_dir, f"token-usage-{month}.jsonl")

    def write_usage(self, month, timestamps):
        path = self.usage_path(month)
        lines = [json.dumps({"timestamp": _iso(ts)}) for ts in timestamps]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def cache_path(self):
        return os.path.join(self.home, ".qwen", ".statusline-qwen-quota-cache-v2.json")

    def write_cache(self, payload):
        os.makedirs(os.path.dirname(self.cache_path()), exist_ok=True)
        stamped = dict(payload)
        stamped.setdefault("cached_at_unix", NOW - 1)
        with open(self.cache_path(), "w", encoding="utf-8") as f:
            json.dump(stamped, f)


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


def _write_records(fx, timestamps):
    """Write each record into the monthly file the module itself will read:
    the CLI keys usage files by LOCAL month, so routing through the module's
    own _local_month keeps the fixture correct in any host timezone."""
    by_month = {}
    for ts in timestamps:
        by_month.setdefault(qwen_quota._local_month(ts), []).append(ts)
    for month, month_timestamps in by_month.items():
        fx.write_usage(month, month_timestamps)


def _check_now_unix_seam(failures):
    value = _REAL_NOW_UNIX()
    if not isinstance(value, (int, float)) or value <= 0:
        failures.append(f"_now_unix() real seam returned {value!r}")


def _check_week_start(failures):
    cases = [
        (NOW, WEEK_START),  # Thursday noon -> that week's Monday 00:00
        (WEEK_START, WEEK_START),  # exactly on the reset boundary
        (WEEK_START - 60, WEEK_START - 7 * 86400),  # Sunday 23:59 -> prev week
        (
            datetime(2026, 8, 3, 0, 0, 0, tzinfo=UTC).timestamp(),
            WEEK_START,  # Monday 08:00 UTC+8 -> still that Monday's reset
        ),
    ]
    for now, expected in cases:
        got = week_start_unix(now)
        if abs(got - expected) > 1e-6:
            failures.append(
                f"week_start_unix({now}) = {got}, expected {expected} "
                f"(diff {got - expected})"
            )


def _check_record_timestamp(failures):
    good = _record_timestamp_unix('{"timestamp": "2026-08-04T00:50:51.076Z"}')
    if good is None:
        failures.append("a valid Z timestamp should parse")
    for bad in (
        "",
        "{not json",
        "[1, 2]",
        '{"no_ts": 1}',
        '{"timestamp": 42}',
        '{"timestamp": "yesterday"}',
    ):
        if _record_timestamp_unix(bad) is not None:
            failures.append(f"line {bad!r} should parse to None")


def _check_anchor_parse(failures):
    with _Fixture() as fx:
        if _anchor() is not None:
            failures.append("no anchor pref must parse to None")
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": f"6000,20000@{NOW}"})
        parsed = _anchor()
        if parsed != (6000, 20000, float(NOW)):
            failures.append(f"valid anchor parsed wrong: {parsed!r}")
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": "garbage"})
        if _anchor() is not None:
            failures.append("a malformed anchor must parse to None")
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": "1,2@0"})
        if _anchor() is not None:
            failures.append("a zero-timestamp anchor must parse to None")
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": f"1,2@{NOW + 3600}"})
        if _anchor() is not None:
            failures.append("a future-dated anchor must parse to None")


def _check_count_window_calls(failures):
    with _Fixture() as fx:
        since_anchor = NOW - 5400  # anchored 1.5h ago
        in_both = [NOW - 600, NOW - 4000]  # in 5h AND since anchor
        five_only = [NOW - 7200]  # in 5h, before the anchor
        outside = [NOW - 20000, WEEK_START - 3600]  # in neither
        _write_records(fx, in_both + five_only + outside)
        # Malformed lines appended to the newest month file - must be skipped.
        newest_month = qwen_quota._local_month(NOW)
        with open(fx.usage_path(newest_month), "a", encoding="utf-8") as f:
            f.write("\n{broken\n" + "[1, 2]\n" + '{"no_ts": 1}\n')

        calls_since, calls_5h = _count_window_calls(NOW, since_anchor)
        if calls_since != 2:
            failures.append(f"calls_since_anchor = {calls_since}, expected 2")
        if calls_5h != 3:
            failures.append(f"calls_5h = {calls_5h}, expected 3")


def _check_count_cross_month_and_unreadable(failures):
    # Local day-1 00:30: the lookback reaches into the PREVIOUS local month,
    # whatever the host timezone, so the read set spans two monthly files.
    naive_local = datetime.fromtimestamp(NOW)
    boundary_now = int(
        datetime(naive_local.year, naive_local.month, 1, 0, 30).timestamp()
    )
    current_month = qwen_quota._local_month(boundary_now)
    previous_month = qwen_quota._local_month(boundary_now - FIVE_H)
    if current_month == previous_month:
        failures.append("fixture precondition broken: boundary months collide")
        return
    with _Fixture() as fx:
        _write_records(fx, [boundary_now - 3600, boundary_now - 2 * 3600])
        # The current month's file exists in the read set but is unreadable (a
        # directory): the OSError arm must contribute nothing, not crash.
        os.makedirs(fx.usage_path(current_month))
        calls_since, calls_5h = _count_window_calls(boundary_now, boundary_now - FIVE_H)
        if calls_since != 2 or calls_5h != 2:
            failures.append(
                f"cross-month counts = ({calls_since}, {calls_5h}), expected (2, 2)"
            )


def _check_five_hour_used_decay(failures):
    entry = {"calls_since_anchor": 100, "calls_5h": 50}
    at_anchor = (6000, 20000, NOW)
    got = _five_hour_used(entry, at_anchor, NOW)
    if abs(got - 6100) > 1e-6:  # 6000 * decay(1.0) + 100
        failures.append(f"at-anchor five_hour_used = {got}, expected 6100")
    mid_decay = (6000, 20000, NOW - int(2.5 * 3600))
    got = _five_hour_used(entry, mid_decay, NOW)
    if abs(got - 3100) > 1e-6:  # 6000 * decay(0.5) + 100
        failures.append(f"mid-decay five_hour_used = {got}, expected 3100")
    post_decay = (6000, 20000, NOW - 6 * 3600)
    got = _five_hour_used(entry, post_decay, NOW)
    if abs(got - 50) > 1e-6:  # window fully post-anchor -> pure calls_5h
        failures.append(f"post-decay five_hour_used = {got}, expected 50")


def _check_week_used(failures):
    entry = {"calls_since_anchor": 100}
    in_window = (6000, 20000, NOW)  # Thursday anchor, after Monday reset
    got = _week_used(entry, in_window, NOW)
    if got != 20100:
        failures.append(f"in-window week_used = {got}, expected 20100")
    crossed = (6000, 20000, WEEK_START - 3600)  # anchored before the reset
    got = _week_used(entry, crossed, NOW)
    if got is not None:
        failures.append(f"reset-crossed week_used = {got}, expected None")


class _SpawnRecorder:
    """Swap qwen_quota.maybe_spawn_refresh for a recorder so cache tests can
    assert spawn behavior without launching detached children."""

    def __init__(self):
        self.calls = []
        self._original = None

    def __enter__(self):
        self._original = qwen_quota.maybe_spawn_refresh
        qwen_quota.maybe_spawn_refresh = lambda kind, argument: self.calls.append(
            (kind, argument)
        )
        return self

    def __exit__(self, *_exc_info):
        qwen_quota.maybe_spawn_refresh = self._original


def _check_cache_swr_contract(failures):
    anchor_key = f"6000,20000@{NOW}"
    with _Fixture() as fx, _SpawnRecorder() as spawner:
        if _qwen_quota_cached(NOW, anchor_key) is not None:
            failures.append("a missing cache must read as None")
        if spawner.calls != [("qwen-quota", 0)]:
            failures.append(f"a missing cache must spawn, got {spawner.calls!r}")

        fresh = {"anchor_key": anchor_key, "cached_at_unix": NOW - 1}
        fx.write_cache(fresh)
        spawner.calls.clear()
        entry = _qwen_quota_cached(NOW, anchor_key)
        if entry is None or entry.get("anchor_key") != anchor_key:
            failures.append(f"a fresh cache must be served, got {entry!r}")
        if spawner.calls:
            failures.append("a fresh cache must not spawn")

        stale = {"anchor_key": anchor_key, "cached_at_unix": NOW - 100}
        fx.write_cache(stale)
        spawner.calls.clear()
        entry = _qwen_quota_cached(NOW, anchor_key)
        if entry is None:
            failures.append("a stale cache must still be served (SWR)")
        if spawner.calls != [("qwen-quota", 0)]:
            failures.append(f"a stale cache must spawn, got {spawner.calls!r}")

        mismatch = {"anchor_key": "OTHER", "cached_at_unix": NOW - 1}
        fx.write_cache(mismatch)
        spawner.calls.clear()
        if _qwen_quota_cached(NOW, anchor_key) is not None:
            failures.append("a cache keyed to a different anchor reads as a miss")
        if spawner.calls != [("qwen-quota", 0)]:
            failures.append(f"an anchor mismatch must spawn, got {spawner.calls!r}")


def _check_refresh_writes_cache(failures):
    with _Fixture() as fx:
        refresh_qwen_quota_cache(0.0)  # no anchor -> no cache written
        if os.path.exists(fx.cache_path()):
            failures.append("refresh without an anchor must not write a cache")
        # Anchor 1.5h in the past so both records fall after it.
        anchor_key = f"6000,20000@{NOW - 5400}"
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": anchor_key})
        _write_records(fx, [NOW - 600, NOW - 4000])
        refresh_qwen_quota_cache(0.0)
        with open(fx.cache_path(), encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("anchor_key") != anchor_key:
            failures.append(f"refresh anchor_key wrong: {cache!r}")
        if cache.get("calls_5h") != 2 or cache.get("calls_since_anchor") != 2:
            failures.append(f"refresh counts wrong: {cache!r}")


def _check_limit_arms(failures):
    with _Fixture() as fx:
        if _limit("STATUSLINE_QWEN_QUOTA_5H", 12000) != 12000:
            failures.append("an unset pref must fall back to the default")
        fx.set_env(STATUSLINE_QWEN_QUOTA_5H="42")
        if _limit("STATUSLINE_QWEN_QUOTA_5H", 12000) != 42:
            failures.append("a numeric pref must parse to its int")
        for off in ("0", "off"):
            fx.set_env(STATUSLINE_QWEN_QUOTA_5H=off)
            if _limit("STATUSLINE_QWEN_QUOTA_5H", 12000) is not None:
                failures.append(f"{off!r} must hide the horizon (None)")
        fx.set_env(STATUSLINE_QWEN_QUOTA_5H="garbage")
        if _limit("STATUSLINE_QWEN_QUOTA_5H", 12000) != 12000:
            failures.append("an unparseable pref must fall back to the default")
        fx.set_env(STATUSLINE_QWEN_QUOTA_5H="-3")
        if _limit("STATUSLINE_QWEN_QUOTA_5H", 12000) is not None:
            failures.append("a negative pref must hide the horizon (None)")


def _check_plan_gate(failures):
    with _Fixture() as fx:
        if _plan_gate():
            failures.append("no plan key + no explicit limits must stay closed")
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY="sk-sp-x")
        if not _plan_gate():
            failures.append("a token-plan key must open the gate")
        os.environ.pop("BAILIAN_TOKEN_PLAN_API_KEY")
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": "1,2@3"})
        if not _plan_gate():
            failures.append("an explicit anchor pref must open the gate")
        os.remove(fx.prefs_path)
        fx.set_env(STATUSLINE_QWEN_QUOTA_WEEKLY="40000")
        if not _plan_gate():
            failures.append("an explicit env limit must open the gate")


def _check_format_scenarios(failures):
    anchor_key = f"6000,20000@{NOW}"
    # Non-qwen platform: no field, no side effects.
    with _Fixture() as fx, _SpawnRecorder() as spawner:
        os.environ.pop("STATUSLINE_PLATFORM")
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": anchor_key})
        if format_qwen_quota() != "":
            failures.append("a non-qwen platform must render no quota field")
        if spawner.calls:
            failures.append("a non-qwen platform must not spawn a refresh")

    # Plan gate closed.
    with _Fixture() as fx:
        fx.write_prefs({})  # empty prefs, no plan key, no anchor
        if format_qwen_quota() != "":
            failures.append("a closed plan gate must render no quota field")

    # No anchor.
    with _Fixture() as fx:
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY="sk-sp-x")
        if format_qwen_quota() != "":
            failures.append("no anchor must render no quota field")

    # Both horizons switched off.
    with _Fixture() as fx:
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY="sk-sp-x")
        fx.write_prefs(
            {
                "STATUSLINE_QWEN_QUOTA_ANCHOR": anchor_key,
                "STATUSLINE_QWEN_QUOTA_5H": "0",
                "STATUSLINE_QWEN_QUOTA_WEEKLY": "off",
            }
        )
        if format_qwen_quota() != "":
            failures.append("two hidden horizons must render no quota field")

    # Cold cache: honest absence plus a spawn to warm it.
    with _Fixture() as fx, _SpawnRecorder() as spawner:
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY="sk-sp-x")
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": anchor_key})
        if format_qwen_quota() != "":
            failures.append("a cold cache must render no quota field")
        if spawner.calls != [("qwen-quota", 0)]:
            failures.append(f"a cold cache must spawn, got {spawner.calls!r}")


def _check_format_warm_render(failures):
    anchor_key = f"6000,20000@{NOW}"
    with _Fixture() as fx, _SpawnRecorder() as spawner:
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY="sk-sp-x")
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": anchor_key})
        fx.write_cache(
            {
                "anchor_key": anchor_key,
                "anchored_at_unix": NOW,
                "calls_since_anchor": 0,
                "calls_5h": 0,
                "cached_at_unix": NOW - 1,
            }
        )
        rendered = format_qwen_quota()
        for needle in ("5h: ", "wk: ", "+5.0h", "+0.0h"):
            if needle not in rendered:
                failures.append(f"warm render missing {needle!r}: {rendered!r}")
        if rendered.count("50%") != 2:
            failures.append(
                f"both horizons at half their limits must show 50%: {rendered!r}"
            )
        if spawner.calls:
            failures.append("a fresh cache render must not spawn")


def _check_format_degraded_cache(failures):
    anchor_key = f"6000,20000@{NOW}"
    # Torn cache: counts missing -> degrade to the bare anchor values.
    with _Fixture() as fx:
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY="sk-sp-x")
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": anchor_key})
        fx.write_cache(
            {
                "anchor_key": anchor_key,
                "anchored_at_unix": NOW,
                "cached_at_unix": NOW - 1,
            }
        )
        rendered = format_qwen_quota()
        if "5h: " not in rendered or "wk: " not in rendered:
            failures.append(
                f"a torn cache must still render both horizons: {rendered!r}"
            )


def _check_pace_and_horizon_guards(failures):
    # The None/zero guard branches are defensive; exercise them directly so
    # the coverage gate (no exclusions) holds on both OSes.
    if qwen_quota._rolling_pace_part(0, 12000) != "":
        failures.append("_rolling_pace_part at zero usage must be ''")
    if qwen_quota._rolling_pace_part(100, None) != "":
        failures.append("_rolling_pace_part with hidden limit must be ''")
    if qwen_quota._weekly_pace_part(None, 40000, NOW) != "":
        failures.append("_weekly_pace_part with no usage must be ''")
    if qwen_quota._weekly_pace_part(100, None, NOW) != "":
        failures.append("_weekly_pace_part with hidden limit must be ''")
    if qwen_quota._horizon("5h", None, 12000, "") != "":
        failures.append("_horizon with no usage must be ''")
    if qwen_quota._horizon("5h", 100, None, "") != "":
        failures.append("_horizon with hidden limit must be ''")


def _check_render_integration(failures):
    payload = {"context_window": {"context_window_size": 1000, "current_usage": 10}}
    originals = {
        name: getattr(qwen_module, name)
        for name in (
            "count_active_sessions",
            "debounce_session_count",
            "format_render_suffix",
            "format_qwen_quota",
        )
    }
    try:
        qwen_module.count_active_sessions = lambda cwd: 1
        qwen_module.debounce_session_count = lambda raw_count, cwd: raw_count
        qwen_module.format_render_suffix = lambda session_id: ""
        qwen_module.format_qwen_quota = lambda: "5h: 50% +5.0h wk: 50% +0.0h"
        _line1, line2 = render_qwen_statusline(payload, "/tmp", "|")
        if "5h: 50% +5.0h wk: 50% +0.0h" not in line2:
            failures.append(f"line2 must carry the quota field, got {line2!r}")
        _line1, line2 = render_qwen_statusline({}, "/tmp", "|")
        if line2 != "5h: 50% +5.0h wk: 50% +0.0h":
            failures.append(f"with only quota to show, it IS line2, got {line2!r}")
        qwen_module.format_qwen_quota = lambda: ""
        _line1, line2 = render_qwen_statusline(payload, "/tmp", "|")
        if "5h: " in line2:
            failures.append(f"an empty quota field must not leak: {line2!r}")
    finally:
        for name, original in originals.items():
            setattr(qwen_module, name, original)


def check(failures):
    _check_now_unix_seam(failures)
    _check_week_start(failures)
    _check_record_timestamp(failures)
    _check_anchor_parse(failures)
    _check_count_window_calls(failures)
    _check_count_cross_month_and_unreadable(failures)
    _check_five_hour_used_decay(failures)
    _check_week_used(failures)
    _check_cache_swr_contract(failures)
    _check_refresh_writes_cache(failures)
    _check_limit_arms(failures)
    _check_plan_gate(failures)
    _check_format_scenarios(failures)
    _check_format_warm_render(failures)
    _check_format_degraded_cache(failures)
    _check_pace_and_horizon_guards(failures)
    _check_render_integration(failures)


def main():
    failures = []
    check(failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(1)
    print(
        "OK: statusline_lib/qwen_quota.py anchors to the dashboard, layers "
        "local deltas, decays the rolling window, and degrades honestly"
    )


if __name__ == "__main__":
    main()
