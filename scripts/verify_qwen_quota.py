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

Low-level math/anchor checks and the shared fixture live in the sibling
_qwen_quota_harness.py to keep each file under the 400-line ceiling.

Run from anywhere; imports from `agent-statusline` by path.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _qwen_quota_harness import (
    _FIVE_HOUR_LIMIT,
    _TEST_API_KEY,
    _TEXT_ENCODING,
    FIVE_H,
    NOW,
    _Fixture,
    _write_records,
    check_qwen_quota_math,
)

import statusline_lib.pace as pace_module
import statusline_lib.qwen as qwen_module
import statusline_lib.qwen_quota as qwen_quota
from statusline_lib.qwen import render_qwen_statusline
from statusline_lib.qwen_quota import (
    _plan_gate,
    _qwen_quota_cached,
    format_qwen_quota,
    refresh_qwen_quota_cache,
)


class _SpawnRecorder:
    """Swap qwen_quota.request_refresh for a recorder so cache tests can
    assert refresh-request behavior without triggering an actual worker-pool
    job."""

    def __init__(self):
        self.calls = []
        self._original = None

    def __enter__(self):
        self._original = qwen_quota.request_refresh
        qwen_quota.request_refresh = lambda kind, argument: self.calls.append(
            (kind, argument)
        )
        return self

    def __exit__(self, *_exc_info):
        qwen_quota.request_refresh = self._original


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
        with open(fx.cache_path(), encoding=_TEXT_ENCODING) as f:
            cache = json.load(f)
        if cache.get("anchor_key") != anchor_key:
            failures.append(f"refresh anchor_key wrong: {cache!r}")
        if cache.get("calls_5h") != 2 or cache.get("calls_since_anchor") != 2:
            failures.append(f"refresh counts wrong: {cache!r}")


def _check_plan_gate(failures):
    with _Fixture() as fx:
        if _plan_gate():
            failures.append("no plan key + no explicit limits must stay closed")
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY=_TEST_API_KEY)
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
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY=_TEST_API_KEY)
        if format_qwen_quota() != "":
            failures.append("no anchor must render no quota field")

    # Both horizons switched off.
    with _Fixture() as fx:
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY=_TEST_API_KEY)
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
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY=_TEST_API_KEY)
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": anchor_key})
        if format_qwen_quota() != "":
            failures.append("a cold cache must render no quota field")
        if spawner.calls != [("qwen-quota", 0)]:
            failures.append(f"a cold cache must spawn, got {spawner.calls!r}")


def _check_format_warm_render(failures):
    anchor_key = f"6000,20000@{NOW}"
    with _Fixture() as fx, _SpawnRecorder() as spawner:
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY=_TEST_API_KEY)
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
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY=_TEST_API_KEY)
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


def _check_format_exhausted_render(failures):
    # Exhausted anchor renders the 5h part with ~<clock> instead of the
    # normal pace suffix; the clock is anchored_at + 5h in local time.
    anchored_at = NOW - 43 * 60
    anchor_key = f"12000,20000@{anchored_at}"
    with _Fixture() as fx, _SpawnRecorder() as spawner:
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY=_TEST_API_KEY)
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": anchor_key})
        fx.write_cache(
            {
                "anchor_key": anchor_key,
                "anchored_at_unix": anchored_at,
                "calls_since_anchor": 50,
                "calls_5h": 10,
                "cached_at_unix": NOW - 1,
            }
        )
        rendered = format_qwen_quota()
        if "5h: " not in rendered:
            failures.append(f"exhausted render missing 5h: {rendered!r}")
        if "~" not in rendered:
            failures.append(f"exhausted render missing ~<clock> suffix: {rendered!r}")
        # The clock string must match _fmt_local_clock(anchored_at + 5h).
        expected_clock = pace_module._fmt_local_clock(anchored_at + FIVE_H)
        expected_suffix = f"~{expected_clock}"
        if expected_suffix not in rendered:
            failures.append(
                f"exhausted render missing {expected_suffix!r}: {rendered!r}"
            )
        # The normal +Hh pace suffix must NOT appear while holding.
        if "+5.0h" in rendered or "-5.0h" in rendered:
            failures.append(
                f"exhausted render must not carry the normal pace: {rendered!r}"
            )
        # Utilization is 100% (12000/12000).
        if "100%" not in rendered:
            failures.append(f"exhausted render must show 100%: {rendered!r}")
        if spawner.calls:
            failures.append(
                f"a fresh exhausted-cache render must not spawn: {spawner.calls!r}"
            )
    # Post-window exhausted anchor: normal pace suffix, no ~<clock>.
    anchor_key_post = f"12000,20000@{NOW - 6 * 3600}"
    with _Fixture() as fx:
        fx.set_env(BAILIAN_TOKEN_PLAN_API_KEY=_TEST_API_KEY)
        fx.write_prefs({"STATUSLINE_QWEN_QUOTA_ANCHOR": anchor_key_post})
        fx.write_cache(
            {
                "anchor_key": anchor_key_post,
                "anchored_at_unix": NOW - 6 * 3600,
                "calls_since_anchor": 0,
                "calls_5h": 0,
                "cached_at_unix": NOW - 1,
            }
        )
        rendered = format_qwen_quota()
        if "~" in rendered:
            failures.append(
                f"post-window exhausted render must not carry ~<clock>: {rendered!r}"
            )


def _check_pace_and_horizon_guards(failures):
    # The None/zero guard branches are defensive; exercise them directly so
    # the coverage gate (no exclusions) holds on both OSes.
    if qwen_quota._rolling_pace_part(0, _FIVE_HOUR_LIMIT) != "":
        failures.append("_rolling_pace_part at zero usage must be ''")
    if qwen_quota._rolling_pace_part(100, None) != "":
        failures.append("_rolling_pace_part with hidden limit must be ''")
    if qwen_quota._weekly_pace_part(None, 40000, NOW) != "":
        failures.append("_weekly_pace_part with no usage must be ''")
    if qwen_quota._weekly_pace_part(100, None, NOW) != "":
        failures.append("_weekly_pace_part with hidden limit must be ''")
    if qwen_quota._horizon("5h", None, _FIVE_HOUR_LIMIT, "") != "":
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
            "format_fable_quota",
        )
    }
    try:
        qwen_module.count_active_sessions = lambda cwd: 1
        qwen_module.debounce_session_count = lambda raw_count, cwd: raw_count
        qwen_module.format_render_suffix = lambda session_id: ""
        qwen_module.format_fable_quota = lambda: ""
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
    check_qwen_quota_math(failures)
    _check_cache_swr_contract(failures)
    _check_refresh_writes_cache(failures)
    _check_plan_gate(failures)
    _check_format_scenarios(failures)
    _check_format_warm_render(failures)
    _check_format_degraded_cache(failures)
    _check_format_exhausted_render(failures)
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
