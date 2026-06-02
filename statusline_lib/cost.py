"""Cost calculation and transcript walking.

Context/model-badge rendering moved to badge.py to keep this file
within the 400-line guideline.

Imports:
  base  -- for color constants, _json_loads, fmt, color_high_good
  badge -- for COMPACT_BUFFER_TOKENS, RED_MARGIN_TOKENS (re-exported here
           for back-compat with external callers via statusline_lib.cost)
"""

import glob
import os
from datetime import datetime

from .base import (
    CACHE_READ,
    CACHE_WRITE,
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

# A turn with cache_read==0 and cache_write>0 after the first parent turn is a
# cache rewrite. The floor suppresses degenerate tiny-write turns from counting.
# Tunable.
TTL_MIN_WRITE_TOKENS = 1000

# ...but a rewrite only counts as a *TTL* eviction when the idle gap since the
# previous turn exceeds the lifetime the prior turn's cache was written with. A
# rewrite seconds after the prior turn is a tool-array/compaction/resume bust
# (e.g. ToolSearch loading a deferred tool reorders the tool block and busts the
# prefix), not an idle timeout - so it must NOT be blamed on TTL. The lifetime is
# not fixed: subscription auth writes 1h cache, API-key/Bedrock/Vertex default to
# 5m, so the gate derives the threshold per-turn from the usage breakdown rather
# than assuming one value. With no timestamps the gap is unknowable and nothing
# counts (conservative).
TTL_5M_SECONDS = 300
TTL_1H_SECONDS = 3600


def _parse_ts(value):
    """Parse a transcript ISO-8601 timestamp to epoch seconds, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _written_ttl_seconds(usage):
    """Lifetime (s) of the cache this turn wrote, from the ephemeral breakdown.

    `cache_creation.ephemeral_{5m,1h}_input_tokens` tells us which TTL the write
    used. Falls back to the longer 1h lifetime when the breakdown is absent so an
    unknown write is treated conservatively (a longer gap is required to blame an
    eviction on TTL).
    """
    creation = usage.get("cache_creation") or {}
    hour = int(creation.get("ephemeral_1h_input_tokens") or 0)
    five_min = int(creation.get("ephemeral_5m_input_tokens") or 0)
    if hour or five_min:
        return TTL_1H_SECONDS if hour >= five_min else TTL_5M_SECONDS
    return TTL_1H_SECONDS


# U+FE0E text-presentation selector keeps the warning glyph monochrome so the
# ANSI red wins cross-platform (Windows Terminal would otherwise color-font it),
# matching the on-target yin-yang's treatment.
TTL_WARN_GLYPH = "⚠︎"


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
    acc["assistant_turns"] += 1
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
    rate_model = model_id or acc["last_model"]
    acc["cost"] += _cost_for_turn(u, rate_model)
    inp_rate, _out_rate = _rates_for(rate_model)
    acc["read_cost"] += r * inp_rate * 0.1 / 1_000_000.0
    acc["write_cost"] += w * inp_rate * 1.25 / 1_000_000.0
    # TTL eviction: parent-only non-first turn with full rewrite (no read) above
    # floor AND an idle gap since the prior turn exceeding the TTL the prior turn's
    # cache was written with (so a seconds-later tool-array/compaction bust, and a
    # warm gap under the cache lifetime, are both excluded); wasted = 1.15x penalty.
    cur_ts = _parse_ts(entry.get("timestamp"))
    prev_ts = acc.get("last_turn_ts")
    prev_ttl = acc.get("last_turn_ttl_seconds") or TTL_1H_SECONDS
    idle_gap_exceeded = (
        prev_ts is not None and cur_ts is not None and (cur_ts - prev_ts) > prev_ttl
    )
    if (
        acc.get("track_evictions")
        and acc["assistant_turns"] > 1
        and r == 0
        and w >= TTL_MIN_WRITE_TOKENS
        and idle_gap_exceeded
    ):
        acc["ttl_evictions"] += 1
        acc["ttl_wasted"] += w * inp_rate * 1.15 / 1_000_000.0
    acc["last_turn_ts"] = cur_ts
    acc["last_turn_ttl_seconds"] = _written_ttl_seconds(u)
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
        "read_cost": 0.0,
        "write_cost": 0.0,
        "ttl_evictions": 0,
        "ttl_wasted": 0.0,
        "assistant_turns": 0,
        "track_evictions": False,
        "last_model": "",
        "last_input": 0,
        "last_cache_create": 0,
        "last_cache_read": 0,
        "last_turn_ts": None,
        "last_turn_ttl_seconds": None,
    }
    seen_ids = set()

    parent_cost = 0.0
    if path and os.path.exists(path):
        # Eviction tracking is parent-only: a subagent's first turn is a full
        # write by construction and isn't user-controllable cache behavior.
        acc["track_evictions"] = True
        _walk_one_transcript(path, acc, seen_ids)
        parent_cost = acc["cost"]
        if include_subagents and path.endswith(".jsonl"):
            acc["track_evictions"] = False
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
        "read_cost": acc["read_cost"],
        "write_cost": acc["write_cost"],
        "ttl_evictions": acc["ttl_evictions"],
        "ttl_wasted": acc["ttl_wasted"],
        "parent_cost": parent_cost,
        "subagent_cost": acc["cost"] - parent_cost,
        "last_model_id": acc["last_model"],
        "last_input": acc["last_input"],
        "last_cache_create": acc["last_cache_create"],
        "last_cache_read": acc["last_cache_read"],
    }


def format_cache(
    read,
    write,
    input_t,
    read_cost=None,
    write_cost=None,
    show_costs=True,
    show_hit=True,
):
    """`reads (cost) / writes (cost) / hit %`.

    The per-figure $ parens render only when both cost args are supplied AND
    `show_costs` (compact mode and the subagent caller pass neither/False, in
    which case the output is byte-identical to the pre-cost format). The $ inside
    each paren reuses that figure's identity color (teal read, orange write).
    `show_hit=False` drops the trailing hit%.
    """
    total_in = read + write + input_t
    if total_in <= 0:
        return ""
    with_costs = show_costs and read_cost is not None and write_cost is not None
    read_part = f"{CACHE_READ}{fmt(read)}{RESET}"
    write_part = f"{CACHE_WRITE}{fmt(write)}{RESET}"
    if with_costs:
        read_part += f" {CACHE_READ}(${read_cost:.2f}){RESET}"
        write_part += f" {CACHE_WRITE}(${write_cost:.2f}){RESET}"
    segment = f"{read_part} / {write_part}"
    if show_hit:
        hit_pct = read * 100.0 / total_in
        segment += f" / {color_high_good(hit_pct, 90, 75)} hit"
    return segment


def format_ttl(evictions, wasted, show_wasted=True):
    """Loud red cache-eviction counter, or "" when there were no evictions.

    `⚠ TTL:N` plus ` (~$X.XX)` of estimated wasted spend when `show_wasted`.
    Whole segment is red - it is a problem signal, not a routine metric.
    """
    if not evictions or evictions <= 0:
        return ""
    segment = f"{RED}{TTL_WARN_GLYPH} TTL:{evictions}{RESET}"
    if show_wasted and wasted and wasted > 0:
        segment += f" {RED}(~${wasted:.2f}){RESET}"
    return segment


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
