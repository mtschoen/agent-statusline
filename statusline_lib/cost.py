"""Cost calculation, transcript walking, context/cache formatting, model badge.

Imports:
  base -- for color constants, _json_loads, fmt, color_high_bad, color_high_good
"""

import contextlib
import glob
import os
import re as _re

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
    color_high_good,
    fmt,
)

_RATES = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}

_WEB_SEARCH_COST_USD = 0.01


def _rates_for(model_id):
    mid = (model_id or "").lower()
    for key, rates in _RATES.items():
        if key in mid:
            return rates
    # Unknown family -- fall back to sonnet rates rather than zero so an
    # unrecognized model doesn't silently render as free.
    return _RATES["sonnet"]


def _cost_for_turn(usage, model_id):
    """Per-Mtok token cost for one assistant turn, plus per-request web search.

    Web search is billed per request, not per token; $0.01 each was verified
    against ~/.claude.json's authoritative per-model costUSD.
    """
    inp_rate, out_rate = _rates_for(model_id)
    i = int(usage.get("input_tokens") or 0)
    r = int(usage.get("cache_read_input_tokens") or 0)
    w = int(usage.get("cache_creation_input_tokens") or 0)
    o = int(usage.get("output_tokens") or 0)
    web_searches = int(
        (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
    )
    token_cost = (
        i * inp_rate + r * (inp_rate * 0.1) + w * (inp_rate * 1.25) + o * out_rate
    ) / 1_000_000.0
    return token_cost + web_searches * _WEB_SEARCH_COST_USD


def _accumulate_assistant_turn(entry, acc, seen_ids):
    """Fold one transcript line into the running totals `acc`. No-op for
    non-assistant turns and for duplicate message ids."""
    msg = entry.get("message") or {}
    if msg.get("role") != "assistant":
        return
    mid = msg.get("id")
    if mid:
        # transcripts repeat assistant turns under one message.id (snapshots/
        # checkpoints carry the same usage); count once.
        if mid in seen_ids:
            return
        seen_ids.add(mid)
    u = msg.get("usage") or {}
    r = int(u.get("cache_read_input_tokens") or 0)
    w = int(u.get("cache_creation_input_tokens") or 0)
    i = int(u.get("input_tokens") or 0)
    o = int(u.get("output_tokens") or 0)
    acc["read"] += r
    acc["write"] += w
    acc["input"] += i
    acc["output"] += o
    model_id = msg.get("model") or ""
    if model_id:
        acc["last_model"] = model_id
    acc["cost"] += _cost_for_turn(u, model_id or acc["last_model"])
    acc["last_input"] = i
    acc["last_cache_create"] = w
    acc["last_cache_read"] = r


def _walk_one_transcript(path, acc, seen_ids):
    """Stream one JSONL transcript, folding each line into `acc`."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = _json_loads(line)
                except Exception:
                    continue
                _accumulate_assistant_turn(entry, acc, seen_ids)
    except OSError:
        # Transcript became unreadable mid-walk; use the totals gathered so far
        # rather than failing the whole render.
        pass


def walk_transcript(path, include_subagents=False):
    """Sum cache/input/output tokens, compute cost, snapshot most-recent turn.

    Returns:
      cache_read, cache_write, input_total, output_total -- session sums
      cost                                               -- $, derived (parent + subagents)
      parent_cost, subagent_cost                         -- $ split (subagent_cost 0 unless include_subagents)
      last_model_id                                      -- model on most recent assistant turn
      last_input, last_cache_create, last_cache_read     -- usage of most recent turn
                                                            (used to derive ctx_used at "now")

    `include_subagents=True` (main script) also walks
    <path-without-.jsonl>/subagents/agent-*.jsonl so the cache total reflects
    everything attributed to this session. The subagent script passes False.
    """
    acc = {
        "read": 0,
        "write": 0,
        "input": 0,
        "output": 0,
        "cost": 0.0,
        "last_model": "",
        "last_input": 0,
        "last_cache_create": 0,
        "last_cache_read": 0,
    }
    seen_ids = set()

    parent_cost = 0.0
    if path and os.path.exists(path):
        _walk_one_transcript(path, acc, seen_ids)
        parent_cost = acc["cost"]
        if include_subagents and path.endswith(".jsonl"):
            sub_dir = path[:-6] + "/subagents"
            if os.path.isdir(sub_dir):
                for sub in glob.glob(os.path.join(sub_dir, "agent-*.jsonl")):
                    _walk_one_transcript(sub, acc, seen_ids)

    return {
        "read": acc["read"],
        "write": acc["write"],
        "input": acc["input"],
        "output": acc["output"],
        "cost": acc["cost"],
        "parent_cost": parent_cost,
        "subagent_cost": acc["cost"] - parent_cost,
        "last_model_id": acc["last_model"],
        "last_input": acc["last_input"],
        "last_cache_create": acc["last_cache_create"],
        "last_cache_read": acc["last_cache_read"],
    }


COMPACT_BUFFER_TOKENS = 33_000
RED_MARGIN_TOKENS = 20_000
ORANGE_THRESHOLD_1M_TOKENS = 500_000  # mid-band warning for 1M-context sessions


def ctx_window_for_model(model_id):
    """Best-effort window inference for per-agent rendering. Opus [1m] -> 1M,
    everything else -> 200K. The main script doesn't need this -- the payload
    carries `context_window.context_window_size` directly."""
    return 1_000_000 if "[1m]" in (model_id or "") else 200_000


def format_context(ctx_used, window_size, model_id=""):
    """`usedK / windowK (P.P%)` colored by token-anchored thresholds.

    Yellow at 200K for 1M models (Opus 1M pricing boundary), at 50% otherwise.
    1M models also get an orange mid-band at 500K so the huge yellow
    span between the pricing boundary and auto-compact has a visible
    midpoint cue. Red at `window_size - 33K compact buffer - 20K
    headroom`; tracks CLAUDE_AUTOCOMPACT_PCT_OVERRIDE if set.
    """
    if window_size <= 0:
        return ""
    override = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    compact_tokens = max(0, window_size - COMPACT_BUFFER_TOKENS)
    if override:
        with contextlib.suppress(ValueError):
            compact_tokens = int(window_size * float(override) / 100)
    red_tokens = max(0, compact_tokens - RED_MARGIN_TOKENS)
    is_1m = window_size >= 1_000_000 or "[1m]" in (model_id or "")
    yellow_tokens = 200_000 if is_1m else window_size // 2
    if ctx_used >= red_tokens:
        ctx_color = RED
    elif is_1m and ctx_used >= ORANGE_THRESHOLD_1M_TOKENS:
        ctx_color = ORANGE
    elif ctx_used >= yellow_tokens:
        ctx_color = YELLOW
    else:
        ctx_color = GREEN
    pct = 100.0 * ctx_used / window_size
    return (
        f"{ctx_color}{fmt(ctx_used)}{RESET} / "
        f"{CTX_DENOM}{fmt(window_size)}{RESET} "
        f"({ctx_color}{pct:.1f}%{RESET})"
    )


def format_cache(read, write, input_t):
    total_in = read + write + input_t
    if total_in <= 0:
        return ""
    hit_pct = read * 100.0 / total_in
    return (
        f"{CACHE_READ}{fmt(read)}{RESET} / "
        f"{CACHE_WRITE}{fmt(write)}{RESET} / "
        f"{color_high_good(hit_pct, 90, 75)} hit"
    )


def _cost_threshold_color(cost):
    """Magnitude band shared by the parent figure and the subagent addend:
    green < $25, yellow < $50, red >= $50."""
    return RED if cost >= 50 else YELLOW if cost >= 25 else GREEN


_SUM_COST_THRESHOLD_YELLOW = 35  # combined parent+subagent total >= this -> yellow
_SUM_COST_THRESHOLD_RED = 70  # combined parent+subagent total >= this -> red


def _sum_threshold_color(cost):
    """Magnitude band for the combined parent+subagent total (the `= $total`
    segment). Breakpoints sit higher than the per-figure bands
    (_cost_threshold_color, 25/50): a total runs bigger than its parts, so
    reusing 25/50 would peg the sum red on routine sessions. 35/70 splits the
    difference -- it flags a combined burn that two individually-modest figures
    can hide, without crying wolf."""
    return (
        RED
        if cost >= _SUM_COST_THRESHOLD_RED
        else YELLOW
        if cost >= _SUM_COST_THRESHOLD_YELLOW
        else GREEN
    )


def format_cost(cost):
    if cost is None or cost <= 0:
        return ""
    return f"{_cost_threshold_color(cost)}${cost:.2f}{RESET}"


# The subagent addend carries the same magnitude bands as the parent figure
# (green/yellow/red via _cost_threshold_color). Its trailing "~" is the estimate
# marker, and its COLOR is the drift signal -- grey when our formula tracks the
# harness, else tinted by the DIRECTION and SEVERITY of the divergence:
#   under-estimate (our_parent < authoritative -- shown cost low, you may pay MORE):
#     * moderate (> 4%):  caution-orange
#     * way off  (> 25%): deep red -- a structural miss (rate hike, a new billed
#       dimension we don't model yet, or 1M-tier doubling); you may be paying
#       WAY more than shown.
#   over-estimate (our_parent > authoritative -- shown cost high, you pay LESS):
#     * moderate (> 4%):  cyan -- reassuring
#     * way off  (> 25%): bright pink -- our estimate is wildly high (e.g. a rate
#       CUT we haven't caught -- Anthropic dropped Opus 3x at 4.5); you're paying
#       WAY less than shown.
# Thresholds are tight because our formula matches the harness's parent
# total_cost_usd to the penny in practice, so even a few percent is a real signal.
_SUBAGENT_COST_COLOR = "\x1b[38;5;245m"  # grey -- "~" tracks the harness
_COST_DRIFT_UNDER_COLOR = ORANGE  # under, moderate: you may pay MORE
_COST_DRIFT_UNDER_MAJOR_COLOR = "\x1b[38;5;124m"  # under, way off: deep red
_COST_DRIFT_OVER_COLOR = "\x1b[38;5;51m"  # over, moderate: cyan, you pay LESS
_COST_DRIFT_OVER_MAJOR_COLOR = "\x1b[38;5;198m"  # over, way off: bright pink
_COST_DRIFT_THRESHOLD = 0.04  # flag at all
_COST_DRIFT_MAJOR_THRESHOLD = 0.25  # "way off"


def format_cost_with_subagents(authoritative_parent, our_parent, subagent_cost):
    """Render `($parent + $sub~) = $total`: authoritative parent + estimated subagent
    spend, then their sum.

    The parent and subagent figures carry the same per-figure magnitude bands
    (green/yellow/red via _cost_threshold_color). The trailing `= $total` is their
    sum and carries its OWN, higher bands (_sum_threshold_color: green < $35,
    yellow < $70, red >= $70) -- two individually-modest figures can add up to a
    combined burn worth flagging that neither part shows alone. The parent is
    the harness's authoritative `total_cost_usd` (ground truth, but PARENT-ONLY
    -- subagents are invisible to it). The subagent figure is our formula's
    estimate, marked with a trailing "~".

    The "~" closes the loop on drift. Drift is a PARENT-side measurement:
    `our_parent` is our formula over the same parent turns as the authoritative
    figure, so a gap means our cost formula has diverged from the harness. We
    tint the estimate marker by the DIRECTION and SEVERITY of that gap: grey when
    it tracks the harness; over-estimate (shown high, you pay less) is cyan,
    escalating to bright pink past 25%; under-estimate (shown low, you may pay
    more) is caution-orange, escalating to deep red past 25%. The same formula
    produces the subagent figure, so the direction carries over; it is NOT a
    per-subagent measurement (no ground truth exists for subagents).

    With no subagent cost the result is just the authoritative figure -- byte
    identical to the pre-subagent behavior.
    """
    parent_part = format_cost(authoritative_parent)
    if not subagent_cost or subagent_cost <= 0:
        return parent_part
    drift = 0.0
    if authoritative_parent and authoritative_parent > 0:
        drift = (our_parent - authoritative_parent) / authoritative_parent
    magnitude = abs(drift)
    major = magnitude > _COST_DRIFT_MAJOR_THRESHOLD
    if magnitude <= _COST_DRIFT_THRESHOLD:
        tilde_color = _SUBAGENT_COST_COLOR  # tracks the harness
    elif drift > 0:  # over-estimate -> you pay LESS
        tilde_color = _COST_DRIFT_OVER_MAJOR_COLOR if major else _COST_DRIFT_OVER_COLOR
    else:  # under-estimate -> you may pay MORE
        tilde_color = (
            _COST_DRIFT_UNDER_MAJOR_COLOR if major else _COST_DRIFT_UNDER_COLOR
        )
    addend = (
        f"{_cost_threshold_color(subagent_cost)}+ ${subagent_cost:.2f}{RESET}"
        f"{tilde_color}~{RESET}"
    )
    body = f"{parent_part} {addend}" if parent_part else addend
    # The sum is meaningful only when there's a parent figure to add to. With no
    # authoritative parent (rare -- payload hasn't delivered a cost yet), `= $total`
    # would just echo the subagent addend, so drop it but still surface the spend.
    if not parent_part:
        return body
    total = authoritative_parent + subagent_cost
    return f"({body}) {_sum_threshold_color(total)}= ${total:.2f}{RESET}"


# Model-family badge: substring match -> short label + ANSI color. Distinct
# from threshold green/yellow/red and the cache identity teal/orange so a
# coloured badge never reads as a warning or a metric. Shared by the main and
# subagent statuslines.
_MODEL_BADGES = [
    (("opus",), "opus", "\x1b[35m"),  # magenta
    (("sonnet",), "sonnet", "\x1b[36m"),  # cyan
    (("haiku",), "haiku", "\x1b[34m"),  # blue
]


def _version_for(mid, key):
    """Extract a dotted `major.minor` version following the family `key` in a
    model id, e.g. `claude-opus-4-8` -> "4.8". Returns "" when no version
    component is present (e.g. an aliased id like `opus`).
    """
    match = _re.search(rf"{key}-(\d+)-(\d+)", mid)
    return f"{match.group(1)}.{match.group(2)}" if match else ""


def format_model_badge(model_id):
    """Colored short model-family badge, e.g. magenta `opus4.8[1m]`.

    Inserts the `major.minor` version when the id carries one and appends the
    `[1m]` runtime-tier suffix when present. Unknown families render as a mauve
    `?`; an empty id returns "" so the caller can omit the segment.
    """
    if not model_id:
        return ""
    mid = model_id.lower()
    suffix = "[1m]" if "[1m]" in mid else ""
    for keys, label, color in _MODEL_BADGES:
        for key in keys:
            if key in mid:
                version = _version_for(mid, key)
                return f"{color}{label}{version}{suffix}{RESET}"
    return f"{CTX_DENOM}?{RESET}"
