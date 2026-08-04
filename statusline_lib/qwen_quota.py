"""Qwen Code plan-quota display: `5h: P% ±Hh wk: P% ±Hh` on line 2.

Data model - dashboard-anchored, because the quota key is shared:

Qwen Code's statusline payload carries no quota data, and the plan quota is
enforced server-side per API KEY with no query API (Alibaba docs confirm no
endpoint; the 429 error text is the only server-side signal). The CLI's
local usage records (~/.qwen/usage/token-usage-<YYYY-MM>.jsonl, one line per
model call, keyed by LOCAL month) would be the natural counter, but the
fleet's plan key is shared - other consumers accrue the bulk of the calls,
so local-only counting showed single-digit percents while the Model Studio
dashboard showed ~88% (observed 2026-08-04: 274 local calls in the 5h
window vs ~10.5k dashboard-implied). Local counting alone is therefore NOT a
valid numerator.

Instead the display is ANCHORED to the dashboard: the operator reads the
Model Studio console and stores the current window usage via
`statusline-ctl set qwen-quota-anchor "<5h_used>,<wk_used>"` (run with
STATUSLINE_PLATFORM=qwen so the pref lands in ~/.qwen), stored as
STATUSLINE_QWEN_QUOTA_ANCHOR = "<5h>,<wk>@<anchored_at_unix>". Between
anchors, locally recorded calls are layered on top:

- weekly window (fixed, resets Monday 00:00 UTC+08:00): exact while the
  anchor sits inside the current window - anchor_wk + local calls since the
  anchor (nothing expires inside a fixed window). Hidden once the week
  resets past the anchor, until re-anchored.
- five-hour window (rolling): when the anchor is BELOW the limit, the
  anchor's contribution decays linearly to zero across the 5h after
  anchoring (uniform-distribution estimate of the anchor population sliding
  out of the window) while local calls since the anchor are added; 5h past
  the anchor the window is fully post-anchor and the count is pure local.
  When the anchor is AT or ABOVE the limit (an exhausted anchor on the
  shared key), the displayed usage is HELD flat at the anchored value until
  anchored_at + 5h - the moment the anchored population has certainly
  expired out of the rolling window. With an exhausted anchor on a shared
  key, ongoing invisible traffic plausibly replaces every call that expires
  out of the window, so the conservative honest estimate is "still
  exhausted until the anchored population is provably gone". After the hold
  window the count falls through to pure local like the decay path, and
  while holding the pace suffix is replaced with the local clock time at
  which the window clears.

With no (or an unparseable) anchor the field renders nothing - a hidden
field beats a wrong one.

Limits default to this plan's tiers (12000/5h, 40000/wk) and are
overridable via STATUSLINE_QWEN_QUOTA_5H / STATUSLINE_QWEN_QUOTA_WEEKLY
(0/off hides the horizon).

Counting the local delta walks the monthly usage files (walk-priced at up to
~90k records/month), so the render path reads a small SWR cache and hands
recomputation to a detached "qwen-quota" refresh child
(refresh._REFRESHER_MODULES) - the same stale-while-revalidate shape as
pace-hourly. The cache entry is keyed to the anchor string, so re-anchoring
invalidates a stale entry instead of serving numbers computed against the
old anchor.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

from .base import app_dir, color_high_bad, platform_name
from .pace import _fmt_delta, _fmt_local_clock, _project_pace
from .prefs import load_prefs, pref
from .refresh import maybe_spawn_refresh
from .ttlcache import read_raw_cache, write_ttl_cache

_FIVE_HOUR_SECONDS = 5 * 3600
_WEEK_SECONDS = 7 * 86400
# The docs pin the weekly reset to a fixed offset (Monday 00:00:00
# UTC+08:00), not a local-timezone wall clock.
_WEEKLY_RESET_OFFSET = timezone(timedelta(hours=8))
_QUOTA_TTL_SECONDS = 15
_DEFAULT_FIVE_HOUR_LIMIT = 12000
_DEFAULT_WEEKLY_LIMIT = 40000
_PLAN_ENV_KEYS = ("BAILIAN_TOKEN_PLAN_API_KEY", "BAILIAN_CODING_PLAN_API_KEY")
_PREF_FIVE_HOUR = "STATUSLINE_QWEN_QUOTA_5H"
_PREF_WEEKLY = "STATUSLINE_QWEN_QUOTA_WEEKLY"
_PREF_ANCHOR = "STATUSLINE_QWEN_QUOTA_ANCHOR"
_LIMIT_OFF = ("0", "off", "false", "no")
_ANCHOR_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*@\s*(\d+(?:\.\d+)?)\s*$")


def _now_unix():
    """Current unix time. Seam so tests can pin the window/freshness clock."""
    return time.time()


def _quota_cache_path():
    return os.path.join(app_dir(), ".statusline-qwen-quota-cache-v2.json")


def week_start_unix(now_unix):
    """Unix time of the latest Monday 00:00:00 UTC+08:00 at or before
    `now_unix` - the start of the fixed weekly quota window."""
    local_now = datetime.fromtimestamp(now_unix, tz=_WEEKLY_RESET_OFFSET)
    monday = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday.timestamp()


def _local_month(ts):
    """The usage-file month key for a unix timestamp: the CLI names files by
    the record's LOCAL month (tokenUsageService getLocalDateParts), so the
    reader must derive keys in local time too, not UTC."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m")


def _usage_file_paths(span_start_unix, now_unix):
    """The monthly usage files covering [span_start, now]: the distinct local
    months the span touches (a window reaching back from a month boundary
    spans two files)."""
    months = {_local_month(ts) for ts in (now_unix, span_start_unix)}
    usage_dir = os.path.join(app_dir(), "usage")
    return [
        os.path.join(usage_dir, f"token-usage-{month}.jsonl")
        for month in sorted(months)
    ]


def _record_timestamp_unix(line):
    """One usage-jsonl line -> unix seconds, or None when the line is blank,
    malformed, or carries no parseable timestamp. Tolerant like the reader
    it serves: a torn last line must not sink the whole count."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _anchor():
    """The parsed dashboard anchor (five_hour_used, week_used, anchored_at),
    or None when the pref is absent, unparseable, or future-dated."""
    raw = pref(_PREF_ANCHOR)
    if raw is None:
        return None
    match = _ANCHOR_RE.match(raw)
    if not match:
        return None
    anchored_at = float(match.group(3))
    if anchored_at <= 0 or anchored_at > _now_unix() + 60:
        return None
    return int(match.group(1)), int(match.group(2)), anchored_at


def _count_window_calls(now_unix, since_anchor):
    """(calls_since_anchor, calls_5h) in one walk of the spanned files. A
    missing or unreadable file contributes nothing."""
    five_hours_ago = now_unix - _FIVE_HOUR_SECONDS
    span_start = min(since_anchor, five_hours_ago)
    calls_since_anchor = 0
    calls_5h = 0
    for path in _usage_file_paths(span_start, now_unix):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    ts = _record_timestamp_unix(line)
                    if ts is None or ts < span_start:
                        continue
                    if ts >= since_anchor:
                        calls_since_anchor += 1
                    if ts >= five_hours_ago:
                        calls_5h += 1
        except OSError:
            continue
    return calls_since_anchor, calls_5h


def refresh_qwen_quota_cache(_argument):
    """Detached-child recompute for refresh.py's "qwen-quota" kind: count the
    local deltas against the current anchor and persist the small cache the
    render path reads. Runs out of process, so the jsonl walk never blocks a
    render. No anchor -> nothing to count; no cache is written and the field
    stays hidden."""
    anchor = _anchor()
    if anchor is None:
        return
    _five_hour_used, _week_used, anchored_at = anchor
    now_unix = _now_unix()
    calls_since_anchor, calls_5h = _count_window_calls(now_unix, anchored_at)
    write_ttl_cache(
        _quota_cache_path(),
        {
            "anchor_key": pref(_PREF_ANCHOR),
            "anchored_at_unix": anchored_at,
            "calls_since_anchor": calls_since_anchor,
            "calls_5h": calls_5h,
        },
    )


def _qwen_quota_cached(now_unix, anchor_key):
    """SWR cache read: serve the entry stale-or-fresh and hand recomputation
    to a detached child when it is missing or past the TTL. None on a true
    miss - the field degrades away for a render or two until the write lands,
    same contract as the pace/spend caches. An entry computed against a
    DIFFERENT anchor string is served as a miss: numbers keyed to the old
    anchor would be wrong, not stale."""
    entry = read_raw_cache(_quota_cache_path())
    if entry is not None and entry.get("anchor_key") != anchor_key:
        entry = None
    if entry is None or now_unix - entry.get("cached_at_unix", 0) >= _QUOTA_TTL_SECONDS:
        maybe_spawn_refresh("qwen-quota", 0)
    return entry


def _limit(pref_name, default):
    """The window's call limit: positive int from the pref/env, the documented
    default when unset, None when the horizon is switched off or the value is
    unusable."""
    raw = pref(pref_name)
    if raw is None:
        return default
    norm = raw.strip().lower()
    if norm in _LIMIT_OFF:
        return None
    try:
        value = int(norm)
    except ValueError:
        return default
    return value if value > 0 else None


def _plan_gate():
    """True when a quota display is wanted: a plan API key is present in the
    environment, or limits/anchor were pinned explicitly via prefs / env.
    Keeps unmetered qwen sessions (e.g. interactive llama-swap) honest."""
    if any(os.environ.get(key) for key in _PLAN_ENV_KEYS):
        return True
    watched = (_PREF_FIVE_HOUR, _PREF_WEEKLY, _PREF_ANCHOR)
    configured = load_prefs().keys() | os.environ.keys()
    return any(key in configured for key in watched)


def _anchor_exhausted(anchor_five_hour, five_hour_limit):
    """True when the anchored 5h usage is at or above the limit: the shared
    key's window is pinned at exhaustion, so invisible traffic plausibly
    replaces every call that expires out of the rolling window."""
    return five_hour_limit is not None and anchor_five_hour >= five_hour_limit


def _hold_clearance_unix(anchor, five_hour_limit, now_unix):
    """anchored_at + 5h when an exhausted anchor is being held flat (the
    moment the anchored population has certainly expired out of the rolling
    window), else None when no hold is in effect (non-exhausted anchor, or
    elapsed >= 5h so the pure-local path owns it)."""
    anchor_five_hour, _anchor_week, anchored_at = anchor
    if not _anchor_exhausted(anchor_five_hour, five_hour_limit):
        return None
    if now_unix - anchored_at >= _FIVE_HOUR_SECONDS:
        return None
    return anchored_at + _FIVE_HOUR_SECONDS


def _five_hour_used(entry, anchor, now_unix, five_hour_limit=None):
    """Anchored 5h usage: linear decay of the anchor population across the 5h
    after anchoring plus every local call since the anchor (all of which sit
    inside the rolling window while the decay runs); pure local once the
    window is fully post-anchor. When the anchor is exhausted (at or above
    the limit), the value is HELD flat at the anchored value until
    anchored_at + 5h - shared-key traffic plausibly replaces every expiring
    call, so the honest estimate is 'still exhausted until the anchored
    population is provably gone'. Local calls made during the hold are NOT
    added: they either 429'd or rode the extra-usage pack, neither of which
    is defensibly counted against the base window."""
    anchor_five_hour, _anchor_week, anchored_at = anchor
    elapsed = now_unix - anchored_at
    if elapsed >= _FIVE_HOUR_SECONDS:
        return float(entry.get("calls_5h") or 0)
    if _anchor_exhausted(anchor_five_hour, five_hour_limit):
        return float(anchor_five_hour)
    decay = max(0.0, (_FIVE_HOUR_SECONDS - elapsed) / _FIVE_HOUR_SECONDS)
    delta = entry.get("calls_since_anchor") or 0
    return anchor_five_hour * decay + delta


def _week_used(entry, anchor, now_unix):
    """Anchored weekly usage: exact while the anchor sits inside the current
    fixed window (nothing expires there - the window only accumulates). None
    once the week has reset past the anchor: the anchor is stale by a whole
    window and only a fresh dashboard reading repairs it."""
    _anchor_five_hour, anchor_week, anchored_at = anchor
    if week_start_unix(now_unix) > anchored_at:
        return None
    return anchor_week + (entry.get("calls_since_anchor") or 0)


def _rolling_pace_part(used, limit):
    """` +Hh`/` -Hh` exhaustion buffer for the rolling five-hour window: how
    long the remaining calls last if the window's average rate continues.
    '' at zero usage (nothing to pace against)."""
    if limit is None or used is None or used <= 0:
        return ""
    delta = _FIVE_HOUR_SECONDS * (limit - used) / used
    return f" {_fmt_delta(delta, 0.05 * _FIVE_HOUR_SECONDS)}"


def _weekly_pace_part(used, weekly_limit, now_unix):
    """` +Hh`/` -Hh` cumulative-pace buffer against Monday's fixed reset,
    reusing format_quota's exact fixed-window math."""
    if weekly_limit is None or used is None:
        return ""
    util = 100.0 * used / weekly_limit
    return _project_pace(util, week_start_unix(now_unix) + _WEEK_SECONDS, _WEEK_SECONDS)


def _horizon(label, used, limit, pace_part):
    if used is None or limit is None:
        return ""
    util = 100.0 * used / limit
    return f"{label}: {color_high_bad(util, 75, 90)}{pace_part}"


def format_qwen_quota():
    """Line-2 field `5h: P% ±Hh wk: P% ±Hh` from the dashboard-anchored plan
    windows; '' when the field is unavailable (non-qwen platform, no plan
    key, no anchor, both horizons hidden, or a cold cache mid first
    recompute)."""
    if platform_name() != "qwen":
        return ""
    if not _plan_gate():
        return ""
    anchor = _anchor()
    if anchor is None:
        return ""
    five_hour_limit = _limit(_PREF_FIVE_HOUR, _DEFAULT_FIVE_HOUR_LIMIT)
    weekly_limit = _limit(_PREF_WEEKLY, _DEFAULT_WEEKLY_LIMIT)
    if five_hour_limit is None and weekly_limit is None:
        return ""
    now_unix = _now_unix()
    entry = _qwen_quota_cached(now_unix, pref(_PREF_ANCHOR))
    if entry is None:
        return ""
    five_hour_used = _five_hour_used(entry, anchor, now_unix, five_hour_limit)
    week_used = _week_used(entry, anchor, now_unix)
    clearance = _hold_clearance_unix(anchor, five_hour_limit, now_unix)
    five_hour_pace = (
        f" ~{_fmt_local_clock(clearance)}"
        if clearance is not None
        else _rolling_pace_part(five_hour_used, five_hour_limit)
    )
    parts = [
        part
        for part in (
            _horizon(
                "5h",
                five_hour_used,
                five_hour_limit,
                five_hour_pace,
            ),
            _horizon(
                "wk",
                week_used,
                weekly_limit,
                _weekly_pace_part(week_used, weekly_limit, now_unix),
            ),
        )
        if part
    ]
    return " ".join(parts)
