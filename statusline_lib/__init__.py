"""Shared rendering helpers for the schoen-claude-status statuslines.

Used by:
  statusline.py            -- main session: 2-line layout
  subagent_statusline.py   -- per-agent panel rows: 1-line metrics

Cost handling differs between the two callers:
  Main script shows the authoritative `cost.total_cost_usd` from the stdin
  payload for the PARENT session, then adds our own estimate of subagent spend.
  The payload is parent-only -- subagents run as isolated sessions invisible to
  it (Claude Code issue #48040) -- so the authoritative figure alone undercounts
  subagent-heavy sessions. Subagent script derives cost from the agent JSONL
  since the per-task fields don't include cost. The walker computes parent and
  subagent costs in one pass so neither path needs a second iteration.

Per-Mtok rates, the 1.25x cache-write multiplier, and the $0.01/web-search
charge match the canonical constants in ~/.claude/CLAUDE.md ("Cost-estimation
formula") and were verified against ~/.claude.json's authoritative per-model
costUSD: our formula matches the harness to the penny across the fleet (Opus,
and -- once web search is included -- every model). The Opus 1M-context tier
doubling is NOT modeled because the harness does not apply it in practice:
measured 0% error on 28 Opus sessions, including 26M-cache-read ones.

Package layout (dependency order, no cycles):
  base     -- _json_loads (orjson fallback), color constants, fmt, color helpers
  sessions -- session counting (psutil lazy), debounce state
  walker   -- binary discovery, root resolution, native pace bridge
  cost     -- cost calc, transcript walking, context/cache/model-badge formatting
  beacon   -- beacon scanning, format_beacon, format_calibrated_eta
  pace     -- pace walking, format_quota
"""

# ruff: noqa: F401
# All imports below are intentional public/private re-exports that make the
# package API identical to the old flat module.  Many underscore names are
# imported by external code (verify scripts, install.py) via
# `statusline_lib._name`, so they MUST appear as package attributes even
# though nothing inside this file calls them.

from .base import (
    CACHE_READ,
    CACHE_WRITE,
    CTX_DENOM,
    GREEN,
    ORANGE,
    RED,
    RESET,
    YELLOW,
    _json_loads,
    color_high_bad,
    color_high_good,
    fmt,
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
    _apply_beacon,
    _bias_factor_cached,
    _compute_objective_drift,
    _find_beacon_anchors,
    _find_session_jsonl,
    _format_clock_and_elapsed,
    _iter_assistant_beacons,
    _iter_beacons_in_text,
    _scan_beacon_anchors,
    format_beacon,
    format_calibrated_eta,
)
from .cost import (
    _COST_DRIFT_MAJOR_THRESHOLD,
    _COST_DRIFT_OVER_COLOR,
    _COST_DRIFT_OVER_MAJOR_COLOR,
    _COST_DRIFT_THRESHOLD,
    _COST_DRIFT_UNDER_COLOR,
    _COST_DRIFT_UNDER_MAJOR_COLOR,
    _MODEL_BADGES,
    _RATES,
    _SUBAGENT_COST_COLOR,
    _SUM_COST_THRESHOLD_RED,
    _SUM_COST_THRESHOLD_YELLOW,
    _WEB_SEARCH_COST_USD,
    COMPACT_BUFFER_TOKENS,
    ORANGE_THRESHOLD_1M_TOKENS,
    RED_MARGIN_TOKENS,
    _accumulate_assistant_turn,
    _cost_for_turn,
    _cost_threshold_color,
    _rates_for,
    _sum_threshold_color,
    _version_for,
    _walk_one_transcript,
    ctx_window_for_model,
    format_cache,
    format_context,
    format_cost,
    format_cost_with_subagents,
    format_model_badge,
    walk_transcript,
)
from .pace import (
    _PACE_CACHE_PATH,
    _PACE_CACHE_TTL_SECONDS,
    _discover_pace_groups,
    _fmt_delta_hours,
    _pace_buckets_cached,
    _pace_costs_for_file,
    _parse_pace_line,
    _project_pace,
    _walk_groups_inline,
    _walk_groups_parallel,
    _walk_pace_buckets,
    _walk_session_group,
    format_quota,
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
    _walk_pace_buckets_native,
    _walker_root_list,
    _walker_subcommand,
)

__all__ = [
    "CACHE_READ",
    "CACHE_WRITE",
    # cost
    "COMPACT_BUFFER_TOKENS",
    "CTX_DENOM",
    "GREEN",
    "ORANGE",
    # base
    "RED",
    "RED_MARGIN_TOKENS",
    "RESET",
    "YELLOW",
    "color_high_bad",
    "color_high_good",
    # sessions
    "count_active_sessions",
    "ctx_window_for_model",
    "debounce_session_count",
    "fmt",
    # beacon
    "format_beacon",
    "format_cache",
    "format_calibrated_eta",
    "format_context",
    "format_cost",
    "format_cost_with_subagents",
    "format_model_badge",
    # pace
    "format_quota",
    "walk_transcript",
]
