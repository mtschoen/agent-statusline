"""Pace walking, project_pace, format_quota.

Imports:
  base   -- for color constants, _json_loads, color_high_bad
  cost   -- for _cost_for_turn
  walker -- for _walker_root_list
"""

import json
import os
from datetime import UTC, datetime

from .base import GREEN, RESET, _json_loads, app_dir, color_high_bad, ramp_color_for
from .cost import _cost_for_turn
from .prefs import pref
from .project import is_on_target, project_delta
from .walker import _walker_root_list

# Current-rate arrow glyphs. Up = current rate is HOTTER than cumulative pace
# (eating your buffer -> slow down); down = cooler (building buffer -> go nuts).
ARROW_UP = "↑"
ARROW_DOWN = "↓"

# On-target reward: both signals within _ON_TARGET_MARGIN_SECONDS of reset-time finish.
# U+FE0E = text-presentation selector; keeps yin-yang monochrome so ANSI green wins.
ON_TARGET_GLYPH = "☯︎"
_ON_TARGET_MARGIN_SECONDS = 4 * 3600


def _now_unix():
    """Current unix time. Seam so tests can pin the window clock."""
    return datetime.now(UTC).timestamp()


_PACE_CACHE_TTL_SECONDS = 15  # 15s: spike visible quickly, cache miss still fast
_PACE_HOURLY_CACHE_PATH = os.path.join(
    app_dir(), ".statusline-pace-hourly-cache-v1.json"
)


def _parse_pace_line(line, seen_ids, earliest):
    """Parse one JSONL line for the pace walk. Returns (ts, usage, model_id),
    or None to skip (blank, malformed, non-assistant, duplicate id, too old)."""
    if not line.strip():
        return None
    try:
        e = _json_loads(line)
    except Exception:
        return None
    msg = e.get("message") or {}
    if msg.get("role") != "assistant":
        return None
    mid = msg.get("id")
    if mid:
        if mid in seen_ids:
            return None
        seen_ids.add(mid)
    ts_str = e.get("timestamp")
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None
    if ts < earliest:
        return None
    return ts, (msg.get("usage") or {}), (msg.get("model") or "")


def _scandir_entries(dir_path):
    """os.scandir as a list; [] when the directory is missing or unreadable
    (a session dir without a subagents/ child is the everyday case)."""
    try:
        with os.scandir(dir_path) as it:
            return list(it)
    except OSError:
        return []


def _entry_in_window(entry, earliest):
    """True when the DirEntry's mtime is at/after `earliest`; unreadable -> False."""
    try:
        return entry.stat().st_mtime >= earliest
    except OSError:
        return False


def _discover_pace_groups(roots, earliest):
    """Group transcript files (parent jsonl + its subagents) by
    (slug, session_id), keeping only files whose mtime could hold in-range
    entries. The mtime prefilter prunes ~80% of files.

    Built on os.scandir rather than glob + os.path.getmtime: DirEntry.stat()
    reuses the directory listing's attributes (no per-file stat syscall on
    Windows, one round-trip per directory on network shares), where per-file
    getmtime cost seconds per render once roots grew to thousands of files.
    """
    groups = {}
    for proj_root in roots:
        for slug_entry in _scandir_entries(proj_root):
            if slug_entry.is_dir(follow_symlinks=False):
                _collect_session_files(slug_entry, earliest, groups)
    return groups


def _collect_session_files(slug_entry, earliest, groups):
    """One slug dir's contribution to the pace groups: parent JSONLs directly
    under the slug dir, plus each session dir's subagent JSONLs."""
    slug = slug_entry.name
    for entry in _scandir_entries(slug_entry.path):
        if entry.name.endswith(".jsonl"):
            if _entry_in_window(entry, earliest):
                session_id = entry.name[: -len(".jsonl")]
                groups.setdefault((slug, session_id), []).append(entry.path)
        elif entry.is_dir(follow_symlinks=False):
            _collect_subagent_files(entry, slug, earliest, groups)


def _collect_subagent_files(session_entry, slug, earliest, groups):
    """agent-*.jsonl under `<session dir>/subagents`, grouped with the parent
    session's (slug, session_id) key."""
    for sub in _scandir_entries(os.path.join(session_entry.path, "subagents")):
        is_agent_jsonl = sub.name.startswith("agent-") and sub.name.endswith(".jsonl")
        if is_agent_jsonl and _entry_in_window(sub, earliest):
            groups.setdefault((slug, session_entry.name), []).append(sub.path)


def _pace_hourly_for_file(path, seen_ids, win_start_unix, n_buckets):
    """Per-file hourly $-burn list, length n_buckets, indexed from window start."""
    buckets = [0.0] * n_buckets
    last_model = ""
    try:
        with open(path, "rb") as f:
            for line in f:
                parsed = _parse_pace_line(line, seen_ids, earliest=win_start_unix)
                if parsed is None:
                    continue
                ts, usage, model_id = parsed
                if model_id:
                    last_model = model_id
                index = int((ts - win_start_unix) // 3600)
                if 0 <= index < n_buckets:
                    buckets[index] += _cost_for_turn(usage, model_id or last_model)
    except OSError:
        return [0.0] * n_buckets
    return buckets


def _walk_session_hourly(paths, win_start_unix, n_buckets):
    """Hourly $-burn for one parent+subagents group. Module-level so a
    ProcessPoolExecutor can serialize it. Shared `seen_ids` across the group's
    files dedups the parent <-> auto-compact-subagent message.id overlap."""
    seen_ids = set()
    totals = [0.0] * n_buckets
    for path in paths:
        per_file = _pace_hourly_for_file(path, seen_ids, win_start_unix, n_buckets)
        for i in range(n_buckets):
            totals[i] += per_file[i]
    return totals


def _sum_hourly(into, addend):
    for i, value in enumerate(addend):
        into[i] += value


def _walk_hourly_inline(groups, win_start_unix, n_buckets):
    totals = [0.0] * n_buckets
    for paths in groups.values():
        _sum_hourly(totals, _walk_session_hourly(paths, win_start_unix, n_buckets))
    return totals


def _walk_hourly_parallel(groups, win_start_unix, n_buckets):
    workers = min(8, os.cpu_count() or 4)
    totals = [0.0] * n_buckets
    try:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_walk_session_hourly, paths, win_start_unix, n_buckets)
                for paths in groups.values()
            ]
            for fut in as_completed(futures):
                try:
                    _sum_hourly(totals, fut.result())
                except Exception:
                    # Worker failure: skip this group's contribution (it zeros out)
                    continue
    except (OSError, RuntimeError):
        return _walk_hourly_inline(groups, win_start_unix, n_buckets)
    return totals


def _walk_pace_hourly(win_start_unix):
    """Hourly in-window $-burn series from window start to now.

    Index 0 is the first hour of the window. Window-local only -- no trailing
    cross-week bucket (the redesign dropped it). The native claude-walker bridge
    is not used here because it returns scalars, not an hourly series.
    """
    roots = _walker_root_list()
    if not roots:
        return []
    now = _now_unix()
    n_buckets = max(1, int((now - win_start_unix) // 3600) + 1)
    groups = _discover_pace_groups(roots, win_start_unix)
    if not groups:
        return [0.0] * n_buckets
    if len(groups) <= 2:
        return _walk_hourly_inline(groups, win_start_unix, n_buckets)
    return _walk_hourly_parallel(groups, win_start_unix, n_buckets)


def _pace_hourly_cached(win_start_unix):
    """15s-TTL cache around _walk_pace_hourly (statusline fires many times/render)."""
    try:
        with open(_PACE_HOURLY_CACHE_PATH, encoding="utf-8") as f:
            cached = json.load(f)
        age = _now_unix() - cached.get("computed_at_unix", 0)
        if (
            age < _PACE_CACHE_TTL_SECONDS
            and cached.get("win_start_unix") == win_start_unix
        ):
            return cached["hourly"]
    except (OSError, ValueError, KeyError):
        pass
    hourly = _walk_pace_hourly(win_start_unix)
    try:
        with open(_PACE_HOURLY_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "computed_at_unix": _now_unix(),
                    "win_start_unix": win_start_unix,
                    "hourly": hourly,
                },
                f,
            )
    except OSError:
        # Best-effort cache write; failure just means we recompute next time.
        pass
    return hourly


def _fmt_delta_hours(seconds):
    sign = "+" if seconds >= 0 else "-"
    return f"{sign}{abs(seconds) / 3600:.1f}h"


def _delta_color(delta, warn_threshold):
    """Gradient on surplus seconds: solid green at/above warn_threshold, ramps
    through yellow to red at 0 (or negative). Higher surplus is better, so
    warn_threshold is the green edge and 0 the red edge."""
    return ramp_color_for(delta, warn_threshold, 0)


def _fmt_delta(delta, warn_threshold):
    return f"{_delta_color(delta, warn_threshold)}{_fmt_delta_hours(delta)}{RESET}"


def _rate_arrow(cumulative_delta, current_rate_delta, warn_threshold):
    """Colored arrow from the current-rate signal, or '' if unavailable.

    Direction: up when the current rate is hotter than the cumulative pace
    (lands earlier -> eating buffer), down when cooler. Color: the current-rate
    delta's own threshold verdict.
    """
    if current_rate_delta is None:
        return ""
    direction = ARROW_UP if current_rate_delta < cumulative_delta else ARROW_DOWN
    return f"{_delta_color(current_rate_delta, warn_threshold)}{direction}{RESET}"


def _weekly_deltas(util, resets_at_unix, period_seconds):
    """(cumulative_delta, current_rate_delta, warn_threshold, elapsed) for the
    weekly window, or None when there isn't enough data. Deltas are seconds vs
    reset (positive = surplus). Shared by the wk: number and the relocated needle.
    """
    if util is None or util <= 0 or not resets_at_unix:
        return None
    reset_dt = datetime.fromtimestamp(resets_at_unix, tz=UTC)
    remaining = (reset_dt - datetime.fromtimestamp(_now_unix(), tz=UTC)).total_seconds()
    elapsed = period_seconds - remaining
    if elapsed <= 0 or remaining <= 0:
        return None
    warn_threshold = 0.05 * period_seconds
    win_start = resets_at_unix - period_seconds
    hourly = _pace_hourly_cached(win_start)
    cumulative_delta, current_rate_delta = project_delta(
        hourly, util, elapsed, remaining, period_seconds
    )
    if cumulative_delta is None:
        return None
    return cumulative_delta, current_rate_delta, warn_threshold, elapsed


def weekly_needle(rate_limits):
    """The relocated subscription needle: colored current-rate arrow, or the
    on-target yin-yang, computed from the weekly window. "" when unavailable.

    STATUSLINE_VERBOSE_PACE renders both numeric deltas instead of the glyph.
    """
    # Best-effort: malformed rate_limits degrades to no needle, not a raise
    # (same guard _project_pace kept around this computation before it moved here).
    try:
        rl = rate_limits or {}
        w = rl.get("seven_day") or {}
        deltas = _weekly_deltas(w.get("used_percentage"), w.get("resets_at"), 7 * 86400)
        if deltas is None:
            return ""
        cumulative_delta, current_rate_delta, warn_threshold, elapsed = deltas
        verbose = pref("STATUSLINE_VERBOSE_PACE") not in (None, "", "0")
        if verbose and current_rate_delta is not None:
            return (
                f" {_fmt_delta(cumulative_delta, warn_threshold)}"
                f"/{_fmt_delta(current_rate_delta, warn_threshold)}"
            )
        if is_on_target(
            cumulative_delta,
            current_rate_delta,
            elapsed,
            margin_seconds=_ON_TARGET_MARGIN_SECONDS,
        ):
            return f"{GREEN}{ON_TARGET_GLYPH}{RESET}"
        return _rate_arrow(cumulative_delta, current_rate_delta, warn_threshold)
    except Exception:
        return ""


# Below this weekly-quota utilization the util/$ calibration is too noisy to
# trust (a tiny denominator early in the window inflates the projected quota), so
# the adaptive target falls back to the flat default rather than emitting a wild
# number. The needle guards the same early-window noise with its warmup prior
# (see project.project_delta).
_WEEKLY_TARGET_MIN_UTIL_PCT = 1.0


def weekly_sustainable_rate(rate_limits):
    """Adaptive weekly target burn in funny-money $/min, or None when not derivable.

    The "rate you can sustain from now to land exactly on your weekly quota at
    reset": remaining quota dollars over the time left in the window. The quota's
    dollar size is calibrated from the window's own util/$ ratio -- util% of the
    weekly quota corresponds to the funny-money $ actually burned in the window so
    far (the same calibration project_delta uses to turn $/h into %/h). Reuses the
    15s-cached hourly series, so on a subscription render where weekly_needle has
    already walked the window this is a cache hit, not a second walk.

    Returns None (caller falls back to the flat default) when there is no weekly
    quota, utilization is below the noise floor or at/over 100%, the reset is
    already past, or the window holds no spend to calibrate against.

    Best-effort like its siblings weekly_needle/weekly_exhaustion: malformed
    rate_limits (e.g. a non-numeric resets_at) degrades to None rather than
    raising into the render path.
    """
    try:
        rl = rate_limits or {}
        w = rl.get("seven_day") or {}
        util = w.get("used_percentage")
        resets_at = w.get("resets_at")
        if util is None or util < _WEEKLY_TARGET_MIN_UTIL_PCT or not resets_at:
            return None
        remaining = resets_at - _now_unix()
        if remaining <= 0:
            return None
        win_start = resets_at - 7 * 86400
        hourly = _pace_hourly_cached(win_start)
        window_spend = sum(hourly) if hourly else 0.0
        if window_spend <= 0:
            return None
        quota_dollars = window_spend / (util / 100.0)
        remaining_dollars = quota_dollars - window_spend
        if remaining_dollars <= 0:
            # util at/over 100%: the whole weekly quota is already spent.
            return None
        return remaining_dollars / (remaining / 60.0)
    except Exception:
        return None


# Past this weekly utilization the window is close enough to running out that an
# absolute "hits 100% at <clock>" beats the relative +Hh pace number on line 2;
# below it the field stays hidden. ">90%" -> show strictly above 90.
_WEEKLY_EXHAUSTION_MIN_UTIL_PCT = 90.0


def _fmt_local_clock(unix_ts):
    """`unix_ts` as a local 12-hour clock, weekday-prefixed only when it is not
    today: "6:00pm" today, "Tue 6:00pm" otherwise. strftime's %I/%p are avoided
    (%I zero-pads the hour and %p casing/availability vary by platform), so the
    12-hour parts are assembled by hand for stable cross-platform output."""
    dt = datetime.fromtimestamp(unix_ts, tz=UTC).astimezone()
    hour12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    clock = f"{hour12}:{dt.minute:02d}{ampm}"
    today = datetime.fromtimestamp(_now_unix(), tz=UTC).astimezone().date()
    if dt.date() == today:
        return clock
    return f"{dt:%a} {clock}"


def weekly_exhaustion(rate_limits):
    """Line-3 field "wk 100% ~<clock>": the local time the weekly quota is
    projected to reach 100% at the *current* burn rate, tinted to match line 2's
    weekly quota % (red past 90% -- running out this early means the quota burned
    too fast). "" unless utilization is past _WEEKLY_EXHAUSTION_MIN_UTIL_PCT AND
    the current-rate forecast lands 100% before the window resets -- if you are
    on pace to make it to reset you will not run out, so nothing is shown.

    Mirrors weekly_needle's degrade-not-raise contract: malformed rate_limits
    yields no field rather than crashing the statusline.
    """
    try:
        rl = rate_limits or {}
        w = rl.get("seven_day") or {}
        util = w.get("used_percentage")
        resets_at = w.get("resets_at")
        if util is None or util <= _WEEKLY_EXHAUSTION_MIN_UTIL_PCT or not resets_at:
            return ""
        deltas = _weekly_deltas(util, resets_at, 7 * 86400)
        if deltas is None:
            return ""
        _cumulative, current_rate_delta, _warn_threshold, _elapsed = deltas
        if current_rate_delta is None or current_rate_delta >= 0:
            # No live-rate calibration, or the forecast lands at/after reset
            # (you make it to the window reset -- not going to run out).
            return ""
        exhaustion_unix = resets_at + current_rate_delta
        # Same hue as line 2's weekly quota %: ramp_color_for(util, 75, 90)
        # mirrors format_quota's color_high_bad(util, 75, 90). This field only
        # shows past 90%, so it tracks that number into solid red.
        color = ramp_color_for(util, 75, 90)
        return f"{color}wk 100% ~{_fmt_local_clock(exhaustion_unix)}{RESET}"
    except Exception:
        return ""


def _project_pace(util, resets_at_unix, period_seconds, use_trailing=False):
    """Returns ' <+-Hh>' (colored cumulative pace) or '' if not enough data.

    The current-rate arrow / on-target glyph no longer live here -- they moved to
    the burn-rate field via pace.weekly_needle. This function now renders only the
    cumulative-pace number for both the 5h and weekly windows.
    """
    if util is None or util <= 0 or not resets_at_unix:
        return ""
    try:
        if not use_trailing:
            reset_dt = datetime.fromtimestamp(resets_at_unix, tz=UTC)
            remaining = (
                reset_dt - datetime.fromtimestamp(_now_unix(), tz=UTC)
            ).total_seconds()
            elapsed = period_seconds - remaining
            if elapsed <= 0 or remaining <= 0:
                return ""
            warn_threshold = 0.05 * period_seconds
            delta = 100.0 * elapsed / util - period_seconds
            return f" {_fmt_delta(delta, warn_threshold)}"
        deltas = _weekly_deltas(util, resets_at_unix, period_seconds)
        if deltas is None:
            return ""
        cumulative_delta, _current, warn_threshold, _elapsed = deltas
        return f" {_fmt_delta(cumulative_delta, warn_threshold)}"
    except Exception:
        return ""


def format_quota(rate_limits, show_pace=True):
    """Returns space-joined '5h: P% +Hh wk: P% +Hh', omitting unavailable windows.

    `show_pace=False` (compact mode) drops the +Hh pace projection, leaving the
    bare '5h: P% wk: P%' utilization figures.
    """
    rl = rate_limits or {}
    parts = []
    for win_key, period_seconds, label, use_trailing in (
        ("five_hour", 5 * 3600, "5h", False),
        ("seven_day", 7 * 86400, "wk", True),
    ):
        w = rl.get(win_key) or {}
        util = w.get("used_percentage")
        if util is None:
            continue
        pct_part = color_high_bad(util, 75, 90)
        proj_part = (
            _project_pace(util, w.get("resets_at"), period_seconds, use_trailing)
            if show_pace
            else ""
        )
        parts.append(f"{label}: {pct_part}{proj_part}")
    return " ".join(parts)
