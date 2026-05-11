"""Shared rendering helpers for the schoen-claude-status statuslines.

Used by:
  statusline.py            -- main session: 2-line layout
  subagent_statusline.py   -- per-agent panel rows: 1-line metrics

Cost handling differs between the two callers:
  Main script reads `cost.total_cost_usd` from the stdin payload (authoritative,
  matches /usage). Subagent script must derive cost from the agent JSONL since
  the per-task fields don't include cost. The walker computes both in one pass
  so the subagent path doesn't need a second iteration.

Per-Mtok rates and the 1.25x cache-write multiplier match the canonical
constants documented in ~/.claude/CLAUDE.md ("Cost-estimation formula"). The
Opus 1M-context tier doubling (>200K context) is intentionally NOT modeled --
matches the main-script's accepted ~5-10% under-estimate for big-context Opus.
"""

import glob
import json
import os
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone

# orjson is 3-5x faster than stdlib json for the per-line parse that dominates
# the pace walk. Optional -- the walker (and walk_transcript) fall back cleanly.
try:
    from orjson import loads as _json_loads
except ImportError:
    _json_loads = json.loads

# --- ANSI colors -----------------------------------------------------------
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
ORANGE = "\x1b[38;5;208m"        # mid-tier between yellow and red
GREEN = "\x1b[32m"
RESET = "\x1b[0m"
# Identity colors (256-color) -- distinct from the threshold band so identity
# never reads as a warning.
CACHE_READ = "\x1b[38;5;38m"     # teal
CACHE_WRITE = ORANGE             # cache-write identity reuses the orange hue
CTX_DENOM = "\x1b[38;5;139m"     # soft mauve

# --- Pricing ---------------------------------------------------------------
# (input_per_mtok, output_per_mtok). Cache read = 0.1x input; cache write =
# 1.25x input (matches billing as of 2026-04-30; docs say 2.0x for 1h-TTL,
# empirically wrong).
_RATES = {
    "opus":   (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku":  (1.0,  5.0),
}


def _rates_for(model_id):
    mid = (model_id or "").lower()
    for key, rates in _RATES.items():
        if key in mid:
            return rates
    # Unknown family -- fall back to sonnet rates rather than zero so an
    # unrecognized model doesn't silently render as free.
    return _RATES["sonnet"]


def _cost_for_turn(usage, model_id):
    """Per-Mtok cost for one assistant turn's usage dict."""
    inp_rate, out_rate = _rates_for(model_id)
    i = int(usage.get("input_tokens") or 0)
    r = int(usage.get("cache_read_input_tokens") or 0)
    w = int(usage.get("cache_creation_input_tokens") or 0)
    o = int(usage.get("output_tokens") or 0)
    return (
        i * inp_rate
        + r * (inp_rate * 0.1)
        + w * (inp_rate * 1.25)
        + o * out_rate
    ) / 1_000_000.0


# --- Number/percentage formatting -----------------------------------------
def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1000:.1f}K"
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
      cost                                               -- $, derived
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

    if path and os.path.exists(path):
        process(path)
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


def format_cost(cost):
    if cost is None or cost <= 0:
        return ""
    color = RED if cost >= 50 else YELLOW if cost >= 25 else GREEN
    return f"{color}${cost:.2f}{RESET}"


# --- Beacon (live progress signal from the active turn) -----------------
_BEACON_DRIFT_COLOR = {"nominal": GREEN, "moderate": YELLOW, "material": RED}
_BEACON_STALE_SECONDS = 300

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
            capture_output=True, text=True, timeout=timeout,
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


def _find_begin_emitted_at(session_id):
    """Scan the session's JSONL for a beacon-pacing anchor timestamp.

    Returns the ISO-8601 timestamp of the most recent kind=begin beacon if
    one exists. If no begin beacon was ever emitted (older sessions that
    started pacing mid-stream with only `report` kinds), falls back to the
    FIRST beacon of any kind — the moment the agent started self-pacing in
    this session. Returns None if no beacons at all.

    Walker only exposes the LATEST beacon, but for the status line we want
    a wall-clock anchor to show "started at HH:MM (Nm elapsed)". Doing the
    scan in Python keeps walker's surface stable; the cost is one forward
    pass over the JSONL per render. JSONLs cap at single-digit MB in
    practice, so the scan is sub-100ms even on big sessions.
    """
    path = _find_session_jsonl(session_id)
    if not path:
        return None
    latest_begin_ts = None
    first_any_ts = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
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
                        if first_any_ts is None:
                            first_any_ts = ts
                        if beacon.get("kind") == "begin":
                            latest_begin_ts = ts
    except OSError:
        return None
    return latest_begin_ts or first_any_ts


def _format_clock_and_elapsed(begin_ts):
    """Convert an ISO-8601 begin timestamp to "HH:MM (Nm)" using local time.

    Returns None if the timestamp can't be parsed.
    """
    if not begin_ts:
        return None
    try:
        # Python's fromisoformat accepts the trailing Z suffix on 3.11+.
        normalized = begin_ts.replace("Z", "+00:00") if begin_ts.endswith("Z") else begin_ts
        dt = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    elapsed = (datetime.now(timezone.utc) - dt).total_seconds()
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

    drift = beacon.get("drift", "nominal")
    color = _BEACON_DRIFT_COLOR.get(drift, RESET)
    eta_seconds = beacon.get("eta_seconds") or 0
    eta_min = max(1, int(eta_seconds // 60))
    summary = (beacon.get("summary") or "")[:60]

    # Show begin-anchor (start clock + elapsed) so the user can sanity-check
    # ETA drift against wall time. If the JSONL scan can't find a begin, fall
    # back to the legacy "~Nm · summary" shape.
    anchor = _format_clock_and_elapsed(_find_begin_emitted_at(session_id))
    if anchor:
        return (f"{color}⏱ start {anchor} · ~{eta_min}m · {summary}{RESET}", beacon)
    return (f"{color}⏱ ~{eta_min}m · {summary}{RESET}", beacon)


def _bias_factor_cached(period_seconds):
    """Return (n_pairs, bias_factor) from beacons-history, file-cached.

    Beacons-history walks the full fleet, so per-render calls are wasteful.
    Cache TTL is short enough that fresh end-beacons influence the next
    render without a manual flush.
    """
    try:
        with open(_BIAS_CACHE_PATH, encoding="utf-8") as f:
            c = json.load(f)
        age = datetime.now(timezone.utc).timestamp() - c.get("computed_at_unix", 0)
        if age < _BIAS_CACHE_TTL_SECONDS and c.get("period_seconds") == period_seconds:
            return c.get("n_pairs", 0), c.get("bias_factor")
    except (OSError, ValueError, KeyError):
        pass

    data = _walker_subcommand(
        "beacons-history",
        "--period", str(int(period_seconds)),
        "--win-start", "0",
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
                    "computed_at_unix": datetime.now(timezone.utc).timestamp(),
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
        age = datetime.now(timezone.utc).timestamp() - c.get("computed_at_unix", 0)
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
                    "computed_at_unix": datetime.now(timezone.utc).timestamp(),
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
                "--period", str(int(period_seconds)),
                "--win-start", repr(float(win_start_unix)),
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
    now = datetime.now(timezone.utc).timestamp()
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
        reset_dt = datetime.fromtimestamp(resets_at_unix, tz=timezone.utc)
        remaining = (reset_dt - datetime.now(timezone.utc)).total_seconds()
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
                        (1.0 - in_window_weight) * trailing_delta
                        + in_window_weight * in_window_delta
                    )
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
        proj_part = _project_pace(util, w.get("resets_at"), period_seconds, use_trailing)
        parts.append(f"{label}: {pct_part}{proj_part}")
    return " ".join(parts)
