"""Shared fixture and low-level math/anchor checks for verify_qwen_quota.py.

Underscore-prefixed so the CI runner glob (`scripts/verify_*.py`) does not
execute this helper file as a standalone test suite.
"""

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta, timezone

import statusline_lib.pace as pace_module
import statusline_lib.qwen_quota as qwen_quota
from statusline_lib.qwen_quota import (
    _anchor,
    _anchor_exhausted,
    _count_window_calls,
    _five_hour_used,
    _hold_clearance_unix,
    _limit,
    _record_timestamp_unix,
    _week_used,
    week_start_unix,
)

_PLUS_8 = timezone(timedelta(hours=8))
# Thursday 2026-08-06 12:00 UTC+8: exactly 3.5 days past the Monday weekly
# reset, so a 50%-utilized weekly window paces to +0.0h.
NOW = int(datetime(2026, 8, 6, 12, 0, 0, tzinfo=_PLUS_8).timestamp())
WEEK_START = int(datetime(2026, 8, 3, 0, 0, 0, tzinfo=_PLUS_8).timestamp())
FIVE_H = 5 * 3600
_FIVE_HOUR_LIMIT = 12_000
_TEST_API_KEY = "sk-sp-x"
_TEXT_ENCODING = "utf-8"

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
        with open(self.prefs_path, "w", encoding=_TEXT_ENCODING) as f:
            json.dump(data, f)

    def usage_path(self, month):
        usage_dir = os.path.join(self.home, ".qwen", "usage")
        os.makedirs(usage_dir, exist_ok=True)
        return os.path.join(usage_dir, f"token-usage-{month}.jsonl")

    def write_usage(self, month, timestamps):
        path = self.usage_path(month)
        lines = [json.dumps({"timestamp": _iso(ts)}) for ts in timestamps]
        with open(path, "w", encoding=_TEXT_ENCODING) as f:
            f.write("\n".join(lines) + "\n")
        return path

    def cache_path(self):
        return os.path.join(self.home, ".qwen", ".statusline-qwen-quota-cache-v2.json")

    def write_cache(self, payload):
        os.makedirs(os.path.dirname(self.cache_path()), exist_ok=True)
        stamped = dict(payload)
        stamped.setdefault("cached_at_unix", NOW - 1)
        with open(self.cache_path(), "w", encoding=_TEXT_ENCODING) as f:
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
        with open(fx.usage_path(newest_month), "a", encoding=_TEXT_ENCODING) as f:
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


def _check_five_hour_used_exhausted_hold(failures):
    # Exhausted anchor (anchor_5h >= limit): held flat at the anchored value
    # across the 5h after anchoring, regardless of local deltas.
    entry = {"calls_since_anchor": 300, "calls_5h": 42}
    exhausted = (_FIVE_HOUR_LIMIT, 20000, NOW)
    # At anchor (elapsed=0): held at 12000, delta NOT added.
    got = _five_hour_used(exhausted, exhausted, NOW, five_hour_limit=_FIVE_HOUR_LIMIT)
    if abs(got - _FIVE_HOUR_LIMIT) > 1e-6:
        failures.append(f"exhausted at-anchor five_hour_used = {got}, expected 12000")
    # 43 minutes in (the observed bug scenario): still 12000, not decayed.
    got = _five_hour_used(
        entry,
        exhausted,
        NOW - 43 * 60,
        five_hour_limit=_FIVE_HOUR_LIMIT,
    )
    if abs(got - _FIVE_HOUR_LIMIT) > 1e-6:
        failures.append(f"exhausted 43min-in five_hour_used = {got}, expected 12000")
    # 4 hours in: still held flat.
    got = _five_hour_used(
        entry,
        exhausted,
        NOW - 4 * 3600,
        five_hour_limit=_FIVE_HOUR_LIMIT,
    )
    if abs(got - _FIVE_HOUR_LIMIT) > 1e-6:
        failures.append(f"exhausted 4h-in five_hour_used = {got}, expected 12000")
    # Anchor above the limit (over-exhausted): also held flat.
    over = (15000, 20000, NOW)
    got = _five_hour_used(entry, over, NOW, five_hour_limit=_FIVE_HOUR_LIMIT)
    if abs(got - 15000) > 1e-6:
        failures.append(f"over-exhausted five_hour_used = {got}, expected 15000")
    # Predicate and clearance helpers agree with the held behavior.
    if not _anchor_exhausted(_FIVE_HOUR_LIMIT, _FIVE_HOUR_LIMIT):
        failures.append("anchor == limit must be exhausted")
    if not _anchor_exhausted(15000, _FIVE_HOUR_LIMIT):
        failures.append("anchor > limit must be exhausted")
    if _anchor_exhausted(11999, _FIVE_HOUR_LIMIT):
        failures.append("anchor < limit must NOT be exhausted")
    if _anchor_exhausted(_FIVE_HOUR_LIMIT, None):
        failures.append("hidden limit must NOT be exhausted")
    clearance = _hold_clearance_unix(exhausted, _FIVE_HOUR_LIMIT, NOW - 43 * 60)
    # Clearance is anchored_at + 5h, and exhausted's anchored_at is NOW.
    expected_clearance = NOW + FIVE_H
    if clearance is None or abs(clearance - expected_clearance) > 1e-6:
        failures.append(f"hold clearance = {clearance}, expected {expected_clearance}")
    post_window_anchor = (_FIVE_HOUR_LIMIT, 20000, NOW - 6 * 3600)
    if _hold_clearance_unix(post_window_anchor, _FIVE_HOUR_LIMIT, NOW) is not None:
        failures.append("post-window clearance must be None")
    if _hold_clearance_unix((6000, 20000, NOW), _FIVE_HOUR_LIMIT, NOW) is not None:
        failures.append("non-exhausted anchor clearance must be None")


def _check_five_hour_used_exhausted_post_window(failures):
    # Exhausted anchor with elapsed >= 5h: falls through to pure local
    # calls_5h, no hold.
    entry = {"calls_since_anchor": 300, "calls_5h": 42}
    exhausted = (_FIVE_HOUR_LIMIT, 20000, NOW - 6 * 3600)
    got = _five_hour_used(entry, exhausted, NOW, five_hour_limit=_FIVE_HOUR_LIMIT)
    if abs(got - 42) > 1e-6:
        failures.append(f"exhausted post-window five_hour_used = {got}, expected 42")


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


def _check_limit_arms(failures):
    with _Fixture() as fx:
        if _limit("STATUSLINE_QWEN_QUOTA_5H", _FIVE_HOUR_LIMIT) != _FIVE_HOUR_LIMIT:
            failures.append("an unset pref must fall back to the default")
        fx.set_env(STATUSLINE_QWEN_QUOTA_5H="42")
        if _limit("STATUSLINE_QWEN_QUOTA_5H", _FIVE_HOUR_LIMIT) != 42:
            failures.append("a numeric pref must parse to its int")
        for off in ("0", "off"):
            fx.set_env(STATUSLINE_QWEN_QUOTA_5H=off)
            if _limit("STATUSLINE_QWEN_QUOTA_5H", _FIVE_HOUR_LIMIT) is not None:
                failures.append(f"{off!r} must hide the horizon (None)")
        fx.set_env(STATUSLINE_QWEN_QUOTA_5H="garbage")
        if _limit("STATUSLINE_QWEN_QUOTA_5H", _FIVE_HOUR_LIMIT) != _FIVE_HOUR_LIMIT:
            failures.append("an unparseable pref must fall back to the default")
        fx.set_env(STATUSLINE_QWEN_QUOTA_5H="-3")
        if _limit("STATUSLINE_QWEN_QUOTA_5H", _FIVE_HOUR_LIMIT) is not None:
            failures.append("a negative pref must hide the horizon (None)")


def check_qwen_quota_math(failures):
    _check_now_unix_seam(failures)
    _check_week_start(failures)
    _check_record_timestamp(failures)
    _check_anchor_parse(failures)
    _check_count_window_calls(failures)
    _check_count_cross_month_and_unreadable(failures)
    _check_five_hour_used_decay(failures)
    _check_five_hour_used_exhausted_hold(failures)
    _check_five_hour_used_exhausted_post_window(failures)
    _check_week_used(failures)
    _check_limit_arms(failures)
