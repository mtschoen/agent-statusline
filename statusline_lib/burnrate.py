"""Live burn-rate field: $X/min + budget needle, plus the API-key day: field.

Imports:
  base    -- color constants, color_high_bad
  cost    -- _cost_for_turn (per-turn $)
  pace    -- _now_unix, _discover_pace_groups, _parse_pace_line, arrows/glyph
  walker  -- _walker_root_list
"""

import json
import os
from datetime import datetime

from .base import (
    CACHE_READ,
    GREEN,
    RESET,
    app_dir,
    color_high_bad,
    ramp_color,
    ramp_color_for,
)
from .cost import _cost_for_turn
from .pace import (
    ARROW_DOWN,
    ARROW_UP,
    ON_TARGET_GLYPH,
    _discover_pace_groups,
    _now_unix,
    _parse_pace_line,
    weekly_needle,
    weekly_sustainable_rate,
)
from .prefs import pref
from .refresh import maybe_spawn_refresh
from .walker import _walker_root_list

# Neutral grey for the rate number; the needle glyph carries the verdict color.
RATE_COLOR = "\x1b[38;5;245m"


def _rate_color(rate, target):
    """Band color for the $/min number vs the target rate. r = rate / target:
    < 0.5x teal (cruising), 0.5-1.5x green (the zen band), 1.5-4x the shared
    gradient, >= 4x red (gradient clamped)."""
    r = rate / target
    if r < 0.5:
        return CACHE_READ
    if r <= 1.5:
        return GREEN
    return ramp_color((r - 1.5) / 2.5)


_SPEND_CACHE_TTL_SECONDS = 15
# v2: per-entry computed_at ({win_start: {computed_at_unix, total}}) so a
# stale entry can be served while a detached child recomputes it; v1 shared
# one computed_at across all windows and is abandoned in place.
_SPEND_CACHE_PATH = os.path.join(app_dir(), ".statusline-burnrate-cache-v2.json")
_SPEND_CACHE_MAX_ENTRIES = 16
# Trailing windows (now-300, midnight, now-86400) move one TTL grid step per
# expiry, so an exact-key lookup misses every step; a neighbor within this
# tolerance is the same window one step ago. Distinct windows sit hours
# apart and can never fall inside it.
_SPEND_NEIGHBOR_TOLERANCE_SECONDS = 60


def _spend_from_path(path, seen_ids, win_start):
    """Sum spend from a single JSONL file, skipping turns before win_start."""
    total = 0.0
    last_model = ""
    try:
        with open(path, "rb") as f:
            for line in f:
                parsed = _parse_pace_line(line, seen_ids, earliest=win_start)
                if parsed is None:
                    continue
                _ts, usage, model_id = parsed
                if model_id:
                    last_model = model_id
                total += _cost_for_turn(usage, model_id or last_model)
    except (OSError, MemoryError):
        # Unreadable or pathological (e.g. a single line too large to buffer)
        # transcript files are skipped; partial spend is still useful. This
        # walk crosses every session under every walker root, so one bad file
        # must not cost the whole burn-rate figure.
        pass
    return total


def _sum_window_spend(win_start):
    """Total funny-money $ across all sessions with a turn ts >= win_start.

    Global / cross-machine: walks every session under _walker_root_list()
    (which includes extra_roots from walker-roots.json). Reuses the pace
    discovery + line parser; dedups message.id within each parent+subagents
    group like the hourly walk does.
    """
    roots = _walker_root_list()
    if not roots:
        return 0.0
    groups = _discover_pace_groups(roots, win_start)
    total = 0.0
    for paths in groups.values():
        seen_ids = set()
        for path in paths:
            total += _spend_from_path(path, seen_ids, win_start)
    return total


def _read_spend_entries():
    """The v2 cache's entry dict ({win_start: {computed_at_unix, total}});
    {} when absent, unreadable, or not the expected shape (a torn write must
    read as "no data", never crash the render)."""
    try:
        with open(_SPEND_CACHE_PATH, encoding="utf-8") as f:
            sums = json.load(f)["sums"]
    except (OSError, ValueError, TypeError, KeyError):
        return {}
    if not isinstance(sums, dict):
        return {}
    # Non-dict values (a torn write, or a stray v1-format scalar) read as
    # absent rather than crashing the render.
    return {key: entry for key, entry in sums.items() if isinstance(entry, dict)}


def _nearest_spend(entries, win_start):
    """The closest entry's total within _SPEND_NEIGHBOR_TOLERANCE_SECONDS of
    `win_start` - the same trailing window one grid step ago - or 0.0 when
    nothing usable is cached yet (honest no-data until the child lands)."""
    best = None
    for key, entry in entries.items():
        try:
            distance = abs(int(key) - win_start)
        except ValueError:
            continue
        in_tolerance = distance <= _SPEND_NEIGHBOR_TOLERANCE_SECONDS
        if in_tolerance and (best is None or distance < best[0]):
            best = (distance, entry.get("total", 0.0))
    return best[1] if best else 0.0


def _window_spend_cached(win_start):
    """Serve the window's spend total from the cache - stale included - and
    hand recomputation to a detached child when the entry is stale or missing
    (stale-while-revalidate; the full-fleet rescan never runs on the render
    path - see refresh.py for why an inline walk freezes the statusline).

    A render asks for up to three windows (5-min, 24h, midnight); one cache
    file holds all of them so they don't evict each other.

    win_start is quantized to the TTL grid first: trailing windows are anchored
    to the moving clock (e.g. now - 300), so the raw value is different on every
    render and would never hit the cache -- each render would re-pay the full
    fleet rescan. Quantizing shifts the window edge by at most one TTL, which is
    noise for a 5-minute rate, and makes renders inside one TTL share a key.
    """
    win_start = int(win_start) - int(win_start) % _SPEND_CACHE_TTL_SECONDS
    entries = _read_spend_entries()
    entry = entries.get(str(win_start))
    if entry is not None:
        if _now_unix() - entry.get("computed_at_unix", 0) < _SPEND_CACHE_TTL_SECONDS:
            return entry.get("total", 0.0)
        maybe_spawn_refresh("window-spend", win_start)
        return entry.get("total", 0.0)
    maybe_spawn_refresh("window-spend", win_start)
    return _nearest_spend(entries, win_start)


def refresh_window_spend_cache(win_start_unix):
    """Recompute one window's spend total and persist it for the render's
    cached read. Runs in the detached refresh child (refresh.run_refresh),
    never on the render path. `win_start_unix` arrives already quantized (the
    render quantizes before spawning). Keeps the newest
    _SPEND_CACHE_MAX_ENTRIES entries so concurrent windows don't evict each
    other."""
    total = _sum_window_spend(win_start_unix)
    entries = _read_spend_entries()
    entries[str(int(win_start_unix))] = {
        "computed_at_unix": _now_unix(),
        "total": total,
    }
    newest = sorted(entries.items(), key=lambda item: item[1]["computed_at_unix"])
    try:
        with open(_SPEND_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"sums": dict(newest[-_SPEND_CACHE_MAX_ENTRIES:])}, f)
    except OSError:
        # Best-effort persist; failure just means the next render respawns.
        pass
    return total


def _five_min_rate():
    """Live global spend rate in $/min over the trailing 300s."""
    return _window_spend_cached(_now_unix() - 300) / 5.0


def _has_quota(rate_limits):
    """True when the payload carries usable subscription quota data."""
    rl = rate_limits or {}
    for win_key in ("five_hour", "seven_day"):
        if (rl.get(win_key) or {}).get("used_percentage") is not None:
            return True
    return False


def _daily_budget():
    """STATUSLINE_DAILY_BUDGET as a positive float (funny-money $/day), else None.

    Malformed / zero / negative -> None (treated as unset).
    """
    raw = pref("STATUSLINE_DAILY_BUDGET")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


# Default "sane" burn rate ($/min) used to color the rate number when
# STATUSLINE_TARGET_RATE is unset. 0/negative/malformed disables coloring.
_DEFAULT_TARGET_RATE = 1.0


def _target_rate():
    """STATUSLINE_TARGET_RATE (prefs file or env) as a positive float ($/min),
    the default when unset, or None when explicitly disabled (0/negative/
    malformed -- which includes the "auto" sentinel, so callers that want the
    derive-or-default behavior must special-case "auto" before calling this; see
    _resolve_target_rate)."""
    raw = pref("STATUSLINE_TARGET_RATE")
    if raw is None:
        return _DEFAULT_TARGET_RATE
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _resolve_target_rate(rate_limits):
    """The target $/min that colors the rate number and fills the →$ arrow.

    Precedence:
      1. An explicit numeric STATUSLINE_TARGET_RATE (prefs file or env) always
         wins -- a positive float, or None (disabled) when it is 0/negative/
         malformed.
      2. Unset, or set to the literal "auto" sentinel: a subscription session
         derives the adaptive weekly-sustainable rate (remaining weekly quota $
         over time to reset) via pace.weekly_sustainable_rate. "auto" exists so
         a live prefs override can force the derive path even when a numeric env
         baseline is configured in settings.json.
      3. Otherwise (API-key / no quota, or weekly data too thin to derive) the
         flat _DEFAULT_TARGET_RATE.
    """
    raw = pref("STATUSLINE_TARGET_RATE")
    if raw is not None and raw.strip().lower() != "auto":
        return _target_rate()
    derived = weekly_sustainable_rate(rate_limits)
    return derived if derived is not None else _DEFAULT_TARGET_RATE


# Needle thresholds (chosen defaults; tune in practice).
_ON_TARGET_RATIO_MARGIN = 0.05  # within +/-5% of budget -> yin-yang
# Arrow color stays solid green until 100% over budget, then fades
# green->yellow->red, reaching full red at 300% over.
_BUDGET_GREEN_RATIO = 2.0
_BUDGET_RED_RATIO = 4.0


def _budget_needle(spend_24h, budget):
    """Colored arrow/yin-yang from the 24h spend integral vs the daily budget.

    r = spend_24h / budget. |r-1|<=margin -> green yin-yang (on budget); else a
    down arrow (r<1) or up arrow (r>1) whose color is solid green up to 2x budget
    (100% over), then fades green->yellow->red, full red at 4x (300% over). Empty
    when there is no budget or no 24h spend to judge.
    """
    if not budget or budget <= 0 or spend_24h <= 0:
        return ""
    ratio = spend_24h / budget
    if abs(ratio - 1.0) <= _ON_TARGET_RATIO_MARGIN:
        return f"{GREEN}{ON_TARGET_GLYPH}{RESET}"
    arrow = ARROW_DOWN if ratio < 1.0 else ARROW_UP
    return (
        f"{ramp_color_for(ratio, _BUDGET_GREEN_RATIO, _BUDGET_RED_RATIO)}{arrow}{RESET}"
    )


def format_burn_rate(rate_limits, show_target=True):
    """Render `$X.XX/min [→$T.TT] <needle>` (colored rate + optional target arrow + needle), or "".

    Rate is the live 5-min global rate. Needle: weekly forecast for subscription
    sessions, 24h-integral budget ratio for API-key sessions, empty otherwise.
    The target rate (both the →$ arrow and the rate-number coloring) is the
    adaptive weekly-sustainable rate on subscription sessions, the flat default
    otherwise, or an explicit STATUSLINE_TARGET_RATE override -- see
    _resolve_target_rate. `show_target=False` (compact mode) drops the arrow.
    """
    rate = _five_min_rate()
    subscription = _has_quota(rate_limits)
    budget = None if subscription else _daily_budget()
    if rate <= 0 and not subscription and budget is None:
        return ""
    if subscription:
        needle = weekly_needle(rate_limits)
    elif budget is not None:
        needle = _budget_needle(_window_spend_cached(_now_unix() - 86400), budget)
    else:
        needle = ""
    target = _resolve_target_rate(rate_limits)
    rate_str = f"${rate:.2f}/min"
    if target is not None and rate > 0:
        body = f"{_rate_color(rate, target)}{rate_str}{RESET}"
    else:
        body = f"{RATE_COLOR}{rate_str}{RESET}"
    target_part = ""
    if show_target and target is not None and rate > 0:
        target_part = f" {GREEN}→${target:.2f}{RESET}"
    if target_part and needle:
        return f"{body}{target_part} {needle}"
    return f"{body}{target_part}{needle}"


def _local_midnight_unix():
    """Unix ts of the most recent local midnight (start of today, local time)."""
    now_local = datetime.fromtimestamp(_now_unix())
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def format_day_budget(rate_limits):
    """` day: NN%` (since-midnight global spend / budget), or "".

    Only for API-key sessions (no quota data) with a valid budget. The midnight
    boundary is artificial: it just defines "today"; there is no projection.
    """
    if _has_quota(rate_limits):
        return ""
    budget = _daily_budget()
    if budget is None:
        return ""
    today_spend = _window_spend_cached(_local_midnight_unix())
    pct = 100.0 * today_spend / budget
    return f"day: {color_high_bad(pct, 75, 90)}"
