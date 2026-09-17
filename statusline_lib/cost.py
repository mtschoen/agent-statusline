"""Cost calculation and transcript walking.

The cost/cache *rendering* helpers (format_cache, format_ttl, format_cost,
format_cost_with_subagents and their color/threshold constants) live in the
sibling costfmt.py; this file keeps the transcript walk + per-turn
accumulation. They were once one module - the split keeps each under the aislop
400-line file gate. The package __init__ aggregates the public API from both, so
callers use `statusline_lib.format_cache` / `statusline_lib.walk_transcript`
regardless of which module defines them.

Imports:
  base -- _json_loads (the walk's only base dependency)
"""

import glob
import os
from datetime import datetime

from .base import _json_loads

_RATES = {
    "fable": (10.0, 50.0),
    "opus": (5.0, 25.0),
    # Sonnet 5's $2/$10 launch pricing was originally introductory, with a
    # scheduled increase to $3/$15 on 2026-09-01. That increase was announced
    # and then CANCELLED before it took effect: $2/$10 is now the standard,
    # unconditional price (platform.claude.com/docs/en/about-claude/pricing,
    # fetched 2026-09-17). No date switch -- Sonnet 5 always bills here.
    "sonnet-5": (2.0, 10.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}

_WEB_SEARCH_COST_USD = 0.01

# Cache-read multiplier (of the base input rate). 0.1x is the default for
# every model except Claude Fable 5.1 and Claude Mythos 5.1, which read cache
# at 0.025x (platform.claude.com/docs/en/about-claude/pricing). This is a
# per-MODEL distinction, not per-family: _RATES["fable"] covers fable-5,
# mythos-5, fable-5-1, and mythos-5-1 alike (they share the $10/$50 base
# rate), but only the 5.1 pair gets the cheaper read multiplier -- Fable 5 and
# Mythos 5 stay at 0.1x. Defined once here and consulted at both cost sites
# (_cost_for_turn and _accumulate_assistant_turn) so they can't drift apart.
CACHE_READ_MULT_DEFAULT = 0.1
CACHE_READ_MULT_FABLE_5_1 = 0.025

# Cache-write cost depends on the write's TTL: a 5-minute write bills at 1.25x
# base input, a 1-hour write at 2.0x (platform.claude.com/docs/en/about-claude/
# pricing -> the "5m Cache Writes" / "1h Cache Writes" columns). The split lives
# in usage.cache_creation.ephemeral_{5m,1h}_input_tokens. Claude Code subscription
# sessions write 1h cache, so a flat 1.25x under-bills them by the 0.75x delta -
# the dominant source of drift from the harness's authoritative total_cost_usd.
WRITE_MULT_5M = 1.25
WRITE_MULT_1H = 2.0

# A non-first parent turn writing at least this much cache after an idle gap
# that outlived the prior turn's written TTL counts as a rewrite (see the
# eviction gate in _accumulate_assistant_turn; deliberately no read-based
# condition). The floor suppresses degenerate tiny-write turns from counting.
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


def _rates_for(model_id):
    """(input_per_mtok, output_per_mtok) for a model id.

    Ordered substring match (first hit wins), mirroring agent-walker SPEC.md:
    fable/mythos -> opus -> haiku -> sonnet-5 -> generic sonnet.
    """
    mid = (model_id or "").lower()
    # fable/mythos first: the Fable family bills at $10/$50 and must win before
    # any substring that could collide.
    if "fable" in mid or "mythos" in mid:
        return _RATES["fable"]
    if "opus" in mid:
        return _RATES["opus"]
    if "haiku" in mid:
        return _RATES["haiku"]
    # sonnet-5 BEFORE the generic sonnet default. "sonnet-5" does NOT match
    # "claude-sonnet-4-5" (no such substring), so Sonnet 4.5 keeps the generic
    # sonnet rates below.
    if "sonnet-5" in mid:
        return _RATES["sonnet-5"]
    # sonnet -- and any unknown family -- falls back to sonnet rates rather than
    # zero so an unrecognized model doesn't silently render as free.
    return _RATES["sonnet"]


def _cache_read_mult(model_id):
    """Cache-read multiplier (of the base input rate) for a model id.

    Ordered substring match: "fable-5" is a substring of "fable-5-1", so the
    5.1 check must run first, or every Fable 5.1 (and Mythos 5.1) turn would
    silently fall through and be billed at the wrong (4x higher) rate.
    """
    mid = (model_id or "").lower()
    if "fable-5-1" in mid or "mythos-5-1" in mid:
        return CACHE_READ_MULT_FABLE_5_1
    return CACHE_READ_MULT_DEFAULT


def _write_cost(usage, inp_rate):
    """Dollar cost of this turn's cache writes, split by TTL.

    5-minute writes bill at WRITE_MULT_5M x base input, 1-hour writes at
    WRITE_MULT_1H x. The ephemeral_{5m,1h} breakdown in usage.cache_creation says
    which. When that breakdown is absent (older or API-key/Bedrock transcripts
    that only carry the flat cache_creation_input_tokens), fall back to the 5m
    multiplier - the single value used before the TTL split existed.
    """
    creation = usage.get("cache_creation") or {}
    five = int(creation.get("ephemeral_5m_input_tokens") or 0)
    hour = int(creation.get("ephemeral_1h_input_tokens") or 0)
    if five or hour:
        return (five * WRITE_MULT_5M + hour * WRITE_MULT_1H) * inp_rate / 1_000_000.0
    flat = int(usage.get("cache_creation_input_tokens") or 0)
    return flat * WRITE_MULT_5M * inp_rate / 1_000_000.0


def _cost_for_turn(usage, model_id):
    """Per-Mtok token cost for one assistant turn, plus per-request web search.

    Web search is billed per request, not per token; $0.01 each was verified
    against ~/.claude.json's authoritative per-model costUSD. Cache writes are
    billed by TTL via _write_cost (5m at 1.25x, 1h at 2.0x); cache reads by
    _cache_read_mult (0.1x, or 0.025x on Fable 5.1 / Mythos 5.1).
    """
    inp_rate, out_rate = _rates_for(model_id)
    i = int(usage.get("input_tokens") or 0)
    r = int(usage.get("cache_read_input_tokens") or 0)
    o = int(usage.get("output_tokens") or 0)
    web_searches = int(
        (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
    )
    token_cost = (
        i * inp_rate + r * (inp_rate * _cache_read_mult(model_id)) + o * out_rate
    ) / 1_000_000.0 + _write_cost(usage, inp_rate)
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
    inp_rate, out_rate = _rates_for(rate_model)
    acc["read_cost"] += r * inp_rate * _cache_read_mult(rate_model) / 1_000_000.0
    acc["write_cost"] += _write_cost(u, inp_rate)
    # The other two cost dimensions, so the full breakdown reconciles to total:
    # fresh (uncached) input at the plain input rate, output at the output rate.
    acc["input_cost"] += i * inp_rate / 1_000_000.0
    acc["output_cost"] += o * out_rate / 1_000_000.0
    # TTL eviction: parent-only non-first turn with a substantial rewrite
    # (w >= TTL_MIN_WRITE_TOKENS) AND an idle gap since the prior turn exceeding
    # the TTL the prior turn's cache was written with. The gap-vs-written-TTL
    # comparison alone is the proof: a gap longer than the written lifetime
    # guarantees THIS session's own cache expired, so any substantial write
    # right after it is TTL-caused waste, independent of the read count.
    # Deliberately carries no read-based condition - two were tried and
    # rejected. A strict r==0 check is masked by a shared system-prompt prefix:
    # when several sessions sharing that prefix resume after one idle gap, only
    # the first-resumed sibling re-warms it from scratch (r==0); the rest read
    # the now-warm prefix while still rewriting everything else (observed
    # 2026-07-11: three sessions resumed after 8.6h idle all carried an
    # identical r=24299 with w=202k-362k full rewrites, and only the fourth,
    # r=0 session raised the warning). A read:write ratio fixes that but breaks
    # on small sessions, where that same fixed ~24k shared-prefix read dwarfs a
    # modest rewrite and pushes the ratio back over any reasonable cutoff. The
    # write floor alone is what keeps a warm double-resume (large read, tiny
    # incidental write) and other trivial writes from tripping the gate.
    cur_ts = _parse_ts(entry.get("timestamp"))
    prev_ts = acc.get("last_turn_ts")
    prev_ttl = acc.get("last_turn_ttl_seconds") or TTL_1H_SECONDS
    idle_gap_exceeded = (
        prev_ts is not None and cur_ts is not None and (cur_ts - prev_ts) > prev_ttl
    )
    if (
        acc.get("track_evictions")
        and acc["assistant_turns"] > 1
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


def _typed_prompt_text(content):
    """Extract the user-typed text from a `message.content` value.

    Plain strings are the common shape, but Claude Code also emits genuine
    user prompts as content-block lists (`[{"type": "text", "text": ...}]`),
    so a list is not synonymous with a tool result. Tool results arrive as
    lists of `tool_result` blocks and carry no typed text; only non-empty
    `text` blocks count. Returns "" when there is no user-typed text.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return " ".join(part.strip() for part in parts if part.strip())


def _accumulate_user_prompt(entry, acc):
    """Count one typed user prompt per qualifying transcript line.

    Parent-transcript only (`track_user_prompts`, mirroring the eviction
    gate): a subagent transcript's user message is its task prompt, not a
    user turn of this session. Tool results arrive as `user` entries whose
    content is an all-`tool_result` block list, harness bookkeeping lines are
    flagged `isMeta`/`isSidechain`, and local slash-command output is a
    `<local-command-...>` wrapper; none of those are typed prompts.
    """
    if not acc.get("track_user_prompts"):
        return
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return
    content = (entry.get("message") or {}).get("content")
    text = _typed_prompt_text(content)
    if not text or text.startswith("<local-command"):
        return
    acc["user_prompts"] += 1


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
                _accumulate_user_prompt(entry, acc)
    except OSError:
        # Transcript became unreadable mid-walk; use the totals gathered so far
        # rather than failing the whole render.
        pass


def walk_transcript(path, include_subagents=False):
    """Sum cache/input/output tokens, compute cost, snapshot most-recent turn.

    Returns:
      cache_read, cache_write, input_total, output_total -- session sums
      read_cost, write_cost, input_cost, output_cost     -- the four $ components
                                                            (sum = token cost; lets
                                                            the full breakdown reconcile)
      cost                                               -- $, derived (parent + subagents)
      parent_cost, subagent_cost                         -- $ split (subagent_cost 0 unless include_subagents)
      last_model_id                                      -- model on most recent assistant turn
      last_input, last_cache_create, last_cache_read     -- usage of most recent turn
                                                            (used to derive ctx_used at "now")
      user_prompts                                       -- typed-prompt count, parent only
      assistant_turns                                    -- deduped assistant-turn count
                                                            (parent + subagents when included)

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
        "input_cost": 0.0,
        "output_cost": 0.0,
        "ttl_evictions": 0,
        "ttl_wasted": 0.0,
        "assistant_turns": 0,
        "user_prompts": 0,
        "track_evictions": False,
        "track_user_prompts": False,
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
        # User-prompt counting is parent-only too: a subagent transcript's
        # user message is its task prompt, not a turn the user typed.
        acc["track_evictions"] = True
        acc["track_user_prompts"] = True
        _walk_one_transcript(path, acc, seen_ids)
        parent_cost = acc["cost"]
        if include_subagents and path.endswith(".jsonl"):
            acc["track_evictions"] = False
            acc["track_user_prompts"] = False
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
        "input_cost": acc["input_cost"],
        "output_cost": acc["output_cost"],
        "ttl_evictions": acc["ttl_evictions"],
        "ttl_wasted": acc["ttl_wasted"],
        "parent_cost": parent_cost,
        "subagent_cost": acc["cost"] - parent_cost,
        "last_model_id": acc["last_model"],
        "last_input": acc["last_input"],
        "last_cache_create": acc["last_cache_create"],
        "last_cache_read": acc["last_cache_read"],
        "user_prompts": acc["user_prompts"],
        "assistant_turns": acc["assistant_turns"],
    }
