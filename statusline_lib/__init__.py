"""Shared rendering helpers for the schoen-claude-status statuslines.

Used by:
  statusline.py            -- main session: up to 3-line layout
  subagent_statusline.py   -- per-agent panel rows: 1-line metrics

Cost handling differs between the two callers:
  Main script shows the authoritative `cost.total_cost_usd` from the stdin
  payload for the PARENT session, then adds our own estimate of subagent spend.
  The payload is parent-only -- subagents run as isolated sessions invisible to
  it (Claude Code issue #48040) -- so the authoritative figure alone undercounts
  subagent-heavy sessions. Subagent script derives cost from the agent JSONL
  since the per-task fields don't include cost. The walker computes parent and
  subagent costs in one pass so neither path needs a second iteration.

Per-Mtok rates, the TTL-split cache-write multipliers (1.25x for 5-minute
writes, 2.0x for 1-hour writes - see _write_cost), and the $0.01/web-search
charge match the canonical constants in ~/.claude/CLAUDE.md ("Cost-estimation
formula") and were verified against ~/.claude.json's authoritative per-model
costUSD: our formula matches the harness to the penny across the fleet (every
deviating project reconciles exactly once 1-hour writes bill at 2.0x). The Opus
1M-context tier doubling is NOT modeled because the harness does not apply it
(official "Long context pricing": the full 1M window bills at standard rates).

Package layout (dependency order, no cycles):
  base     -- _json_loads (orjson fallback), color constants, fmt, color helpers
  prefs    -- live ~/.claude/.statusline-prefs.json resolver (pref/pref_bool)
  sessions -- session counting (psutil lazy), debounce state
  walker   -- binary discovery, root resolution, native pace bridge
  cost     -- cost calc, transcript walking, context/cache/model-badge formatting
  beacon   -- beacon scanning, format_beacon, format_calibrated_eta, session timing
  diffstat -- format_lines (session +/- line counts for line 2)
  pace     -- pace walking, format_quota
"""

# ruff: noqa: F401
# All imports below are intentional public/private re-exports that make the
# package API identical to the old flat module.  Many underscore names are
# imported by external code (verify scripts, install.py) via
# `statusline_lib._name`, so they MUST appear as package attributes even
# though nothing inside this file calls them.

from .badge import (
    _MODEL_BADGES,
    COMPACT_BUFFER_TOKENS,
    ORANGE_THRESHOLD_1M_TOKENS,
    RED_MARGIN_TOKENS,
    _qwen_size_for,
    _qwen_version_for,
    _version_for,
    ctx_window_for_model,
    format_context,
    format_model_badge,
)
from .base import (
    CACHE_READ,
    CACHE_WRITE,
    CTX_DENOM,
    GREEN,
    ORANGE,
    RAMP,
    RED,
    RESET,
    YELLOW,
    _json_loads,
    color_high_bad,
    color_high_good,
    fmt,
    ramp_color,
    ramp_color_for,
)
from .beacon import (
    _BEACON_BLOCK_RE,
    _BEACON_DRIFT_COLOR,
    _BEACON_STALE_SECONDS,
    _BIAS_CACHE_PATH,
    _BIAS_CACHE_TTL_SECONDS,
    _CALIBRATION_MIN_PAIRS,
    _DRIFT_MATERIAL_ELAPSED_SECONDS,
    _DRIFT_MATERIAL_RATIO,
    _DRIFT_MODERATE_RATIO,
    _SESSION_TIMING_COLOR,
    _apply_beacon,
    _bias_factor_cached,
    _compute_objective_drift,
    _find_beacon_anchors,
    _find_session_jsonl,
    _fmt_duration_ms,
    _format_clock_and_elapsed,
    _iter_assistant_beacons,
    _iter_beacons_in_text,
    _scan_beacon_anchors,
    format_beacon,
    format_calibrated_eta,
    format_session_timing,
)
from .burnrate import (
    RATE_COLOR,
    _daily_budget,
    _five_min_rate,
    _has_quota,
    _local_midnight_unix,
    _sum_window_spend,
    _window_spend_cached,
    format_burn_rate,
    format_day_budget,
)
from .compact import (
    DROP_ORDER,
    full_flags,
    resolve_flags,
    terminal_columns,
    visible_width,
)
from .cost import (
    _RATES,
    _WEB_SEARCH_COST_USD,
    TTL_MIN_WRITE_TOKENS,
    _accumulate_assistant_turn,
    _cost_for_turn,
    _rates_for,
    _walk_one_transcript,
    walk_transcript,
)
from .costfmt import (
    _COST_DRIFT_MAJOR_THRESHOLD,
    _COST_DRIFT_OVER_COLOR,
    _COST_DRIFT_OVER_MAJOR_COLOR,
    _COST_DRIFT_THRESHOLD,
    _COST_DRIFT_UNDER_COLOR,
    _COST_DRIFT_UNDER_MAJOR_COLOR,
    _SUBAGENT_COST_COLOR,
    _SUM_COST_THRESHOLD_RED,
    _SUM_COST_THRESHOLD_YELLOW,
    TTL_WARN_GLYPH,
    _cost_threshold_color,
    _sum_threshold_color,
    format_cache,
    format_cost,
    format_cost_with_subagents,
    format_ttl,
)
from .diffstat import (
    format_lines,
)
from .pace import (
    _PACE_CACHE_TTL_SECONDS,
    _PACE_HOURLY_CACHE_PATH,
    ARROW_DOWN,
    ARROW_UP,
    ON_TARGET_GLYPH,
    _discover_pace_groups,
    _fmt_delta_hours,
    _now_unix,
    _pace_hourly_cached,
    _pace_hourly_for_file,
    _parse_pace_line,
    _project_pace,
    _sum_hourly,
    _walk_hourly_inline,
    _walk_hourly_parallel,
    _walk_pace_hourly,
    _walk_session_hourly,
    _weekly_deltas,
    format_quota,
    weekly_needle,
)
from .prefs import (
    load_prefs,
    pref,
    pref_bool,
    prefs_path,
)
from .qwen import (
    format_qwen_api_stats,
    format_qwen_cache,
    format_qwen_files,
    format_qwen_thinking,
    format_qwen_tokens,
)
from .sessions import (
    _SESSION_DEBOUNCE_DWELL_SECONDS,
    _count_via_psutil,
    _load_debounce_state,
    _load_session_count_cache,
    _process_matches,
    _resolve_psutil,
    _save_debounce_state,
    _save_session_count_cache,
    count_active_sessions,
    debounce_session_count,
)
from .walker import (
    _WALKER_BIN_ENV,
    _WALKER_ROOTS_CONFIG_PATH,
    _find_walker_binary,
    _walker_root_list,
    _walker_subcommand,
)

__all__ = [
    "CACHE_READ",
    "CACHE_WRITE",
    "COMPACT_BUFFER_TOKENS",
    "CTX_DENOM",
    "DROP_ORDER",
    "GREEN",
    "ORANGE",
    "RAMP",
    "RED",
    "RED_MARGIN_TOKENS",
    "RESET",
    "YELLOW",
    "color_high_bad",
    "color_high_good",
    "count_active_sessions",
    "ctx_window_for_model",
    "debounce_session_count",
    "fmt",
    "format_beacon",
    "format_burn_rate",
    "format_cache",
    "format_calibrated_eta",
    "format_context",
    "format_cost",
    "format_cost_with_subagents",
    "format_day_budget",
    "format_lines",
    "format_model_badge",
    "format_quota",
    "format_qwen_api_stats",
    "format_qwen_cache",
    "format_qwen_files",
    "format_qwen_thinking",
    "format_qwen_tokens",
    "format_session_timing",
    "format_ttl",
    "full_flags",
    "load_prefs",
    "pref",
    "pref_bool",
    "prefs_path",
    "ramp_color",
    "ramp_color_for",
    "resolve_flags",
    "terminal_columns",
    "visible_width",
    "walk_transcript",
    "weekly_needle",
]
