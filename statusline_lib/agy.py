"""Antigravity CLI ("agy") payload adapters.

Antigravity's stdin payload shares the Claude-common shape (context_window,
model, workspace, ...) but replaces `rate_limits`/`cost` with its own `quota`
block and adds `agent_state`. Its brain transcripts (`~/.gemini/antigravity/
brain/<id>/.system_generated/logs/transcript.jsonl`) carry zero usage/cost
data (verified empirically 2026-07-10), so none of the transcript-walking
machinery in pace.py/cost.py (hourly $-burn series, session-cumulative cache,
subagent cost split) can be reused for agy sessions -- everything here is
derived from the single stdin payload, once, per render.

Imports:
  base    -- color_high_bad
  costfmt -- format_cache (reused for the per-turn cache fallback)
  pace    -- _now_unix (clock seam), _project_pace (payload-only projection)
"""

from datetime import datetime

from .base import RESET, color_high_bad
from .costfmt import format_cache
from .pace import _project_pace

# Antigravity's own model + third-party-provider quota pairs. Each pair holds
# a 5h and a weekly window keyed the same way Claude's rate_limits is, so the
# render can share pace._project_pace's payload-only (non-trailing) formula.
_AGY_QUOTA_PAIRS = {
    "gemini": ("gemini-5h", "gemini-weekly"),
    "3p": ("3p-5h", "3p-weekly"),
}


def _agy_window_metrics(window):
    """(used_percentage, resets_at_unix) from one agy quota window dict, or
    (None, None) when the window is missing or malformed. `remaining_fraction`
    inverts straight to a Claude-shaped `used_percentage`; `reset_time` is
    parsed instead of derived from `reset_in_seconds` so the result doesn't
    depend on this process's clock matching the payload producer's."""
    if not isinstance(window, dict):
        return None, None
    remaining = window.get("remaining_fraction")
    reset_time = window.get("reset_time")
    if remaining is None or not isinstance(reset_time, str):
        return None, None
    try:
        remaining = float(remaining)
        resets_at = datetime.fromisoformat(
            reset_time.replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return None, None
    used_percentage = max(0.0, min(100.0, (1.0 - remaining) * 100.0))
    return used_percentage, resets_at


def _agy_pair_worst_utilization(quota, pair_keys):
    """Highest used_percentage across a pair's windows, or None if neither
    window is usable. "Worst" = most constrained = most useful to surface."""
    worst = None
    for key in pair_keys:
        util, _resets_at = _agy_window_metrics(quota.get(key))
        if util is not None and (worst is None or util > worst):
            worst = util
    return worst


def _agy_primary_pair(quota):
    """Pick which quota pair (gemini-* or 3p-*) to render. gemini-* is the
    default -- the primary pair for a Gemini-model session -- but 3p-* wins
    when it is STRICTLY more utilized somewhere in its pair: surfacing
    whichever pair is closer to its cap is more useful than always defaulting
    to gemini regardless of which one is actually the constraint."""
    gemini_worst = _agy_pair_worst_utilization(quota, _AGY_QUOTA_PAIRS["gemini"])
    threep_worst = _agy_pair_worst_utilization(quota, _AGY_QUOTA_PAIRS["3p"])
    if threep_worst is not None and (
        gemini_worst is None or threep_worst > gemini_worst
    ):
        return _AGY_QUOTA_PAIRS["3p"]
    return _AGY_QUOTA_PAIRS["gemini"]


def format_agy_quota(quota, show_pace=True):
    """Antigravity's answer to pace.format_quota: '5h: P% +Hh wk: P% +Hh',
    sourced from the `quota` payload block instead of `rate_limits`.

    Both windows use pace._project_pace's non-trailing (payload-only)
    extrapolation -- the same formula format_quota uses for its `5h:` window.
    format_quota's `wk:` window instead uses a trailing-24h current-rate
    forecast calibrated from an hourly walk of local transcript files
    (pace._weekly_deltas -> _pace_hourly_cached); agy has no usable transcript
    data for that walk (see module docstring), so its weekly figure would
    silently be a walk over nothing. Reusing the plain elapsed-fraction
    extrapolation for both windows keeps the number honest about what it's
    derived from, at the cost of the trailing-rate window's extra
    responsiveness.

    Malformed input degrades to "" rather than raising: _agy_window_metrics
    already catches its own parse errors and pace._project_pace already
    guards its own math, so there is no exception path left here to catch --
    every window that can't be parsed is simply skipped.
    """
    if not isinstance(quota, dict) or not quota:
        return ""
    five_hour_key, weekly_key = _agy_primary_pair(quota)
    parts = []
    for key, period_seconds, label in (
        (five_hour_key, 5 * 3600, "5h"),
        (weekly_key, 7 * 86400, "wk"),
    ):
        util, resets_at = _agy_window_metrics(quota.get(key))
        if util is None:
            continue
        pct_part = color_high_bad(util, 75, 90)
        proj_part = (
            _project_pace(util, resets_at, period_seconds, use_trailing=False)
            if show_pace
            else ""
        )
        parts.append(f"{label}: {pct_part}{proj_part}")
    return " ".join(parts)


# Muted grey -- matches the session-name/subagent-cost secondary tone, so the
# agent-state tag reads as ambient status, not a warning.
_AGENT_STATE_COLOR = "\x1b[38;5;245m"
_AGENT_STATE_GLYPHS = {
    "working": "●",  # filled circle
    "idle": "○",  # open circle
}


def format_agent_state(agent_state):
    """Small muted `[glyph state]` tag for Antigravity's `agent_state` field
    ("working"/"idle"/...). "" when absent -- Claude Code's payload carries no
    such field, so the tag is naturally omitted there. An unrecognized state
    still renders (the raw string, no glyph) rather than disappearing -- a new
    agy state silently vanishing would be a worse failure mode than a
    glyph-less label appearing once."""
    state = str(agent_state or "").strip()
    if not state:
        return ""
    glyph = _AGENT_STATE_GLYPHS.get(state.lower())
    label = f"{glyph} {state}" if glyph else state
    return f"{_AGENT_STATE_COLOR}[{label}]{RESET}"


def format_agy_cache(current_usage, show_hit=True):
    """Reduced cache field for agy: per-TURN read/write + hit%, from
    `context_window.current_usage` -- the only cache signal agy's payload
    carries. Unlike the Claude/Qwen cache column (session-cumulative, walked
    from local transcripts), this is a single turn's snapshot, and it never
    carries a $ figure: agy has no per-Mtok rate to price these tokens at.
    Reuses costfmt.format_cache with cost args omitted rather than a bespoke
    formatter, since agy's current_usage keys already match Claude's shape.
    """
    if not isinstance(current_usage, dict):
        return ""
    read = int(current_usage.get("cache_read_input_tokens") or 0)
    write = int(current_usage.get("cache_creation_input_tokens") or 0)
    if read <= 0 and write <= 0:
        # format_cache's own guard is `read + write + input_t <= 0`, which
        # would still render "0 / 0 / 0% hit" on a turn with fresh input but
        # zero cache activity -- noise for a field whose whole point is the
        # cache signal. Gate on cache activity specifically instead.
        return ""
    input_t = int(current_usage.get("input_tokens") or 0)
    return format_cache(read, write, input_t, show_costs=False, show_hit=show_hit)
