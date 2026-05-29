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
"""

import glob
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime

# NOTE: concurrent.futures (ProcessPoolExecutor) is imported lazily inside the
# only function that uses it (the multi-session parallel cost walk). Importing
# it at module load dragged in all of `multiprocessing` -- ~26ms on every
# statusline render, for a code path the per-render walk never touches.

# orjson is 3-5x faster than stdlib json for the per-line parse that dominates
# the pace walk. Optional -- the walker (and walk_transcript) fall back cleanly.
try:
    from orjson import loads as _json_loads
except ImportError:
    _json_loads = json.loads

# --- ANSI colors -----------------------------------------------------------
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
ORANGE = "\x1b[38;5;208m"  # mid-tier between yellow and red
GREEN = "\x1b[32m"
RESET = "\x1b[0m"
# Identity colors (256-color) -- distinct from the threshold band so identity
# never reads as a warning.
CACHE_READ = "\x1b[38;5;38m"  # teal
CACHE_WRITE = ORANGE  # cache-write identity reuses the orange hue
CTX_DENOM = "\x1b[38;5;139m"  # soft mauve

# --- Multi-session warning -------------------------------------------------
# Detects other Claude Code sessions running in the same cwd so the
# statusline can warn that a second interactive instance is active here.
#
# Enumerates `claude` processes whose own cwd matches and which are not in
# `-p` headless mode (Task subagents, scripted runs). Ground truth -- catches
# idle sessions, ignores ones that cleanly /exit'd a moment ago. Requires
# `psutil`; without it the badge stays off entirely (any mtime-based
# substitute false-positives for ~5 minutes after a clean /exit, which the
# 20s restart-handoff debounce can't suppress).

# psutil is imported lazily (in _resolve_psutil), not at module load: on a
# session-count cache hit -- the common case during a burst of renders -- we pay
# neither its ~22ms import nor the ~18ms process scan.
_psutil = None  # cached module handle within a process; None if unavailable.


def _resolve_psutil():
    """Import psutil on first use; return the module, or None if unavailable."""
    global _psutil
    if _psutil is None:
        try:
            import psutil as module
        except ImportError:
            return None
        _psutil = module
    return _psutil


# Process enumeration is the most expensive thing the statusline does per render
# (~40ms incl. the psutil import). The count only changes when a Claude session
# starts/stops in this cwd -- far slower than the render cadence -- so memoize it
# on disk with a short TTL. This is NOT output caching: the badge is still
# re-derived from this count + the live debounce every render; we only skip a
# redundant OS scan whose answer cannot have changed within the TTL window.
_SESSION_COUNT_CACHE_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", ".statusline-sessioncount-cache.json"
)
_SESSION_COUNT_CACHE_TTL_SECONDS = 8
_SESSION_COUNT_CACHE_MAX_AGE_SECONDS = 86400  # prune entries older than a day


def _load_session_count_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_session_count_cache(path, cache, now):
    pruned = {
        k: v
        for k, v in cache.items()
        if isinstance(v, dict)
        and (now - v.get("ts", 0)) <= _SESSION_COUNT_CACHE_MAX_AGE_SECONDS
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(pruned, f)
    except OSError:
        pass


def count_active_sessions(
    cwd, *, now=None, cache_path=None, ttl=_SESSION_COUNT_CACHE_TTL_SECONDS
):
    """Return how many interactive Claude sessions are running in `cwd`.

    Memoized on disk for `ttl` seconds keyed by cwd. Returns 0 when psutil is
    unavailable, `cwd` is empty, or any error occurs -- never raises (statusline
    rendering must not crash).
    """
    if not cwd:
        return 0
    now = time.time() if now is None else now
    path = cache_path or _SESSION_COUNT_CACHE_PATH
    key = os.path.normcase(cwd)

    cache = _load_session_count_cache(path)
    entry = cache.get(key)
    # Clock-skew guard: a future-stamped entry (now - ts < 0) is treated as a
    # miss so a backwards clock jump can't pin a stale count indefinitely.
    if isinstance(entry, dict) and 0 <= (now - entry.get("ts", 0)) < ttl:
        return int(entry.get("count", 0))

    psutil = _resolve_psutil()
    if psutil is None:
        return 0
    try:
        count = _count_via_psutil(cwd, psutil)
    except Exception:
        return 0
    cache[key] = {"count": count, "ts": now}
    _save_session_count_cache(path, cache, now)
    return count


def _process_matches(name, cmdline, cwd, target_cwd):
    """Pure classifier: does this (name, cmdline, cwd) tuple represent an
    interactive Claude session rooted at `target_cwd`? Extracted so unit
    tests don't need a live or mocked psutil."""
    n = (name or "").lower()
    if n not in ("claude", "claude.exe", "node", "node.exe"):
        return False
    cl = cmdline or ()
    if n in ("node", "node.exe") and not any(
        "claude" in (arg or "").lower() for arg in cl
    ):
        return False
    # -p / --print is the headless mode used by Task subagents and scripted runs.
    if "-p" in cl or "--print" in cl:
        return False
    if not cwd:
        return False
    return os.path.normcase(cwd) == os.path.normcase(target_cwd)


def _count_via_psutil(target_cwd, psutil):
    count = 0
    for p in psutil.process_iter(["name"]):
        name = (p.info.get("name") or "").lower()
        # Cheap name pre-filter -- avoids calling cmdline()/cwd() on every
        # process (hundreds on a typical box).
        if name not in ("claude", "claude.exe", "node", "node.exe"):
            continue
        try:
            cmdline = p.cmdline()
            pcwd = p.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if _process_matches(name, cmdline, pcwd, target_cwd):
            count += 1
    return count


# --- Multi-session badge debounce -----------------------------------------
# `count_active_sessions` reports live process truth, but a restart produces a
# brief handoff overlap: the old `claude` process is still winding down when
# the new one spins up, so for a few seconds two processes legitimately match
# the cwd. Painting `[2 sessions]` for that blip is noise. We suppress the
# badge until an elevated (>= 2) count has *persisted* for the dwell window.
#
# The statusline re-renders only when a turn is processed, so the dwell is
# timed against a stored wall-clock timestamp, never a render count. State is
# a small JSON file keyed by cwd: {cwd: {"first": ts, "last": ts}}. It is
# re-derived from live truth every render, so unlike a cache it can't drift --
# a wrong entry self-corrects on the next render. A gap longer than
# `_SESSION_DEBOUNCE_GAP_SECONDS` since the last elevated observation means the
# previous episode's clearing render was missed (lazy refresh), so we treat
# the new observation as a fresh episode and re-arm rather than trust a stale
# "first" stamp.
_SESSION_DEBOUNCE_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", ".statusline-session-debounce.json"
)
_SESSION_DEBOUNCE_DWELL_SECONDS = 20
_SESSION_DEBOUNCE_GAP_SECONDS = 30
_SESSION_DEBOUNCE_MAX_AGE_SECONDS = 86400  # prune entries older than a day


def _load_debounce_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_debounce_state(path, state, now):
    pruned = {
        k: v
        for k, v in state.items()
        if isinstance(v, dict)
        and (now - v.get("last", 0)) <= _SESSION_DEBOUNCE_MAX_AGE_SECONDS
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(pruned, f)
    except OSError:
        pass


def debounce_session_count(
    raw_count,
    cwd,
    *,
    now=None,
    state_path=None,
    dwell_seconds=_SESSION_DEBOUNCE_DWELL_SECONDS,
    gap_seconds=_SESSION_DEBOUNCE_GAP_SECONDS,
):
    """Return the session count to *display*, suppressing brief restart blips.

    Reports `raw_count` unchanged once an elevated (>= 2) count has persisted
    for `dwell_seconds`; until then an elevated count is reported as 1 so the
    badge stays quiet. Counts below 2 pass straight through and clear any
    tracked episode. Returns `raw_count` unchanged when `cwd` is empty (no key
    to track state by). Never raises -- statusline rendering must not crash.
    """
    now = time.time() if now is None else now
    key = os.path.normcase(cwd or "")
    if not key:
        return raw_count
    path = state_path or _SESSION_DEBOUNCE_PATH
    state = _load_debounce_state(path)
    entry = state.get(key)

    if raw_count < 2:
        if entry is not None:
            state.pop(key, None)
            _save_debounce_state(path, state, now)
        return raw_count

    # raw_count >= 2: continue an in-progress episode, or start a fresh one.
    if not isinstance(entry, dict) or (now - entry.get("last", 0)) > gap_seconds:
        entry = {"first": now, "last": now}
    else:
        entry = {"first": entry.get("first", now), "last": now}
    state[key] = entry
    _save_debounce_state(path, state, now)

    if now - entry["first"] >= dwell_seconds:
        return raw_count
    return 1


# --- Pricing ---------------------------------------------------------------
# (input_per_mtok, output_per_mtok). Cache read = 0.1x input; cache write =
# 1.25x input (matches billing as of 2026-04-30; docs say 2.0x for 1h-TTL,
# empirically wrong).
_RATES = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}

# Server-side web search is billed per request: $10 / 1,000 = $0.01 each.
# Verified against ~/.claude.json lastModelUsage.costUSD -- adding this term
# closes the 30-45% under-count on search-heavy (haiku) sessions to exact 1.000.
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


# --- Number/percentage formatting -----------------------------------------
def fmt(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1000:.1f}K"
    return str(int(n))


def color_high_bad(pct, warn, danger, decimals=0):
    """Higher is worse (e.g. ctx %, quota %). >= warn -> yellow, >= danger -> red."""
    c = RED if pct >= danger else YELLOW if pct >= warn else GREEN
    spec = f".{decimals}f"
    return f"{c}{format(pct, spec)}%{RESET}"


def color_high_good(pct, warn, danger, decimals=0):
    """Higher is better (e.g. cache hit %). < warn -> yellow, < danger -> red."""
    c = RED if pct < danger else YELLOW if pct < warn else GREEN
    spec = f".{decimals}f"
    return f"{c}{format(pct, spec)}%{RESET}"


# --- Transcript walker ----------------------------------------------------
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
    read_total = write_total = input_total = output_total = 0
    cost_total = 0.0
    last_model = ""
    last_input = last_cache_create = last_cache_read = 0
    seen_ids = set()

    def process(p):
        nonlocal read_total, write_total, input_total, output_total
        nonlocal cost_total, last_model, last_input, last_cache_create, last_cache_read
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = _json_loads(line)
                    except Exception:
                        continue
                    msg = e.get("message") or {}
                    if msg.get("role") != "assistant":
                        continue
                    mid = msg.get("id")
                    if mid:
                        # transcripts repeat assistant turns under one message.id
                        # (snapshots/checkpoints carry the same usage); count once.
                        if mid in seen_ids:
                            continue
                        seen_ids.add(mid)
                    u = msg.get("usage") or {}
                    r = int(u.get("cache_read_input_tokens") or 0)
                    w = int(u.get("cache_creation_input_tokens") or 0)
                    i = int(u.get("input_tokens") or 0)
                    o = int(u.get("output_tokens") or 0)
                    read_total += r
                    write_total += w
                    input_total += i
                    output_total += o
                    model_id = msg.get("model") or ""
                    if model_id:
                        last_model = model_id
                    cost_total += _cost_for_turn(u, model_id or last_model)
                    last_input = i
                    last_cache_create = w
                    last_cache_read = r
        except OSError:
            pass

    parent_cost = 0.0
    if path and os.path.exists(path):
        process(path)
        # Snapshot before subagents so parent and subagent cost can be reported
        # separately: the main statusline shows the authoritative parent figure
        # plus our own subagent estimate.
        parent_cost = cost_total
        if include_subagents and path.endswith(".jsonl"):
            sub_dir = path[:-6] + "/subagents"
            if os.path.isdir(sub_dir):
                for sub in glob.glob(os.path.join(sub_dir, "agent-*.jsonl")):
                    process(sub)

    return {
        "read": read_total,
        "write": write_total,
        "input": input_total,
        "output": output_total,
        "cost": cost_total,
        "parent_cost": parent_cost,
        "subagent_cost": cost_total - parent_cost,
        "last_model_id": last_model,
        "last_input": last_input,
        "last_cache_create": last_cache_create,
        "last_cache_read": last_cache_read,
    }


# --- Field formatters -----------------------------------------------------
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
        try:
            compact_tokens = int(window_size * float(override) / 100)
        except ValueError:
            pass
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


# --- Model badge ----------------------------------------------------------
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


# --- Beacon (live progress signal from the active turn) -----------------
_BEACON_DRIFT_COLOR = {"nominal": GREEN, "moderate": YELLOW, "material": RED}
_BEACON_STALE_SECONDS = 300

# Drift thresholds. ratio = (elapsed_so_far + current_eta) / original_begin_eta.
# Anchored on observed reality, not the agent's self-assessment — historical
# data showed agents never self-reported moderate or material, even on
# lifecycles that ended up 2-10× over the begin estimate (the lowballed-and-
# kept-lowballing pattern). 30-min elapsed cap matches the original SKILL
# guidance: long absolute durations are material regardless of ratio.
_DRIFT_MODERATE_RATIO = 1.5
_DRIFT_MATERIAL_RATIO = 2.0
_DRIFT_MATERIAL_ELAPSED_SECONDS = 1800


def _compute_objective_drift(begin_ts, begin_eta_seconds, current_eta_seconds):
    """Classify drift from elapsed + current eta vs original begin eta.

    Returns "nominal" / "moderate" / "material". Falls back to "nominal"
    when inputs are insufficient (no begin anchor, no begin eta, or eta
    not parseable) — better to under-color than to flash red on missing
    data.
    """
    if not begin_ts or not begin_eta_seconds or begin_eta_seconds <= 0:
        return "nominal"
    try:
        normalized = (
            begin_ts.replace("Z", "+00:00") if begin_ts.endswith("Z") else begin_ts
        )
        begin_dt = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return "nominal"
    if begin_dt.tzinfo is None:
        begin_dt = begin_dt.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - begin_dt).total_seconds()
    if elapsed < 0:
        elapsed = 0
    if elapsed > _DRIFT_MATERIAL_ELAPSED_SECONDS:
        return "material"
    try:
        eta = float(current_eta_seconds or 0)
    except (TypeError, ValueError):
        eta = 0.0
    ratio = (elapsed + max(0.0, eta)) / begin_eta_seconds
    if ratio >= _DRIFT_MATERIAL_RATIO:
        return "material"
    if ratio >= _DRIFT_MODERATE_RATIO:
        return "moderate"
    return "nominal"


_BIAS_CACHE_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", ".statusline-bias-cache.json"
)
_BIAS_CACHE_TTL_SECONDS = 60
_CALIBRATION_MIN_PAIRS = 20


def _walker_subcommand(subcommand, *args, timeout=2):
    """Invoke a claude-walker subcommand. Return parsed JSON or None on any error."""
    bin_path = _find_walker_binary()
    if not bin_path:
        return None
    try:
        result = subprocess.run(
            [bin_path, subcommand, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return _json_loads(result.stdout)
    except (ValueError, TypeError):
        return None


import re as _re

_BEACON_BLOCK_RE = _re.compile(
    r"<progress-beacon>\s*(\{.*?\})\s*</progress-beacon>", _re.DOTALL
)


def _find_session_jsonl(session_id):
    """Locate the JSONL transcript for `session_id` across project dirs."""
    if not session_id:
        return None
    home = os.path.expanduser("~")
    pattern = os.path.join(home, ".claude", "projects", "*", f"{session_id}.jsonl")
    for path in glob.glob(pattern):
        return path
    return None


def _find_beacon_anchors(session_id):
    """Scan the session's JSONL for the active lifecycle's anchors.

    Returns (turn_anchor_ts, step_anchor_ts, begin_eta_seconds):
      turn_anchor_ts — ISO-8601 timestamp of the most recent kind=begin beacon,
        or None if the session never emitted one. Surfaced by the status line
        as an explicit `no begin` error rather than silently anchoring to the
        first non-begin beacon (that fallback masked agents skipping begin).
      step_anchor_ts — ISO-8601 timestamp of the most recent kind=report
        beacon that was emitted AFTER turn_anchor_ts. None if no report has
        fired in the current lifecycle. Drives the "step HH:MM (Mm)" mid-turn
        anchor so the user sees motion as the agent progresses through
        sub-tasks within a turn.
      begin_eta_seconds — `eta_seconds` from the most recent kind=begin beacon,
        used as the original-estimate denominator when the status line
        computes objective drift from elapsed-vs-original. None if no begin
        is in flight or it carried a non-positive eta.

    Walker only exposes the LATEST beacon, but for the status line we want
    wall-clock anchors. Doing the scan in Python keeps walker's surface
    stable; the cost is one forward pass over the JSONL per render. JSONLs
    cap at single-digit MB in practice, so the scan is sub-100ms even on
    big sessions.
    """
    path = _find_session_jsonl(session_id)
    if not path:
        return (None, None, None)
    latest_begin_ts = None
    latest_report_ts = None
    latest_begin_eta = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    evt = _json_loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(evt, dict) or evt.get("type") != "assistant":
                    continue
                ts = evt.get("timestamp")
                if not ts:
                    continue
                msg = evt.get("message") or {}
                content = msg.get("content") or []
                if not isinstance(content, list):
                    continue
                for chunk in content:
                    if not isinstance(chunk, dict) or chunk.get("type") != "text":
                        continue
                    text = chunk.get("text") or ""
                    if "<progress-beacon>" not in text:
                        continue
                    for match in _BEACON_BLOCK_RE.finditer(text):
                        try:
                            beacon = _json_loads(match.group(1))
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(beacon, dict):
                            continue
                        kind = beacon.get("kind")
                        if kind == "begin":
                            latest_begin_ts = ts
                            # New begin resets the step anchor — any reports
                            # before this begin belonged to a closed lifecycle.
                            latest_report_ts = None
                            eta = beacon.get("eta_seconds")
                            try:
                                eta_val = float(eta) if eta is not None else 0.0
                            except (TypeError, ValueError):
                                eta_val = 0.0
                            latest_begin_eta = eta_val if eta_val > 0 else None
                        elif kind == "report":
                            # Only track reports that fall within the current
                            # begin's lifecycle (i.e., after the latest begin).
                            if latest_begin_ts is not None:
                                latest_report_ts = ts
                        elif kind == "end":
                            # end closes the lifecycle. Any later reports
                            # without a fresh begin are orphans — surface
                            # them as "no begin" rather than carrying a stale
                            # turn anchor across the turn boundary.
                            latest_begin_ts = None
                            latest_report_ts = None
                            latest_begin_eta = None
    except OSError:
        return (None, None, None)
    return (latest_begin_ts, latest_report_ts, latest_begin_eta)


def _format_clock_and_elapsed(begin_ts):
    """Convert an ISO-8601 begin timestamp to "HH:MM (Nm)" using local time.

    Returns None if the timestamp can't be parsed.
    """
    if not begin_ts:
        return None
    try:
        # Python's fromisoformat accepts the trailing Z suffix on 3.11+.
        normalized = (
            begin_ts.replace("Z", "+00:00") if begin_ts.endswith("Z") else begin_ts
        )
        dt = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone()
    elapsed = (datetime.now(UTC) - dt).total_seconds()
    if elapsed < 0:
        elapsed = 0
    elapsed_min = max(0, int(elapsed) // 60)
    return f"{local:%H:%M} ({elapsed_min}m)"


def format_beacon(session_id):
    """Render the live beacon column for `session_id`.

    Returns (rendered_str | None, beacon_dict | None). None means the
    column should be hidden (no session, no beacon, kind=end, or walker
    unavailable). Stale beacons (>5 min old) render as "⏱ stale Nm" in
    red so the user can tell the agent has gone quiet on its own promise.
    """
    if not session_id:
        return (None, None)
    data = _walker_subcommand("beacons-latest", "--session-id", session_id)
    if not data:
        return (None, None)
    beacon = data.get("beacon")
    if not beacon or beacon.get("kind") == "end":
        return (None, None)

    age = data.get("age_seconds")
    if age is not None and age > _BEACON_STALE_SECONDS:
        minutes = max(0, int(age) // 60)
        return (f"{RED}⏱ stale {minutes}m{RESET}", beacon)

    eta_seconds = beacon.get("eta_seconds") or 0
    eta_min = max(1, int(eta_seconds // 60))
    summary = (beacon.get("summary") or "")[:60]

    turn_ts, step_ts, begin_eta = _find_beacon_anchors(session_id)
    drift = _compute_objective_drift(turn_ts, begin_eta, eta_seconds)
    color = _BEACON_DRIFT_COLOR.get(drift, RESET)
    turn_anchor = _format_clock_and_elapsed(turn_ts)
    step_anchor = _format_clock_and_elapsed(step_ts)
    if turn_anchor and step_anchor:
        return (
            f"{color}⏱ turn {turn_anchor} · step {step_anchor} · ~{eta_min}m · {summary}{RESET}",
            beacon,
        )
    if turn_anchor:
        return (f"{color}⏱ turn {turn_anchor} · ~{eta_min}m · {summary}{RESET}", beacon)
    return (f"{RED}⏱ no begin · ~{eta_min}m · {summary}{RESET}", beacon)


def _bias_factor_cached(period_seconds):
    """Return (n_pairs, bias_factor) from beacons-history, file-cached.

    Beacons-history walks the full fleet, so per-render calls are wasteful.
    Cache TTL is short enough that fresh end-beacons influence the next
    render without a manual flush.
    """
    try:
        with open(_BIAS_CACHE_PATH, encoding="utf-8") as f:
            c = json.load(f)
        age = datetime.now(UTC).timestamp() - c.get("computed_at_unix", 0)
        if age < _BIAS_CACHE_TTL_SECONDS and c.get("period_seconds") == period_seconds:
            return c.get("n_pairs", 0), c.get("bias_factor")
    except (OSError, ValueError, KeyError):
        pass

    data = _walker_subcommand(
        "beacons-history",
        "--period",
        str(int(period_seconds)),
        "--win-start",
        "0",
        timeout=5,
    )
    if not data:
        return 0, None
    n_pairs = data.get("n_pairs", 0)
    bias_factor = data.get("bias_factor")
    try:
        with open(_BIAS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "computed_at_unix": datetime.now(UTC).timestamp(),
                    "period_seconds": period_seconds,
                    "n_pairs": n_pairs,
                    "bias_factor": bias_factor,
                },
                f,
            )
    except OSError:
        pass
    return n_pairs, bias_factor


def format_calibrated_eta(raw_eta_seconds, period_seconds=604800):
    """Render the calibrated-ETA line, or None if too few pairs to calibrate.

    Multiplies `raw_eta_seconds` by a bias factor derived from a 7-day
    median of (actual_elapsed / begin_eta) ratios across the user's fleet.
    Gated on n_pairs >= 20 so a handful of outlier sessions can't bias
    the figure on a fresh install.
    """
    if not raw_eta_seconds or raw_eta_seconds <= 0:
        return None
    n_pairs, bias = _bias_factor_cached(period_seconds)
    if n_pairs < _CALIBRATION_MIN_PAIRS or bias is None:
        return None
    calibrated = float(raw_eta_seconds) * float(bias)
    cal_min = max(1, int(calibrated // 60))
    return f"~{cal_min}m calibrated ({float(bias):.1f}×)"


# --- Quota (main script only) --------------------------------------------
def _fmt_delta_hours(seconds):
    sign = "+" if seconds >= 0 else "-"
    return f"{sign}{abs(seconds) / 3600:.1f}h"


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
        pass
    return trailing, window


# Optional native walker (~/claude-walker, separate repo). Honors the
# CLI/output contract documented in claude-walker/SPEC.md. Cuts the cold
# pace walk from ~250ms parallel Python to ~80-180ms single-process; the
# Python implementation below stays as the fallback when the binary isn't
# present or fails for any reason.
_WALKER_BIN_ENV = "CLAUDE_WALKER_BIN"


def _find_walker_binary():
    """Locate the optional native walker. Returns absolute path or None.

    Search order: $CLAUDE_WALKER_BIN, the canonical claude-walker C++ build
    location under $HOME, then PATH (in case the user installed it elsewhere).
    """
    override = os.environ.get(_WALKER_BIN_ENV)
    if override and os.path.isfile(override):
        return override
    home = os.path.expanduser("~")
    for relative in (
        # MSVC multi-config (default on Windows)
        os.path.join("claude-walker", "cpp", "build", "Release", "walker.exe"),
        # Ninja/MinGW single-config on Windows
        os.path.join("claude-walker", "cpp", "build", "walker.exe"),
        # Single-config on Linux/macOS
        os.path.join("claude-walker", "cpp", "build", "walker"),
    ):
        path = os.path.join(home, relative)
        if os.path.isfile(path):
            return path
    # Canonical install name (`claude-walker`) takes precedence over the
    # legacy `walker` lookup so a system-installed binary wins over an old
    # checkout that happens to be on PATH.
    for name in ("claude-walker.exe", "claude-walker", "walker.exe", "walker"):
        which = shutil.which(name)
        if which:
            return which
    return None


_WALKER_ROOTS_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "walker-roots.json"
)


def _walker_root_list():
    """Default root + extras from walker-roots.json. Mirrors C++ resolve_roots.

    Failure modes match the SPEC: missing file => no extras; malformed JSON =>
    stderr message + no extras. Only directories that exist on disk make it
    into the result. Realpath-deduped.
    """
    home = os.path.expanduser("~")
    default = os.path.join(home, ".claude", "projects")
    all_paths = [default]
    try:
        with open(_WALKER_ROOTS_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        extras = cfg.get("extra_roots") or []
        if isinstance(extras, list):
            all_paths.extend(str(p) for p in extras if isinstance(p, str) and p)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        import sys

        print(
            f"statusline_lib: ignoring malformed {_WALKER_ROOTS_CONFIG_PATH}: {exc}",
            file=sys.stderr,
        )

    seen = set()
    result = []
    for p in all_paths:
        try:
            canon = os.path.realpath(p)
        except OSError:
            canon = os.path.normpath(p)
        if not os.path.isdir(canon):
            continue
        if canon in seen:
            continue
        seen.add(canon)
        result.append(canon)
    return result


def _walk_pace_buckets_native(period_seconds, win_start_unix):
    """Try the native walker. Returns (trailing, window) or None on any failure.

    Per SPEC.md the binary either exits 0 with one JSON line on stdout or the
    caller falls back -- so any non-zero exit, parse error, or missing field
    drops cleanly to the Python implementation.
    """
    bin_path = _find_walker_binary()
    if not bin_path:
        return None
    try:
        out = subprocess.run(
            [
                bin_path,
                "--period",
                str(int(period_seconds)),
                "--win-start",
                repr(float(win_start_unix)),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout)
    except (ValueError, TypeError):
        return None
    trailing = data.get("trailing_usd")
    window = data.get("window_usd")
    if trailing is None or window is None:
        return None
    try:
        return float(trailing), float(window)
    except (TypeError, ValueError):
        return None


def _walk_session_group(paths, period_cutoff, win_start_unix):
    """Walk one parent+subagents group, return (trailing_dollars, window_dollars).

    Module-level so ProcessPoolExecutor can serialize a reference to it.

    Sequential within a group so the dedup set catches the parent <->
    auto-compact-subagent message.id overlap (the only collision pattern that
    actually appears -- 146 instances in the working corpus, all parent-vs-its-
    own-acompact-subagent). Cross-session collisions weren't observed and are
    not defended against here; if they ever appear in real data the cost
    impact would still round to zero.
    """
    earliest = min(period_cutoff, win_start_unix)
    trailing = window_cost = 0.0
    seen_ids = set()
    for path in paths:
        last_model = ""
        try:
            with open(path, "rb") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        e = _json_loads(line)
                    except Exception:
                        continue
                    msg = e.get("message") or {}
                    if msg.get("role") != "assistant":
                        continue
                    mid = msg.get("id")
                    if mid:
                        if mid in seen_ids:
                            continue
                        seen_ids.add(mid)
                    ts_str = e.get("timestamp")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        ).timestamp()
                    except (ValueError, TypeError):
                        continue
                    if ts < earliest:
                        continue
                    model_id = msg.get("model") or ""
                    if model_id:
                        last_model = model_id
                    c = _cost_for_turn(msg.get("usage") or {}, model_id or last_model)
                    if ts >= period_cutoff:
                        trailing += c
                    if ts >= win_start_unix:
                        window_cost += c
        except OSError:
            continue
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

    # Group by (slug, session_id) so each work unit owns its own dedup set.
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

    if not groups:
        return 0.0, 0.0

    # Inline walk if the parallelism win wouldn't beat process-pool startup.
    if len(groups) <= 2:
        trailing = window_cost = 0.0
        for paths in groups.values():
            t, w = _walk_session_group(paths, period_cutoff, win_start_unix)
            trailing += t
            window_cost += w
        return trailing, window_cost

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
        for paths in groups.values():
            t, w = _walk_session_group(paths, period_cutoff, win_start_unix)
            trailing += t
            window_cost += w
    return trailing, window_cost


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
