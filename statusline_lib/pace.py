"""Pace walking, project_pace, format_quota.

Imports:
  base   -- for color constants, _json_loads, color_high_bad
  cost   -- for _cost_for_turn
  walker -- for _walk_pace_buckets_native, _walker_root_list
"""

import glob
import json
import os
from datetime import UTC, datetime

from .base import GREEN, RED, RESET, YELLOW, _json_loads, color_high_bad
from .cost import _cost_for_turn
from .walker import _walk_pace_buckets_native, _walker_root_list

_PACE_CACHE_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", ".statusline-pace-cache-v2.json"
)
# Cold pace walk costs ~250ms parallel-Python or ~95ms with the native
# claude-walker. The cache stays because the statusline fires many times per
# render, but at sub-100ms cold the TTL can stay tight: 15s means a usage
# spike shows in the pace projection within ~15s without making cache misses
# feel sluggish.
_PACE_CACHE_TTL_SECONDS = 15


def _pace_buckets_cached(period_seconds, win_start_unix):
    """Cached wrapper around _walk_pace_buckets. See _walk_pace_buckets for math."""
    try:
        with open(_PACE_CACHE_PATH, encoding="utf-8") as f:
            c = json.load(f)
        age = datetime.now(UTC).timestamp() - c.get("computed_at_unix", 0)
        if (
            age < _PACE_CACHE_TTL_SECONDS
            and c.get("period_seconds") == period_seconds
            and c.get("win_start_unix") == win_start_unix
        ):
            return c["trailing_dollars"], c["window_dollars"]
    except (OSError, ValueError, KeyError):
        pass
    trailing, window = _walk_pace_buckets(period_seconds, win_start_unix)
    try:
        with open(_PACE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "computed_at_unix": datetime.now(UTC).timestamp(),
                    "period_seconds": period_seconds,
                    "win_start_unix": win_start_unix,
                    "trailing_dollars": trailing,
                    "window_dollars": window,
                },
                f,
            )
    except OSError:
        # Best-effort cache write; failure just means we recompute next time.
        pass
    return trailing, window


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


def _pace_costs_for_file(path, seen_ids, earliest, period_cutoff, win_start_unix):
    """(trailing_dollars, window_dollars) cost contributed by one JSONL file."""
    trailing = window_cost = 0.0
    last_model = ""
    try:
        with open(path, "rb") as f:
            for line in f:
                parsed = _parse_pace_line(line, seen_ids, earliest)
                if parsed is None:
                    continue
                ts, usage, model_id = parsed
                if model_id:
                    last_model = model_id
                c = _cost_for_turn(usage, model_id or last_model)
                if ts >= period_cutoff:
                    trailing += c
                if ts >= win_start_unix:
                    window_cost += c
    except OSError:
        return 0.0, 0.0
    return trailing, window_cost


def _walk_session_group(paths, period_cutoff, win_start_unix):
    """Walk one parent+subagents group, return (trailing_dollars, window_dollars).

    Module-level so ProcessPoolExecutor can serialize a reference to it.

    Sequential within a group (a single shared `seen_ids`) so the dedup set
    catches the parent <-> auto-compact-subagent message.id overlap (the only
    collision pattern that actually appears -- 146 instances in the working
    corpus, all parent-vs-its-own-acompact-subagent). Cross-session collisions
    weren't observed and are not defended against here; if they ever appear in
    real data the cost impact would still round to zero.
    """
    earliest = min(period_cutoff, win_start_unix)
    trailing = window_cost = 0.0
    seen_ids = set()
    for path in paths:
        t, w = _pace_costs_for_file(
            path, seen_ids, earliest, period_cutoff, win_start_unix
        )
        trailing += t
        window_cost += w
    return trailing, window_cost


def _discover_pace_groups(roots, earliest):
    """Group transcript files (parent jsonl + its subagents) by
    (slug, session_id), keeping only files whose mtime could hold in-range
    entries. The mtime prefilter prunes ~80% of files."""
    groups = {}
    for proj_root in roots:
        for path in glob.glob(os.path.join(proj_root, "*", "*.jsonl")):
            try:
                if os.path.getmtime(path) < earliest:
                    continue
            except OSError:
                continue
            slug = os.path.basename(os.path.dirname(path))
            session_id = os.path.splitext(os.path.basename(path))[0]
            groups.setdefault((slug, session_id), []).append(path)
        sub_pattern = os.path.join(proj_root, "*", "*", "subagents", "agent-*.jsonl")
        for path in glob.glob(sub_pattern):
            try:
                if os.path.getmtime(path) < earliest:
                    continue
            except OSError:
                continue
            sub_dir = os.path.dirname(path)
            session_dir = os.path.dirname(sub_dir)
            session_id = os.path.basename(session_dir)
            slug = os.path.basename(os.path.dirname(session_dir))
            groups.setdefault((slug, session_id), []).append(path)
    return groups


def _walk_groups_inline(groups, period_cutoff, win_start_unix):
    """Sequential sum over groups -- used for <=2 groups and as the
    parallel-path fallback."""
    trailing = window_cost = 0.0
    for paths in groups.values():
        t, w = _walk_session_group(paths, period_cutoff, win_start_unix)
        trailing += t
        window_cost += w
    return trailing, window_cost


def _walk_groups_parallel(groups, period_cutoff, win_start_unix):
    """Dispatch group walks to a ProcessPoolExecutor; fall back to inline if
    the pool can't start."""
    workers = min(8, os.cpu_count() or 4)
    trailing = window_cost = 0.0
    try:
        # Lazy import: pulls in multiprocessing (~26ms). Only paid here, on the
        # rare >2-group parallel path -- never on a normal statusline render.
        from concurrent.futures import ProcessPoolExecutor, as_completed

        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_walk_session_group, paths, period_cutoff, win_start_unix)
                for paths in groups.values()
            ]
            for fut in as_completed(futures):
                try:
                    t, w = fut.result()
                except Exception:
                    continue
                trailing += t
                window_cost += w
    except (OSError, RuntimeError):
        # ProcessPoolExecutor unavailable (sandboxed env, no fork on some
        # platforms, etc.) -- fall back to inline sequential walk.
        return _walk_groups_inline(groups, period_cutoff, win_start_unix)
    return trailing, window_cost


def _walk_pace_buckets(period_seconds, win_start_unix):
    """Sum assistant-turn cost across all transcripts into two buckets.

    Returns (trailing_dollars, window_dollars):
      trailing_dollars -- cost in the trailing `period_seconds` from now
      window_dollars   -- cost since `win_start_unix` (current rate-limit window)

    Used to project the weekly quota at a stable trailing-period burn rate,
    calibrated to %/$ via the current window's (util, window_dollars). The
    in-window-only rate is unstable on day 1 of a fresh window where the
    elapsed-since-window-start denominator is tiny.

    Implementation:
      * mtime filter prunes ~80% of files that can't contain in-range entries.
      * Survivors are grouped by parent session (parent.jsonl + its
        subagents/agent-*.jsonl) so dedup is local to the group.
      * Groups dispatch to a ProcessPoolExecutor for true CPU parallelism.
        Single-group walks run inline to skip ~150ms pool-startup tax.

    Expensive on the typical fleet (~150ms parallel, was ~750ms single-thread);
    call via _pace_buckets_cached. The native claude-walker binary, if present,
    runs the same walk in ~80-180ms and short-circuits this path entirely.
    """
    native = _walk_pace_buckets_native(period_seconds, win_start_unix)
    if native is not None:
        return native

    roots = _walker_root_list()
    if not roots:
        return 0.0, 0.0
    now = datetime.now(UTC).timestamp()
    period_cutoff = now - period_seconds
    earliest = min(period_cutoff, win_start_unix)

    groups = _discover_pace_groups(roots, earliest)
    if not groups:
        return 0.0, 0.0

    # Inline walk if the parallelism win wouldn't beat process-pool startup.
    if len(groups) <= 2:
        return _walk_groups_inline(groups, period_cutoff, win_start_unix)
    return _walk_groups_parallel(groups, period_cutoff, win_start_unix)


def _fmt_delta_hours(seconds):
    sign = "+" if seconds >= 0 else "-"
    return f"{sign}{abs(seconds) / 3600:.1f}h"


def _project_pace(util, resets_at_unix, period_seconds, use_trailing=False):
    """Returns ' +X.Yh' (colored) or '' if not enough data.

    Two pace estimators:
      * in-window: extrapolates `util / elapsed_in_window` to reset time. Noisy
        early in the window (tiny denominator), tightens as elapsed grows.
      * trailing-period: walks JSONL transcripts for trailing-period $-burn,
        calibrates to %/$ via (util, current-window $), projects forward. Stable
        from day 1, slightly biased mid-week by data from the prior period's tail.

    use_trailing=True linearly blends the two by `elapsed / period`: pure
    trailing at window start, pure in-window at window end. The two converge at
    week-end (the trailing window aligns with the current window) so the late-
    week blend is mostly cosmetic; the early-week blend is what stabilizes day
    1. Falls back to in-window only when JSONL calibration is degenerate (zero
    $ in window).
    """
    if util is None or util <= 0 or not resets_at_unix:
        return ""
    try:
        reset_dt = datetime.fromtimestamp(resets_at_unix, tz=UTC)
        remaining = (reset_dt - datetime.now(UTC)).total_seconds()
        elapsed = period_seconds - remaining
        if elapsed <= 0 or remaining <= 0:
            return ""
        in_window_delta = 100.0 * elapsed / util - period_seconds
        delta = in_window_delta
        if use_trailing:
            win_start = resets_at_unix - period_seconds
            trailing_d, window_d = _pace_buckets_cached(period_seconds, win_start)
            if trailing_d > 0 and window_d > 0:
                hourly_pct = trailing_d * util / (window_d * period_seconds / 3600)
                if hourly_pct > 0:
                    trailing_delta = (100.0 - util) / hourly_pct * 3600 - remaining
                    in_window_weight = elapsed / period_seconds
                    delta = (
                        1.0 - in_window_weight
                    ) * trailing_delta + in_window_weight * in_window_delta
        warn_threshold = 0.05 * period_seconds
        if delta < 0:
            color = RED
        elif delta <= warn_threshold:
            color = YELLOW
        else:
            color = GREEN
        return f" {color}{_fmt_delta_hours(delta)}{RESET}"
    except Exception:
        return ""


def format_quota(rate_limits):
    """Returns space-joined '5h: P% +Hh wk: P% +Hh', omitting unavailable windows."""
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
        proj_part = _project_pace(
            util, w.get("resets_at"), period_seconds, use_trailing
        )
        parts.append(f"{label}: {pct_part}{proj_part}")
    return " ".join(parts)
